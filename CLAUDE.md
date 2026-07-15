# Stepping Desk — AI Email Summarization Platform (Phase 1)

This file is the authoritative, maintained record of the architecture for this project. It was distilled on 2026-07-10 from a full HLD/LLD design conversation pasted verbatim into `CONTEXT.txt` (kept only as historical record — do not treat it as more current than this file). Update this file as decisions change; do not let it drift from the code.

## Working agreement

The user acts as product owner and final decision-maker but engages at a principal-engineer level. Collaboration rules, set explicitly at project kickoff:

- Challenge weak architectural decisions and explain why. Do not agree by default.
- When multiple approaches exist, compare tradeoffs explicitly before recommending one.
- Implement **one module at a time**. Never generate a whole project/multiple modules in one response. Wait for approval before continuing to the next.
- Always explain WHY, not just what.
- Ask before assuming when requirements are ambiguous.
- Follow SOLID, Clean/Hexagonal Architecture, event-driven design, idempotent processing, DI, testability, observability, and security-first practices throughout.

## Business context

- Enterprise ticketing system **Stepping Desk** (MySQL DB `TrackEaseV2DB`) needs AI email summarization as Phase 1 of a longer roadmap toward an AI-powered Service Desk.
- Future roadmap (not Phase 1, but every design decision should not preclude it): semantic search, vector DB, RAG, similar-ticket search, auto-reply suggestions, email classification, module/department/priority/SLA prediction, auto-assignment, AI support assistant.
- Volume: currently <100 tickets/day, 1–20 emails/ticket. ~40,000 historical tickets to be backfilled later. Design for future volume growth.
- Latency: relaxed, ~1 minute acceptable. No real-time requirement.
- Ticket history is immutable; emails are never edited.
- No HIPAA/GDPR mandate currently. Security focus is access control + secure/minimal logging rather than pre-inference PII masking, because the LLM is self-hosted on the org's own RunPod infra (no third-party data exfiltration concern) — this may need revisiting if that changes.

## Existing infrastructure

```
Stepping Desk --(ticket created/updated)--> Amazon SQS --> Python worker
```

- **Trigger**: one SQS event per new email associated with a ticket (creation or reply). Message contains `ticketId`, `emailMetaId`, `messageId`.
- **Email storage**: bodies/attachments are NOT in MySQL. Amazon S3 stores complete HTML, plain text, and attachments. An internal **Email API** is the single source of truth — returns subject, sender, recipients, HTML body, plain text body, message ID, parent message ID, thread ID, attachments, and base64 attachment contents. **As of 2026-07-15, lookup is no longer by `messageId` alone** — the API requires query parameters `companyId` (static, always `"steppingcloud"`), `ticketId`, `emailMetaId`, `messageId`, and `threadId`. This was a fix for the empty-`[]`-response finding (ticket 239907, see below): messageId-only lookups were occasionally returning an empty array for emails that do in fact exist; the fuller set of identifiers disambiguates. `EmailGateway.fetch_email` takes all four (`companyId` is hardcoded in the adapter, not passed by callers).
- **LLM**: Qwen2.5-7B-Instruct, self-hosted on **RunPod Serverless** via **vLLM**, RTX 4090 (24GB VRAM). vLLM guided/constrained JSON decoding enforces the output schema. Cold starts are acceptable (async, SQS-driven). No secondary/fallback LLM provider — RunPod downtime is handled via standard SQS retry.
- **Compute for the worker**: ECS/Fargate or EC2 (Lambda ruled out — worker is CPU/IO-bound with potentially long attachment-parsing + LLM-call durations).

## Database schema

### Existing tables (do not modify structurally without a migration plan)

