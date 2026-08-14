"""Provider-agnostic chat client with a JSON-object mode.

Defaults to OpenRouter's OpenAI-compatible endpoint; any OpenAI-compatible
base URL works (HYDRA_MEM_LLM_BASE_URL / HYDRA_MEM_LLM_API_KEY).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import config


class LLMError(RuntimeError):
    pass


def _unfence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_line = text.index("\n")
        text = text[first_line + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def chat_json(system: str, user: str) -> Any:
    if config.LLM_MODE == "mock":
        raise LLMError("LLM is in mock mode; no API key configured")
    if not config.LLM_API_KEY:
        raise LLMError(
            "HYDRA_MEM_LLM_API_KEY (or OPENROUTER_API_KEY) is not set; "
            "set it or run with HYDRA_MEM_LLM_MODE=mock"
        )
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc
    if resp.status_code != 200:
        raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Malformed LLM response: {body}") from exc
    try:
        return json.loads(_unfence(content))
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned non-JSON: {content[:500]}") from exc
