"""Canonical summary schema, version 1.

Split by provenance: `LlmSummaryOutput` is the guided-decoding + validation
target the LLM is responsible for. `SummaryDocument` is the enriched,
persisted form — it wraps validated LLM output with fields the orchestrator
fills in afterward (attachment extraction status, context completeness,
source counts). The LLM never asserts anything about its own grounding;
that keeps the split trustworthy.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

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


class ModuleName(StrEnum):
    """Stepping Desk's `module` dropdown (`DropdownConfig_Table`), as of
    the 2026-07-17 snapshot. "Unassigned" is deliberately excluded --
    `ClassificationHints.module` is already nullable, so `None` is the
    one way to say "not enough signal," rather than having two.
    """

    COMPENSATION = "Compensation"
    EC = "EC"
    EC_PAYROLL = "EC Payroll"
    LMS = "LMS"
    ONB = "ONB"
    PLATFORM = "Platform"
    PMGM = "PMGM"
    RCM = "RCM"
    REPORTING_AND_ANALYTICS = "Reporting & Analytics"
    RMK = "RMK"
    INTEGRATION = "Integration"
    PEOPLE_ANALYTICS = "People Analytics"
    EC_BENEFITS = "EC Benefits"
    TIME = "Time"
    OTHERS = "Others"
    PAYROLL_INPUT_BTP = "Payroll Input (BTP)"
    OFB = "OFB"
    QUALTRICS = "Qualtrics"
    TAGS = "Tags"


class PriorityLevel(StrEnum):
    """Stepping Desk's `priority` dropdown, as of the 2026-07-17
    snapshot. Note this list mixes what look like two different
    taxonomies (severity: Urgent/High/Medium/Low, and SLA response
    tiers: SR-1..SR-4) into one dropdown -- carried through as-is since
    splitting it would be a business-data change, not a code decision.
    """

    URGENT = "Urgent"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    SR_1 = "SR-1"
    SR_2 = "SR-2"
    SR_3 = "SR-3"
    SR_4 = "SR-4"


class RequestType(StrEnum):
    """Stepping Desk's `requestType` dropdown, as of the 2026-07-17
    snapshot. "No Category" is excluded for the same reason
    "Unassigned" is excluded from `ModuleName` -- `None` already covers
    it.
    """

    CHANGE_REQUEST = "Change Request"
    IMPLEMENTATION = "Implementation"
    INCIDENT_MANAGEMENT = "Incident Management"
    QUERY = "Query"
    L1_SUPPORT = "L1 Support"


class ClassificationHints(BaseModel):
    """Advisory classification suggestions -- populated starting prompt
    v2 (prompt v1 leaves this whole object null; see its system
    template). Advisory only: nothing in this pipeline auto-writes
    these into `Ticket`'s dropdown FK columns (categoryId/priorityId/
    requestTypeId/...) -- a human reviews the suggestion and applies it
    or not. Fields stay nullable for the same reason the rest of
    `LlmSummaryOutput` does: the model is never forced to guess when
    the conversation gives no basis for a value.

    `current_status` (above) is intentionally NOT redone against
    Stepping Desk's real ticket-status dropdown here, despite the
    overlap -- that vocabulary has no clean equivalent for
    `current_status`'s AWAITING_SUPPORT case, and conflating the LLM's
    conversational read with the ticket's formal workflow state was a
    decision explicitly deferred, not made, when this was added.
    """

    module: ModuleName | None = None
    priority: PriorityLevel | None = None
    request_type: RequestType | None = None


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

    @field_validator(
        "timeline", "resolution_attempts", "pending_actions", "keywords", mode="before"
    )
    @classmethod
    def _coerce_null_list_to_empty(cls, value: Any) -> Any:
        """vLLM's outlines guided-decoding backend isn't perfectly strict
        about excluding ``null`` from array-typed fields -- confirmed live
        2026-07-13, where the model emitted ``null`` for a "nothing to
        report" list field despite the guided-JSON schema declaring it as
        an array. Treat that the same as an empty list rather than
        failing validation and burning an app-level retry on it.
        """
        return [] if value is None else value


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
