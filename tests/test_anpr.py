"""
tests/test_anpr.py

Tests for the ANPR pipeline (anpr/yolov11.py, anpr/live_test.py) and the
PARSeq tokenizer (anpr/parseq_infer.py).

Deliberately covers only logic that runs WITHOUT the trained models: both
anpr/crop.pt and anpr/ocr.ckpt are gitignored (5.4MB and 353MB), so a clean
checkout does not have them and these tests must still pass there. The
detector and recognizer are therefore mocked at their boundaries — what's
under test is the surrounding pipeline behaviour (cropping, sorting,
confidence gating, rotation, stats), not the weights.

See conftest.py for why picamera2 and friends are stubbed at collection time.
"""
import numpy as np
import pytest

from anpr.live_test import RunStats, rotate_frame
from anpr.yolov11 import ANPRPipeline, PlateDetection, PlateDetector, PlateRead, crop_box


def _frame(height=100, width=200, value=128):
    return np.full((height, width, 3), value, dtype=np.uint8)


# --- crop_box ---------------------------------------------------------------


def test_crop_box_applies_padding_around_the_box():
    frame = _frame(height=100, width=200)

    crop = crop_box(frame, (50, 40, 70, 60), padding=5)

    # 20x20 box grown by 5px on every side
    assert crop.shape[:2] == (30, 30)


def test_crop_box_clamps_padding_at_the_frame_edge():
    frame = _frame(height=100, width=200)

    # Box flush against the top-left corner: padding can only extend right/down.
    crop = crop_box(frame, (0, 0, 10, 10), padding=5)

    assert crop.shape[:2] == (15, 15)


def test_crop_box_returns_none_for_a_degenerate_box():
    frame = _frame()

    # Entirely off the right edge — clamps to zero width rather than slicing.
    assert crop_box(frame, (500, 500, 600, 600), padding=0) is None


# --- PlateRead --------------------------------------------------------------


def test_plate_read_combines_both_confidences():
    plate = PlateRead(
        text="GT1234", ocr_confidence=0.5, detector_confidence=0.8, box=(0, 0, 1, 1)
    )

    assert plate.confidence == pytest.approx(0.4)


# --- PlateDetector ----------------------------------------------------------


class _FakeBox:
    def __init__(self, xyxy, conf):
        self.xyxy = [np.array(xyxy, dtype=np.float32)]
        self.conf = [conf]


class _FakeResult:
    def __init__(self, boxes):
        self.boxes = boxes


def _detector_returning(boxes):
    detector = PlateDetector.__new__(PlateDetector)
    detector.confidence = 0.25
    detector.model_path = "<fake>"

    class _FakeModel:
        def predict(self, _frame, **_kwargs):
            return [_FakeResult(boxes)]

    detector._model = _FakeModel()
    return detector


def test_detect_returns_boxes_sorted_by_confidence_descending():
    detector = _detector_returning(
        [
            _FakeBox([10, 10, 50, 30], 0.4),
            _FakeBox([60, 20, 100, 40], 0.9),
            _FakeBox([0, 0, 20, 10], 0.7),
        ]
    )

    detections = detector.detect(_frame())

    assert [round(d.confidence, 2) for d in detections] == [0.9, 0.7, 0.4]
    assert detections[0].box == (60, 20, 100, 40)


def test_detect_handles_a_result_with_no_boxes():
    detector = _detector_returning([])

    assert detector.detect(_frame()) == []


# --- ANPRPipeline -----------------------------------------------------------


def _pipeline_with(detections, ocr_results):
    """Build an ANPRPipeline whose detector and OCR are both pre-stubbed."""
    pipeline = ANPRPipeline.__new__(ANPRPipeline)

    class _FakeDetector:
        def detect(self, _frame):
            return detections

    class _FakeOCR:
        def read_batch(self, crops):
            assert len(crops) == len(ocr_results), "one OCR result per crop"
            return ocr_results

    pipeline.detector = _FakeDetector()
    pipeline._ocr = _FakeOCR()
    return pipeline


def test_read_plates_pairs_each_detection_with_its_ocr_result():
    pipeline = _pipeline_with(
        detections=[
            PlateDetection(box=(10, 10, 60, 30), confidence=0.9),
            PlateDetection(box=(70, 10, 120, 30), confidence=0.6),
        ],
        ocr_results=[("GT1234", 0.8), ("AS999", 0.3)],
    )

    plates = pipeline.read_plates(_frame())

    assert [(p.text, p.ocr_confidence, p.detector_confidence) for p in plates] == [
        ("GT1234", 0.8, 0.9),
        ("AS999", 0.3, 0.6),
    ]


