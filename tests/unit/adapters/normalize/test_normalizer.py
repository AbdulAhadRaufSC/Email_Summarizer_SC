from __future__ import annotations

from summarizer.adapters.normalize.normalizer import DefaultThreadNormalizer
from summarizer.domain.models import RawEmail


def _email(**overrides: object) -> RawEmail:
    fields: dict[str, object] = {
        "message_id": "msg-1",
        "subject": "Cannot log in",
        "from_address": "cust@example.com",
        "from_name": "Customer One",
        "to_addresses": ["support@example.com"],
        "date": "2026-07-01",
        "text_body": "Hello, I cannot log in.",
        "latest_text_body": None,
        "html_body": None,
        "in_reply_to": None,
        "thread_id": "thread-1",
        **overrides,
    }
    return RawEmail(**fields)  # type: ignore[arg-type]


class TestEmptyAndSubject:
    def test_empty_list_returns_empty_conversation(self) -> None:
        result = DefaultThreadNormalizer().normalize([])
        assert result.subject == ""
        assert result.emails == []

    def test_subject_taken_from_first_email(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [_email(subject="First"), _email(message_id="msg-2", subject="Second")]
        )
        assert result.subject == "First"


class TestBodySelection:
    def test_prefers_latest_text_body_when_present(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [_email(latest_text_body="Just the new reply", text_body="Full quoted mess")]
        )
        assert result.emails[0].body == "Just the new reply"

    def test_falls_back_to_text_body_when_latest_missing(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [_email(latest_text_body=None, text_body="Only this is available")]
        )
        assert "Only this is available" in result.emails[0].body

    def test_email_with_no_body_at_all_is_dropped(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [_email(latest_text_body=None, text_body=None)]
        )
        assert result.emails == []

    def test_email_with_whitespace_only_body_is_dropped(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [_email(latest_text_body="   \n  ", text_body=None)]
        )
        assert result.emails == []


class TestHtmlFallback:
    """Agent replies composed in Stepping Desk's own editor have been
    observed to carry only ``mailBody`` (HTML) with no plain-text part
    at all, unlike real inbound customer email -- without a fallback,
    those emails were silently dropped (empty body), which is exactly
    what made the assembled prompt look customer-only."""

    def test_falls_back_to_html_body_when_no_plain_text_available(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [
                _email(
                    latest_text_body=None,
                    text_body=None,
                    html_body="<p>Please try clearing your browser cache.</p>",
                    from_address="agent@steppingcloud.com",
                    from_name="Support Agent",
                )
            ]
        )
        assert len(result.emails) == 1
        assert "Please try clearing your browser cache." in result.emails[0].body

    def test_html_fallback_preserves_block_boundaries(self) -> None:
        html = "<div>Hi there,</div><div>The fix has shipped.</div><p>Regards,<br>Support</p>"
        result = DefaultThreadNormalizer().normalize(
            [_email(latest_text_body=None, text_body=None, html_body=html)]
        )
        body = result.emails[0].body
        assert "Hi there," in body
        assert "The fix has shipped." in body
        # Block tags must not collapse adjacent text together.
        assert "there,The fix" not in body

    def test_html_fallback_still_goes_through_quote_stripping(self) -> None:
        html = (
            "<p>The reset link has been sent.</p>"
            "<blockquote>On Mon, Jul 1, 2026 at 9:00 AM, Customer &lt;cust@example.com&gt; wrote:"
            "<br>&gt; I can't log in.</blockquote>"
        )
        result = DefaultThreadNormalizer().normalize(
            [_email(latest_text_body=None, text_body=None, html_body=html)]
        )
        body = result.emails[0].body
        assert "The reset link has been sent." in body
        assert "I can't log in" not in body

    def test_both_text_and_html_missing_is_still_dropped(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [_email(latest_text_body=None, text_body=None, html_body=None)]
        )
        assert result.emails == []

    def test_plain_text_body_is_preferred_over_html(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [
                _email(
                    latest_text_body=None,
                    text_body="Plain text wins",
                    html_body="<p>HTML should not be used</p>",
                )
            ]
        )
        assert result.emails[0].body == "Plain text wins"


class TestQuoteStripping:
    def test_strips_on_wrote_quote_block_from_text_body(self) -> None:
        text = (
            "Thanks for the update, that resolved it.\n\n"
            "On Mon, Jul 1, 2026 at 9:00 AM, Support <support@example.com> wrote:\n"
            "> Please try resetting your password.\n"
            "> Let us know if that helps."
        )
        email = _email(latest_text_body=None, text_body=text)
        result = DefaultThreadNormalizer().normalize([email])
        body = result.emails[0].body
        assert "Please try resetting" not in body
        assert "Thanks for the update" in body

    def test_strips_gt_prefixed_quote_lines(self) -> None:
        text = "My new reply.\n> old quoted line one\n> old quoted line two"
        email = _email(latest_text_body=None, text_body=text)
        result = DefaultThreadNormalizer().normalize([email])
        body = result.emails[0].body
        assert "old quoted line" not in body
        assert "My new reply" in body


class TestSignatureStripping:
    def test_strips_sent_from_iphone_signature(self) -> None:
        text = "See attached logs.\nSent from my iPhone"
        result = DefaultThreadNormalizer().normalize([_email(latest_text_body=text)])
        body = result.emails[0].body
        assert "Sent from my iPhone" not in body
        assert "See attached logs" in body

    def test_strips_dash_dash_signature_delimiter(self) -> None:
        text = "Please advise.\n--\nJohn Doe\nSupport Engineer"
        result = DefaultThreadNormalizer().normalize([_email(latest_text_body=text)])
        body = result.emails[0].body
        assert "John Doe" not in body
        assert "Please advise" in body

    def test_strips_confidentiality_disclaimer(self) -> None:
        text = "The issue is fixed now.\nCONFIDENTIAL: This email may contain privileged info."
        result = DefaultThreadNormalizer().normalize([_email(latest_text_body=text)])
        body = result.emails[0].body
        assert "CONFIDENTIAL" not in body
        assert "issue is fixed" in body


class TestDeduplication:
    def test_identical_bodies_across_emails_are_deduplicated(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [
                _email(message_id="msg-1", latest_text_body="Same content here"),
                _email(message_id="msg-2", latest_text_body="Same content here"),
            ]
        )
        assert len(result.emails) == 1
        assert result.emails[0].message_id == "msg-1"

    def test_dedup_is_case_and_whitespace_insensitive(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [
                _email(message_id="msg-1", latest_text_body="Hello World"),
                _email(message_id="msg-2", latest_text_body="  hello world  "),
            ]
        )
        assert len(result.emails) == 1

    def test_distinct_bodies_are_both_kept(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [
                _email(message_id="msg-1", latest_text_body="First message"),
                _email(message_id="msg-2", latest_text_body="Second, different message"),
            ]
        )
        assert len(result.emails) == 2


class TestFieldMapping:
    def test_carries_sender_and_note_flag_through(self) -> None:
        result = DefaultThreadNormalizer().normalize(
            [
                _email(
                    latest_text_body="Internal note text",
                    from_address="agent@company.com",
                    from_name="Agent Smith",
                    is_note=True,
                )
            ]
        )
        email = result.emails[0]
        assert email.sender == "agent@company.com"
        assert email.sender_name == "Agent Smith"
        assert email.is_note is True
