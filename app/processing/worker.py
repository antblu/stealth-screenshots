"""Async worker coordinating serial question detection and bounded concurrent LLM submission."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app.detection.state import QuestionDetector
from app.llm.client import LLMClientError, MultimodalLLMClient
from app.metrics import MetricsRegistry
from app.source.buffer import BoundedFrameBuffer
from app.storage.repository import InMemoryQuestionRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmissionItem:
    """Immutable handoff from serial detector to bounded FIFO LLM submission queue."""

    record_id: str
    frame_id: int
    payload_bytes: bytes
    media_type: str
    arrived_mono: float
    accepted_mono: float
    prepared_mono: float
    enqueued_mono: float


class ProcessingWorker:
    """Consumes frames from BoundedFrameBuffer, runs serial detection, and dispatches LLM sends."""

    def __init__(
        self,
        *,
        buffer: BoundedFrameBuffer,
        detector: QuestionDetector,
        llm_client: MultimodalLLMClient,
        repository: InMemoryQuestionRepository,
        metrics: MetricsRegistry,
        model_name: str,
        max_concurrent_requests: int = 2,
        submission_queue_size: int = 16,
    ) -> None:
        self._buffer = buffer
        self._detector = detector
        self._llm_client = llm_client
        self._repository = repository
        self._metrics = metrics
        self._model_name = model_name
        self._max_concurrent_requests = max(1, max_concurrent_requests)
        self._submission_queue: asyncio.Queue[SubmissionItem] = asyncio.Queue(
            maxsize=max(1, submission_queue_size)
        )
        self._send_semaphore = asyncio.Semaphore(self._max_concurrent_requests)
        self._in_flight_tasks: set[asyncio.Task[None]] = set()
        self._in_flight_records: dict[asyncio.Task[None], str] = {}
        self._stop_event = asyncio.Event()
        self.processing_active: bool = False

        self._metrics.set_value(
            "submission_queue_capacity", self._submission_queue.maxsize
        )
        self._metrics.set_value("submission_queue_size", 0)

    @property
    def submission_queue_size(self) -> int:
        return self._submission_queue.qsize()

    @property
    def submission_queue_maxsize(self) -> int:
        return self._submission_queue.maxsize

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Continuously process buffered frames until stopped, then drain cleanly."""
        self.processing_active = True
        sender_task = asyncio.create_task(self._sender_loop(), name="llm-sender")
        try:
            while not self._stop_event.is_set():
                try:
                    envelope = await asyncio.wait_for(self._buffer.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                await self.process_one(
                    envelope.frame_id,
                    envelope.jpeg_bytes,
                    arrived_mono=envelope.arrived_mono,
                )
        except asyncio.CancelledError:
            raise
        finally:
            self._stop_event.set()
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
            await self._cleanup_on_shutdown()
            self.processing_active = False

    async def _cleanup_on_shutdown(self) -> None:
        """Cancel any in-flight sends and mark queued/stranded pending records as failed."""
        tasks = list(self._in_flight_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for task in tasks:
            rid = self._in_flight_records.pop(task, None)
            if rid is not None:
                rec = self._repository.get_by_id(rid)
                if rec is not None and rec.status == "pending":
                    self._repository.mark_failed(rid, error="worker_shutdown")
                    self._metrics.inc("questions_never_reached_endpoint")

        while not self._submission_queue.empty():
            try:
                item = self._submission_queue.get_nowait()
                rec = self._repository.get_by_id(item.record_id)
                if rec is not None and rec.status == "pending":
                    self._repository.mark_failed(item.record_id, error="worker_shutdown")
                    self._metrics.inc("questions_never_reached_endpoint")
            except asyncio.QueueEmpty:
                break

        self._metrics.set_value("submission_queue_size", self._submission_queue.qsize())
        self._metrics.set_value("in_flight_llm_requests", 0)

    async def _sender_loop(self) -> None:
        """Pull accepted questions from the bounded FIFO queue as send slots open."""
        try:
            while not self._stop_event.is_set():
                await self._send_semaphore.acquire()
                acquired = True
                try:
                    while not self._stop_event.is_set():
                        try:
                            item = await asyncio.wait_for(
                                self._submission_queue.get(), timeout=0.2
                            )
                            self._metrics.set_value(
                                "submission_queue_size", self._submission_queue.qsize()
                            )
                            task = asyncio.create_task(
                                self._submit_item(item, preacquired=True),
                                name=f"llm-send-{item.record_id}",
                            )
                            acquired = False  # ownership transferred to _submit_item
                            self._in_flight_tasks.add(task)
                            self._in_flight_records[task] = item.record_id
                            task.add_done_callback(self._on_task_done)
                            break
                        except asyncio.TimeoutError:
                            continue
                finally:
                    if acquired:
                        self._send_semaphore.release()
        except asyncio.CancelledError:
            raise

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._in_flight_tasks.discard(task)
        self._in_flight_records.pop(task, None)
        self._metrics.set_value("in_flight_llm_requests", len(self._in_flight_tasks))

    async def _submit_item(
        self, item: SubmissionItem, *, preacquired: bool
    ) -> None:
        """Send a single queued question to the LLM and update its repository record."""
        dequeued_mono = time.perf_counter()
        queue_wait_ms = (dequeued_mono - item.enqueued_mono) * 1000.0
        self._metrics.record_latency("queue_wait_ms", queue_wait_ms)
        self._metrics.set_value("in_flight_llm_requests", len(self._in_flight_tasks))

        try:
            llm_response = await self._llm_client.answer_question(
                item.payload_bytes,
                image_media_type=item.media_type,
                send_semaphore=self._send_semaphore if preacquired else None,
                preacquired=preacquired,
            )
            if llm_response.first_upload_completed_mono is not None:
                d2s_ms = (
                    llm_response.first_upload_completed_mono - item.accepted_mono
                ) * 1000.0
                self._metrics.record_latency("detected_to_sent_ms", d2s_ms)
                self._metrics.record_latency(
                    "frame_to_upload_ms",
                    (llm_response.first_upload_completed_mono - item.arrived_mono) * 1000.0,
                )
            self._repository.mark_completed(
                item.record_id,
                answer_text=llm_response.text,
                latency_ms=llm_response.latency_ms,
            )
            logger.info(
                "Question %s answered (%d ms)",
                item.record_id,
                int(llm_response.latency_ms),
            )
        except asyncio.CancelledError:
            rec = self._repository.get_by_id(item.record_id)
            if rec is not None and rec.status == "pending":
                self._repository.mark_failed(item.record_id, error="worker_shutdown")
                self._metrics.inc("questions_never_reached_endpoint")
            raise
        except LLMClientError as exc:
            self._repository.mark_failed(item.record_id, error=str(exc))
            logger.warning("Question %s LLM processing failed: %s", item.record_id, exc)
        except Exception as exc:
            self._repository.mark_failed(item.record_id, error=type(exc).__name__)
            logger.exception("Unexpected error processing question %s", item.record_id)

    async def process_one(
        self,
        frame_id: int,
        jpeg_bytes: bytes,
        *,
        arrived_mono: float | None = None,
    ) -> None:
        """Analyze a single frame serially and enqueue accepted questions for LLM submission."""
        frame_arrived = arrived_mono if arrived_mono is not None else time.perf_counter()
        result = await asyncio.to_thread(self._detector.evaluate_frame, jpeg_bytes)
        self._metrics.inc("frames_analyzed")
        if result.ocr_ms is not None:
            self._metrics.record_latency("ocr_ms", result.ocr_ms)
        if result.reason == "invalid_image":
            self._metrics.inc("malformed_frames")

        if result.is_duplicate:
            self._metrics.inc("duplicates_skipped")
            logger.debug("Skipped duplicate question (frame %d)", frame_id)
        if not result.should_process:
            return

        accepted_mono = result.accepted_mono or time.perf_counter()
        prepared_mono = result.prepared_mono or accepted_mono
        self._metrics.record_latency(
            "frame_to_detection_ms", (accepted_mono - frame_arrived) * 1000.0
        )
        self._metrics.record_latency(
            "image_prep_ms", (prepared_mono - accepted_mono) * 1000.0
        )

        logger.info(
            "New question detected (frame %d, confidence=%.2f)",
            frame_id,
            result.confidence,
        )

        self._metrics.inc("questions_detected")
        self._metrics.set_value("last_question_at", datetime.now(timezone.utc))

        record = self._repository.create_pending(
            frame_id=frame_id,
            ocr_text=result.raw_text,
            confidence=result.confidence,
            model=self._model_name,
        )

        payload_bytes = (
            result.cropped_jpeg_bytes
            if result.cropped_jpeg_bytes is not None
            else jpeg_bytes
        )
        item = SubmissionItem(
            record_id=record.id,
            frame_id=frame_id,
            payload_bytes=payload_bytes,
            media_type=result.upload_media_type,
            arrived_mono=frame_arrived,
            accepted_mono=accepted_mono,
            prepared_mono=prepared_mono,
            enqueued_mono=time.perf_counter(),
        )

        if not self.processing_active:
            # Direct standalone invocation (e.g. unit tests calling process_one directly).
            await self._submit_item(item, preacquired=False)
            return

        try:
            self._submission_queue.put_nowait(item)
            self._metrics.set_value(
                "submission_queue_size", self._submission_queue.qsize()
            )
        except asyncio.QueueFull:
            self._metrics.inc("submission_queue_dropped")
            self._metrics.inc("questions_never_reached_endpoint")
            self._repository.mark_failed(
                record.id, error="submission_queue_saturated"
            )
            logger.warning(
                "Submission queue saturated (size=%d); dropped question %s",
                self._submission_queue.maxsize,
                record.id,
            )
