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
    llm_api_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "meta-llama/llama-3.1-8b-instruct"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        llm_api_url = os.getenv("LLM_API_URL", "").strip()
        llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        llm_model = os.getenv("LLM_MODEL", "meta-llama/llama-3.1-8b-instruct").strip()
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if llm_api_key and not llm_api_url.startswith(("http://", "https://")):
            errors.append("LLM_API_URL must be an absolute HTTP(S) URL when LLM_API_KEY is set")
        if llm_api_key and not llm_model:
            errors.append("LLM_MODEL must not be empty when LLM_API_KEY is set")
        if errors:
            raise ValueError("; ".join(errors))
        return cls(
            competition_api_url=api_url,
            team_api_key=team_key,
            mcp_endpoint=mcp_endpoint,
            root=resolved_root,
            llm_api_url=llm_api_url or None,
            llm_api_key=llm_api_key or None,
            llm_model=llm_model,
        )
