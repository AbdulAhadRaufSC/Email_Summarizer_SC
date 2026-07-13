"""Integration tests for MySqlSummaryRepository against a real MySQL
engine, via testcontainers.

Not run by default (`uv run pytest` excludes anything marked
`integration`, see pyproject.toml addopts) and NOT executed by the agent
that wrote this file -- the dev sandbox it ran in has neither Docker nor
a reachable MySQL instance, only a Windows machine with no `docker`
command. Run explicitly wherever Docker is available:

    uv run pytest -m integration

Everything that could actually be verified without a live database
(the CAS decision matrix, parameter construction, error classification)
is instead covered -- and passing -- in
tests/unit/adapters/persistence/test_mysql_summary_repository.py.
"""

import threading
from collections.abc import Callable, Iterator

import pymysql
import pytest
from pymysql.connections import Connection
from sqlalchemy.engine import make_url
from testcontainers.mysql import MySqlContainer

from summarizer.adapters.persistence.mysql_summary_repository import MySqlSummaryRepository
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

pytestmark = pytest.mark.integration

# Mirrors the ticketAiSummary shape in CLAUDE.md (draft table + the
# emailMetaId / summaryJson additions agreed during the LLD).
_DDL = """
CREATE TABLE ticketAiSummary (
  id bigint NOT NULL AUTO_INCREMENT,
  ticketId bigint NOT NULL,
  emailMetaId bigint NOT NULL,
  latestMessageId varchar(255) NOT NULL,
  summary longtext,
  summaryJson json DEFAULT NULL,
  modelName varchar(100) DEFAULT NULL,
  modelVersion varchar(50) DEFAULT NULL,
  promptVersion varchar(50) DEFAULT NULL,
  summaryStatus varchar(50) DEFAULT NULL,
  triggeredBy varchar(250) DEFAULT NULL,
  processingTimeMs int DEFAULT NULL,
  tokenInput int DEFAULT NULL,
  tokenOutput int DEFAULT NULL,
  retryCount int DEFAULT '0',
  errorMessage varchar(500) DEFAULT NULL,
  createdAt timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  updatedAt timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_ticketId (ticketId)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
"""


@pytest.fixture(scope="module")
def mysql_container() -> Iterator[MySqlContainer]:
    with MySqlContainer("mysql:8.0") as container:
        yield container


@pytest.fixture
def connection_factory(mysql_container: MySqlContainer) -> Callable[[], Connection]:
    url = make_url(mysql_container.get_connection_url())

    def _connect() -> Connection:
        return pymysql.connect(
            host=url.host,
            port=url.port or 3306,
            user=url.username,
            password=url.password or "",
            database=url.database,
            autocommit=False,
        )

    # Fresh schema per test for isolation; the container itself (module
    # scoped) is reused since it's what's expensive to start.
    setup_conn = _connect()
    try:
        with setup_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS ticketAiSummary")
            cur.execute(_DDL)
        setup_conn.commit()
    finally:
        setup_conn.close()

    return _connect


@pytest.fixture
def repository(connection_factory: Callable[[], Connection]) -> MySqlSummaryRepository:
    return MySqlSummaryRepository(connection_factory)


def _write(marker: int, human_summary: str = "summary") -> SummaryWrite:
    return SummaryWrite(
        document=SummaryDocument(
            content=LlmSummaryOutput(
                human_summary=human_summary,
                customer_issue="issue",
                current_status=TicketStatus.OPEN,
            ),
            context_completeness=ContextCompleteness(status=CompletenessStatus.COMPLETE),
            source=SourceInfo(email_count=1, frontier_email_meta_id=marker),
        ),
        latest_message_id=f"msg-{marker}",
        model_name="Qwen2.5-7B-Instruct",
        model_version="2.5",
        prompt_version="v1",
        status=PersistedSummaryStatus.OK,
    )


class TestFreshTicket:
    def test_get_frontier_is_none_before_any_write(
        self, repository: MySqlSummaryRepository
    ) -> None:
        assert repository.get_frontier(ticket_id=1) is None

    def test_first_write_is_written_and_readable(self, repository: MySqlSummaryRepository) -> None:
        outcome = repository.upsert(1, _write(100), marker=100, mode=WriteMode.APPEND_ONLY)
        assert outcome is WriteOutcome.WRITTEN
        assert repository.get_frontier(1) == 100


