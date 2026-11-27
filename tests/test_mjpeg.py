"""Tests for MJPEG boundary extraction, incremental parsing, buffering, and client."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import SourceConfig
from app.metrics import MetricsRegistry
from app.source.buffer import BoundedFrameBuffer
from app.source.mjpeg import MjpegClient
from app.source.parser import MjpegStreamParser, extract_boundary
from tests.conftest import make_question_jpeg


def _build_multipart_body(frames: list[bytes], boundary: str = "frame") -> bytes:
    parts: list[bytes] = []
    for frame in frames:
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
        )
        parts.append(frame)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts)


def test_extract_boundary_variants() -> None:
    assert extract_boundary("multipart/x-mixed-replace; boundary=frame") == b"--frame"
    assert extract_boundary('multipart/x-mixed-replace; boundary="myboundary"') == b"--myboundary"
    assert extract_boundary("multipart/x-mixed-replace; boundary=--already") == b"--already"
    assert extract_boundary("image/jpeg") is None
    assert extract_boundary(None) is None


def test_parser_preserves_original_jpeg_bytes_across_chunks() -> None:
    jpeg1 = make_question_jpeg(["Question 1: What is 2 + 2?"])
    jpeg2 = make_question_jpeg(["Question 2: What is 3 + 3?"])
    raw = _build_multipart_body([jpeg1, jpeg2], boundary="frame")

    parser = MjpegStreamParser(boundary=b"--frame")
    extracted: list[bytes] = []
    chunk_size = 137  # arbitrary odd chunk size splitting headers & images
    for i in range(0, len(raw), chunk_size):
        extracted.extend(parser.feed(raw[i : i + chunk_size]))

    assert len(extracted) == 2
    assert extracted[0] == jpeg1
    assert extracted[1] == jpeg2
    assert parser.malformed_frames == 0


def test_parser_emits_length_delimited_jpeg_without_next_boundary() -> None:
    jpeg = make_question_jpeg(["Which frame is ready?"])
    raw = _build_multipart_body([jpeg])
    jpeg_end = raw.index(jpeg) + len(jpeg)
    parser = MjpegStreamParser(boundary=b"--frame", validate_decode=False)

    assert parser.feed(raw[: jpeg_end - 1]) == []
    assert parser.feed(raw[jpeg_end - 1 : jpeg_end]) == [jpeg]
    assert parser.feed(raw[jpeg_end:]) == []


def test_parser_falls_back_to_boundary_for_missing_or_incorrect_length() -> None:
    jpeg = make_question_jpeg(["Which frame is ready?"])
    raw = _build_multipart_body([jpeg, jpeg]).replace(
        f"Content-Length: {len(jpeg)}\r\n".encode(), b"Content-Length: 999999\r\n", 1
    ).replace(f"Content-Length: {len(jpeg)}\r\n".encode(), b"")
    parser = MjpegStreamParser(boundary=b"--frame", validate_decode=False)

    assert parser.feed(raw) == [jpeg, jpeg]


def test_parser_handles_malformed_and_incomplete_images() -> None:
    valid = make_question_jpeg(["Valid question?"])
    corrupt = b"\xff\xd8\x00\x01\x02corrupt-data\xff\xd9"
    truncated = b"\xff\xd8\x00\x01\x02incomplete"

    raw = _build_multipart_body([corrupt, truncated, valid], boundary="bnd")
    parser = MjpegStreamParser(boundary=b"--bnd")
    extracted = parser.feed(raw)

    assert extracted == [valid]
    assert parser.malformed_frames == 2


def test_parser_enforces_bounded_memory() -> None:
    parser = MjpegStreamParser(boundary=b"--bnd", max_buffer_bytes=2048)
    parser.feed(b"x" * 5000)
    assert parser.buffer_overflows >= 1
    assert len(parser._buffer) <= 2048


def test_bounded_frame_buffer_drops_oldest() -> None:
    buf = BoundedFrameBuffer(max_size=2)
    buf.push_nowait(b"frame-1")
    buf.push_nowait(b"frame-2")
    buf.push_nowait(b"frame-3")

    assert buf.qsize() == 2
    assert buf.dropped_count == 1


@pytest.mark.asyncio
async def test_mjpeg_client_streams_auth_headers_and_reconnects() -> None:
    jpeg1 = make_question_jpeg(["Q1: Capital of France?"])
    jpeg2 = make_question_jpeg(["Q2: Capital of Japan?"])
    stream_body_1 = _build_multipart_body([jpeg1], boundary="frame")
    stream_body_2 = _build_multipart_body([jpeg2], boundary="frame")

    attempts = 0
    seen_auth_headers: list[str | None] = []
    seen_paths: list[str] = []

    class _InterruptingStream(httpx.AsyncByteStream):
        def __init__(self, data: bytes, fail_midway: bool) -> None:
            self._data = data
            self._fail_midway = fail_midway

        async def __aiter__(self):
            yield self._data
            if self._fail_midway:
                raise httpx.ReadError("simulated connection drop")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        seen_auth_headers.append(request.headers.get("Authorization"))
        seen_paths.append(request.url.path)
        if attempts == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"},
                stream=_InterruptingStream(stream_body_1, fail_midway=True),
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"},
            stream=_InterruptingStream(stream_body_2, fail_midway=False),
        )

    cfg = SourceConfig(
        url="http://127.0.0.1:8080/api/stream/mjpeg",
        headers={"Authorization": "Bearer stream-secret"},
        reconnect=True,
        reconnect_delay=0.01,
        max_reconnect_delay=0.05,
    )
    buf = BoundedFrameBuffer(max_size=8)
    metrics = MetricsRegistry()
    client = MjpegClient(
        cfg,
        buf,
        metrics,
        transport=httpx.MockTransport(handler),
    )

    task = asyncio.create_task(client.run())
    for _ in range(100):
        if metrics.frames_received >= 2:
            break
        await asyncio.sleep(0.01)
    client.stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert seen_paths[0] == "/api/stream/mjpeg"
    assert all(h == "Bearer stream-secret" for h in seen_auth_headers)
    assert metrics.frames_received >= 2
    assert metrics.reconnects >= 1
