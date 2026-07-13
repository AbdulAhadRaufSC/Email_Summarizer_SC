"""MySQL adapter for the EmailMetadataRepository port.

Queries ``Email_Metadata`` to enumerate a ticket's emails, ordered by
``emailMetaId``.  Excludes drafts (``isDraft=1``) and deleted rows
(``isDeleted=1``); includes notes (``isNote=1``) per user decision.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pymysql.err
from pymysql.connections import Connection

from summarizer.domain.errors import SummaryPersistenceTransient
from summarizer.domain.models import EmailRef

logger = logging.getLogger(__name__)

_TRANSIENT_MYSQL_ERROR_CODES = {1205, 1213, 2006, 2013}

_LIST_EMAIL_REFS_SQL = """
SELECT emailMetaId, messageId, isNote
FROM Email_Metadata
WHERE ticketId = %s
  AND isDraft = 0
  AND isDeleted = 0
ORDER BY emailMetaId ASC
"""


class MySqlEmailMetadataRepository:
    """Reads email references for a ticket from MySQL ``Email_Metadata``.

    Uses the same connection-factory pattern as
    ``MySqlSummaryRepository``: one connection per call, closed when
    done.  Read-only, no transactions needed.
    """

    def __init__(self, connection_factory: Callable[[], Connection]) -> None:
        self._connection_factory = connection_factory

    def list_email_refs(self, ticket_id: int) -> list[EmailRef]:
        conn = self._connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(_LIST_EMAIL_REFS_SQL, (ticket_id,))
                rows = cur.fetchall()
                return [
                    EmailRef(
                        email_meta_id=int(row[0]),
                        message_id=str(row[1]),
                        is_note=bool(row[2]),
                    )
                    for row in rows
                ]
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else None
            if code in _TRANSIENT_MYSQL_ERROR_CODES:
                raise SummaryPersistenceTransient(str(exc)) from exc
            raise
        finally:
            conn.close()
