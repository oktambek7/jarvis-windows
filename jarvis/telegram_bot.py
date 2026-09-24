"""Inbound Telegram bot — give Jarvis tasks (text or voice notes) from
Telegram, not just the microphone, and get the reply back as a message.

Separate from tools/telegram.py's send_telegram_message: that one sends AS
you, through your own MTProto user session, so you can message your other
contacts. This one runs your OWN bot (a token from @BotFather) that listens
for messages FROM you and routes them through the same tool loop the mic
uses — a voice note sent to the bot can run_shell / open_app / etc. exactly
like saying it out loud, and it can use a different "brain" (Gemini, or a
pluggable custom model via tools/custom_brain.py) without touching the Live
voice path at all.

Off by default until BOTH TELEGRAM_BOT_TOKEN is set AND
telegram_bot.allow_from lists at least one Telegram user id. An empty
allowlist must never be read as "allow everyone" — this bot can run shell
commands on the PC.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from . import genai_util
from .tools.custom_brain import configured as custom_model_configured
from .tools.custom_brain import run_custom_agent
from .tools.delegate import claude_executable, run_claude_with_escalation
from .tools.gemini_agent import run_gemini_agent
from .tools.telegram import credentials

# What the model is told about the context it's running in. Telegram renders
# Markdown and has no TTS budget to protect, so this is looser than the
# voice-surface delegate prompts (VOICE_CONTEXT / gemini_agent.AGENT_CONTEXT).
TELEGRAM_CONTEXT = (
    "You are Jarvis, reached here through a Telegram bot instead of the "
    "microphone. Do the work fully and autonomously; don't ask for "
    "permission. Reply in the language the user wrote in, with a clear, "
    "concise message describing what you did and the result. Telegram "
    "renders Markdown — keep formatting light and only where it helps."
)

MAX_STEPS = 20


def bot_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None


def allow_from(cfg) -> set[int]:
    """Telegram user ids allowed to command Jarvis, from config.yaml or .env.

    config.yaml is tracked by git, so putting a personal user id there means
    carrying a permanent local modification, a conflict on every pull, and the
    chance of committing the id to a public repo -- a mistake this project has
    already had to undo once. When telegram_bot.allow_from is left empty,
    TELEGRAM_ALLOWED_USER_IDS in .env is read instead (comma-separated), which
    is where every other per-machine secret here already lives and which
    .gitignore has always covered.

    Empty in both places still means the bot stays off. This adds a second
    place to say yes; it never becomes a way to default to "anyone".
    """
    ids = cfg.get("telegram_bot.allow_from", []) or []
    if not ids:
        ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    out = set()
    for raw in ids:
        try:
            out.add(int(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    return out


def session_path(cfg) -> str:
    """Where Telethon keeps the bot session, with its directory created.

    Same reason as the user-account session in tools/telegram.py: sqlite3 does
    not create missing parents and reports the failure only as "unable to open
    database file". The bot can be started on a fresh clone before anything
    else has written to the gitignored `data/`.
    """
    path = cfg.path("telegram_bot.session_path", "data/telegram_bot")
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def status_path(cfg) -> Path:
    return cfg.path("telegram_bot.status_path", "data/telegram_bot_status.json")


def read_status(cfg) -> dict[str, Any] | None:
    """Last known status, or None if the bot has never run on this machine."""
    path = status_path(cfg)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_status(cfg, **fields: Any) -> None:
    """Merge `fields` into the on-disk status so a viewer can tell — without
    tailing logs or having a terminal to look at — whether the bot is
    currently up, when it last saw a message, and what the last error was.
    """
    path = status_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data.update(fields)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_configured(cfg) -> bool:
    return bool(cfg.get("telegram_bot.enabled", True)) and bool(bot_token()) and bool(allow_from(cfg)) and credentials(cfg) is not None


def startup_warning(cfg) -> str | None:
    """Why the bot isn't starting, or None if it is (or is deliberately off)."""
    if not cfg.get("telegram_bot.enabled", True):
        return None
    if not bot_token():
        return None  # not set up at all — not worth warning about every run
    missing = []
    if not allow_from(cfg):
        missing.append("telegram_bot.allow_from (config.yaml)")
    if credentials(cfg) is None:
        missing.append("TELEGRAM_API_ID/TELEGRAM_API_HASH (.env)")
    if missing:
        return "Telegram bot ishga tushmadi — kerak: " + ", ".join(missing)
    return None


