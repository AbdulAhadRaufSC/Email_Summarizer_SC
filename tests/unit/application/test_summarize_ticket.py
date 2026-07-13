"""Unit tests for the SummarizeTicket orchestrator, using hand-rolled
fakes for all seven ports (Protocols -> trivial to fake, no mocking
framework needed).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from summarizer.application.command import SummarizeTicketCommand
from summarizer.application.summarize_ticket import SummarizeTicket
from summarizer.domain.errors import (
    ConversationUnreconstructable,
    EmailApiTransient,
    LlmOutputInvalid,
    LlmOutputInvalidExhausted,
)
from summarizer.domain.models import (
    EmailRef,
    ExtractedAttachment,
    LlmRawResponse,
    NormalizedConversation,
    NormalizedEmail,
    Prompt,
    RawAttachment,
    RawEmail,
)
from summarizer.domain.ports import PersistedSummaryStatus, SummaryWrite, WriteMode, WriteOutcome
from summarizer.domain.schema.v1 import (
    CompletenessStatus,
    ExtractionStatus,
    LlmSummaryOutput,
    TicketStatus,
)


def _ref(email_meta_id: int, message_id: str, is_note: bool = False) -> EmailRef:
    return EmailRef(email_meta_id=email_meta_id, message_id=message_id, is_note=is_note)


def _raw_email(message_id: str, attachments: list[RawAttachment] | None = None) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        subject="Cannot log in",
        from_address="cust@example.com",
        from_name="Customer",
        to_addresses=["support@example.com"],
        date="2026-07-01",
        text_body=f"Body for {message_id}",
        latest_text_body=f"Body for {message_id}",
        html_body=None,
        in_reply_to=None,
        thread_id="thread-1",
        attachments=attachments or [],
    )


def _llm_output(**overrides: object) -> LlmSummaryOutput:
    fields: dict[str, object] = {
        "human_summary": "Password reset resolved the login issue.",
        "customer_issue": "Could not log in.",
        "current_status": TicketStatus.RESOLVED,
        **overrides,
    }
    return LlmSummaryOutput(**fields)


class FakeEmailMetaRepo:
    def __init__(self, refs: list[EmailRef]):
        self._refs = refs

    def list_email_refs(self, ticket_id: int) -> list[EmailRef]:
        return self._refs


class FakeEmailGateway:
    def __init__(self, emails: dict[str, RawEmail], error: Exception | None = None):
        self._emails = emails
        self._error = error
        self.fetch_calls: list[str] = []

    def fetch_email(self, message_id: str) -> RawEmail:
        self.fetch_calls.append(message_id)
        if self._error is not None:
            raise self._error
        return self._emails[message_id]


class FakeExtractor:
    def __init__(self, status: ExtractionStatus = ExtractionStatus.EXTRACTED):
        self._status = status
        self.extract_calls: list[RawAttachment] = []

    def extract(self, attachment: RawAttachment) -> ExtractedAttachment:
        self.extract_calls.append(attachment)
        return ExtractedAttachment(
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            size=attachment.size,
            extraction_status=self._status,
            extracted_text="extracted" if self._status == ExtractionStatus.EXTRACTED else None,
            error_message="boom" if self._status == ExtractionStatus.FAILED else None,
        )


class FakeNormalizer:
    def __init__(self, conversation: NormalizedConversation | None = None):
        self._conversation = conversation
        self.normalize_calls: list[list[RawEmail]] = []

    def normalize(self, emails: list[RawEmail]) -> NormalizedConversation:
        self.normalize_calls.append(list(emails))
        if self._conversation is not None:
            return self._conversation
        return NormalizedConversation(
            subject="Cannot log in",
            emails=[
                NormalizedEmail(
                    message_id=e.message_id,
                    sender=e.from_address,
                    sender_name=e.from_name,
                    date=e.date,
                    body=e.text_body or "",
                    is_note=e.is_note,
                )
                for e in emails
            ],
        )


class FakePromptBuilder:
    prompt_version = "v1"

    def build(
        self,
        conversation: NormalizedConversation,
        attachments: list[ExtractedAttachment],
        *,
        context_budget: int,
    ) -> Prompt:
        return Prompt(
            system_message="system prompt",
            user_message="user prompt",
            json_schema={},
            prompt_version="v1",
            estimated_tokens=100,
        )


@dataclass
class FakeLLMClient:
    responses: list[LlmRawResponse] = field(default_factory=list)
    model_name: str = "Qwen2.5-7B-Instruct"
    model_version: str = "2.5"
    calls: int = 0

    def complete(self, prompt: Prompt) -> LlmRawResponse:
        self.calls += 1
        return self.responses.pop(0)


class FakeValidator:
    def __init__(self, outcomes: list[LlmSummaryOutput | Exception]):
        self._outcomes = list(outcomes)
        self.calls = 0

    def validate(self, raw: LlmRawResponse) -> LlmSummaryOutput:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSummaryRepository:
    def __init__(self, frontier: int | None = None, outcome: WriteOutcome = WriteOutcome.WRITTEN):
        self._frontier = frontier
        self._outcome = outcome
        self.upsert_calls: list[tuple[int, SummaryWrite, int, WriteMode]] = []

    def get_frontier(self, ticket_id: int) -> int | None:
        return self._frontier

    def upsert(
        self, ticket_id: int, write: SummaryWrite, marker: int, mode: WriteMode
    ) -> WriteOutcome:
        self.upsert_calls.append((ticket_id, write, marker, mode))
        return self._outcome


def _build_orchestrator(
    *,
    refs: list[EmailRef],
    emails: dict[str, RawEmail],
    extractor: FakeExtractor | None = None,
    normalizer: FakeNormalizer | None = None,
    llm_responses: list[LlmRawResponse] | None = None,
    validator_outcomes: list[LlmSummaryOutput | Exception] | None = None,
    summaries: FakeSummaryRepository | None = None,
    email_gateway_error: Exception | None = None,
    llm_validation_retries: int = 3,
) -> tuple[SummarizeTicket, dict[str, object]]:
    email_meta = FakeEmailMetaRepo(refs)
    email_api = FakeEmailGateway(emails, error=email_gateway_error)
    fake_extractor = extractor or FakeExtractor()
    fake_normalizer = normalizer or FakeNormalizer()
    llm = FakeLLMClient(responses=llm_responses or [LlmRawResponse(text="{}")])
    validator = FakeValidator(validator_outcomes or [_llm_output()])
    summary_repo = summaries or FakeSummaryRepository()

    orchestrator = SummarizeTicket(
        email_meta=email_meta,
        email_api=email_api,
        extractor=fake_extractor,
        normalizer=fake_normalizer,
        prompt_builder=FakePromptBuilder(),
        llm=llm,
        validator=validator,
        summaries=summary_repo,
        context_budget=16384,
        llm_validation_retries=llm_validation_retries,
    )
    fakes = {
        "email_meta": email_meta,
        "email_api": email_api,
        "extractor": fake_extractor,
        "normalizer": fake_normalizer,
        "llm": llm,
        "validator": validator,
        "summaries": summary_repo,
    }
    return orchestrator, fakes


class TestHappyPath:
    def test_fresh_ticket_writes_ok_status(self) -> None:
        refs = [_ref(101, "msg-101"), _ref(102, "msg-102")]
        emails = {"msg-101": _raw_email("msg-101"), "msg-102": _raw_email("msg-102")}
        orchestrator, fakes = _build_orchestrator(refs=refs, emails=emails)

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=102, message_id="msg-102")
        )

        assert result.write_outcome is WriteOutcome.WRITTEN
        assert result.status is PersistedSummaryStatus.OK

        summaries: FakeSummaryRepository = fakes["summaries"]  # type: ignore[assignment]
        assert len(summaries.upsert_calls) == 1
        ticket_id, write, marker, mode = summaries.upsert_calls[0]
        assert ticket_id == 1
        assert marker == 102
        assert mode is WriteMode.APPEND_ONLY
        assert write.status is PersistedSummaryStatus.OK
        assert write.latest_message_id == "msg-102"
        assert write.model_name == "Qwen2.5-7B-Instruct"
        assert write.prompt_version == "v1"

    def test_reuses_ryw_fetch_instead_of_fetching_triggering_email_twice(self) -> None:
        refs = [_ref(101, "msg-101"), _ref(102, "msg-102")]
        emails = {"msg-101": _raw_email("msg-101"), "msg-102": _raw_email("msg-102")}
        orchestrator, fakes = _build_orchestrator(refs=refs, emails=emails)

        orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=102, message_id="msg-102")
        )

        email_api: FakeEmailGateway = fakes["email_api"]  # type: ignore[assignment]
        assert email_api.fetch_calls.count("msg-102") == 1
        assert email_api.fetch_calls.count("msg-101") == 1

    def test_correlates_is_note_from_ref_onto_raw_email(self) -> None:
        refs = [_ref(101, "msg-101", is_note=True)]
        emails = {"msg-101": _raw_email("msg-101")}
        normalizer = FakeNormalizer()
        orchestrator, fakes = _build_orchestrator(refs=refs, emails=emails, normalizer=normalizer)

        orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        seen = normalizer.normalize_calls[0]
        assert seen[0].is_note is True


class TestFrontierCheck:
    def test_stale_marker_under_append_only_skips_without_touching_other_ports(self) -> None:
        summaries = FakeSummaryRepository(frontier=150)
        orchestrator, fakes = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            summaries=summaries,
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=100, message_id="msg-101")
        )

        assert result.write_outcome is WriteOutcome.SKIPPED_SUPERSEDED
        assert result.status is None
        email_api: FakeEmailGateway = fakes["email_api"]  # type: ignore[assignment]
        assert email_api.fetch_calls == []
        assert summaries.upsert_calls == []

    def test_newer_marker_under_append_only_proceeds(self) -> None:
        summaries = FakeSummaryRepository(frontier=50)
        orchestrator, _ = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            summaries=summaries,
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        assert result.write_outcome is WriteOutcome.WRITTEN

    def test_reprocess_mode_ignores_stale_marker(self) -> None:
        summaries = FakeSummaryRepository(frontier=150)
        orchestrator, _ = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            summaries=summaries,
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(
                ticket_id=1, email_meta_id=100, message_id="msg-101", mode=WriteMode.REPROCESS
            )
        )

        assert result.write_outcome is WriteOutcome.WRITTEN
        _, _, marker, mode = summaries.upsert_calls[0]
        assert marker == 100
        assert mode is WriteMode.REPROCESS

    def test_write_outcome_from_repository_is_passed_through(self) -> None:
        # A race where the repository itself decides the write is
        # superseded (e.g. a concurrent newer write landed first).
        summaries = FakeSummaryRepository(frontier=None, outcome=WriteOutcome.SKIPPED_SUPERSEDED)
        orchestrator, _ = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            summaries=summaries,
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        assert result.write_outcome is WriteOutcome.SKIPPED_SUPERSEDED
        assert result.status is None


class TestTerminalFailures:
    def test_no_email_refs_raises_conversation_unreconstructable(self) -> None:
        orchestrator, _ = _build_orchestrator(refs=[], emails={})

        with pytest.raises(ConversationUnreconstructable):
            orchestrator.execute(
                SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
            )

    def test_normalizer_producing_zero_emails_raises_conversation_unreconstructable(self) -> None:
        empty_conversation = NormalizedConversation(subject="x", emails=[])
        orchestrator, _ = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            normalizer=FakeNormalizer(conversation=empty_conversation),
        )

        with pytest.raises(ConversationUnreconstructable):
            orchestrator.execute(
                SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
            )

    def test_llm_output_invalid_exhausted_raises_after_retry_budget(self) -> None:
        orchestrator, _ = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            llm_responses=[LlmRawResponse(text="bad")] * 4,
            validator_outcomes=[LlmOutputInvalid("nope")] * 4,
            llm_validation_retries=3,
        )

        with pytest.raises(LlmOutputInvalidExhausted):
            orchestrator.execute(
                SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
            )


class TestTransientPropagation:
    def test_email_api_transient_propagates_unhandled(self) -> None:
        orchestrator, _ = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            email_gateway_error=EmailApiTransient("503"),
        )

        with pytest.raises(EmailApiTransient):
            orchestrator.execute(
                SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
            )


class TestLlmRetryLoop:
    def test_succeeds_after_one_retry_and_reports_retry_count(self) -> None:
        orchestrator, fakes = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            llm_responses=[
                LlmRawResponse(text="bad", token_input=100, token_output=10),
                LlmRawResponse(text="good", token_input=120, token_output=30),
            ],
            validator_outcomes=[LlmOutputInvalid("malformed"), _llm_output()],
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        assert result.retry_count == 1
        assert result.token_input == 220
        assert result.token_output == 40

    def test_no_retry_needed_reports_zero_retry_count(self) -> None:
        orchestrator, _ = _build_orchestrator(
            refs=[_ref(101, "msg-101")],
            emails={"msg-101": _raw_email("msg-101")},
            llm_responses=[LlmRawResponse(text="good", token_input=100, token_output=20)],
            validator_outcomes=[_llm_output()],
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        assert result.retry_count == 0
        assert result.token_input == 100
        assert result.token_output == 20


class TestAttachmentCompleteness:
    def test_all_attachments_extracted_yields_ok_status(self) -> None:
        attachment = RawAttachment(
            filename="log.txt", mime_type="text/plain", size=10, attachment_id="a1"
        )
        refs = [_ref(101, "msg-101")]
        emails = {"msg-101": _raw_email("msg-101", attachments=[attachment])}
        orchestrator, _ = _build_orchestrator(
            refs=refs, emails=emails, extractor=FakeExtractor(status=ExtractionStatus.EXTRACTED)
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        assert result.status is PersistedSummaryStatus.OK

    def test_failed_attachment_extraction_yields_partial_status(self) -> None:
        attachment = RawAttachment(
            filename="corrupt.pdf", mime_type="application/pdf", size=10, attachment_id="a1"
        )
        refs = [_ref(101, "msg-101")]
        emails = {"msg-101": _raw_email("msg-101", attachments=[attachment])}
        orchestrator, fakes = _build_orchestrator(
            refs=refs, emails=emails, extractor=FakeExtractor(status=ExtractionStatus.FAILED)
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        assert result.status is PersistedSummaryStatus.PARTIAL
        summaries: FakeSummaryRepository = fakes["summaries"]  # type: ignore[assignment]
        write = summaries.upsert_calls[0][1]
        assert write.document.context_completeness.status is CompletenessStatus.PARTIAL
        assert "corrupt.pdf" in write.document.context_completeness.missing[0]

    def test_metadata_only_attachment_is_not_treated_as_missing(self) -> None:
        attachment = RawAttachment(
            filename="photo.png", mime_type="image/png", size=10, attachment_id="a1"
        )
        refs = [_ref(101, "msg-101")]
        emails = {"msg-101": _raw_email("msg-101", attachments=[attachment])}
        orchestrator, _ = _build_orchestrator(
            refs=refs,
            emails=emails,
            extractor=FakeExtractor(status=ExtractionStatus.METADATA_ONLY),
        )

        result = orchestrator.execute(
            SummarizeTicketCommand(ticket_id=1, email_meta_id=101, message_id="msg-101")
        )

        assert result.status is PersistedSummaryStatus.OK
