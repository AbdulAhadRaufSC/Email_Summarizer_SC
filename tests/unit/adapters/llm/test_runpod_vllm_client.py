from __future__ import annotations

from typing import Any

import pytest
import requests

from summarizer.adapters.llm.runpod_vllm_client import RunpodVllmClient
from summarizer.domain.errors import LlmTransient
from summarizer.domain.models import Prompt


def _prompt() -> Prompt:
    return Prompt(
        text="<|im_start|>system\n...<|im_end|>",
        json_schema={"type": "object", "properties": {"human_summary": {"type": "string"}}},
        prompt_version="v1",
        estimated_tokens=42,
    )


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


class _FakeSession:
    """Serves queued responses in order; `post` and `get` share nothing
    but each pulls from its own queue so submit vs poll can be scripted
    independently."""

    def __init__(
        self,
        post_responses: list[_FakeResponse] | None = None,
        get_responses: list[_FakeResponse] | None = None,
        post_exc: Exception | None = None,
    ):
        self._post_responses = list(post_responses or [])
        self._get_responses = list(get_responses or [])
        self._post_exc = post_exc
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def post(
        self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: int
    ) -> _FakeResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self._post_exc is not None:
            raise self._post_exc
        return self._post_responses.pop(0)

    def get(self, url: str, headers: dict[str, str], timeout: int) -> _FakeResponse:
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return self._get_responses.pop(0)


def _client_with(session: _FakeSession, **overrides: Any) -> RunpodVllmClient:
    fields: dict[str, Any] = {
        "endpoint_id": "ep-123",
        "api_key": "secret",
        "model_name": "Qwen2.5-7B-Instruct",
        "model_version": "2.5",
        "poll_interval_seconds": 0.0,
        **overrides,
    }
    client = RunpodVllmClient(**fields)
    client._session = session  # type: ignore[assignment]
    return client


_COMPLETED_OUTPUT = {
    "status": "COMPLETED",
    "output": [{"choices": [{"tokens": ["  {\"human_summary\": \"ok\"}  "]}]}],
}


class TestCompleteHappyPath:
    def test_returns_text_from_completed_job(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[_FakeResponse(200, _COMPLETED_OUTPUT)],
        )
        client = _client_with(session)

        result = client.complete(_prompt())

        assert result.text == '{"human_summary": "ok"}'

    def test_extracts_usage_tokens_when_present(self) -> None:
        completed = {
            "status": "COMPLETED",
            "output": [
                {
                    "choices": [{"tokens": ["ok"]}],
                    "usage": {"input": 100, "output": 20},
                }
            ],
        }
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[_FakeResponse(200, completed)],
        )
        client = _client_with(session)

        result = client.complete(_prompt())

        assert result.token_input == 100
        assert result.token_output == 20

    def test_usage_tokens_none_when_absent(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[_FakeResponse(200, _COMPLETED_OUTPUT)],
        )
        client = _client_with(session)

        result = client.complete(_prompt())

        assert result.token_input is None
        assert result.token_output is None

    def test_polls_through_in_queue_and_in_progress(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[
                _FakeResponse(200, {"status": "IN_QUEUE"}),
                _FakeResponse(200, {"status": "IN_PROGRESS"}),
                _FakeResponse(200, _COMPLETED_OUTPUT),
            ],
        )
        client = _client_with(session)

        result = client.complete(_prompt())

        assert result.text == '{"human_summary": "ok"}'
        assert len(session.get_calls) == 3

    def test_model_name_and_version_exposed_as_attributes(self) -> None:
        session = _FakeSession()
        client = _client_with(session, model_name="Qwen2.5-7B-Instruct", model_version="2.5")

        assert client.model_name == "Qwen2.5-7B-Instruct"
        assert client.model_version == "2.5"


class TestSubmissionPayload:
    def test_includes_guided_json_schema_and_sampling_params(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[_FakeResponse(200, _COMPLETED_OUTPUT)],
        )
        client = _client_with(
            session, temperature=0.1, top_p=0.9, repetition_penalty=1.2, max_output_tokens=512
        )
        prompt = _prompt()

        client.complete(prompt)

        sent = session.post_calls[0]["json"]
        params = sent["input"]["sampling_params"]
        assert sent["input"]["prompt"] == prompt.text
        assert params["max_tokens"] == 512
        assert params["temperature"] == 0.1
        assert params["top_p"] == 0.9
        assert params["repetition_penalty"] == 1.2
        assert params["guided_decoding"] == {"json": prompt.json_schema}

    def test_sends_bearer_auth_header(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[_FakeResponse(200, _COMPLETED_OUTPUT)],
        )
        client = _client_with(session, api_key="my-secret-key")

        client.complete(_prompt())

        assert session.post_calls[0]["headers"]["Authorization"] == "Bearer my-secret-key"


class TestErrorHandling:
    def test_missing_job_id_raises_llm_transient(self) -> None:
        session = _FakeSession(post_responses=[_FakeResponse(200, {"unexpected": "shape"})])
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_connection_error_on_submit_raises_llm_transient(self) -> None:
        session = _FakeSession(post_exc=requests.exceptions.ConnectionError("refused"))
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_timeout_on_submit_raises_llm_transient(self) -> None:
        session = _FakeSession(post_exc=requests.exceptions.Timeout("slow"))
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_5xx_on_submit_raises_llm_transient(self) -> None:
        session = _FakeSession(post_responses=[_FakeResponse(503, {}, text="unavailable")])
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_4xx_on_submit_propagates_unwrapped(self) -> None:
        # A 401/400 from RunPod is a config/auth bug, not a transient
        # condition -- it must NOT be silently retried via SQS.
        session = _FakeSession(post_responses=[_FakeResponse(401, {}, text="unauthorized")])
        client = _client_with(session)

        with pytest.raises(requests.exceptions.HTTPError):
            client.complete(_prompt())

    def test_failed_job_status_raises_llm_transient(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[_FakeResponse(200, {"status": "FAILED", "error": "OOM"})],
        )
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_empty_output_raises_llm_transient(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[_FakeResponse(200, {"status": "COMPLETED", "output": []})],
        )
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_malformed_output_shape_raises_llm_transient(self) -> None:
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=[
                _FakeResponse(200, {"status": "COMPLETED", "output": [{"unexpected": "shape"}]})
            ],
        )
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_poll_exhaustion_raises_llm_transient(self) -> None:
        get_responses = [_FakeResponse(200, {"status": "IN_PROGRESS"}) for _ in range(3)]
        session = _FakeSession(
            post_responses=[_FakeResponse(200, {"id": "job-1"})],
            get_responses=get_responses,
        )
        client = _client_with(session, poll_max_attempts=3)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())
