"""
anpr/live_test.py

Roadside live test: point the camera at moving traffic, run ONLY the ANPR
models over the frames (no RFID, no DB, no payment, no SMS), and store what
they saw for later review and threshold tuning.

This is deliberately standalone from core/main.py's toll flow. The point is
to measure how the trained models actually behave on real moving vehicles —
the PARSeq checkpoint reports val_accuracy ~23.8%, so the honest expectation
is a lot of partial and wrong reads. Recording *everything* (including
low-confidence rejects) is the whole value: it turns
ANPR_OCR_MIN_CONFIDENCE from a guess into something tunable against real
roadside data.

Two capture modes, because it isn't obvious in advance which suits fast
roadside traffic better:

  --mode motion      Watch a cheap 640x480 'lores' stream for frame-to-frame
                     motion and, once it sustains, fire a burst of full-res
                     captures. Far less disk churn and CPU, and it reuses the
                     same frame-differencing signal sensors/presence.py
                     already uses (thresholds come from core/config.py, where
                     they were set from a measured noise floor on this
                     hardware).
  --mode continuous  Grab full-res frames at a fixed rate and run detection on
                     every one. Catches vehicles that motion-triggering misses
                     (e.g. a car already in frame when the run starts), at the
                     cost of much heavier SD-card writes and CPU.

Both modes use a two-stream camera config: a low-res stream for the motion
signal and a high-res 'main' stream for anything actually fed to the models.
That matters — sensors/presence.py's 640x480 is fine for "did something
move" but far too coarse to read a plate from a moving vehicle.

Output, one directory per run under anpr/live_runs/ (gitignored):

    <run>/frames/000123.jpg        full frame, as fed to the detector
    <run>/crops/000123_0.jpg       each detected plate region
    <run>/detections.jsonl         one row per detection
    <run>/run_meta.json            config + totals, written on exit

Usage:
    # verify the camera's mounted orientation first — see --calibrate
    python3 -m anpr.live_test --calibrate

    python3 -m anpr.live_test --mode motion --duration 600
    python3 -m anpr.live_test --mode continuous --fps 2 --duration 300
"""

import argparse
import json
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from anpr.yolov11 import rotate_frame
from core.config import (
    ANPR_CAPTURE_RESOLUTION,
    ANPR_CAPTURE_ROTATION,
    ANPR_DETECTOR_CONFIDENCE,
    ANPR_OCR_MIN_CONFIDENCE,
    PRESENCE_CLEAR_FRAMES,
    PRESENCE_MOTION_THRESHOLD,
    PRESENCE_POLL_INTERVAL_SECONDS,
    PRESENCE_RESOLUTION,
    PRESENCE_SUSTAIN_FRAMES,
)

LIVE_RUNS_DIR = Path(__file__).resolve().parent / "live_runs"

# Local aliases: these two now live in core/config.py (ANPR_CAPTURE_*) so
# sensors/presence.py's real-time capture and this script's roadside data
# collection share one source of truth — see anpr.yolov11.rotate_frame's
# docstring. Kept under their original names here since both are part of
# this module's CLI surface (--rotate's default, run_meta.json's config).
CAPTURE_RESOLUTION = ANPR_CAPTURE_RESOLUTION
DEFAULT_ROTATION = ANPR_CAPTURE_ROTATION


@dataclass
class RunStats:
    """Totals for the run, written to run_meta.json on exit."""

    frames_processed: int = 0
    frames_with_detections: int = 0
    detections: int = 0
    reads_above_threshold: int = 0
    empty_reads: int = 0
    triggers: int = 0
    inference_ms: List[float] = field(default_factory=list)

    def summary(self) -> dict:
        times = self.inference_ms
        return {
            "frames_processed": self.frames_processed,
            "frames_with_detections": self.frames_with_detections,
            "detections": self.detections,
            "reads_above_threshold": self.reads_above_threshold,
            "empty_reads": self.empty_reads,
            "triggers": self.triggers,
            "detection_rate": (
                round(self.frames_with_detections / self.frames_processed, 4)
                if self.frames_processed
                else 0.0
            ),
            "mean_inference_ms": round(sum(times) / len(times), 1) if times else 0.0,
            "max_inference_ms": round(max(times), 1) if times else 0.0,
        }


