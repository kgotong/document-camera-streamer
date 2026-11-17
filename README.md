# Document Camera Streaming Server

Streams a USB document camera (or any webcam) over the network with a live web viewer.

## Features

- **MJPEG HTTP stream** — works in any browser via `<img>`, zero JS required
- **WebSocket stream** — lower latency binary frames, decodes off the main thread
- **Snapshot endpoint** — grab a still frame as JPEG
- **One capture, many viewers** — a single background thread owns the camera; every
  viewer shares the newest frame, so N viewers no longer means N× the CPU
- **Hot-plug recovery** — unplug the camera and the server reconnects with backoff
- **Camera switching** — from the UI dropdown or the API, validated against the
  devices that actually exist
- **Change resolution live** — pick any mode from the viewer while people keep
  watching; 16:10 modes such as 1920x1200 are included, and the camera's honest
  answer is shown when a driver substitutes a neighbouring mode
- **Network meter** — the viewer reports the bytes-per-second actually being
  sent to clients (per endpoint and total), so you can see what a mode costs
- **Fits the window** — the viewer letterboxes the whole camera frame inside the
  viewport, so nothing is cropped or scrolled off, at any window size
- **Honest status** — the viewer shows *Disconnected* instead of a frozen frame,
  and `/health` reports `degraded` with the reason
- **Bounded viewers** — a configurable cap prevents one machine from saturating the host

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web viewer UI |
| `/stream` | GET | MJPEG live stream |
| `/ws` | WebSocket | Binary JPEG frame stream (`ping` → `pong`) |
| `/snapshot` | GET | Single JPEG snapshot (503 with `Retry-After` when no frame) |
| `/api/cameras` | GET | List cameras (`?force=true` bypasses the 30 s cache) |
| `/api/cameras/detect` | GET | Categorised list plus name-matching confidence |
| `/api/info` | GET | Camera state, resolution, viewer count, uptime, network rates |
| `/api/camera/{index}` | POST | Switch camera (404 unknown index, 409 could not start) |
| `/api/resolutions` | GET | Every capture mode the viewer offers, plus the mode in use |
| `/api/resolution` | POST | Change capture resolution while streaming (422 bad size, 409 camera stayed down) |
| `/health` | GET | `200 ok` when streaming, `503 degraded` otherwise |

`/api/cameras` returns every camera, not only document cameras:

```json
[{"index": 0, "width": 1920, "height": 1080, "fps": 30.0, "backend": "DSHOW",
  "name": "IPEVO V4K", "name_confidence": "exact",
  "is_document_camera": true, "in_use": true}]
```

`name_confidence` tells you how much to trust `name`:

| Value | Meaning |
|---|---|
| `exact` | One camera, one matching Windows device |
| `assumed-order` | Counts agree, so names were paired positionally — a guess |
| `mismatched` | Windows and OpenCV disagree on the count; names are withheld |
| `unavailable` | The name lookup failed or found nothing |
| `in-use` | The device in the list is the one currently streaming |

## Quick Start

Run the launcher — it uses the project's `.venv` when present, so it works from
any shell without activating anything:

```bat
run.cmd --desktop-fit
```

Or set up and run it manually:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python server.py --desktop-fit
```

> **Use the project interpreter.** A bare `python` or `python3` on PATH is often a
> different installation without these packages, which shows up as
> `ModuleNotFoundError: No module named 'fastapi'`. Either activate the
> environment (`.venv\Scripts\activate`) or prefix commands with
> `.venv\Scripts\python`.

Open **http://localhost:8080**. The server binds to `0.0.0.0`, so any device on
the network can reach it at `http://<your-laptop-ip>:8080`.

## Command line

```
python server.py [--resolution WxH] [--desktop-fit] [--screen WxH]
                 [--device INDEX] [--fps N] [--jpeg-quality N]
                 [--max-viewers N] [--host HOST] [--port PORT]
                 [--list-resolutions]
```

