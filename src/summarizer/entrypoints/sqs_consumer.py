"""SQS entrypoint: long-running consumer for the live ticket-summary
queue. Thin wrapper around the same `SummarizeTicket` orchestrator the
CLI (`entrypoints/cli.py`) drives one ticket at a time -- see CLAUDE.md's
"Event flow" and "Queueing" sections. This file does not change when
the backfill entrypoint is added; they share the orchestrator, not this
polling loop.

Confirmed message shape (2026-07-21), on `steppingcloud-stage-ticket-ai-
summary-queue`: ``{"ticketId": ..., "emailMetaId": ..., "threadId": ...}``
-- the same three identifiers the CLI takes, since the getMailBody
contract locates an email by threadId, not messageId (see CLAUDE.md's
"Existing infrastructure").

DLQ handling: the queue already has a redrive policy configured
(maxReceiveCount=5), so this module never manages the DLQ directly --
on any failure (transient or terminal) it simply leaves the message
unacked and lets SQS's own redrive policy route it to the DLQ once
receives are exhausted. This matches CLAUDE.md's "DLQ on exhausted
retries" framing and keeps this entrypoint from needing to know a DLQ
queue URL at all.

Visibility heartbeat: modeled on the extend-every-(timeout-margin)s
pattern in this repo's `SQS_Pipeline_context.js` reference (a different
team's Node.js SQS worker) -- a background thread re-extends the
message's visibility timeout while a job is still running, so a slow
RunPod cold start can't outlive a fixed timeout and get redelivered
mid-flight. CAS makes a duplicate delivery safe regardless (just
wasteful), so this is a waste-avoidance measure, not a correctness one.

Mode: always `WriteMode.APPEND_ONLY`. `REPROCESS` is administrative-only
(see CLAUDE.md's CAS write strategy) and must never be set from this
path -- there is deliberately no code path here that could set it.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import FrameType
from typing import Any, Protocol

import boto3  # type: ignore[import-untyped]

from summarizer.application.command import SummarizeTicketCommand
from summarizer.application.summarize_ticket import SummarizeTicket
from summarizer.composition import build_use_case
from summarizer.config.logging_config import configure_logging
from summarizer.config.settings import Settings, SqsSettings
from summarizer.domain.errors import SummarizerError, TerminalError, TransientError
from summarizer.domain.ports import WriteMode

logger = logging.getLogger(__name__)

TRIGGERED_BY = "sqs"


class SqsClient(Protocol):
    """The subset of boto3's SQS client this module needs -- narrowed to
    a Protocol so tests substitute a hand-rolled fake instead of mocking
    boto3 (consistent with how every other port in this codebase is
    tested; see application/summarize_ticket.py's test fakes)."""

    def receive_message(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_message(self, **kwargs: Any) -> Any: ...
    def change_message_visibility(self, **kwargs: Any) -> Any: ...


class MalformedMessage(Exception):
    """The message body isn't valid JSON or is missing a required field.
    Never retried specially -- just left unacked like any other failure,
    so it ages out to the DLQ via the queue's own redrive policy."""


def parse_command(body: str) -> SummarizeTicketCommand:
    """Parse an SQS message body into a command. Raises `MalformedMessage`
    on anything that isn't the confirmed `{ticketId, emailMetaId,
    threadId}` shape."""
    try:
        data = json.loads(body)
        return SummarizeTicketCommand(
            ticket_id=int(data["ticketId"]),
            email_meta_id=int(data["emailMetaId"]),
            thread_id=str(data["threadId"]),
            mode=WriteMode.APPEND_ONLY,
            triggered_by=TRIGGERED_BY,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise MalformedMessage(f"Could not parse SQS message body: {exc}") from exc


def _start_visibility_heartbeat(
    sqs: SqsClient,
    queue_url: str,
    receipt_handle: str,
    visibility_timeout: int,
    margin_seconds: int,
) -> Callable[[], None]:
    """Starts a daemon thread that re-extends the message's visibility
    timeout every `visibility_timeout - margin_seconds` while the job
    keeps running. Returns a callable that stops it."""
    interval = max(visibility_timeout - margin_seconds, 5)
    stop_event = threading.Event()

    def _extend() -> None:
        while not stop_event.wait(interval):
            try:
                sqs.change_message_visibility(
                    QueueUrl=queue_url,
                    ReceiptHandle=receipt_handle,
                    VisibilityTimeout=visibility_timeout,
                )
            except Exception:
                logger.warning("Failed to extend message visibility", exc_info=True)

    threading.Thread(target=_extend, daemon=True).start()
    return stop_event.set


def process_message(
    sqs: SqsClient,
    queue_url: str,
    use_case: SummarizeTicket,
    message: dict[str, Any],
    *,
    visibility_timeout: int,
    heartbeat_margin_seconds: int,
) -> None:
    """Handle one SQS message end to end. Deletes it on success; leaves
    it unacked on any failure (transient, terminal, or an unexpected
    bug) so SQS's own redrive policy takes over -- never crashes the
    caller, since one bad message must not take down the whole poller."""
    receipt_handle = message["ReceiptHandle"]
    receive_count = message.get("Attributes", {}).get("ApproximateReceiveCount")

    try:
        command = parse_command(message["Body"])
    except MalformedMessage as exc:
        logger.error(
            "Malformed SQS message -- leaving for redrive/DLQ: %s",
            exc,
            extra={"receive_count": receive_count},
        )
        return

    stop_heartbeat = _start_visibility_heartbeat(
        sqs, queue_url, receipt_handle, visibility_timeout, heartbeat_margin_seconds
    )
    try:
        result = use_case.execute(command)
    except TransientError as exc:
        logger.warning(
            "Transient failure -- leaving for SQS redelivery: %s",
            exc,
            extra={
                "ticket_id": command.ticket_id,
                "email_meta_id": command.email_meta_id,
                "receive_count": receive_count,
            },
        )
        return
    except TerminalError as exc:
        logger.error(
            "Terminal failure -- leaving for SQS redrive to DLQ: %s",
            exc,
            extra={
                "ticket_id": command.ticket_id,
                "email_meta_id": command.email_meta_id,
                "receive_count": receive_count,
            },
        )
        return
    except SummarizerError as exc:
        logger.error(
            "Unexpected pipeline error -- leaving for SQS redelivery: %s",
            exc,
            extra={"ticket_id": command.ticket_id, "email_meta_id": command.email_meta_id},
        )
        return
    except Exception:
        # A real bug in our own code. Never crash the poller over one
        # message -- log loudly and leave it; DLQ-depth alarms (see
        # CLAUDE.md's Queueing section) are what should page someone.
        logger.error(
            "Unhandled exception processing message -- leaving for SQS redelivery",
            extra={"ticket_id": command.ticket_id, "email_meta_id": command.email_meta_id},
            exc_info=True,
        )
        return
    finally:
        stop_heartbeat()

    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
    logger.info(
        "Summarization complete",
        extra={
            "ticket_id": command.ticket_id,
            "write_outcome": result.write_outcome.value,
            "status": result.status.value if result.status is not None else None,
            "processing_time_ms": result.processing_time_ms,
            "retry_count": result.retry_count,
            "token_input": result.token_input,
            "token_output": result.token_output,
        },
    )


def poll_once(
    sqs: SqsClient, use_case: SummarizeTicket, pool: ThreadPoolExecutor, settings: SqsSettings
) -> None:
    """One receive-and-dispatch cycle -- the unit the main loop repeats."""
    response = sqs.receive_message(
        QueueUrl=settings.queue_url,
        MaxNumberOfMessages=settings.max_messages,
        WaitTimeSeconds=settings.wait_time_seconds,
        VisibilityTimeout=settings.visibility_timeout,
        AttributeNames=["ApproximateReceiveCount"],
    )
    messages = response.get("Messages", [])
    if not messages:
        return

    logger.info("Received batch", extra={"count": len(messages)})
    futures = [
        pool.submit(
            process_message,
            sqs,
            settings.queue_url,
            use_case,
            message,
            visibility_timeout=settings.visibility_timeout,
            heartbeat_margin_seconds=settings.heartbeat_margin_seconds,
        )
        for message in messages
    ]
    for future in futures:
        try:
            future.result()
        except Exception:
            # process_message already catches everything it knows how
            # to handle -- this is defense in depth, not an expected path.
            logger.error("Unhandled exception escaped process_message", exc_info=True)


def main() -> int:
    configure_logging()
    settings = Settings()
    use_case = build_use_case(settings)
    sqs = boto3.client("sqs")

    shutting_down = threading.Event()

    def _handle_shutdown_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received shutdown signal", extra={"signal": signum})
        shutting_down.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    logger.info("SQS consumer starting", extra={"queue_url": settings.sqs.queue_url})

    with ThreadPoolExecutor(max_workers=settings.sqs.concurrency) as pool:
        while not shutting_down.is_set():
            try:
                poll_once(sqs, use_case, pool, settings.sqs)
            except Exception:
                logger.error("Unhandled error in poll loop -- backing off", exc_info=True)
                shutting_down.wait(10)

    logger.info("SQS consumer shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
