"""Config loading: config.yaml + .env, with dotted lookup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Thin wrapper over the parsed config.yaml with `cfg.get("a.b.c")` lookup."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: str = "") -> Path:
        """Resolve a config value as a path, relative to the project root."""
        raw = str(self.get(dotted, default))
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (ROOT / p)

    # -- secrets come from the environment, never from config.yaml --

    @property
    def gemini_key(self) -> str:
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set.\n"
                "  1. cp .env.example .env\n"
                "  2. Get a free key at https://aistudio.google.com/apikey\n"
                "  3. Paste it into .env as GEMINI_API_KEY=..."
            )
        return key

    @property
    def aisha_key(self) -> str | None:
        return os.getenv("AISHA_API_KEY", "").strip() or None

    @property
    def is_full_autonomy(self) -> bool:
        return str(self.get("agent.autonomy", "guarded")).lower() == "full"


def load_config(path: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = path or (ROOT / "config.yaml")
    with open(cfg_path, encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh) or {})
