"""
Camera capture module for document camera streaming.
====================================================

Design notes
------------
Capture runs in a dedicated background thread that owns the ``cv2.VideoCapture``
handle. The thread publishes the newest JPEG frame into a slot guarded by a
condition variable; request handlers wait on that slot instead of touching the
device themselves. This keeps the asyncio event loop free (a 1080p read plus
encode is 10-25 ms) and lets any number of viewers share a single capture.

The same thread owns reconnection. If the device disappears it retries with
exponential backoff, so unplugging and replugging the camera recovers without a
server restart.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# Suppress noisy OpenCV warnings during camera enumeration.
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("OPENCV_VIDEOIO_LOG_LEVEL", "ERROR")

import cv2

logger = logging.getLogger(__name__)

_OPENCV_LOG_LEVELS = {
    "SILENT": 0, "FATAL": 0, "ERROR": 1, "WARNING": 2, "WARN": 2,
    "INFO": 3, "DEBUG": 4, "VERBOSE": 5,
}


def _apply_opencv_log_level() -> None:
    """Quiet OpenCV's own driver chatter; this module reports state instead.

    Backend probing emits lines such as ``obsensor ... Camera index out of
    range`` on every retry when no camera is attached, which drowns out the
    application log. Set ``DOCSTREAM_OPENCV_LOG_LEVEL`` to restore it.
    """
    configured = os.getenv("DOCSTREAM_OPENCV_LOG_LEVEL", "SILENT").upper()
    level = _OPENCV_LOG_LEVELS.get(configured)
    if level is None:
        return
    try:
        cv2.utils.logging.setLogLevel(level)
    except Exception:  # pragma: no cover - OpenCV build without the helper
        logger.debug("Could not set the OpenCV log level")


_apply_opencv_log_level()

IS_WINDOWS = platform.system() == "Windows"

# Keywords that strongly indicate a document camera / visualizer.
DOC_CAM_KEYWORDS = [
    "document camera", "doc cam", "visualizer", "visualiser",
    "ipevo", "hovercam", "hover cam", "elmo", "avervision",
    "avermedia", "lumens", "wolfvision", "okiolabs", "vz-",
    "ziggi", "v4k", "vz-x", "do-cam", "document scanner",
]

MAX_PROBE_INDEX = int(os.getenv("DOCSTREAM_MAX_PROBE_INDEX", "8"))
ENUM_CACHE_TTL = float(os.getenv("DOCSTREAM_ENUM_CACHE_TTL", "30"))
JPEG_QUALITY = int(os.getenv("DOCSTREAM_JPEG_QUALITY", "85"))

OPEN_BACKOFF_MIN = 0.5
OPEN_BACKOFF_MAX = 5.0
MAX_READ_FAILURES = 15          # consecutive failed reads before reconnecting
STALE_FRAME_SECONDS = 5.0       # a frame older than this means "not live"

_ENUM_LOCK = threading.Lock()
# Cache keyed by the probe index that was deliberately skipped, because that
# answer is only valid for that same skip value.
_ENUM_CACHE: Dict[Optional[int], Tuple[float, List[Dict[str, Any]]]] = {}
# A failing name lookup (for example WMI denied by policy) is reported once so
# it cannot flood the log on every cache refresh.
_NAME_LOOKUP_FAILURE_REPORTED = False

_PS_NAME_QUERY = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_PnPEntity | "
    "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' } | "
    "Select-Object Name, DeviceID | ConvertTo-Json -Compress"
)


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------
def windows_camera_names() -> List[Dict[str, str]]:
    """Return PnP camera device names in WMI order.

    This order has no defined relationship to OpenCV's device indices, so the
    result is only used to label cameras, never to identify them.
    """
    global _NAME_LOOKUP_FAILURE_REPORTED
    if not IS_WINDOWS:
        return []
    started = time.monotonic()

    def report_failure(message: str, *args: Any) -> None:
        global _NAME_LOOKUP_FAILURE_REPORTED
        if _NAME_LOOKUP_FAILURE_REPORTED:
            logger.debug(message, *args)
        else:
            _NAME_LOOKUP_FAILURE_REPORTED = True
            logger.warning(message, *args)

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_NAME_QUERY],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report_failure("Camera name lookup could not run PowerShell: %s", exc)
        return []
    if result.returncode != 0:
        report_failure(
            "Camera name lookup failed (exit %s): %s",
            result.returncode, (result.stderr or "").strip()[:200],
        )
        return []

    raw = (result.stdout or "").strip()
    if not raw:
        logger.info("Camera name lookup found no PnP camera devices")
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        report_failure("Camera name lookup returned unparsable JSON: %s", exc)
        return []

    items = [parsed] if isinstance(parsed, dict) else parsed
    names: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (item.get("Name") or "").strip()
        if name:
            names.append({"name": name, "device_id": str(item.get("DeviceID") or "")})
    logger.debug(
        "Camera name lookup found %d device(s) in %.2fs",
        len(names), time.monotonic() - started,
    )
    return names


def _probe_camera(index: int) -> Optional[Dict[str, Any]]:
    """Open one index briefly and report what it is. Never raises."""
    backend = cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY
    cap = None
    try:
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            return None
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        try:
            backend_name = cap.getBackendName()
        except Exception:  # pragma: no cover - backend dependent
            backend_name = "unknown"
        return {
            "index": index,
            "width": max(width, 0),
            "height": max(height, 0),
            "fps": round(fps, 2) if fps > 0 else None,
            "backend": backend_name,
        }
    except Exception as exc:  # pragma: no cover - driver dependent
        logger.debug("Probe of camera index %s failed: %s", index, exc)
        return None
    finally:
        if cap is not None:
            cap.release()


def _is_document_camera_name(name: Optional[str]) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return any(keyword in lowered for keyword in DOC_CAM_KEYWORDS)


def attach_names(cameras: List[Dict[str, Any]], names: List[Dict[str, str]]) -> str:
    """Label cameras with friendly names and report how trustworthy that is.

    Returns one of ``exact`` (one camera, one name), ``assumed-order`` (counts
    agree, so names are paired positionally - a guess), ``mismatched`` (counts
    disagree, so names are withheld rather than risk a wrong label) or
    ``unavailable`` (the name lookup failed or found nothing).
    """
    if not cameras:
        return "none"
    if not names:
        return "unavailable"
    if len(names) != len(cameras):
        logger.warning(
            "Camera naming skipped: OpenCV found %d device(s) but Windows reported %d; "
            "indices cannot be matched to names safely",
            len(cameras), len(names),
        )
        return "mismatched"
    for cam, device in zip(cameras, names):
        cam["name"] = device["name"]
        cam["device_id"] = device["device_id"]
    if len(cameras) == 1:
        return "exact"
    logger.debug("Camera names paired positionally; order is assumed, not guaranteed")
    return "assumed-order"


def enumerate_cameras(
    force: bool = False, skip_index: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Probe device indices and return camera descriptors.

    ``skip_index`` keeps the probe away from the device that is currently being
    streamed, so refreshing the camera list cannot disturb a live stream.
    """
    now = time.monotonic()
    with _ENUM_LOCK:
        cached = _ENUM_CACHE.get(skip_index)
        if cached and not force and now - cached[0] < ENUM_CACHE_TTL:
            return [dict(cam) for cam in cached[1]]

    cameras: List[Dict[str, Any]] = []
    for index in range(MAX_PROBE_INDEX):
        if skip_index is not None and index == skip_index:
            continue
        probed = _probe_camera(index)
        if probed is not None:
            cameras.append(probed)

    # No cameras means no names to look up; skipping the query keeps a
    # cameraless startup fast instead of paying for a PowerShell round-trip.
    names = windows_camera_names() if cameras else []
    confidence = attach_names(cameras, names)
    for cam in cameras:
        cam["name_confidence"] = confidence
        cam["is_document_camera"] = _is_document_camera_name(cam.get("name"))

    with _ENUM_LOCK:
        _ENUM_CACHE[skip_index] = (time.monotonic(), [dict(cam) for cam in cameras])
    return cameras


