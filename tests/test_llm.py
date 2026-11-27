"""Tests for multimodal LLM request formatting, response extraction, and retries."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.config import LLMConfig
from app.llm.client import (
    LLMClientError,
    MultimodalLLMClient,
    build_chat_payload,
    extract_response_text,
)
from app.metrics import MetricsRegistry
from tests.conftest import make_question_jpeg


def test_build_chat_payload_exact_structure() -> None:
    jpeg_bytes = make_question_jpeg(["What is 2 + 2?"])
    payload = build_chat_payload(
        jpeg_bytes=jpeg_bytes,
        model="gemma-4-E4B-it-GGUF:UD-Q4_K_XL",
        system_prompt="Return only the final answer.",
        temperature=0.0,
        stream=False,
        max_tokens=64,
    )

    assert payload["model"] == "gemma-4-E4B-it-GGUF:UD-Q4_K_XL"
    assert payload["temperature"] == 0.0
    assert payload["stream"] is False
    assert payload["max_tokens"] == 64
    assert payload["messages"][0] == {
        "role": "system",
        "content": "Return only the final answer.",
    }
    user_content = payload["messages"][1]["content"]
    assert user_content[0]["type"] == "image_url"
    expected_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    assert user_content[0]["image_url"]["url"] == f"data:image/jpeg;base64,{expected_b64}"


def test_extract_response_text() -> None:
    assert extract_response_text({"choices": [{"message": {"content": " 42 \n"}}]}) == "42"
    with pytest.raises(LLMClientError):
        extract_response_text({"choices": []})


@pytest.mark.asyncio
async def test_llm_client_posts_directly_and_retries_transient_errors() -> None:
    jpeg_bytes = make_question_jpeg(["What is 3 * 7?"])
    seen_urls: list[str] = []
    seen_headers: list[httpx.Headers] = []
    seen_bodies: list[dict] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        seen_urls.append(str(request.url))
        seen_headers.append(request.headers)
        seen_bodies.append(json.loads(request.content.decode("utf-8")))
        if calls == 1:
            return httpx.Response(429, json={"error": "rate_limited"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "21"}}]},
        )

    cfg = LLMConfig(
        endpoint="https://llm.example.com/v1/chat/completions",
        api_key="super-secret-llm-key",
        model="gemma-4-E4B-it-GGUF:UD-Q4_K_XL",
        system_prompt="Answer only.",
        temperature=0.0,
        stream=False,
        max_retries=2,
        retry_backoff_base=0.01,
    )
    metrics = MetricsRegistry()
    client = MultimodalLLMClient(
        cfg,
        "Answer only.",
        metrics=metrics,
        transport=httpx.MockTransport(handler),
    )

    resp = await client.answer_question(jpeg_bytes)
    assert resp.text == "21"
    assert calls == 2
    assert seen_urls[0] == "https://llm.example.com/v1/chat/completions"
    assert seen_headers[0]["Authorization"] == "Bearer super-secret-llm-key"
    assert seen_headers[0]["Content-Type"] == "application/json"
    assert seen_bodies[0]["model"] == "gemma-4-E4B-it-GGUF:UD-Q4_K_XL"
    assert metrics.llm_requests_succeeded == 1
    assert metrics.llm_retries == 1


@pytest.mark.asyncio
async def test_llm_client_does_not_leak_api_key_on_auth_error() -> None:
    secret = "sk-do-not-leak-me"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    cfg = LLMConfig(
        endpoint="https://llm.example.com/v1/chat/completions",
        api_key=secret,
        model="gemma-4-E4B-it-GGUF:UD-Q4_K_XL",
        system_prompt="Answer only.",
        max_retries=1,
        retry_backoff_base=0.01,
    )
    client = MultimodalLLMClient(
        cfg, "Answer only.", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(LLMClientError) as exc_info:
        await client.answer_question(make_question_jpeg(["Test?"]))
    assert secret not in str(exc_info.value)
