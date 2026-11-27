"""Local OCR engines backed by RapidOCR (PaddleOCR ONNX) or Tesseract."""

from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING, Protocol

import cv2
import numpy as np
import pytesseract

if TYPE_CHECKING:
    from rapidocr import RapidOCR

logger = logging.getLogger(__name__)


class OcrEngine(Protocol):
    """Protocol for pluggable local OCR implementations."""

    def extract_text(self, image: np.ndarray) -> str: ...


class RapidOcrEngine:
    """CPU OCR using RapidOCR models via ONNX Runtime."""

    def __init__(self, min_score: float = 0.5, *, threads: int = 4) -> None:
        self._min_score = min_score
        self._threads = min(threads, os.cpu_count() or 1)
        self._engine: RapidOCR | None = None

    def _get_engine(self) -> RapidOCR:
        if self._engine is None:
            from rapidocr import RapidOCR

            self._engine = RapidOCR(params={
                "EngineConfig.onnxruntime.intra_op_num_threads": self._threads,
                "Global.log_level": "warning",
            })
        return self._engine

    def extract_text(self, image: np.ndarray) -> str:
        try:
            result = self._get_engine()(image, text_score=self._min_score)
        except Exception as exc:
            logger.warning("RapidOCR extraction failed: %s", exc)
            return ""

        lines = (line.strip() for line in result.txts or ())
        return "\n".join(line for line in lines if line)


class TesseractOcrEngine:
    """Local OCR using pytesseract and OpenCV preprocessing."""

    def __init__(self, lang: str = "eng", psm: int = 6) -> None:
        self._lang = lang
        self._psm = psm
        self._warned_missing = False

    def is_available(self) -> bool:
        return shutil.which("tesseract") is not None

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """Prepare BGR image for cleaner OCR extraction."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        h, w = gray.shape[:2]
        if w < 800:
            scale = 2.0
            gray = cv2.resize(
                gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC
            )
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return thresh

    def extract_text(self, image: np.ndarray) -> str:
        if not self.is_available():
            if not self._warned_missing:
                logger.warning(
                    "Tesseract binary not found on PATH; OCR returning empty text."
                )
                self._warned_missing = True
            return ""

        processed = self.preprocess(image)
        config = f"--psm {self._psm}"
        try:
            text = pytesseract.image_to_string(
                processed, lang=self._lang, config=config
            )
            return text.strip()
        except Exception as exc:
            logger.warning("OCR extraction failed: %s", exc)
            return ""


def create_ocr_engine(backend: str = "rapidocr", *, threads: int = 4) -> OcrEngine:
    """Instantiate the configured local OCR engine."""
    normalized = backend.strip().lower()
    if normalized == "rapidocr":
        return RapidOcrEngine(threads=threads)
    if normalized == "tesseract":
        return TesseractOcrEngine()
    raise ValueError(f"Unsupported OCR backend: {backend}")
