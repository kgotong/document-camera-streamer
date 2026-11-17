"""
Document Camera Streaming Server
================================
Streams a USB document camera over the network via:
  - MJPEG HTTP stream  (GET /stream)
  - WebSocket frames   (WS /ws)
  - Snapshot endpoint  (GET /snapshot)
  - Camera info        (GET /api/info)
  - Camera list        (GET /api/cameras)
  - Camera select      (POST /api/camera/{index})
  - Resolution list    (GET /api/resolutions)
  - Resolution change  (POST /api/resolution)

All viewers share one capture thread (see ``camera.py``); handlers only await
the newest published frame, so the event loop is never blocked by device I/O.
Byte counters on both stream endpoints feed the viewer's network meter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.concurrency import run_in_threadpool

    from camera import (
        CAPTURE_RESOLUTIONS,
        DocumentCamera,
        detect_desktop_resolution,
        enumerate_cameras,
        fit_resolution,
        identify_cameras,
        parse_resolution,
        probe_resolutions,
        resolution_label,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - environment problem
    missing = exc.name or "a dependency"
    raise SystemExit(
        f"Cannot start: the module '{missing}' is not installed for this interpreter.\n"
        f"  Interpreter: {sys.executable}\n"
        "\n"
        "Install the dependencies with one of:\n"
        r"  .venv\Scripts\python -m pip install -r requirements.txt   (project virtualenv)"
        "\n"
        "  python -m pip install -r requirements.txt"
        "\n"
        "Or use the launcher, which picks the virtualenv automatically:\n"
        "  run.cmd"
    ) from None

# ---------------------------------------------------------------------------
# Configuration (environment defaults; command line overrides them)
# ---------------------------------------------------------------------------
HOST = os.getenv("DOCSTREAM_HOST", "0.0.0.0")
PORT = int(os.getenv("DOCSTREAM_PORT", "8080"))
MAX_VIEWERS = int(os.getenv("DOCSTREAM_MAX_VIEWERS", "8"))
MJPEG_BOUNDARY = "frameboundary"
FRAME_WAIT_TIMEOUT = 2.0        # seconds a viewer waits for the next frame
SNAPSHOT_WAIT_TIMEOUT = 2.0
SNAPSHOT_MAX_AGE = 2.0
DEVICE_PINNED = "DOCSTREAM_DEVICE_INDEX" in os.environ

logging.basicConfig(
    level=os.getenv("DOCSTREAM_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("streamserver")

camera = DocumentCamera(
    device_index=int(os.getenv("DOCSTREAM_DEVICE_INDEX", "0")),
    width=int(os.getenv("DOCSTREAM_WIDTH", "1920")),
    height=int(os.getenv("DOCSTREAM_HEIGHT", "1080")),
    fps=int(os.getenv("DOCSTREAM_FPS", "30")),
)

# Desktop resolution of the machine running the server; used by --desktop-fit
# and reported to the viewer so it can size the image sensibly.
_display: Optional[Tuple[int, int]] = None

STARTED_AT = time.monotonic()


class ViewerCounter:
    """Bounded counter for concurrent stream clients."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0

    def acquire(self) -> bool:
        if self.active >= self.limit:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


viewers = ViewerCounter(MAX_VIEWERS)