def identify_cameras(
    force: bool = False, skip_index: Optional[int] = None
) -> Dict[str, Any]:
    """Categorise cameras into document cameras versus everything else."""
    cameras = enumerate_cameras(force=force, skip_index=skip_index)
    document_cameras = [cam for cam in cameras if cam.get("is_document_camera")]
    other_cameras = [cam for cam in cameras if not cam.get("is_document_camera")]

    best_guess: Optional[int] = None
    if document_cameras:
        best_guess = document_cameras[0]["index"]
    elif cameras:
        # No name evidence: prefer the largest sensor, then the DSHOW backend.
        def rank(cam: Dict[str, Any]) -> Tuple[int, int]:
            pixels = int(cam["width"]) * int(cam["height"])
            directshow = 1 if "dshow" in (cam.get("backend") or "").lower() else 0
            return pixels, directshow

        best_guess = max(cameras, key=rank)["index"]

    return {
        "cameras": cameras,
        "document_cameras": document_cameras,
        "other_cameras": other_cameras,
        "best_guess_index": best_guess,
        "names_confidence": cameras[0]["name_confidence"] if cameras else "none",
    }


def mjpg_fourcc() -> int:
    """Return the MJPG FOURCC, preferring the non-deprecated OpenCV API."""
    fourcc = getattr(cv2.VideoWriter, "fourcc", None) or cv2.VideoWriter_fourcc
    return int(fourcc("M", "J", "P", "G"))


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------
# Capture modes offered by the viewer and probed by --list-resolutions: the
# common USB webcam / document camera modes, largest first. 16:10 is included
# (1920x1200, 2560x1600, 1280x800) so those screens get a native mode.
CAPTURE_RESOLUTIONS: List[Tuple[int, int]] = [
    (3840, 2160), (2560, 1600), (2560, 1440), (2048, 1536),
    (1920, 1200), (1920, 1080), (1600, 1200), (1600, 900),
    (1280, 1024), (1366, 768), (1280, 800), (1152, 864), (1280, 720),
    (1024, 768), (1024, 576), (960, 540), (800, 600), (720, 576),
    (848, 480), (720, 480), (640, 480), (640, 360), (352, 288), (320, 240),
]

