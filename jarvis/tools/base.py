"""Tool registry.

A tool is declared exactly once here and becomes available to every surface:
the Gemini Live voice session and the one-shot `--text` CLI path. Add a tool
with the @tool decorator and it shows up in both automatically.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# PowerShell / cmd fragments that can ruin your day. Only consulted when
# agent.autonomy is "guarded" — at "full" these run without asking.
#
# This list is a SAFETY CONTROL, not decoration. In guarded mode it is the only
# thing standing between a misheard Uzbek voice command and an unrecoverable
# action, so it errs toward catching too much. A false positive costs one
# spoken "ha"; a false negative can cost a directory.
#
# Windows-specific traps worth knowing about:
#   - Remove-Item has the aliases rm, del, erase, rd, rmdir, ri — all of which
#     the model may emit, so the alias forms are matched too.
#   - `format` and `diskpart` are unrecoverable.
#   - Registry writes under HKLM: can make a machine unbootable.
DESTRUCTIVE_PATTERNS = [
    # --- deletion ---
    r"\bRemove-Item\b",
    r"\b(?:rm|del|erase|rd|rmdir|ri)\b.*(?:-Recurse|-Force|/s\b|/q\b|\*)",
    r"\bClear-(?:Content|Disk|RecycleBin)\b",
    r"\bRemove-(?:ItemProperty|Partition|PSDrive|LocalUser|ADUser)\b",

    # --- disk and volume ---
    r"\bformat\b.*/(?:fs|q|y)\b", r"\bFormat-Volume\b", r"\bdiskpart\b",
    r"\bInitialize-Disk\b", r"\bClear-Disk\b", r"\bSet-Partition\b",

    # --- power and process control ---
    r"\b(?:Stop|Restart)-Computer\b", r"\bshutdown(?:\.exe)?\b", r"\blogoff\b",
    r"\bStop-Process\b", r"\btaskkill\b", r"\bStop-Service\b",
    r"\bSet-Service\b", r"\bRestart-Service\b",

    # --- package management and installers ---
    r"\bwinget\s+(?:install|uninstall|upgrade)\b",
    r"\bchoco(?:latey)?\s+(?:install|uninstall|upgrade)\b",
    r"\bscoop\s+(?:install|uninstall)\b",
    r"\bpip\s+(?:install|uninstall)\b", r"\bnpm\s+(?:publish|uninstall)\b",
    r"\bInstall-(?:Module|Package|WindowsFeature)\b",
    r"\bUninstall-(?:Module|Package)\b",
    r"\bStart-Process\b.*-Verb\s+RunAs\b",

    # --- registry and boot ---
    r"\breg(?:\.exe)?\s+(?:delete|add|import)\b",
    r"\b(?:Set|New|Remove)-ItemProperty\b.*HK(?:LM|CU|CR|U|CC):",
    r"\bbcdedit\b", r"\bbootrec\b", r"\bsfc\b", r"\bDISM\b",

    # --- policy, scheduling, accounts ---
    r"\bSet-ExecutionPolicy\b", r"\bschtasks\b",
    r"\b(?:Register|Unregister)-ScheduledTask\b",
    r"\bnet\s+(?:user|localgroup)\b", r"\bAdd-LocalGroupMember\b",
    r"\bSet-ACL\b", r"\bicacls\b", r"\btakeown\b",

    # --- git operations that rewrite or publish ---
    r"\bgit\s+push\b", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\b",

    # --- network writes ---
    r"\bInvoke-(?:WebRequest|RestMethod)\b.*-Method\s*(?:POST|PUT|DELETE|PATCH)",
    r"\bcurl\b.*-X\s*(?:POST|PUT|DELETE|PATCH)",
    r"\bSet-NetFirewallProfile\b", r"\bnetsh\b",

    # --- remote code execution ---
    r"\bInvoke-Expression\b", r"\biex\b", r"\bDownloadString\b",
]
_DESTRUCTIVE_RE = re.compile("|".join(DESTRUCTIVE_PATTERNS), re.IGNORECASE)


def looks_destructive(text: str) -> bool:
    return bool(_DESTRUCTIVE_RE.search(text or ""))


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    # If True, guarded mode always confirms. If False, the handler may still
    # self-report as destructive at call time (e.g. run_shell inspecting argv).
    always_destructive: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # Tools hidden because their integration isn't configured. Better to
        # not offer Gemini a tool at all than to have it confidently call
        # something that always errors.
        self._disabled: set[str] = set()
        # Injected by the runtime so handlers can reach shared services.
        self.ctx: dict[str, Any] = {}

    def disable(self, *names: str) -> None:
        self._disabled.update(names)

    def enabled_tools(self) -> list[Tool]:
        return [t for name, t in self._tools.items() if name not in self._disabled]

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        always_destructive: bool = False,
    ):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters or {"type": "OBJECT", "properties": {}},
                    handler=fn,
                    always_destructive=always_destructive,
                )
            )
            return fn

        return deco

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(n for n in self._tools if n not in self._disabled)

    def gemini_declarations(self) -> list[dict[str, Any]]:
        """Render every enabled tool as a Gemini function declaration."""
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self.enabled_tools()
        ]

    async def invoke(self, name: str, args: dict[str, Any], surface: str = "voice") -> dict[str, Any]:
        """Run a tool by name. Never raises — errors come back as a result dict
        so the model can hear about the failure and try something else."""
        audit = self.ctx.get("audit")
        tool = self._tools.get(name)

        if tool is None:
            if audit:
                audit.note(surface, f"unknown tool requested: {name}")
            return {"ok": False, "error": f"Unknown tool: {name}"}

        if audit:
            audit.tool_start(surface, name, args)

        started = time.perf_counter()
        try:
            # Handlers receive the registry context as `ctx` if they ask for it.
            sig = inspect.signature(tool.handler)
            call_args = dict(args)
            if "ctx" in sig.parameters:
                call_args["ctx"] = self.ctx
            if "surface" in sig.parameters:
                call_args["surface"] = surface

            result = tool.handler(**call_args)
            if inspect.isawaitable(result):
                result = await result

            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            result.setdefault("ok", True)

            elapsed = int((time.perf_counter() - started) * 1000)
            if audit:
                audit.tool_end(surface, name, result.get("ok", True),
                               result.get("result") or result.get("error"), None, elapsed)
            return result

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a crashed tool must not kill the session
            elapsed = int((time.perf_counter() - started) * 1000)
            msg = f"{type(exc).__name__}: {exc}"
            if audit:
                audit.tool_end(surface, name, False, None, msg, elapsed)
            return {"ok": False, "error": msg}


# One global registry; tool modules import this and decorate against it.
registry = ToolRegistry()
