"""Text normalization, RapidFuzz similarity, and duplicate detection."""

from __future__ import annotations

import re
import unicodedata
from collections import deque

from rapidfuzz import fuzz

from app.detection.visual import hamming_distance

_WHITESPACE_RE = re.compile(r"\s+")
_NOISE_RE = re.compile(r"[^\w\s?.,:;()+\-*/=<>^%$]")


def normalize_text(text: str) -> str:
    """Normalize OCR text for reliable comparison and deduplication."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.lower()
    normalized = _NOISE_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def text_similarity(text_a: str, text_b: str) -> float:
    """Compute full-text similarity score in [0.0, 100.0] using RapidFuzz."""
    if not text_a or not text_b:
        return 0.0
    return float(fuzz.ratio(text_a, text_b))


class DuplicateDetector:
    """Sliding-window duplicate detector for questions and perceptual hashes."""

    def __init__(
        self,
        *,
        window_size: int = 50,
        similarity_threshold: float = 85.0,
        phash_threshold: int = 6,
    ) -> None:
        self._texts: deque[str] = deque(maxlen=window_size)
        self._phashes: deque[int] = deque(maxlen=window_size)
        self._similarity_threshold = similarity_threshold
        self._phash_threshold = phash_threshold

    def is_duplicate(self, normalized_text: str, phash: int | None = None) -> bool:
        """Check whether the candidate matches a recently processed question."""
        if normalized_text:
            for prev in self._texts:
                if text_similarity(normalized_text, prev) >= self._similarity_threshold:
                    return True
        elif phash is not None:
            for prev_hash in self._phashes:
                if hamming_distance(phash, prev_hash) <= self._phash_threshold:
                    return True
        return False

    def record(self, normalized_text: str, phash: int | None = None) -> None:
        """Record a processed question into the deduplication window."""
        if normalized_text:
            self._texts.append(normalized_text)
        if phash is not None:
            self._phashes.append(phash)

    def clear(self) -> None:
        self._texts.clear()
        self._phashes.clear()
