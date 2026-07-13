
import os
import re
import json
import time
import requests
import markdown

RUNPOD_ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID", "")

RUNPOD_RUN_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run"
RUNPOD_STATUS_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status"

RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")

HEADERS = {
    "Authorization": f"Bearer {RUNPOD_API_KEY}",
    "Content-Type": "application/json"
}


def _call_runpod(prompt: str) -> str:

    payload = {
        "input": {
            "prompt": prompt,
            "sampling_params": {
                "max_tokens": 400,
                "temperature": 0.0,
                "top_p": 1.0,
                "repetition_penalty": 1.05
            }
        }
    }

    response = requests.post(
        RUNPOD_RUN_URL,
        headers=HEADERS,
        json=payload,
        timeout=120
    )

    response.raise_for_status()
    result = response.json()

    if "id" not in result:
        raise RuntimeError(f"Unexpected RunPod response: {result}")

    job_id = result["id"]

    # Poll job status
    for _ in range(120):

        status_response = requests.get(
            f"{RUNPOD_STATUS_URL}/{job_id}",
            headers=HEADERS,
            timeout=120
        )

        status_response.raise_for_status()
        data = status_response.json()

        status = data.get("status")

        if status == "COMPLETED":

            output = data.get("output")

            if not output:
                raise RuntimeError(f"No output returned: {data}")

            try:
                # Extract model text from RunPod response
                text = output[0]["choices"][0]["tokens"][0]
                return text.strip()

            except Exception:
                raise RuntimeError(f"Unexpected RunPod output format: {data}")

        elif status in ["IN_QUEUE", "IN_PROGRESS"]:
            time.sleep(1)
            continue

        else:
            raise RuntimeError(f"RunPod job failed: {data}")

    raise TimeoutError("RunPod inference timed out")


# ────────────────────────────────────────────────
# CLEANING HELPERS
# ────────────────────────────────────────────────

def _strip_code_blocks(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S | re.I)
    text = re.sub(r"`{1,3}", "", text)
    return text


def _force_section_start(text: str) -> str:
    text = text.strip()

    section_starts = [
        "key insights", "potential reasons", "recommendations", "actionable next steps"
    ]

    for start in section_starts:
        idx = text.lower().find(start)
        if idx != -1:
            heading_match = re.search(r'^#{1,3}\s.*', text[:idx + len(start) + 100], re.M | re.I)
            if heading_match:
                return text[heading_match.start():]
            return text[idx - 50:]

    return text


def _clean_llm_output(text: str) -> str:
    if not text:
        return ""

    text = text.strip()
    text = _strip_code_blocks(text)
    text = _force_section_start(text)

    text = re.sub(r'\n{4,}', '\n\n', text)
    text = re.sub(r'^\s*json\s*$', '', text, flags=re.I | re.M)
    text = re.sub(r'\n+$', '', text)

    return text.strip()


def _clean_benchmark_output(text: str) -> str:
    text = _clean_llm_output(text)

    if "### Key Insights" not in text:
        return (
            "### Key Insights\n"
            "- Insufficient or unclear headline format for benchmarking.\n"
            "- Could not extract valid metric name or value.\n"
            "- Please provide headline in format: \"Metric Name: Value (Entity)\""
        )

    lines = text.splitlines()

    cleaned = []
    capture = False
    bullet_count = 0

    for line in lines:

        stripped = line.strip()

        if stripped.startswith("### Key Insights"):
            cleaned.append(line)
            capture = True
            continue

        if capture:

            if stripped.startswith("- ") and bullet_count < 5:
                cleaned.append(line)
                bullet_count += 1

            elif stripped == "" and bullet_count > 0:
                cleaned.append(line)

            elif stripped and not stripped.startswith("- "):
                break

    result = "\n".join(cleaned).rstrip()

    bullets = [l for l in result.splitlines() if l.strip().startswith("- ")]

    if len(bullets) < 3:
        return (
            "### Key Insights\n"
            "- Insufficient data or unclear headline for reliable comparison.\n"
            "- Missing clear numerical value or metric name.\n"
            "- Headline should follow format like: \"Attrition Rate: 12.5% (Tata Power)\""
        )

    return result.strip()