class ThroughputMeter:
    """Rolling byte-rate estimate for one streaming endpoint.

    Only events inside a short window are kept, so the reading tracks what is
    happening now (a resolution change moves it immediately) instead of
    averaging across the whole session.
    """

    def __init__(self, window: float = 3.0) -> None:
        self.window = window
        self._lock = threading.Lock()
        self._events: Deque[Tuple[float, int]] = deque()
        self._total = 0

    def add(self, nbytes: int) -> None:
        """Record bytes handed to a client."""
        if nbytes <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._events.append((now, int(nbytes)))
            self._total += int(nbytes)
            self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def stats(self) -> Dict[str, int]:
        """``bytes_per_second`` over the window plus the lifetime total."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            total = self._total
            if not self._events:
                return {"bytes_per_second": 0, "bytes_sent": total}
            recent = sum(nbytes for _ts, nbytes in self._events)
            # Never divide by less than half a second: a single burst would
            # otherwise be reported as an absurd rate.
            covered = max(now - self._events[0][0], 0.5)
        return {"bytes_per_second": int(recent / covered), "bytes_sent": total}


stream_meter = ThroughputMeter()
ws_meter = ThroughputMeter()


# ---------------------------------------------------------------------------
# Shared helpers (all synchronous device work happens in a worker thread)
# ---------------------------------------------------------------------------
def _uptime() -> float:
    return round(time.monotonic() - STARTED_AT, 1)


def _skip_index() -> Optional[int]:
    """Index to keep out of device probes while it is actively streaming."""
    if camera.get_info()["state"] in ("streaming", "switching", "reconnecting"):
        return int(camera.device_index)
    return None


def list_cameras_sync(force: bool = False) -> List[Dict[str, Any]]:
    """Camera list, including the device currently in use."""
    active = _skip_index()
    cameras = enumerate_cameras(force=force, skip_index=active)

    # Only describe the in-use device when it is genuinely active; otherwise an
    # unreachable configured index would show up as a phantom camera.
    if active is not None and all(cam["index"] != active for cam in cameras):
        info = camera.get_info()
        cameras.insert(0, {
            "index": active,
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"],
            "backend": info["backend"],
            "name": None,
            "name_confidence": "in-use",
            "is_document_camera": False,
        })

    state = camera.get_info()["state"]
    for cam in cameras:
        cam["in_use"] = active is not None and cam["index"] == active and state == "streaming"
    cameras.sort(key=lambda cam: cam["index"])
    return cameras


def display_info() -> Optional[Dict[str, int]]:
    """Desktop resolution of the server machine, when known."""
    if _display is None:
        return None
    return {"width": int(_display[0]), "height": int(_display[1])}


def info_payload() -> Dict[str, Any]:
    info = camera.get_info()
    stream = stream_meter.stats()
    ws = ws_meter.stats()
    info.update({
        "viewers": viewers.active,
        "max_viewers": viewers.limit,
        "uptime_s": _uptime(),
        "display": display_info(),
        "network": {
            "stream_bps": stream["bytes_per_second"],
            "websocket_bps": ws["bytes_per_second"],
            "total_bps": stream["bytes_per_second"] + ws["bytes_per_second"],
            "bytes_sent": stream["bytes_sent"] + ws["bytes_sent"],
            "frame_bytes": info.get("frame_bytes"),
        },
    })
    return info


def _resolution_entries(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Catalog modes for the picker, annotated with what the camera produced."""
    tested = {
        (mode["width"], mode["height"]): mode
        for mode in camera.negotiated_modes(info.get("device_index"))
    }
    actual = (int(info["width"]), int(info["height"]))
    entries: List[Dict[str, Any]] = []
    for width, height in CAPTURE_RESOLUTIONS:
        mode = tested.get((width, height))
        entries.append({
            "width": width,
            "height": height,
            "label": resolution_label(width, height),
            "current": actual == (width, height),
            "tested": None if mode is None else {
                "exact": mode["exact"],
                "actual": f"{mode['actual_width']}x{mode['actual_height']}",
            },
        })
    # A driver can land on a mode outside the catalog; keep it selectable.
    if actual[0] > 0 and actual[1] > 0 and actual not in set(CAPTURE_RESOLUTIONS):
        entries.insert(0, {
            "width": actual[0],
            "height": actual[1],
            "label": resolution_label(*actual) + " — reported by camera",
            "current": True,
            "tested": None,
        })
    return entries


def resolution_payload() -> Dict[str, Any]:
    """Current capture mode plus every mode the viewer can request."""
    info = camera.get_info()
    actual = (int(info["width"]), int(info["height"]))
    target = (int(info["target_width"]), int(info["target_height"]))
    return {
        "current": {
            "width": actual[0],
            "height": actual[1],
            "target_width": target[0],
            "target_height": target[1],
            "exact": actual == target,
            "state": info["state"],
        },
        "resolutions": _resolution_entries(info),
    }


