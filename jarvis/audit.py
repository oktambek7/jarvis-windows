"""Append-only audit log.

Running at autonomy: "full" means nothing stops a tool call, so the log is the
only record of what Jarvis actually did. Every tool invocation lands here
before it runs and again when it finishes, so a crash mid-command still leaves
a trace of what was attempted.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, record: dict[str, Any]) -> None:
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def tool_start(self, surface: str, tool: str, args: dict[str, Any]) -> None:
        self._write({"event": "tool_start", "surface": surface, "tool": tool, "args": args})

    def tool_end(
        self,
        surface: str,
        tool: str,
        ok: bool,
        result: Any = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self._write(
            {
                "event": "tool_end",
                "surface": surface,
                "tool": tool,
                "ok": ok,
                # Truncate: audit log is for "what happened", not a data dump.
                "result": (str(result)[:2000] if result is not None else None),
                "error": error,
                "duration_ms": duration_ms,
            }
        )

    def note(self, surface: str, message: str, **extra: Any) -> None:
        self._write({"event": "note", "surface": surface, "message": message, **extra})
