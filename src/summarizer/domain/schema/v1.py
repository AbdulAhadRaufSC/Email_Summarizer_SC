"""Canonical summary schema, version 1.

Split by provenance: `LlmSummaryOutput` is the guided-decoding + validation
target the LLM is responsible for. `SummaryDocument` is the enriched,
persisted form — it wraps validated LLM output with fields the orchestrator
fills in afterward (attachment extraction status, context completeness,
source counts). The LLM never asserts anything about its own grounding;
that keeps the split trustworthy.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class TicketStatus(StrEnum):
    OPEN = "open"
    AWAITING_CUSTOMER = "awaiting_customer"
    AWAITING_SUPPORT = "awaiting_support"
    RESOLVED = "resolved"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Actor(StrEnum):
    CUSTOMER = "customer"
    SUPPORT = "support"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class TimelineEvent(BaseModel):
    description: str
    date: str | None = None
    actor: Actor = Actor.UNKNOWN


class PendingAction(BaseModel):
    description: str
    owner: Actor = Actor.UNKNOWN


class ClassificationHints(BaseModel):
    """Reserved for a future promptVersion or dedicated classifier.

    Not populated by prompt v1: a generative model can't reliably classify
    into a taxonomy it hasn't been given, so the shape exists now to avoid
    a schema migration later, without forcing the model to guess today.
    """

    module: str | None = None
    category: str | None = None
    priority: str | None = None
    suggested_team: str | None = None


class LlmSummaryOutput(BaseModel):
    """Produced by the LLM. Also the source of the vLLM guided-decoding
    JSON schema via `LlmSummaryOutput.model_json_schema()`.
    """

    model_config = {"extra": "forbid"}

    # Required core — prompt v1 elicits these reliably.
    human_summary: str = Field(min_length=1)
    customer_issue: str = Field(min_length=1)
    current_status: TicketStatus

    # Best-effort — nullable/defaulted so the model is never forced to invent.
    executive_summary: str | None = None
    business_impact: str | None = None
    timeline: list[TimelineEvent] = Field(default_factory=list)
    resolution_attempts: list[str] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    final_resolution: str | None = None
    keywords: list[str] = Field(default_factory=list)
    classification: ClassificationHints | None = None


class ExtractionStatus(StrEnum):
    EXTRACTED = "extracted"
    METADATA_ONLY = "metadata_only"
    FAILED = "failed"


class AttachmentRef(BaseModel):
    filename: str
    mime_type: str
    extraction_status: ExtractionStatus
    text_summary: str | None = None


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ContextCompleteness(BaseModel):
    """The structured record behind the PARTIAL status."""

    status: CompletenessStatus
    missing: list[str] = Field(default_factory=list)


class SourceInfo(BaseModel):
    email_count: int
    frontier_email_meta_id: int


class SummaryDocument(BaseModel):
    """Persisted to `ticketAiSummary.summaryJson`."""

    schema_version: str = SCHEMA_VERSION
    content: LlmSummaryOutput
    attachments: list[AttachmentRef] = Field(default_factory=list)
    context_completeness: ContextCompleteness
    source: SourceInfo