def _snapshot_bytes() -> Optional[bytes]:
    """Newest frame if fresh, otherwise wait briefly for a new one."""
    seq, jpeg, age = camera.latest_frame_with_age()
    if jpeg is not None and age is not None and age <= SNAPSHOT_MAX_AGE:
        return jpeg
    result = camera.wait_frame(seq, SNAPSHOT_WAIT_TIMEOUT)
    return result[1] if result else None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _display

    logger.info("Starting Document Camera Streaming Server")
    if _display is None:
        _display = await run_in_threadpool(detect_desktop_resolution)
    if _display is not None:
        logger.info(
            "Desktop resolution %dx%d; capture target %dx%d",
            _display[0], _display[1], camera.target_width, camera.target_height,
        )
    else:
        logger.info(
            "Desktop resolution not detected; capture target %dx%d",
            camera.target_width, camera.target_height,
        )

    detected = await run_in_threadpool(identify_cameras)
    cameras = detected["cameras"]
    logger.info(
        "Detected %d camera(s) (name confidence: %s)",
        len(cameras), detected["names_confidence"],
    )
    for cam in cameras:
        logger.info(
            "  [%s] %s %dx%d backend=%s document_camera=%s",
            cam["index"], cam.get("name") or "(unnamed)",
            cam["width"], cam["height"], cam["backend"],
            cam.get("is_document_camera", False),
        )

    if not cameras:
        logger.warning(
            "No camera detected. Common causes: another app is using it "
            "(Teams, Zoom, a browser tab), Windows camera access is disabled, "
            "or the process is running without device access. "
            "Check what the camera reports with: run.cmd --list-resolutions"
        )
    elif not DEVICE_PINNED:
        best = detected["best_guess_index"]
        if best is not None and best != camera.device_index:
            logger.info("Selecting detected camera index %s", best)
            camera.device_index = best

    if await run_in_threadpool(camera.start):
        logger.info("Camera ready")
    else:
        logger.warning(
            "Camera not streaming yet (%s); retrying in the background",
            camera.get_info().get("error"),
        )

    try:
        yield
    finally:
        await run_in_threadpool(camera.stop)
        logger.info("Server stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Document Camera Streamer", lifespan=lifespan)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    html_path = static_dir / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Streaming server running. Create static/index.html for the UI.</h1>"
    )


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------
@app.get("/api/cameras")
async def api_cameras(force: bool = False) -> JSONResponse:
    """List every usable camera, flagging document cameras and the active one."""
    return JSONResponse(await run_in_threadpool(list_cameras_sync, force))


@app.get("/api/cameras/detect")
async def api_detect_cameras(force: bool = False) -> JSONResponse:
    """Categorise cameras and report how names were matched."""
    return JSONResponse(
        await run_in_threadpool(identify_cameras, force, _skip_index())
    )


@app.get("/api/info")
async def api_info() -> JSONResponse:
    return JSONResponse(info_payload())


@app.get("/api/resolutions")
async def api_resolutions() -> JSONResponse:
    """Every capture mode the viewer offers, plus the mode in use."""
    return JSONResponse(resolution_payload())


async def _requested_resolution(
    request: Request,
    width: Optional[int],
    height: Optional[int],
    resolution: Optional[str],
) -> Tuple[int, int]:
    """Read the target size from query parameters or a JSON body."""
    if resolution is None and width is None and height is None:
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=422, detail="Body must be JSON") from exc
            if not isinstance(body, dict):
                raise HTTPException(status_code=422, detail="Body must be a JSON object")
            resolution = body.get("resolution")
            width = body.get("width")
            height = body.get("height")

    try:
        if resolution:
            return parse_resolution(str(resolution))
        if width is None or height is None:
            raise ValueError("Provide resolution=WxH, or both width and height")
        target = (int(width), int(height))
        if target[0] <= 0 or target[1] <= 0:
            raise ValueError("Resolution must be positive")
        return target
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/resolution")
async def api_set_resolution(
    request: Request,
    width: Optional[int] = None,
    height: Optional[int] = None,
    resolution: Optional[str] = None,
    timeout: float = 8.0,
) -> JSONResponse:
    """Change the capture resolution while viewers keep their connection.

    Accepts ``{"resolution": "1920x1200"}`` (or ``width``/``height``) in the
    JSON body, or the same values as query parameters. Drivers may quietly
    deliver a neighbouring mode, so the reply reports what was applied.
    """
    target = await _requested_resolution(request, width, height, resolution)
    wait = max(1.0, min(float(timeout), 20.0))
    applied = await run_in_threadpool(
        camera.set_resolution, target[0], target[1], wait
    )

    payload = {
        "success": applied,
        "requested": {"width": target[0], "height": target[1]},
        **info_payload(),
    }
    payload["exact"] = (payload["width"], payload["height"]) == target
    if not applied:
        return JSONResponse(
            status_code=409,
            content={
                **payload,
                "error": payload.get("error")
                or f"Camera did not reopen at {target[0]}x{target[1]}",
            },
        )
    logger.info(
        "Capture resolution %dx%d requested; camera reports %dx%d",
        target[0], target[1], payload["width"], payload["height"],
    )
    return JSONResponse(payload)


