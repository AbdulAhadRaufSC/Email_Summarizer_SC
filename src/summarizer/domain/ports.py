"""Port interfaces for the summarization pipeline, defined as Protocols
(structural typing -> trivial fakes in tests).

Every port the pipeline depends on is declared here.  Concrete
implementations live in ``adapters/`` and are wired in
``composition.py``.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from summarizer.domain.models import (
    EmailRef,
    ExtractedAttachment,
    LlmRawResponse,
    NormalizedConversation,
    Prompt,
    RawAttachment,
    RawEmail,
)
from summarizer.domain.schema.v1 import LlmSummaryOutput, SummaryDocument

# ── Enums ────────────────────────────────────────────────────────────


class WriteMode(StrEnum):
    APPEND_ONLY = "append_only"
    REPROCESS = "reprocess"


class WriteOutcome(StrEnum):
    WRITTEN = "written"
    SKIPPED_SUPERSEDED = "skipped_superseded"


class PersistedSummaryStatus(StrEnum):
    """The only two lifecycle states a summary row can actually hold.

    TRANSIENT_FAIL and TERMINAL_FAIL are never persisted to
    ticketAiSummary (see CLAUDE.md status lifecycle) -- narrowing the
    type here makes that invariant impossible to violate by accident.
    """

    OK = "OK"
    PARTIAL = "PARTIAL"


# ── DTOs ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SummaryWrite:
    """Everything one CAS write needs, beyond the ticket id and marker.

    Deviation from the `upsert(ticket_id, document, marker, mode)`
    signature sketched during the LLD conversation: that signature only
    carried `SummaryDocument`, which has no home for operational
    row-level metadata (latestMessageId, modelName/Version, promptVersion,
    status, timing, token counts, retryCount, errorMessage) that also has
    to land in the same atomic write. Bundling it here keeps `upsert` a
    single atomic round-trip instead of splitting into a content write
    plus a separate metrics write. See CLAUDE.md open questions.
    """

    document: SummaryDocument
    latest_message_id: str
    model_name: str
    model_version: str
    prompt_version: str
    status: PersistedSummaryStatus
    triggered_by: str | None = None
    processing_time_ms: int | None = None
    token_input: int | None = None
    token_output: int | None = None
    retry_count: int = 0
    error_message: str | None = None


# ── Port Protocols ───────────────────────────────────────────────────


class EmailMetadataRepository(Protocol):
    """Enumerates a ticket's emails from MySQL ``Email_Metadata``,
    ordered by ``emailMetaId`` ascending.  Excludes drafts and deleted
    rows; includes notes (``isNote=1``)."""

    def list_email_refs(self, ticket_id: int) -> list[EmailRef]: ...


class EmailGateway(Protocol):
    """Fetches full email content + attachments from the internal Email
    API.  Raises ``EmailNotYetAvailable`` (RYW gate) or
    ``EmailApiTransient`` on failure.

    ``ticket_id``, ``email_meta_id`` and ``thread_id`` are passed
    alongside ``message_id`` because the Email API now requires all of
    them as query parameters -- looking a message up by ``messageId``
    alone was occasionally returning an empty result (see CLAUDE.md's
    "real-data finding" for ticket 239907)."""

    def fetch_email(
        self,
        *,
        ticket_id: int,
        email_meta_id: int,
        message_id: str,
        thread_id: str | None,
    ) -> RawEmail: ...


class AttachmentExtractor(Protocol):
    """Extracts text from a raw attachment.  Sandboxed, size/time/ratio
    capped.  **Never raises** for one bad file — returns an
    ``ExtractedAttachment`` with status ``METADATA_ONLY`` or
    ``FAILED``."""

    def extract(self, attachment: RawAttachment) -> ExtractedAttachment: ...


class ThreadNormalizer(Protocol):
    """Cleans a list of raw emails into a normalised conversation:
    quote-stripping, deduplication, signature/disclaimer removal.
    PII-mask hook lives behind this interface for future use."""

    def normalize(self, emails: list[RawEmail]) -> NormalizedConversation: ...


class PromptBuilder(Protocol):
    """Builds a versioned, token-budgeted prompt from the normalised
    conversation and extracted attachments.  Outputs the JSON schema
    from ``LlmSummaryOutput.model_json_schema()`` for guided decoding."""

    @property
    def prompt_version(self) -> str: ...

    def build(
        self,
        conversation: NormalizedConversation,
        attachments: list[ExtractedAttachment],
        *,
        context_budget: int,
    ) -> Prompt: ...


class LLMClient(Protocol):
    """One guided-JSON call to RunPod/vLLM.  Raises ``LlmTransient`` on
    timeout/5xx.  Returns raw text and token usage."""

    model_name: str
    model_version: str

    def complete(self, prompt: Prompt) -> LlmRawResponse: ...


class Validator(Protocol):
    """Parses + schema-validates raw LLM output into
    ``LlmSummaryOutput``.  Raises ``LlmOutputInvalid`` on failure,
    which drives an app-level retry."""

    def validate(self, raw: LlmRawResponse) -> LlmSummaryOutput: ...


class SummaryRepository(Protocol):
    def get_frontier(self, ticket_id: int) -> int | None:
        """The stored emailMetaId CAS marker for a ticket, or None if no
        summary has ever been written for it."""
        ...

    def upsert(
        self,
        ticket_id: int,
        write: SummaryWrite,
        marker: int,
        mode: WriteMode,
    ) -> WriteOutcome:
        """APPEND_ONLY: write only if marker > stored frontier, else
        SKIPPED_SUPERSEDED. Used by both live traffic and DLQ redrives --
        a redrive carrying a superseded marker must be skipped, not
        force-applied (see CLAUDE.md CAS write strategy).

        REPROCESS: force-overwrite content; frontier becomes
        max(stored, marker) so it never regresses. Reserved for
        deliberate administrative operations (prompt/model upgrades,
        quality backfills) -- never used for the live append path or for
        DLQ redrive.
        """
        ...
