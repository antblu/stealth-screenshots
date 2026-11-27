"""Application assembly, lifecycle management, and ASGI entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from app.api.routes import router
from app.config import AppConfig, load_config
from app.detection.state import QuestionDetector
from app.llm.client import MultimodalLLMClient
from app.metrics import MetricsRegistry
from app.processing.worker import ProcessingWorker
from app.source.buffer import BoundedFrameBuffer
from app.source.mjpeg import MjpegClient
from app.storage.repository import InMemoryQuestionRepository


@dataclass
class AppContainer:
    config: AppConfig
    metrics: MetricsRegistry
    buffer: BoundedFrameBuffer
    mjpeg_client: MjpegClient
    detector: QuestionDetector
    llm_client: MultimodalLLMClient
    repository: InMemoryQuestionRepository
    worker: ProcessingWorker


def build_container(
    config: AppConfig,
    *,
    base_dir: Path | None = None,
) -> AppContainer:
    metrics = MetricsRegistry()
    buffer = BoundedFrameBuffer(max_size=config.source.max_queue_size)
    mjpeg_client = MjpegClient(config.source, buffer, metrics)
    detector = QuestionDetector(config.detection)
    system_prompt = config.llm.resolve_system_prompt(base_dir=base_dir)
    llm_client = MultimodalLLMClient(config.llm, system_prompt, metrics=metrics)
    repository = InMemoryQuestionRepository(max_history=config.storage.max_history)
    worker = ProcessingWorker(
        buffer=buffer,
        detector=detector,
        llm_client=llm_client,
        repository=repository,
        metrics=metrics,
        model_name=config.llm.model,
        max_concurrent_requests=config.llm.max_concurrent_requests,
        submission_queue_size=config.llm.submission_queue_size,
    )
    return AppContainer(
        config=config,
        metrics=metrics,
        buffer=buffer,
        mjpeg_client=mjpeg_client,
        detector=detector,
        llm_client=llm_client,
        repository=repository,
        worker=worker,
    )


def create_app(
    config: AppConfig | None = None,
    *,
    start_background: bool = True,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    resolved_config = config or load_config()
    logging.basicConfig(
        level=getattr(logging, resolved_config.logging.level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    container = build_container(resolved_config, base_dir=Path.cwd())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task[None]] = []
        if start_background:
            tasks.append(
                asyncio.create_task(
                    container.mjpeg_client.run(), name="mjpeg-ingestion"
                )
            )
            tasks.append(
                asyncio.create_task(
                    container.worker.run(), name="question-worker"
                )
            )
        try:
            yield
        finally:
            container.mjpeg_client.stop()
            container.worker.stop()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await container.llm_client.aclose()

    app = FastAPI(
        title="MJPEG Question Detection & LLM Processor",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.server_token = resolved_config.server.bearer_token.get_secret_value().strip()
    app.include_router(router)
    return app


def main() -> None:
    cfg = load_config()
    app = create_app(cfg, start_background=True)
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=cfg.logging.level.lower(),
    )


if __name__ == "__main__":
    main()
