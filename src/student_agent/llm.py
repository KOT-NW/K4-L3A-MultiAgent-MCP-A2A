from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QwenConfig:
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3:8b"
    api_key: str = "ollama"
    timeout_s: float = 120.0


class QwenClient:
    """OpenAI-compatible client for local Ollama Qwen3-8B.

    Uses `openai` lib pointed at Ollama's /v1 endpoint so the lab stays
    under 10B params with a reproducible local model.
    """

    def __init__(self, config: QwenConfig | None = None) -> None:
        self.config = config or QwenConfig()

    def _chat_sync(self, system: str, user: str) -> str:
        from openai import OpenAI

        client = OpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.timeout_s,
        )
        resp = client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or "{}"

    async def generate_json(
        self, system: str, user: str, *, retries: int = 2
    ) -> dict[str, Any] | None:
        last_error: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                text = await asyncio.to_thread(self._chat_sync, system, user)
                value = json.loads(text)
                if isinstance(value, dict):
                    return value
            except Exception as exc:  # noqa: BLE001 - fall through to retry/fallback
                last_error = exc
        _ = last_error
        return None
