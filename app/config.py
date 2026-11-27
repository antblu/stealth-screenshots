"""Configuration loading, environment expansion, and Pydantic validation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_dotenv_if_present(dotenv_path: str | Path = ".env") -> None:
    """Load key=value pairs from a .env file into os.environ without overwriting."""
    path = Path(dotenv_path)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} placeholders in YAML data."""
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default = match.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            return default if default is not None else ""

        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


class SourceConfig(BaseModel):
    type: Literal["mjpeg"] = "mjpeg"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    connect_timeout: float = Field(default=10.0, gt=0)
    read_timeout: float = Field(default=30.0, gt=0)
    reconnect: bool = True
    reconnect_delay: float = Field(default=1.0, gt=0)
    max_reconnect_delay: float = Field(default=30.0, gt=0)
    max_buffer_bytes: int = Field(default=10 * 1024 * 1024, gt=1024)
    max_queue_size: int = Field(default=8, ge=1, le=256)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("source.url must start with http:// or https://")
        return v


class CropConfig(BaseModel):
    """Normalized region-of-interest crop bounds (0.0 to 1.0) and JPEG compression."""

    left: float = Field(default=0.0, ge=0.0, lt=1.0)
    top: float = Field(default=0.0, ge=0.0, lt=1.0)
    right: float = Field(default=1.0, gt=0.0, le=1.0)
    bottom: float = Field(default=1.0, gt=0.0, le=1.0)
    max_dim: int | None = Field(default=1024, ge=128, le=4096)
    jpeg_quality: int = Field(default=75, ge=10, le=100)

    @model_validator(mode="after")
    def validate_bounds(self) -> CropConfig:
        if self.left >= self.right:
            raise ValueError("crop.left must be less than crop.right")
        if self.top >= self.bottom:
            raise ValueError("crop.top must be less than crop.bottom")
        return self


class UploadCropConfig(BaseModel):
    """Upload-specific crop, padding, resize, and encoding settings."""

    mode: Literal["inherit", "fixed", "content", "full"] = "inherit"
    left: float = Field(default=0.0, ge=0.0, lt=1.0)
    top: float = Field(default=0.0, ge=0.0, lt=1.0)
    right: float = Field(default=1.0, gt=0.0, le=1.0)
    bottom: float = Field(default=1.0, gt=0.0, le=1.0)
    padding: float = Field(default=0.02, ge=0.0, le=0.25)
    max_dim: int | None = Field(default=1024, ge=128, le=4096)
    format: Literal["jpeg", "png"] = "jpeg"
    jpeg_quality: int = Field(default=75, ge=10, le=100)
    jpeg_optimize: bool = True
    fallback_to_wider: bool = True
    min_content_area_ratio: float = Field(default=0.015, gt=0.0, le=1.0)
    max_content_area_ratio: float = Field(default=0.92, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> UploadCropConfig:
        if self.left >= self.right:
            raise ValueError("upload_crop.left must be less than upload_crop.right")
        if self.top >= self.bottom:
            raise ValueError("upload_crop.top must be less than upload_crop.bottom")
        return self


class DetectionConfig(BaseModel):
    visual_change_detection: bool = True
    ocr_enabled: bool = True
    ocr_backend: Literal["rapidocr", "tesseract"] = "rapidocr"
    ocr_threads: int = Field(default=4, ge=1, le=64)
    stability_check: bool = True
    deduplication: bool = True
    phash_threshold: int = Field(default=4, ge=0, le=64)
    ssim_threshold: float = Field(default=0.985, ge=0.0, le=1.0)
    stability_frames: int = Field(default=2, ge=1, le=20)
    text_similarity_threshold: float = Field(default=92.0, ge=0.0, le=100.0)
    dedup_window_size: int = Field(default=50, ge=1, le=1000)
    min_question_confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    min_text_length: int = Field(default=8, ge=1)
    crop: CropConfig | None = None
    upload_crop: UploadCropConfig | None = None


class LLMConfig(BaseModel):
    endpoint: str
    api_key: SecretStr = Field(default=SecretStr(""))
    model: str
    system_prompt: str | None = None
    system_prompt_file: str | None = "./system_prompt.txt"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    timeout: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_base: float = Field(default=1.0, gt=0)
    max_concurrent_requests: int = Field(default=2, ge=1, le=16)
    submission_queue_size: int = Field(default=16, ge=1, le=1024)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("llm.endpoint must start with http:// or https://")
        return v

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("llm.model must not be empty")
        return v

    def resolve_system_prompt(self, base_dir: Path | None = None) -> str:
        """Return inline system_prompt or load it from system_prompt_file."""
        if self.system_prompt is not None and self.system_prompt.strip():
            return self.system_prompt.strip()
        if self.system_prompt_file:
            prompt_path = Path(self.system_prompt_file)
            if not prompt_path.is_absolute() and base_dir is not None:
                prompt_path = base_dir / prompt_path
            if prompt_path.is_file():
                content = prompt_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
                raise ValueError(f"System prompt file is empty: {prompt_path}")
            raise ValueError(f"System prompt file not found: {prompt_path}")
        raise ValueError("Either llm.system_prompt or llm.system_prompt_file must be configured")


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    bearer_token: SecretStr = Field(default=SecretStr(""))


class StorageConfig(BaseModel):
    backend: Literal["memory"] = "memory"
    max_history: int = Field(default=100, ge=1, le=10000)


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class AppConfig(BaseModel):
    source: SourceConfig
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    llm: LLMConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @model_validator(mode="after")
    def validate_separate_tokens(self) -> AppConfig:
        llm_key = self.llm.api_key.get_secret_value().strip()
        srv_tok = self.server.bearer_token.get_secret_value().strip()
        if llm_key and srv_tok and llm_key == srv_tok:
            raise ValueError(
                "server.bearer_token must be distinct from llm.api_key"
            )
        return self


def load_config(
    config_path: str | Path | None = None,
    *,
    load_dotenv: bool = True,
) -> AppConfig:
    """Load, expand, and validate application configuration from YAML."""
    if load_dotenv:
        load_dotenv_if_present()

    resolved_path = Path(
        config_path or os.environ.get("CONFIG_PATH", "configuration.yaml")
    )
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {resolved_path}")

    raw_data = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    expanded = expand_env_vars(raw_data)
    return AppConfig.model_validate(expanded)
