"""Bounded async frame buffer decoupling MJPEG ingestion from analysis."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class FrameEnvelope:
    """Immutable container for a received JPEG frame."""

    frame_id: int
    jpeg_bytes: bytes
    received_at: datetime
    arrived_mono: float = field(default_factory=time.perf_counter)

    @property
    def size_bytes(self) -> int:
        return len(self.jpeg_bytes)


class BoundedFrameBuffer:
    """Bounded async queue that drops oldest frames when full."""

    def __init__(self, max_size: int = 8) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._queue: asyncio.Queue[FrameEnvelope] = asyncio.Queue(maxsize=max_size)
        self._next_id: int = 1
        self.dropped_count: int = 0

    def push_nowait(self, jpeg_bytes: bytes) -> FrameEnvelope:
        """Enqueue a JPEG frame, dropping the oldest frame if at capacity."""
        envelope = FrameEnvelope(
            frame_id=self._next_id,
            jpeg_bytes=jpeg_bytes,
            received_at=datetime.now(timezone.utc),
            arrived_mono=time.perf_counter(),
        )
        self._next_id += 1

        if self._queue.full():
            try:
                self._queue.get_nowait()
                self.dropped_count += 1
            except asyncio.QueueEmpty:
                pass

        self._queue.put_nowait(envelope)
        return envelope

    async def get(self) -> FrameEnvelope:
        """Wait for and return the next FrameEnvelope."""
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()

    def clear(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
