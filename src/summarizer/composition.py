"""Composition root -- the single place concrete adapters are named and
wired into the `SummarizeTicket` use-case. Nothing outside this module
(and tests, which substitute fakes for the Protocol ports directly)
should import a concrete adapter class.
"""

from __future__ import annotations

from collections.abc import Callable

import pymysql
from pymysql.connections import Connection

from summarizer.adapters.email.http_email_gateway import HttpEmailGateway
from summarizer.adapters.email.mysql_email_metadata import MySqlEmailMetadataRepository
from summarizer.adapters.extraction.extractor import SandboxedExtractor
from summarizer.adapters.llm.runpod_vllm_client import RunpodVllmClient
from summarizer.adapters.normalize.normalizer import DefaultThreadNormalizer
from summarizer.adapters.persistence.mysql_summary_repository import MySqlSummaryRepository
from summarizer.adapters.prompt.prompt_builder import TemplatePromptBuilder
from summarizer.adapters.validation.pydantic_validator import PydanticValidator
from summarizer.application.summarize_ticket import SummarizeTicket
from summarizer.config.settings import Settings


def _build_connection_factory(settings: Settings) -> Callable[[], Connection]:
    def _connect() -> Connection:
        return pymysql.connect(
            host=settings.db.host,
            port=settings.db.port,
            user=settings.db.user,
            password=settings.db.password,
            database=settings.db.name,
            autocommit=False,
        )

    return _connect


def build_use_case(settings: Settings) -> SummarizeTicket:
    # One factory shared by both MySQL adapters -- each still opens its
    # own connection per call (see MySqlSummaryRepository's docstring);
    # sharing the factory just avoids repeating the connection params.
    connection_factory = _build_connection_factory(settings)

    return SummarizeTicket(
        email_meta=MySqlEmailMetadataRepository(connection_factory),
        email_api=HttpEmailGateway(
            base_url=settings.email_api.base_url,
            timeout_seconds=settings.email_api.timeout_seconds,
        ),
        extractor=SandboxedExtractor(
            max_file_bytes=settings.extraction.max_file_bytes,
            timeout_seconds=settings.extraction.timeout_seconds,
            max_decompression_ratio=settings.extraction.max_decompression_ratio,
            max_xlsx_rows=settings.extraction.max_xlsx_rows,
            max_xlsx_cells=settings.extraction.max_xlsx_cells,
        ),
        normalizer=DefaultThreadNormalizer(),
        prompt_builder=TemplatePromptBuilder(prompt_version=settings.pipeline.prompt_version),
        llm=RunpodVllmClient(
            endpoint_id=settings.runpod.endpoint_id,
            api_key=settings.runpod.api_key,
            model_name=settings.llm.model_name,
            model_version=settings.llm.model_version,
            max_output_tokens=settings.llm.max_output_tokens,
            temperature=settings.llm.temperature,
            top_p=settings.llm.top_p,
            repetition_penalty=settings.llm.repetition_penalty,
            poll_interval_seconds=settings.llm.poll_interval_seconds,
            poll_max_attempts=settings.llm.poll_max_attempts,
            request_timeout_seconds=settings.llm.request_timeout_seconds,
        ),
        validator=PydanticValidator(),
        summaries=MySqlSummaryRepository(connection_factory),
        context_budget=settings.llm.max_context_tokens,
        email_fetch_concurrency=settings.pipeline.email_fetch_concurrency,
        llm_validation_retries=settings.pipeline.llm_validation_retries,
    )
