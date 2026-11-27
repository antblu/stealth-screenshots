"""FastAPI route definitions for health, status, responses, history, and metrics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.api.auth import require_bearer_token
from app.api.schemas import (
    HealthResponse,
    HistoryResponse,
    QuestionResponse,
    StatusResponse,
)
from app.storage.repository import QuestionRecord

router = APIRouter()


def _to_schema(record: QuestionRecord) -> QuestionResponse:
    return QuestionResponse(
        id=record.id,
        frame_id=record.frame_id,
        detected_at=record.detected_at,
        completed_at=record.completed_at,
        status=record.status,
        ocr_text=record.ocr_text,
        confidence=record.confidence,
        model=record.model,
        answer_text=record.answer_text,
        error=record.error,
        latency_ms=record.latency_ms,
    )


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def get_health() -> HealthResponse:
    """Public liveness/health endpoint."""
    return HealthResponse(status="ok")


@router.get(
    "/status",
    response_model=StatusResponse,
    dependencies=[Depends(require_bearer_token)],
    tags=["system"],
)
async def get_status(request: Request) -> StatusResponse:
    """Return runtime status of the MJPEG source, worker, and repository."""
    ctx = request.app.state.container
    q_size = ctx.worker.submission_queue_size
    q_max = ctx.worker.submission_queue_maxsize
    return StatusResponse(
        source_connected=ctx.mjpeg_client.connected,
        source_url=ctx.config.source.url,
        source_last_error=ctx.mjpeg_client.last_error,
        buffer_size=ctx.buffer.qsize(),
        worker_active=ctx.worker.processing_active,
        model=ctx.config.llm.model,
        history_count=ctx.repository.count(),
        submission_queue_size=q_size,
        submission_queue_maxsize=q_max,
        submission_queue_dropped=ctx.metrics.submission_queue_dropped,
        submission_queue_saturated=(q_max > 0 and q_size >= q_max),
    )


@router.get(
    "/latest",
    response_model=QuestionResponse,
    dependencies=[Depends(require_bearer_token)],
    tags=["questions"],
)
async def get_latest(request: Request) -> QuestionResponse:
    """Return the latest completed question and LLM response as JSON."""
    record = request.app.state.container.repository.get_latest_completed()
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No completed question responses available yet",
        )
    return _to_schema(record)


@router.get(
    "/",
    dependencies=[Depends(require_bearer_token)],
    tags=["questions"],
    responses={200: {"content": {"text/plain": {}}}},
)
@router.get(
    "/latest/text",
    dependencies=[Depends(require_bearer_token)],
    tags=["questions"],
    responses={200: {"content": {"text/plain": {}}}},
)
async def get_latest_text(request: Request) -> Response:
    """Return only the generated text of the latest completed response as text/plain."""
    record = request.app.state.container.repository.get_latest_completed()
    if record is None or record.answer_text is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No completed question responses available yet",
        )
    return Response(content=record.answer_text, media_type="text/plain; charset=utf-8")


@router.get(
    "/history",
    response_model=HistoryResponse,
    dependencies=[Depends(require_bearer_token)],
    tags=["questions"],
)
async def get_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> HistoryResponse:
    """Return recent question processing history (newest first)."""
    repo = request.app.state.container.repository
    records = repo.list_history(limit=limit)
    return HistoryResponse(
        items=[_to_schema(r) for r in records],
        total=repo.count(),
    )


@router.get(
    "/questions/{question_id}",
    response_model=QuestionResponse,
    dependencies=[Depends(require_bearer_token)],
    tags=["questions"],
)
async def get_question(question_id: str, request: Request) -> QuestionResponse:
    """Return a specific question and response by ID."""
    record = request.app.state.container.repository.get_by_id(question_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Question {question_id} not found",
        )
    return _to_schema(record)


@router.get(
    "/metrics",
    dependencies=[Depends(require_bearer_token)],
    tags=["system"],
)
async def get_metrics(request: Request) -> dict[str, Any]:
    """Return operational metrics."""
    return request.app.state.container.metrics.snapshot()
