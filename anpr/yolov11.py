"""
anpr/yolov11.py

ANPR pipeline: YOLOv11n plate detection (anpr/crop.pt) followed by PARSeq
text recognition (anpr/ocr.ckpt, see anpr/parseq_infer.py).

Two models, two frameworks — confirmed 2026-08-19 by inspecting the pickled
class references inside each file rather than by loading them:

    crop.pt    ultralytics DetectionModel, YOLOv11n, trained run
               'yolo11n_lpr_run1'. Single job: find the plate region.
    ocr.ckpt   PARSeq, a PyTorch Lightning checkpoint. NOT a YOLO model and
               NOT loadable by ultralytics — plan.md's Phase 5 scope note had
               left "the OCR model might also be YOLO-format" open as a
               possibility, and it is not. See anpr/parseq_infer.py.

Both models are loaded lazily, on first use rather than at import, because
they are expensive on a Pi (the PARSeq checkpoint alone is ~353MB) and
because importing this module should stay cheap for callers that only want
the dataclasses or that never hit the ANPR path at all.

Usage:
    from anpr.yolov11 import ANPRPipeline

    pipeline = ANPRPipeline()
    for plate in pipeline.read_plates(frame_bgr):
        print(plate.text, plate.confidence, plate.box)
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from core.config import (
    ANPR_DETECTOR_CONFIDENCE,
    ANPR_DETECTOR_PATH,
    ANPR_OCR_CHECKPOINT_PATH,
    ANPR_OCR_MIN_CONFIDENCE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlateRead:
    """One plate: where the detector found it, and what the OCR made of it.

    `detector_confidence` and `ocr_confidence` are kept separate rather than
    combined into one score — they fail independently and mean different
    things. A high detector score with a low OCR score is "definitely a
    plate, unreadable"; the reverse is usually a false positive that happened
    to decode to something. Callers deciding whether to trust a match should
    look at both.
    """

    text: str
    ocr_confidence: float
    detector_confidence: float
    box: Tuple[int, int, int, int]  # x1, y1, x2, y2 in source-frame pixels

    @property
    def confidence(self) -> float:
        """Single combined score, for callers that just want one number."""
        return self.ocr_confidence * self.detector_confidence


@dataclass(frozen=True)
class PlateDetection:
    """A detector hit before any OCR has been attempted."""

    box: Tuple[int, int, int, int]
    confidence: float


class PlateDetector:
    """Thin wrapper over the trained ultralytics YOLOv11n plate detector."""

    def __init__(
        self,
        model_path: str = ANPR_DETECTOR_PATH,
        confidence: float = ANPR_DETECTOR_CONFIDENCE,
    ) -> None:
        self.model_path = model_path
        self.confidence = confidence
        self._model = None  # lazy, see module docstring

    def _ensure_model(self):
        if self._model is None:
            from ultralytics import YOLO  # deferred: heavy import

            logger.info("Loading plate detector from %s", self.model_path)
            self._model = YOLO(self.model_path)
        return self._model

    def detect(self, frame: np.ndarray) -> List[PlateDetection]:
        """Find plate regions in a BGR frame, best-scoring first.

        ultralytics is told verbose=False because this runs in a tight
        roadside capture loop — its default per-call stdout summary would
        bury the live test's own output.
        """
        model = self._ensure_model()
        results = model.predict(frame, conf=self.confidence, verbose=False)

        detections: List[PlateDetection] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                detections.append(
                    PlateDetection(box=(x1, y1, x2, y2), confidence=float(box.conf[0]))
                )

        detections.sort(key=lambda d: d.confidence, reverse=True)
        return detections


def crop_box(
    frame: np.ndarray, box: Sequence[int], padding: int = 4
) -> Optional[np.ndarray]:
    """Cut a detection box out of a frame, with a little margin.

    The padding exists because the detector is trained to bound the plate
    tightly, and PARSeq reads noticeably better with a few pixels of quiet
    zone around the glyphs than with characters flush against the crop edge.
    Clamped to the frame, so a box near the border just gets less padding
    rather than an out-of-range slice.

    Returns None for a degenerate (zero-area) crop, which a box clamped
    entirely outside the frame can otherwise produce — callers should skip
    those rather than hand an empty array to cv2.resize.
    """
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = box
    x1 = max(0, int(x1) - padding)
    y1 = max(0, int(y1) - padding)
    x2 = min(width, int(x2) + padding)
    y2 = min(height, int(y2) + padding)
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


class ANPRPipeline:
    """Detect plates in a frame and read them: crop.pt -> ocr.ckpt.

    This is the single seam both core/main.py's RFID-timeout fallback and
    anpr/live_test.py go through, so the two can never drift into different
    detection/OCR behaviour.
    """

    def __init__(
        self,
        detector_path: str = ANPR_DETECTOR_PATH,
        ocr_checkpoint_path: str = ANPR_OCR_CHECKPOINT_PATH,
        detector_confidence: float = ANPR_DETECTOR_CONFIDENCE,
        device: str = "cpu",
    ) -> None:
        self.detector = PlateDetector(detector_path, detector_confidence)
        self._ocr_checkpoint_path = ocr_checkpoint_path
        self._device = device
        self._ocr = None  # lazy, see module docstring

    def _ensure_ocr(self):
        if self._ocr is None:
            from anpr.parseq_infer import PARSeqRecognizer  # deferred: heavy import

            logger.info("Loading PARSeq OCR from %s", self._ocr_checkpoint_path)
            self._ocr = PARSeqRecognizer(self._ocr_checkpoint_path, device=self._device)
        return self._ocr

    def warmup(self) -> None:
        """Load both models now instead of on the first frame.

        Worth calling before a roadside run: the first inference otherwise
        pays several seconds of model loading, which on a motion-triggered
        capture is exactly the moment a vehicle is in frame.
        """
        self.detector._ensure_model()
        self._ensure_ocr()

    def read_plates(self, frame: np.ndarray) -> List[PlateRead]:
        """Full pipeline over one BGR frame. Best-scoring plate first.

        Every detection is OCR'd and returned, including low-confidence
        reads — filtering is the caller's decision (see
        ANPR_OCR_MIN_CONFIDENCE and `best_plate` below). The live test wants
        the rejects recorded too, so it can be used to tune the threshold
        against real roadside data instead of guessing.
        """
        detections = self.detector.detect(frame)
        if not detections:
            return []

        crops, kept = [], []
        for detection in detections:
            crop = crop_box(frame, detection.box)
            if crop is None:
                continue
            crops.append(crop)
            kept.append(detection)

        if not crops:
            return []

        # One batched forward pass — the ViT encoder dominates runtime on a
        # Pi CPU, so batching several plates from one frame is much cheaper
        # than a call each.
        ocr_results = self._ensure_ocr().read_batch(crops)

        return [
            PlateRead(
                text=text,
                ocr_confidence=ocr_confidence,
                detector_confidence=detection.confidence,
                box=detection.box,
            )
            for detection, (text, ocr_confidence) in zip(kept, ocr_results)
        ]

    def best_plate(
        self, frame: np.ndarray, min_ocr_confidence: float = ANPR_OCR_MIN_CONFIDENCE
    ) -> Optional[PlateRead]:
        """Highest-confidence readable plate in the frame, or None.

        This is the shape core/main.py's fallback wants: one answer, already
        confidence-gated, or nothing. Empty reads are rejected regardless of
        score — a blank string is never a usable plate match no matter how
        confident the model claims to be about it.
        """
        candidates = [
            plate
            for plate in self.read_plates(frame)
            if plate.text and plate.ocr_confidence >= min_ocr_confidence
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.confidence)
