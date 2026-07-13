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
- **Email storage**: bodies/attachments are NOT in MySQL. Amazon S3 stores complete HTML, plain text, and attachments. An internal **Email API** is the single source of truth — given a `messageId`, it returns subject, sender, recipients, HTML body, plain text body, message ID, parent message ID, thread ID, attachments, and base64 attachment contents.
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

- Text extraction (Phase 1 scope): **PDF, DOCX, TXT, CSV, XLSX**.
- Metadata-only (filename, MIME type, size — no content extraction): images, EML, unknown/unsupported types.
- Untrusted-input hardening required: sandboxed/resource-limited extraction subprocess, hard per-file size + wall-clock timeout caps, decompression-ratio caps (zip-bomb defense for XLSX/DOCX), disabled external XML entity resolution (billion-laughs defense), row/cell caps for XLSX. A single bad attachment must never crash or fail the whole ticket — always fall back to metadata-only.

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
    def fetch_email(self, message_id: str) -> RawEmail: ...
    # full content + attachments from the internal Email API;
    # raises EmailNotYetAvailable (RYW) or EmailApiTransient

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
- Exact vLLM `--max-model-len` for the RunPod deployment — treat as config, not hardcoded, until confirmed.
- Whether `emailMetaId` already exists as a column on the live `ticketAiSummary` table, or needs to be added via migration.
- If ticket-merge support is ever added to Stepping Desk, the CAS monotonicity assumption must be revisited (currently safe because merges are not supported).
- **Integration tests for `MySqlSummaryRepository` have never been executed.** They're written (testcontainers + real MySQL 8.0), but the dev sandbox that wrote them has no Docker. Run `uv run pytest -m integration` wherever Docker is available (or in CI) before trusting the concurrency/locking behavior in production. Everything covered by `uv run pytest` (default, unit only) has been run and passes.
- No CI pipeline configured yet — tests are currently only run locally/on-demand.

## Implementation notes (deviations from the LLD sketch, decided during Phase 5)

These were resolved while implementing `MySqlSummaryRepository` (the first module built, per user's "de-risk the hardest part first" call on 2026-07-10) and intentionally diverge from the literal code sketched during the LLD conversation. Update the "Ports" section above if these are later revisited.

- **`SummaryRepository.upsert()` takes a `SummaryWrite` DTO, not a bare `SummaryDocument`.** The LLD's sketched signature (`upsert(ticket_id, document, marker, mode)`) had no home for row-level operational metadata that also has to land in the same atomic write: `latestMessageId`, `modelName`/`modelVersion`/`promptVersion`, `status`, `triggeredBy`, `processingTimeMs`, `tokenInput`/`tokenOutput`, `retryCount`, `errorMessage`. `SummaryWrite` (in `domain/ports.py`) bundles `document` with that metadata. A new `PersistedSummaryStatus` enum (`OK` | `PARTIAL` only) narrows `status` so the repository can't be asked to persist a `TRANSIENT_FAIL`/`TERMINAL_FAIL` row, which matches the status lifecycle (those two states are never written).
- **New error type: `SummaryPersistenceTransient(TransientError)`** in `domain/errors.py`. The originally-named transient errors (`EmailApiTransient`, `EmailNotYetAvailable`, `LlmTransient`) didn't cover MySQL connectivity failures (deadlock, lock-wait timeout, connection loss). `MySqlSummaryRepository` wraps only a known-transient set of MySQL error codes (1205, 1213, 2006, 2013) into this; anything else propagates unwrapped so a real bug fails loudly instead of being silently routed to DLQ as a normal outcome.
- **Concurrency strategy for the CAS write**: explicit transaction + `SELECT ... FOR UPDATE` (locks on the `UNIQUE ticketId` constraint) rather than `INSERT ... ON DUPLICATE KEY UPDATE`. Chosen for an unambiguous, auditable `WriteOutcome` (the ON DUPLICATE KEY approach has notoriously quirky affected-rows semantics) at the cost of one extra round trip, which is irrelevant at this system's volume. A row that doesn't exist yet locks nothing, so concurrent first-writes for a brand-new ticket can still race on the INSERT; the loser catches the duplicate-key `IntegrityError`, rolls back, and retries once — proven sufficient regardless of how many writers race (reasoning + a real multi-threaded test in `test_mysql_summary_repository_integration.py::TestConcurrentFirstWrite`).
- **Driver**: PyMySQL (pure Python, no C build toolchain — relevant since local dev happens on Windows) rather than an ORM. The CAS write is a couple of hand-tuned queries where controlling the exact `WHERE`/locking semantics matters more than ORM convenience.
- The pure "given stored/incoming marker + mode, what should happen" decision logic is factored out as a standalone function (`decide_write` in `mysql_summary_repository.py`) specifically so it's unit-testable without a database — it's the part that encodes the R1 staleness guard and the frontier-non-regression invariant.

## Current status (as of 2026-07-10)

- Phases 1–3 (requirements gathering, business analysis, HLD) complete and approved.
- Phase 4 (LLD) chunk 1 complete and approved: canonical schema, folder structure, ports, DI sketch. Chunk 2 deferred, being resolved just-in-time.
- Phase 5 (implementation): started. Project scaffolded with `uv` (Python 3.12, `src/summarizer` layout, ruff + mypy strict + pytest configured). Built so far: `domain/schema/v1.py`, `domain/errors.py`, `domain/ports.py` (partial — only what `SummaryRepository` needs; the other six ports get added with their own modules), and `adapters/persistence/mysql_summary_repository.py` (the CAS/reprocess write logic — the piece the user chose to de-risk first). 32 unit tests passing (`uv run pytest`); 8 integration tests written but not yet executed (see Open Questions). Nothing else in the pipeline (email retrieval, attachment extraction, normalization, prompting, LLM client, validation, entrypoints) has been built yet.
