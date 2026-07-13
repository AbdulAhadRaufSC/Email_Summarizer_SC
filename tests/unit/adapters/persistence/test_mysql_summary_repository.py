"""Unit tests for the parts of MySqlSummaryRepository that don't need a
live database: the pure CAS decision function, parameter construction,
and transient-error classification.

Transaction/locking behavior (SELECT ... FOR UPDATE, the insert-race
retry, rollback-on-error) requires a real MySQL engine and is covered by
tests/integration/persistence/test_mysql_summary_repository_integration.py
instead of mocked here -- a mocked cursor/connection would only prove the
mock behaves as programmed, not that the SQL is correct.
"""

import json

import pymysql.err
import pytest

from summarizer.adapters.persistence.mysql_summary_repository import (
    MySqlSummaryRepository,
    decide_write,
)
from summarizer.domain.ports import (
    PersistedSummaryStatus,
    SummaryWrite,
    WriteMode,
    WriteOutcome,
)
from summarizer.domain.schema.v1 import (
    CompletenessStatus,
    ContextCompleteness,
    LlmSummaryOutput,
    SourceInfo,
    SummaryDocument,
    TicketStatus,
)


def _document() -> SummaryDocument:
    return SummaryDocument(
        content=LlmSummaryOutput(
            human_summary="Password reset issue resolved.",
            customer_issue="Could not log in after reset.",
            current_status=TicketStatus.RESOLVED,
        ),
        context_completeness=ContextCompleteness(status=CompletenessStatus.COMPLETE),
        source=SourceInfo(email_count=4, frontier_email_meta_id=150),
    )


def _write(**overrides: object) -> SummaryWrite:
    fields: dict[str, object] = {
        "document": _document(),
        "latest_message_id": "msg-150",
        "model_name": "Qwen2.5-7B-Instruct",
        "model_version": "2.5",
        "prompt_version": "v1",
        "status": PersistedSummaryStatus.OK,
        **overrides,
    }
    return SummaryWrite(**fields)  # type: ignore[arg-type]


class TestDecideWriteAppendOnly:
    def test_fresh_ticket_writes(self) -> None:
        decision = decide_write(stored_marker=None, incoming_marker=100, mode=WriteMode.APPEND_ONLY)
        assert decision.outcome is WriteOutcome.WRITTEN
        assert decision.new_marker == 100

    def test_newer_marker_writes(self) -> None:
        decision = decide_write(stored_marker=100, incoming_marker=150, mode=WriteMode.APPEND_ONLY)
        assert decision.outcome is WriteOutcome.WRITTEN
        assert decision.new_marker == 150

    def test_stale_marker_is_skipped(self) -> None:
        """A DLQ redrive for an email the ticket has since moved past
        must not overwrite the newer summary -- this is the exact
        scenario CLAUDE.md's CAS write strategy calls out."""
        decision = decide_write(stored_marker=150, incoming_marker=100, mode=WriteMode.APPEND_ONLY)
        assert decision.outcome is WriteOutcome.SKIPPED_SUPERSEDED
        assert decision.new_marker is None

    def test_equal_marker_is_skipped(self) -> None:
        """Redelivery of the same event (at-least-once SQS) must be a
        no-op, not a duplicate write."""
        decision = decide_write(stored_marker=150, incoming_marker=150, mode=WriteMode.APPEND_ONLY)
        assert decision.outcome is WriteOutcome.SKIPPED_SUPERSEDED


