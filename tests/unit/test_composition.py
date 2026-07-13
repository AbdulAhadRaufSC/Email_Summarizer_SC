from __future__ import annotations

import pytest

from summarizer.adapters.email.http_email_gateway import HttpEmailGateway
from summarizer.adapters.email.mysql_email_metadata import MySqlEmailMetadataRepository
from summarizer.adapters.extraction.extractor import SandboxedExtractor
from summarizer.adapters.llm.runpod_vllm_client import RunpodVllmClient
from summarizer.adapters.normalize.normalizer import DefaultThreadNormalizer
from summarizer.adapters.persistence.mysql_summary_repository import MySqlSummaryRepository
from summarizer.adapters.prompt.prompt_builder import TemplatePromptBuilder
from summarizer.adapters.validation.pydantic_validator import PydanticValidator
from summarizer.application.summarize_ticket import SummarizeTicket
from summarizer.composition import build_use_case
from summarizer.config.settings import Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_USER", "svc")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("DB_NAME", "TrackEaseV2DB")
    monkeypatch.setenv("RUNPOD_ENDPOINT_ID", "ep-123")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-secret")
    return Settings(_env_file=None)


class TestBuildUseCase:
    def test_returns_summarize_ticket_instance(self, settings: Settings) -> None:
        use_case = build_use_case(settings)
        assert isinstance(use_case, SummarizeTicket)

    def test_wires_concrete_adapter_types(self, settings: Settings) -> None:
        use_case = build_use_case(settings)

        assert isinstance(use_case._email_meta, MySqlEmailMetadataRepository)
        assert isinstance(use_case._email_api, HttpEmailGateway)
        assert isinstance(use_case._extractor, SandboxedExtractor)
        assert isinstance(use_case._normalizer, DefaultThreadNormalizer)
        assert isinstance(use_case._prompt_builder, TemplatePromptBuilder)
        assert isinstance(use_case._llm, RunpodVllmClient)
        assert isinstance(use_case._validator, PydanticValidator)
        assert isinstance(use_case._summaries, MySqlSummaryRepository)

    def test_propagates_pipeline_limits_from_settings(self, settings: Settings) -> None:
        use_case = build_use_case(settings)

        assert use_case._context_budget == settings.llm.max_context_tokens
        assert use_case._email_fetch_concurrency == settings.pipeline.email_fetch_concurrency
        assert use_case._llm_validation_retries == settings.pipeline.llm_validation_retries

    def test_llm_client_carries_model_identity_from_settings(self, settings: Settings) -> None:
        use_case = build_use_case(settings)

        assert use_case._llm.model_name == settings.llm.model_name
        assert use_case._llm.model_version == settings.llm.model_version

    def test_prompt_builder_carries_configured_version(self, settings: Settings) -> None:
        use_case = build_use_case(settings)
        assert use_case._prompt_builder.prompt_version == settings.pipeline.prompt_version
