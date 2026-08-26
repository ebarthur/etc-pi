"""
sensors/presence.py

Camera-based vehicle-presence trigger for the Smart Toll orchestrator, and
the frame source for ANPR (anpr/yolov11.py) once RFID's window times out.

No dedicated presence sensor (IR break-beam, ultrasonic, inductive loop) is
available yet, so this runs a two-stream camera config (mirroring
anpr/live_test.py's RoadsideCamera): a cheap "lores" stream it polls for
motion, and a full-res "main" stream it only reads from on demand. A vehicle
is flagged "arrived" once the mean absolute luma difference between
consecutive lores frames stays above PRESENCE_MOTION_THRESHOLD for
PRESENCE_SUSTAIN_FRAMES in a row (debounces a single noisy frame from a real
approach). See core/config.py for how the threshold was picked -- from a
real measured noise floor on this hardware, not guessed -- and note it was
measured against a full-size vehicle at roadside distance, so it's worth
re-checking against the real noise floor before trusting it on a scaled-down
rig where the subject fills much less of the frame.

The interface (wait_for_vehicle / wait_until_clear / capture_frame) is
deliberately hardware-agnostic so core/main.py wouldn't need to change if
this is ever swapped out for a real presence sensor plus a separate camera.

All `self._picam2.capture_array(...)` calls go through `self._lock`: in
dev mode, core/dev_stream.py's MJPEG preview thread calls capture_frame()
concurrently with this class's own main-loop thread (wait_for_vehicle's
lores polling, wait_until_clear, and the RFID-timeout capture_frame() call)
-- Picamera2 doesn't document concurrent capture_array() calls from
multiple threads as safe, so this serializes them rather than assuming it
works.

Usage (from core/main.py):
    from sensors.presence import PresenceSensor

    presence = PresenceSensor()
    presence.wait_for_vehicle()   # blocks until motion is detected
    ...                            # RFID window, then ANPR on capture_frame()
    presence.wait_until_clear()   # blocks until motion settles back down
    presence.cleanup()
"""

import logging
import threading
import time
from pathlib import Path
from typing import Union

import cv2
import numpy as np
from picamera2 import Picamera2

from anpr.yolov11 import rotate_frame
from core.config import (
    ANPR_CAPTURE_RESOLUTION,
    ANPR_CAPTURE_ROTATION,
    PRESENCE_CLEAR_FRAMES,
    PRESENCE_MOTION_THRESHOLD,
    PRESENCE_POLL_INTERVAL_SECONDS,
    PRESENCE_RESOLUTION,
    PRESENCE_SUSTAIN_FRAMES,
)

logger = logging.getLogger(__name__)


class PresenceSensor:
    """Frame-differencing motion trigger, standing in for dedicated presence hardware."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._picam2 = Picamera2()
        config = self._picam2.create_video_configuration(
            main={"size": ANPR_CAPTURE_RESOLUTION, "format": "RGB888"},
            lores={"size": PRESENCE_RESOLUTION, "format": "YUV420"},
        )
        self._picam2.configure(config)
        self._picam2.start()
        # Let auto-exposure/white-balance settle before the first real diff --
        # an unsettled first frame reads as a large, spurious diff against
        # whatever comes right after it (confirmed during manual capture
        # testing earlier this session).
        time.sleep(1.0)
        self._last_frame = self._lores_luma()

    def _lores_luma(self) -> np.ndarray:
        """Grayscale (Y-plane) frame from the cheap lores stream.

        YUV420's first HxW bytes are the Y (luma) plane -- already
        grayscale, so no colour conversion or channel averaging needed.
        """
        with self._lock:
            frame = self._picam2.capture_array("lores")
        width, height = PRESENCE_RESOLUTION
        return frame[:height, :width].astype(np.int16)

    def _frame_diff(self) -> float:
        frame = self._lores_luma()
        diff = float(np.abs(frame - self._last_frame).mean())
        self._last_frame = frame
        return diff

    def wait_for_vehicle(self) -> None:
        """Block until motion stays above threshold for PRESENCE_SUSTAIN_FRAMES in a row."""
        streak = 0
        while streak < PRESENCE_SUSTAIN_FRAMES:
            diff = self._frame_diff()
            if diff >= PRESENCE_MOTION_THRESHOLD:
                streak += 1
                logger.debug("motion diff=%.2f >= threshold=%.2f, streak=%d/%d", diff, PRESENCE_MOTION_THRESHOLD, streak, PRESENCE_SUSTAIN_FRAMES)
            else:
                if streak:
                    logger.debug("motion diff=%.2f below threshold, streak reset", diff)
                streak = 0
            time.sleep(PRESENCE_POLL_INTERVAL_SECONDS)
        logger.info("Vehicle arrival: motion sustained for %d frames", PRESENCE_SUSTAIN_FRAMES)

    def wait_until_clear(self) -> None:
        """Block until motion drops below threshold for PRESENCE_CLEAR_FRAMES in a row.

        Debounces re-arming so a vehicle still sitting in frame (e.g. mid-charge)
        doesn't immediately count as a second arrival.
        """
        streak = 0
        while streak < PRESENCE_CLEAR_FRAMES:
            diff = self._frame_diff()
            if diff < PRESENCE_MOTION_THRESHOLD:
                streak += 1
                logger.debug("motion diff=%.2f < threshold, clear streak=%d/%d", diff, streak, PRESENCE_CLEAR_FRAMES)
            else:
                if streak:
                    logger.debug("motion diff=%.2f still above threshold, clear streak reset", diff)
                streak = 0
            time.sleep(PRESENCE_POLL_INTERVAL_SECONDS)
        logger.info("Scene clear -- re-armed for next arrival")

    def capture_frame(self) -> np.ndarray:
        """One full-res BGR frame from the "main" stream, rotated upright and
        ready for anpr.yolov11.ANPRPipeline.

        picamera2's "RGB888" format actually hands back channels in BGR
        order, which is what OpenCV and both ANPR models' preprocessing
        already expect -- so no colour conversion happens here, deliberately
        (matches anpr/live_test.py's RoadsideCamera.capture_frame()).
        """
        with self._lock:
            raw = self._picam2.capture_array("main")
        frame = rotate_frame(raw, ANPR_CAPTURE_ROTATION)
        logger.debug("Captured full-res frame from 'main' stream: shape=%s", frame.shape)
        return frame

    def capture_fallback_frame(self, path: Union[str, Path]) -> None:
        """Save capture_frame()'s output to disk, e.g. for audit/debugging."""
        frame = self.capture_frame()
        cv2.imwrite(str(path), frame)
        logger.debug("Saved fallback frame to %s", path)

    def cleanup(self) -> None:
        self._picam2.stop()
        self._picam2.close()