@app.post("/api/camera/{index}")
async def api_select_camera(index: int) -> JSONResponse:
    """Switch to a camera device by index."""
    if index < 0:
        raise HTTPException(status_code=422, detail="Camera index must be >= 0")

    known = await run_in_threadpool(list_cameras_sync, False)
    if all(cam["index"] != index for cam in known):
        # A camera plugged in since the last scan should still be selectable.
        known = await run_in_threadpool(list_cameras_sync, True)
    if all(cam["index"] != index for cam in known):
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"No camera at index {index}",
                "available": [cam["index"] for cam in known],
            },
        )

    switched = await run_in_threadpool(camera.switch_to, index, 5.0)
    payload = {"success": switched, **info_payload()}
    if not switched:
        return JSONResponse(
            status_code=409,
            content={
                **payload,
                "error": payload.get("error") or f"Camera {index} did not start streaming",
            },
        )
    logger.info("Switched to camera index %s", index)
    return JSONResponse(payload)


@app.get("/snapshot")
async def snapshot() -> Response:
    """Return a single JPEG snapshot from the camera."""
    jpeg = await run_in_threadpool(_snapshot_bytes)
    if jpeg is None:
        return JSONResponse(
            {"error": "Camera not available"},
            status_code=503,
            headers={"Retry-After": "2"},
        )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "Content-Disposition": "inline; filename=snapshot.jpg",
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# MJPEG stream  (browser-compatible, zero JS needed)
# ---------------------------------------------------------------------------
def mjpeg_part(jpeg: bytes) -> bytes:
    header = (
        f"--{MJPEG_BOUNDARY}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg)}\r\n\r\n"
    )
    return header.encode("ascii") + jpeg + b"\r\n"


@app.get("/stream")
async def mjpeg_stream(request: Request) -> StreamingResponse:
    """MJPEG stream endpoint. Use in a browser with ``<img src="/stream">``."""
    if not viewers.acquire():
        raise HTTPException(
            status_code=503,
            detail=f"Too many viewers (limit {viewers.limit})",
            headers={"Retry-After": "5"},
        )

    async def event_stream():
        seq = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                result = await run_in_threadpool(camera.wait_frame, seq, FRAME_WAIT_TIMEOUT)
                if result is None:
                    # Camera down: keep the connection open but emit nothing.
                    # A text part inside an image/jpeg stream would render as a
                    # broken image, so the UI shows offline state instead.
                    await asyncio.sleep(0.1)
                    continue
                seq, jpeg = result
                part = mjpeg_part(jpeg)
                stream_meter.add(len(part))
                yield part
        finally:
            viewers.release()

    return StreamingResponse(
        event_stream(),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# WebSocket stream  (lower latency, binary frames)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_stream(ws: WebSocket) -> None:
    """Stream JPEG frames over WebSocket as binary messages."""
    await ws.accept()
    if not viewers.acquire():
        await ws.close(code=1013, reason="Too many viewers")
        return

    seq = 0
    try:
        while True:
            # Serve control messages (ping) without blocking frame delivery.
            try:
                message = await asyncio.wait_for(ws.receive_text(), timeout=0.01)
                if message == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                pass

            result = await run_in_threadpool(camera.wait_frame, seq, FRAME_WAIT_TIMEOUT)
            if result is None:
                await asyncio.sleep(0.1)
                continue
            seq, jpeg = result
            await ws.send_bytes(jpeg)
            ws_meter.add(len(jpeg))
    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected")
    except RuntimeError:
        # Raised when the peer goes away between receive and send.
        logger.debug("WebSocket client went away")
    except Exception as exc:  # pragma: no cover - transport dependent
        logger.warning("WebSocket error: %s", exc)
    finally:
        viewers.release()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> JSONResponse:
    info = camera.get_info()
    healthy = info["connected"]
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "camera": info,
            "viewers": viewers.active,
            "uptime_s": _uptime(),
        },
        status_code=200 if healthy else 503,
    )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def resolution_arg(text: str) -> Tuple[int, int]:
    """argparse adapter for ``WIDTHxHEIGHT`` values."""
    try:
        return parse_resolution(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Stream a document camera over HTTP (MJPEG + WebSocket).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python server.py --resolution 1280x720\n"
            "  python server.py --resolution 1920x1200   # 16:10 screens\n"
            "  python server.py --desktop-fit            # sized for this screen\n"
            "  python server.py --desktop-fit --screen 1920x1080\n"
            "  python server.py --list-resolutions       # what the camera accepts\n"
            "\n"
            "The capture resolution can also be changed while the server runs:\n"
            "  POST /api/resolution {\"resolution\": \"1920x1200\"}\n"
            "or from the dropdown in the web viewer.\n"
        ),
    )
    parser.add_argument("--host", default=HOST, help=f"bind address (default {HOST})")
    parser.add_argument("--port", type=int, default=PORT, help=f"bind port (default {PORT})")
    parser.add_argument(
        "--device", type=int, default=None, metavar="INDEX",
        help="camera index (disables automatic camera selection)",
    )
    parser.add_argument(
        "--resolution", type=resolution_arg, default=None, metavar="WxH",
        help="capture resolution, e.g. 1920x1080",
    )
    parser.add_argument(
        "--desktop-fit", action="store_true",
        help="choose the largest capture mode that fits the desktop resolution",
    )
    parser.add_argument(
        "--fit-margin", type=float, default=0.9, metavar="FRACTION",
        help="share of the desktop to use with --desktop-fit (default 0.9)",
    )
    parser.add_argument(
        "--screen", type=resolution_arg, default=None, metavar="WxH",
        help="desktop resolution override when detection is unavailable",
    )
    parser.add_argument(
        "--fps", type=int, default=None, metavar="N", help="capture rate target",
    )
    parser.add_argument(
        "--jpeg-quality", type=int, default=None, metavar="N",
        help="JPEG quality, 1-100 (default 85)",
    )
    parser.add_argument(
        "--max-viewers", type=int, default=None, metavar="N",
        help="concurrent stream clients (default 8)",
    )
    parser.add_argument(
        "--list-resolutions", action="store_true",
        help="print the capture modes the camera accepts, then exit",
    )
    return parser


