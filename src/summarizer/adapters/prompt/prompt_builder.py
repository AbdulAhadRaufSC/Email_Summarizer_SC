"""Versioned, token-budgeted prompt builder.

Assembles a system + user prompt for Qwen2.5-7B-Instruct, enforces the
context window (default 16384 tokens), and embeds the JSON schema for
guided decoding.

Truncation order when the prompt exceeds the budget:
1. Attachment text (oldest attachments first)
2. Oldest conversation history
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import tiktoken

from summarizer.domain.models import (
    ExtractedAttachment,
    NormalizedConversation,
    Prompt,
)
from summarizer.domain.schema.v1 import ExtractionStatus, LlmSummaryOutput

logger = logging.getLogger(__name__)

# The Qwen2.5 tokenizer is closest to cl100k_base for budget estimation.
_ENCODING = tiktoken.get_encoding("cl100k_base")

_SYSTEM_TEMPLATE = (Path(__file__).parent / "templates" / "v1" / "system.txt").read_text(
    encoding="utf-8"
)


def _count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text, disallowed_special=()))


class TemplatePromptBuilder:
    """Builds a versioned, token-budgeted prompt.

    The prompt is a single string formatted for Qwen2.5-Instruct's
    chat template (``<|im_start|>system...`` / ``<|im_start|>user...``).
    The JSON schema is embedded in the system message and also returned
    on the ``Prompt`` for guided decoding.
    """

    def __init__(self, prompt_version: str = "v1") -> None:
        self._prompt_version = prompt_version
        self._json_schema = LlmSummaryOutput.model_json_schema()

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def build(
        self,
        conversation: NormalizedConversation,
        attachments: list[ExtractedAttachment],
        *,
        context_budget: int,
    ) -> Prompt:
        schema_str = json.dumps(self._json_schema, indent=2)

        system_msg = _SYSTEM_TEMPLATE.replace("{{JSON_SCHEMA}}", schema_str)

        # Build conversation section.
        conv_lines = self._format_conversation(conversation)

        # Build attachment section.
        att_lines = self._format_attachments(attachments)

        # Assemble the user message.
        user_parts = [
            f"## Ticket Subject\n{conversation.subject}",
            f"## Email Conversation ({len(conversation.emails)} emails)\n{conv_lines}",
        ]
        if att_lines:
            user_parts.append(f"## Attachments\n{att_lines}")

        user_msg = "\n\n".join(user_parts)

        # Apply Qwen2.5-Instruct chat template.
        full_prompt = (
            f"<|im_start|>system\n{system_msg}<|im_end|>\n"
            f"<|im_start|>user\n{user_msg}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

        # Reserve tokens for the model's output.
        max_input_tokens = context_budget - 2048  # reserve for output

        # Truncate if over budget.
        current_tokens = _count_tokens(full_prompt)
        if current_tokens > max_input_tokens:
            full_prompt = self._truncate(
                system_msg, conversation, attachments, max_input_tokens,
            )
            current_tokens = _count_tokens(full_prompt)

        logger.info(
            "Prompt built: ~%d tokens (budget: %d)",
            current_tokens, context_budget,
        )

        return Prompt(
            text=full_prompt,
            json_schema=self._json_schema,
            prompt_version=self._prompt_version,
            estimated_tokens=current_tokens,
        )

    def _truncate(
        self,
        system_msg: str,
        conversation: NormalizedConversation,
        attachments: list[ExtractedAttachment],
        max_input_tokens: int,
    ) -> str:
        """Progressively truncate to fit the budget.

        Order: drop attachment text first, then oldest emails.
        """
        logger.warning("Prompt exceeds budget, truncating")

        # Step 1: Drop all attachment text.
        user_parts = [
            f"## Ticket Subject\n{conversation.subject}",
            f"## Email Conversation ({len(conversation.emails)} emails)\n"
            f"{self._format_conversation(conversation)}",
        ]
        # Only include attachment metadata, no text.
        att_metadata = "\n".join(
            f"- {a.filename} ({a.mime_type}, {a.size} bytes) [text omitted due to length]"
            for a in attachments
        )
        if att_metadata:
            user_parts.append(f"## Attachments (metadata only)\n{att_metadata}")

        user_msg = "\n\n".join(user_parts)
        full = (
            f"<|im_start|>system\n{system_msg}<|im_end|>\n"
            f"<|im_start|>user\n{user_msg}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

        if _count_tokens(full) <= max_input_tokens:
            return full

        # Step 2: Drop oldest emails one at a time.
        emails = list(conversation.emails)
        while len(emails) > 1 and _count_tokens(full) > max_input_tokens:
            emails.pop(0)  # drop oldest
            trimmed = NormalizedConversation(
                subject=conversation.subject, emails=emails,
            )
            user_parts_trimmed = [
                f"## Ticket Subject\n{conversation.subject}",
                f"[Note: {len(conversation.emails) - len(emails)} oldest emails "
                f"omitted due to length]\n"
                f"## Email Conversation ({len(emails)} emails shown)\n"
                f"{self._format_conversation(trimmed)}",
            ]
            if att_metadata:
                user_parts_trimmed.append(f"## Attachments (metadata only)\n{att_metadata}")

            user_msg = "\n\n".join(user_parts_trimmed)
            full = (
                f"<|im_start|>system\n{system_msg}<|im_end|>\n"
                f"<|im_start|>user\n{user_msg}<|im_end|>\n"
                f"<|im_start|>assistant\n"
            )

        return full

    @staticmethod
    def _format_conversation(conversation: NormalizedConversation) -> str:
        parts: list[str] = []
        for i, email in enumerate(conversation.emails, 1):
            sender = email.sender_name or email.sender
            label = "[Internal Note] " if email.is_note else ""
            date_str = f" ({email.date})" if email.date else ""
            parts.append(
                f"### Email {i}{date_str}\n"
                f"**From:** {sender}\n"
                f"{label}{email.body}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _format_attachments(attachments: list[ExtractedAttachment]) -> str:
        if not attachments:
            return ""

        parts: list[str] = []
        for att in attachments:
            if att.extraction_status == ExtractionStatus.EXTRACTED and att.extracted_text:
                parts.append(
                    f"### {att.filename} ({att.mime_type})\n"
                    f"{att.extracted_text}"
                )
            else:
                status = att.extraction_status.value
                parts.append(
                    f"- {att.filename} ({att.mime_type}, {att.size} bytes) "
                    f"[{status}]"
                )
        return "\n\n".join(parts)
