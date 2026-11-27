"""Tests for visual analysis, question classification, stability, and deduplication."""

from __future__ import annotations

from app.config import DetectionConfig
from app.detection.classifier import classify_question
from app.detection.similarity import normalize_text, text_similarity
from app.detection.state import QuestionDetector
from app.detection.visual import compute_phash, compute_ssim, decode_jpeg, hamming_distance, to_normalized_gray
from tests.conftest import FakeOcrEngine, make_question_jpeg


def test_phash_and_ssim_detect_visual_change() -> None:
    jpg_a = make_question_jpeg(["What is the boiling point of water?"])
    jpg_b = make_question_jpeg(["What is the boiling point of water?"])
    jpg_c = make_question_jpeg(
        ["Calculate the derivative of x^3 + 2x^2.", "A) 3x^2 + 4x", "B) x^2 + 2x"],
        bg_color=(30, 35, 50),
        fg_color=(240, 240, 240),
        header_color=(180, 60, 60),
    )

    img_a = decode_jpeg(jpg_a)
    img_b = decode_jpeg(jpg_b)
    img_c = decode_jpeg(jpg_c)
    assert img_a is not None and img_b is not None and img_c is not None

    assert hamming_distance(compute_phash(img_a), compute_phash(img_b)) == 0
    assert compute_ssim(to_normalized_gray(img_a), to_normalized_gray(img_b)) > 0.99
    assert compute_ssim(to_normalized_gray(img_a), to_normalized_gray(img_c)) < 0.90


def test_question_classifier() -> None:
    q1 = classify_question("What is the capital of France? A) Paris B) London")
    assert q1.is_question is True
    assert q1.confidence >= 0.7

    q2 = classify_question("Solve for x: 2x + 5 = 15")
    assert q2.is_question is True

    non_q = classify_question("Welcome to Chapter 3.")
    assert non_q.is_question is False


def test_text_normalization_and_similarity() -> None:
    a = normalize_text("Question 1:  What is 2 + 2?\n")
    b = normalize_text("question 1: what is 2 + 2?")
    assert a == b
    assert text_similarity(a, b) == 100.0


def test_detector_stability_and_deduplication() -> None:
    q1_jpg = make_question_jpeg(["Question 1: What is 2 + 2?"], bg_color=(250, 250, 250), header_color=(30, 80, 160))
    q1_recolored_jpg = make_question_jpeg(
        ["Question 1: What is 2 + 2?", "Select one option below."],
        bg_color=(35, 40, 55),
        fg_color=(240, 240, 240),
        header_color=(180, 60, 60),
    )
    q2_jpg = make_question_jpeg(
        ["Question 2: What is 5 * 6?", "A) 25", "B) 30", "C) 35"],
        bg_color=(245, 245, 230),
        header_color=(20, 120, 70),
    )

    ocr = FakeOcrEngine(
        mapping={
            248: "Question 1: What is 2 + 2?",
            218: "Question 1: What is 2 + 2?",
            119: "Question 2: What is 5 * 6?",
        },
        default_text="Question 1: What is 2 + 2?",
    )
    # Build exact mean->text mapping from decoded images
    for jpg, txt in [
        (q1_jpg, "Question 1: What is 2 + 2?"),
        (q1_recolored_jpg, "Question 1: What is 2 + 2?"),
        (q2_jpg, "Question 2: What is 5 * 6?"),
    ]:
        img = decode_jpeg(jpg)
        assert img is not None
        ocr.mapping[int(img.mean())] = txt

    cfg = DetectionConfig(stability_frames=2, text_similarity_threshold=85.0)
    detector = QuestionDetector(cfg, ocr_engine=ocr)

    # Frame 1 of Q1: awaits stability
    r1 = detector.evaluate_frame(q1_jpg)
    assert r1.should_process is False
    assert r1.reason == "awaiting_stability"

    # Frame 2 of Q1: stable -> triggers!
    r2 = detector.evaluate_frame(q1_jpg)
    assert r2.should_process is True
    assert r2.reason == "new_question_detected"

    # Frame 3 of Q1: same scene -> skipped without re-running OCR
    prev_ocr_calls = ocr.calls
    r3 = detector.evaluate_frame(q1_jpg)
    assert r3.should_process is False
    assert r3.reason == "scene_already_triggered"
    assert ocr.calls == prev_ocr_calls

    # Visually different rendering of Q1 -> reaches stability, caught by deduplication
    assert detector.evaluate_frame(q1_recolored_jpg).reason == "awaiting_stability"
    r4 = detector.evaluate_frame(q1_recolored_jpg)
    assert r4.should_process is False
    assert r4.reason == "duplicate_question"
    assert r4.is_duplicate is True

    # New question Q2 -> reaches stability and triggers
    assert detector.evaluate_frame(q2_jpg).reason == "awaiting_stability"
    r5 = detector.evaluate_frame(q2_jpg)
    assert r5.should_process is True
    assert r5.reason == "new_question_detected"


