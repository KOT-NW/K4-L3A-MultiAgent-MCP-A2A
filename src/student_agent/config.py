from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TEAM_KEY_PATTERN = re.compile(r"^sk-team-[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Settings:
    competition_api_url: str
    team_api_key: str
    mcp_endpoint: str
    root: Path
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen3:8b"
    llm_api_key: str = "ollama"
    llm_timeout_s: float = 120.0

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        llm_base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").strip()
        llm_model = os.getenv("LLM_MODEL", "qwen3:8b").strip()
        llm_api_key = os.getenv("LLM_API_KEY", "ollama").strip()
        try:
            llm_timeout = float(os.getenv("LLM_TIMEOUT_S", "120"))
        except ValueError:
            llm_timeout = 120.0
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if errors:
            raise ValueError("; ".join(errors))
        return cls(api_url, team_key, mcp_endpoint, resolved_root,
                   llm_base_url, llm_model, llm_api_key, llm_timeout)
