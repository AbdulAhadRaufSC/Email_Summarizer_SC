"""SummarizeTicket -- the pipeline orchestrator.

Wires the seven ports together in the exact order CLAUDE.md's "Event
flow (happy path)" specifies: frontier check -> enumerate refs -> RYW
gate -> fetch emails -> extract attachments -> normalize -> build
prompt -> LLM call -> validate (with app-level retry) -> enrich ->
CAS upsert.

Error-handling philosophy (see domain/errors.py): TransientError
subclasses raised by any port (EmailApiTransient, EmailNotYetAvailable,
LlmTransient, SummaryPersistenceTransient) are intentionally left
unhandled here -- they propagate to the entrypoint, which leaves the
SQS message unacked for redelivery. Only the two truly terminal
conditions this orchestrator itself identifies --  a ticket with no
reconstructable thread, and LLM output that never validates -- are
raised explicitly.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from summarizer.domain.errors import (
    ConversationUnreconstructable,
    LlmOutputInvalid,
    LlmOutputInvalidExhausted,
)
from summarizer.domain.models import EmailRef, ExtractedAttachment, Prompt, RawEmail
from summarizer.domain.ports import (
    AttachmentExtractor,
    EmailGateway,
    EmailMetadataRepository,
    LLMClient,
    PersistedSummaryStatus,
    PromptBuilder,
    SummaryRepository,
    SummaryWrite,
    ThreadNormalizer,
    Validator,
    WriteMode,
    WriteOutcome,
)
from summarizer.domain.schema.v1 import (
    AttachmentRef,
    CompletenessStatus,
    ContextCompleteness,
    ExtractionStatus,
    LlmSummaryOutput,
    SourceInfo,
    SummaryDocument,
)

from .command import SummarizeTicketCommand
from .result import SummaryResult

logger = logging.getLogger(__name__)


class SummarizeTicket:
    def __init__(
        self,
        email_meta: EmailMetadataRepository,
        email_api: EmailGateway,
        extractor: AttachmentExtractor,
        normalizer: ThreadNormalizer,
        prompt_builder: PromptBuilder,
        llm: LLMClient,
        validator: Validator,
        summaries: SummaryRepository,
        *,
        context_budget: int,
        email_fetch_concurrency: int = 5,
        llm_validation_retries: int = 3,
    ) -> None:
        self._email_meta = email_meta
        self._email_api = email_api
        self._extractor = extractor
        self._normalizer = normalizer
        self._prompt_builder = prompt_builder
        self._llm = llm
        self._validator = validator
        self._summaries = summaries
        self._context_budget = context_budget
        self._email_fetch_concurrency = email_fetch_concurrency
        self._llm_validation_retries = llm_validation_retries

    def execute(self, command: SummarizeTicketCommand) -> SummaryResult:
        start = time.monotonic()

        stored_frontier = self._summaries.get_frontier(command.ticket_id)
        if (
            command.mode is WriteMode.APPEND_ONLY
            and stored_frontier is not None
            and command.email_meta_id <= stored_frontier
        ):
            logger.info(
                "Skipping superseded event",
                extra={"ticket_id": command.ticket_id, "email_meta_id": command.email_meta_id},
            )
            return SummaryResult(write_outcome=WriteOutcome.SKIPPED_SUPERSEDED)

        refs = self._email_meta.list_email_refs(command.ticket_id)
        if not refs:
            raise ConversationUnreconstructable(
                f"No emails found for ticket {command.ticket_id}"
            )

        triggering_ref = next(
            (r for r in refs if r.email_meta_id == command.email_meta_id), None
        )
        triggering_thread_id = triggering_ref.thread_id if triggering_ref else None

        # Read-your-writes gate: the triggering email must be fetchable
        # before we summarize a thread that might not include it yet.
        triggering_email = self._email_api.fetch_email(
            ticket_id=command.ticket_id,
            email_meta_id=command.email_meta_id,
            message_id=command.message_id,
            thread_id=triggering_thread_id,
        )

        raw_emails = self._fetch_all(command.ticket_id, refs, triggering_email)
        extracted_attachments = self._extract_attachments(raw_emails)

        conversation = self._normalizer.normalize(raw_emails)
        if not conversation.emails:
            raise ConversationUnreconstructable(
                f"Thread for ticket {command.ticket_id} normalized to zero usable emails"
            )

        prompt = self._prompt_builder.build(
            conversation, extracted_attachments, context_budget=self._context_budget
        )

        llm_output, retry_count, token_input, token_output = self._complete_with_retries(prompt)

        completeness = self._build_completeness(extracted_attachments)
        document = SummaryDocument(
            content=llm_output,
            attachments=[self._to_attachment_ref(a) for a in extracted_attachments],
            context_completeness=completeness,
            source=SourceInfo(
                email_count=len(raw_emails), frontier_email_meta_id=command.email_meta_id
            ),
        )
        persisted_status = (
            PersistedSummaryStatus.OK
            if completeness.status is CompletenessStatus.COMPLETE
            else PersistedSummaryStatus.PARTIAL
        )
        processing_time_ms = int((time.monotonic() - start) * 1000)

        write = SummaryWrite(
            document=document,
            latest_message_id=command.message_id,
            model_name=self._llm.model_name,
            model_version=self._llm.model_version,
            prompt_version=self._prompt_builder.prompt_version,
            status=persisted_status,
            triggered_by=command.triggered_by,
            processing_time_ms=processing_time_ms,
            token_input=token_input,
            token_output=token_output,
            retry_count=retry_count,
        )

        outcome = self._summaries.upsert(
            command.ticket_id, write, command.email_meta_id, command.mode
        )

        return SummaryResult(
            write_outcome=outcome,
            status=persisted_status if outcome is WriteOutcome.WRITTEN else None,
            processing_time_ms=processing_time_ms,
            token_input=token_input,
            token_output=token_output,
            retry_count=retry_count,
        )

    def _fetch_all(
        self, ticket_id: int, refs: Sequence[EmailRef], triggering_email: RawEmail
    ) -> list[RawEmail]:
        """Fetch every referenced email, reusing the RYW-gate fetch for
        the triggering message instead of fetching it twice."""
        cache = {triggering_email.message_id: triggering_email}

        def fetch_one(ref: EmailRef) -> RawEmail:
            raw = cache.get(ref.message_id) or self._email_api.fetch_email(
                ticket_id=ticket_id,
                email_meta_id=ref.email_meta_id,
                message_id=ref.message_id,
                thread_id=ref.thread_id,
            )
            return dataclasses.replace(raw, is_note=ref.is_note)

        with ThreadPoolExecutor(max_workers=self._email_fetch_concurrency) as pool:
            return list(pool.map(fetch_one, refs))

    def _extract_attachments(self, raw_emails: Sequence[RawEmail]) -> list[ExtractedAttachment]:
        return [
            self._extractor.extract(attachment)
            for email in raw_emails
            for attachment in email.attachments
        ]

    def _complete_with_retries(
        self, prompt: Prompt
    ) -> tuple[LlmSummaryOutput, int, int | None, int | None]:
        total_input = 0
        total_output = 0
        retry_count = 0
        last_error: LlmOutputInvalid | None = None

        for _attempt in range(self._llm_validation_retries + 1):
            raw = self._llm.complete(prompt)
            total_input += raw.token_input or 0
            total_output += raw.token_output or 0
            try:
                output = self._validator.validate(raw)
                return output, retry_count, total_input or None, total_output or None
            except LlmOutputInvalid as exc:
                last_error = exc
                retry_count += 1
                logger.warning("LLM output failed validation, retrying: %s", exc)

        raise LlmOutputInvalidExhausted(
            f"LLM output never validated after {self._llm_validation_retries} retries: {last_error}"
        ) from last_error

    @staticmethod
    def _build_completeness(extracted: Sequence[ExtractedAttachment]) -> ContextCompleteness:
        missing = [
            f"attachment {a.filename!r} extraction failed: {a.error_message}"
            for a in extracted
            if a.extraction_status is ExtractionStatus.FAILED
        ]
        status = CompletenessStatus.PARTIAL if missing else CompletenessStatus.COMPLETE
        return ContextCompleteness(status=status, missing=missing)

    @staticmethod
    def _to_attachment_ref(attachment: ExtractedAttachment) -> AttachmentRef:
        return AttachmentRef(
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            extraction_status=attachment.extraction_status,
        )
