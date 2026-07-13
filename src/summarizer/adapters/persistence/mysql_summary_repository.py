"""MySQL adapter for the SummaryRepository port: compare-and-set writes
against `ticketAiSummary`, keyed on `emailMetaId` as the CAS marker.

Design note (see CLAUDE.md "Compare-and-set (CAS) write strategy"): the
same code path serves both live traffic and DLQ redrives under
WriteMode.APPEND_ONLY. REPROCESS is reserved for deliberate
administrative operations and must never be selected automatically by
the redrive path -- that distinction lives one layer up (the caller
chooses `mode`); this module just enforces whichever mode it's given.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pymysql.err
from pymysql.connections import Connection

from summarizer.domain.errors import SummaryPersistenceTransient
from summarizer.domain.ports import SummaryWrite, WriteMode, WriteOutcome

logger = logging.getLogger(__name__)

# One collision is the most that can ever happen on the insert-race path
# (see MySqlSummaryRepository docstring for why) -- a second implies
# something other than the expected two-way race and should surface as
# an error rather than retry silently forever.
_MAX_INSERT_RACE_RETRIES = 1

_TRANSIENT_MYSQL_ERROR_CODES = {
    1205,  # ER_LOCK_WAIT_TIMEOUT
    1213,  # ER_LOCK_DEADLOCK
    2006,  # CR_SERVER_GONE_ERROR
    2013,  # CR_SERVER_LOST
}

_SELECT_FOR_UPDATE_SQL = "SELECT emailMetaId FROM ticketAiSummary WHERE ticketId = %s FOR UPDATE"
_SELECT_FRONTIER_SQL = "SELECT emailMetaId FROM ticketAiSummary WHERE ticketId = %s"

_INSERT_SQL = """
INSERT INTO ticketAiSummary (
    ticketId, emailMetaId, latestMessageId, summary, summaryJson,
    modelName, modelVersion, promptVersion, summaryStatus,
    triggeredBy, processingTimeMs, tokenInput, tokenOutput,
    retryCount, errorMessage
) VALUES (
    %(ticket_id)s, %(marker)s, %(latest_message_id)s, %(summary)s, %(summary_json)s,
    %(model_name)s, %(model_version)s, %(prompt_version)s, %(status)s,
    %(triggered_by)s, %(processing_time_ms)s, %(token_input)s, %(token_output)s,
    %(retry_count)s, %(error_message)s
)
"""

_UPDATE_SQL = """
UPDATE ticketAiSummary
SET emailMetaId = %(marker)s,
    latestMessageId = %(latest_message_id)s,
    summary = %(summary)s,
    summaryJson = %(summary_json)s,
    modelName = %(model_name)s,
    modelVersion = %(model_version)s,
    promptVersion = %(prompt_version)s,
    summaryStatus = %(status)s,
    triggeredBy = %(triggered_by)s,
    processingTimeMs = %(processing_time_ms)s,
    tokenInput = %(token_input)s,
    tokenOutput = %(token_output)s,
    retryCount = %(retry_count)s,
    errorMessage = %(error_message)s