# Subset used by --desktop-fit. Drivers silently fake many catalog modes, so
# the automatic choice stays on modes document cameras report honestly.
FIT_RESOLUTIONS: List[Tuple[int, int]] = [
    (3840, 2160), (2560, 1440), (1920, 1200), (1920, 1080), (1600, 900),
    (1280, 720), (1024, 768), (960, 540), (848, 480), (800, 600),
    (640, 480), (640, 360), (320, 240),
]

# Relative difference in aspect ratio that still counts as "the same shape".
ASPECT_TOLERANCE = 0.01

# A mode this close in area to the largest fit is preferred when its shape
# matches the screen (16:10 screen -> 1920x1200 rather than 1920x1080).
ASPECT_PREFERENCE_AREA = 0.9

_COMMON_ASPECTS = [
    ((21, 9), "21:9"), ((16, 10), "16:10"), ((16, 9), "16:9"),
    ((5, 4), "5:4"), ((4, 3), "4:3"), ((3, 2), "3:2"),
]


def aspect_label(width: int, height: int) -> str:
    """Human-readable shape of a mode, e.g. ``16:10``."""
    if width <= 0 or height <= 0:
        return "?"
    ratio = width / height
    for (num, den), name in _COMMON_ASPECTS:
        if abs(ratio - num / den) <= ASPECT_TOLERANCE:
            return name
    return f"{ratio:.2f}:1"


def resolution_label(width: int, height: int) -> str:
    """Label for the viewer's resolution picker."""
    return f"{width} × {height} ({aspect_label(width, height)})"


def parse_resolution(text: str) -> Tuple[int, int]:
    """Parse ``1920x1080`` (also accepts ``×``, ``*``, comma or space)."""
    normalized = (text or "").strip().lower()
    normalized = normalized.replace("×", "x").replace("*", "x").replace(",", "x")
    parts = [part for part in re.split(r"[x\s]+", normalized) if part]
    if len(parts) != 2:
        raise ValueError(f"Expected WIDTHxHEIGHT, got {text!r}")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Expected WIDTHxHEIGHT, got {text!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"Resolution must be positive, got {text!r}")
    return width, height


