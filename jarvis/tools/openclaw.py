"""Hand hard work to a self-hosted OpenClaw gateway instead of Gemini or Claude.

OpenClaw is a separate, already-running Docker deployment (see
docs/OPENCLAW.md) with its own Telegram bot, skills, and a custom
non-Gemini/Claude model provider. Jarvis stays the ears and mouth; this
hands substantial tasks to OpenClaw's own agent through its `openclaw agent`
CLI, run inside the gateway container with `docker exec` — spending neither
Gemini nor Claude quota.

Division of labour, alongside delegate_to_gemini and delegate_to_claude:
  Gemini Live         = ears, mouth, reflexes.
  delegate_to_gemini  = default hands, spends Gemini quota.
  delegate_to_claude  = fallback hands, spends Claude quota, used if Gemini's
                        agent fails or the user explicitly asks for Claude.
  delegate_to_openclaw = hands for anything the user explicitly wants routed
                        through OpenClaw instead — its own skills (Google
                        Workspace, browser automation, etc.), or simply to
                        spend neither Gemini's nor Claude's quota.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

from .. import winplat
from .agentwork import deliver
from .base import registry


def container_name(cfg) -> str:
    return str(cfg.get("openclaw.container", "workshop-openclaw-gateway-1"))


def agent_id(cfg) -> str:
    return str(cfg.get("openclaw.agent_id", "main"))


def docker_executable() -> str | None:
    return winplat.resolve_executable("docker")


def container_running(cfg) -> bool:
    """Whether the configured OpenClaw gateway container is up right now.

    A quick, synchronous `docker inspect` — cheap enough to run at startup so
    Jarvis can hide delegate_to_openclaw entirely instead of offering a tool
    that would always fail (same philosophy as the Claude CLI / Telegram
    session checks in app.py).
    """
    docker = docker_executable()
    if docker is None:
        return False
    try:
        out = subprocess.run(
            [docker, "inspect", "--format", "{{.State.Running}}", container_name(cfg)],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.decode("utf-8", "replace").strip() == "true"


def _extract_reply(stdout: str) -> str:
    """OpenClaw's own `agent` CLI streams progress to stdout as it works and
    is not guaranteed to end in a single JSON object — its own tg-watcher.js
    (workspace/skills/meeting-scanner/scripts/tg-watcher.js in the OpenClaw
    workshop) just reads the last non-empty line as the reply. Try structured
    JSON first since some OpenClaw commands support --json, then fall back to
    that same last-line heuristic so this stays in sync with how OpenClaw
    reads its own output.
    """
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            for key in ("result", "text", "message", "reply"):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return lines[-1] if lines else ""


async def _run_openclaw_agent(cfg, task: str) -> tuple[bool, str]:
    docker = docker_executable()
    if docker is None:
        return False, "Docker topilmadi — OpenClaw ishlamayapti."

    timeout = int(cfg.get("openclaw.timeout", 300))
    argv = [
        docker, "exec", container_name(cfg),
        "node", "/app/dist/index.js", "agent",
        "--agent", agent_id(cfg),
        "--local",
        "--timeout", str(timeout),
        "--message", task,
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"OpenClaw {timeout} soniyada javob bermadi."

    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip()[:1000]
        return False, f"OpenClaw xato qaytardi: {detail or 'unknown error'}"

    reply = _extract_reply(out.decode("utf-8", "replace"))
    return True, reply or "Vazifa bajarildi."


@registry.tool(
    name="delegate_to_openclaw",
    description=(
        "Hand a substantial task to OpenClaw — a separate, self-hosted agent "
        "with its own Telegram bot, Google Workspace automation, browser "
        "automation and skills, running on a custom model that is neither "
        "Gemini nor Claude. Use this when the user explicitly asks for "
        "OpenClaw, or asks for something that is one of OpenClaw's own "
        "skills (e.g. Gmail/Calendar/Drive via Google Workspace, reading "
        "Telegram chat history, browser-based lookups). Otherwise prefer "
        "delegate_to_gemini for general multi-step work. Set background=true "
        "for anything expected to take over a minute; the user will be "
        "notified when it finishes rather than waiting in silence."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": (
                    "Full, self-contained instructions (Uzbek or English). "
                    "OpenClaw cannot see this conversation, so restate all "
                    "needed context and the definition of done."
                ),
            },
            "background": {
                "type": "BOOLEAN",
                "description": "Run without blocking the conversation. Default false.",
            },
        },
        "required": ["task"],
    },
)
async def delegate_to_openclaw(
    task: str, ctx: dict, surface: str = "voice", background: bool = False
) -> dict:
    cfg = ctx["config"]
    if not cfg.get("openclaw.enabled", True):
        return {"ok": False, "error": "OpenClaw delegation is disabled in config.yaml."}

    async def _runner() -> tuple[bool, str]:
        return await _run_openclaw_agent(cfg, task)

    return await deliver(ctx, surface, task, "openclaw", background, _runner, backend="OpenClaw")
