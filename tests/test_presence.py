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

PresenceSensor is a two-stream camera (see its module docstring): a cheap
"lores" stream for motion, and a full-res "main" stream only read on demand
by capture_frame()/capture_fallback_frame(). The mocked capture_array()
below dispatches on the stream name to keep those two independent, matching
real Picamera2 behaviour.
"""
from unittest.mock import MagicMock

import numpy as np

from sensors.presence import PresenceSensor


def _yuv_frame(value, width, height):
    """A uniform-value YUV420 frame: first `height` rows/`width` cols are luma."""
    return np.full((height * 3 // 2, width), value, dtype=np.uint8)


def _make_sensor(
    monkeypatch,
    lores_values,
    main_frame=None,
    threshold=50.0,
    sustain=2,
    clear=2,
    resolution=(4, 4),
):
    """Build a PresenceSensor against a mocked Picamera2.

    lores_values[0] is consumed immediately as __init__'s baseline frame;
    the rest are handed out one per capture_array("lores") call, in order.
    capture_array("main") always returns `main_frame`, independent of the
    lores sequence.
    """
    lores_frames = iter([_yuv_frame(v, *resolution) for v in lores_values])

    def _capture_array(name="main"):
        if name == "lores":
            return next(lores_frames)
        return main_frame

    mock_picam2 = MagicMock()
    mock_picam2.capture_array.side_effect = _capture_array
    monkeypatch.setattr("sensors.presence.Picamera2", lambda: mock_picam2)
    monkeypatch.setattr("sensors.presence.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("sensors.presence.PRESENCE_RESOLUTION", resolution)
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


def test_capture_frame_reads_main_stream_and_rotates(monkeypatch):
    main_frame = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    sensor = _make_sensor(monkeypatch, [10], main_frame=main_frame)
    monkeypatch.setattr("sensors.presence.ANPR_CAPTURE_ROTATION", 0)  # passthrough

    frame = sensor.capture_frame()

    assert np.array_equal(frame, main_frame)


def test_capture_fallback_frame_writes_capture_frame_to_disk(monkeypatch, tmp_path):
    main_frame = np.zeros((2, 2, 3), dtype=np.uint8)
    sensor = _make_sensor(monkeypatch, [10], main_frame=main_frame)
    monkeypatch.setattr("sensors.presence.ANPR_CAPTURE_ROTATION", 0)  # passthrough
    mock_imwrite = MagicMock()
    monkeypatch.setattr("sensors.presence.cv2.imwrite", mock_imwrite)

    path = tmp_path / "capture.jpg"
    sensor.capture_fallback_frame(path)

    args, _ = mock_imwrite.call_args
    assert args[0] == str(path)
    assert np.array_equal(args[1], main_frame)


def test_cleanup_stops_and_closes_camera(monkeypatch):
    sensor = _make_sensor(monkeypatch, [10])

    sensor.cleanup()

    sensor._picam2.stop.assert_called_once()
    sensor._picam2.close.assert_called_once()
