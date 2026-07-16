"""HTTP adapter for the EmailGateway port.

Calls the internal Email API's ``getMailBody`` endpoint with query
parameters ``companyId``, ``ticketId``, ``emailMetaId`` and
``threadId`` to fetch full email content + attachments.

Contract updated 2026-07-16 (see CLAUDE.md). Two changes from the
previous ``messageId``-inclusive contract:

* The API no longer takes ``messageId`` as a filter param. Lookup is
  by (companyId, ticketId, emailMetaId, threadId) alone --
  ``email_meta_id`` is already the row's own PK, so it's unambiguous
  without ``messageId``. ``message_id`` is still accepted on this
  method's signature (used as the RawEmail fallback when the response
  omits its own id) but is not sent on the wire.
* The response envelope changed from a bare JSON array to
  ``{"status": <int>, "result": {...single email object...}}``.

Per user confirmation (2026-07-16):
* No authentication required.
* No rate limits.
* An HTTP status other than 200 is an error (404 -> not yet available).

NOT YET CONFIRMED AGAINST STAGING: whether "not yet available" can
also be signalled via a body-level ``status`` field on an HTTP-200
response, rather than only via HTTP 404. Handled defensively below so
the RYW gate degrades safely either way; verify against a real
not-yet-available ticket on stage and simplify this once confirmed.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from summarizer.domain.errors import EmailApiTransient, EmailNotYetAvailable
from summarizer.domain.models import RawAttachment, RawEmail

logger = logging.getLogger(__name__)

_COMPANY_ID = "steppingcloud"


def _strip_data_uri_prefix(value: str | None) -> str | None:
    """``fileBase64`` arrives as ``data:<mime>;base64,<data>``, not raw
    base64. ``base64.b64decode`` doesn't error on the prefix (its
    letters are valid base64 alphabet) -- it silently decodes to
    corrupted bytes instead, so this must be stripped explicitly."""
    if value is None:
        return None
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


class HttpEmailGateway:
    """Fetches a single email from the Email API by
    (ticket, emailMeta, thread) -- see module docstring for the
    2026-07-16 contract change.

    Attachments include base64 ``fileBase64`` (data-URI prefixed),
    which is stripped and passed through for the extractor to decode.
    """

    def __init__(self, base_url: str, timeout_seconds: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._session = requests.Session()

    def fetch_email(
        self,
        *,
        ticket_id: int,
        email_meta_id: int,
        message_id: str,
        thread_id: str | None,
    ) -> RawEmail:
        params: dict[str, str | int | None] = {
            "companyId": _COMPANY_ID,
            "ticketId": ticket_id,
            "emailMetaId": email_meta_id,
            "threadId": thread_id,
        }
        logger.info(
            "Fetching email from API",
            extra={
                "ticket_id": ticket_id,
                "email_meta_id": email_meta_id,
                "message_id": message_id,
                "thread_id": thread_id,
            },
        )

        try:
            response = self._session.get(self._base_url, params=params, timeout=self._timeout)
        except requests.exceptions.ConnectionError as exc:
            raise EmailApiTransient(f"Connection error fetching {message_id}: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise EmailApiTransient(f"Timeout fetching {message_id}: {exc}") from exc

        if response.status_code == 404:
            raise EmailNotYetAvailable(
                f"Email {message_id} not found (404) — may not be available yet"
            )

        if response.status_code != 200:
            raise EmailApiTransient(
                f"Email API returned {response.status_code} for {message_id}"
            )

        data = response.json()
        email_data = self._unwrap_envelope(data, message_id)
        return self._parse_email(email_data, message_id)

    def _unwrap_envelope(self, data: Any, message_id: str) -> dict[str, Any]:
        """Unwrap ``{"status": int, "result": {...}}``.

        Defensively checks the body-level ``status`` in case "not yet
        available" is signalled that way on an HTTP-200 response (see
        module docstring — unconfirmed against staging).
        """
        if not isinstance(data, dict) or "result" not in data:
            raise EmailApiTransient(
                f"Unexpected Email API response shape for {message_id}: {data!r}"
            )

        body_status = data.get("status")
        if body_status is not None and body_status != 200:
            if body_status == 404:
                raise EmailNotYetAvailable(
                    f"Email API body status 404 for {message_id} — may not be available yet"
                )
            raise EmailApiTransient(f"Email API body status {body_status} for {message_id}")

        result = data.get("result")
        if not result:
            raise EmailNotYetAvailable(f"Email API returned empty result for {message_id}")

        return result  # type: ignore[no-any-return]

    def _parse_email(self, data: dict, message_id: str) -> RawEmail:  # type: ignore[type-arg]
        """Parse the Email API JSON into a ``RawEmail`` domain object."""

        from_info = data.get("from", {})
        from_values = from_info.get("value", [])
        from_address = from_values[0].get("address", "") if from_values else ""
        from_name = from_values[0].get("name") if from_values else None

        to_info = data.get("to", {})
        to_values = to_info.get("value", [])
        to_addresses = [v.get("address", "") for v in to_values]

        raw_attachments = [
            RawAttachment(
                filename=att.get("fileName", "unknown"),
                mime_type=att.get("fileType", "application/octet-stream"),
                size=att.get("size", 0),
                attachment_id=str(att.get("emailAttachmentID", "")),
                content_base64=_strip_data_uri_prefix(att.get("fileBase64")),
            )
            for att in data.get("attachment", [])
        ]

        return RawEmail(
            message_id=data.get("messageId") or data.get("id") or message_id,
            subject=data.get("subject", ""),
            from_address=from_address,
            from_name=from_name,
            to_addresses=to_addresses,
            date=data.get("date"),
            text_body=data.get("text"),
            latest_text_body=data.get("latest_text_body"),
            html_body=data.get("mailBody"),
            in_reply_to=data.get("inReplyTo"),
            thread_id=data.get("threadId"),
            attachments=raw_attachments,
        )