```sql
CREATE TABLE `Email_Metadata` (
  `emailMetaId` int NOT NULL AUTO_INCREMENT,
  `subject` varchar(300) DEFAULT NULL,
  `inReplyTo` varchar(200) DEFAULT NULL,
  `messageId` varchar(200) DEFAULT NULL,
  `parentMessageId` varchar(200) DEFAULT NULL,
  `seqno` int DEFAULT NULL,
  `ticketId` int DEFAULT NULL,
  `threadId` varchar(255) DEFAULT NULL,
  `isNote` tinyint(1) DEFAULT '0',
  `isDraft` tinyint(1) DEFAULT '0',
  `mailContentAsText` varchar(300) DEFAULT NULL,
  `createdBy` int DEFAULT NULL,
  `createdOn` datetime DEFAULT CURRENT_TIMESTAMP,
  `senderId` int NOT NULL,
  `isDeleted` tinyint(1) DEFAULT '0',
  `gId` text,
  `reference` text,
  `threadTopic` varchar(255) DEFAULT NULL,
  `threadIndex` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`emailMetaId`),
  KEY `Email_Metadata_FK` (`createdBy`),
  KEY `idx_em_ticket_draft_deleted_meta` (`ticketId`,`isDraft`,`isDeleted`)
) ENGINE=InnoDB AUTO_INCREMENT=134036 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `Ticket` (
  `ticketId` int NOT NULL AUTO_INCREMENT,
  `threadId` varchar(255) DEFAULT NULL,
  `companyId` varchar(50) DEFAULT NULL,
  `subject` varchar(500) DEFAULT NULL,
  `source` varchar(50) DEFAULT NULL,
  `byMessageId` varchar(100) DEFAULT NULL,
  `createdOn` datetime DEFAULT CURRENT_TIMESTAMP,
  `lastModifiedOn` datetime DEFAULT NULL,
  `groupId` int DEFAULT NULL,
  `agentId` int DEFAULT NULL,
  `coAgentId` int DEFAULT NULL,
  `effortHrs` float DEFAULT NULL,
  `revisedEffortHrs` int DEFAULT NULL,
  `isApprovedEffortHrs` varchar(25) DEFAULT NULL,
  `resolutionDueBy` datetime DEFAULT NULL,
  `resolvedAt` datetime DEFAULT NULL,
  `closedAt` datetime DEFAULT NULL,
  `firstResponseDueBy` datetime DEFAULT NULL,
  `firstResponseAt` datetime DEFAULT NULL,
  `isMergedAs` varchar(15) DEFAULT NULL,
  `isTemporary` tinyint(1) DEFAULT '0',
  `categoryId` int DEFAULT NULL,
  `crStatusId` int DEFAULT NULL,
  `priorityId` int DEFAULT NULL,
  `requestTypeId` int DEFAULT NULL,
  `requestStatusId` int DEFAULT NULL,
  `senderId` int DEFAULT NULL,
  `isDeleted` tinyint(1) NOT NULL DEFAULT '0',
  `responseSLAStatus` varchar(100) DEFAULT NULL,
  `resolutionSLAStatus` varchar(100) DEFAULT NULL,
  PRIMARY KEY (`ticketId`),
  KEY `Ticket_FK` (`senderId`),
  -- ... categoryId/crStatusId/priorityId/requestStatusId/requestTypeId FKs to DropdownConfig_Table
) ENGINE=InnoDB AUTO_INCREMENT=239905 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

`isMergedAs` on `Ticket` implies merge/split concepts exist in the schema, but **per the user, merges of multiple existing tickets into one are NOT currently supported** — splits are (a split spawns a completely independent new ticket with no inherited summary). This is load-bearing: it's what keeps the CAS marker strategy below valid. If merge support is ever added, the CAS strategy must be revisited (see Open Questions).

### `ticketAiSummary` — draft table, Phase 1 target shape

Original draft (from `CONTEXT.txt`) plus two additions agreed during LLD: **`emailMetaId`** (FK to `Email_Metadata`, doubles as the CAS marker) and **`summaryJson`** (nullable, versioned structured envelope). Confirm in code/migration whether `emailMetaId` already exists on the live table before writing the repository adapter — this was still an open item as of the last design conversation.

```sql
CREATE TABLE `ticketAiSummary` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `ticketId` bigint NOT NULL,
  `emailMetaId` bigint NOT NULL,        -- CAS marker + FK to Email_Metadata (frontier email)
  `latestMessageId` varchar(255) NOT NULL,
  `summary` longtext,                    -- denormalized human_summary for UI reads without JSON parsing
  `summaryJson` json DEFAULT NULL,       -- versioned SummaryDocument envelope (see canonical schema)
  `modelName` varchar(100) DEFAULT NULL,
  `modelVersion` varchar(50) DEFAULT NULL,
  `promptVersion` varchar(50) DEFAULT NULL,
  `summaryStatus` varchar(50) DEFAULT NULL,   -- OK | PARTIAL | TRANSIENT_FAIL | TERMINAL_FAIL
  `triggeredBy` varchar(250) DEFAULT NULL,
  `processingTimeMs` int DEFAULT NULL,
  `tokenInput` int DEFAULT NULL,
  `tokenOutput` int DEFAULT NULL,
  `retryCount` int DEFAULT '0',          -- APP-LEVEL retries only (LLM/validation), NOT SQS receive count
  `errorMessage` varchar(500) DEFAULT NULL,
  `createdAt` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updatedAt` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_ticketId` (`ticketId`),  -- one row per ticket
  KEY `idxTicketId` (`ticketId`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

## Locked architecture decisions

### Event flow (happy path)

```
SQS message {ticketId, emailMetaId, messageId}
  -> idempotency/frontier check (skip if emailMetaId <= stored frontier, under append-only mode)
  -> enumerate ticket's emails via Email_Metadata (ordered by emailMetaId)
  -> Read-Your-Writes gate: verify triggering messageId is fetchable via Email API
       -> not yet available => transient error, let SQS redeliver (do NOT summarize an incomplete thread)
  -> fetch all emails, bounded concurrency, via Email API
  -> extract attachment text (sandboxed)
  -> normalize/clean thread (quote-strip, dedup, drop signatures/disclaimers)
  -> build versioned, token-budgeted prompt
  -> call Qwen2.5-7B on RunPod/vLLM with guided JSON decoding
  -> validate response (Pydantic), retry on invalid up to N app-level retries
  -> enrich into SummaryDocument (system fields: attachment status, completeness, source info)
  -> compare-and-set upsert into ticketAiSummary
  -> ack
```

### Queueing

- Standard SQS (not FIFO — a DB-level CAS guard protects correctness regardless of ordering, so FIFO's per-group ordering wasn't needed).
- DLQ on exhausted retries. Alarm on DLQ depth > 0. Redrive runbook required.
- **Two separate queues**: live/real-time traffic, and a **dedicated backfill queue** (rate-limited producer, bounded consumer concurrency) for the ~40k historical backfill, so batch processing never starves live traffic. Both queues drive the same worker logic (reused, not duplicated). Autoscaling target: `ApproximateNumberOfMessagesVisible` per queue.
- SQS visibility timeout must comfortably exceed worst-case (Email API retrieval + attachment parsing + RunPod cold start + inference + validation retries + DB write), to avoid mid-flight redelivery. CAS makes duplicate delivery safe, just wasteful.

### Compare-and-set (CAS) write strategy — the core correctness mechanism

`emailMetaId` is both the FK to the frontier email and the CAS marker — no separate timestamp column needed.

- **Append-only (default, live traffic AND DLQ redrives)**: write succeeds only if `incoming.emailMetaId > stored.emailMetaId`; otherwise skip as superseded. **DLQ redrives must use this same path, not reprocess mode** — a redrive can carry a stale/superseded `emailMetaId` (the ticket may have advanced further while the message sat in the DLQ), and force-overwriting would clobber a newer, correct summary with older content. This was a specifically-caught edge case; do not "fix" DLQ redrive by routing it through reprocess mode.
- **Reprocess mode (administrative only — prompt improvements, model upgrades, quality backfills, deliberate re-runs of the current frontier)**: force-overwrites the summary content but the frontier marker must never move backward: `frontier := GREATEST(stored.emailMetaId, incoming.emailMetaId)`.
- This single mechanism also makes the 40k backfill idempotent/resumable for free (skip-if-current).
- Modeled in code as `WriteMode.APPEND_ONLY` vs `WriteMode.REPROCESS` (enum, not a boolean flag) so the distinction can't be violated by accident.

### Status lifecycle

- **OK** — all emails and supported attachments retrieved.
- **PARTIAL** — core thread + triggering email reconstructed, but some email/attachment permanently failed. Summary explicitly notes what's missing (`context_completeness.missing`). Still a valid, useful summary — written and treated as success.
- **TRANSIENT_FAIL** — Email API 5xx, RunPod cold-start timeout, MySQL blip, etc. Left to fail; SQS redelivers with backoff via `maxReceiveCount`. No summary is written for this state.
- **TERMINAL_FAIL** — core conversation or triggering email unreconstructable, or a poison message. Retries exhausted -> DLQ.
- Per-attachment failures are **never** a pipeline failure — they degrade in place to `METADATA_ONLY`/`FAILED` in `context_completeness` / `AttachmentRef`.

### Attachments

- Text extraction (Phase 1 scope): **PDF, DOCX, TXT, CSV, XLSX, and images (OCR)**.
- Metadata-only (filename, MIME type, size — no content extraction): EML, unknown/unsupported types.
- Untrusted-input hardening required: sandboxed/resource-limited extraction subprocess, hard per-file size + wall-clock timeout caps, decompression-ratio caps (zip-bomb defense for XLSX/DOCX), disabled external XML entity resolution (billion-laughs defense), row/cell caps for XLSX. A single bad attachment must never crash or fail the whole ticket — always fall back to metadata-only.
- **Images were originally out of scope** ("Metadata-only... images, EML, unknown/unsupported types" — a deliberate call made in the original HLD conversation in `CONTEXT.txt`, on the reasoning that OCR/vision was disproportionate complexity for Phase 1). **Reopened 2026-07-13** at the user's request, since real tickets do carry screenshots (error dialogs, UI states) that are often the most important context in the thread. Implemented as **local OCR** (`pytesseract` + `Pillow`, via the Tesseract engine) inside the same sandboxed extractor as the other formats — not a vision-capable LLM call, which was considered and rejected for Phase 1 since it would mean provisioning a second RunPod endpoint/model (Qwen2.5-7B-Instruct is text-only). **Deployment note: this needs the `tesseract-ocr` system package on the worker's PATH — `uv sync` alone does not provide it.** Must be added to whatever Docker image / EC2 AMI ships the worker (e.g. `apt-get install tesseract-ocr` on Debian/Ubuntu); local dev without it degrades every image attachment to `FAILED` (not `METADATA_ONLY` — pytesseract's `TesseractNotFoundError` is treated like any other extraction failure) rather than crashing. Pillow's default `Image.MAX_IMAGE_PIXELS` guard covers decompression-bomb defense for images the same way the existing ratio cap covers XLSX/DOCX — no extra code needed there.

### Prompt / conversation assembly

- **No incremental/delta summarization.** Always re-summarize the full thread on every event. Rejected as unnecessary complexity at current volume (<100 tickets/day, ≤20 emails/ticket).
- Thread normalization strips duplicated quoted replies, signatures, disclaimers — both a token-budget necessity (quoted chains are O(n²) across a thread) and a quality improvement (duplicated text degrades LLM output).
- Token budget is enforced; if the assembled prompt would exceed the model's context window, degrade in this order: drop attachment text first, then oldest quoted history. `vLLM --max-model-len` is a configured value, not hardcoded (exact value was still TBD as of the last design session — treat as config).
- No chunking / hierarchical (map-reduce) summarization in Phase 1 — a single inference call is expected to be sufficient at current thread lengths.

### Classification — explicitly out of scope for Phase 1 generation

Module/Category/Priority/Team prediction are **not** produced by the LLM call in Phase 1. Rationale: a generative model can't reliably classify into a taxonomy it hasn't been given, and forcing enum guesses now would pollute the future knowledge base with low-trust fields. The schema reserves a `classification` field (see below) but prompt v1 does not elicit it — population waits for a controlled vocabulary and either a later prompt version or a dedicated classifier.

### Embeddings / RAG boundary — deferred, not inline

- Embedding generation is explicitly **not** a stage inside the summarization worker — keeps summarization latency/availability decoupled from the embedding model, and means re-embedding never forces re-summarization.
- Extension point is the `SummaryRepository` port boundary (a decorator seam), not scattered publish calls in the orchestration core.
- **Phase 1 does NOT implement a transactional outbox / `SummaryUpserted` event.** This is a deliberate, safe deferral: because every summary is persisted with its frontier marker (`emailMetaId`) and metadata (`promptVersion`, `modelVersion`, `schema_version`), a future vector-index build can bootstrap by **replaying committed summaries directly from the DB** — it does not depend on historical events that were never emitted. When embeddings become an active requirement, either introduce the outbox then, or just replay from the table.

## Canonical Summary schema (LLD — locked)

Design principle: split the document **by provenance**. The LLM only produces what it can extract from the conversation; facts the LLM has no business asserting (which attachments parsed, what context is missing) are filled in by the orchestrator afterward. This keeps grounding intact (a 7B model can't hallucinate an extraction status it never saw).

```python
# domain/schema/v1.py — Pydantic v2. This model is both the validation
# contract AND the source of the vLLM guided-decoding JSON schema
# (LlmSummaryOutput.model_json_schema()).
from enum import Enum
from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

class TicketStatus(str, Enum):
    OPEN = "open"; AWAITING_CUSTOMER = "awaiting_customer"
    AWAITING_SUPPORT = "awaiting_support"; RESOLVED = "resolved"
    CLOSED = "closed"; UNKNOWN = "unknown"        # UNKNOWN = never forced to guess

class Actor(str, Enum):
    CUSTOMER = "customer"; SUPPORT = "support"
    SYSTEM = "system"; UNKNOWN = "unknown"

class TimelineEvent(BaseModel):
    description: str
    date: str | None = None                        # ISO date only if stated in-thread
    actor: Actor = Actor.UNKNOWN

class PendingAction(BaseModel):
    description: str
    owner: Actor = Actor.UNKNOWN

class ClassificationHints(BaseModel):              # RESERVED — not populated by prompt v1
    module: str | None = None
    category: str | None = None
    priority: str | None = None
    suggested_team: str | None = None

class LlmSummaryOutput(BaseModel):                  # <-- produced by the LLM
    model_config = {"extra": "forbid"}

    # Required core — prompt v1 elicits these reliably
    human_summary: str = Field(min_length=1)        # the field surfaced in the UI
    customer_issue: str = Field(min_length=1)
    current_status: TicketStatus

    # Best-effort — nullable/defaulted so the model is never forced to invent
    executive_summary: str | None = None            # 1-line TL;DR, good for embedding
    business_impact: str | None = None
    timeline: list[TimelineEvent] = Field(default_factory=list)
    resolution_attempts: list[str] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    final_resolution: str | None = None
    keywords: list[str] = Field(default_factory=list)   # hybrid/semantic search
    classification: ClassificationHints | None = None   # reserved, not elicited in v1
```

```python
# System-produced fields, merged in by the orchestrator after validation — never LLM output.
class ExtractionStatus(str, Enum):
    EXTRACTED = "extracted"; METADATA_ONLY = "metadata_only"; FAILED = "failed"

class AttachmentRef(BaseModel):
    filename: str
    mime_type: str
    extraction_status: ExtractionStatus
    text_summary: str | None = None

class CompletenessStatus(str, Enum):
    COMPLETE = "complete"; PARTIAL = "partial"

class ContextCompleteness(BaseModel):               # the structured record behind PARTIAL
    status: CompletenessStatus
    missing: list[str] = Field(default_factory=list)   # e.g. "email <messageId> unretrievable"

class SourceInfo(BaseModel):
    email_count: int
    frontier_email_meta_id: int                     # mirrors the DB CAS marker inside the JSON

class SummaryDocument(BaseModel):                    # <-- persisted to summaryJson
    schema_version: str = SCHEMA_VERSION            # self-describing when it travels to the vector store
    content: LlmSummaryOutput                        # LLM-produced (validated)
    attachments: list[AttachmentRef] = Field(default_factory=list)   # system
    context_completeness: ContextCompleteness                        # system
    source: SourceInfo                                               # system
```

Versioning: `schema_version` pins the structure, `promptVersion` pins the elicitation (instructions + which fields are actively requested), `modelName`/`modelVersion` pin the generator. Every row carries all of these so any stored summary is fully interpretable and reproducible in isolation.

## Folder structure (hexagonal / clean architecture)

Dependency rule: `domain` imports nothing from outer layers; `application` depends only on `domain`; `adapters`/`entrypoints` depend inward. Concrete implementations are named in exactly one place: `composition.py`.

```
src/summarizer/
├── domain/                      # pure: no I/O, no framework, no SDKs
│   ├── models.py                # Conversation, RawEmail, RawAttachment, ExtractedAttachment,
│   │                            #   NormalizedConversation, Prompt, EmailRef, ...
│   ├── schema/
│   │   ├── v1.py                # LlmSummaryOutput, SummaryDocument (above)
│   │   └── registry.py          # schema_version -> model, for forward-compat parsing
│   ├── ports.py                 # Protocol interfaces (below)
│   └── errors.py                # TransientError / TerminalError hierarchy
├── application/
│   ├── summarize_ticket.py      # SummarizeTicket use-case (the orchestrator)
│   ├── command.py               # SummarizeTicketCommand(ticket_id, email_meta_id, message_id)
│   └── result.py                # SummaryResult(status, timing, tokens, retry_count)
├── adapters/
│   ├── email/                   # MySqlEmailMetadataRepository, HttpEmailGateway
│   ├── extraction/               # SandboxedExtractor + per-type (pdf/docx/xlsx/csv/txt) + sandbox runner
│   ├── normalize/                # DefaultThreadNormalizer (quote-strip, PII hook)
│   ├── prompt/                   # TemplatePromptBuilder + templates/v1/
│   ├── llm/                      # RunpodVllmClient (guided JSON, timeout)
│   ├── validation/                # PydanticValidator
│   └── persistence/               # MySqlSummaryRepository (CAS) + outbox_decorator.py (Phase-2 seam)
├── config/                      # settings.py (pydantic-settings, env/SSM), logging.py
├── observability/                # metrics.py, correlation context
├── entrypoints/                  # sqs_consumer.py, backfill.py  (thin)
└── composition.py                # composition root — the only place concretes are wired
tests/ {unit, integration, e2e, fixtures}   # fixtures/ holds synthetic test tickets (no real prod data available)
```

## Ports (Protocols)

```python
# domain/ports.py
from typing import Protocol
from enum import Enum

class EmailMetadataRepository(Protocol):
    def list_email_refs(self, ticket_id: int) -> list[EmailRef]: ...
    # enumerate a ticket's emails from MySQL Email_Metadata, ordered by emailMetaId

class EmailGateway(Protocol):
    def fetch_email(
        self, *, ticket_id: int, email_meta_id: int, message_id: str, thread_id: str | None
    ) -> RawEmail: ...
    # full content + attachments from the internal Email API;
    # raises EmailNotYetAvailable (RYW) or EmailApiTransient
    # (see "Existing infrastructure" above: lookup is by the full
    #  identifier set + static companyId, not messageId alone)

class AttachmentExtractor(Protocol):
    def extract(self, attachment: RawAttachment) -> ExtractedAttachment: ...
    # sandboxed, size/time/ratio-capped; NEVER raises for one bad file —
    # returns EXTRACTED / METADATA_ONLY / FAILED

class ThreadNormalizer(Protocol):
    def normalize(self, emails: list[RawEmail]) -> NormalizedConversation: ...
    # quote-strip, dedup, drop signatures/disclaimers; PII-mask hook lives here

class PromptBuilder(Protocol):
    prompt_version: str
    def build(self, conversation: NormalizedConversation,
              attachments: list[ExtractedAttachment], *, context_budget: int) -> Prompt: ...
    # versioned template + token budgeting (attachment text truncated before history)

class LLMClient(Protocol):
    model_name: str
    model_version: str
    def complete(self, prompt: Prompt, *, json_schema: dict) -> LlmRawResponse: ...
    # one guided-JSON call to RunPod/vLLM; raises LlmTransient on timeout/5xx; returns token usage

class Validator(Protocol):
    def validate(self, raw: LlmRawResponse) -> LlmSummaryOutput: ...
    # parse + schema-validate; raises LlmOutputInvalid -> drives an app-level retry

class WriteMode(str, Enum):
    APPEND_ONLY = "append_only"
    REPROCESS = "reprocess"

class WriteOutcome(str, Enum):
    WRITTEN = "written"
    SKIPPED_SUPERSEDED = "skipped_superseded"

class SummaryRepository(Protocol):
    def get_frontier(self, ticket_id: int) -> int | None: ...
    def upsert(self, ticket_id: int, document: SummaryDocument,
               marker: int, mode: WriteMode) -> WriteOutcome: ...
    # APPEND_ONLY: CAS write only if marker > stored, else SKIPPED_SUPERSEDED
    # REPROCESS:   force-overwrite, frontier := max(stored, marker)
    # the future outbox is a decorator wrapping this port
```

Errors are a two-branch hierarchy the entrypoint maps mechanically to queue behavior: `TransientError` (`EmailApiTransient`, `EmailNotYetAvailable`, `LlmTransient`) → don't ack, let SQS redeliver; `TerminalError` (`ConversationUnreconstructable`, `LlmOutputInvalidExhausted`) → route to DLQ.

`SummarizeTicket.execute(command)` orchestration order: read frontier, skip early if `command.email_meta_id <= frontier` under `APPEND_ONLY` → enumerate refs → RYW-verify triggering `message_id` → fetch all emails (bounded concurrency) → extract attachments → normalize → build budgeted prompt → `complete` → `validate` loop up to N app-retries → enrich into `SummaryDocument` → CAS upsert. `WRITTEN` → `OK`/`PARTIAL` (derived from `context_completeness`); `SKIPPED_SUPERSEDED` → ack as a successful no-op.

## Dependency injection

Constructor injection everywhere; no globals, no service locator. `composition.py` is the single place concretes are named — also the seam where tests substitute fakes.

```python
# composition.py — illustrative wiring shape, not final code
def build_use_case(settings: Settings) -> SummarizeTicket:
    return SummarizeTicket(
        email_meta = MySqlEmailMetadataRepository(settings.db),
        email_api  = HttpEmailGateway(settings.email_api),
        extractor  = SandboxedExtractor(settings.extraction),
        normalizer = DefaultThreadNormalizer(settings.normalize),
        prompt     = TemplatePromptBuilder(settings.prompt),   # carries prompt_version
        llm        = RunpodVllmClient(settings.llm),           # carries model_name/version
        validator  = PydanticValidator(),
        summaries  = MySqlSummaryRepository(settings.db),      # (+ OutboxDecorator later)
        limits     = settings.limits,
    )
```

## Open questions / not yet decided

- **LLD chunk 2** (configuration strategy, logging/observability strategy, testing strategy, full prompt-versioning strategy) was scoped but never delivered — being resolved just-in-time per module (per user's call on 2026-07-10) rather than as a standalone doc.
- If ticket-merge support is ever added to Stepping Desk, the CAS monotonicity assumption must be revisited (currently safe because merges are not supported).
- **Integration tests for `MySqlSummaryRepository` have never been executed.** They're written (testcontainers + real MySQL 8.0), but the dev sandbox that wrote them has no Docker. Run `uv run pytest -m integration` wherever Docker is available (or in CI) before trusting the concurrency/locking behavior in production. Everything covered by `uv run pytest` (default, unit only) has been run and passes.
- No CI pipeline configured yet — tests are currently only run locally/on-demand.

Resolved since the above was originally written (user answers captured in `implementation_plan.md`):
- `--max-model-len` is **16384 tokens**, wired as `LlmSettings.max_context_tokens` (`config/settings.py`), not hardcoded.
- `emailMetaId` **already exists** on the live `ticketAiSummary` table — no migration needed.
- **SQS setup is deferred** (the team will wire it up later). Until then, a CLI entrypoint (`entrypoints/cli.py`, not yet built) drives the pipeline for a single `ticketId` at a time so the pipeline can be tested end-to-end without SQS. The SQS consumer will be a thin wrapper around the same `SummarizeTicket` orchestrator when it's added — this does not change the CAS/queueing design above.
- Email API: no auth, no rate limits, any non-200 is an error (404 specifically maps to `EmailNotYetAvailable` for the RYW gate), response is always a JSON array.

## Implementation notes (deviations from the LLD sketch, decided during Phase 5)

These were resolved while implementing `MySqlSummaryRepository` (the first module built, per user's "de-risk the hardest part first" call on 2026-07-10) and intentionally diverge from the literal code sketched during the LLD conversation. Update the "Ports" section above if these are later revisited.

- **`SummaryRepository.upsert()` takes a `SummaryWrite` DTO, not a bare `SummaryDocument`.** The LLD's sketched signature (`upsert(ticket_id, document, marker, mode)`) had no home for row-level operational metadata that also has to land in the same atomic write: `latestMessageId`, `modelName`/`modelVersion`/`promptVersion`, `status`, `triggeredBy`, `processingTimeMs`, `tokenInput`/`tokenOutput`, `retryCount`, `errorMessage`. `SummaryWrite` (in `domain/ports.py`) bundles `document` with that metadata. A new `PersistedSummaryStatus` enum (`OK` | `PARTIAL` only) narrows `status` so the repository can't be asked to persist a `TRANSIENT_FAIL`/`TERMINAL_FAIL` row, which matches the status lifecycle (those two states are never written).
- **New error type: `SummaryPersistenceTransient(TransientError)`** in `domain/errors.py`. The originally-named transient errors (`EmailApiTransient`, `EmailNotYetAvailable`, `LlmTransient`) didn't cover MySQL connectivity failures (deadlock, lock-wait timeout, connection loss). `MySqlSummaryRepository` wraps only a known-transient set of MySQL error codes (1205, 1213, 2006, 2013) into this; anything else propagates unwrapped so a real bug fails loudly instead of being silently routed to DLQ as a normal outcome.
- **Concurrency strategy for the CAS write**: explicit transaction + `SELECT ... FOR UPDATE` (locks on the `UNIQUE ticketId` constraint) rather than `INSERT ... ON DUPLICATE KEY UPDATE`. Chosen for an unambiguous, auditable `WriteOutcome` (the ON DUPLICATE KEY approach has notoriously quirky affected-rows semantics) at the cost of one extra round trip, which is irrelevant at this system's volume. A row that doesn't exist yet locks nothing, so concurrent first-writes for a brand-new ticket can still race on the INSERT; the loser catches the duplicate-key `IntegrityError`, rolls back, and retries once — proven sufficient regardless of how many writers race (reasoning + a real multi-threaded test in `test_mysql_summary_repository_integration.py::TestConcurrentFirstWrite`).
- **Driver**: PyMySQL (pure Python, no C build toolchain — relevant since local dev happens on Windows) rather than an ORM. The CAS write is a couple of hand-tuned queries where controlling the exact `WHERE`/locking semantics matters more than ORM convenience.
- The pure "given stored/incoming marker + mode, what should happen" decision logic is factored out as a standalone function (`decide_write` in `mysql_summary_repository.py`) specifically so it's unit-testable without a database — it's the part that encodes the R1 staleness guard and the frontier-non-regression invariant.

## Current status (as of 2026-07-13)

- Phases 1–3 (requirements gathering, business analysis, HLD) complete and approved.
- Phase 4 (LLD) chunk 1 complete and approved: canonical schema, folder structure, ports, DI sketch. Chunk 2 deferred, being resolved just-in-time.
- Phase 5 (implementation): in progress, following the module-by-module build order in `implementation_plan.md`. Built and stabilized (unit-tested, `mypy --strict` clean, `ruff` clean):
  - `domain/schema/v1.py`, `domain/errors.py`, `domain/models.py`, `domain/ports.py` (all seven ports now declared).
  - `adapters/persistence/mysql_summary_repository.py` — CAS/reprocess write logic (built first, per the "de-risk the hardest part first" call on 2026-07-10).
  - `config/settings.py`, `config/logging_config.py` — pydantic-settings config (`.env`-driven) + structured JSON logging. Note: `pyproject.toml`'s `[tool.mypy]` now sets `plugins = ["pydantic.mypy"]`, required for `Settings`' nested `Field(default_factory=...)` sub-settings to type-check under strict mode.
  - `adapters/email/mysql_email_metadata.py`, `adapters/email/http_email_gateway.py` — email retrieval (MySQL ref enumeration + HTTP fetch from the internal Email API).
  - `adapters/extraction/` (`extractor.py` + `handlers.py`) — sandboxed attachment extraction (PDF/DOCX/XLSX/CSV/TXT, **plus images via OCR** — see "Attachments" section above for why this was reopened and what it requires at deploy time), per-attachment timeout + size + decompression-ratio caps, never raises. XXE/billion-laughs hardening is satisfied for free: `python-docx` sets `resolve_entities=False` on its own lxml parser, and `openpyxl` auto-detects `lxml` (present transitively via `python-docx`) and does the same — no `defusedxml` dependency needed.
  - `adapters/normalize/normalizer.py` — quote-stripping (via `email_reply_parser` + regex fallback), signature/disclaimer removal, thread dedup.
  - `adapters/prompt/prompt_builder.py` + `templates/v1/system.txt` — versioned, token-budgeted prompt assembly for Qwen2.5-Instruct's chat template; truncation order (attachments first, then oldest emails) matches the locked design.
  - `adapters/llm/runpod_vllm_client.py` — `RunpodVllmClient`. **Confirmed against the real deployment** (user-provided, 2026-07-13): RunPod Serverless, official worker-vllm **v2.14.0**, guided decoding via the `outlines` backend, model `Qwen/Qwen2.5-7B-Instruct`, exposed via the **OpenAI-compatible route** `/openai/v1/chat/completions` — not RunPod's native `/run` + poll `/status` handler that `runpod_context.py` uses (that reference is from an unrelated prior project and only supplied the "RunPod uses an async job pattern" framing, not the actual contract). Consequence: this is a **single synchronous POST**, no job-id polling; request body is standard OpenAI chat-completions shape (`model`, `messages: [{role, content}]`, plus vLLM's extra `repetition_penalty` and flat `guided_json` fields — the latter is vLLM's stable, long-standing OpenAI-server convention, unlike the native handler's version-dependent `SamplingParams` guided-decoding field name); response parsed via `choices[0].message.content` + standard `usage.prompt_tokens`/`usage.completion_tokens`. `Settings.llm.model_name` must equal the exact model id worker-vllm was launched with (`Qwen/Qwen2.5-7B-Instruct`) since the OpenAI route validates the `model` field against it. `request_timeout_seconds` defaults to 300s (was 120s under the old async design) since the call now blocks synchronously through any cold start. A RunPod job `FAILED`/error response is currently treated as retryable (`LlmTransient`, since there's no fallback LLM provider) rather than terminal — a genuinely bad prompt would retry until DLQ rather than fail fast.
  - **`domain/models.py::Prompt` carries `system_message` + `user_message` separately, not one pre-templated string.** Direct consequence of the OpenAI-route discovery above: that route applies Qwen's chat template server-side from a `messages` list, so `prompt_builder.py` no longer hand-builds `<|im_start|>system...<|im_end|>` markers (the original design, written against the wrong integration pattern before this was confirmed) — doing so would have either double-templated or had the model see literal control-token text. `TemplatePromptBuilder`'s token-budgeting and truncation logic (attachments first, then oldest emails) is otherwise unchanged, just estimated over `system_message + user_message` instead of one flattened string.
  - `adapters/validation/pydantic_validator.py` — `PydanticValidator`, strips markdown code fences defensively before `json.loads` + `LlmSummaryOutput.model_validate`; any failure raises `LlmOutputInvalid` for the orchestrator's retry loop.
  - `application/` — `SummarizeTicketCommand`, `SummaryResult`, and `SummarizeTicket.execute()` (the orchestrator), following the exact event flow in this file's "Event flow (happy path)" section. One judgment call made here, not explicitly specified in the LLD: an `EmailApiTransient`/`EmailNotYetAvailable` raised while fetching *any* referenced email (not just the triggering one) is left to propagate as transient (whole message redelivered), rather than being mapped to a PARTIAL/`context_completeness.missing` entry — only attachment extraction failures degrade to PARTIAL in place. Rationale: CLAUDE.md's "never a pipeline failure" language is scoped to attachments specifically; a systemic Email API blip should retry the whole ticket, and a permanently-missing individual email is effectively a poison message that should reach the DLQ through the normal SQS retry exhaustion path, not a bespoke completeness code path.
  - `composition.py` — the DI root; `entrypoints/cli.py` — `python -m summarizer.entrypoints.cli --ticket-id N --email-meta-id N --message-id ID [--reprocess] [--triggered-by X]`, the stand-in for the SQS consumer until SQS is wired up. Maps `TransientError`/`TerminalError` to exit code 1 (logged); anything else (a real bug) propagates unhandled with a full traceback rather than being swallowed.
  - 178 unit tests passing (`uv run pytest`), `mypy --strict` and `ruff` both clean on `src/`; 8 integration tests for `MySqlSummaryRepository` still unexecuted (see Open Questions — needs Docker).
- **The full pipeline has now been run successfully end-to-end against real staging infra** (2026-07-13: real MySQL `TrackEaseV2DB`, the real internal Email API, and the real RunPod endpoint) — ticket 239904, 3 emails, no attachments, wrote a real `OK`-status row to `ticketAiSummary` with a sensible summary, correct `context_completeness`, and real token usage (1707 in / 261 out, ~10.2s). A second run against the same email correctly short-circuited to `SKIPPED_SUPERSEDED` via the real CAS frontier check. Getting there surfaced six real bugs, all fixed and covered by regression tests:
  1. **`config/settings.py` never actually read `.env` for nested sub-settings.** `Settings.model_config` has `env_file=".env"`, but `DatabaseSettings`/`RunpodSettings`/etc. are each independently instantiated via their own `Field(default_factory=...)` and don't inherit that — pydantic-settings' `.env` loading is per-class. They were silently falling back to real OS env vars only, which every unit test happened to set directly (`monkeypatch.setenv`), so this was invisible until a real run with only a `.env` file crashed on missing required fields. Fixed by calling `load_dotenv()` explicitly at import time in `settings.py`, before any settings class is instantiated.
  2. **The live `ticketAiSummary` table was missing the `summaryJson` column.** Confirmed by inspecting the real schema (`DESCRIBE ticketAiSummary`) — only `summary` (text) existed; `summaryJson` (the versioned structured envelope) never got added despite being part of the agreed Phase 1 shape. Fixed live via `ALTER TABLE ticketAiSummary ADD COLUMN summaryJson JSON DEFAULT NULL` (user-approved before running, since it's a schema change to shared staging infra).
  3. **`Settings.llm.model_name` had the wrong casing.** Configured as `Qwen/Qwen2.5-7B-Instruct` (the HF repo id); the live RunPod deployment's `GET /openai/v1/models` reports it as lowercase `qwen/qwen2.5-7b-instruct`. The mismatch didn't produce a clean "model not found" — it made every chat-completions call fail with a generic, fast (~1.3s) `500 Internal Server Error`, which read like a broken endpoint until `/v1/models` was checked directly. Fixed by correcting the default and documenting that the model field must match `/v1/models` exactly, case included.
  4. **`max_context_tokens` was wrong.** Documented (and previously "resolved") as 16384; the live endpoint's `/v1/models` reports `max_model_len: 32768`. Corrected to 32768 — this had never actually been confirmed against the real deployment, only asserted.
  5. **vLLM's `outlines` guided-decoding backend isn't perfectly strict about excluding `null` from array-typed fields.** A real inference call returned `null` for `resolution_attempts` despite the guided-JSON schema declaring it as an array — consistently, across all 3 app-level retries, not a sampling fluke. `LlmSummaryOutput` now has a `field_validator(mode="before")` on all four list fields (`timeline`, `resolution_attempts`, `pending_actions`, `keywords`) that coerces `None` to `[]` before the rest of validation runs, rather than burning retries (or exhausting them into a false `LlmOutputInvalidExhausted`) on a decoding quirk instead of an actual bad response.
  6. **`_JsonFormatter`'s field allow-list was too narrow.** It only ever picked up `ticket_id`/`message_id`/`email_meta_id` from `extra=`, so the CLI's completion log line was silently missing `write_outcome`, `status`, `processing_time_ms`, `retry_count`, `token_input`, `token_output` — real operational data was going into the log call but never reaching the output. Extended the (deliberately still explicit, not blanket) allow-list to include those six operational fields.
- **A separate real-data finding, code-level fix applied 2026-07-15, not yet re-verified against staging**: ticket 239907 (7 emails) has one historical email whose `Email_Metadata` row exists in MySQL but whose body returns `200 []` (empty) from the Email API, consistently and repeatedly — not a transient blip. Root cause was traced to the Email API doing a messageId-only lookup; the fix (see "Existing infrastructure" above) is to always pass the full identifier set (`companyId`, `ticketId`, `emailMetaId`, `messageId`, `threadId`) as query parameters. `HttpEmailGateway`, `EmailGateway.fetch_email`, `EmailRef` (now carries `thread_id`), the `Email_Metadata` SELECT, and the orchestrator's call sites were all updated accordingly; unit tests updated and passing, `mypy --strict`/`ruff` clean. **Not yet re-run against ticket 239907 on real staging infra** — do that before considering this closed. If the empty-`[]` behavior persists even with the fuller query, the still-open R6 question from `CONTEXT.txt` (whether a permanently-missing non-triggering email should degrade the summary to `PARTIAL` rather than blocking the ticket indefinitely) is still live and would need a product decision.
- A `runpod_context.py` reference snippet (RunPod async `/run` + poll `/status` pattern, from an unrelated prior project) sits at the repo root — it was the model for `RunpodVllmClient`'s general shape early on and is not itself part of the package; the actual confirmed contract is the OpenAI-compatible route documented above.
