"""Tests for the camera module: capture thread, frame slot, enumeration."""

from __future__ import annotations

import time
import unittest
from unittest import mock

import cv2
import numpy as np

import camera as camera_module
from camera import (
    CAPTURE_RESOLUTIONS,
    DocumentCamera,
    attach_names,
    detect_desktop_resolution,
    enumerate_cameras,
    fit_resolution,
    identify_cameras,
    mjpg_fourcc,
    parse_resolution,
    probe_resolutions,
    resolution_label,
)


class FakeCapture:
    """Stand-in for ``cv2.VideoCapture`` driven by class-level configuration.

    ``available`` maps index -> {"width", "height", "modes"}. When ``modes`` is
    given the fake behaves like a driver that only honours the nearest mode it
    supports, which is what ``probe_resolutions`` has to cope with.
    """

    available: dict = {}
    read_should_fail = False
    frame_matches_mode = False
    created: list = []

    def __init__(self, index=0, api=0):
        self.index = index
        self.api = api
        entry = FakeCapture.available.get(index)
        self.opened = entry is not None
        entry = entry or {}
        self._modes = entry.get("modes")
        self._width = float(entry.get("width", 1920.0))
        self._height = float(entry.get("height", 1080.0))
        self._fps = float(entry.get("fps", 30.0))
        self._req_width = self._width
        self._req_height = self._height
        self._width_requested = False
        self._height_requested = False
        self.released = False
        FakeCapture.created.append(self)

    def isOpened(self):
        return self.opened

    def release(self):
        self.released = True

    def getBackendName(self):
        return "FAKE"

    def set(self, prop, value):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            self._req_width = float(value)
            self._width_requested = True
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self._req_height = float(value)
            self._height_requested = True
        if self._modes and self._width_requested and self._height_requested:
            best = min(
                self._modes,
                key=lambda mode: abs(mode[0] - self._req_width) + abs(mode[1] - self._req_height),
            )
            self._width, self._height = float(best[0]), float(best[1])
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self._width
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self._height
        if prop == cv2.CAP_PROP_FPS:
            return self._fps
        return 0.0

    def read(self):
        if not self.opened or FakeCapture.read_should_fail:
            return False, None
        if FakeCapture.frame_matches_mode:
            shape = (int(self._height), int(self._width), 3)
        else:
            shape = (18, 24, 3)
        return True, np.zeros(shape, dtype=np.uint8)


class FakeCaptureTestCase(unittest.TestCase):
    """Shared setup: fake device layer, no PowerShell, cold enumeration cache."""

    def setUp(self):
        FakeCapture.available = {}
        FakeCapture.read_should_fail = False
        FakeCapture.frame_matches_mode = False
        FakeCapture.created = []
        camera_module._ENUM_CACHE.clear()

        patcher = mock.patch("camera.cv2.VideoCapture", FakeCapture)
        patcher.start()
        self.addCleanup(patcher.stop)

        names = mock.patch("camera.windows_camera_names", return_value=[])
        names.start()
        self.addCleanup(names.stop)

    def make_camera(self, **kwargs) -> DocumentCamera:
        cam = DocumentCamera(**kwargs)
        self.addCleanup(cam.stop, 2.0)
        return cam


class EnumerationTests(FakeCaptureTestCase):
    def test_probes_all_indices_once_then_caches(self):
        FakeCapture.available = {0: {}, 1: {}}
        first = enumerate_cameras()
        self.assertEqual([cam["index"] for cam in first], [0, 1])
        created = len(FakeCapture.created)

        second = enumerate_cameras()
        self.assertEqual([cam["index"] for cam in second], [0, 1])
        self.assertEqual(len(FakeCapture.created), created, "cached call must not probe again")

    def test_force_bypasses_the_cache(self):
        FakeCapture.available = {0: {}}
        enumerate_cameras()
        created = len(FakeCapture.created)
        enumerate_cameras(force=True)
        self.assertGreater(len(FakeCapture.created), created)

    def test_skips_the_device_in_use(self):
        FakeCapture.available = {0: {}, 1: {}}
        found = enumerate_cameras(skip_index=0)
        self.assertEqual([cam["index"] for cam in found], [1])

    def test_attach_names_exact_for_a_single_camera(self):
        cameras = [{"index": 0}]
        confidence = attach_names(cameras, [{"name": "IPEVO V4K", "device_id": "usb\\1"}])
        self.assertEqual(confidence, "exact")
        self.assertEqual(cameras[0]["name"], "IPEVO V4K")
        self.assertEqual(cameras[0]["device_id"], "usb\\1")

    def test_attach_names_flags_positional_guessing(self):
        cameras = [{"index": 0}, {"index": 1}]
        names = [
            {"name": "IPEVO V4K", "device_id": "a"},
            {"name": "USB Webcam", "device_id": "b"},
        ]
        confidence = attach_names(cameras, names)
        self.assertEqual(confidence, "assumed-order")
        self.assertEqual(cameras[0]["name"], "IPEVO V4K")

    def test_attach_names_withholds_names_when_counts_disagree(self):
        cameras = [{"index": 0}, {"index": 1}]
        confidence = attach_names(cameras, [{"name": "IPEVO V4K", "device_id": "a"}])
        self.assertEqual(confidence, "mismatched")
        self.assertNotIn("name", cameras[0])
        self.assertNotIn("name", cameras[1])

    def test_identify_prefers_document_camera_by_name(self):
        FakeCapture.available = {0: {}, 1: {}}
        with mock.patch("camera.windows_camera_names", return_value=[
            {"name": "IPEVO V4K", "device_id": "a"},
            {"name": "USB2.0 HD UVC WebCam", "device_id": "b"},
        ]):
            result = identify_cameras(force=True)

        self.assertEqual(result["names_confidence"], "assumed-order")
        self.assertEqual(result["best_guess_index"], 0)
        self.assertEqual([cam["index"] for cam in result["document_cameras"]], [0])
        self.assertEqual([cam["index"] for cam in result["other_cameras"]], [1])

    def test_identify_without_names_falls_back_to_resolution(self):
        FakeCapture.available = {0: {"width": 640.0, "height": 480.0},
                                 1: {"width": 1920.0, "height": 1080.0}}
        result = identify_cameras(force=True)
        self.assertEqual(result["names_confidence"], "unavailable")
        self.assertEqual(result["best_guess_index"], 1)

    def test_fourcc_helper_returns_a_fourcc(self):
        self.assertIsInstance(mjpg_fourcc(), int)