def apply_options(args: argparse.Namespace) -> None:
    """Apply parsed options to the camera and viewer settings."""
    global _display, DEVICE_PINNED

    if args.device is not None:
        camera.device_index = args.device
        DEVICE_PINNED = True
    if args.fps is not None:
        camera.target_fps = max(1, int(args.fps))
    if args.jpeg_quality is not None:
        camera.jpeg_quality = max(1, min(int(args.jpeg_quality), 100))
    if args.max_viewers is not None:
        viewers.limit = max(1, int(args.max_viewers))

    _display = args.screen if args.screen is not None else detect_desktop_resolution()

    if args.resolution is not None:
        camera.target_width, camera.target_height = args.resolution
        if args.desktop_fit:
            logger.info("--resolution takes precedence over --desktop-fit")
    elif args.desktop_fit:
        if _display is None:
            raise SystemExit(
                "Could not detect the desktop resolution. "
                "Pass --screen WxH (for example --screen 1920x1080)."
            )
        camera.target_width, camera.target_height = fit_resolution(_display, args.fit_margin)
        logger.info(
            "Desktop %dx%d -> capture %dx%d (margin %.2f)",
            _display[0], _display[1],
            camera.target_width, camera.target_height, args.fit_margin,
        )


def print_resolutions(index: int) -> int:
    """Print the capture modes the device accepts."""
    modes = probe_resolutions(index)
    if not modes:
        print(f"No camera could be opened at index {index}.")
        return 1
    print(f"Camera {index} capture modes:")
    for mode in modes:
        note = "supported" if mode["exact"] else f"nearest to {', '.join(mode['requested'])}"
        print(f"  {mode['width']:>5}x{mode['height']:<5} {note}")
    print("\nRequest one with: python server.py --resolution WxH")
    print("Or switch at runtime: POST /api/resolution {\"resolution\": \"WxH\"}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_resolutions:
        return print_resolutions(args.device if args.device is not None else camera.device_index)

    apply_options(args)
    logger.info(
        "Requested capture mode %dx%d @ %s fps",
        camera.target_width, camera.target_height, camera.target_fps,
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
