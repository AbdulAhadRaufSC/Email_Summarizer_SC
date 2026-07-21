from __future__ import annotations

import pytest

from summarizer.application.command import SummarizeTicketCommand
from summarizer.application.result import SummaryResult
from summarizer.domain.errors import (
    ConversationUnreconstructable,
    EmailApiTransient,
    LlmOutputInvalidExhausted,
)
from summarizer.domain.ports import PersistedSummaryStatus, WriteMode, WriteOutcome
from summarizer.entrypoints import cli


class _FakeUseCase:
    def __init__(self, result: SummaryResult | None = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.received_command: SummarizeTicketCommand | None = None

    def execute(self, command: SummarizeTicketCommand) -> SummaryResult:
        self.received_command = command
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


@pytest.fixture(autouse=True)
def _stub_settings_and_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: object())
    monkeypatch.setattr(cli, "configure_logging", lambda: None)


def _run(monkeypatch: pytest.MonkeyPatch, use_case: _FakeUseCase, argv: list[str]) -> int:
    monkeypatch.setattr(cli, "build_use_case", lambda settings: use_case)
    return cli.main(argv)


BASE_ARGS = ["--ticket-id", "1", "--email-meta-id", "101", "--thread-id", "thread-101"]


class TestSuccessfulRun:
    def test_written_ok_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_case = _FakeUseCase(
            result=SummaryResult(
                write_outcome=WriteOutcome.WRITTEN, status=PersistedSummaryStatus.OK
            )
        )
        exit_code = _run(monkeypatch, use_case, BASE_ARGS)
        assert exit_code == 0

    def test_skipped_superseded_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_case = _FakeUseCase(
            result=SummaryResult(write_outcome=WriteOutcome.SKIPPED_SUPERSEDED, status=None)
        )
        exit_code = _run(monkeypatch, use_case, BASE_ARGS)
        assert exit_code == 0


class TestArgumentParsing:
    def test_builds_command_with_append_only_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        _run(monkeypatch, use_case, BASE_ARGS)

        command = use_case.received_command
        assert command is not None
        assert command.ticket_id == 1
        assert command.email_meta_id == 101
        assert command.thread_id == "thread-101"
        assert command.mode is WriteMode.APPEND_ONLY
        assert command.triggered_by == "cli"

    def test_reprocess_flag_sets_reprocess_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        _run(monkeypatch, use_case, [*BASE_ARGS, "--reprocess"])

        assert use_case.received_command is not None
        assert use_case.received_command.mode is WriteMode.REPROCESS

    def test_custom_triggered_by(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        _run(monkeypatch, use_case, [*BASE_ARGS, "--triggered-by", "manual-backfill"])

        assert use_case.received_command is not None
        assert use_case.received_command.triggered_by == "manual-backfill"

    def test_missing_required_arg_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_case = _FakeUseCase(result=SummaryResult(write_outcome=WriteOutcome.WRITTEN))
        monkeypatch.setattr(cli, "build_use_case", lambda settings: use_case)

        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--ticket-id", "1"])
        assert exc_info.value.code != 0


class TestErrorMapping:
    def test_transient_error_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_case = _FakeUseCase(error=EmailApiTransient("503"))
        exit_code = _run(monkeypatch, use_case, BASE_ARGS)
        assert exit_code == 1

    def test_terminal_error_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_case = _FakeUseCase(error=ConversationUnreconstructable("no emails"))
        exit_code = _run(monkeypatch, use_case, BASE_ARGS)
        assert exit_code == 1

    def test_llm_output_invalid_exhausted_returns_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_case = _FakeUseCase(error=LlmOutputInvalidExhausted("never validated"))
        exit_code = _run(monkeypatch, use_case, BASE_ARGS)
        assert exit_code == 1

    def test_unexpected_bug_propagates_unhandled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A real programming error must fail loudly with a traceback,
        # not be swallowed as if it were a normal pipeline outcome.
        use_case = _FakeUseCase(error=RuntimeError("bug"))
        monkeypatch.setattr(cli, "build_use_case", lambda settings: use_case)

        with pytest.raises(RuntimeError):
            cli.main(BASE_ARGS)