class TestAppendOnly:
    def test_newer_marker_overwrites(self, repository: MySqlSummaryRepository) -> None:
        repository.upsert(1, _write(100, "v1"), marker=100, mode=WriteMode.APPEND_ONLY)
        outcome = repository.upsert(1, _write(150, "v2"), marker=150, mode=WriteMode.APPEND_ONLY)
        assert outcome is WriteOutcome.WRITTEN
        assert repository.get_frontier(1) == 150

    def test_stale_marker_is_skipped_and_leaves_content_untouched(
        self,
        repository: MySqlSummaryRepository,
        connection_factory: Callable[[], Connection],
    ) -> None:
        repository.upsert(1, _write(150, "current"), marker=150, mode=WriteMode.APPEND_ONLY)

        outcome = repository.upsert(
            1, _write(100, "stale-redrive"), marker=100, mode=WriteMode.APPEND_ONLY
        )

        assert outcome is WriteOutcome.SKIPPED_SUPERSEDED
        assert repository.get_frontier(1) == 150
        conn = connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT summary FROM ticketAiSummary WHERE ticketId = %s", (1,))
                row = cur.fetchone()
                assert row is not None
                (summary,) = row
        finally:
            conn.close()
        assert summary == "current"

    def test_dlq_redrive_of_superseded_message_is_a_safe_no_op(
        self, repository: MySqlSummaryRepository
    ) -> None:
        """End-to-end version of the scenario in CLAUDE.md: a message for
        email 140 sits in the DLQ while live traffic advances the ticket
        to 150. The redrive must go through APPEND_ONLY, same as any
        other event -- this proves it does not clobber the newer row."""
        repository.upsert(7, _write(140, "based-on-140"), marker=140, mode=WriteMode.APPEND_ONLY)
        repository.upsert(7, _write(150, "based-on-150"), marker=150, mode=WriteMode.APPEND_ONLY)

        redrive_outcome = repository.upsert(
            7, _write(140, "based-on-140"), marker=140, mode=WriteMode.APPEND_ONLY
        )

        assert redrive_outcome is WriteOutcome.SKIPPED_SUPERSEDED
        assert repository.get_frontier(7) == 150


class TestReprocess:
    def test_overwrites_content_and_preserves_newer_frontier(
        self, repository: MySqlSummaryRepository
    ) -> None:
        repository.upsert(1, _write(150, "current"), marker=150, mode=WriteMode.APPEND_ONLY)

        outcome = repository.upsert(
            1,
            _write(100, "reprocessed-with-improved-prompt"),
            marker=100,
            mode=WriteMode.REPROCESS,
        )

        assert outcome is WriteOutcome.WRITTEN
        assert repository.get_frontier(1) == 150  # frontier never regresses

    def test_same_frontier_reprocess_updates_content(
        self, repository: MySqlSummaryRepository
    ) -> None:
        repository.upsert(1, _write(150, "v1"), marker=150, mode=WriteMode.APPEND_ONLY)
        outcome = repository.upsert(
            1, _write(150, "v2-reprocessed"), marker=150, mode=WriteMode.REPROCESS
        )
        assert outcome is WriteOutcome.WRITTEN
        assert repository.get_frontier(1) == 150


class TestConcurrentFirstWrite:
    def test_two_concurrent_first_writes_converge_on_the_higher_marker(
        self, repository: MySqlSummaryRepository
    ) -> None:
        """The race this whole locking design exists for: two workers
        both see no existing row for a brand-new ticket and race to
        INSERT. Regardless of which one physically wins, the result
        must converge on the higher marker with its content -- never a
        lost update, never an unhandled IntegrityError (see
        MySqlSummaryRepository's docstring for why one retry suffices).
        """
        outcomes: dict[int, WriteOutcome] = {}
        errors: list[BaseException] = []

        def _do(marker: int) -> None:
            try:
                outcomes[marker] = repository.upsert(
                    99,
                    _write(marker, f"content-{marker}"),
                    marker=marker,
                    mode=WriteMode.APPEND_ONLY,
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_do, args=(marker,)) for marker in (100, 200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"upsert() raised under concurrency: {errors}"
        assert WriteOutcome.WRITTEN in outcomes.values()
        assert repository.get_frontier(99) == 200
