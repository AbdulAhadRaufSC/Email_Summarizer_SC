"""Input to the SummarizeTicket use-case."""

from __future__ import annotations

from dataclasses import dataclass

from summarizer.domain.ports import WriteMode


@dataclass(frozen=True, slots=True)
class SummarizeTicketCommand:
    """One SQS message's worth of work: summarize `ticket_id` as of the
    email identified by `email_meta_id` / `message_id`.

    `mode` defaults to APPEND_ONLY (live traffic, DLQ redrives). REPROCESS
    is opt-in, reserved for deliberate administrative re-runs (see
    CLAUDE.md's CAS write strategy) -- never set it from the SQS
    entrypoint's default path.
    """

    ticket_id: int
    email_meta_id: int
    message_id: str
    mode: WriteMode = WriteMode.APPEND_ONLY
    triggered_by: str | None = None
