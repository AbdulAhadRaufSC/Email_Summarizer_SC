# Build the AI Email Summarization Pipeline — All Remaining Modules

Phase 5 implementation of the Stepping Desk summarization worker. The domain schema, error hierarchy, `SummaryRepository` port, and `MySqlSummaryRepository` adapter are already built and tested. This plan covers everything else needed for an end-to-end working pipeline.

## Context from User Answers

| Question | Answer |
|---|---|
| Email API auth | None (open on internal network) |
| Email API rate limits | None |
| Email API errors | Anything non-200 is an error |
| Email API response | Always a JSON array (like the sample) |
| RunPod / vLLM | Async `/run` + poll `/status` pattern (see `runpod_context.py`) |
| `--max-model-len` | **16384 tokens** |
| `ticketAiSummary` table | Already exists, `emailMetaId` column present |
| DB config | `.env` file (host/port/user/password/name) |
| SQS | **Not needed yet** — will be set up later by the team |
| Secrets | `.env` file |
| Email_Metadata filters | Exclude `isDraft=1` and `isDeleted=1`; keep notes |
| Attachment libs | My pick (most efficient) |
| Prompt template | I'll draft it, user reviews |
| Everything else | "Do whatever is most efficient" |

## Proposed Changes

Since SQS is deferred, I'll build a **CLI entrypoint** (`cli.py`) that can process a single ticket by `ticketId` — this lets you test the full pipeline end-to-end without SQS. The SQS consumer becomes a thin wrapper later.

Modules will be built **one at a time**, in dependency order (leaves first, orchestrator last).

---

### Domain Layer — Complete the Models & Ports

#### [NEW] [models.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/domain/models.py)
Domain data carriers used across the pipeline: `EmailRef`, `RawEmail`, `RawAttachment`, `ExtractedAttachment`, `NormalizedConversation`, `Prompt`, `LlmRawResponse`.

#### [MODIFY] [ports.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/domain/ports.py)
Add the remaining 6 port Protocols: `EmailMetadataRepository`, `EmailGateway`, `AttachmentExtractor`, `ThreadNormalizer`, `PromptBuilder`, `LLMClient`, `Validator`.

---

### Config Layer

#### [NEW] [settings.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/config/settings.py)
`pydantic-settings` based config loaded from `.env`: DB connection, RunPod endpoint/key, Email API base URL, token budgets, extraction limits, retry counts.

#### [NEW] [logging.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/config/logging_config.py)
Structured JSON logging setup, PII-safe (no email bodies in logs), keyed by `ticketId`/`messageId`.

---

### Adapter: Email Metadata (MySQL)

#### [NEW] [mysql_email_metadata.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/email/mysql_email_metadata.py)
`MySqlEmailMetadataRepository` — queries `Email_Metadata` for a ticket's emails, ordered by `emailMetaId`, excluding `isDraft=1` and `isDeleted=1`.

---

### Adapter: Email Gateway (HTTP)

#### [NEW] [http_email_gateway.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/email/http_email_gateway.py)
`HttpEmailGateway` — calls `https://maildata.stage.steppingdesk.com/api/emails/<messageId>`, parses the JSON array response into `RawEmail` + `RawAttachment` domain objects. Raises `EmailNotYetAvailable` on 404, `EmailApiTransient` on 5xx/timeout.

---

### Adapter: Attachment Extraction

#### [NEW] [extractor.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/extraction/extractor.py)
`SandboxedExtractor` — dispatcher that routes by MIME type to per-format handlers. Enforces size cap (10 MB), wall-clock timeout (30s), decompression ratio cap. Never raises — always returns `ExtractedAttachment` with status `EXTRACTED`, `METADATA_ONLY`, or `FAILED`.

#### [NEW] [handlers.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/extraction/handlers.py)
Per-format extraction: `PdfHandler` (PyMuPDF), `DocxHandler` (python-docx), `XlsxHandler` (openpyxl), `CsvHandler` (stdlib csv), `TxtHandler` (plain read).

---

### Adapter: Thread Normalizer

#### [NEW] [normalizer.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/normalize/normalizer.py)
`DefaultThreadNormalizer` — strips quoted replies ("On … wrote:"), removes email signatures/disclaimers, deduplicates content across the thread. Uses `email-reply-parser` as the primary engine with regex fallbacks.

---

### Adapter: Prompt Builder

#### [NEW] [prompt_builder.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/prompt/prompt_builder.py)
`TemplatePromptBuilder` — builds a versioned, token-budgeted system+user prompt. Enforces a 16384-token context window (configurable). Truncation order: attachment text first, then oldest conversation history. Outputs the JSON schema from `LlmSummaryOutput.model_json_schema()` inline in the prompt for guided decoding.

