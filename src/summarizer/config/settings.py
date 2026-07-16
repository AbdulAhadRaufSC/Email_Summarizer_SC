"""Application settings, loaded from environment variables / ``.env`` file.

Uses ``pydantic-settings`` so every parameter is validated at startup
and exposed as a typed attribute.  Secrets stay in ``.env`` (gitignored)
and are never logged.

``load_dotenv()`` runs at import time rather than relying on each
``BaseSettings`` subclass's own ``env_file`` config: pydantic-settings'
``.env`` loading is per-class, not inherited through a nested
``Field(default_factory=...)`` -- ``Settings`` (the top-level class) has
``env_file=".env"``, but ``DatabaseSettings``, ``RunpodSettings``, etc.
are each independently instantiated by their own default factory and
would otherwise only see real process environment variables, never the
``.env`` file. Loading the file into ``os.environ`` once, up front,
makes every sub-settings class see the same values uniformly.
"""

from __future__ import annotations

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


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

    base_url: str = "https://maildata.stage.steppingdesk.com/api/getMailBody"
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

    # Must exactly match the model id GET /openai/v1/models reports for
    # this endpoint -- confirmed live 2026-07-13 to be lowercase
    # ("qwen/qwen2.5-7b-instruct"), NOT the HF repo casing
    # ("Qwen/Qwen2.5-7B-Instruct"). A mismatched case was silently
    # returning a generic 500 rather than a clean "model not found",
    # which is why this needed a live call to catch.
    model_name: str = "qwen/qwen2.5-7b-instruct"
    model_version: str = "2.5"
    # Confirmed live via GET /openai/v1/models ("max_model_len": 32768)
    # 2026-07-13 -- supersedes the earlier unverified value of 16384.
    max_context_tokens: int = 32768
    max_output_tokens: int = 2048
    temperature: float = 0.0
    top_p: float = 1.0
    repetition_penalty: float = 1.05
    # Generous: RunPod's OpenAI-compatible route blocks synchronously
    # until the job completes, including any cold-start time.
    request_timeout_seconds: int = 300


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