def test_read_plates_returns_empty_when_nothing_is_detected():
    pipeline = _pipeline_with(detections=[], ocr_results=[])

    assert pipeline.read_plates(_frame()) == []


def test_best_plate_rejects_reads_below_the_confidence_threshold():
    pipeline = _pipeline_with(
        detections=[PlateDetection(box=(10, 10, 60, 30), confidence=0.9)],
        ocr_results=[("GT1234", 0.2)],
    )

    assert pipeline.best_plate(_frame(), min_ocr_confidence=0.5) is None


def test_best_plate_rejects_an_empty_read_however_confident():
    pipeline = _pipeline_with(
        detections=[PlateDetection(box=(10, 10, 60, 30), confidence=0.99)],
        ocr_results=[("", 0.99)],
    )

    assert pipeline.best_plate(_frame(), min_ocr_confidence=0.5) is None


def test_best_plate_picks_the_highest_combined_confidence():
    pipeline = _pipeline_with(
        detections=[
            # Higher OCR score, but a weak detection -> lower combined.
            PlateDetection(box=(10, 10, 60, 30), confidence=0.5),
            PlateDetection(box=(70, 10, 120, 30), confidence=0.95),
        ],
        ocr_results=[("WEAKDET", 0.9), ("GT1234", 0.8)],
    )

    best = pipeline.best_plate(_frame(), min_ocr_confidence=0.5)

    assert best.text == "GT1234"


# --- rotation ---------------------------------------------------------------


def test_rotate_frame_zero_is_a_passthrough():
    frame = _frame(height=10, width=20)

    assert rotate_frame(frame, 0) is frame


@pytest.mark.parametrize(
    "degrees,expected", [(90, (20, 10)), (180, (10, 20)), (270, (20, 10))]
)
def test_rotate_frame_swaps_axes_for_quarter_turns(degrees, expected):
    frame = _frame(height=10, width=20)

    assert rotate_frame(frame, degrees).shape[:2] == expected


# --- RunStats ---------------------------------------------------------------


def test_run_stats_summary_computes_detection_rate_and_timings():
    stats = RunStats()
    stats.frames_processed = 10
    stats.frames_with_detections = 4
    stats.detections = 6
    stats.reads_above_threshold = 2
    stats.inference_ms = [100.0, 200.0, 300.0]

    summary = stats.summary()

    assert summary["detection_rate"] == pytest.approx(0.4)
    assert summary["mean_inference_ms"] == pytest.approx(200.0)
    assert summary["max_inference_ms"] == pytest.approx(300.0)


def test_run_stats_summary_handles_a_run_that_processed_nothing():
    """A run stopped before its first frame must not divide by zero."""
    summary = RunStats().summary()

    assert summary["detection_rate"] == 0.0
    assert summary["mean_inference_ms"] == 0.0


# --- PARSeq tokenizer -------------------------------------------------------


def _tokenizer(charset="0123456789ABCDEF"):
    pytest.importorskip("timm", reason="PARSeq's ViT encoder needs timm")
    from anpr.parseq_infer import Tokenizer

    return Tokenizer(charset)


def test_tokenizer_special_ids_match_parseq_ordering():
    """EOS first, then the charset, then BOS and PAD — the order the
    checkpoint was trained with. Getting this wrong shifts every character."""
    tokenizer = _tokenizer("0123456789ABCDEF")

    assert tokenizer.eos_id == 0
    assert tokenizer.bos_id == 17  # len(charset) + 1
    assert tokenizer.pad_id == 18  # len(charset) + 2
    assert len(tokenizer) == 19  # len(charset) + 3


def test_tokenizer_decode_stops_at_eos():
    torch = pytest.importorskip("torch")
    tokenizer = _tokenizer("0123456789ABCDEF")

    # ids: 1 -> '0', 2 -> '1', then EOS; anything after must be ignored.
    logits = torch.full((1, 5, len(tokenizer)), -10.0)
    for position, token_id in enumerate([1, 2, tokenizer.eos_id, 3, 4]):
        logits[0, position, token_id] = 10.0

    (text, confidence) = tokenizer.decode(logits)[0]

    assert text == "01"
    assert confidence > 0.9


def test_tokenizer_decode_reports_zero_confidence_for_an_immediate_eos():
    """No characters to average over must read as 0.0, never NaN."""
    torch = pytest.importorskip("torch")
    tokenizer = _tokenizer("0123456789ABCDEF")

    logits = torch.full((1, 3, len(tokenizer)), -10.0)
    logits[0, :, tokenizer.eos_id] = 10.0

    (text, confidence) = tokenizer.decode(logits)[0]

    assert text == ""
    assert confidence == 0.0
