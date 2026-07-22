# 07 — Security

- [1. Threat model & posture](#1-threat-model--posture)
- [2. Authentication & authorization](#2-authentication--authorization)
- [3. Secrets management](#3-secrets-management)
- [4. Untrusted-input hardening (attachment sandbox)](#4-untrusted-input-hardening-attachment-sandbox)
- [5. Injection & data-safety](#5-injection--data-safety)
- [6. Logging & data-leak surface](#6-logging--data-leak-surface)
- [7. Network security posture](#7-network-security-posture)
- [8. Security findings summary](#8-security-findings-summary)

---

## 1. Threat model & posture

This is an internal, non-internet-facing backend worker. It **accepts no public input**; its
inputs come from trusted internal systems (SQS, the Email API, MySQL). The primary security
concerns, in the order they actually matter here, are:

1. **Untrusted file content** — attachments on customer emails are the one genuinely
   untrusted input, and are the most-hardened part of the system.
2. **Secret handling** — DB, RunPod, and AWS credentials.
3. **Sensitive-data leakage** — customer email bodies must not end up in logs.
4. **Self-hosted LLM** — because the model runs on the org's own RunPod infra, there is no
   third-party data-exfiltration concern, which is *why* pre-inference PII masking was
   deprioritized in Phase 1 (documented decision in `CLAUDE.md`).

There is **no HIPAA/GDPR mandate currently** (documented), so the posture is access-control +
minimal logging rather than data-subject controls. This should be revisited if the model ever
moves off self-hosted infra.

## 2. Authentication & authorization

**There is no user authentication or authorization in this system** — no login, sessions,
tokens, roles, or permission checks. That's correct for a headless worker with no human-facing
surface. Service-to-service auth is:

| Integration | Auth mechanism | Evidence |
|-------------|----------------|----------|
| AWS SQS | boto3 default credential chain (env vars) | [sqs_consumer.py:253](../src/summarizer/entrypoints/sqs_consumer.py#L253) |
| MySQL RDS | username/password | [composition.py:27-35](../src/summarizer/composition.py#L27-L35) |
| RunPod | `Authorization: Bearer {api_key}` | [runpod_vllm_client.py:62-64](../src/summarizer/adapters/llm/runpod_vllm_client.py#L62-L64) |
| Internal Email API | **none** (confirmed no-auth endpoint) | [http_email_gateway.py:20-22](../src/summarizer/adapters/email/http_email_gateway.py#L20-L22) |

_Inferred from implementation:_ authorization is entirely perimeter-based (network placement
+ credentials), not application-level.

## 3. Secrets management

Secrets are loaded from a `.env` file into `os.environ` at import time (`load_dotenv()` in
[settings.py:24](../src/summarizer/config/settings.py#L24)) and consumed by
pydantic-settings and boto3. `.gitignore` correctly excludes `.env`
([.gitignore:20-23](../.gitignore#L20-L23)).

### 🔴 Finding S1 — live secrets present in the working tree (`.env`)
The local `.env` contains **real, live credentials in plaintext**: a RunPod API key, the RDS
`admin` password, and an AWS access-key/secret pair.

- **Mitigating fact:** `.env` is **not** tracked by git and **never appears in git history**
  (verified: `git ls-files --error-unmatch .env` fails; `git log --all -- .env` is empty). So
  these are *not* leaked to the repository.
- **Residual risk (Medium):** live secrets sit unencrypted on the developer machine, are
  copyable into logs/screenshots, and — because the same values are the *staging* RDS admin
  and an AWS IAM key — grant broad access if the machine is compromised or the file is shared.
- **Recommendations:**
  1. Never share the `.env` or this working tree without scrubbing it.
  2. Add a committed **`.env.example`** with keys and empty values (`.gitignore` already
     whitelists `!.env.example`).
  3. Rotate the RunPod key, RDS `admin` password, and AWS key if there's any chance the tree
     was shared. Prefer IAM roles (instance/task role) over a static AWS key pair in prod.
  4. Prefer AWS Secrets Manager / SSM Parameter Store over a file for production (`CLAUDE.md`
     already anticipates SSM).

## 4. Untrusted-input hardening (attachment sandbox)

The extractor treats attachment bytes as hostile. Controls in
[`SandboxedExtractor`](../src/summarizer/adapters/extraction/extractor.py) and
[`handlers.py`](../src/summarizer/adapters/extraction/handlers.py):

| Threat | Control | Where |
|--------|---------|-------|
| Oversized file / memory exhaustion | `max_file_bytes` (10 MB) reject before parsing | extractor.py:108-112 |
| Zip bomb (DOCX/XLSX) | decompression-ratio cap (decoded/raw ≤ 100×) | extractor.py:114-122 |
| Spreadsheet blow-up | XLSX row (50k) + cell (500k) caps; CSV 50k-row cap | handlers.py:37-65, 68-78 |
| Runaway parse (hang) | wall-clock timeout per extraction via a 1-worker pool | extractor.py:161-165 |
| Image decompression bomb | Pillow `MAX_IMAGE_PIXELS` guard (raises → FAILED) | handlers.py:86-95 |
| **XXE / billion-laughs** | `python-docx` (`resolve_entities=False`) + `openpyxl` via lxml disable external entities | documented in `CLAUDE.md`; no `defusedxml` needed |
| One bad file crashing the ticket | extractor **never raises** — degrades to `FAILED`/`METADATA_ONLY` | extractor.py:73-74 |

**Note (not full OS-level isolation):** "sandbox" here means *resource-capped, exception-safe
in-process extraction*, not a separate process/container with seccomp. The timeout uses a
thread pool, so a truly runaway C-extension (e.g. inside PyMuPDF) that ignores the GIL
handoff could still block the worker thread beyond the timeout. _Inferred from
implementation._ For Phase 1's trusted-ish internal corpus this is a reasonable tradeoff;
a subprocess/container boundary would be the hardening upgrade (see
[10 — Technical Debt](10-technical-debt.md)).

## 5. Injection & data-safety

- **SQL injection:** not possible on the paths reviewed — every query uses **parameterized**
  statements (`%s` / `%(name)s` placeholders), never string interpolation
  ([mysql_email_metadata.py:23-30](../src/summarizer/adapters/email/mysql_email_metadata.py#L23-L30),
  [mysql_summary_repository.py:41-72](../src/summarizer/adapters/persistence/mysql_summary_repository.py#L41-L72)).
- **Prompt injection:** a customer email could contain text attempting to manipulate the LLM.
  The mitigations present are indirect: **guided JSON decoding** constrains output to the
  schema (the model can't emit arbitrary actions), and the output is **advisory data written
  to a DB**, never executed. There is no explicit prompt-injection defense beyond that.
  _Inferred from implementation._ Low impact given the output is non-actionable summary text.
- **CSRF / XSS / CORS:** **not applicable** — no browser, no HTML rendered to users, no
  cross-origin surface. (The `summary`/`summaryJson` is rendered by the separate Stepping Desk
  UI, which is responsible for escaping it — out of scope for this repo.)
- **Rate limiting:** none in this worker; back-pressure is handled by SQS + bounded
  concurrency instead.

## 6. Logging & data-leak surface

The logging design is **anti-leak by construction**: the JSON formatter copies only an
explicit allow-list of `extra=` keys, so email bodies can't accidentally reach logs
([logging_config.py:34-52](../src/summarizer/config/logging_config.py#L34-L52)).

### 🔴 Finding S2 — prompt (email bodies + attachment text) logged at INFO
A debug block in the prompt builder logs the **entire assembled prompt** — which contains
full customer email bodies and extracted attachment text — via `logger.info`
([prompt_builder.py:101-111](../src/summarizer/adapters/prompt/prompt_builder.py#L101-L111)),
bypassing the allow-list entirely. The `###########` marker shows it's leftover debug
scaffolding.
- **Impact (Medium–High):** sensitive customer content written to stdout/log aggregation on
  every single run, contradicting the project's own stated logging policy.
- **Fix:** delete the block (lines 101-111). Zero behavioral impact on the pipeline; the
  legitimate `"Prompt built: ~N tokens"` line just above it is sufficient.

## 7. Network security posture

- All outbound calls are HTTPS (Email API, RunPod, RDS/TLS-capable, SQS).
- The worker is presumed to run inside a private network/VPC alongside RDS and the Email API
  (the Email API being unauthenticated only makes sense on a trusted internal network).
  _Inferred from implementation._ Deploying this worker anywhere the Email API is reachable
  without auth would expose that API.

## 8. Security findings summary

| ID | Finding | Severity | Fix |
|----|---------|----------|-----|
| **S1** | Live secrets in local `.env` (untracked, not in history) | Medium | `.env.example`; rotate keys; use IAM role + Secrets Manager in prod |
| **S2** | Full prompt (email bodies + attachments) logged at INFO | Medium–High | Delete debug block `prompt_builder.py:101-111` |
| S3 | Unauthenticated Email API relied upon | Low (by design) | Ensure network isolation; add auth if it ever leaves the VPC |
| S4 | In-process (not subprocess) attachment sandbox | Low | Consider subprocess/container isolation for defence in depth |
| S5 | No explicit prompt-injection handling | Low | Acceptable — output is non-actionable; revisit if summaries ever auto-act |