class RoadsideCamera:
    """Two-stream Picamera2 wrapper: lores for motion, main for the models.

    Kept separate from sensors.presence.PresenceSensor rather than reusing it:
    that class owns its own Picamera2 instance at a fixed 640x480 single
    stream, and only one process can hold the camera at a time. The motion
    maths here is the same, but it runs on the lores stream of a camera that
    is simultaneously configured to hand back full-res frames on demand.
    """

    def __init__(self, rotation: int = DEFAULT_ROTATION) -> None:
        from picamera2 import Picamera2  # deferred: hardware-only import

        self.rotation = rotation
        self._picam2 = Picamera2()
        config = self._picam2.create_video_configuration(
            main={"size": CAPTURE_RESOLUTION, "format": "RGB888"},
            lores={"size": PRESENCE_RESOLUTION, "format": "YUV420"},
        )
        self._picam2.configure(config)
        self._picam2.start()
        # Same reason sensors/presence.py sleeps here: an unsettled
        # auto-exposure/white-balance frame reads as a large spurious diff
        # against whatever follows it.
        time.sleep(1.0)
        self._last_lores = self._lores_gray()

    def _lores_gray(self) -> np.ndarray:
        """Grayscale plane of the lores stream.

        The lores stream is YUV420, whose first HxW bytes are the Y (luma)
        plane — already grayscale, so this needs no colour conversion and no
        channel averaging, unlike presence.py's RGB888 stream.
        """
        frame = self._picam2.capture_array("lores")
        height = PRESENCE_RESOLUTION[1]
        return frame[:height, : PRESENCE_RESOLUTION[0]].astype(np.int16)

    def motion_diff(self) -> float:
        """Mean absolute luma change since the previous lores frame."""
        frame = self._lores_gray()
        diff = float(np.abs(frame - self._last_lores).mean())
        self._last_lores = frame
        return diff

    def capture_frame(self) -> np.ndarray:
        """One full-res BGR frame, rotated upright, ready for the models.

        picamera2's "RGB888" format actually hands back channels in BGR order,
        which is what OpenCV and both models' preprocessing already expect —
        so no colour conversion happens here, deliberately.
        """
        return rotate_frame(self._picam2.capture_array("main"), self.rotation)

    def cleanup(self) -> None:
        self._picam2.stop()
        self._picam2.close()