class CaptureThreadTests(FakeCaptureTestCase):
    def test_publishes_frames_and_reports_connected(self):
        FakeCapture.available = {0: {}}
        cam = self.make_camera(device_index=0)

        self.assertTrue(cam.start(wait=3.0))
        result = cam.wait_frame(0, 2.0)

        self.assertIsNotNone(result)
        seq, jpeg = result
        self.assertGreaterEqual(seq, 1)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"), "frame should be JPEG data")

        info = cam.get_info()
        self.assertTrue(info["connected"])
        self.assertEqual(info["state"], "streaming")
        self.assertEqual(info["device_index"], 0)
        self.assertEqual(info["width"], 1920)

    def test_missing_camera_reports_error_and_keeps_retrying(self):
        cam = self.make_camera()

        self.assertFalse(cam.start(wait=0.5))
        info = cam.get_info()
        self.assertEqual(info["state"], "error")
        self.assertIsNotNone(info["error"])
        self.assertTrue(cam._thread is not None and cam._thread.is_alive())

    def test_read_failures_trigger_reconnect(self):
        FakeCapture.available = {0: {}}
        cam = self.make_camera()
        self.assertTrue(cam.start(wait=3.0))

        # Stop the reopening from succeeding so the reconnecting state is observable.
        with mock.patch.object(cam, "_open_capture", return_value=None):
            FakeCapture.read_should_fail = True
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and cam.state != "reconnecting":
                time.sleep(0.02)
            self.assertEqual(cam.state, "reconnecting")

    def test_switch_to_another_index(self):
        FakeCapture.available = {0: {}, 1: {"width": 1280.0, "height": 720.0}}
        cam = self.make_camera()
        self.assertTrue(cam.start(wait=3.0))

        self.assertTrue(cam.switch_to(1, timeout=5.0))
        info = cam.get_info()
        self.assertEqual(info["device_index"], 1)
        self.assertEqual(info["width"], 1280)

    def test_wait_frame_returns_newest_frame_only(self):
        FakeCapture.available = {0: {}}
        cam = self.make_camera()
        self.assertTrue(cam.start(wait=3.0))

        first = cam.wait_frame(0, 2.0)
        self.assertIsNotNone(first)
        second = cam.wait_frame(first[0], 2.0)
        self.assertIsNotNone(second)
        self.assertGreater(second[0], first[0])

    def test_device_is_released_on_stop(self):
        FakeCapture.available = {0: {}}
        cam = self.make_camera()
        self.assertTrue(cam.start(wait=3.0))

        cam.stop()
        self.assertEqual(cam.state, "stopped")
        self.assertTrue(any(cap.released for cap in FakeCapture.created))


