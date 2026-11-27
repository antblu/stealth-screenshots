"""Configuration loading and validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AppConfig, expand_env_vars, load_config


def test_expand_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_HOST", "127.0.0.1")
    data = {
        "url": "http://${TEST_HOST}:8080/stream",
        "fallback": "${MISSING_VAR:-default_val}",
        "empty": "${MISSING_VAR}",
    }
    expanded = expand_env_vars(data)
    assert expanded["url"] == "http://127.0.0.1:8080/stream"
    assert expanded["fallback"] == "default_val"
    assert expanded["empty"] == ""


def test_load_project_configuration_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MJPEG_URL", "http://127.0.0.1:8080/stream")
    monkeypatch.setenv("LLM_ENDPOINT", "https://llm.example.com/v1/chat/completions")
    monkeypatch.setenv("LLM_MODEL", "gemma-4-E4B-it-GGUF:UD-Q4_K_XL")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("SERVER_TOKEN", "server-secret")

    cfg = load_config(Path("configuration.yaml"), load_dotenv=False)
    assert cfg.source.url == "http://127.0.0.1:8080/stream"
    assert cfg.source.max_queue_size == 1
    assert cfg.detection.stability_check is True
    assert cfg.detection.crop is not None
    assert cfg.detection.crop.left == pytest.approx(0.26)
    assert cfg.detection.crop.top == pytest.approx(0.06)
    assert cfg.detection.crop.right == pytest.approx(0.74)
    assert cfg.detection.crop.bottom == pytest.approx(1.0)
    assert cfg.llm.endpoint == "https://llm.example.com/v1/chat/completions"
    assert cfg.llm.model == "gemma-4-E4B-it-GGUF:UD-Q4_K_XL"
    assert cfg.server.port == 8000
    prompt = cfg.llm.resolve_system_prompt()
    assert "UNKNOWN" in prompt


def test_reject_reused_llm_and_server_token() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        AppConfig.model_validate(
            {
                "source": {"url": "http://127.0.0.1:8080/stream"},
                "llm": {
                    "endpoint": "https://llm.example.com/v1/chat/completions",
                    "api_key": "same-secret",
                    "model": "test-model",
                },
                "server": {"bearer_token": "same-secret"},
            }
        )


def test_reject_invalid_urls() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "source": {"url": "rtsp://127.0.0.1:8554/stream"},
                "llm": {
                    "endpoint": "https://llm.example.com/v1/chat/completions",
                    "model": "test-model",
                },
            }
        )


def test_reject_invalid_crop_bounds() -> None:
    with pytest.raises(ValidationError, match="crop.left"):
        AppConfig.model_validate(
            {
                "source": {"url": "http://127.0.0.1:8080/stream"},
                "detection": {"crop": {"left": 0.7, "right": 0.3}},
                "llm": {
                    "endpoint": "https://llm.example.com/v1/chat/completions",
                    "model": "test-model",
                },
            }
        )