| Option | Effect |
|---|---|
| `--resolution WxH` | Request a capture mode, e.g. `--resolution 1920x1080` |
| `--desktop-fit` | Pick the best common mode that fits this machine's desktop (largest, preferring the screen's shape) |
| `--fit-margin FRACTION` | How much of the desktop to use (default `0.9`, leaving room for window chrome) |
| `--screen WxH` | Desktop resolution override for headless or remote hosts |
| `--list-resolutions` | Probe the camera and print the modes the driver actually accepts |
| `--device INDEX` | Pin a camera and disable auto-selection |
| `--fps N` | Capture rate target |
| `--jpeg-quality N` | JPEG quality, 1-100 |
| `--max-viewers N` | Concurrent stream clients |

Sizing for the screen you are actually viewing on — no guessing a resolution:

```bash
python server.py --desktop-fit                  # e.g. desktop 1920x1080 -> capture 1600x900
python server.py --desktop-fit --fit-margin 1.0 # use the full desktop
python server.py --desktop-fit --screen 2560x1440
```

Detection reports **physical pixels**, so a scaled display is not under-reported:
a 1920x1200 screen at 125% Windows scaling still reports 1920x1200 (not
1536x960). The chooser prefers a mode shaped like the screen: a 16:10 desktop
gets 1920x1200 at `--fit-margin 1.0` (and 1600x900 at the default `0.9`), while a
16:9 desktop keeps 16:9 modes even when a 16:10 mode would also fit.

Ask the camera what it supports before choosing (each candidate is opened and
confirmed with a real frame, because drivers often silently keep a neighbouring
mode):

```bash
python server.py --list-resolutions
# Camera 0 capture modes:
#    1920x1080 supported
#     1280x720 supported
#      640x480 supported
```

The server logs both sizes at startup, and `/api/info` reports them:

```json
{"width": 1600, "height": 900, "target_width": 1600, "target_height": 900,
 "display": {"width": 1920, "height": 1080},
 "network": {"stream_bps": 2187318, "websocket_bps": 0,
             "total_bps": 2187318, "bytes_sent": 3935259, "frame_bytes": 73000}}
```

`network` is measured on the server: `stream_bps` counts bytes handed to MJPEG
viewers, `websocket_bps` counts binary frames sent over `/ws`, and the reader is a
rolling window so it follows what is happening now. `frame_bytes` is the size of
the newest JPEG, which is what makes a resolution change visibly faster or slower.

Because the page scales to the viewport, picking a mode that fits your desktop
keeps the image at (or near) native size — sharp, never cropped, never needing a
scrollbar.

### Changing resolution while it streams

The **Resolution** dropdown in the viewer switches the capture mode in place —
capture reopens, viewers stay connected, and the frame simply changes size. It
offers every common mode, including 16:10 (1920x1200, 2560x1600, 1280x800) and
4:3 (1600x1200, 1024x768):

```
3840x2160  2560x1600  2560x1440  2048x1536  1920x1200  1920x1080
1600x1200  1600x900   1280x1024  1366x768   1280x800   1152x864
1280x720   1024x768   1024x576   960x540    800x600    720x576
848x480    720x480    640x480    640x360    352x288    320x240
```

The same thing from the command line or a script:

```bash
curl -X POST http://localhost:8080/api/resolution \
     -H 'Content-Type: application/json' -d '{"resolution": "1920x1200"}'
curl -X POST "http://localhost:8080/api/resolution?width=1920&height=1200"
```

Drivers frequently accept a request and keep a neighbouring mode, so the reply
states what the camera actually produced:

```json
{"success": true, "exact": false,
 "requested": {"width": 1920, "height": 1200},
 "width": 1280, "height": 720, "state": "streaming"}
```

`/api/resolutions` annotates each mode with what this camera did when it was
tried, so the picker replaces guesswork with evidence:

```json
{"current": {"width": 1280, "height": 720, "exact": true, "state": "streaming"},
 "resolutions": [
   {"width": 1920, "height": 1200, "label": "1920 × 1200 (16:10)",
    "current": false, "tested": {"exact": false, "actual": "1280x720"}},
   {"width": 1280, "height": 720, "label": "1280 × 720 (16:9)",
    "current": true, "tested": {"exact": true, "actual": "1280x720"}}]}
```

## Configuration

Command-line options win over environment variables, which win over the defaults.