#### [NEW] [templates/v1/system.txt](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/prompt/templates/v1/system.txt)
The v1 system prompt template. Grounded, extractive instructions for Qwen2.5-7B.

---

### Adapter: LLM Client (RunPod)

#### [NEW] [runpod_vllm_client.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/llm/runpod_vllm_client.py)
`RunpodVllmClient` — matches the existing RunPod async `/run` + poll `/status/{job_id}` pattern from `runpod_context.py`. Sends guided-JSON schema in `sampling_params`. Raises `LlmTransient` on timeout/failure. Returns token usage stats.

---

### Adapter: Validator

#### [NEW] [pydantic_validator.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/adapters/validation/pydantic_validator.py)
`PydanticValidator` — parses raw LLM text into `LlmSummaryOutput` via Pydantic. Raises `LlmOutputInvalid` on schema validation failure (drives app-level retry).

---

### Application Layer — Orchestrator

#### [NEW] [command.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/application/command.py)
`SummarizeTicketCommand` dataclass — `ticket_id`, `email_meta_id`, `message_id`, `mode` (defaults to `APPEND_ONLY`).

#### [NEW] [result.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/application/result.py)
`SummaryResult` dataclass — `status`, `write_outcome`, `processing_time_ms`, `token_input`, `token_output`, `retry_count`.

#### [NEW] [summarize_ticket.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/application/summarize_ticket.py)
`SummarizeTicket.execute(command)` — the orchestrator. Follows the exact flow from CLAUDE.md: frontier check → enumerate refs → RYW gate → fetch emails → extract attachments → normalize → build prompt → LLM call → validate (with retry loop) → enrich into `SummaryDocument` → CAS upsert.

---

### Composition Root & Entrypoint

#### [NEW] [composition.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/composition.py)
DI wiring — the single place where concrete adapters are instantiated and injected into `SummarizeTicket`.

#### [NEW] [entrypoints/cli.py](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/src/summarizer/entrypoints/cli.py)
CLI entrypoint: `python -m summarizer.entrypoints.cli --ticket-id 12345`. Loads `.env`, builds the use case via `composition.py`, and executes. This lets you test end-to-end without SQS.

---

### Dependencies

#### [MODIFY] [pyproject.toml](file:///c:/Users/Sri Nandhini T/Desktop/STP CLD Summarizerinator/pyproject.toml)
Add runtime dependencies:
- `requests` — HTTP client for Email API + RunPod
- `pydantic-settings` — config from `.env`
- `python-dotenv` — `.env` file loading
- `PyMuPDF` — PDF extraction
- `python-docx` — DOCX extraction
- `openpyxl` — XLSX extraction
- `email-reply-parser` — thread quote stripping
- `tiktoken` — token counting for prompt budgeting

> [!IMPORTANT]
> **SQS consumer is deferred** per your instruction. The CLI entrypoint replaces it for now. When your team sets up SQS, I'll add `sqs_consumer.py` as a thin wrapper around the same `SummarizeTicket` orchestrator.

## Build Order

I'll build one module at a time, with tests, waiting for your approval between each:

| Step | Module | Depends On |
|---|---|---|
| 1 | `domain/models.py` + complete `ports.py` | Nothing new |
| 2 | `config/settings.py` + `config/logging_config.py` | Nothing new |
| 3 | `adapters/email/mysql_email_metadata.py` | models, ports, settings |
| 4 | `adapters/email/http_email_gateway.py` | models, ports, settings |
| 5 | `adapters/extraction/` (extractor + handlers) | models, ports |
| 6 | `adapters/normalize/normalizer.py` | models, ports |
| 7 | `adapters/prompt/prompt_builder.py` + template | models, ports, schema |
| 8 | `adapters/llm/runpod_vllm_client.py` | models, ports, settings |
| 9 | `adapters/validation/pydantic_validator.py` | models, ports, schema |
| 10 | `application/` (command, result, orchestrator) | All ports |
| 11 | `composition.py` + `entrypoints/cli.py` | Everything |
| 12 | `pyproject.toml` dependency update | — |

## Verification Plan

### Automated Tests
- Unit tests for every module (pure logic, fakes for I/O)
- `uv run pytest` — all unit tests pass
- `uv run mypy src/` — strict type checking passes
- `uv run ruff check src/` — linting passes

### Manual Verification
- Run `python -m summarizer.entrypoints.cli --ticket-id <real_ticket_id>` against staging DB + Email API + RunPod to verify end-to-end
