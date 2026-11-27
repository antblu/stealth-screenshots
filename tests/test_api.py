"""Tests for FastAPI endpoints, bearer authentication, and worker integration."""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app
from tests.conftest import make_test_config


@pytest.mark.asyncio
async def test_api_auth_and_endpoints() -> None:
    cfg = make_test_config(server_token="api-secret-token")
    app = create_app(cfg, start_background=False)
    repo = app.state.container.repository
    metrics = app.state.container.metrics

    rec = repo.create_pending(
        frame_id=1,
        ocr_text="What is 2 + 2?",
        confidence=0.9,
        model=cfg.llm.model,
    )
    repo.mark_completed(rec.id, answer_text="4", latency_ms=123.4)
    metrics.inc("questions_detected")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Public health check
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        # Private endpoints reject missing/invalid bearer token
        for path in ("/", "/status", "/latest", "/latest/text", "/history", f"/questions/{rec.id}", "/metrics"):
            unauth = await client.get(path)
            assert unauth.status_code == 401
            wrong = await client.get(path, headers={"Authorization": "Bearer wrong"})
            assert wrong.status_code == 401

        auth_headers = {"Authorization": "Bearer api-secret-token"}

        root_text = await client.get("/", headers=auth_headers)
        assert root_text.status_code == 200
        assert root_text.headers["content-type"].startswith("text/plain")
        assert root_text.text == "4"

        latest_text = await client.get("/latest/text", headers=auth_headers)
        assert latest_text.status_code == 200
        assert latest_text.headers["content-type"].startswith("text/plain")
        assert latest_text.text == "4"

        latest_json = await client.get("/latest", headers=auth_headers)
        assert latest_json.status_code == 200
        assert latest_json.json()["id"] == rec.id
        assert latest_json.json()["answer_text"] == "4"

        history = await client.get("/history", headers=auth_headers)
        assert history.status_code == 200
        assert history.json()["total"] == 1

        by_id = await client.get(f"/questions/{rec.id}", headers=auth_headers)
        assert by_id.status_code == 200
        assert by_id.json()["id"] == rec.id

        missing = await client.get("/questions/q-999999", headers=auth_headers)
        assert missing.status_code == 404

        status_resp = await client.get("/status", headers=auth_headers)
        assert status_resp.status_code == 200
        assert status_resp.json()["model"] == cfg.llm.model

        metrics_resp = await client.get("/metrics", headers=auth_headers)
        assert metrics_resp.status_code == 200
        assert metrics_resp.json()["questions_detected"] == 1


@pytest.mark.asyncio
async def test_api_rejects_when_server_token_unconfigured() -> None:
    cfg = make_test_config(server_token="")
    app = create_app(cfg, start_background=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/latest", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_processing_worker_end_to_end() -> None:
    from app.detection.state import QuestionDetector
    from app.llm.client import MultimodalLLMClient
    from app.metrics import MetricsRegistry
    from app.processing.worker import ProcessingWorker
    from app.source.buffer import BoundedFrameBuffer
    from app.storage.repository import InMemoryQuestionRepository
    from tests.conftest import FakeOcrEngine, make_question_jpeg

    def llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "42"}}]})

    cfg = make_test_config(stability_frames=2)
    metrics = MetricsRegistry()
    buffer = BoundedFrameBuffer(max_size=4)
    detector = QuestionDetector(
        cfg.detection,
        ocr_engine=FakeOcrEngine(default_text="What is 6 * 7?"),
    )
    llm_client = MultimodalLLMClient(
        cfg.llm,
        "Answer only.",
        metrics=metrics,
        transport=httpx.MockTransport(llm_handler),
    )
    repo = InMemoryQuestionRepository(max_history=10)
    worker = ProcessingWorker(
        buffer=buffer,
        detector=detector,
        llm_client=llm_client,
        repository=repo,
        metrics=metrics,
        model_name=cfg.llm.model,
    )

    jpeg = make_question_jpeg(["What is 6 * 7?"])
    await worker.process_one(1, jpeg)
    assert repo.count() == 0  # awaiting stability

    await worker.process_one(2, jpeg)
    latest = repo.get_latest_completed()
    assert latest is not None
    assert latest.answer_text == "42"
    assert metrics.questions_detected == 1
    assert metrics.llm_requests_succeeded == 1
