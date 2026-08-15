"""
tests/test_presence.py

Tests for sensors/presence.py against a mocked Picamera2 (see conftest.py
for why picamera2 is stubbed at collection time). The frame-diff math runs
for real against real numpy arrays -- only the camera hardware is faked.

Frame sequences below use a smooth ramp (10 -> 80 -> 150 -> 230, etc.)
rather than instant jumps, to mirror how a real approaching vehicle
actually reads: continuous frame-to-frame change while it's still moving
into frame, not one single jump followed by silence. See sensors/presence.py
and core/config.py for why "sustained consecutive diffs above threshold" is
the arrival signal, not "any diff from a fixed baseline".
"""
from unittest.mock import MagicMock

import numpy as np

from sensors.presence import PresenceSensor


def _frame(value, shape=(4, 4, 3)):
    return np.full(shape, value, dtype=np.uint8)


def _make_sensor(monkeypatch, frame_values, threshold=50.0, sustain=2, clear=2):
    """Build a PresenceSensor against a mocked Picamera2.

    frame_values[0] is consumed immediately as __init__'s baseline frame;
    the rest are handed out one per capture_array() call in order.
    """
    mock_picam2 = MagicMock()
    mock_picam2.capture_array.side_effect = [_frame(v) for v in frame_values]
    monkeypatch.setattr("sensors.presence.Picamera2", lambda: mock_picam2)
    monkeypatch.setattr("sensors.presence.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("sensors.presence.PRESENCE_MOTION_THRESHOLD", threshold)
    monkeypatch.setattr("sensors.presence.PRESENCE_SUSTAIN_FRAMES", sustain)
    monkeypatch.setattr("sensors.presence.PRESENCE_CLEAR_FRAMES", clear)
    return PresenceSensor()


def test_wait_for_vehicle_returns_once_motion_sustained(monkeypatch):
    # baseline=10; then a ramp giving 2 consecutive above-threshold diffs
    sensor = _make_sensor(monkeypatch, [10, 10, 80, 150], threshold=50.0, sustain=2)

    sensor.wait_for_vehicle()

    assert sensor._picam2.capture_array.call_count == 4


def test_wait_for_vehicle_resets_streak_on_a_quiet_frame(monkeypatch):
    # motion (streak 1), a quiet frame (resets to 0), then motion again (streak 1, 2)
    sensor = _make_sensor(monkeypatch, [10, 80, 85, 160, 230], threshold=50.0, sustain=2)

    sensor.wait_for_vehicle()

    assert sensor._picam2.capture_array.call_count == 5


def test_wait_until_clear_returns_once_motion_settles(monkeypatch):
    # baseline=250 (as if a vehicle just filled the frame); still moving away,
    # then settles for 2 consecutive quiet frames
    sensor = _make_sensor(monkeypatch, [250, 10, 12, 13], threshold=50.0, clear=2)

    sensor.wait_until_clear()

    assert sensor._picam2.capture_array.call_count == 4


def test_capture_fallback_frame_delegates_to_picam2(monkeypatch, tmp_path):
    sensor = _make_sensor(monkeypatch, [10])

    path = tmp_path / "capture.jpg"
    sensor.capture_fallback_frame(path)

    sensor._picam2.capture_file.assert_called_once_with(str(path))


def test_cleanup_stops_and_closes_camera(monkeypatch):
    sensor = _make_sensor(monkeypatch, [10])

    sensor.cleanup()

    sensor._picam2.stop.assert_called_once()
    sensor._picam2.close.assert_called_once()
