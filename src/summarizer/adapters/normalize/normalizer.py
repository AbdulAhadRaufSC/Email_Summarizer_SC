"""Thread normalizer adapter.

Cleans raw emails into a ``NormalizedConversation``:
* Strips quoted reply blocks ("On … wrote:", "> …" prefixes)
* Removes email signatures and disclaimers
* Deduplicates identical content across the thread
* Prefers ``latest_text_body`` (just the new reply) over ``text_body``
  (full accumulated text) when available

Uses ``email_reply_parser`` as the primary engine with regex
fallbacks for edge cases the library doesn't handle.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from email_reply_parser import EmailReplyParser  # type: ignore[import-untyped]

from summarizer.domain.models import NormalizedConversation, NormalizedEmail, RawEmail

logger = logging.getLogger(__name__)

# Tags whose boundaries should force a line break when flattening HTML to
# text, so e.g. "<p>Hi</p><p>There</p>" doesn't collapse into "HiThere".
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "table", "ul", "ol",
}
# Tags whose text content is never real message content.
_SKIP_TAGS = {"script", "style"}


class _HtmlTextExtractor(HTMLParser):
    """Minimal HTML-to-text flattener, stdlib only.

    Not a general-purpose HTML renderer -- just enough to recover
    readable text from an email's HTML body. ``HTMLParser`` is a plain
    tokenizer (no DTD/external-entity resolution), so it carries none of
    the XXE-style risk the extraction sandbox guards against for
    XLSX/DOCX.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        lines = [line.strip() for line in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line)


def _html_to_text(html: str | None) -> str | None:
    """Best-effort plain-text recovery from an HTML email body.

    Used as a last-resort fallback when neither ``latest_text_body`` nor
    ``text_body`` is available. Agent replies composed in Stepping
    Desk's own editor (rather than sent from a real email client) have
    been observed to carry only ``mailBody`` (HTML) with no separate
    plain-text part, unlike inbound customer email which always has one
    -- without this fallback those replies silently vanish (empty body
    -> dropped by ``normalize()``), producing a thread that reads as
    customer-only.
    """
    if not html or not html.strip():
        return None
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.debug("HTML-to-text conversion failed", exc_info=True)
        return None
    text = parser.get_text()
    return text or None

# Common signature/disclaimer patterns (case-insensitive).
_SIGNATURE_PATTERNS = [
    re.compile(r"^--\s*$", re.MULTILINE),
    re.compile(r"^_{3,}\s*$", re.MULTILINE),
    re.compile(r"^Sent from my (iPhone|iPad|Android|Samsung|Galaxy)", re.MULTILINE | re.IGNORECASE),
    re.compile(
        r"^(This email|This message|This communication|CONFIDENTIAL|DISCLAIMER)",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(r"^Get Outlook for (iOS|Android)", re.MULTILINE | re.IGNORECASE),
]

# Quote header patterns for fallback stripping.
_QUOTE_HEADER_PATTERNS = [
    re.compile(r"^On .+wrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:\s+.+\nSent:\s+.+\nTo:\s+", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^>{1,}\s*", re.MULTILINE),
]


class DefaultThreadNormalizer:
    """Cleans a list of raw emails into a normalised conversation.

    The PII-mask hook is a placeholder for future use — the normalizer
    calls it on each cleaned body, defaulting to a no-op pass-through.
    """

    def normalize(self, emails: list[RawEmail]) -> NormalizedConversation:
        if not emails:
            return NormalizedConversation(subject="", emails=[])

        subject = emails[0].subject or ""
        seen_bodies: set[str] = set()
        normalized: list[NormalizedEmail] = []

        for email in emails:
            body = self._clean_body(email)

            if not body:
                continue

            # Deduplicate: skip if we've seen this exact body before.
            body_key = body.strip().lower()
            if body_key in seen_bodies:
                logger.debug(
                    "Skipping duplicate body for %s",
                    email.message_id,
                )
                continue
            seen_bodies.add(body_key)

            normalized.append(
                NormalizedEmail(
                    message_id=email.message_id,
                    sender=email.from_address,
                    sender_name=email.from_name,
                    date=email.date,
                    body=body,
                    is_note=email.is_note,
                )
            )

        return NormalizedConversation(subject=subject, emails=normalized)

    def _clean_body(self, email: RawEmail) -> str:
        """Extract and clean the most relevant text from an email.

        Prefers ``latest_text_body`` (just the new reply, no quoted
        history) over ``text_body`` (the full accumulated text).  Falls
        back to ``email_reply_parser`` + regex stripping on ``text_body``
        if ``latest_text_body`` is unavailable, and further falls back
        to flattening ``html_body`` to text if there's no plain-text
        body at all (see ``_html_to_text`` docstring for why this
        matters).
        """
        # Prefer the latest-only body (pre-stripped by the Email API).
        text = email.latest_text_body
        if text and text.strip():
            cleaned = self._strip_signatures(text.strip())
            if cleaned:
                return self._pii_mask(cleaned)

        # Fall back to the full text body and strip quotes.
        text = email.text_body
        if not text or not text.strip():
            text = _html_to_text(email.html_body)

        if not text or not text.strip():
            return ""

        # Use email_reply_parser to get only the visible (non-quoted) parts.
        try:
            reply = EmailReplyParser.parse_reply(text)
            if reply and reply.strip():
                cleaned = self._strip_signatures(reply.strip())
                if cleaned:
                    return self._pii_mask(cleaned)
        except Exception:
            logger.debug("email_reply_parser failed, falling back to regex", exc_info=True)

        # Regex fallback: strip quote headers and ">" lines.
        cleaned = self._regex_strip_quotes(text.strip())
        cleaned = self._strip_signatures(cleaned)
        return self._pii_mask(cleaned)

    @staticmethod
    def _strip_signatures(text: str) -> str:
        """Remove common email signatures and disclaimers."""
        for pattern in _SIGNATURE_PATTERNS:
            match = pattern.search(text)
            if match:
                text = text[: match.start()].rstrip()
        return text

    @staticmethod
    def _regex_strip_quotes(text: str) -> str:
        """Remove quoted reply blocks via regex."""
        for pattern in _QUOTE_HEADER_PATTERNS:
            match = pattern.search(text)
            if match:
                # Keep everything before the quote header.
                text = text[: match.start()].rstrip()
        # Strip lines starting with ">"
        lines = [
            line for line in text.splitlines()
            if not line.strip().startswith(">")
        ]
        return "\n".join(lines).strip()

    @staticmethod
    def _pii_mask(text: str) -> str:
        """Placeholder for future PII masking.

        Currently a no-op pass-through.  When PII masking becomes a
        requirement, this method is the single insertion point — nothing
        else in the pipeline needs to change.
        """
        return text