def fit_resolution(
    desktop: Tuple[int, int], margin: float = 0.9
) -> Tuple[int, int]:
    """Best common capture mode that fits inside ``desktop``.

    ``margin`` leaves room for window chrome and the page header, so the whole
    camera frame stays visible without the viewer scaling it up. Modes that
    match the screen's aspect ratio win, so a 16:10 display gets 1920x1200
    rather than a 16:9 mode that would be letterboxed.
    """
    max_width = max(int(desktop[0] * margin), 1)
    max_height = max(int(desktop[1] * margin), 1)
    fitting = [
        (width, height) for width, height in FIT_RESOLUTIONS
        if width <= max_width and height <= max_height
    ]
    if not fitting:
        return FIT_RESOLUTIONS[-1]

    best = max(fitting, key=lambda mode: mode[0] * mode[1])
    target = desktop[0] / desktop[1] if desktop[1] else 0
    if not target:
        return best

    # Of the modes nearly as large as the biggest fit, take the one shaped like
    # the screen, so a 16:10 display gets 1920x1200 instead of letterboxed 16:9.
    contenders = [
        mode for mode in fitting
        if mode[0] * mode[1] >= best[0] * best[1] * ASPECT_PREFERENCE_AREA
    ]
    return min(
        contenders,
        key=lambda mode: (
            abs(mode[0] / mode[1] - target) / target, -(mode[0] * mode[1]),
        ),
    )


_DPI_AWARENESS_SET = False


def _enable_dpi_awareness() -> None:
    """Report physical pixels instead of DPI-virtualized ones on Windows.

    Without this, a 1920x1200 display at 125% scaling reports 1536x960, and a
    capture mode chosen to "fit the desktop" comes out one size too small.
    """
    global _DPI_AWARENESS_SET
    if _DPI_AWARENESS_SET:
        return
    _DPI_AWARENESS_SET = True
    try:
        import ctypes

        try:
            # PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            # PROCESS_DPI_AWARE fallback (Windows Vista+)
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("Could not enable DPI awareness: %s", exc)


def detect_desktop_resolution() -> Optional[Tuple[int, int]]:
    """Pixel size of the primary display, or ``None`` when it is unknown.

    Always reports physical pixels, so a scaled display does not shrink the
    answer. Headless sessions and locked-down environments can refuse this;
    callers should fall back to an explicit ``--screen`` override.
    """
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes

            _enable_dpi_awareness()
            user32 = ctypes.windll.user32
            width = int(user32.GetSystemMetrics(0))
            height = int(user32.GetSystemMetrics(1))
            if width > 0 and height > 0:
                return width, height
        elif system == "Darwin":
            output = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            matches = re.findall(r"Resolution:\s*(\d+)\s*x\s*(\d+)", output)
            if matches:
                return max(
                    ((int(width), int(height)) for width, height in matches),
                    key=lambda size: size[0] * size[1],
                )
        else:
            output = subprocess.run(
                ["xrandr", "--query"], capture_output=True, text=True, timeout=5,
            ).stdout
            match = re.search(r"current\s+(\d+)\s*x\s+(\d+)", output)
            if match:
                return int(match.group(1)), int(match.group(2))
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("Desktop resolution detection failed: %s", exc)
    return None


