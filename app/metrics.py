"""Operational metrics tracking for the MJPEG question processing pipeline."""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return round(ordered[f], 2)
    return round(ordered[f] + (ordered[c] - ordered[f]) * (k - f), 2)


class MetricsRegistry:
    """Thread-safe in-memory counters, gauges, and bounded latency windows."""

    def __init__(self, latency_window: int = 500) -> None:
        self._lock = threading.Lock()
        self.started_at: datetime = datetime.now(timezone.utc)
        self.frames_received: int = 0
        self.frames_dropped: int = 0
        self.malformed_frames: int = 0
        self.reconnects: int = 0
        self.frames_analyzed: int = 0
        self.questions_detected: int = 0
        self.duplicates_skipped: int = 0
        self.submission_queue_dropped: int = 0
        self.questions_never_reached_endpoint: int = 0
        self.submission_queue_size: int = 0
        self.submission_queue_capacity: int = 0
        self.in_flight_llm_requests: int = 0
        self.llm_requests_total: int = 0
        self.llm_requests_succeeded: int = 0
        self.llm_requests_failed: int = 0
        self.llm_retries: int = 0
        self.last_frame_at: datetime | None = None
        self.last_question_at: datetime | None = None
        self.last_llm_latency_ms: float | None = None
        self._latencies: dict[str, deque[float]] = {
            "frame_to_detection_ms": deque(maxlen=latency_window),
            "frame_to_upload_ms": deque(maxlen=latency_window),
            "detected_to_sent_ms": deque(maxlen=latency_window),
            "ocr_ms": deque(maxlen=latency_window),
            "image_prep_ms": deque(maxlen=latency_window),
            "queue_wait_ms": deque(maxlen=latency_window),
        }

    def inc(self, field: str, amount: int = 1) -> None:
        with self._lock:
            current = getattr(self, field)
            setattr(self, field, current + amount)

    def set_value(self, field: str, value: Any) -> None:
        with self._lock:
            setattr(self, field, value)

    def record_latency(self, metric_name: str, value_ms: float) -> None:
        with self._lock:
            bucket = self._latencies.get(metric_name)
            if bucket is not None:
                bucket.append(max(0.0, float(value_ms)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(timezone.utc)
            f2d = list(self._latencies["frame_to_detection_ms"])
            f2u = list(self._latencies["frame_to_upload_ms"])
            d2s = list(self._latencies["detected_to_sent_ms"])
            ocr = list(self._latencies["ocr_ms"])
            prep = list(self._latencies["image_prep_ms"])
            qwait = list(self._latencies["queue_wait_ms"])
            return {
                "uptime_seconds": round((now - self.started_at).total_seconds(), 2),
                "frames_received": self.frames_received,
                "frames_dropped": self.frames_dropped,
                "malformed_frames": self.malformed_frames,
                "reconnects": self.reconnects,
                "frames_analyzed": self.frames_analyzed,
                "questions_detected": self.questions_detected,
                "duplicates_skipped": self.duplicates_skipped,
                "submission_queue_dropped": self.submission_queue_dropped,
                "questions_never_reached_endpoint": self.questions_never_reached_endpoint,
                "submission_queue_size": self.submission_queue_size,
                "submission_queue_capacity": self.submission_queue_capacity,
                "submission_queue_saturated": (
                    self.submission_queue_capacity > 0
                    and self.submission_queue_size >= self.submission_queue_capacity
                ),
                "in_flight_llm_requests": self.in_flight_llm_requests,
                "llm_requests_total": self.llm_requests_total,
                "llm_requests_succeeded": self.llm_requests_succeeded,
                "llm_requests_failed": self.llm_requests_failed,
                "llm_retries": self.llm_retries,
                "last_frame_at": (
                    self.last_frame_at.isoformat() if self.last_frame_at else None
                ),
                "last_question_at": (
                    self.last_question_at.isoformat() if self.last_question_at else None
                ),
                "last_llm_latency_ms": self.last_llm_latency_ms,
                "frame_to_detection_p50_ms": _percentile(f2d, 50),
                "frame_to_detection_p95_ms": _percentile(f2d, 95),
                "frame_to_upload_p50_ms": _percentile(f2u, 50),
                "frame_to_upload_p95_ms": _percentile(f2u, 95),
                "detected_to_sent_p50_ms": _percentile(d2s, 50),
                "detected_to_sent_p95_ms": _percentile(d2s, 95),
                "ocr_p50_ms": _percentile(ocr, 50),
                "ocr_p95_ms": _percentile(ocr, 95),
                "image_prep_p50_ms": _percentile(prep, 50),
                "image_prep_p95_ms": _percentile(prep, 95),
                "queue_wait_p50_ms": _percentile(qwait, 50),
                "queue_wait_p95_ms": _percentile(qwait, 95),
            }
