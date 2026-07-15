"""Unit tests for MySqlEmailMetadataRepository using a fake connection.

The query here is a single, non-transactional SELECT with no locking
semantics (unlike MySqlSummaryRepository's CAS write), so a fake
cursor/connection is sufficient to prove the row -> EmailRef mapping and
the transient-error classification without needing a real MySQL engine.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import pymysql.err
import pytest

from summarizer.adapters.email.mysql_email_metadata import MySqlEmailMetadataRepository
from summarizer.domain.errors import SummaryPersistenceTransient


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None, error: Exception | None = None):
        self._rows = rows or []
        self._error = error
        self.executed_with: tuple[str, tuple[Any, ...]] | None = None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.executed_with = (sql, params)
        if self._error is not None:
            raise self._error

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


class TestListEmailRefs:
    def test_maps_rows_to_email_refs_in_order(self) -> None:
        cursor = _FakeCursor(
            rows=[
                (101, "msg-101", 0, "thread-1"),
                (102, "msg-102", 1, "thread-1"),
            ]
        )
        connection = _FakeConnection(cursor)
        repo = MySqlEmailMetadataRepository(connection_factory=lambda: connection)  # type: ignore[arg-type, return-value]

        refs = repo.list_email_refs(ticket_id=42)

        assert [r.email_meta_id for r in refs] == [101, 102]
        assert refs[0].message_id == "msg-101"
        assert refs[0].is_note is False
        assert refs[0].thread_id == "thread-1"
        assert refs[1].is_note is True

    def test_returns_empty_list_when_no_rows(self) -> None:
        connection = _FakeConnection(_FakeCursor(rows=[]))
        repo = MySqlEmailMetadataRepository(connection_factory=lambda: connection)  # type: ignore[arg-type, return-value]

        assert repo.list_email_refs(ticket_id=42) == []

    def test_queries_with_ticket_id_param(self) -> None:
        cursor = _FakeCursor(rows=[])
        connection = _FakeConnection(cursor)
        repo = MySqlEmailMetadataRepository(connection_factory=lambda: connection)  # type: ignore[arg-type, return-value]

        repo.list_email_refs(ticket_id=42)

        assert cursor.executed_with is not None
        _, params = cursor.executed_with
        assert params == (42,)

    def test_closes_connection_on_success(self) -> None:
        connection = _FakeConnection(_FakeCursor(rows=[]))
        repo = MySqlEmailMetadataRepository(connection_factory=lambda: connection)  # type: ignore[arg-type, return-value]

        repo.list_email_refs(ticket_id=42)

        assert connection.closed is True

    def test_closes_connection_on_error(self) -> None:
        connection = _FakeConnection(
            _FakeCursor(error=pymysql.err.OperationalError(2006, "gone away"))
        )
        repo = MySqlEmailMetadataRepository(connection_factory=lambda: connection)  # type: ignore[arg-type, return-value]

        with pytest.raises(SummaryPersistenceTransient):
            repo.list_email_refs(ticket_id=42)

        assert connection.closed is True


class TestTransientErrorClassification:
    @pytest.mark.parametrize("code", [1205, 1213, 2006, 2013])
    def test_known_transient_codes_are_wrapped(self, code: int) -> None:
        connection = _FakeConnection(_FakeCursor(error=pymysql.err.OperationalError(code, "boom")))
        repo = MySqlEmailMetadataRepository(connection_factory=lambda: connection)  # type: ignore[arg-type, return-value]

        with pytest.raises(SummaryPersistenceTransient):
            repo.list_email_refs(ticket_id=1)

    @pytest.mark.parametrize("code", [1045, 1146, 9999])
    def test_other_codes_propagate_unwrapped(self, code: int) -> None:
        connection = _FakeConnection(
            _FakeCursor(error=pymysql.err.OperationalError(code, "not transient"))
        )
        repo = MySqlEmailMetadataRepository(connection_factory=lambda: connection)  # type: ignore[arg-type, return-value]

        with pytest.raises(pymysql.err.OperationalError):
            repo.list_email_refs(ticket_id=1)
