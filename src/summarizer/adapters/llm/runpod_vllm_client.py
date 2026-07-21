"""RunPod Serverless / worker-vllm adapter for the LLMClient port.

Calls worker-vllm's **OpenAI-compatible** route
(``/openai/v1/chat/completions``) rather than RunPod's native ``/run``
+ poll ``/status`` handler -- confirmed against this deployment
(worker-vllm v2.14.0, guided decoding via the ``outlines`` backend).
This is a single synchronous call: RunPod's OpenAI-compatible route
blocks until the job completes (or the request times out), so there is
no job-id polling loop here, unlike the ``runpod_context.py`` reference
snippet at the repo root (which targets the native handler of an
unrelated prior project).

Guided decoding uses vLLM's own OpenAI-server convention: a flat
top-level ``guided_json`` field on the chat-completion request body.
This is a stable, long-standing vLLM convention (distinct from the
native handler's ``SamplingParams``-based guided-decoding field, which
changed shape across vLLM versions) -- more confident here than the
prior native-handler implementation was.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from summarizer.domain.errors import LlmTransient
from summarizer.domain.models import LlmRawResponse, Prompt

logger = logging.getLogger(__name__)


class RunpodVllmClient:
    """One guided-JSON chat-completion call to a RunPod worker-vllm
    OpenAI-compatible endpoint.

    Raises ``LlmTransient`` on connection errors, timeouts, and 5xx
    responses (RunPod downtime has no fallback provider -- per
    CLAUDE.md it's handled via standard SQS retry). A 4xx is a
    config/auth/model-name error, not a transient one, and is left to
    propagate unwrapped so it fails loudly instead of retrying forever.
    """

    def __init__(
        self,
        endpoint_id: str,
        api_key: str,
        model_name: str,
        model_version: str,
        *,
        max_output_tokens: int = 2048,
        temperature: float = 0.0,
        top_p: float = 1.0,
        repetition_penalty: float = 1.05,
        request_timeout_seconds: int = 600,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version

        self._url = f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._top_p = top_p
        self._repetition_penalty = repetition_penalty
        self._request_timeout_seconds = request_timeout_seconds
        self._session = requests.Session()

    def complete(self, prompt: Prompt) -> LlmRawResponse:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": prompt.system_message},
                {"role": "user", "content": prompt.user_message},
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "repetition_penalty": self._repetition_penalty,
            "guided_json": prompt.json_schema,
        }

        try:
            response = self._session.post(
                self._url,
                headers=self._headers,
                json=payload,
                timeout=self._request_timeout_seconds,
            )
        except requests.exceptions.ConnectionError as exc:
            raise LlmTransient(f"Connection error calling RunPod: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise LlmTransient(f"Timeout calling RunPod: {exc}") from exc

        if response.status_code >= 500:
            raise LlmTransient(f"RunPod returned {response.status_code}: {response.text}")
        response.raise_for_status()  # 4xx: config/auth/model bug, propagate unwrapped

        return self._parse_response(response.json())

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LlmRawResponse:
        try:
            text = str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmTransient(f"Unexpected RunPod response format: {data}") from exc

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        token_input = usage.get("prompt_tokens") if usage else None
        token_output = usage.get("completion_tokens") if usage else None

        return LlmRawResponse(text=text, token_input=token_input, token_output=token_output)
