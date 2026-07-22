"""Structured JSON logging setup.

Configures stdlib ``logging`` with a JSON formatter that:
* Keys every record by ``ticketId`` / ``messageId`` when set via
  ``extra=``
* Never logs email bodies, attachment content, or PII
* Works with both the CLI entrypoint and the future SQS consumer
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Emits one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Carry pipeline correlation + operational fields when provided
        # via `extra=`. Deliberately an explicit allow-list (not "log
        # whatever's in extra=") so a careless future `extra=` call
        # elsewhere can't accidentally leak email body / attachment
        # content into logs.
        for key in (
            "ticket_id",
            "message_id",
            "thread_id",
            "email_meta_id",
            "write_outcome",
            "status",
            "processing_time_ms",
            "retry_count",
            "token_input",
            "token_output",
            "queue_url",
            "count",
            "receive_count",
            "signal",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(*, level: int = logging.INFO) -> None:
    """Call once at process startup (CLI or SQS consumer entrypoint)."""

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers if called more than once (e.g. tests).
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)

    # Quiet noisy third-party loggers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pymysql").setLevel(logging.WARNING)