| Variable | Default | Purpose |
|---|---|---|
| `DOCSTREAM_HOST` | `0.0.0.0` | Bind address |
| `DOCSTREAM_PORT` | `8080` | Bind port |
| `DOCSTREAM_DEVICE_INDEX` | `0` | Pin a camera and disable auto-selection |
| `DOCSTREAM_WIDTH` / `DOCSTREAM_HEIGHT` | `1920` / `1080` | Requested capture size |
| `DOCSTREAM_FPS` | `30` | Capture pacing target |
| `DOCSTREAM_JPEG_QUALITY` | `85` | Encoder quality |
| `DOCSTREAM_MAX_VIEWERS` | `8` | Concurrent stream/WebSocket clients |
| `DOCSTREAM_MAX_PROBE_INDEX` | `8` | Indices scanned when listing cameras |
| `DOCSTREAM_ENUM_CACHE_TTL` | `30` | Seconds a camera scan stays cached |
| `DOCSTREAM_OPENCV_LOG_LEVEL` | `SILENT` | OpenCV driver log level (`SILENT`, `ERROR`, `WARNING`, `INFO`, `DEBUG`) |
| `DOCSTREAM_LOG_LEVEL` | `INFO` | Application log verbosity |

### Choosing a camera

Auto-selection prefers a device whose Windows name matches a document-camera
keyword (IPEVO, HoverCam, Elmo, AVerVision, Lumens, WolfVision, …). Because
Windows enumeration order and OpenCV indices are not guaranteed to agree, that
match is reported as `name_confidence: assumed-order` unless only one camera
exists. When you care about a specific device, pin it:

```bash
# Windows PowerShell
$env:DOCSTREAM_DEVICE_INDEX = '1'; python server.py
```

## Usage Scenarios

### Embed in another page

```html
<img src="http://your-server:8080/stream" alt="Document Camera">
```

### Grab a snapshot programmatically

```bash
curl http://localhost:8080/snapshot -o doc.jpg
```

### Use with OBS / vMix

Add a **Browser source** pointing to `http://your-server:8080/stream`.

## Testing

The suite uses the standard library only — no extra packages, no camera needed:

```bash
python -m unittest discover -s tests -t . -v
```

## Troubleshooting

- **`ModuleNotFoundError: No module named 'fastapi'`** — you are running a
  different Python than the one the packages were installed into. Use
  `run.cmd`, or prefix the command with `.venv\Scripts\python`, or activate the
  environment first.
- **No camera found** — the server keeps retrying in the background; check the log
  line for the index it tried. `/api/cameras/detect` shows what it can see.
- **Camera names missing, or "Camera name lookup failed"** — the PowerShell/WMI
  query is blocked (some policy environments deny `Get-CimInstance`). Detection
  falls back to resolution, and pinning `DOCSTREAM_DEVICE_INDEX` skips it entirely.
- **`--desktop-fit` cannot detect the desktop** — headless sessions and some
  service accounts report no display; pass the size yourself with
  `--screen 1920x1080`.
- **Black screen** — MJPG is requested before the resolution, which most USB
  document cameras need for 1080p; if it persists try another port or cable. A
  camera that drops out now reports *disconnected* rather than showing a stale frame.
- **Port in use** — set `DOCSTREAM_PORT`.
- **Too many viewers (503)** — raise `DOCSTREAM_MAX_VIEWERS`.
- **MJPEG works but WebSocket does not** — check that nothing (a proxy) strips
  `Upgrade` headers; the UI falls back to MJPEG on its own.

## Project Layout

```
server.py           FastAPI app, endpoints, viewer accounting
camera.py           capture thread, frame slot, device enumeration, reconnection
static/index.html   viewer UI (vanilla JS, no build step)
tests/              unittest suite, including a dependency-free ASGI client
```

## Security

There is no authentication: anyone who can reach the port can view the stream,
take snapshots, and switch cameras. That is intended for a trusted LAN or a
dedicated kiosk. Do not expose it directly to the internet — put it behind a
reverse proxy with TLS and access control instead.

## Tech Stack

- **Python 3.10+** (developed on 3.11)
- **FastAPI** — async web framework
- **OpenCV** — camera capture
- **uvicorn** — ASGI server
- **Vanilla HTML/CSS/JS** — no frontend framework, no build step