def probe_resolutions(device_index: int = 0) -> List[Dict[str, Any]]:
    """Report the capture modes the device in ``device_index`` accepts.

    Drivers often accept a request and silently keep a neighbouring mode, so
    every candidate is opened on its own handle and confirmed with a real frame.
    """
    backend = cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY
    fourcc = mjpg_fourcc()
    found: Dict[Tuple[int, int], Dict[str, Any]] = {}

    for width, height in CAPTURE_RESOLUTIONS:
        cap = None
        try:
            cap = cv2.VideoCapture(device_index, backend)
            if not cap.isOpened():
                return []
            cap.set(cv2.CAP_PROP_FOURCC, fourcc)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            ok, frame = cap.read()
            if ok and frame is not None:
                actual_height, actual_width = frame.shape[:2]
            if actual_width <= 0 or actual_height <= 0:
                continue
            entry = found.get((actual_width, actual_height))
            if entry is None:
                entry = {
                    "width": actual_width,
                    "height": actual_height,
                    "exact": False,
                    "requested": [],
                }
                found[(actual_width, actual_height)] = entry
            entry["requested"].append(f"{width}x{height}")
            if (actual_width, actual_height) == (width, height):
                entry["exact"] = True
        except Exception as exc:  # pragma: no cover - driver dependent
            logger.debug("Probing %sx%s failed: %s", width, height, exc)
        finally:
            if cap is not None:
                cap.release()

    return sorted(
        found.values(), key=lambda entry: (-(entry["width"] * entry["height"]), entry["width"])
    )


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------
class DocumentCamera:
    """A document camera (or any USB webcam) with a background capture thread."""

    def __init__(
        self,
        device_index: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
        jpeg_quality: int = JPEG_QUALITY,
    ) -> None:
        self.device_index = device_index
        self.target_width = width
        self.target_height = height
        self.target_fps = fps
        self.jpeg_quality = jpeg_quality

        # Guards only the handle reference; never held across device I/O.
        self._cap: Optional[cv2.VideoCapture] = None
        self._cap_lock = threading.Lock()
        # Serialises start/switch so two callers cannot race a reopen.
        self._switch_lock = threading.Lock()

        self._frame_cv = threading.Condition()
        self._frame: Optional[bytes] = None
        self._frame_seq = 0
        self._frame_ts = 0.0

        self._state_lock = threading.Lock()
        self._state = "idle"
        self._last_error: Optional[str] = None
        self._active_index: Optional[int] = None
        self._actual: Dict[str, Any] = {"width": 0, "height": 0, "fps": None, "backend": None}

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._reopen = threading.Event()
        # Incremented on every successful open, so a caller that asked for a
        # change can tell a fresh session from the one already streaming.
        self._session = 0
        # Requested mode -> what the driver actually delivered, keyed by
        # (device index, width, height) because modes are per device.
        self._negotiated: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

    # -- public state -------------------------------------------------------
    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def is_open(self) -> bool:
        return self.state == "streaming"

    @property
    def frame_seq(self) -> int:
        with self._frame_cv:
            return self._frame_seq

    def get_info(self) -> Dict[str, Any]:
        """Describe the camera without touching the device."""
        with self._state_lock:
            state = self._state
            error = self._last_error
            actual = dict(self._actual)
            active = self._active_index
        seq, frame, age = self.latest_frame_with_age()
        connected = state == "streaming" and (age is None or age < STALE_FRAME_SECONDS)
        return {
            "connected": connected,
            "state": state,
            "device_index": active if active is not None else self.device_index,
            "width": actual["width"],
            "height": actual["height"],
            "fps": actual["fps"],
            "target_width": self.target_width,
            "target_height": self.target_height,
            "target_fps": self.target_fps,
            "backend": actual["backend"],
            "frames_captured": seq,
            "frame_bytes": len(frame) if frame else None,
            "last_frame_age_s": round(age, 3) if age is not None else None,
            "error": error,
        }

    def negotiated_modes(self, index: Optional[int] = None) -> List[Dict[str, Any]]:
        """Modes requested since start, with what the driver actually gave.

        ``index`` defaults to the device in use; results are per device, so a
        camera switch starts the record again instead of mixing two devices.
        """
        with self._state_lock:
            wanted = self._active_index if index is None else index
            entries = [
                dict(entry)
                for (cam_index, _width, _height), entry in self._negotiated.items()
                if cam_index == wanted
            ]
        return sorted(
            entries, key=lambda entry: (-(entry["width"] * entry["height"]), entry["width"])
        )

    # -- frame slot ---------------------------------------------------------
    def latest_frame_with_age(self) -> Tuple[int, Optional[bytes], Optional[float]]:
        with self._frame_cv:
            age = (time.monotonic() - self._frame_ts) if self._frame_ts else None
            return self._frame_seq, self._frame, age

    def wait_frame(
        self, after_seq: int = 0, timeout: float = 1.0
    ) -> Optional[Tuple[int, bytes]]:
        """Block until a frame newer than ``after_seq`` exists.

        Returns ``(sequence, jpeg)`` or ``None`` on timeout / shutdown. Always
        returns the *newest* frame, so slow clients skip stale ones instead of
        queuing them.
        """
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._frame_cv:
            while True:
                if self._stop.is_set():
                    return None
                if self._frame_seq > after_seq and self._frame is not None:
                    return self._frame_seq, self._frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._frame_cv.wait(min(remaining, 0.5))

    def _publish(self, jpeg: bytes) -> None:
        with self._frame_cv:
            self._frame = jpeg
            self._frame_seq += 1
            self._frame_ts = time.monotonic()
            self._frame_cv.notify_all()

    def _wake_waiters(self) -> None:
        with self._frame_cv:
            self._frame_cv.notify_all()

    def _set_state(self, state: str, error: Optional[str] = None) -> None:
        with self._state_lock:
            if self._state == state and self._last_error == error:
                return
            self._state = state
            self._last_error = error
        if error:
            logger.warning("Camera state=%s: %s", state, error)
        else:
            logger.debug("Camera state=%s", state)

    # -- lifecycle ----------------------------------------------------------
    def start(self, wait: float = 3.0) -> bool:
        """Start the capture thread and report whether it is streaming yet."""
        with self._switch_lock:
            if self._thread is not None and self._thread.is_alive():
                return self.is_open
            self._stop.clear()
            self._reopen.clear()
            self._set_state("starting")
            self._thread = threading.Thread(
                target=self._capture_loop, name="camera-capture", daemon=True,
            )
            self._thread.start()

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self._state == "streaming":
                return True
            if self._state == "error":
                return False
            time.sleep(0.02)
        return self._state == "streaming"

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the capture thread to finish and join it."""
        self._stop.set()
        self._reopen.set()      # wake any backoff wait
        self._wake_waiters()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Capture thread did not stop within %.1fs", timeout)
        self._thread = None
        with self._cap_lock:
            self._cap = None
        self._set_state("stopped")

    def switch_to(self, index: int, timeout: float = 5.0) -> bool:
        """Switch device index and wait for the stream to come back."""
        with self._switch_lock:
            if index == self.device_index and self._state == "streaming":
                return True
            if self._thread is None or not self._thread.is_alive():
                self.device_index = index
                return self.start(wait=timeout)
            logger.info("Switching camera to index %s", index)
            self.device_index = index
            self._set_state("switching")
            self._reopen.set()
            self._wake_waiters()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._state == "streaming" and self._active_index == index:
                    return True
                time.sleep(0.02)
            return self._state == "streaming" and self._active_index == index

    def set_resolution(self, width: int, height: int, timeout: float = 8.0) -> bool:
        """Reopen the device at ``width``x``height`` without restarting the server.

        Returns ``True`` once a fresh capture session is streaming. Drivers may
        deliver a neighbouring mode instead of the requested one, so the caller
        should compare ``get_info()`` with the target; ``negotiated_modes()``
        keeps that answer for the resolution picker.
        """
        width, height = int(width), int(height)
        if width <= 0 or height <= 0:
            raise ValueError("Resolution must be positive")

        with self._switch_lock:
            self.target_width, self.target_height = width, height
            if self._thread is None or not self._thread.is_alive():
                logger.info(
                    "Capture is not running; resolution target set to %dx%d", width, height,
                )
                return False

            session = self._session
            logger.info("Changing capture resolution to %dx%d", width, height)
            self._set_state("switching")
            self._reopen.set()
            self._wake_waiters()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._session != session and self._state == "streaming":
                    return True
                time.sleep(0.02)
            return False

    # Backwards-compatible aliases.
    def open(self) -> bool:
        return self.start()

    def release(self) -> None:
        self.stop()

    def get_frame_jpeg(self) -> Optional[bytes]:
        """Return the newest captured frame, without touching the device."""
        return self.latest_frame_with_age()[1]

    # -- capture thread -----------------------------------------------------
    def _capture_loop(self) -> None:
        backoff = OPEN_BACKOFF_MIN
        while not self._stop.is_set():
            self._reopen.clear()
            index = int(self.device_index)
            cap = self._open_capture(index)
            if cap is None:
                if self._stop.is_set():
                    break
                # Wait, but wake immediately if a switch or shutdown happens.
                self._reopen.wait(backoff)
                backoff = min(backoff * 2, OPEN_BACKOFF_MAX)
                continue

            backoff = OPEN_BACKOFF_MIN
            failures = 0
            period = 1.0 / max(self.target_fps, 1)
            next_deadline = time.monotonic()

            while not self._stop.is_set() and not self._reopen.is_set():
                try:
                    ok, frame = cap.read()
                except Exception as exc:  # pragma: no cover - driver dependent
                    self._set_state("reconnecting", f"Camera read failed: {exc}")
                    break
                if not ok:
                    failures += 1
                    if failures >= MAX_READ_FAILURES:
                        self._set_state(
                            "reconnecting",
                            f"Camera at index {index} stopped returning frames",
                        )
                        break
                    self._stop.wait(0.05)
                    continue

                failures = 0
                try:
                    encoded, buffer = cv2.imencode(
                        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
                    )
                except Exception as exc:  # pragma: no cover - driver dependent
                    self._set_state("reconnecting", f"Frame encoding failed: {exc}")
                    break
                if encoded:
                    self._publish(buffer.tobytes())

                next_deadline += period
                delay = next_deadline - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_deadline = time.monotonic()

            with self._cap_lock:
                self._cap = None
            try:
                cap.release()
            except Exception:  # pragma: no cover - driver dependent
                pass
            if self._stop.is_set():
                break
            self._set_state("switching" if self._reopen.is_set() else "reconnecting")

        self._set_state("stopped")

    def _open_capture(self, index: int) -> Optional[cv2.VideoCapture]:
        """Open ``index``, configure it and mark the camera streaming."""
        attempts = [(cv2.CAP_DSHOW, "DSHOW")] if IS_WINDOWS else []
        attempts.append((cv2.CAP_ANY, "ANY"))
        last_error: Optional[str] = None
        fourcc = mjpg_fourcc()
        # Read the target once so a concurrent set_resolution cannot leave this
        # open half-configured for the old size and half for the new one.
        target = (int(self.target_width), int(self.target_height))

        for api, label in attempts:
            cap = None
            try:
                cap = cv2.VideoCapture(index, api)
                if not cap.isOpened():
                    cap.release()
                    last_error = last_error or f"{label} backend could not open index {index}"
                    continue

                # The codec must be selected before the resolution: many USB
                # devices negotiate a low-resolution uncompressed mode otherwise.
                cap.set(cv2.CAP_PROP_FOURCC, fourcc)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, target[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, target[1])
                cap.set(cv2.CAP_PROP_FPS, self.target_fps)

                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                try:
                    backend_name = cap.getBackendName()
                except Exception:  # pragma: no cover - backend dependent
                    backend_name = label

                with self._cap_lock:
                    self._cap = cap
                with self._state_lock:
                    self._active_index = index
                    self._session += 1
                    self._actual = {
                        "width": max(width, 0),
                        "height": max(height, 0),
                        "fps": round(fps, 2) if fps > 0 else None,
                        "backend": backend_name,
                    }
                    self._negotiated[(index, target[0], target[1])] = {
                        "width": target[0],
                        "height": target[1],
                        "actual_width": max(width, 0),
                        "actual_height": max(height, 0),
                        "exact": (width, height) == target,
                    }
                self._set_state("streaming")
                logger.info(
                    "Camera opened [index=%s backend=%s] %dx%d @ %s fps",
                    index, backend_name, width, height, f"{fps:.1f}" if fps > 0 else "?",
                )
                return cap
            except cv2.error as exc:  # pragma: no cover - driver dependent
                last_error = f"{label} backend failed: {exc}"
                logger.debug("Opening camera %s through %s failed: %s", index, label, exc)
                if cap is not None:
                    cap.release()

        self._set_state("error", last_error or f"No camera could be opened at index {index}")
        return None
