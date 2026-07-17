import pytest
from pydantic import ValidationError

from summarizer.domain.schema.v1 import (
    SCHEMA_VERSION,
    ClassificationHints,
    CompletenessStatus,
    ContextCompleteness,
    LlmSummaryOutput,
    ModuleName,
    PriorityLevel,
    RequestType,
    SourceInfo,
    SummaryDocument,
    TicketStatus,
)


def _minimal_llm_output(**overrides: object) -> LlmSummaryOutput:
    fields = {
        "human_summary": "Customer could not log in after a password reset.",
        "customer_issue": "Login fails with 'invalid credentials' after reset.",
        "current_status": TicketStatus.RESOLVED,
        **overrides,
    }
    return LlmSummaryOutput(**fields)  # type: ignore[arg-type]


class TestLlmSummaryOutput:
    def test_required_core_fields_are_enough(self) -> None:
        output = _minimal_llm_output()
        assert output.human_summary
        assert output.customer_issue
        assert output.current_status is TicketStatus.RESOLVED

    def test_best_effort_fields_default_without_forcing_invention(self) -> None:
        output = _minimal_llm_output()
        assert output.executive_summary is None
        assert output.business_impact is None
        assert output.timeline == []
        assert output.resolution_attempts == []
        assert output.pending_actions == []
        assert output.final_resolution is None
        assert output.keywords == []
        assert output.classification is None

    @pytest.mark.parametrize(
        "field_name", ["timeline", "resolution_attempts", "pending_actions", "keywords"]
    )
    def test_null_list_field_from_llm_coerces_to_empty_list(self, field_name: str) -> None:
        # Confirmed live 2026-07-13: vLLM's outlines guided-decoding
        # backend isn't perfectly strict about excluding null from
        # array-typed fields -- the model emitted null for
        # resolution_attempts despite the guided-JSON schema declaring
        # it as an array. This must not be a validation failure.
        output = _minimal_llm_output(**{field_name: None})
        assert getattr(output, field_name) == []

    @pytest.mark.parametrize("missing_field", ["human_summary", "customer_issue", "current_status"])
    def test_missing_required_field_is_rejected(self, missing_field: str) -> None:
        fields = {
            "human_summary": "x",
            "customer_issue": "y",
            "current_status": TicketStatus.OPEN,
        }
        del fields[missing_field]
        with pytest.raises(ValidationError):
            LlmSummaryOutput(**fields)  # type: ignore[arg-type]

    def test_empty_string_required_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_llm_output(human_summary="")

    def test_extra_fields_are_forbidden(self) -> None:
        """Guards the grounding guarantee: the model can't smuggle in a
        field (e.g. a hallucinated confidence score) the schema never
        asked for."""
        with pytest.raises(ValidationError):
            LlmSummaryOutput(
                human_summary="x",
                customer_issue="y",
                current_status=TicketStatus.OPEN,
                confidence=0.99,  # type: ignore[call-arg]
            )

    def test_unknown_status_never_forces_a_guess(self) -> None:
        output = _minimal_llm_output(current_status=TicketStatus.UNKNOWN)
        assert output.current_status is TicketStatus.UNKNOWN


class TestClassificationHints:
    """Advisory-only classification suggestions (module/priority/
    request_type), populated starting prompt v2 -- see v2/system.txt."""

    def test_all_fields_default_to_none(self) -> None:
        hints = ClassificationHints()
        assert hints.module is None
        assert hints.priority is None
        assert hints.request_type is None

    def test_accepts_valid_values_from_each_taxonomy(self) -> None:
        hints = ClassificationHints(
            module=ModuleName.EC_PAYROLL,
            priority=PriorityLevel.SR_2,
            request_type=RequestType.INCIDENT_MANAGEMENT,
        )
        assert hints.module == "EC Payroll"
        assert hints.priority == "SR-2"
        assert hints.request_type == "Incident Management"

    def test_rejects_a_value_outside_the_taxonomy(self) -> None:
        """Guards against the model (or a caller) smuggling in a value
        that isn't a real Stepping Desk dropdown option -- e.g. a
        plausible-sounding but nonexistent module name."""
        with pytest.raises(ValidationError):
            ClassificationHints(module="Not A Real Module")  # type: ignore[arg-type]

    def test_unassigned_and_no_category_are_not_valid_choices(self) -> None:
        """"Unassigned" (module) and "No Category" (requestType) are the
        real dropdowns' own placeholder/untriaged values -- deliberately
        excluded from the LLM-facing taxonomy since `None` already means
        "no basis to classify," and having two ways to say that invites
        drift between them."""
        with pytest.raises(ValidationError):
            ClassificationHints(module="Unassigned")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            ClassificationHints(request_type="No Category")  # type: ignore[arg-type]

    def test_classification_hints_embed_in_llm_summary_output(self) -> None:
        output = _minimal_llm_output(
            classification=ClassificationHints(module=ModuleName.TIME, priority=PriorityLevel.HIGH)
        )
        assert output.classification is not None
        assert output.classification.module == "Time"
        assert output.classification.priority == "High"
        assert output.classification.request_type is None


class TestSummaryDocument:
    def _document(self, **overrides: object) -> SummaryDocument:
        fields = {
            "content": _minimal_llm_output(),
            "context_completeness": ContextCompleteness(status=CompletenessStatus.COMPLETE),
            "source": SourceInfo(email_count=3, frontier_email_meta_id=42),
            **overrides,
        }
        return SummaryDocument(**fields)  # type: ignore[arg-type]

    def test_schema_version_defaults_to_current(self) -> None:
        assert self._document().schema_version == SCHEMA_VERSION == "1.0"

    def test_attachments_default_to_empty(self) -> None:
        assert self._document().attachments == []

    def test_partial_completeness_records_what_is_missing(self) -> None:
        doc = self._document(
            context_completeness=ContextCompleteness(
                status=CompletenessStatus.PARTIAL,
                missing=["email <msg-123> unretrievable"],
            )
        )
        assert doc.context_completeness.status is CompletenessStatus.PARTIAL
        assert doc.context_completeness.missing == ["email <msg-123> unretrievable"]

    def test_json_round_trip_preserves_content(self) -> None:
        """MySqlSummaryRepository persists via model_dump_json() into the
        summaryJson column -- a lossy round trip would silently corrupt
        stored summaries."""
        original = self._document()
        restored = SummaryDocument.model_validate_json(original.model_dump_json())
        assert restored == original
