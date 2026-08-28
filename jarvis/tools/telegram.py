"""Telegram messaging — send messages to your own contacts by voice.

Uses Telethon (the MTProto *user* API) rather than the Bot API on purpose: a
bot can only message a chat that has already started a conversation with it,
which is useless for "message Aziz on Telegram" — this needs to act as the
user, able to reach anyone already in their contacts or chat list, same as
opening the app and typing would.

One-time setup, done once outside the voice loop (this module never prompts
for anything interactively itself):
  1. Get a free api_id/api_hash from https://my.telegram.org/apps
  2. Put them in .env as TELEGRAM_API_ID / TELEGRAM_API_HASH
  3. Run: python -m jarvis --telegram-login
     (asks for your phone number and the code Telegram texts you, once)

After that the session is saved to disk (telegram.session_path in
config.yaml) and Jarvis reconnects silently on every future run.
"""

from __future__ import annotations

import asyncio
import os

from .base import registry

# One client for the process, connected lazily on first use and kept open —
# reconnecting per call would add a ~1s handshake to every message.
_client = None
_client_lock = asyncio.Lock()


def session_path(cfg) -> str:
    return str(cfg.path("telegram.session_path", "data/telegram"))


def credentials(cfg) -> tuple[int, str] | None:
    """(api_id, api_hash) from the environment, or None if not configured."""
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        return None
    try:
        return int(api_id), api_hash
    except ValueError:
        return None


async def _get_client(cfg):
    """Return a connected, authorized TelegramClient, or raise a clear error."""
    global _client
    from telethon import TelegramClient

    creds = credentials(cfg)
    if creds is None:
        raise RuntimeError(
            "Telegram sozlanmagan: .env faylida TELEGRAM_API_ID va "
            "TELEGRAM_API_HASH yo'q. Bepul olish: https://my.telegram.org/apps"
        )

    async with _client_lock:
        if _client is None:
            api_id, api_hash = creds
            _client = TelegramClient(session_path(cfg), api_id, api_hash)
        if not _client.is_connected():
            await _client.connect()
        if not await _client.is_user_authorized():
            raise RuntimeError(
                "Telegram seansi hali tasdiqlanmagan. Terminalda bir marta "
                "ishga tushiring: python -m jarvis --telegram-login"
            )
    return _client


async def _find_dialog(client, name: str):
    """Fuzzy-match a chat by display name, case-insensitive.

    An exact match wins; otherwise the first dialog whose name contains the
    query, so "Ali" can hit "Aliyor aka" without the user saying the full
    name — the same matching style open_app/close_app use for Windows apps.
    """
    needle = name.strip().lower()
    best_substr = None
    async for dialog in client.iter_dialogs():
        dname = (dialog.name or "").strip()
        if not dname:
            continue
        low = dname.lower()
        if low == needle:
            return dialog
        if best_substr is None and needle in low:
            best_substr = dialog
    return best_substr


@registry.tool(
    name="send_telegram_message",
    description=(
        "Send a Telegram message to one of the user's contacts or chats by "
        "name, using the user's own Telegram account. Use whenever the user "
        "asks to message, write to, or tell someone something 'on Telegram' "
        "/ 'telegram orqali'. Do NOT use open_app for this — it only opens "
        "the app window, it cannot find a contact or send anything."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "contact": {
                "type": "STRING",
                "description": "Contact or chat name as it appears in Telegram, e.g. 'Aziz', 'Oila guruhi'.",
            },
            "message": {"type": "STRING", "description": "The message text to send."},
        },
        "required": ["contact", "message"],
    },
)
async def send_telegram_message(contact: str, message: str, ctx: dict) -> dict:
    cfg = ctx["config"]
    try:
        client = await _get_client(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    try:
        dialog = await _find_dialog(client, contact)
        if dialog is None:
            return {"ok": False, "error": f"'{contact}' nomli kontakt yoki chat topilmadi."}
        await client.send_message(dialog.entity, message)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Xabar yuborilmadi: {exc}"}

    return {"ok": True, "sent_to": dialog.name, "result": f"{dialog.name} ga xabar yuborildi."}
