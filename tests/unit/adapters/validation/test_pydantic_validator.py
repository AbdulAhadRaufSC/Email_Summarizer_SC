from __future__ import annotations

import json

import pytest

from summarizer.adapters.validation.pydantic_validator import PydanticValidator
from summarizer.domain.errors import LlmOutputInvalid
from summarizer.domain.models import LlmRawResponse
from summarizer.domain.schema.v1 import TicketStatus

VALID_PAYLOAD = {
    "human_summary": "Customer could not log in; password reset resolved it.",
    "customer_issue": "Could not log in after a recent password change.",
    "current_status": "resolved",
}


class TestValidJson:
    def test_parses_minimal_valid_output(self) -> None:
        raw = LlmRawResponse(text=json.dumps(VALID_PAYLOAD))
        result = PydanticValidator().validate(raw)

        assert result.human_summary == VALID_PAYLOAD["human_summary"]
        assert result.current_status == TicketStatus.RESOLVED

    def test_strips_surrounding_whitespace(self) -> None:
        raw = LlmRawResponse(text=f"\n\n  {json.dumps(VALID_PAYLOAD)}  \n")
        result = PydanticValidator().validate(raw)
        assert result.customer_issue == VALID_PAYLOAD["customer_issue"]

    def test_strips_markdown_code_fence(self) -> None:
        raw = LlmRawResponse(text=f"```json\n{json.dumps(VALID_PAYLOAD)}\n```")
        result = PydanticValidator().validate(raw)
        assert result.current_status == TicketStatus.RESOLVED

    def test_strips_bare_code_fence_without_json_tag(self) -> None:
        raw = LlmRawResponse(text=f"```\n{json.dumps(VALID_PAYLOAD)}\n```")
        result = PydanticValidator().validate(raw)
        assert result.current_status == TicketStatus.RESOLVED

    def test_parses_full_payload_with_optional_fields(self) -> None:
        payload = {
            **VALID_PAYLOAD,
            "executive_summary": "Password reset fixed a login issue.",
            "timeline": [{"description": "Password reset", "actor": "support"}],
            "pending_actions": [],
            "keywords": ["password reset", "login"],
        }
        raw = LlmRawResponse(text=json.dumps(payload))
        result = PydanticValidator().validate(raw)

        assert result.keywords == ["password reset", "login"]
        assert result.timeline[0].description == "Password reset"


class TestInvalidOutput:
    def test_malformed_json_raises_llm_output_invalid(self) -> None:
        raw = LlmRawResponse(text="{not valid json")
        with pytest.raises(LlmOutputInvalid):
            PydanticValidator().validate(raw)

    def test_missing_required_field_raises_llm_output_invalid(self) -> None:
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "human_summary"}
        raw = LlmRawResponse(text=json.dumps(payload))
        with pytest.raises(LlmOutputInvalid):
            PydanticValidator().validate(raw)

    def test_empty_required_string_raises_llm_output_invalid(self) -> None:
        payload = {**VALID_PAYLOAD, "human_summary": ""}
        raw = LlmRawResponse(text=json.dumps(payload))
        with pytest.raises(LlmOutputInvalid):
            PydanticValidator().validate(raw)

    def test_invalid_enum_value_raises_llm_output_invalid(self) -> None:
        payload = {**VALID_PAYLOAD, "current_status": "not_a_real_status"}
        raw = LlmRawResponse(text=json.dumps(payload))
        with pytest.raises(LlmOutputInvalid):
            PydanticValidator().validate(raw)

    def test_unexpected_extra_field_raises_llm_output_invalid(self) -> None:
        # LlmSummaryOutput.model_config = {"extra": "forbid"} -- a model
        # that hallucinates a field outside the schema must fail loudly.
        payload = {**VALID_PAYLOAD, "totally_made_up_field": "oops"}
        raw = LlmRawResponse(text=json.dumps(payload))
        with pytest.raises(LlmOutputInvalid):
            PydanticValidator().validate(raw)

    def test_non_object_json_raises_llm_output_invalid(self) -> None:
        raw = LlmRawResponse(text="[1, 2, 3]")
        with pytest.raises(LlmOutputInvalid):
            PydanticValidator().validate(raw)

    def test_empty_string_raises_llm_output_invalid(self) -> None:
        raw = LlmRawResponse(text="")
        with pytest.raises(LlmOutputInvalid):
            PydanticValidator().validate(raw)
