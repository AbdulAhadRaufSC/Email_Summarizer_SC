"""Versioned, token-budgeted prompt builder.

Assembles a system message and a user message for the RunPod
worker-vllm OpenAI-compatible chat-completions route, which applies
Qwen2.5-7B-Instruct's own chat template server-side from a
``messages`` list -- this builder does not hand-template chat control
tokens itself (see ``domain/models.py::Prompt`` for why).

Truncation order when the prompt exceeds the budget:
1. Attachment text (all attachments' text dropped together)
2. Oldest conversation history, one email at a time
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

# Token counts here are an estimate for budgeting purposes only -- the
# real tokenization happens server-side via the model's own chat
# template and tokenizer. cl100k_base is close enough for that purpose.
_ENCODING = tiktoken.get_encoding("cl100k_base")

_SYSTEM_TEMPLATE = (Path(__file__).parent / "templates" / "v1" / "system.txt").read_text(
    encoding="utf-8"
)


def _count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text, disallowed_special=()))


class TemplatePromptBuilder:
    """Builds a versioned, token-budgeted system/user message pair.

    The JSON schema is embedded in the system message and also returned
    on the ``Prompt`` for the client to pass as ``guided_json``.
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
        system_tokens = _count_tokens(system_msg)

        user_msg = self._assemble_user_message(
            conversation, self._format_attachments(attachments)
        )

        # Reserve tokens for the model's output.
        max_input_tokens = context_budget - 2048

        current_tokens = system_tokens + _count_tokens(user_msg)
        if current_tokens > max_input_tokens:
            user_msg = self._truncate(conversation, attachments, max_input_tokens - system_tokens)
            current_tokens = system_tokens + _count_tokens(user_msg)

        logger.info(
            "Prompt built: ~%d tokens (budget: %d)",
            current_tokens, context_budget,
        )
        ###########
        logger.info(
            "Prompt: %s",
            Prompt(
                system_message=system_msg,
                user_message=user_msg,
                json_schema=self._json_schema,
                prompt_version=self._prompt_version,
                estimated_tokens=current_tokens,
        )
        )
        return Prompt(
            system_message=system_msg,
            user_message=user_msg,
            json_schema=self._json_schema,
            prompt_version=self._prompt_version,
            estimated_tokens=current_tokens,
        )

    def _assemble_user_message(
        self, conversation: NormalizedConversation, attachment_section: str
    ) -> str:
        parts = [
            f"## Ticket Subject\n{conversation.subject}",
            f"## Email Conversation ({len(conversation.emails)} emails)\n"
            f"{self._format_conversation(conversation)}",
        ]
        if attachment_section:
            parts.append(f"## Attachments\n{attachment_section}")
        return "\n\n".join(parts)

    def _truncate(
        self,
        conversation: NormalizedConversation,
        attachments: list[ExtractedAttachment],
        max_user_tokens: int,
    ) -> str:
        """Progressively truncate the user message to fit the budget.

        Order: drop attachment text first, then oldest emails.
        """
        logger.warning("Prompt exceeds budget, truncating")

        att_metadata = self._format_attachment_metadata_only(attachments)

        # Step 1: drop all attachment text, keep metadata only.
        user_msg = self._assemble_user_message(conversation, att_metadata)
        if _count_tokens(user_msg) <= max_user_tokens:
            return user_msg

        # Step 2: drop oldest emails one at a time.
        emails = list(conversation.emails)
        while len(emails) > 1 and _count_tokens(user_msg) > max_user_tokens:
            emails.pop(0)  # drop oldest
            trimmed = NormalizedConversation(subject=conversation.subject, emails=emails)
            parts = [
                f"## Ticket Subject\n{conversation.subject}",
                f"[Note: {len(conversation.emails) - len(emails)} oldest emails "
                f"omitted due to length]\n"
                f"## Email Conversation ({len(emails)} emails shown)\n"
                f"{self._format_conversation(trimmed)}",
            ]
            if att_metadata:
                parts.append(f"## Attachments (metadata only)\n{att_metadata}")
            user_msg = "\n\n".join(parts)

        return user_msg

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

    @staticmethod
    def _format_attachment_metadata_only(attachments: list[ExtractedAttachment]) -> str:
        return "\n".join(
            f"- {a.filename} ({a.mime_type}, {a.size} bytes) [text omitted due to length]"
            for a in attachments
        )