async def _run_task(jarvis, task: str, surface: str) -> str:
    cfg = jarvis.cfg
    backend = str(cfg.get("telegram_bot.brain", "gemini")).lower()
    max_steps = int(cfg.get("telegram_bot.max_steps", MAX_STEPS))

    if backend == "custom" and custom_model_configured(cfg):
        ok, text = await run_custom_agent(
            cfg, task, jarvis.log, context=TELEGRAM_CONTEXT, max_steps=max_steps, surface=surface
        )
    else:
        cwd = str(cfg.get("claude.default_cwd", "~"))
        ok, text = await run_gemini_agent(
            cfg,
            jarvis.client,
            jarvis.log,
            task,
            cwd,
            context=TELEGRAM_CONTEXT,
            max_steps=max_steps,
            surface=surface,
        )
        # Gemini agent failed outright (e.g. daily quota exhausted on every
        # configured model) — fall back to Claude Code rather than leaving
        # the Telegram bot silently unable to do anything, same as the
        # Gemini-then-Claude order the voice loop's delegate tools use.
        if not ok and cfg.get("claude.enabled", True) and claude_executable(cfg) is not None:
            jarvis.log.warn(f"Gemini agent xato qaytardi ({text[:200]!r}) — Claude Code bilan qayta urinildi.")
            workdir = Path(os.path.expandvars(cwd)).expanduser()
            if workdir.is_dir():
                ok, text = await run_claude_with_escalation(cfg, task, workdir, jarvis.log)
    return text if ok else f"Xato: {text}"


async def start_bot(jarvis) -> None:
    """Run the Telegram bot listener until cancelled. Meant to run as a
    background task alongside the wake-word voice loop (see app.run)."""
    from telethon import TelegramClient, events

    cfg = jarvis.cfg
    token = bot_token()
    allowed = allow_from(cfg)
    creds = credentials(cfg)
    if not token or not allowed or creds is None:
        return

    api_id, api_hash = creds
    client = TelegramClient(session_path(cfg), api_id, api_hash)

    @client.on(events.NewMessage(incoming=True))
    async def _on_message(event) -> None:  # noqa: ANN001 - Telethon event type
        if event.sender_id not in allowed:
            jarvis.log.warn(f"Telegram bot: ruxsatsiz foydalanuvchidan xabar ({event.sender_id}) — e'tiborsiz qoldirildi.")
            return

        if event.voice or event.audio:
            audio_bytes = await event.download_media(file=bytes)
            mime = (event.file.mime_type if event.file else None) or "audio/ogg"
            task = await genai_util.transcribe_audio(jarvis.client, cfg, audio_bytes, mime, jarvis.log)
            if not task.strip():
                await event.reply("Ovozli xabarni tushuna olmadim.")
                return
        else:
            task = (event.raw_text or "").strip()
            if not task:
                return

        jarvis.log.info(f"Telegram bot: {event.sender_id} dan vazifa qabul qilindi: {task[:200]!r}")
        _write_status(cfg, last_message_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), last_chat_id=event.sender_id)

        jarvis.memory.add_turn("telegram", "user", task)
        jarvis.audit.note("telegram", "message_in", chat_id=event.sender_id)
        reply = await _run_task(jarvis, task, surface="telegram")
        jarvis.memory.add_turn("telegram", "assistant", reply)
        jarvis.audit.note("telegram", "message_out", chat_id=event.sender_id)
        jarvis.log.info(f"Telegram bot: {event.sender_id} ga javob yuborildi: {reply[:200]!r}")
        await event.reply(reply[:4000] or "Vazifa bajarildi.")

    try:
        _write_status(cfg, status="starting", error=None)
        await client.start(bot_token=token)
        me = await client.get_me()
        started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _write_status(cfg, status="running", bot_username=me.username, started_at=started_at, error=None)
        jarvis.log.info(f"Telegram bot ishga tushdi: @{me.username} (allow_from: {sorted(allowed)}).")
        await client.run_until_disconnected()
        _write_status(cfg, status="stopped")
    except asyncio.CancelledError:
        _write_status(cfg, status="stopped")
        raise
    except Exception as exc:  # noqa: BLE001 - a Telegram-side crash must not kill the voice loop
        _write_status(cfg, status="error", error=str(exc))
        jarvis.log.warn(f"Telegram bot to'xtadi: {exc}")
    finally:
        await client.disconnect()