class DetectionWriter:
    """Owns the run directory and appends to detections.jsonl.

    The JSONL handle is flushed after every row rather than left to buffer:
    a roadside run ends by Ctrl-C or by the Pi losing power, and a half-
    buffered results file would lose exactly the data the run was for.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.frames_dir = run_dir / "frames"
        self.crops_dir = run_dir / "crops"
        for directory in (self.frames_dir, self.crops_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._jsonl = (run_dir / "detections.jsonl").open("a", encoding="utf-8")

    def save_frame(self, frame: np.ndarray, frame_id: int) -> Path:
        path = self.frames_dir / f"{frame_id:06d}.jpg"
        cv2.imwrite(str(path), frame)
        return path

    def save_crop(self, crop: np.ndarray, frame_id: int, index: int) -> Path:
        path = self.crops_dir / f"{frame_id:06d}_{index}.jpg"
        cv2.imwrite(str(path), crop)
        return path

    def write_row(self, row: dict) -> None:
        self._jsonl.write(json.dumps(row) + "\n")
        self._jsonl.flush()

    def write_meta(self, meta: dict) -> None:
        (self.run_dir / "run_meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    def close(self) -> None:
        self._jsonl.close()


def _process_frame(
    frame: np.ndarray,
    frame_id: int,
    pipeline,
    writer: DetectionWriter,
    stats: RunStats,
    mode: str,
    min_confidence: float,
    save_empty: bool,
) -> int:
    """Run the models over one frame, persist what they found, return N detections."""
    started = time.perf_counter()
    plates = pipeline.read_plates(frame)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    stats.frames_processed += 1
    stats.inference_ms.append(elapsed_ms)

    if not plates:
        if save_empty:
            writer.save_frame(frame, frame_id)
        return 0

    stats.frames_with_detections += 1
    frame_path = writer.save_frame(frame, frame_id)
    timestamp = datetime.now(timezone.utc).isoformat()

    from anpr.yolov11 import crop_box  # local import keeps module import cheap

    for index, plate in enumerate(plates):
        stats.detections += 1
        if not plate.text:
            stats.empty_reads += 1
        if plate.text and plate.ocr_confidence >= min_confidence:
            stats.reads_above_threshold += 1

        crop_path: Optional[Path] = None
        crop = crop_box(frame, plate.box)
        if crop is not None and crop.size:
            crop_path = writer.save_crop(crop, frame_id, index)

        writer.write_row(
            {
                "timestamp": timestamp,
                "mode": mode,
                "frame_id": frame_id,
                "frame_path": str(frame_path.relative_to(writer.run_dir)),
                "crop_path": (
                    str(crop_path.relative_to(writer.run_dir)) if crop_path else None
                ),
                "plate_text": plate.text,
                "ocr_confidence": round(plate.ocr_confidence, 4),
                "detector_confidence": round(plate.detector_confidence, 4),
                "combined_confidence": round(plate.confidence, 4),
                "above_threshold": bool(
                    plate.text and plate.ocr_confidence >= min_confidence
                ),
                "box": list(plate.box),
                "inference_ms": round(elapsed_ms, 1),
            }
        )

        marker = "OK " if plate.text and plate.ocr_confidence >= min_confidence else "low"
        print(
            f"  [{marker}] {plate.text or '<empty>':<12} "
            f"ocr={plate.ocr_confidence:.2f} det={plate.detector_confidence:.2f}"
        )

    return len(plates)


def run_motion_mode(
    camera: RoadsideCamera,
    pipeline,
    writer: DetectionWriter,
    stats: RunStats,
    args,
    should_stop,
) -> None:
    """Wait for sustained motion, then burst-capture and run the models.

    The burst matters for moving traffic: a single frame fired at the instant
    motion is confirmed often catches the vehicle mid-frame or motion-blurred,
    and the plate may not be legible in that particular one. Capturing
    several in quick succession gives the detector more than one chance at
    the same vehicle.

    After a burst, the loop waits for motion to settle (PRESENCE_CLEAR_FRAMES
    consecutive quiet frames) before re-arming, so one vehicle passing
    through doesn't immediately re-trigger as a second one.
    """
    frame_id = 0
    streak = 0
    print(f"Watching for motion (threshold={PRESENCE_MOTION_THRESHOLD}) — Ctrl-C to stop")

    while not should_stop():
        if camera.motion_diff() >= PRESENCE_MOTION_THRESHOLD:
            streak += 1
        else:
            streak = 0

        if streak < PRESENCE_SUSTAIN_FRAMES:
            time.sleep(PRESENCE_POLL_INTERVAL_SECONDS)
            continue

        stats.triggers += 1
        print(f"\n[trigger {stats.triggers}] motion sustained — capturing {args.burst} frame(s)")
        for _ in range(args.burst):
            if should_stop():
                break
            frame_id += 1
            _process_frame(
                camera.capture_frame(), frame_id, pipeline, writer, stats,
                "motion", args.min_confidence, args.save_empty,
            )

        # Re-arm: drain motion until the scene is quiet again.
        clear = 0
        while clear < PRESENCE_CLEAR_FRAMES and not should_stop():
            clear = clear + 1 if camera.motion_diff() < PRESENCE_MOTION_THRESHOLD else 0
            time.sleep(PRESENCE_POLL_INTERVAL_SECONDS)
        streak = 0


def run_continuous_mode(
    camera: RoadsideCamera,
    pipeline,
    writer: DetectionWriter,
    stats: RunStats,
    args,
    should_stop,
) -> None:
    """Capture and process at a fixed rate, regardless of motion.

    The sleep is computed against the frame's own elapsed time rather than a
    flat interval, because detection+OCR on a Pi CPU can easily take longer
    than the requested period. When it does, this simply runs as fast as it
    can instead of accumulating an ever-growing backlog of overdue frames.
    """
    frame_id = 0
    period = 1.0 / args.fps
    print(f"Capturing continuously at {args.fps} fps — Ctrl-C to stop")

    while not should_stop():
        started = time.perf_counter()
        frame_id += 1
        found = _process_frame(
            camera.capture_frame(), frame_id, pipeline, writer, stats,
            "continuous", args.min_confidence, args.save_empty,
        )
        if not found and frame_id % 10 == 0:
            print(f"  ...{frame_id} frames, {stats.detections} detections so far")
        time.sleep(max(0.0, period - (time.perf_counter() - started)))


def run_calibrate(rotation: int) -> int:
    """Save one frame at each of the 4 rotations, so the mount can be checked.

    plan.md records that this camera is physically mounted ~90° from upright
    and that the sensor's own metadata does not report it. Both models were
    trained on upright plates, so getting this wrong silently costs accuracy
    rather than failing loudly — worth 30 seconds of checking before a run.
    """
    out_dir = LIVE_RUNS_DIR / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)

    camera = RoadsideCamera(rotation=0)  # capture raw, rotate below
    try:
        time.sleep(1.0)
        raw = camera._picam2.capture_array("main")
    finally:
        camera.cleanup()

    for degrees in (0, 90, 180, 270):
        path = out_dir / f"rotate_{degrees:03d}.jpg"
        cv2.imwrite(str(path), rotate_frame(raw, degrees))
        print(f"wrote {path}")

    print(
        f"\nOpen those four and pick the upright one, then pass it as --rotate "
        f"(current default is {rotation})."
    )
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mode", choices=("motion", "continuous"), default="motion",
        help="motion: burst-capture on sustained motion (default). "
             "continuous: fixed-rate capture of every frame.",
    )
    parser.add_argument(
        "--fps", type=float, default=2.0,
        help="Frames per second in continuous mode (default: 2)",
    )
    parser.add_argument(
        "--burst", type=int, default=3,
        help="Full-res frames captured per motion trigger (default: 3)",
    )
    parser.add_argument(
        "--duration", type=float, default=0.0,
        help="Stop after this many seconds (default: 0 = run until Ctrl-C)",
    )
    parser.add_argument(
        "--rotate", type=int, choices=(0, 90, 180, 270), default=DEFAULT_ROTATION,
        help=f"Clockwise rotation applied to captures (default: {DEFAULT_ROTATION}). "
             f"Run --calibrate to check.",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=ANPR_OCR_MIN_CONFIDENCE,
        help=f"OCR confidence at/above which a read counts as usable "
             f"(default: {ANPR_OCR_MIN_CONFIDENCE}). Lower-confidence reads are "
             f"still recorded, just flagged above_threshold=false.",
    )
    parser.add_argument(
        "--detector-confidence", type=float, default=ANPR_DETECTOR_CONFIDENCE,
        help=f"Minimum YOLO box confidence (default: {ANPR_DETECTOR_CONFIDENCE})",
    )
    parser.add_argument(
        "--save-empty", action="store_true",
        help="Also save frames where nothing was detected (useful for diagnosing "
             "misses; costs a lot more disk)",
    )
    parser.add_argument(
        "--calibrate", action="store_true",
        help="Save one frame at each rotation and exit, to check the camera mount",
    )
    args = parser.parse_args(argv)

    if args.calibrate:
        return run_calibrate(args.rotate)

    run_dir = LIVE_RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Run directory: {run_dir}")

    # Ctrl-C sets a flag rather than raising inside the loop, so the run
    # always reaches the meta/summary write below with its stats intact.
    stopping = {"now": False}

    def _stop(_signum, _frame):
        if stopping["now"]:  # second Ctrl-C: give up immediately
            raise KeyboardInterrupt
        stopping["now"] = True
        print("\nStopping after this frame...")

    signal.signal(signal.SIGINT, _stop)

    deadline = time.monotonic() + args.duration if args.duration > 0 else None

    def should_stop() -> bool:
        return stopping["now"] or (deadline is not None and time.monotonic() >= deadline)

    from anpr.yolov11 import ANPRPipeline

    pipeline = ANPRPipeline(detector_confidence=args.detector_confidence)
    print("Loading models (first load is slow on a Pi)...")
    pipeline.warmup()
    print("Models ready.")

    writer = DetectionWriter(run_dir)
    stats = RunStats()
    started_at = datetime.now(timezone.utc).isoformat()
    wall_start = time.monotonic()

    camera = RoadsideCamera(rotation=args.rotate)
    try:
        runner = run_motion_mode if args.mode == "motion" else run_continuous_mode
        runner(camera, pipeline, writer, stats, args, should_stop)
    except KeyboardInterrupt:
        pass
    finally:
        camera.cleanup()
        summary = stats.summary()
        writer.write_meta(
            {
                "started_at": started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - wall_start, 1),
                "config": {
                    "mode": args.mode,
                    "fps": args.fps,
                    "burst": args.burst,
                    "rotate": args.rotate,
                    "capture_resolution": list(CAPTURE_RESOLUTION),
                    "min_confidence": args.min_confidence,
                    "detector_confidence": args.detector_confidence,
                    "save_empty": args.save_empty,
                },
                "totals": summary,
            }
        )
        writer.close()

        print("\n=== Run summary ===")
        for key, value in summary.items():
            print(f"  {key:<24} {value}")
        print(f"\nResults: {run_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
