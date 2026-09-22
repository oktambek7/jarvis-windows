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
import subprocess
from pathlib import Path

from .. import winplat
from .agentwork import deliver
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


def claude_auth_status(cfg, timeout: float = 20.0) -> tuple[bool | None, str]:
    """Whether the Claude CLI is signed in, and whose account it would spend.

    Returns (logged_in, detail). logged_in is None when the question could not
    be answered at all — no CLI, or a build predating `claude auth status` —
    which is different from a confident "not signed in" and must not be
    reported as a failure.

    `claude auth status --json` is a local credential read: it costs no tokens
    and contacts no model, so --doctor can afford to run it every time.
    """
    executable = claude_executable(cfg)
    if executable is None:
        return None, "topilmadi — npm install -g @anthropic-ai/claude-code"

    argv = winplat.launch_argv(executable, ["auth", "status", "--json"])
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"holatini aniqlab bo'lmadi: {exc}"

    try:
        payload = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        # An older CLI without the subcommand prints usage text, not JSON; a
        # crashed one prints nothing at all. Neither means "signed out".
        return None, f"{executable} (holat aniqlanmadi — CLI'ni yangilang)"
    if not isinstance(payload, dict) or "loggedIn" not in payload:
        return None, f"{executable} (holat aniqlanmadi)"

    if not payload.get("loggedIn"):
        return False, "tizimga kirilmagan — `claude auth login` ni ishga tushiring"

    # Which account foots the bill is the whole point of showing this: a
    # delegated task spends whatever this line names, not the Gemini key.
    bits = [str(payload.get("email") or payload.get("authMethod") or "logged in")]
    plan = str(payload.get("subscriptionType") or "").strip()
    if plan:
        bits.append(plan)
    return True, " — ".join(bits)


def _claude_argv(cfg, task: str, cwd: Path, model: str) -> list[str]:
    # On Windows npm installs the CLI as claude.cmd, and CreateProcess cannot
    # execute a batch file — it must be launched through cmd /c. Resolving the
    # full path first also means a PATH that is fine in the shell but not in
    # this process fails with a clear message instead of WinError 2.
    executable = claude_executable(cfg) or str(cfg.get("claude.command", "claude"))
    args = [
        "-p", task,
        "--output-format", "json",
        "--model", model,
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


async def _run_claude(cfg, task: str, cwd: Path, model: str) -> tuple[bool, str, bool]:
    """Returns (ok, text, timed_out). timed_out is broken out separately so
    the caller can decide whether escalating to a stronger model is worth it
    — retrying a timeout on a slower, pricier model just pays double for
    another likely timeout, so that case skips escalation entirely.
    """
    proc = await asyncio.create_subprocess_exec(
        *_claude_argv(cfg, task, cwd, model),
        stdin=asyncio.subprocess.DEVNULL,
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
        return False, f"Vazifa {int(timeout)} soniyada tugamadi.", True

    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip()[:1000]
        return False, f"Claude xato qaytardi: {detail or 'unknown error'}", False

    return True, _extract_text(out.decode("utf-8", "replace")), False


async def run_claude_with_escalation(cfg, task: str, cwd: Path, log=None) -> tuple[bool, str]:
    """Try the cheap model first; only pay for the expensive one if the cheap
    run actually failed (crash, non-zero exit) — not merely timed out.

    This keeps the common case (task succeeds) on the cheap model while still
    giving hard tasks a real second chance, so tightening the budget doesn't
    turn into "Jarvis just fails more often."
    """
    primary = str(cfg.get("claude.model", "sonnet"))
    ok, text, timed_out = await _run_claude(cfg, task, cwd, primary)
    if ok or timed_out:
        return ok, text

    escalate = str(cfg.get("claude.escalate_model", "") or "").strip()
    if not escalate or escalate == primary:
        return ok, text

    if log:
        log.warn(f"{primary} vazifani bajara olmadi — {escalate} bilan qayta urinildi")
    ok2, text2, _ = await _run_claude(cfg, task, cwd, escalate)
    return ok2, text2


@registry.tool(
    name="delegate_to_claude",
    description=(
        "Hand a substantial task to Claude Code, which has full autonomous access "
        "to the user's machine and can work for minutes. This spends Claude "
        "tokens, which are the more limited budget — prefer delegate_to_gemini "
        "for the same class of task, and only reach for this one if Gemini's "
        "agent already failed, or the user explicitly asks for Claude. USE THIS "
        "FOR: writing or editing code, fixing bugs, refactoring, creating "
        "projects, running and interpreting test suites, git workflows, research "
        "across many files, installing and configuring software, anything "
        "needing several steps or real reasoning. DO NOT use it for: one-liners "
        "like checking the time, opening an app or reading a single file (use "
        "run_shell); looking something up or opening a link (use google_search / "
        "open_url). Set background=true for anything expected to take over a "
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

    log = ctx.get("log")

    async def _runner() -> tuple[bool, str]:
        return await run_claude_with_escalation(cfg, task, workdir, log)

    return await deliver(ctx, surface, task, str(workdir), background, _runner, backend="Claude")


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
