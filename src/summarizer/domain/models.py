"""Domain data carriers used across the summarization pipeline.

These are plain frozen dataclasses — no I/O, no validation logic, no
framework dependencies.  They define the shapes that flow between ports
and are referenced by both the application layer and the adapters.

Design notes
------------
* ``RawEmail.is_note`` defaults to ``False`` because the Email API does
  not carry this flag — it originates from ``Email_Metadata.isNote`` in
  MySQL.  The orchestrator correlates it by calling
  ``dataclasses.replace(raw_email, is_note=ref.is_note)`` after fetching.
* ``LlmRawResponse`` token counts are optional because RunPod's
  serverless wrapper may not always expose them.
* List fields are intentionally ``list`` (not ``tuple``) to match the
  existing Pydantic style used in ``schema/v1.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Email retrieval ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EmailRef:
    """Lightweight reference to an email, from ``Email_Metadata``.

    Carries just enough for the gateway to fetch the full content and
    for the orchestrator to know the frontier ordering.
    """

    email_meta_id: int
    message_id: str
    is_note: bool


@dataclass(frozen=True, slots=True)
class RawAttachment:
    """An attachment as returned by the Email API, before extraction."""

    filename: str
    mime_type: str
    size: int
    attachment_id: str
    content_base64: str | None = None


@dataclass(frozen=True, slots=True)
class RawEmail:
    """A single email as returned by the Email API.

    ``latest_text_body`` is the newest reply portion only (no quoted
    history); ``text_body`` is the full accumulated text.  The
    normalizer decides which to prefer.
    """

    message_id: str
    subject: str
    from_address: str
    from_name: str | None
    to_addresses: list[str]
    date: str | None
    text_body: str | None
    latest_text_body: str | None
    html_body: str | None
    in_reply_to: str | None
    thread_id: str | None
    attachments: list[RawAttachment] = field(default_factory=list)
    is_note: bool = False


# ── Attachment extraction ────────────────────────────────────────────

# ExtractionStatus is defined in domain/schema/v1.py and re-used here
# to avoid duplicating the enum.  This is a domain-to-domain import,
# not a layer violation.
from summarizer.domain.schema.v1 import ExtractionStatus  # noqa: E402


@dataclass(frozen=True, slots=True)
class ExtractedAttachment:
    """Result of running a ``RawAttachment`` through the extractor.

    ``extraction_status`` is never a pipeline-level error — a failed
    extraction degrades to ``METADATA_ONLY`` / ``FAILED`` in place.
    """

    filename: str
    mime_type: str
    size: int
    extraction_status: ExtractionStatus
    extracted_text: str | None = None
    error_message: str | None = None


# ── Thread normalisation ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NormalizedEmail:
    """A single cleaned email after quote-stripping and signature removal."""

    message_id: str
    sender: str
    sender_name: str | None
    date: str | None
    body: str
    is_note: bool


@dataclass(frozen=True, slots=True)
class NormalizedConversation:
    """The full cleaned thread, ready for prompt assembly."""

    subject: str
    emails: list[NormalizedEmail]


# ── Prompt / LLM ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Prompt:
    """Assembled, token-budgeted prompt ready for the LLM.

    ``system_message`` / ``user_message`` rather than one pre-templated
    string: the RunPod endpoint is worker-vllm's OpenAI-compatible
    ``/openai/v1/chat/completions`` route, which applies the model's own
    chat template server-side from a ``messages`` list. Pre-baking
    ``<|im_start|>``-style markers into a flat string would either
    double-template or have the model see literal control-token text
    instead of real special tokens.

    ``json_schema`` is both embedded in ``system_message`` (so the model
    sees the target format) and passed separately for vLLM's
    ``guided_json`` constrained decoding.
    """

    system_message: str
    user_message: str
    json_schema: dict[str, Any]
    prompt_version: str
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class LlmRawResponse:
    """Raw text returned by the LLM, before validation.

    Token counts are optional because RunPod's serverless wrapper
    may not always expose usage statistics.
    """

    text: str
    token_input: int | None = None
    token_output: int | None = None
