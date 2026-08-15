"""
sensors/presence.py

Camera-based vehicle-presence trigger for the Smart Toll orchestrator.

No dedicated presence sensor (IR break-beam, ultrasonic, inductive loop) is
available yet, so this reuses the camera already wired up for the future
ANPR fallback (see anpr/yolov11.py, plan.md Phase 5): it grabs a low-res
frame on a short poll interval and flags "vehicle arrived" once the mean
absolute pixel difference between consecutive frames stays above
PRESENCE_MOTION_THRESHOLD for PRESENCE_SUSTAIN_FRAMES in a row (debounces a
single noisy frame from a real approach). See core/config.py for how the
threshold was picked -- from a real measured noise floor on this hardware,
not guessed.

The interface (wait_for_vehicle / wait_until_clear) is deliberately
hardware-agnostic so core/main.py wouldn't need to change if this is ever
swapped out for a real presence sensor.

Usage (from core/main.py):
    from sensors.presence import PresenceSensor

    presence = PresenceSensor()
    presence.wait_for_vehicle()   # blocks until motion is detected
    ...                            # RFID window / ANPR fallback here
    presence.wait_until_clear()   # blocks until motion settles back down
    presence.cleanup()
"""

import time
from pathlib import Path
from typing import Union

import numpy as np
from picamera2 import Picamera2

from core.config import (
    PRESENCE_CLEAR_FRAMES,
    PRESENCE_MOTION_THRESHOLD,
    PRESENCE_POLL_INTERVAL_SECONDS,
    PRESENCE_RESOLUTION,
    PRESENCE_SUSTAIN_FRAMES,
)


class PresenceSensor:
    """Frame-differencing motion trigger, standing in for dedicated presence hardware."""

    def __init__(self) -> None:
        self._picam2 = Picamera2()
        config = self._picam2.create_video_configuration(
            main={"size": PRESENCE_RESOLUTION, "format": "RGB888"}
        )
        self._picam2.configure(config)
        self._picam2.start()
        # Let auto-exposure/white-balance settle before the first real diff --
        # an unsettled first frame reads as a large, spurious diff against
        # whatever comes right after it (confirmed during manual capture
        # testing earlier this session).
        time.sleep(1.0)
        self._last_frame = self._grayscale_frame()

    def _grayscale_frame(self) -> np.ndarray:
        frame = self._picam2.capture_array()
        return frame.mean(axis=2)  # cheap grayscale: average the RGB channels

    def _frame_diff(self) -> float:
        frame = self._grayscale_frame()
        diff = float(np.abs(frame.astype(np.int16) - self._last_frame.astype(np.int16)).mean())
        self._last_frame = frame
        return diff

    def wait_for_vehicle(self) -> None:
        """Block until motion stays above threshold for PRESENCE_SUSTAIN_FRAMES in a row."""
        streak = 0
        while streak < PRESENCE_SUSTAIN_FRAMES:
            diff = self._frame_diff()
            streak = streak + 1 if diff >= PRESENCE_MOTION_THRESHOLD else 0
            time.sleep(PRESENCE_POLL_INTERVAL_SECONDS)

    def wait_until_clear(self) -> None:
        """Block until motion drops below threshold for PRESENCE_CLEAR_FRAMES in a row.

        Debounces re-arming so a vehicle still sitting in frame (e.g. mid-charge)
        doesn't immediately count as a second arrival.
        """
        streak = 0
        while streak < PRESENCE_CLEAR_FRAMES:
            diff = self._frame_diff()
            streak = streak + 1 if diff < PRESENCE_MOTION_THRESHOLD else 0
            time.sleep(PRESENCE_POLL_INTERVAL_SECONDS)

    def capture_fallback_frame(self, path: Union[str, Path]) -> None:
        """Save a still for ANPR to consume, once anpr/yolov11.py exists.

        Saved at PRESENCE_RESOLUTION (640x480), not full sensor resolution --
        fine for now since there's no model to feed it yet. Revisit
        resolution/stream config (e.g. a second full-res "main" stream
        alongside this "lores" one) together with the known ~90-degree
        capture rotation (see plan.md) once Phase 5 actually starts.
        """
        self._picam2.capture_file(str(path))

    def cleanup(self) -> None:
        self._picam2.stop()
        self._picam2.close()
