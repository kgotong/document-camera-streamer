"""Contract tests for the HTTP/WebSocket surface of the streaming server."""

from __future__ import annotations

import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

import server
from tests.asgi_client import http_get, http_post

IDLE_INFO = {
    "connected": False,
    "state": "idle",
    "device_index": 0,
    "width": 0,
    "height": 0,
    "fps": None,
    "target_width": 1920,
    "target_height": 1080,
    "target_fps": 30,
    "backend": None,
    "frames_captured": 0,
    "last_frame_age_s": None,
    "error": None,
}

STREAMING_INFO = {
    **IDLE_INFO,
    "connected": True,
    "state": "streaming",
    "width": 1920,
    "height": 1080,
    "fps": 30.0,
    "backend": "DSHOW",
    "frames_captured": 42,
    "last_frame_age_s": 0.01,
}

CAMERA_ZERO = {
    "index": 0,
    "width": 1920,
    "height": 1080,
    "fps": 30.0,
    "backend": "DSHOW",
    "name": "IPEVO V4K",
    "name_confidence": "exact",
    "is_document_camera": True,
}


class ServerContractTestCase(unittest.TestCase):
    def setUp(self):
        self.info = dict(IDLE_INFO)

        info_patch = mock.patch.object(
            server.camera, "get_info", side_effect=lambda: dict(self.info)
        )
        info_patch.start()
        self.addCleanup(info_patch.stop)

        enum_patch = mock.patch.object(server, "enumerate_cameras", return_value=[])
        self.enumerate_mock = enum_patch.start()
        self.addCleanup(enum_patch.stop)

    # -- static UI ---------------------------------------------------------
    def test_root_serves_the_viewer(self):
        status, headers, body = http_get(server.app, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"Document Camera Stream", body)

    # -- health ------------------------------------------------------------
    def test_health_is_degraded_without_a_camera(self):
        status, _headers, body = http_get(server.app, "/health")
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["camera"]["connected"])

    def test_health_is_ok_while_streaming(self):
        self.info = dict(STREAMING_INFO)
        status, _headers, body = http_get(server.app, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    # -- info --------------------------------------------------------------
    def test_info_exposes_state_and_viewers(self):
        status, _headers, body = http_get(server.app, "/api/info")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for key in ("connected", "state", "device_index", "width", "height",
                    "target_width", "target_height", "viewers", "max_viewers",
                    "uptime_s", "display"):
            self.assertIn(key, payload)

    def test_info_reports_the_server_desktop_resolution(self):
        with mock.patch.object(server, "_display", (1920, 1080)):
            status, _headers, body = http_get(server.app, "/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["display"], {"width": 1920, "height": 1080})

    def test_info_reports_network_throughput(self):
        meter = server.ThroughputMeter()
        with mock.patch.object(server, "stream_meter", meter):
            meter.add(100_000)
            status, _headers, body = http_get(server.app, "/api/info")
        self.assertEqual(status, 200)
        network = json.loads(body)["network"]
        for key in ("stream_bps", "websocket_bps", "total_bps", "bytes_sent", "frame_bytes"):
            self.assertIn(key, network)
        self.assertGreater(network["total_bps"], 0)
        self.assertEqual(network["bytes_sent"], 100_000)

    # -- resolutions -------------------------------------------------------
    def test_resolution_list_offers_1920x1200_and_marks_the_current_mode(self):
        self.info = dict(STREAMING_INFO)
        status, _headers, body = http_get(server.app, "/api/resolutions")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        modes = {(mode["width"], mode["height"]): mode for mode in payload["resolutions"]}
        self.assertIn((1920, 1200), modes)
        self.assertEqual(modes[(1920, 1200)]["label"], "1920 × 1200 (16:10)")
        self.assertTrue(modes[(1920, 1080)]["current"])
        self.assertFalse(modes[(1920, 1200)]["current"])
        self.assertEqual(payload["current"]["width"], 1920)

    def test_resolution_change_accepts_query_parameters(self):
        self.info = dict(STREAMING_INFO)
        with mock.patch.object(server.camera, "set_resolution", return_value=True) as change:
            status, _headers, body = http_post(
                server.app, "/api/resolution?width=1920&height=1200"
            )
        change.assert_called_once()
        self.assertEqual(change.call_args.args[:2], (1920, 1200))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["requested"], {"width": 1920, "height": 1200})

    def test_resolution_change_accepts_a_json_body(self):
        self.info = dict(STREAMING_INFO)
        with mock.patch.object(server.camera, "set_resolution", return_value=True) as change:
            status, _headers, _body = http_post(
                server.app, "/api/resolution", body=json.dumps({"resolution": "1920x1200"}).encode()
            )
        self.assertEqual(status, 200)
        self.assertEqual(change.call_args.args[:2], (1920, 1200))

    def test_resolution_change_rejects_a_bad_json_body(self):
        status, _headers, _body = http_post(server.app, "/api/resolution", body=b"{not json")
        self.assertEqual(status, 422)

    def test_resolution_change_rejects_garbage(self):
        status, _headers, _body = http_post(server.app, "/api/resolution?resolution=wide")
        self.assertEqual(status, 422)

    def test_resolution_change_requires_a_complete_size(self):
        status, _headers, _body = http_post(server.app, "/api/resolution?width=1920")
        self.assertEqual(status, 422)

    def test_resolution_change_reports_conflict_when_the_camera_stays_down(self):
        with mock.patch.object(server.camera, "set_resolution", return_value=False):
            status, _headers, body = http_post(
                server.app, "/api/resolution?resolution=1920x1200"
            )
        self.assertEqual(status, 409)
        self.assertFalse(json.loads(body)["success"])
        self.assertTrue(json.loads(body)["error"])

    # -- camera list -------------------------------------------------------
    def test_camera_list_includes_document_flag(self):
        self.enumerate_mock.return_value = [dict(CAMERA_ZERO)]
        status, _headers, body = http_get(server.app, "/api/cameras")
        self.assertEqual(status, 200)
        cameras = json.loads(body)
        self.assertEqual(len(cameras), 1)
        self.assertTrue(cameras[0]["is_document_camera"])
        self.assertFalse(cameras[0]["in_use"])

    def test_camera_list_keeps_the_active_device_visible(self):
        self.info = dict(STREAMING_INFO)
        self.enumerate_mock.return_value = []      # active device is skipped
        status, _headers, body = http_get(server.app, "/api/cameras")
        self.assertEqual(status, 200)
        cameras = json.loads(body)
        self.assertEqual([cam["index"] for cam in cameras], [0])
        self.assertTrue(cameras[0]["in_use"])

    def test_camera_list_is_empty_when_nothing_is_connected(self):
        self.enumerate_mock.return_value = []
        status, _headers, body = http_get(server.app, "/api/cameras")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [], "an idle erroring camera must not be invented")

    def test_detect_endpoint_reports_name_confidence(self):
        detected = {
            "cameras": [dict(CAMERA_ZERO)],
            "document_cameras": [dict(CAMERA_ZERO)],
            "other_cameras": [],
            "best_guess_index": 0,
            "names_confidence": "exact",
        }
        with mock.patch.object(server, "identify_cameras", return_value=detected):
            status, _headers, body = http_get(server.app, "/api/cameras/detect")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["names_confidence"], "exact")

    # -- camera switching --------------------------------------------------
    def test_switching_to_an_unknown_index_returns_404(self):
        status, _headers, body = http_post(server.app, "/api/camera/5")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["detail"]["available"], [])

    def test_negative_index_is_rejected(self):
        status, _headers, _body = http_post(server.app, "/api/camera/-1")
        self.assertEqual(status, 422)

    def test_switching_to_a_known_camera_succeeds(self):
        self.enumerate_mock.return_value = [dict(CAMERA_ZERO)]
        self.info = dict(STREAMING_INFO)
        with mock.patch.object(server.camera, "switch_to", return_value=True) as switch:
            status, _headers, body = http_post(server.app, "/api/camera/0")
        switch.assert_called_once()
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["success"])

    def test_failed_switch_reports_conflict(self):
        self.enumerate_mock.return_value = [dict(CAMERA_ZERO)]
        with mock.patch.object(server.camera, "switch_to", return_value=False):
            status, _headers, body = http_post(server.app, "/api/camera/0")
        self.assertEqual(status, 409)
        self.assertFalse(json.loads(body)["success"])

    # -- snapshot ----------------------------------------------------------
    def test_snapshot_without_frames_returns_503(self):
        with mock.patch.object(server, "_snapshot_bytes", return_value=None):
            status, headers, _body = http_get(server.app, "/snapshot")
        self.assertEqual(status, 503)
        self.assertIn("retry-after", headers)

    def test_snapshot_returns_jpeg_bytes(self):
        frame = b"\xff\xd8\xff\xe0fake-jpeg"
        with mock.patch.object(server, "_snapshot_bytes", return_value=frame):
            status, headers, body = http_get(server.app, "/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertEqual(body, frame)

    # -- viewer limits -----------------------------------------------------
    def test_stream_rejects_viewers_over_the_limit(self):
        with mock.patch.object(server.viewers, "limit", 0):
            status, _headers, body = http_get(server.app, "/stream")
        self.assertEqual(status, 503)
        self.assertIn("Too many viewers", body.decode("utf-8", "replace"))

    def test_viewer_counter_never_goes_negative(self):
        counter = server.ViewerCounter(2)
        self.assertTrue(counter.acquire())
        self.assertTrue(counter.acquire())
        self.assertFalse(counter.acquire())
        counter.release()
        counter.release()
        counter.release()
        self.assertEqual(counter.active, 0)


class ThroughputMeterTests(unittest.TestCase):
    def test_reports_zero_before_any_traffic(self):
        stats = server.ThroughputMeter().stats()
        self.assertEqual(stats, {"bytes_per_second": 0, "bytes_sent": 0})

    def test_rate_tracks_recent_bytes(self):
        meter = server.ThroughputMeter(window=1.0)
        meter.add(120_000)
        stats = meter.stats()
        self.assertEqual(stats["bytes_sent"], 120_000)
        self.assertGreater(stats["bytes_per_second"], 0)

    def test_old_events_drop_out_of_the_window(self):
        meter = server.ThroughputMeter(window=0.2)
        meter.add(50_000)
        time.sleep(0.25)
        stats = meter.stats()
        self.assertEqual(stats["bytes_per_second"], 0)
        self.assertEqual(stats["bytes_sent"], 50_000, "lifetime total must survive")

    def test_ignores_empty_writes(self):
        meter = server.ThroughputMeter()
        meter.add(0)
        meter.add(-1)
        self.assertEqual(meter.stats(), {"bytes_per_second": 0, "bytes_sent": 0})


class CliOptionTests(unittest.TestCase):
    """The command line must configure the camera before the server starts."""

    def setUp(self):
        self.saved = {
            "width": server.camera.target_width,
            "height": server.camera.target_height,
            "fps": server.camera.target_fps,
            "quality": server.camera.jpeg_quality,
            "device": server.camera.device_index,
            "viewers": server.viewers.limit,
            "display": server._display,
            "pinned": server.DEVICE_PINNED,
        }

    def tearDown(self):
        saved = self.saved
        server.camera.target_width = saved["width"]
        server.camera.target_height = saved["height"]
        server.camera.target_fps = saved["fps"]
        server.camera.jpeg_quality = saved["quality"]
        server.camera.device_index = saved["device"]
        server.viewers.limit = saved["viewers"]
        server._display = saved["display"]
        server.DEVICE_PINNED = saved["pinned"]

    def parse(self, argv):
        return server.build_parser().parse_args(argv)

    def test_defaults_come_from_the_environment(self):
        args = self.parse([])
        self.assertEqual(args.port, server.PORT)
        self.assertEqual(args.host, server.HOST)
        self.assertIsNone(args.resolution)
        self.assertFalse(args.desktop_fit)

    def test_resolution_option_sets_the_capture_target(self):
        server.apply_options(self.parse(["--resolution", "1280x720"]))
        self.assertEqual(server.camera.target_width, 1280)
        self.assertEqual(server.camera.target_height, 720)

    def test_desktop_fit_uses_the_screen_override(self):
        server.apply_options(self.parse(["--desktop-fit", "--screen", "1920x1080"]))
        self.assertEqual((server.camera.target_width, server.camera.target_height), (1600, 900))
        self.assertEqual(server.display_info(), {"width": 1920, "height": 1080})

    def test_desktop_fit_can_use_a_custom_margin(self):
        server.apply_options(
            self.parse(["--desktop-fit", "--screen", "1920x1080", "--fit-margin", "1.0"])
        )
        self.assertEqual((server.camera.target_width, server.camera.target_height), (1920, 1080))

    def test_explicit_resolution_beats_desktop_fit(self):
        server.apply_options(
            self.parse(["--desktop-fit", "--resolution", "640x480", "--screen", "1920x1080"])
        )
        self.assertEqual((server.camera.target_width, server.camera.target_height), (640, 480))

    def test_desktop_fit_without_detection_asks_for_a_screen_override(self):
        with mock.patch.object(server, "detect_desktop_resolution", return_value=None):
            with self.assertRaises(SystemExit) as raised:
                server.apply_options(self.parse(["--desktop-fit"]))
        self.assertIn("--screen", str(raised.exception))

    def test_invalid_resolution_is_rejected(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                self.parse(["--resolution", "not-a-size"])

    def test_device_fps_quality_and_viewer_options(self):
        server.apply_options(
            self.parse(["--device", "3", "--fps", "15", "--jpeg-quality", "70",
                        "--max-viewers", "3"])
        )
        self.assertEqual(server.camera.device_index, 3)
        self.assertTrue(server.DEVICE_PINNED)
        self.assertEqual(server.camera.target_fps, 15)
        self.assertEqual(server.camera.jpeg_quality, 70)
        self.assertEqual(server.viewers.limit, 3)

    def test_listing_resolutions_reports_a_missing_camera(self):
        with mock.patch.object(server, "probe_resolutions", return_value=[]):
            with redirect_stdout(StringIO()) as output:
                code = server.print_resolutions(0)
        self.assertEqual(code, 1)
        self.assertIn("index 0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
