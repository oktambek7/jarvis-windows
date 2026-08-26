"""Hand hard work to Claude Code.

Division of labour:
  Gemini Live  = ears, mouth, reflexes. Sub-second. Knows when to defer.
  Claude Code  = hands and long-form thinking. Writes code, refactors repos,
                 researches, drives multi-step tasks to completion.

Gemini decides which is which. If a request needs more than one or two shell
commands to satisfy, it should land here.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .. import winplat
from .base import registry

# What Claude is told about the context it's running in. Without this it writes
# long markdown reports that are miserable to listen to out loud.
VOICE_CONTEXT = (
    "You are operating as the execution engine for a voice assistant called "
    "Jarvis. The user speaks Uzbek and hears your final message read aloud by a "
    "text-to-speech voice.\n"
    "- Do the work fully. You have full tool access; do not ask for permission.\n"
    "- Your FINAL message is the only thing spoken. Make it 1-3 short sentences "
    "in Uzbek (Latin script), stating what you did and the outcome.\n"
    "- Never emit markdown, code blocks, file trees, or bullet lists in the "
    "final message. Nobody can hear formatting.\n"
    "- If you could not finish, say plainly in Uzbek what blocked you."
)


def claude_executable(cfg) -> str | None:
    """Absolute path to the Claude CLI, or None if it is not installed."""
    return winplat.resolve_executable(str(cfg.get("claude.command", "claude")))


def _claude_argv(cfg, task: str, cwd: Path) -> list[str]:
    # On Windows npm installs the CLI as claude.cmd, and CreateProcess cannot
    # execute a batch file — it must be launched through cmd /c. Resolving the
    # full path first also means a PATH that is fine in the shell but not in
    # this process fails with a clear message instead of WinError 2.
    executable = claude_executable(cfg) or str(cfg.get("claude.command", "claude"))
    args = [
        "-p", task,
        "--output-format", "json",
        "--model", str(cfg.get("claude.model", "opus")),
        "--append-system-prompt", VOICE_CONTEXT,
        "--add-dir", str(cwd),
    ]
    # Full autonomy means Claude shouldn't stop to ask either — there's no
    # keyboard in a voice loop to approve anything with.
    if cfg.is_full_autonomy:
        args.append("--dangerously-skip-permissions")
    else:
        args += ["--permission-mode", "acceptEdits"]
    return winplat.launch_argv(executable, args)


def _extract_text(stdout: str) -> str:
    """Pull the final assistant message out of `--output-format json`."""
    stdout = stdout.strip()
    if not stdout:
        return ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout[:4000]

    if isinstance(payload, dict):
        for key in ("result", "text", "content"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return stdout[:4000]


async def _run_claude(cfg, task: str, cwd: Path) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        *_claude_argv(cfg, task, cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env={**os.environ, "CLAUDE_CODE_NONINTERACTIVE": "1"},
    )
    timeout = float(cfg.get("claude.timeout", 900))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"Vazifa {int(timeout)} soniyada tugamadi."

    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip()[:1000]
        return False, f"Claude xato qaytardi: {detail or 'unknown error'}"

    return True, _extract_text(out.decode("utf-8", "replace"))


@registry.tool(
    name="delegate_to_claude",
    description=(
        "Hand a substantial task to Claude Code, which has full autonomous access "
        "to the user's machine and can work for minutes. USE THIS FOR: writing or "
        "editing code, fixing bugs, refactoring, creating projects, running and "
        "interpreting test suites, git workflows, research across many files, "
        "installing and configuring software, anything needing several steps or "
        "real reasoning. DO NOT use it for one-liners like checking the time, "
        "opening an app or reading a single file — do those yourself with "
        "run_shell. Set background=true for anything expected to take over a "
        "minute; the user will be notified when it finishes rather than waiting "
        "in silence."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": (
                    "Full, self-contained instructions in English. Claude cannot see "
                    "the conversation, so restate all needed context, file paths and "
                    "the definition of done."
                ),
            },
            "cwd": {
                "type": "STRING",
                "description": "Project directory to work in. Defaults to the user's home.",
            },
            "background": {
                "type": "BOOLEAN",
                "description": "Run without blocking the conversation. Default false.",
            },
        },
        "required": ["task"],
    },
)
async def delegate_to_claude(
    task: str, ctx: dict, surface: str = "voice", cwd: str | None = None, background: bool = False
) -> dict:
    cfg = ctx["config"]
    if not cfg.get("claude.enabled", True):
        return {"ok": False, "error": "Claude delegation is disabled in config.yaml."}
    if claude_executable(cfg) is None:
        return {
            "ok": False,
            "error": (
                "Claude Code CLI topilmadi. O'rnatish: npm install -g "
                "@anthropic-ai/claude-code"
            ),
        }

    workdir = Path(os.path.expandvars(cwd or str(cfg.get("claude.default_cwd", "~")))).expanduser()
    if not workdir.is_dir():
        return {"ok": False, "error": f"Directory does not exist: {workdir}"}

    memory = ctx.get("memory")
    job_id = memory.job_start(surface, task, str(workdir)) if memory else -1

    if not background:
        ok, text = await _run_claude(cfg, task, workdir)
        if memory:
            memory.job_finish(job_id, ok, text)
        return {"ok": ok, "job_id": job_id, ("result" if ok else "error"): text}

    # Background: return immediately, announce the result when it lands.
    async def _bg() -> None:
        ok, text = await _run_claude(cfg, task, workdir)
        if memory:
            memory.job_finish(job_id, ok, text)
        announce = ctx.get("announce")
        if announce:
            prefix = "Vazifa bajarildi" if ok else "Vazifa bajarilmadi"
            await announce(f"{prefix}: {text}")

    asyncio.create_task(_bg())
    return {
        "ok": True,
        "job_id": job_id,
        "result": (
            "Vazifa fonda ishga tushirildi. Tugagach xabar beraman. "
            "Tell the user you've started it and will report back."
        ),
    }


@registry.tool(
    name="check_jobs",
    description=(
        "List recent background tasks handed to Claude and their status. Use when "
        "the user asks 'is it done', 'nima bo'ldi', 'how's that task going'."
    ),
    parameters={"type": "OBJECT", "properties": {}},
)
async def check_jobs(ctx: dict) -> dict:
    memory = ctx.get("memory")
    if memory is None:
        return {"ok": False, "error": "Memory unavailable."}
    jobs = memory.recent_jobs(8)
    if not jobs:
        return {"ok": True, "result": "Hech qanday vazifa yo'q."}
    return {
        "ok": True,
        "jobs": [
            {
                "id": j["id"],
                "status": j["status"],
                "task": (j["task"] or "")[:180],
                "result": (j["result"] or "")[:400],
            }
            for j in jobs
        ],
    }
