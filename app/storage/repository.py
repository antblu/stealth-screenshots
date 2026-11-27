"""Bounded in-memory repository for detected questions and LLM responses."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(slots=True)
class QuestionRecord:
    id: str
    frame_id: int
    detected_at: datetime
    status: Literal["pending", "completed", "failed"]
    ocr_text: str
    confidence: float
    model: str
    completed_at: datetime | None = None
    answer_text: str | None = None
    error: str | None = None
    latency_ms: float | None = None


class InMemoryQuestionRepository:
    """Thread-safe bounded in-memory history of question processing records."""

    def __init__(self, max_history: int = 100) -> None:
        if max_history < 1:
            raise ValueError("max_history must be >= 1")
        self._max_history = max_history
        self._lock = threading.Lock()
        self._order: deque[str] = deque()
        self._records: dict[str, QuestionRecord] = {}
        self._seq: int = 0

    def create_pending(
        self,
        *,
        frame_id: int,
        ocr_text: str,
        confidence: float,
        model: str,
    ) -> QuestionRecord:
        with self._lock:
            self._seq += 1
            qid = f"q-{self._seq:06d}"
            record = QuestionRecord(
                id=qid,
                frame_id=frame_id,
                detected_at=datetime.now(timezone.utc),
                status="pending",
                ocr_text=ocr_text,
                confidence=confidence,
                model=model,
            )
            if len(self._order) >= self._max_history:
                oldest_id = self._order.popleft()
                self._records.pop(oldest_id, None)
            self._order.append(qid)
            self._records[qid] = record
            return record

    def mark_completed(
        self,
        question_id: str,
        *,
        answer_text: str,
        latency_ms: float,
    ) -> QuestionRecord | None:
        with self._lock:
            record = self._records.get(question_id)
            if record is None:
                return None
            record.status = "completed"
            record.answer_text = answer_text
            record.latency_ms = latency_ms
            record.completed_at = datetime.now(timezone.utc)
            return record

    def mark_failed(
        self,
        question_id: str,
        *,
        error: str,
        latency_ms: float | None = None,
    ) -> QuestionRecord | None:
        with self._lock:
            record = self._records.get(question_id)
            if record is None:
                return None
            record.status = "failed"
            record.error = error
            record.latency_ms = latency_ms
            record.completed_at = datetime.now(timezone.utc)
            return record

    def get_by_id(self, question_id: str) -> QuestionRecord | None:
        with self._lock:
            return self._records.get(question_id)

    def get_latest_completed(self) -> QuestionRecord | None:
        with self._lock:
            for qid in reversed(self._order):
                record = self._records.get(qid)
                if record is not None and record.status == "completed":
                    return record
            return None

    def list_history(self, limit: int = 50) -> list[QuestionRecord]:
        with self._lock:
            ids = list(reversed(self._order))[: max(0, limit)]
            return [self._records[qid] for qid in ids if qid in self._records]

    def count(self) -> int:
        with self._lock:
            return len(self._records)
