"""Pydantic response schemas for the output HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class StatusResponse(BaseModel):
    source_connected: bool
    source_url: str
    source_last_error: str | None
    buffer_size: int
    worker_active: bool
    model: str
    history_count: int
    submission_queue_size: int = 0
    submission_queue_maxsize: int = 0
    submission_queue_dropped: int = 0
    submission_queue_saturated: bool = False


class QuestionResponse(BaseModel):
    id: str
    frame_id: int
    detected_at: datetime
    completed_at: datetime | None
    status: Literal["pending", "completed", "failed"]
    ocr_text: str
    confidence: float
    model: str
    answer_text: str | None
    error: str | None
    latency_ms: float | None


class HistoryResponse(BaseModel):
    items: list[QuestionResponse]
    total: int
