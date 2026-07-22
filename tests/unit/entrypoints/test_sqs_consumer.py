"""Unit tests for the SQS consumer entrypoint. Uses hand-rolled fakes
for the SQS client and the use-case, consistent with how every other
port in this codebase is tested (no mocking framework)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from summarizer.application.command import SummarizeTicketCommand
from summarizer.application.result import SummaryResult
from summarizer.config.settings import SqsSettings
from summarizer.domain.errors import ConversationUnreconstructable, EmailApiTransient
from summarizer.domain.ports import PersistedSummaryStatus, WriteMode, WriteOutcome
from summarizer.entrypoints import sqs_consumer


class _FakeUseCase:
    def __init__(
        self, result: SummaryResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.received_commands: list[SummarizeTicketCommand] = []

    def execute(self, command: SummarizeTicketCommand) -> SummaryResult:
        self.received_commands.append(command)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _FakeSqsClient:
    def __init__(self) -> None:
        self.deleted: list[dict[str, Any]] = []
        self.visibility_changes: list[dict[str, Any]] = []
        self.receive_responses: list[dict[str, Any]] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        return self.receive_responses.pop(0)

    def delete_message(self, **kwargs: Any) -> None:
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs: Any) -> None:
        self.visibility_changes.append(kwargs)


def _message(
    *,
    ticket_id: int = 1,
    email_meta_id: int = 101,
    thread_id: str = "thread-1",
    receipt_handle: str = "rh-1",
    receive_count: str = "1",
    body: str | None = None,
) -> dict[str, Any]:
    if body is None:
        body = json.dumps(
            {"ticketId": ticket_id, "emailMetaId": email_meta_id, "threadId": thread_id}
        )
    return {
        "Body": body,
        "ReceiptHandle": receipt_handle,
        "Attributes": {"ApproximateReceiveCount": receive_count},
    }


def _stub_heartbeat(monkeypatch: pytest.MonkeyPatch, stopped: list[bool] | None = None) -> None:
    def _fake_start(*args: Any, **kwargs: Any) -> Any:
        return (lambda: stopped.append(True)) if stopped is not None else (lambda: None)

    monkeypatch.setattr(sqs_consumer, "_start_visibility_heartbeat", _fake_start)


class TestParseCommand:
    def test_parses_valid_body(self) -> None:
        command = sqs_consumer.parse_command(
            json.dumps({"ticketId": 1, "emailMetaId": 101, "threadId": "thread-abc"})
        )
        assert command.ticket_id == 1
        assert command.email_meta_id == 101
        assert command.thread_id == "thread-abc"
        assert command.mode is WriteMode.APPEND_ONLY
        assert command.triggered_by == "sqs"

    def test_invalid_json_raises_malformed_message(self) -> None:
        with pytest.raises(sqs_consumer.MalformedMessage):
            sqs_consumer.parse_command("not json")

    def test_missing_field_raises_malformed_message(self) -> None:
        with pytest.raises(sqs_consumer.MalformedMessage):
            sqs_consumer.parse_command(json.dumps({"ticketId": 1, "emailMetaId": 101}))

    def test_non_numeric_ticket_id_raises_malformed_message(self) -> None:
        with pytest.raises(sqs_consumer.MalformedMessage):
            sqs_consumer.parse_command(
                json.dumps({"ticketId": "abc", "emailMetaId": 101, "threadId": "t"})
            )


class TestProcessMessage:
    def test_success_deletes_message_and_stops_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stopped: list[bool] = []
        _stub_heartbeat(monkeypatch, stopped)
        sqs = _FakeSqsClient()
        use_case = _FakeUseCase(
            result=SummaryResult(
                write_outcome=WriteOutcome.WRITTEN, status=PersistedSummaryStatus.OK
            )
        )
        message = _message()

        sqs_consumer.process_message(
            sqs, "queue-url", use_case, message, visibility_timeout=120, heartbeat_margin_seconds=30
        )

        assert len(sqs.deleted) == 1
        assert sqs.deleted[0]["ReceiptHandle"] == "rh-1"
        assert stopped == [True]
        assert len(use_case.received_commands) == 1
        assert use_case.received_commands[0].ticket_id == 1

    def test_skipped_superseded_is_treated_as_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_heartbeat(monkeypatch)
        sqs = _FakeSqsClient()
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.SKIPPED_SUPERSEDED))
        message = _message()

        sqs_consumer.process_message(
            sqs, "queue-url", use_case, message, visibility_timeout=120, heartbeat_margin_seconds=30
        )

        assert len(sqs.deleted) == 1

    def test_transient_error_does_not_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_heartbeat(monkeypatch)
        sqs = _FakeSqsClient()
        use_case = _FakeUseCase(error=EmailApiTransient("503"))
        message = _message()

        sqs_consumer.process_message(
            sqs, "queue-url", use_case, message, visibility_timeout=120, heartbeat_margin_seconds=30
        )

        assert sqs.deleted == []

    def test_terminal_error_does_not_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_heartbeat(monkeypatch)
        sqs = _FakeSqsClient()
        use_case = _FakeUseCase(error=ConversationUnreconstructable("no emails"))
        message = _message()

        sqs_consumer.process_message(
            sqs, "queue-url", use_case, message, visibility_timeout=120, heartbeat_margin_seconds=30
        )

        assert sqs.deleted == []

    def test_unexpected_exception_does_not_crash_or_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_heartbeat(monkeypatch)
        sqs = _FakeSqsClient()
        use_case = _FakeUseCase(error=RuntimeError("bug"))
        message = _message()

        # Must not raise -- one bad message can never crash the poller.
        sqs_consumer.process_message(
            sqs, "queue-url", use_case, message, visibility_timeout=120, heartbeat_margin_seconds=30
        )

        assert sqs.deleted == []

    def test_malformed_body_does_not_invoke_use_case_or_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_heartbeat(monkeypatch)
        sqs = _FakeSqsClient()
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        message = _message(body="not json")

        sqs_consumer.process_message(
            sqs, "queue-url", use_case, message, visibility_timeout=120, heartbeat_margin_seconds=30
        )

        assert sqs.deleted == []
        assert use_case.received_commands == []


class TestVisibilityHeartbeat:
    def test_stop_prevents_extension_calls(self) -> None:
        sqs = _FakeSqsClient()
        stop = sqs_consumer._start_visibility_heartbeat(
            sqs, "queue-url", "rh-1", visibility_timeout=1, margin_seconds=0
        )
        stop()
        assert sqs.visibility_changes == []


class TestPollOnce:
    def test_empty_batch_does_nothing(self) -> None:
        sqs = _FakeSqsClient()
        sqs.receive_responses.append({"Messages": []})
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        settings = SqsSettings(queue_url="q", concurrency=2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            sqs_consumer.poll_once(sqs, use_case, pool, settings)

        assert use_case.received_commands == []

    def test_dispatches_each_message_in_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_heartbeat(monkeypatch)
        sqs = _FakeSqsClient()
        sqs.receive_responses.append(
            {
                "Messages": [
                    _message(ticket_id=1, receipt_handle="rh-1"),
                    _message(ticket_id=2, receipt_handle="rh-2"),
                ]
            }
        )
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        settings = SqsSettings(queue_url="q", concurrency=2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            sqs_consumer.poll_once(sqs, use_case, pool, settings)

        assert len(use_case.received_commands) == 2
        assert len(sqs.deleted) == 2

    def test_unhandled_exception_in_process_message_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates a bug that slips past process_message's own
        # catch-all -- poll_once must still not raise.
        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("escaped bug")

        monkeypatch.setattr(sqs_consumer, "process_message", _boom)
        sqs = _FakeSqsClient()
        sqs.receive_responses.append({"Messages": [_message()]})
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        settings = SqsSettings(queue_url="q", concurrency=2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            sqs_consumer.poll_once(sqs, use_case, pool, settings)  # must not raise
