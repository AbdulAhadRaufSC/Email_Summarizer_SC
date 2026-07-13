"""Application settings, loaded from environment variables / ``.env`` file.

Uses ``pydantic-settings`` so every parameter is validated at startup
and exposed as a typed attribute.  Secrets stay in ``.env`` (gitignored)
and are never logged.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_")

    host: str
    port: int = 3306
    user: str
    password: str
    name: str


class RunpodSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RUNPOD_")

    endpoint_id: str
    api_key: str


class EmailApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMAIL_API_")

    base_url: str = "https://maildata.stage.steppingdesk.com/api/emails"
    timeout_seconds: int = 30


class ExtractionSettings(BaseSettings):
    """Limits for the sandboxed attachment extractor."""

    model_config = SettingsConfigDict(env_prefix="EXTRACTION_")

    max_file_bytes: int = 10 * 1024 * 1024  # 10 MB
    timeout_seconds: int = 30
    max_decompression_ratio: int = 100
    max_xlsx_rows: int = 50_000
    max_xlsx_cells: int = 500_000


class LlmSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_")

    model_name: str = "Qwen2.5-7B-Instruct"
    model_version: str = "2.5"
    max_context_tokens: int = 16384
    max_output_tokens: int = 2048
    temperature: float = 0.0
    top_p: float = 1.0
    repetition_penalty: float = 1.05
    poll_interval_seconds: float = 1.0
    poll_max_attempts: int = 180
    request_timeout_seconds: int = 120


class PipelineSettings(BaseSettings):
    """Orchestrator-level knobs."""

    model_config = SettingsConfigDict(env_prefix="PIPELINE_")

    llm_validation_retries: int = 3
    email_fetch_concurrency: int = 5
    prompt_version: str = "v1"


class Settings(BaseSettings):
    """Top-level settings container.  Composed from domain-specific
    sub-settings, each loading from their own ``env_prefix``.

    Usage::

        from summarizer.config.settings import Settings
        settings = Settings()   # reads .env + OS env vars
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    runpod: RunpodSettings = Field(default_factory=RunpodSettings)
    email_api: EmailApiSettings = Field(default_factory=EmailApiSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
