"""CLI entrypoint: process a single ticket end-to-end without SQS.

SQS setup is deferred (the team will wire it up later -- see CLAUDE.md's
Open Questions). This lets the pipeline be exercised against real infra
(MySQL, the internal Email API, RunPod) one ticket at a time. When SQS
is added, the consumer becomes a thin wrapper around the same
`SummarizeTicket.execute()` call this makes -- this file does not
change.

Usage:
    python -m summarizer.entrypoints.cli --ticket-id 12345 \\
        --email-meta-id 67890 --message-id "<abc@stepping-desk>"
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from summarizer.application.command import SummarizeTicketCommand
from summarizer.composition import build_use_case
from summarizer.config.logging_config import configure_logging
from summarizer.config.settings import Settings
from summarizer.domain.errors import SummarizerError, TerminalError, TransientError
from summarizer.domain.ports import WriteMode

logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize a single Stepping Desk ticket without SQS."
    )
    parser.add_argument("--ticket-id", type=int, required=True)
    parser.add_argument("--email-meta-id", type=int, required=True)
    parser.add_argument("--message-id", type=str, required=True)
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help=(
            "Force-overwrite the summary content (administrative use only -- "
            "never use this for normal live-traffic or DLQ-redrive runs)."
        ),
    )
    parser.add_argument("--triggered-by", type=str, default="cli")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = _parse_args(argv)

    settings = Settings()
    use_case = build_use_case(settings)

    
    #add thread_id and company_id
    command = SummarizeTicketCommand(
        ticket_id=args.ticket_id,
        email_meta_id=args.email_meta_id,
        message_id=args.message_id,
        mode=WriteMode.REPROCESS if args.reprocess else WriteMode.APPEND_ONLY,
        triggered_by=args.triggered_by,
    )

    try:
        result = use_case.execute(command)
    except TransientError as exc:
        logger.error("Transient failure -- SQS would redeliver this: %s", exc)
        return 1
    except TerminalError as exc:
        logger.error("Terminal failure -- SQS would route this to the DLQ: %s", exc)
        return 1
    except SummarizerError as exc:
        logger.error("Unexpected pipeline error: %s", exc)
        return 1

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
