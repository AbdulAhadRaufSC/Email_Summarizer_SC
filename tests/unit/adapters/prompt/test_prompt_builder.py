from __future__ import annotations

from summarizer.adapters.prompt.prompt_builder import TemplatePromptBuilder
from summarizer.domain.models import ExtractedAttachment, NormalizedConversation, NormalizedEmail
from summarizer.domain.schema.v1 import ExtractionStatus, LlmSummaryOutput


def _conversation(
    n_emails: int = 2, body: str = "Some conversation body."
) -> NormalizedConversation:
    emails = [
        NormalizedEmail(
            message_id=f"msg-{i}",
            sender="cust@example.com",
            sender_name="Customer One",
            date="2026-07-0" + str(i + 1),
            body=f"{body} (email {i})",
            is_note=False,
        )
        for i in range(n_emails)
    ]
    return NormalizedConversation(subject="Cannot log in", emails=emails)


class TestBuildHappyPath:
    def test_includes_subject_and_conversation(self) -> None:
        builder = TemplatePromptBuilder()
        prompt = builder.build(_conversation(), [], context_budget=16384)

        assert "Cannot log in" in prompt.text
        assert "email 0" in prompt.text
        assert "email 1" in prompt.text

    def test_uses_qwen_chat_template_markers(self) -> None:
        builder = TemplatePromptBuilder()
        prompt = builder.build(_conversation(), [], context_budget=16384)

        assert prompt.text.startswith("<|im_start|>system\n")
        assert "<|im_start|>user\n" in prompt.text
        assert prompt.text.rstrip().endswith("<|im_start|>assistant")

    def test_embeds_json_schema_matching_llm_summary_output(self) -> None:
        builder = TemplatePromptBuilder()
        prompt = builder.build(_conversation(), [], context_budget=16384)

        assert prompt.json_schema == LlmSummaryOutput.model_json_schema()
        assert "human_summary" in prompt.text

    def test_prompt_version_property_and_field_match(self) -> None:
        builder = TemplatePromptBuilder(prompt_version="v2-test")
        assert builder.prompt_version == "v2-test"

        prompt = builder.build(_conversation(), [], context_budget=16384)
        assert prompt.prompt_version == "v2-test"

    def test_estimated_tokens_is_positive_and_reasonable(self) -> None:
        builder = TemplatePromptBuilder()
        prompt = builder.build(_conversation(), [], context_budget=16384)

        assert prompt.estimated_tokens > 0
        assert prompt.estimated_tokens < 16384 - 2048


class TestAttachmentFormatting:
    def test_extracted_attachment_text_is_included(self) -> None:
        attachments = [
            ExtractedAttachment(
                filename="log.txt",
                mime_type="text/plain",
                size=10,
                extraction_status=ExtractionStatus.EXTRACTED,
                extracted_text="ERROR: disk full",
            )
        ]
        builder = TemplatePromptBuilder()
        prompt = builder.build(_conversation(), attachments, context_budget=16384)

        assert "ERROR: disk full" in prompt.text
        assert "log.txt" in prompt.text

    def test_metadata_only_attachment_shows_status_not_text(self) -> None:
        attachments = [
            ExtractedAttachment(
                filename="photo.png",
                mime_type="image/png",
                size=2048,
                extraction_status=ExtractionStatus.METADATA_ONLY,
            )
        ]
        builder = TemplatePromptBuilder()
        prompt = builder.build(_conversation(), attachments, context_budget=16384)

        assert "photo.png" in prompt.text
        assert "metadata_only" in prompt.text

    def test_no_attachments_omits_attachments_section(self) -> None:
        builder = TemplatePromptBuilder()
        prompt = builder.build(_conversation(), [], context_budget=16384)

        assert "## Attachments" not in prompt.text


class TestTruncation:
    def test_drops_attachment_text_before_email_history(self) -> None:
        # A budget too small for everything but large enough for the
        # conversation alone once attachment text is dropped.
        long_attachment_text = "attachment filler text. " * 2000
        conversation = _conversation(n_emails=2, body="short body")
        attachments = [
            ExtractedAttachment(
                filename="huge.txt",
                mime_type="text/plain",
                size=len(long_attachment_text),
                extraction_status=ExtractionStatus.EXTRACTED,
                extracted_text=long_attachment_text,
            )
        ]
        builder = TemplatePromptBuilder()
        # budget large enough for conversation + overhead, too small for
        # conversation + full attachment text.
        prompt = builder.build(conversation, attachments, context_budget=2048 + 1700)

        assert "attachment filler text" not in prompt.text
        assert "huge.txt" in prompt.text  # metadata retained
        assert "email 0" in prompt.text  # conversation preserved
        assert "email 1" in prompt.text

    def test_drops_oldest_emails_when_still_over_budget(self) -> None:
        # Enough emails that even after dropping attachments, the
        # conversation itself must be trimmed from the oldest end.
        long_body = "conversation filler text. " * 200
        conversation = _conversation(n_emails=10, body=long_body)

        builder = TemplatePromptBuilder()
        prompt = builder.build(conversation, [], context_budget=2048 + 600)

        assert "omitted due to length" in prompt.text
        # The newest email should survive; earliest ones should be gone.
        assert "email 9" in prompt.text
        assert "email 0" not in prompt.text

    def test_truncated_prompt_still_within_budget_or_minimal(self) -> None:
        long_body = "conversation filler text. " * 200
        conversation = _conversation(n_emails=10, body=long_body)

        builder = TemplatePromptBuilder()
        budget = 2048 + 3000
        prompt = builder.build(conversation, [], context_budget=budget)

        # Either it fits, or we're down to a single (still oversized) email
        # and can't trim further -- both are acceptable, never silently
        # dropping the newest/triggering email.
        assert prompt.estimated_tokens <= (budget - 2048) or "email 9" in prompt.text
