"""OpenAI-compatible multimodal LLM client using HTTPX."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import LLMConfig
from app.metrics import MetricsRegistry

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class LLMClientError(RuntimeError):
    """Raised when an LLM request fails without exposing credentials."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    latency_ms: float
    model: str
    first_upload_completed_mono: float | None = None
    response_completed_mono: float | None = None


class _UploadTimingStream(httpx.AsyncByteStream):
    """Wraps an AsyncByteStream to timestamp transport-layer upload completion."""

    def __init__(
        self,
        inner: httpx.AsyncByteStream,
        on_complete: Callable[[float], None],
    ) -> None:
        self._inner = inner
        self._on_complete = on_complete
        self._recorded = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk
        if not self._recorded:
            self._recorded = True
            self._on_complete(time.perf_counter())

    async def aclose(self) -> None:
        await self._inner.aclose()


def _infer_media_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(_PNG_SIGNATURE):
        return "image/png"
    return "image/jpeg"


def build_chat_payload(
    *,
    jpeg_bytes: bytes,
    model: str,
    system_prompt: str,
    temperature: float = 0.0,
    stream: bool = False,
    max_tokens: int | None = None,
    image_media_type: str | None = None,
) -> dict[str, Any]:
    """Construct the OpenAI-compatible multimodal Chat Completions payload."""
    media_type = image_media_type or _infer_media_type(jpeg_bytes)
    encoded_image = base64.b64encode(jpeg_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "stream": stream,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{encoded_image}",
                        },
                    }
                ],
            },
        ],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def extract_response_text(data: dict[str, Any]) -> str:
    """Extract generated text from choices[0].message.content."""
    try:
        choices = data["choices"]
        if not isinstance(choices, list) or not choices:
            raise KeyError("choices is empty")
        message = choices[0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMClientError(f"Malformed LLM response structure: {exc}") from exc

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined
    raise LLMClientError("LLM response content is empty or unsupported")


class MultimodalLLMClient:
    """Async client for OpenAI/LiteLLM-compatible Chat Completions endpoints."""

    def __init__(
        self,
        config: LLMConfig,
        system_prompt: str,
        metrics: MetricsRegistry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._system_prompt = system_prompt
        self._metrics = metrics
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            pool_size = max(10, self._config.max_concurrent_requests * 2)
            client_kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self._config.timeout),
                "limits": httpx.Limits(
                    max_connections=pool_size,
                    max_keepalive_connections=pool_size,
                ),
            }
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**client_kwargs)
        return self._client

    async def start(self) -> None:
        """Eagerly initialize the shared HTTPX client."""
        self._get_client()

    async def aclose(self) -> None:
        """Close the shared HTTPX connection pool."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> MultimodalLLMClient:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def answer_question(
        self,
        jpeg_bytes: bytes,
        *,
        image_media_type: str | None = None,
        send_semaphore: asyncio.Semaphore | None = None,
        preacquired: bool = False,
    ) -> LLMResponse:
        """Submit image bytes to the configured LLM endpoint with retries."""
        payload = build_chat_payload(
            jpeg_bytes=jpeg_bytes,
            model=self._config.model,
            system_prompt=self._system_prompt,
            temperature=self._config.temperature,
            stream=self._config.stream,
            max_tokens=self._config.max_tokens,
            image_media_type=image_media_type,
        )
        body_bytes = json.dumps(payload).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        api_key = self._config.api_key.get_secret_value().strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        if self._metrics is not None:
            self._metrics.inc("llm_requests_total")

        client = self._get_client()
        start_ts = time.perf_counter()
        first_upload_mono: float | None = None
        last_error: Exception | None = None

        def _on_upload_done(ts: float) -> None:
            nonlocal first_upload_mono
            if first_upload_mono is None:
                first_upload_mono = ts

        for attempt in range(self._config.max_retries + 1):
            holding_slot = False
            try:
                if send_semaphore is not None:
                    if attempt == 0 and preacquired:
                        holding_slot = True
                    else:
                        await send_semaphore.acquire()
                        holding_slot = True

                req = client.build_request(
                    "POST",
                    self._config.endpoint,
                    content=body_bytes,
                    headers=headers,
                )
                if isinstance(req.stream, httpx.AsyncByteStream):
                    req.stream = _UploadTimingStream(req.stream, _on_upload_done)

                response = await client.send(req)
                if first_upload_mono is None:
                    first_upload_mono = time.perf_counter()

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    raise httpx.HTTPStatusError(
                        f"Retryable HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                if response.status_code >= 400:
                    raise LLMClientError(
                        f"LLM endpoint returned HTTP {response.status_code}"
                    )

                data = response.json()
                text = extract_response_text(data)
                completed_mono = time.perf_counter()
                latency_ms = round((completed_mono - start_ts) * 1000.0, 2)
                if self._metrics is not None:
                    self._metrics.inc("llm_requests_succeeded")
                    self._metrics.set_value("last_llm_latency_ms", latency_ms)
                return LLMResponse(
                    text=text,
                    latency_ms=latency_ms,
                    model=self._config.model,
                    first_upload_completed_mono=first_upload_mono,
                    response_completed_mono=completed_mono,
                )
            except LLMClientError:
                if self._metrics is not None:
                    self._metrics.inc("llm_requests_failed")
                raise
            except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_error = exc
                # Release send slot BEFORE sleeping in retry backoff.
                if holding_slot and send_semaphore is not None:
                    send_semaphore.release()
                    holding_slot = False
                if attempt < self._config.max_retries:
                    if self._metrics is not None:
                        self._metrics.inc("llm_retries")
                    backoff = self._config.retry_backoff_base * (2**attempt)
                    logger.warning(
                        "LLM request attempt %d failed (%s); retrying in %.1fs",
                        attempt + 1,
                        type(exc).__name__,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    break
            except asyncio.CancelledError:
                if attempt == 0 and preacquired and not holding_slot:
                    pass
                raise
            except Exception as exc:
                last_error = exc
                break
            finally:
                if holding_slot and send_semaphore is not None:
                    send_semaphore.release()
                    holding_slot = False

        if self._metrics is not None:
            self._metrics.inc("llm_requests_failed")
        raise LLMClientError(
            f"LLM request failed after {self._config.max_retries + 1} attempts: "
            f"{type(last_error).__name__}"
        )