"""RunPod Serverless / vLLM adapter for the LLMClient port.

Matches the async ``/run`` + poll ``/status/{job_id}`` pattern the user
provided as a reference (see ``runpod_context.py`` at the repo root,
from an unrelated prior project) rather than RunPod's OpenAI-compatible
route, since that reference is the known-working shape for this infra.

**Unverified assumption, flagged explicitly**: the guided-JSON field
sent to the vLLM worker is ``sampling_params.guided_decoding.json``,
which matches vLLM's current (>=0.6) ``GuidedDecodingParams`` shape.
Older ``worker-vllm`` deployments instead expect a top-level
``sampling_params.guided_json`` key. There is no RunPod endpoint
reachable from this dev sandbox to confirm which one this deployment
uses -- ``_GUIDED_JSON_PARAM_KEY`` below is the single line to change
if the first real call comes back without the schema being enforced.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from summarizer.domain.errors import LlmTransient
from summarizer.domain.models import LlmRawResponse, Prompt

logger = logging.getLogger(__name__)

_GUIDED_JSON_PARAM_KEY = "guided_decoding"  # see module docstring


class RunpodVllmClient:
    """One guided-JSON call to a RunPod Serverless vLLM endpoint.

    Raises ``LlmTransient`` on connection errors, timeouts, 5xx
    responses from the RunPod control plane, and non-terminal/failed
    job outcomes (RunPod downtime has no fallback provider -- per
    CLAUDE.md it's handled via standard SQS retry, so job failures are
    treated as retryable rather than terminal). A 4xx from RunPod
    itself (bad API key, bad endpoint id) is a config error, not a
    transient one, and is left to propagate unwrapped.
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
        poll_interval_seconds: float = 1.0,
        poll_max_attempts: int = 180,
        request_timeout_seconds: int = 120,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version

        self._run_url = f"https://api.runpod.ai/v2/{endpoint_id}/run"
        self._status_url = f"https://api.runpod.ai/v2/{endpoint_id}/status"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._top_p = top_p
        self._repetition_penalty = repetition_penalty
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_max_attempts = poll_max_attempts
        self._request_timeout_seconds = request_timeout_seconds
        self._session = requests.Session()

    def complete(self, prompt: Prompt) -> LlmRawResponse:
        job_id = self._submit(prompt)
        return self._poll_until_complete(job_id)

    # ── Submission ───────────────────────────────────────────────

    def _submit(self, prompt: Prompt) -> str:
        payload: dict[str, Any] = {
            "input": {
                "prompt": prompt.text,
                "sampling_params": {
                    "max_tokens": self._max_output_tokens,
                    "temperature": self._temperature,
                    "top_p": self._top_p,
                    "repetition_penalty": self._repetition_penalty,
                    _GUIDED_JSON_PARAM_KEY: {"json": prompt.json_schema},
                },
            }
        }

        response = self._post_or_get(
            lambda: self._session.post(
                self._run_url,
                headers=self._headers,
                json=payload,
                timeout=self._request_timeout_seconds,
            )
        )

        data = response.json()
        job_id = data.get("id")
        if not job_id:
            raise LlmTransient(f"RunPod /run response missing job id: {data}")
        return str(job_id)

    # ── Polling ──────────────────────────────────────────────────

    def _poll_until_complete(self, job_id: str) -> LlmRawResponse:
        for _ in range(self._poll_max_attempts):
            response = self._post_or_get(
                lambda: self._session.get(
                    f"{self._status_url}/{job_id}",
                    headers=self._headers,
                    timeout=self._request_timeout_seconds,
                )
            )
            data = response.json()
            status = data.get("status")

            if status == "COMPLETED":
                return self._parse_completed(data)
            if status in ("IN_QUEUE", "IN_PROGRESS"):
                time.sleep(self._poll_interval_seconds)
                continue
            raise LlmTransient(f"RunPod job {job_id} did not complete: status={status} data={data}")

        raise LlmTransient(
            f"RunPod job {job_id} timed out after {self._poll_max_attempts} polls"
        )

    def _post_or_get(self, call: Callable[[], requests.Response]) -> requests.Response:
        try:
            response = call()
        except requests.exceptions.ConnectionError as exc:
            raise LlmTransient(f"Connection error calling RunPod: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise LlmTransient(f"Timeout calling RunPod: {exc}") from exc

        if response.status_code >= 500:
            raise LlmTransient(f"RunPod returned {response.status_code}: {response.text}")
        response.raise_for_status()  # 4xx: config/auth bug, propagate unwrapped
        return response

    @staticmethod
    def _parse_completed(data: dict[str, Any]) -> LlmRawResponse:
        output = data.get("output")
        if not output:
            raise LlmTransient(f"RunPod job completed with no output: {data}")

        try:
            choice = output[0]["choices"][0]
            text = str(choice["tokens"][0]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmTransient(f"Unexpected RunPod output format: {data}") from exc

        usage = output[0].get("usage") if isinstance(output[0], dict) else None
        token_input = usage.get("input") if isinstance(usage, dict) else None
        token_output = usage.get("output") if isinstance(usage, dict) else None

        return LlmRawResponse(text=text, token_input=token_input, token_output=token_output)
