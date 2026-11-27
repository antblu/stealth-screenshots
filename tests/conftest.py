"""Shared test fixtures and synthetic educational image helpers."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from app.config import (
    AppConfig,
    DetectionConfig,
    LLMConfig,
    LoggingConfig,
    ServerConfig,
    SourceConfig,
    StorageConfig,
)


def make_question_jpeg(
    text_lines: list[str],
    *,
    width: int = 800,
    height: int = 400,
    bg_color: tuple[int, int, int] = (255, 255, 255),
    fg_color: tuple[int, int, int] = (20, 20, 20),
    header_color: tuple[int, int, int] = (30, 80, 160),
    quality: int = 90,
) -> bytes:
    """Render a synthetic educational slide as JPEG bytes."""
    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, 48], fill=header_color)
    font = ImageFont.load_default(size=28)
    y = 72
    for line in text_lines:
        draw.text((40, y), line, fill=fg_color, font=font)
        y += 48
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def make_test_config(
    *,
    mjpeg_url: str = "http://127.0.0.1:8080/stream",
    llm_endpoint: str = "https://llm.example.com/v1/chat/completions",
    llm_api_key: str = "test-llm-key",
    server_token: str = "test-server-token",
    stability_frames: int = 2,
) -> AppConfig:
    return AppConfig(
        source=SourceConfig(
            url=mjpeg_url,
            reconnect=False,
            connect_timeout=2.0,
            read_timeout=5.0,
        ),
        detection=DetectionConfig(
            visual_change_detection=True,
            ocr_enabled=True,
            stability_check=True,
            deduplication=True,
            stability_frames=stability_frames,
        ),
        llm=LLMConfig(
            endpoint=llm_endpoint,
            api_key=llm_api_key,
            model="gemma-4-E4B-it-GGUF:UD-Q4_K_XL",
            system_prompt="Answer only.",
            temperature=0.0,
            stream=False,
            timeout=10.0,
            max_retries=1,
            retry_backoff_base=0.01,
        ),
        server=ServerConfig(
            host="127.0.0.1",
            port=8000,
            bearer_token=server_token,
        ),
        storage=StorageConfig(max_history=10),
        logging=LoggingConfig(level="INFO"),
    )


@dataclass
class FakeOcrEngine:
    """Deterministic OCR stub for unit tests."""

    mapping: dict[int, str] | None = None
    default_text: str = ""
    calls: int = 0

    def extract_text(self, image) -> str:
        self.calls += 1
        if self.mapping is not None:
            key = int(image.mean())
            if key in self.mapping:
                return self.mapping[key]
        return self.default_text
