"""Output of the SummarizeTicket use-case."""

from __future__ import annotations

from dataclasses import dataclass

from summarizer.domain.ports import PersistedSummaryStatus, WriteOutcome


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """What happened when running one `SummarizeTicketCommand`.

    `status` is None when `write_outcome` is SKIPPED_SUPERSEDED -- no
    write happened, so there is no OK/PARTIAL status to report. Callers
    (the SQS entrypoint, the CLI) treat SKIPPED_SUPERSEDED the same as
    a successful no-op: ack the message.
    """

    write_outcome: WriteOutcome
    status: PersistedSummaryStatus | None = None
    processing_time_ms: int = 0
    token_input: int | None = None
    token_output: int | None = None
    retry_count: int = 0
