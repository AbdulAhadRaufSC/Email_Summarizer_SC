from __future__ import annotations

from typing import Any

import pytest
import requests

from summarizer.adapters.email.http_email_gateway import HttpEmailGateway
from summarizer.domain.errors import EmailApiTransient, EmailNotYetAvailable

SAMPLE_EMAIL = {
    "messageId": "msg-1",
    "subject": "Cannot log in",
    "from": {"value": [{"address": "cust@example.com", "name": "Customer One"}]},
    "to": {"value": [{"address": "support@example.com"}]},
    "date": "2026-07-01T10:00:00Z",
    "text": "Full accumulated text",
    "html": "<p>Full accumulated text</p>",
    "inReplyTo": None,
    "threadId": "thread-1",
    "attachments": [
        {
            "filename": "log.txt",
            "mimeType": "text/plain",
            "size": 123,
            "attachmentId": "att-1",
            "content": "aGVsbG8=",
        }
    ],
}


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None, raise_exc: Exception | None = None):
        self._response = response
        self._raise_exc = raise_exc
        self.last_url: str | None = None
        self.last_params: dict[str, Any] | None = None
        self.last_timeout: int | None = None

    def get(self, url: str, params: dict[str, Any], timeout: int) -> _FakeResponse:
        self.last_url = url
        self.last_params = params
        self.last_timeout = timeout
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._response is not None
        return self._response


def _gateway_with(
    session: _FakeSession, base_url: str = "https://mail.example.com/api/emails"
) -> HttpEmailGateway:
    gw = HttpEmailGateway(base_url=base_url, timeout_seconds=5)
    gw._session = session  # type: ignore[assignment]
    return gw


def _fetch(
    gw: HttpEmailGateway,
    *,
    ticket_id: int = 1,
    email_meta_id: int = 101,
    message_id: str = "msg-1",
    thread_id: str | None = "thread-1",
):
    return gw.fetch_email(
        ticket_id=ticket_id,
        email_meta_id=email_meta_id,
        message_id=message_id,
        thread_id=thread_id,
    )


class TestFetchEmailHappyPath:
    def test_parses_full_email(self) -> None:
        session = _FakeSession(_FakeResponse(200, [SAMPLE_EMAIL]))
        gw = _gateway_with(session)

        email = _fetch(gw)

        assert email.message_id == "msg-1"
        assert email.subject == "Cannot log in"
        assert email.from_address == "cust@example.com"
        assert email.from_name == "Customer One"
        assert email.to_addresses == ["support@example.com"]
        assert email.thread_id == "thread-1"
        assert len(email.attachments) == 1
        assert email.attachments[0].filename == "log.txt"
        assert email.attachments[0].content_base64 == "aGVsbG8="

    def test_requests_correct_url_query_params_and_timeout(self) -> None:
        session = _FakeSession(_FakeResponse(200, [SAMPLE_EMAIL]))
        gw = _gateway_with(session, base_url="https://mail.example.com/api/emails/")

        _fetch(gw, ticket_id=7, email_meta_id=101, message_id="msg-1", thread_id="thread-1")

        assert session.last_url == "https://mail.example.com/api/emails"
        assert session.last_params == {
            "companyId": "steppingcloud",
            "ticketId": 7,
            "emailMetaId": 101,
            "messageId": "msg-1",
            "threadId": "thread-1",
        }
        assert session.last_timeout == 5

    def test_passes_none_thread_id_through_as_none(self) -> None:
        session = _FakeSession(_FakeResponse(200, [SAMPLE_EMAIL]))
        gw = _gateway_with(session)

        _fetch(gw, thread_id=None)

        assert session.last_params is not None
        assert session.last_params["threadId"] is None

    def test_handles_missing_optional_fields(self) -> None:
        minimal = {"messageId": "msg-2"}
        session = _FakeSession(_FakeResponse(200, [minimal]))
        gw = _gateway_with(session)

        email = _fetch(gw, message_id="msg-2")

        assert email.subject == ""
        assert email.from_address == ""
        assert email.from_name is None
        assert email.to_addresses == []
        assert email.attachments == []

    def test_falls_back_to_requested_message_id_when_absent(self) -> None:
        no_id = {k: v for k, v in SAMPLE_EMAIL.items() if k != "messageId"}
        session = _FakeSession(_FakeResponse(200, [no_id]))
        gw = _gateway_with(session)

        email = _fetch(gw, message_id="requested-id")

        assert email.message_id == "requested-id"


class TestFetchEmailErrors:
    def test_404_raises_email_not_yet_available(self) -> None:
        session = _FakeSession(_FakeResponse(404, {}))
        gw = _gateway_with(session)

        with pytest.raises(EmailNotYetAvailable):
            _fetch(gw)

    def test_empty_array_raises_email_not_yet_available(self) -> None:
        session = _FakeSession(_FakeResponse(200, []))
        gw = _gateway_with(session)

        with pytest.raises(EmailNotYetAvailable):
            _fetch(gw)

    def test_5xx_raises_email_api_transient(self) -> None:
        session = _FakeSession(_FakeResponse(503, {}))
        gw = _gateway_with(session)

        with pytest.raises(EmailApiTransient):
            _fetch(gw)

    def test_connection_error_raises_email_api_transient(self) -> None:
        session = _FakeSession(raise_exc=requests.exceptions.ConnectionError("refused"))
        gw = _gateway_with(session)

        with pytest.raises(EmailApiTransient):
            _fetch(gw)

    def test_timeout_raises_email_api_transient(self) -> None:
        session = _FakeSession(raise_exc=requests.exceptions.Timeout("slow"))
        gw = _gateway_with(session)

        with pytest.raises(EmailApiTransient):
            _fetch(gw)
