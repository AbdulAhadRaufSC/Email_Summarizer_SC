"""Two-branch error hierarchy. The entrypoint maps these mechanically to
queue behavior: TransientError -> don't ack, let SQS redeliver.
TerminalError -> route to DLQ.

Errors that don't fit cleanly into either branch (a programming error, a
constraint violation that isn't a known-transient condition) should be
left unwrapped so they fail loudly instead of being silently routed as if
they were a normal business outcome.
"""


class SummarizerError(Exception):
    """Base for all domain-level errors in the summarization pipeline."""


class TransientError(SummarizerError):
    """Retryable. The entrypoint leaves the SQS message unacked."""


class TerminalError(SummarizerError):
    """Not retryable. The entrypoint routes the message to the DLQ."""


class EmailApiTransient(TransientError):
    """Email API returned a 5xx / timeout / connection failure."""


class EmailNotYetAvailable(TransientError):
    """Read-your-writes gate: the triggering messageId isn't retrievable
    yet. Do not summarize a thread missing the email that triggered it."""


class LlmTransient(TransientError):
    """RunPod/vLLM cold start, timeout, or 5xx."""


class SummaryPersistenceTransient(TransientError):
    """MySQL connection loss, deadlock, or lock-wait timeout on the CAS
    write. Not part of the originally-named set (EmailApiTransient /
    EmailNotYetAvailable / LlmTransient) — added because
    MySqlSummaryRepository needs a way to signal a retry-safe DB failure
    distinctly from a genuine bug. See CLAUDE.md open questions."""


class ConversationUnreconstructable(TerminalError):
    """Core thread or triggering email could not be reconstructed after
    exhausting retries."""


class LlmOutputInvalidExhausted(TerminalError):
    """LLM output failed schema validation after exhausting app-level
    retries."""


class LlmOutputInvalid(SummarizerError):
    """LLM output failed schema validation on a single attempt.

    This is intentionally **not** in the TransientError/TerminalError
    hierarchy: it is caught and retried inside the orchestrator's
    app-level retry loop, not by the SQS entrypoint.  After exhausting
    retries the orchestrator raises ``LlmOutputInvalidExhausted``
    (TerminalError) instead.
    """