class SetResolutionTests(FakeCaptureTestCase):
    def test_changes_the_mode_while_streaming(self):
        FakeCapture.available = {0: {"modes": [(1920, 1200), (1280, 720)]}}
        cam = self.make_camera(width=1920, height=1080)
        self.assertTrue(cam.start(wait=3.0))
        self.assertEqual((cam.get_info()["width"], cam.get_info()["height"]), (1920, 1200))

        self.assertTrue(cam.set_resolution(1280, 720, timeout=5.0))

        info = cam.get_info()
        self.assertTrue(info["connected"])
        self.assertEqual((info["width"], info["height"]), (1280, 720))
        self.assertEqual((info["target_width"], info["target_height"]), (1280, 720))
        self.assertTrue(cam.wait_frame(0, 2.0) is not None, "stream must survive the change")

    def test_reports_a_driver_that_keeps_another_mode(self):
        FakeCapture.available = {0: {}}      # fake driver ignores the request
        cam = self.make_camera(width=1280, height=720)
        self.assertTrue(cam.start(wait=3.0))

        self.assertTrue(cam.set_resolution(1920, 1200, timeout=5.0))

        info = cam.get_info()
        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        negotiated = {(mode["width"], mode["height"]): mode for mode in cam.negotiated_modes()}
        self.assertFalse(negotiated[(1920, 1200)]["exact"])
        self.assertEqual(negotiated[(1920, 1200)]["actual_width"], 1920)

    def test_without_a_running_capture_only_the_target_changes(self):
        cam = self.make_camera()

        self.assertFalse(cam.set_resolution(1600, 1200))

        info = cam.get_info()
        self.assertEqual((info["target_width"], info["target_height"]), (1600, 1200))

    def test_rejects_a_non_positive_size(self):
        cam = self.make_camera()
        with self.assertRaises(ValueError):
            cam.set_resolution(0, 1080)


class ResolutionHelperTests(unittest.TestCase):
    def test_parse_resolution_formats(self):
        cases = {
            "1920x1080": (1920, 1080),
            "1280X720": (1280, 720),
            "1280×720": (1280, 720),
            " 640 * 480 ": (640, 480),
            "1024,768": (1024, 768),
            "800 600": (800, 600),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_resolution(text), expected, text)

    def test_parse_resolution_rejects_garbage(self):
        for text in ("", "1920", "1920x", "axb", "0x480", "-640x480"):
            with self.assertRaises(ValueError, msg=text):
                parse_resolution(text)

    def test_fit_resolution_picks_the_largest_mode_that_fits(self):
        cases = {
            (1920, 1080): (1600, 900),
            (2560, 1440): (1920, 1080),
            (3840, 2160): (2560, 1440),
            (1366, 768): (960, 540),
            (800, 600): (640, 480),
        }
        for desktop, expected in cases.items():
            self.assertEqual(fit_resolution(desktop, 0.9), expected, str(desktop))

    def test_fit_resolution_on_a_16_by_10_desktop(self):
        # 0.9 of 1920x1200 is too short for a 1200-line mode; at full margin the
        # matching 16:10 mode wins over the 16:9 one of the same width.
        self.assertEqual(fit_resolution((1920, 1200), 0.9), (1600, 900))
        self.assertEqual(fit_resolution((1920, 1200), 1.0), (1920, 1200))

    def test_fit_resolution_keeps_the_screen_shape_on_16_by_9(self):
        # 1920x1200 would fit inside 2560x1440, but the screen is 16:9, so the
        # matching mode is preferred even though it has fewer pixels.
        self.assertEqual(fit_resolution((2560, 1440), 0.9), (1920, 1080))

    def test_fit_resolution_never_exceeds_the_desktop(self):
        for desktop in ((1920, 1080), (1366, 768), (1280, 800), (800, 600)):
            width, height = fit_resolution(desktop, 0.9)
            self.assertLessEqual(width, desktop[0])
            self.assertLessEqual(height, desktop[1])

    def test_detect_desktop_resolution_is_a_size_or_none(self):
        detected = detect_desktop_resolution()
        self.assertTrue(detected is None or (detected[0] > 0 and detected[1] > 0))


class ResolutionCatalogTests(unittest.TestCase):
    def test_catalog_offers_16_by_10_and_is_sorted_largest_first(self):
        self.assertIn((1920, 1200), CAPTURE_RESOLUTIONS)
        self.assertIn((2560, 1600), CAPTURE_RESOLUTIONS)
        areas = [width * height for width, height in CAPTURE_RESOLUTIONS]
        self.assertEqual(areas, sorted(areas, reverse=True))
        self.assertEqual(resolution_label(1920, 1200), "1920 × 1200 (16:10)")
        self.assertEqual(resolution_label(1280, 720), "1280 × 720 (16:9)")


class ProbeResolutionTests(FakeCaptureTestCase):
    def test_probe_reports_the_modes_the_driver_accepts(self):
        FakeCapture.frame_matches_mode = True
        FakeCapture.available = {0: {"modes": [(1280, 720), (640, 480)]}}

        modes = probe_resolutions(0)

        self.assertEqual(
            [(mode["width"], mode["height"]) for mode in modes],
            [(1280, 720), (640, 480)],
        )
        self.assertTrue(all(mode["exact"] for mode in modes))

    def test_probe_returns_nothing_without_a_camera(self):
        self.assertEqual(probe_resolutions(0), [])


if __name__ == "__main__":
    unittest.main()
