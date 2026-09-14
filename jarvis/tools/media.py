"""Media control, Spotify, and quick web lookups — the tools that were
missing before, which is why Jarvis used to fall back to minutes of
SendKeys/UIAutomation guesswork to pause a song or search YouTube.

Media transport (play/pause/next/previous/volume) goes through the same
hardware media keys a keyboard sends, via a tiny P/Invoke call to
user32.dll's keybd_event. Windows routes those to whatever app owns the
active media session (SMTC) — Spotify, a YouTube tab in Chrome, anything —
so one tool works regardless of which app is actually playing, and it's
instant: no window to find, no UI to click.

Spotify search and YouTube/web search go through URI/URL deep links
(spotify:search:..., youtube.com/results?search_query=..., a Google search
URL) opened with the OS "open" verb — one call, no keystroke simulation.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

from .base import registry

# Virtual-key codes for Windows' multimedia keys (winuser.h).
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF

_ACTION_TO_VK = {
    "play_pause": _VK_MEDIA_PLAY_PAUSE,
    "next": _VK_MEDIA_NEXT_TRACK,
    "previous": _VK_MEDIA_PREV_TRACK,
    "volume_up": _VK_VOLUME_UP,
    "volume_down": _VK_VOLUME_DOWN,
    "mute": _VK_VOLUME_MUTE,
}

_MEDIA_KEY_SCRIPT = """
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, int dwFlags, int dwExtraInfo);' -Name Win32Media -Namespace Jarvis
$vk = {vk}
for ($i = 0; $i -lt {presses}; $i++) {{
  [Jarvis.Win32Media]::keybd_event($vk, 0, 0, 0)   # key down
  [Jarvis.Win32Media]::keybd_event($vk, 0, 2, 0)   # key up (KEYEVENTF_KEYUP)
  Start-Sleep -Milliseconds 60
}}
Write-Output "sent:$vk x {presses}"
"""


async def _powershell(script: str, timeout: float = 15) -> dict:
    from .. import winplat

    try:
        argv = winplat.powershell_argv(script)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "Media tugmasi yuborilmadi: vaqt tugadi."}

    if proc.returncode != 0:
        return {"ok": False, "error": err.decode("utf-8", "replace").strip()[:300]}
    return {"ok": True, "detail": out.decode("utf-8", "replace").strip()}


@registry.tool(
    name="media_control",
    description=(
        "Control whatever is currently playing audio on the PC — Spotify, a "
        "YouTube tab, any app — using the system media keys. Use for "
        "'to'xtat'/'pauza', 'davom ettir', 'keyingisi', 'oldingisi', "
        "'ovozni ko'tar/pasaytir', 'ovozni o'chir'. This is instant and does "
        "NOT need the app to be focused or even visible — prefer it over "
        "open_app or run_shell for anything about controlling playback."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["play_pause", "next", "previous", "volume_up", "volume_down", "mute"],
                "description": "Which transport control to send.",
            },
            "presses": {
                "type": "INTEGER",
                "description": (
                    "How many times to press it — mainly for volume_up/volume_down "
                    "when the user wants a bigger change (e.g. 5). Default 1."
                ),
            },
        },
        "required": ["action"],
    },
)
async def media_control(action: str, presses: int = 1) -> dict:
    vk = _ACTION_TO_VK.get(action)
    if vk is None:
        return {"ok": False, "error": f"Noma'lum amal: {action}"}
    presses = max(1, min(int(presses or 1), 20))
    script = _MEDIA_KEY_SCRIPT.format(vk=vk, presses=presses)
    result = await _powershell(script)
    if not result.get("ok"):
        return result
    return {"ok": True, "action": action, "presses": presses}


@registry.tool(
    name="play_spotify",
    description=(
        "Open Spotify directly to search results for a song, artist or "
        "album, so the user can hit play on the right track — one call, no "
        "window hunting. Use whenever the user asks to play music 'Spotify "
        "orqali' or just says 'X qo'shig'ini qo'y' with no app named. Call "
        "with no query to just bring Spotify to the front. After this, use "
        "media_control(play_pause) if the user then asks to actually start "
        "playback."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Song, artist or album to search for, e.g. 'Drake', 'Shape of You Ed Sheeran'.",
            }
        },
    },
)
async def play_spotify(query: str = "") -> dict:
    uri = f"spotify:search:{quote(query)}" if query.strip() else "spotify:"
    try:
        os.startfile(uri)  # type: ignore[attr-defined]  # Windows-only
        return {"ok": True, "opened": uri}
    except (AttributeError, OSError):
        # Spotify desktop app isn't installed / its URI scheme isn't
        # registered — fall back to the web player, which always works.
        web_url = (
            f"https://open.spotify.com/search/{quote(query)}"
            if query.strip()
            else "https://open.spotify.com"
        )
        try:
            os.startfile(web_url)  # type: ignore[attr-defined]
            return {"ok": True, "opened": web_url, "note": "Spotify ilovasi topilmadi — brauzerda ochildi."}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Spotify ochilmadi: {exc}"}


@registry.tool(
    name="play_youtube",
    description=(
        "Open YouTube search results for a song or video in the default "
        "browser — one call. Use for 'YouTube'dan X ni qo'y' or any 'video "
        "qo'y' request. Prefer this over open_app('Chrome') + typing into "
        "the address bar, which is slow and unreliable."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {"query": {"type": "STRING", "description": "What to search for."}},
        "required": ["query"],
    },
)
async def play_youtube(query: str) -> dict:
    url = f"https://www.youtube.com/results?search_query={quote(query)}"
    try:
        os.startfile(url)  # type: ignore[attr-defined]
        return {"ok": True, "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"YouTube ochilmadi: {exc}"}


@registry.tool(
    name="search_web",
    description=(
        "Open a Google search for a query in the default browser — one "
        "call. Use only when the user explicitly wants to SEE search "
        "results in a browser (e.g. 'shuni brauzerda qidir'); for answering "
        "a factual question out loud, use Google Search grounding instead "
        "and just speak the answer."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {"query": {"type": "STRING"}},
        "required": ["query"],
    },
)
async def search_web(query: str) -> dict:
    url = f"https://www.google.com/search?q={quote(query)}"
    try:
        os.startfile(url)  # type: ignore[attr-defined]
        return {"ok": True, "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Qidiruv ochilmadi: {exc}"}
