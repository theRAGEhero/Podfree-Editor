"""Shared helpers for calling OpenRouter-hosted language models."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

import requests

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Pricing in USD per million tokens (input, output)
MODEL_PRICING: Mapping[str, tuple[float, float]] = {
    "anthropic/claude-3.7-sonnet": (3.1579, 15.7895),
    "anthropic/claude-3.5-haiku": (0.8421, 4.2105),
    "anthropic/claude-3-haiku": (0.2632, 1.3158),
    "deepseek/deepseek-r1": (0.4211, 2.1053),
    "amazon/nova-lite-v1": (0.0632, 0.2526),
}

DEFAULT_MAX_OUTPUT_TOKENS = 1500

def load_env_file(*candidates: Path) -> None:
    """Populate os.environ with values from the first existing .env file."""
    for env_path in candidates:
        if not env_path or not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if value and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        break


def get_api_key() -> Optional[str]:
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent.parent
    load_env_file(project_root / ".env")
    load_env_file(script_dir.parent / ".env")  # scripts/.env fallback
    load_env_file(Path.cwd() / ".env")
    return os.getenv("OPENROUTER_API_KEY")


def estimate_call_cost(model: str, input_tokens: int, output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS) -> Optional[float]:
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return None
    price_in, price_out = pricing
    cost = (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out
    return cost


def estimate_hourly_cost(model: str, input_token_ratio: float = 0.9) -> Optional[float]:
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return None
    price_in, price_out = pricing
    total_tokens = 60 * 150 * 1.3  # 60 minutes * 150 words/min * tokens-per-word heuristic
    input_tokens = total_tokens * input_token_ratio
    output_tokens = total_tokens - input_tokens
    cost = (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out
    return cost


def call_openrouter(
    api_key: str,
    *,
    model: str,
    messages: Iterable[Mapping[str, str]],
    temperature: float = 0.2,
    extra: Optional[Dict[str, object]] = None,
    timeout: int = 90,
) -> Dict[str, object]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, object] = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
    }
    if extra:
        payload.update(extra)

    response = requests.post(OPENROUTER_BASE_URL, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def extract_content(response: Mapping[str, object]) -> str:
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("Model response missing choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise ValueError("Model response missing content")
    if isinstance(content, str):
        return content.strip()
    return json.dumps(content)
