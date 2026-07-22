from __future__ import annotations

from typing import Any

import pytest
import requests

from summarizer.adapters.llm.runpod_vllm_client import RunpodVllmClient
from summarizer.domain.errors import LlmTransient
from summarizer.domain.models import Prompt


def _prompt() -> Prompt:
    return Prompt(
        system_message="You are a precise summarizer. Schema: {...}",
        user_message="## Ticket Subject\nCannot log in\n\n## Email Conversation\n...",
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
    def __init__(self, response: _FakeResponse | None = None, raise_exc: Exception | None = None):
        self._response = response
        self._raise_exc = raise_exc
        self.last_call: dict[str, Any] | None = None

    def post(
        self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: int
    ) -> _FakeResponse:
        self.last_call = {"url": url, "headers": headers, "json": json, "timeout": timeout}
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._response is not None
        return self._response


def _client_with(session: _FakeSession, **overrides: Any) -> RunpodVllmClient:
    fields: dict[str, Any] = {
        "endpoint_id": "ep-123",
        "api_key": "secret",
        "model_name": "Qwen/Qwen2.5-7B-Instruct",
        "model_version": "2.5",
        **overrides,
    }
    client = RunpodVllmClient(**fields)
    client._session = session  # type: ignore[assignment]
    return client


_COMPLETED_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": '  {"human_summary": "ok"}  '}}],
}


class TestCompleteHappyPath:
    def test_returns_text_from_response(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, _COMPLETED_RESPONSE))
        client = _client_with(session)

        result = client.complete(_prompt())

        assert result.text == '{"human_summary": "ok"}'

    def test_extracts_usage_tokens_when_present(self) -> None:
        payload = {
            **_COMPLETED_RESPONSE,
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        session = _FakeSession(response=_FakeResponse(200, payload))
        client = _client_with(session)

        result = client.complete(_prompt())

        assert result.token_input == 100
        assert result.token_output == 20

    def test_usage_tokens_none_when_absent(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, _COMPLETED_RESPONSE))
        client = _client_with(session)

        result = client.complete(_prompt())

        assert result.token_input is None
        assert result.token_output is None

    def test_model_name_and_version_exposed_as_attributes(self) -> None:
        session = _FakeSession()
        client = _client_with(
            session, model_name="Qwen/Qwen2.5-7B-Instruct", model_version="2.5"
        )

        assert client.model_name == "Qwen/Qwen2.5-7B-Instruct"
        assert client.model_version == "2.5"

    def test_calls_openai_compatible_chat_completions_url(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, _COMPLETED_RESPONSE))
        client = _client_with(session)

        client.complete(_prompt())

        assert session.last_call is not None
        assert session.last_call["url"] == (
            "https://api.runpod.ai/v2/ep-123/openai/v1/chat/completions"
        )


class TestRequestPayload:
    def test_sends_system_and_user_messages_with_correct_roles(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, _COMPLETED_RESPONSE))
        client = _client_with(session)
        prompt = _prompt()

        client.complete(prompt)

        assert session.last_call is not None
        body = session.last_call["json"]
        assert body["messages"] == [
            {"role": "system", "content": prompt.system_message},
            {"role": "user", "content": prompt.user_message},
        ]

    def test_includes_response_format_schema_and_sampling_params(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, _COMPLETED_RESPONSE))
        client = _client_with(
            session, temperature=0.1, top_p=0.9, repetition_penalty=1.2, max_output_tokens=512
        )
        prompt = _prompt()

        client.complete(prompt)

        assert session.last_call is not None
        body = session.last_call["json"]
        assert body["model"] == "Qwen/Qwen2.5-7B-Instruct"
        assert body["max_tokens"] == 512
        assert body["temperature"] == 0.1
        assert body["top_p"] == 0.9
        assert body["repetition_penalty"] == 1.2
        # Structured output must go out as OpenAI `response_format`, never
        # as a flat top-level `guided_json` -- the live endpoint silently
        # ignores the latter and runs unconstrained (see the client's
        # module docstring for the 2026-07-22 proof).
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "LlmSummaryOutput", "schema": prompt.json_schema},
        }
        assert "guided_json" not in body

    def test_sends_bearer_auth_header(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, _COMPLETED_RESPONSE))
        client = _client_with(session, api_key="my-secret-key")

        client.complete(_prompt())

        assert session.last_call is not None
        assert session.last_call["headers"]["Authorization"] == "Bearer my-secret-key"

    def test_uses_configured_request_timeout(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, _COMPLETED_RESPONSE))
        client = _client_with(session, request_timeout_seconds=45)

        client.complete(_prompt())

        assert session.last_call is not None
        assert session.last_call["timeout"] == 45


class TestErrorHandling:
    def test_connection_error_raises_llm_transient(self) -> None:
        session = _FakeSession(raise_exc=requests.exceptions.ConnectionError("refused"))
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_timeout_raises_llm_transient(self) -> None:
        session = _FakeSession(raise_exc=requests.exceptions.Timeout("slow"))
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_5xx_raises_llm_transient(self) -> None:
        session = _FakeSession(response=_FakeResponse(503, {}, text="unavailable"))
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_4xx_propagates_unwrapped(self) -> None:
        # A 401/400/404 (bad key, bad endpoint, model-name mismatch) is
        # a config bug, not a transient condition -- it must NOT be
        # silently retried forever via SQS redelivery.
        session = _FakeSession(response=_FakeResponse(401, {}, text="unauthorized"))
        client = _client_with(session)

        with pytest.raises(requests.exceptions.HTTPError):
            client.complete(_prompt())

    def test_empty_choices_raises_llm_transient(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, {"choices": []}))
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())

    def test_malformed_response_shape_raises_llm_transient(self) -> None:
        session = _FakeSession(response=_FakeResponse(200, {"unexpected": "shape"}))
        client = _client_with(session)

        with pytest.raises(LlmTransient):
            client.complete(_prompt())
