"""Opt-in live integration test against the configured LiteLLM endpoint.

Run explicitly with:
    LIVE_INTEGRATION=1 pytest -m integration
    Set LLM_ENDPOINT, LLM_MODEL, and LLM_API_KEY in .env or the environment.
"""

from __future__ import annotations

import os

import pytest

from app.config import LLMConfig, load_dotenv_if_present
from app.llm.client import MultimodalLLMClient
from tests.conftest import make_question_jpeg


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_litellm_multimodal_question() -> None:
    load_dotenv_if_present()
    if os.environ.get("LIVE_INTEGRATION") != "1":
        pytest.skip("Set LIVE_INTEGRATION=1 to run live LiteLLM integration tests")

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        pytest.skip("LLM_API_KEY is required for live LiteLLM integration test")

    endpoint = os.environ.get("LLM_ENDPOINT", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not endpoint or not model:
        pytest.skip("LLM_ENDPOINT and LLM_MODEL are required for live integration")
    system_prompt = (
        "Answer the question presented in the supplied image. "
        "Return only the final answer. Do not provide explanations, reasoning, "
        "introductory remarks, or additional commentary. "
        "If the question cannot be determined, return UNKNOWN."
    )

    jpeg_bytes = make_question_jpeg(
        ["Question: What is 7 + 5?", "A) 10", "B) 11", "C) 12", "D) 13"]
    )
    cfg = LLMConfig(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        temperature=0.0,
        stream=False,
        timeout=60.0,
    )
    client = MultimodalLLMClient(cfg, system_prompt)
    resp = await client.answer_question(jpeg_bytes)

    assert resp.text
    assert "12" in resp.text or "C" in resp.text.upper()