class TestDecideWriteReprocess:
    def test_fresh_ticket_writes(self) -> None:
        decision = decide_write(stored_marker=None, incoming_marker=100, mode=WriteMode.REPROCESS)
        assert decision.outcome is WriteOutcome.WRITTEN
        assert decision.new_marker == 100

    def test_frontier_advances_when_incoming_is_newer(self) -> None:
        decision = decide_write(stored_marker=100, incoming_marker=150, mode=WriteMode.REPROCESS)
        assert decision.new_marker == 150

    def test_frontier_never_regresses_when_incoming_is_older(self) -> None:
        """The administrative reprocess case: re-running the current
        frontier under a new prompt/model version. The frontier marker
        must stay at the stored value (GREATEST semantics) even though
        this write's content is applied unconditionally."""
        decision = decide_write(stored_marker=150, incoming_marker=100, mode=WriteMode.REPROCESS)
        assert decision.outcome is WriteOutcome.WRITTEN
        assert decision.new_marker == 150

    def test_same_frontier_reprocess_writes(self) -> None:
        decision = decide_write(stored_marker=150, incoming_marker=150, mode=WriteMode.REPROCESS)
        assert decision.outcome is WriteOutcome.WRITTEN
        assert decision.new_marker == 150


class TestReprocessIsUnsafeForRedrive:
    """Documents *why* DLQ redrive must call decide_write with
    APPEND_ONLY and never REPROCESS, per CLAUDE.md's DLQ redrive note.

    Scenario: email 140 lands in the DLQ. Live traffic advances the
    ticket to a good summary based on email 150. The redrive of 140
    fires later.
    """

    def test_append_only_redrive_correctly_skips_the_superseded_message(self) -> None:
        decision = decide_write(stored_marker=150, incoming_marker=140, mode=WriteMode.APPEND_ONLY)
        assert decision.outcome is WriteOutcome.SKIPPED_SUPERSEDED

    def test_reprocess_would_incorrectly_overwrite_fresh_content_with_stale_content(
        self,
    ) -> None:
        """If a redrive were (mis-)routed through REPROCESS instead, the
        frontier marker is preserved at 150 (GREATEST holds), but the
        outcome is still WRITTEN -- meaning stale, 140-based content
        would silently clobber the correct 150-based summary while the
        marker misleadingly still reads 150. decide_write cannot protect
        against this by itself: the safety property depends entirely on
        the caller choosing APPEND_ONLY for redrives, never REPROCESS.
        """
        decision = decide_write(stored_marker=150, incoming_marker=140, mode=WriteMode.REPROCESS)
        assert decision.outcome is WriteOutcome.WRITTEN
        assert decision.new_marker == 150


class TestParams:
    def test_serializes_document_and_denormalizes_human_summary(self) -> None:
        write = _write()
        params = MySqlSummaryRepository._params(ticket_id=42, write=write, marker=150)

        assert params["ticket_id"] == 42
        assert params["marker"] == 150
        assert params["summary"] == write.document.content.human_summary
        assert json.loads(params["summary_json"]) == json.loads(write.document.model_dump_json())
        assert params["status"] == "OK"
        assert isinstance(params["status"], str)

    def test_marker_none_is_passed_through(self) -> None:
        """upsert() never actually calls _params with marker=None (that
        path returns SKIPPED_SUPERSEDED before reaching it), but the
        helper itself should not silently coerce None -- a caller bug
        that got this far should surface as a NULL-column DB error, not
        a wrong-but-plausible value."""
        params = MySqlSummaryRepository._params(ticket_id=1, write=_write(), marker=None)
        assert params["marker"] is None

    def test_optional_fields_pass_through_as_none(self) -> None:
        write = _write(triggered_by=None, processing_time_ms=None, error_message=None)
        params = MySqlSummaryRepository._params(ticket_id=1, write=write, marker=1)
        assert params["triggered_by"] is None
        assert params["processing_time_ms"] is None
        assert params["error_message"] is None


class TestWrapIfTransient:
    @pytest.mark.parametrize("code", [1205, 1213, 2006, 2013])
    def test_known_transient_codes_are_wrapped(self, code: int) -> None:
        exc = pymysql.err.OperationalError(code, "boom")
        wrapped = MySqlSummaryRepository._wrap_if_transient(exc)
        assert wrapped is not None
        assert str(exc) in str(wrapped)

    @pytest.mark.parametrize("code", [1062, 1146, 9999])
    def test_other_codes_are_not_wrapped(self, code: int) -> None:
        exc = pymysql.err.OperationalError(code, "not transient")
        assert MySqlSummaryRepository._wrap_if_transient(exc) is None