WHERE ticketId = %(ticket_id)s
"""


@dataclass(frozen=True, slots=True)
class _WriteDecision:
    outcome: WriteOutcome
    new_marker: int | None  # None iff outcome is SKIPPED_SUPERSEDED


def decide_write(
    stored_marker: int | None, incoming_marker: int, mode: WriteMode
) -> _WriteDecision:
    """Pure CAS decision logic, isolated from I/O so it can be exhaustively
    unit tested without a database.

    APPEND_ONLY: write only if incoming > stored (or no row exists yet).
    Equal-or-behind is SKIPPED_SUPERSEDED -- this is what makes DLQ
    redrive of a since-superseded message safe: a redrive must go
    through this same path, never REPROCESS (see CLAUDE.md CAS write
    strategy / DLQ redrive note).

    REPROCESS: always writes; the frontier is max(stored, incoming) so a
    deliberate reprocess of an older snapshot can never move the
    frontier backward.
    """
    if mode is WriteMode.APPEND_ONLY:
        if stored_marker is not None and incoming_marker <= stored_marker:
            return _WriteDecision(WriteOutcome.SKIPPED_SUPERSEDED, None)
        return _WriteDecision(WriteOutcome.WRITTEN, incoming_marker)

    new_marker = incoming_marker if stored_marker is None else max(stored_marker, incoming_marker)
    return _WriteDecision(WriteOutcome.WRITTEN, new_marker)


class MySqlSummaryRepository:
    """CAS/reprocess-aware persistence for `ticketAiSummary`.

    Concurrency strategy: `SELECT ... FOR UPDATE` inside an explicit
    transaction locks the ticket's row (keyed on the UNIQUE ticketId
    constraint) so two concurrent writers for the *same* ticket
    serialize; other tickets are unaffected.

    A row that doesn't exist yet locks nothing, so two-or-more concurrent
    first-writes for a brand-new ticket can all reach the INSERT branch.
    Exactly one wins; the rest get a duplicate-key IntegrityError, roll
    back, and retry once. On that second attempt the row now exists, so
    `SELECT ... FOR UPDATE` blocks on the winner's lock instead of
    racing, then proceeds down the UPDATE branch. One retry is always
    sufficient regardless of how many writers race, because only the
    very first INSERT anywhere in the race can fail with a collision --
    every subsequent attempt finds a row to lock.

    `connection_factory` must return a new PyMySQL connection with
    autocommit disabled (PyMySQL's default). Pooling, if any, is that
    factory's concern -- this class acquires one connection per call and
    closes it when done.
    """

    def __init__(self, connection_factory: Callable[[], Connection]) -> None:
        self._connection_factory = connection_factory

    def get_frontier(self, ticket_id: int) -> int | None:
        conn = self._connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(_SELECT_FRONTIER_SQL, (ticket_id,))
                row = cur.fetchone()
                return int(row[0]) if row else None
        except pymysql.err.OperationalError as exc:
            wrapped = self._wrap_if_transient(exc)
            if wrapped is not None:
                raise wrapped from exc
            raise
        finally:
            conn.close()

    def upsert(
        self,
        ticket_id: int,
        write: SummaryWrite,
        marker: int,
        mode: WriteMode,
    ) -> WriteOutcome:
        last_error: Exception | None = None

        for _attempt in range(_MAX_INSERT_RACE_RETRIES + 1):
            conn = self._connection_factory()
            try:
                conn.begin()
                with conn.cursor() as cur:
                    cur.execute(_SELECT_FOR_UPDATE_SQL, (ticket_id,))
                    row = cur.fetchone()
                    stored_marker = int(row[0]) if row else None

                    decision = decide_write(stored_marker, marker, mode)
                    if decision.outcome is WriteOutcome.SKIPPED_SUPERSEDED:
                        conn.rollback()
                        return WriteOutcome.SKIPPED_SUPERSEDED

                    params = self._params(ticket_id, write, decision.new_marker)

                    if row is None:
                        try:
                            cur.execute(_INSERT_SQL, params)
                        except pymysql.err.IntegrityError as exc:
                            conn.rollback()
                            last_error = exc
                            logger.info(
                                "ticketAiSummary insert race for ticket_id=%s; "
                                "retrying as update",
                                ticket_id,
                            )
                            continue
                    else:
                        cur.execute(_UPDATE_SQL, params)

                conn.commit()
                return WriteOutcome.WRITTEN
            except pymysql.err.OperationalError as exc:
                self._safe_rollback(conn)
                wrapped = self._wrap_if_transient(exc)
                if wrapped is not None:
                    raise wrapped from exc
                raise
            except Exception:
                self._safe_rollback(conn)
                raise
            finally:
                conn.close()

        raise SummaryPersistenceTransient(
            f"exhausted insert/update race retries for ticket_id={ticket_id}"
        ) from last_error

    @staticmethod
    def _params(ticket_id: int, write: SummaryWrite, marker: int | None) -> dict[str, Any]:
        return {
            "ticket_id": ticket_id,
            "marker": marker,
            "latest_message_id": write.latest_message_id,
            "summary": write.document.content.human_summary,
            "summary_json": write.document.model_dump_json(),
            "model_name": write.model_name,
            "model_version": write.model_version,
            "prompt_version": write.prompt_version,
            "status": write.status.value,
            "triggered_by": write.triggered_by,
            "processing_time_ms": write.processing_time_ms,
            "token_input": write.token_input,
            "token_output": write.token_output,
            "retry_count": write.retry_count,
            "error_message": write.error_message,
        }

    @staticmethod
    def _wrap_if_transient(exc: pymysql.err.OperationalError) -> Exception | None:
        code = exc.args[0] if exc.args else None
        if code in _TRANSIENT_MYSQL_ERROR_CODES:
            return SummaryPersistenceTransient(str(exc))
        return None

    @staticmethod
    def _safe_rollback(conn: Connection) -> None:
        try:
            conn.rollback()
        except Exception:
            logger.warning("rollback failed on an already-broken connection", exc_info=True)
