"""Tests for decoupled submission queue, HTTPX reuse, upload cropping, and latency metrics."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest

from app.config import CropConfig, DetectionConfig, LLMConfig, UploadCropConfig
from app.detection.state import QuestionDetector
from app.detection.visual import decode_jpeg, encode_image
from app.llm.client import MultimodalLLMClient, build_chat_payload
from app.main import create_app
from app.metrics import MetricsRegistry
from app.processing.worker import ProcessingWorker
from app.source.buffer import BoundedFrameBuffer
from app.storage.repository import InMemoryQuestionRepository
from tests.conftest import FakeOcrEngine, make_question_jpeg, make_test_config


class _LocalBodyTimingServer:
    """Minimal local HTTP/1.1 server recording exact request-body receipt timestamps."""

    def __init__(self, delay_by_seq: dict[int, float] | None = None, fail_seqs: set[int] | None = None) -> None:
        self.delay_by_seq = delay_by_seq or {}
        self.fail_seqs = fail_seqs or set()
        self.receipts: list[tuple[int, float, dict]] = []
        self._seq = 0
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                headers = await reader.readuntil(b"\r\n\r\n")
                content_length = 0
                for line in headers.decode("latin-1").split("\r\n")[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.strip().lower() == "content-length":
                            content_length = int(v.strip())
                body = await reader.readexactly(content_length)
                recv_mono = time.perf_counter()
                self._seq += 1
                seq = self._seq
                parsed = json.loads(body.decode("utf-8"))
                self.receipts.append((seq, recv_mono, parsed))

                if seq in self.fail_seqs:
                    resp_body = json.dumps({"error": "rate_limited"}).encode("utf-8")
                    status_line = b"HTTP/1.1 429 Too Many Requests\r\n"
                else:
                    delay = self.delay_by_seq.get(seq, 0.0)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    resp_body = json.dumps(
                        {"choices": [{"message": {"content": f"answer-{seq}"}}]}
                    ).encode("utf-8")
                    status_line = b"HTTP/1.1 200 OK\r\n"

                writer.write(
                    status_line
                    + b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(resp_body)}\r\n".encode("ascii")
                    + b"Connection: keep-alive\r\n\r\n"
                    + resp_body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, EOFError):
            pass
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_transport_upload_completion_matches_local_receiver_and_reuses_client() -> None:
    server = _LocalBodyTimingServer(delay_by_seq={1: 0.25, 2: 0.25})
    await server.start()
    try:
        cfg = LLMConfig(
            endpoint=f"http://127.0.0.1:{server.port}/v1/chat/completions",
            api_key="secret",
            model="test-model",
            system_prompt="Answer only.",
            timeout=5.0,
        )
        jpg = make_question_jpeg(["What is 2 + 2?"])
        async with MultimodalLLMClient(cfg, "Answer only.") as client:
            http_client_1 = client._get_client()
            resp1 = await client.answer_question(jpg)
            resp2 = await client.answer_question(jpg)
            http_client_2 = client._get_client()

            assert http_client_1 is http_client_2
            assert resp1.first_upload_completed_mono is not None
            assert resp2.first_upload_completed_mono is not None
            # Upload completes ~250ms BEFORE response completes
            assert (resp1.response_completed_mono - resp1.first_upload_completed_mono) >= 0.20  # type: ignore[operator]
            # Transport upload timestamp matches server full-body receipt within 50ms
            assert abs(server.receipts[0][1] - resp1.first_upload_completed_mono) < 0.05
            assert abs(server.receipts[1][1] - resp2.first_upload_completed_mono) < 0.05
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_decoupled_worker_out_of_order_completion_and_latest_semantics() -> None:
    # Request 1 (Q1) takes 0.35s, Request 2 (Q2) takes 0.05s -> Q2 completes first!
    server = _LocalBodyTimingServer(delay_by_seq={1: 0.35, 2: 0.05})
    await server.start()
    try:
        q1 = make_question_jpeg(["Q1: First question?"], bg_color=(250, 250, 250))
        q2 = make_question_jpeg(["Q2: Second question?"], bg_color=(220, 230, 240))
        img1 = decode_jpeg(q1)
        img2 = decode_jpeg(q2)
        assert img1 is not None and img2 is not None

        ocr = FakeOcrEngine(
            mapping={int(img1.mean()): "Q1: First question?", int(img2.mean()): "Q2: Second question?"}
        )
        cfg = make_test_config(
            llm_endpoint=f"http://127.0.0.1:{server.port}/v1/chat/completions",
            stability_frames=1,
        )
        cfg.detection.crop = None
        cfg.llm.max_concurrent_requests = 2
        cfg.llm.submission_queue_size = 8

        metrics = MetricsRegistry()
        buffer = BoundedFrameBuffer(max_size=1)
        detector = QuestionDetector(cfg.detection, ocr_engine=ocr)
        llm_client = MultimodalLLMClient(cfg.llm, "Answer only.", metrics=metrics)
        repo = InMemoryQuestionRepository(max_history=10)
        worker = ProcessingWorker(
            buffer=buffer,
            detector=detector,
            llm_client=llm_client,
            repository=repo,
            metrics=metrics,
            model_name=cfg.llm.model,
            max_concurrent_requests=2,
            submission_queue_size=8,
        )

        task = asyncio.create_task(worker.run())
        buffer.push_nowait(q1)
        await asyncio.sleep(0.05)
        buffer.push_nowait(q2)

        for _ in range(100):
            if metrics.llm_requests_succeeded >= 2:
                break
            await asyncio.sleep(0.02)

        worker.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await llm_client.aclose()

        r1 = repo.get_by_id("q-000001")
        r2 = repo.get_by_id("q-000002")
        assert r1 is not None and r2 is not None
        assert r1.answer_text == "answer-1"
        assert r2.answer_text == "answer-2"
        # /latest must return Q2 (newest detected question with completed answer)
        latest = repo.get_latest_completed()
        assert latest is not None and latest.id == "q-000002"

        # Verify each sent screenshot matches its detected question
        sent_b64_1 = server.receipts[0][2]["messages"][1]["content"][0]["image_url"]["url"].split(",", 1)[1]
        sent_b64_2 = server.receipts[1][2]["messages"][1]["content"][0]["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(sent_b64_1) == q1
        assert base64.b64decode(sent_b64_2) == q2
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_same_question_screen_changes_do_not_add_llm_requests() -> None:
    first_text = "Question 1: What is the capital of France?"
    second_text = "Question 2: Which value represents the speed of light?"
    first = make_question_jpeg([first_text])
    cursor_image = decode_jpeg(first)
    assert cursor_image is not None
    cursor_image[360:368, 750:758] = 0
    with_cursor, _ = encode_image(cursor_image, quality=90)
    repainted = make_question_jpeg([first_text], bg_color=(230, 235, 240))
    second = make_question_jpeg([second_text])
    repainted_image = decode_jpeg(repainted)
    second_image = decode_jpeg(second)
    assert repainted_image is not None and second_image is not None
    ocr = FakeOcrEngine(
        mapping={
            int(repainted_image.mean()): f"{first_text} 0:05",
            int(second_image.mean()): second_text,
        },
        default_text=first_text,
    )
    config = make_test_config()
    sent_images: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        url = data["messages"][1]["content"][0]["image_url"]["url"]
        sent_images.append(base64.b64decode(url.split(",", 1)[1]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    metrics = MetricsRegistry()
    detector = QuestionDetector(config.detection, ocr_engine=ocr)
    transport = httpx.MockTransport(handle)
    async with MultimodalLLMClient(config.llm, "Answer only.", transport=transport) as client:
        worker = ProcessingWorker(
            buffer=BoundedFrameBuffer(max_size=1),
            detector=detector,
            llm_client=client,
            repository=InMemoryQuestionRepository(max_history=10),
            metrics=metrics,
            model_name=config.llm.model,
        )
        frames = (
            first, first, first, with_cursor, with_cursor,
            repainted, repainted, second, second,
        )
        for frame_id, frame in enumerate(frames, start=1):
            await worker.process_one(frame_id, frame)

    assert sent_images == [first, second]
    assert metrics.duplicates_skipped >= 1
    assert ocr.calls <= 4
    assert metrics.snapshot()["frame_to_upload_p50_ms"] is not None


@pytest.mark.asyncio
async def test_retry_releases_send_slot_during_backoff() -> None:
    # Even with max_concurrent_requests=1, when Q1 hits 429 and sleeps in backoff,
    # Q2 must be able to use the released send slot immediately!
    events: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        img_url = body["messages"][1]["content"][0]["image_url"]["url"]
        events.append((img_url[-12:], time.perf_counter()))
        if len(events) == 1:
            return httpx.Response(429, json={"error": "rate_limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cfg = make_test_config()
    cfg.llm.max_retries = 1
    cfg.llm.retry_backoff_base = 0.25
    cfg.llm.max_concurrent_requests = 1

    client = MultimodalLLMClient(
        cfg.llm, "Answer only.", transport=httpx.MockTransport(handler)
    )
    sem = asyncio.Semaphore(1)
    jpg1 = make_question_jpeg(["Q1?"], bg_color=(250, 250, 250))
    jpg2 = make_question_jpeg(["Q2?"], bg_color=(200, 200, 200))

    await sem.acquire()
    t1 = asyncio.create_task(
        client.answer_question(jpg1, send_semaphore=sem, preacquired=True)
    )
    await asyncio.sleep(0.03)
    # Q1 is now sleeping in 0.25s backoff; Q2 should acquire the slot immediately!
    t0_q2 = time.perf_counter()
    resp2 = await client.answer_question(jpg2, send_semaphore=sem, preacquired=False)
    q2_elapsed = time.perf_counter() - t0_q2
    resp1 = await t1
    await client.aclose()

    assert resp1.text == "ok"
    assert resp2.text == "ok"
    assert q2_elapsed < 0.15  # did not wait for Q1's 0.25s backoff


@pytest.mark.asyncio
async def test_queue_saturation_and_shutdown_leave_no_stranded_pending_records() -> None:
    server = _LocalBodyTimingServer(delay_by_seq={1: 1.0, 2: 1.0, 3: 1.0})
    await server.start()
    try:
        cfg = make_test_config(
            llm_endpoint=f"http://127.0.0.1:{server.port}/v1/chat/completions",
            stability_frames=1,
        )
        cfg.detection.crop = None
        cfg.detection.deduplication = False
        cfg.llm.max_concurrent_requests = 1
        cfg.llm.submission_queue_size = 1

        app = create_app(cfg, start_background=False)
        ctx = app.state.container
        # Replace detector OCR with deterministic stub
        ctx.detector = QuestionDetector(
            cfg.detection, ocr_engine=FakeOcrEngine(default_text="What is 2 + 2?")
        )
        ctx.worker._detector = ctx.detector

        worker_task = asyncio.create_task(ctx.worker.run())
        await asyncio.sleep(0.02)

        # Send 4 distinct frames rapidly while server delays 1.0s
        for i in range(4):
            jpg = make_question_jpeg([f"Question {i}: What is {i}?"])
            # Vary color so pHash changes
            jpg = make_question_jpeg(
                [f"Question {i}: What is {i}?"],
                bg_color=(250 - i * 40, 250 - i * 20, 240),
            )
            await ctx.worker.process_one(i + 1, jpg)
            await asyncio.sleep(0.02)

        # At least one should have been dropped due to queue saturation (maxsize=1, conc=1)
        assert ctx.metrics.submission_queue_dropped >= 1
        assert ctx.metrics.questions_never_reached_endpoint >= 1

        # Shutdown immediately while remaining items are in-flight/queued
        ctx.worker.stop()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await ctx.llm_client.aclose()

        # Verify ZERO stranded pending records
        history = ctx.repository.list_history(limit=20)
        assert all(rec.status != "pending" for rec in history)
    finally:
        await server.stop()


def test_upload_crop_and_png_data_url() -> None:
    # Dark mode slide with diagram + text
    jpg = make_question_jpeg(
        ["Question: What is shown?", "A) Circle", "B) Square"],
        width=1920,
        height=1080,
        bg_color=(20, 24, 34),
        fg_color=(240, 240, 240),
    )
    det_cfg = DetectionConfig(
        stability_check=False,
        crop=CropConfig(left=0.26, top=0.06, right=0.74, bottom=1.0),
        upload_crop=UploadCropConfig(mode="content", format="png", max_dim=1024),
    )
    detector = QuestionDetector(
        det_cfg, ocr_engine=FakeOcrEngine(default_text="Question: What is shown?")
    )
    res = detector.evaluate_frame(jpg)
    assert res.should_process is True
    assert res.accepted_mono is not None
    assert res.prepared_mono is not None
    assert res.prepared_mono >= res.accepted_mono
    assert res.upload_media_type == "image/png"
    assert res.cropped_jpeg_bytes is not None
    assert res.cropped_jpeg_bytes.startswith(b"\x89PNG\r\n\x1a\n")

    payload = build_chat_payload(
        jpeg_bytes=res.cropped_jpeg_bytes,
        model="test-model",
        system_prompt="Answer only.",
        image_media_type=res.upload_media_type,
    )
    url = payload["messages"][1]["content"][0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
