"""HTTP adapter for the EmailGateway port.

Calls the internal Email API at ``{base_url}`` with query parameters
``companyId``, ``ticketId``, ``emailMetaId``, ``messageId`` and
``threadId`` to fetch full email content + attachments.

Previously the API was looked up by ``messageId`` alone
(``{base_url}/{messageId}``); that was occasionally returning an empty
array for emails that do in fact exist (see CLAUDE.md's ticket 239907
finding). Disambiguating with the full set of identifiers fixes that.
``companyId`` is static -- this deployment only ever serves one
company, "steppingcloud".

Per user confirmation:
* No authentication required.
* No rate limits.
* Any non-200 status is an error.
* Response is always a JSON array.
"""

from __future__ import annotations

import logging

import requests

from summarizer.domain.errors import EmailApiTransient, EmailNotYetAvailable
from summarizer.domain.models import RawAttachment, RawEmail

logger = logging.getLogger(__name__)

_COMPANY_ID = "steppingcloud"


class HttpEmailGateway:
    """Fetches a single email from the Email API by its full set of
    identifiers (ticket, emailMeta, message, thread).

    The API returns a JSON array; we take the first element.
    Attachments include base64 ``content`` which is passed through
    as-is for the extractor to decode later.
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
            "messageId": message_id,
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

        # Response is always a JSON array; take the first element.
        if isinstance(data, list):
            if not data:
                raise EmailNotYetAvailable(f"Email API returned empty array for {message_id}")
            email_data = data[0]
        else:
            email_data = data

        return self._parse_email(email_data, message_id)

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
                filename=att.get("filename", "unknown"),
                mime_type=att.get("mimeType", "application/octet-stream"),
                size=att.get("size", 0),
                attachment_id=att.get("attachmentId", ""),
                content_base64=att.get("content"),
            )
            for att in data.get("attachments", [])
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
            html_body=data.get("html"),
            in_reply_to=data.get("inReplyTo"),
            thread_id=data.get("threadId"),
            attachments=raw_attachments,
        )