def test_rapidocr_extracts_clean_question_text() -> None:
    from app.detection.ocr import RapidOcrEngine

    jpg = make_question_jpeg(
        ["Question 1: What is the capital of France?", "A) Berlin", "B) Paris"]
    )
    img = decode_jpeg(jpg)
    assert img is not None

    engine = RapidOcrEngine()
    text = engine.extract_text(img)
    assert "capital of France" in text
    assert "Paris" in text


def test_detector_catches_localized_quiz_question_change() -> None:
    """Simulate a browser quiz where 80% of the page is static and only the question changes."""
    shared_header = [
        "Browser Tabs | Bookmarks | Training Portal | User Dashboard",
        "Linux and Bash Quiz - Systems Applications",
    ]
    q1 = make_question_jpeg(
        shared_header + ["Question 1/10: How do I terminate a running process?", "A) ctrl-c  B) ctrl-z"]
    )
    q2 = make_question_jpeg(
        shared_header + ["Question 2/10: Which command prints environment variables?", "A) env  B) pwd"]
    )

    detector = QuestionDetector(DetectionConfig(stability_frames=2))

    assert detector.evaluate_frame(q1).reason == "awaiting_stability"
    assert detector.evaluate_frame(q1).should_process is True
    assert detector.evaluate_frame(q1).reason == "scene_already_triggered"

    # Transition to Question 2 on the same browser template must trigger!
    assert detector.evaluate_frame(q2).reason == "awaiting_stability"
    r2 = detector.evaluate_frame(q2)
    assert r2.should_process is True
    assert r2.reason == "new_question_detected"


def test_detector_crops_roi_and_returns_cropped_jpeg() -> None:
    from app.config import CropConfig

    jpg = make_question_jpeg(
        ["Question 1: What is 2 + 2?", "A) 3", "B) 4"],
        width=1000,
        height=500,
    )
    ocr = FakeOcrEngine(default_text="Question 1: What is 2 + 2?")
    cfg = DetectionConfig(
        stability_check=True,
        stability_frames=2,
        crop=CropConfig(left=0.26, top=0.06, right=0.74, bottom=1.0),
    )
    detector = QuestionDetector(cfg, ocr_engine=ocr)

    r1 = detector.evaluate_frame(jpg)
    assert r1.should_process is False
    assert r1.reason == "awaiting_stability"

    r2 = detector.evaluate_frame(jpg)
    assert r2.should_process is True
    assert r2.cropped_jpeg_bytes is not None

    cropped_img = decode_jpeg(r2.cropped_jpeg_bytes)
    assert cropped_img is not None
    assert cropped_img.shape[:2] == (470, 480)


def test_detector_downscales_large_cropped_image() -> None:
    from app.config import CropConfig

    jpg = make_question_jpeg(
        ["Question 1: What is 2 + 2?", "A) 3", "B) 4"],
        width=2400,
        height=1600,
    )
    ocr = FakeOcrEngine(default_text="Question 1: What is 2 + 2?")
    cfg = DetectionConfig(
        stability_check=False,
        crop=CropConfig(
            left=0.25,
            top=0.0,
            right=0.75,
            bottom=1.0,
            max_dim=1024,
            jpeg_quality=75,
        ),
    )
    detector = QuestionDetector(cfg, ocr_engine=ocr)
    result = detector.evaluate_frame(jpg)
    assert result.should_process is True
    assert result.cropped_jpeg_bytes is not None
    assert len(result.cropped_jpeg_bytes) < len(jpg)

    out_img = decode_jpeg(result.cropped_jpeg_bytes)
    assert out_img is not None
    assert max(out_img.shape[:2]) == 1024
