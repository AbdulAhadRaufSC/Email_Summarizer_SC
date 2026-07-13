"""Port interfaces for the summarization pipeline, defined as Protocols
(structural typing -> trivial fakes in tests).

Only the pieces needed by modules built so far are declared here. The
other ports named in CLAUDE.md (EmailMetadataRepository, EmailGateway,
AttachmentExtractor, ThreadNormalizer, PromptBuilder, LLMClient,
Validator) get added as their own modules are implemented, so this file
never references domain/models.py types that don't exist yet.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from summarizer.domain.schema.v1 import SummaryDocument


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
