"""Question detection state machine (isolated from LLM request state)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from app.config import DetectionConfig
from app.detection.classifier import classify_question
from app.detection.ocr import OcrEngine, create_ocr_engine
from app.detection.similarity import DuplicateDetector, normalize_text
from app.detection.visual import (
    compute_phash,
    crop_image,
    decode_jpeg,
    frames_similar,
    prepare_upload_image,
    to_normalized_gray,
)


@dataclass(frozen=True, slots=True)
class DetectionResult:
    should_process: bool
    reason: str
    raw_text: str = ""
    normalized_text: str = ""
    confidence: float = 0.0
    phash: int | None = None
    is_duplicate: bool = False
    cropped_jpeg_bytes: bytes | None = None
    upload_media_type: str = "image/jpeg"
    accepted_mono: float | None = None
    prepared_mono: float | None = None
    used_wider_fallback: bool = False
    ocr_ms: float | None = None


@dataclass
class DetectionState:
    """Tracks visual scene stability and deduplication independently of LLM state."""

    last_phash: int | None = None
    last_gray: np.ndarray | None = None
    stable_count: int = 0
    scene_triggered: bool = False
    frames_analyzed: int = 0
    duplicates_skipped: int = 0
    questions_detected: int = 0
    reasons_history: list[str] = field(default_factory=list)


class QuestionDetector:
    """Modular question detection engine combining visual, OCR, and text signals."""

    def __init__(
        self,
        config: DetectionConfig,
        ocr_engine: OcrEngine | None = None,
    ) -> None:
        self._config = config
        self._ocr: OcrEngine = ocr_engine or create_ocr_engine(
            config.ocr_backend, threads=config.ocr_threads
        )
        self._dedup = DuplicateDetector(
            window_size=config.dedup_window_size,
            similarity_threshold=config.text_similarity_threshold,
            phash_threshold=config.phash_threshold,
        )
        self.state = DetectionState()
        self._last_evaluated_frame: tuple[bytes, int] | None = None

    def evaluate_frame(self, jpeg_bytes: bytes) -> DetectionResult:
        """Analyze a JPEG frame and decide whether it contains a new question."""
        last_evaluated = self._last_evaluated_frame
        if (
            self._config.visual_change_detection
            and self.state.scene_triggered
            and last_evaluated is not None
            and jpeg_bytes == last_evaluated[0]
        ):
            self.state.frames_analyzed += 1
            self.state.stable_count += 1
            return DetectionResult(
                should_process=False,
                reason="scene_already_triggered",
                phash=last_evaluated[1],
            )

        full_image = decode_jpeg(jpeg_bytes)
        if full_image is None:
            return DetectionResult(should_process=False, reason="invalid_image")

        if self._config.crop is not None:
            image = crop_image(
                full_image,
                left=self._config.crop.left,
                top=self._config.crop.top,
                right=self._config.crop.right,
                bottom=self._config.crop.bottom,
            )
        else:
            image = full_image

        self.state.frames_analyzed += 1
        phash = compute_phash(image)
        gray = to_normalized_gray(image)

        if self._config.visual_change_detection:
            if self.state.last_phash is not None and self.state.last_gray is not None:
                similar = frames_similar(
                    phash,
                    self.state.last_phash,
                    gray,
                    self.state.last_gray,
                    phash_threshold=self._config.phash_threshold,
                    ssim_threshold=self._config.ssim_threshold,
                )
            else:
                similar = False

            if similar:
                self.state.stable_count += 1
                if self.state.scene_triggered:
                    return DetectionResult(
                        should_process=False,
                        reason="scene_already_triggered",
                        phash=phash,
                    )
            else:
                self.state.last_phash = phash
                self.state.last_gray = gray
                self.state.stable_count = 1
                self.state.scene_triggered = False

            if (
                self._config.stability_check
                and self.state.stable_count < self._config.stability_frames
            ):
                return DetectionResult(
                    should_process=False,
                    reason="awaiting_stability",
                    phash=phash,
                )

        # Mark scene as evaluated so we don't re-run OCR repeatedly on an idle slide.
        self.state.scene_triggered = True
        if self._config.visual_change_detection:
            self._last_evaluated_frame = (jpeg_bytes, phash)

        raw_text = ""
        normalized = ""
        confidence = 1.0
        ocr_ms = None

        if self._config.ocr_enabled:
            ocr_started_mono = time.perf_counter()
            raw_text = self._ocr.extract_text(image)
            ocr_ms = (time.perf_counter() - ocr_started_mono) * 1000.0
            normalized = normalize_text(raw_text)
            classification = classify_question(
                raw_text,
                min_confidence=self._config.min_question_confidence,
                min_length=self._config.min_text_length,
            )
            confidence = classification.confidence
            if not classification.is_question:
                return DetectionResult(
                    should_process=False,
                    reason="not_a_question",
                    raw_text=raw_text,
                    normalized_text=normalized,
                    confidence=confidence,
                    phash=phash,
                    ocr_ms=ocr_ms,
                )

        if self._config.deduplication and self._dedup.is_duplicate(
            normalized, phash=phash
        ):
            self.state.duplicates_skipped += 1
            return DetectionResult(
                should_process=False,
                reason="duplicate_question",
                raw_text=raw_text,
                normalized_text=normalized,
                confidence=confidence,
                phash=phash,
                is_duplicate=True,
                ocr_ms=ocr_ms,
            )

        if self._config.deduplication:
            self._dedup.record(normalized, phash=phash)

        self.state.questions_detected += 1
        accepted_mono = time.perf_counter()
        upload_bytes, upload_media_type, used_fallback = prepare_upload_image(
            full_image,
            detection_crop=self._config.crop,
            upload_crop=self._config.upload_crop,
            detection_image=image,
        )
        prepared_mono = time.perf_counter()
        return DetectionResult(
            should_process=True,
            reason="new_question_detected",
            raw_text=raw_text,
            normalized_text=normalized,
            confidence=confidence,
            phash=phash,
            cropped_jpeg_bytes=upload_bytes,
            upload_media_type=upload_media_type,
            accepted_mono=accepted_mono,
            prepared_mono=prepared_mono,
            used_wider_fallback=used_fallback,
            ocr_ms=ocr_ms,
        )
