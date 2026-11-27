"""Asynchronous MJPEG HTTP streaming client."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.config import SourceConfig
from app.metrics import MetricsRegistry
from app.source.buffer import BoundedFrameBuffer
from app.source.parser import MjpegStreamParser, extract_boundary

logger = logging.getLogger(__name__)


class MjpegClient:
    """Persistent async HTTP client that ingests MJPEG frames into a bounded buffer."""

    def __init__(
        self,
        config: SourceConfig,
        buffer: BoundedFrameBuffer,
        metrics: MetricsRegistry,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._buffer = buffer
        self._metrics = metrics
        self._transport = transport
        self._stop_event = asyncio.Event()
        self.connected: bool = False
        self.last_error: str | None = None

    def stop(self) -> None:
        """Signal the client loop to stop."""
        self._stop_event.set()

    async def run(self) -> None:
        """Continuously consume the MJPEG stream with automatic reconnection."""
        delay = self._config.reconnect_delay

        while not self._stop_event.is_set():
            try:
                await self._consume_once()
                delay = self._config.reconnect_delay
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("MJPEG stream error: %s", self.last_error)

            if self._stop_event.is_set() or not self._config.reconnect:
                break

            self._metrics.inc("reconnects")
            logger.info("Reconnecting to MJPEG source in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2.0, self._config.max_reconnect_delay)

        self.connected = False

    async def _consume_once(self) -> None:
        timeout = httpx.Timeout(
            connect=self._config.connect_timeout,
            read=self._config.read_timeout,
            write=10.0,
            pool=10.0,
        )
        client_kwargs: dict = {
            "timeout": timeout,
            "follow_redirects": True,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream(
                "GET",
                self._config.url,
                headers=self._config.headers or None,
            ) as response:
                response.raise_for_status()
                self.connected = True
                self.last_error = None

                boundary = extract_boundary(response.headers.get("content-type"))
                parser = MjpegStreamParser(
                    boundary=boundary,
                    max_buffer_bytes=self._config.max_buffer_bytes,
                    validate_decode=False,  # The detector decodes the frames it consumes.
                )
                prev_malformed = 0

                async for chunk in response.aiter_bytes():
                    if self._stop_event.is_set():
                        break
                    frames = parser.feed(chunk)
                    if parser.malformed_frames > prev_malformed:
                        self._metrics.inc(
                            "malformed_frames",
                            parser.malformed_frames - prev_malformed,
                        )
                        prev_malformed = parser.malformed_frames

                    for jpeg_bytes in frames:
                        prev_dropped = self._buffer.dropped_count
                        self._buffer.push_nowait(jpeg_bytes)
                        self._metrics.inc("frames_received")
                        self._metrics.set_value(
                            "last_frame_at", datetime.now(timezone.utc)
                        )
                        if self._buffer.dropped_count > prev_dropped:
                            self._metrics.inc(
                                "frames_dropped",
                                self._buffer.dropped_count - prev_dropped,
                            )

                self.connected = False
