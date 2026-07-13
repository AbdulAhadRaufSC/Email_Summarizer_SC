"""Pydantic adapter for the Validator port.

Parses raw LLM text into ``LlmSummaryOutput``. vLLM's guided decoding
should already constrain the output to the schema, so this is mostly a
formality -- but it's the last line of defence against a model that
still wraps the JSON in prose or a markdown code fence despite the
system prompt's instructions not to, and it's what turns "the model
technically satisfied the grammar but violated a semantic constraint
Pydantic checks" (e.g. ``min_length=1``) into a retryable failure
instead of a bad summary silently reaching the database.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import ValidationError

from summarizer.domain.errors import LlmOutputInvalid
from summarizer.domain.models import LlmRawResponse
from summarizer.domain.schema.v1 import LlmSummaryOutput

logger = logging.getLogger(__name__)

_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


class PydanticValidator:
    """Parses + schema-validates raw LLM output.

    Raises ``LlmOutputInvalid`` on any failure (malformed JSON or a
    schema/constraint violation) -- caught by the orchestrator's
    app-level retry loop, not the SQS entrypoint.
    """

    def validate(self, raw: LlmRawResponse) -> LlmSummaryOutput:
        text = _CODE_FENCE.sub("", raw.text).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LlmOutputInvalid(f"LLM output is not valid JSON: {exc}") from exc

        try:
            return LlmSummaryOutput.model_validate(data)
        except ValidationError as exc:
            raise LlmOutputInvalid(f"LLM output failed schema validation: {exc}") from exc
