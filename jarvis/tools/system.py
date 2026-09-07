"""Windows control tools — the hands that touch the machine directly.

Anything fast and mechanical lives here. Anything that needs thinking across
many steps gets handed to Claude Code instead (see delegate.py).

A note on the port from the macOS original: that codebase had two shell-ish
tools, `run_shell` (zsh) and `applescript` (the escape hatch for driving native
apps that have no CLI). On Windows both of those collapse into PowerShell — it
is simultaneously the command line AND the automation layer, reaching COM,
WMI, the registry and the shell. Shipping two tools that invoke the same
interpreter would just make the model dither over which to pick, so there is
one `run_shell`, and its description carries the automation use cases the
AppleScript tool used to advertise.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from .. import winplat
from .base import looks_destructive, registry

# Voice replies should not read out a 40 KB log file. Tools truncate hard and
# tell the model they did, so it can offer to show the rest another way.
MAX_OUTPUT = 4000


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters truncated]"


def _resolve(path: str) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


async def _confirm_if_needed(ctx: dict[str, Any], description: str, risky: bool) -> str | None:
    """Return an error string if the action was refused, else None.

    At autonomy "full" this is a no-op. At "guarded" it routes through whatever
    confirmation callback the active surface installed.
    """
    cfg = ctx.get("config")
    if cfg is None or cfg.is_full_autonomy or not risky:
        return None
    confirm = ctx.get("confirm")
    if confirm is None:
        return None
    approved = await confirm(description)
    if not approved:
        return "Foydalanuvchi bu amalni rad etdi."  # user declined
    return None


async def _powershell(script: str, timeout: float = 60, cwd: Path | None = None) -> dict:
    """Run a PowerShell script and return exit code plus captured output."""
    try:
        argv = winplat.powershell_argv(script)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": f"Buyruq {timeout} soniyada tugamadi."}

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": _clip(out.decode("utf-8", "replace").strip()),
        "stderr": _clip(err.decode("utf-8", "replace").strip(), 1500),
    }


# ----------------------------------------------------------------- shell

@registry.tool(
    name="run_shell",
    description=(
        "Run a PowerShell command on the user's Windows PC and return its output. "
        "This is the main way to DO things. Use it for anything scriptable: "
        "checking processes and services, git, disk space, searching files, "
        "network checks, winget, launching CLIs, reading the registry, WMI/CIM "
        "queries, volume and media control, window management via COM. "
        "Prefer this over guessing. Returns stdout, stderr and exit code."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "command": {"type": "STRING", "description": "The PowerShell command or script to execute."},
            "cwd": {"type": "STRING", "description": "Working directory. Defaults to the user's home."},
            "timeout": {"type": "NUMBER", "description": "Seconds before giving up. Default 60."},
        },
        "required": ["command"],
    },
)
async def run_shell(command: str, ctx: dict, cwd: str | None = None, timeout: float = 60) -> dict:
    refusal = await _confirm_if_needed(
        ctx, f"Buyruqni bajaraymi: {command}", looks_destructive(command)
    )
    if refusal:
        return {"ok": False, "error": refusal}

    workdir = _resolve(cwd) if cwd else Path.home()
    if not workdir.is_dir():
        return {"ok": False, "error": f"Directory does not exist: {workdir}"}

    return await _powershell(command, timeout=timeout, cwd=workdir)


# ----------------------------------------------------------------- files

@registry.tool(
    name="read_file",
    description="Read a text file from disk and return its contents.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "path": {"type": "STRING", "description": "Absolute or ~-relative file path."},
        },
        "required": ["path"],
    },
)
async def read_file(path: str) -> dict:
    p = _resolve(path)
    if not p.is_file():
        return {"ok": False, "error": f"File not found: {p}"}
    try:
        # Off the event loop: it is also pumping mic frames to the Live socket,
        # and a synchronous read of a big file stalls them into a stutter.
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not read {p}: {exc}"}
    return {"ok": True, "path": str(p), "bytes": p.stat().st_size, "content": _clip(content, 8000)}


@registry.tool(
    name="write_file",
    description=(
        "Write or append text to a file, creating parent directories as needed. "
        "Use for notes, configs, quick scripts. For editing existing code prefer "
        "delegate_to_claude, which can read context before changing anything."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "path": {"type": "STRING"},
            "content": {"type": "STRING"},
            "append": {"type": "BOOLEAN", "description": "Append instead of overwrite. Default false."},
        },
        "required": ["path", "content"],
    },
)
async def write_file(path: str, content: str, ctx: dict, append: bool = False) -> dict:
    p = _resolve(path)
    overwriting = p.exists() and not append
    refusal = await _confirm_if_needed(ctx, f"{p} faylini qayta yozaymi?", overwriting)
    if refusal:
        return {"ok": False, "error": refusal}

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the model's \n from silently becoming \r\n, which
        # matters when it is writing a script or a config file.
        with open(p, "a" if append else "w", encoding="utf-8", newline="") as fh:
            fh.write(content)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not write {p}: {exc}"}
    return {"ok": True, "path": str(p), "bytes_written": len(content.encode()), "appended": append}


@registry.tool(
    name="list_dir",
    description="List the contents of a directory with file sizes.",
    parameters={
        "type": "OBJECT",
        "properties": {"path": {"type": "STRING"}},
        "required": ["path"],
    },
)
async def list_dir(path: str) -> dict:
    p = _resolve(path)
    if not p.is_dir():
        return {"ok": False, "error": f"Not a directory: {p}"}
    entries = []
    for child in sorted(p.iterdir())[:300]:
        try:
            size = child.stat().st_size if child.is_file() else None
        except OSError:
            size = None
        entries.append({"name": child.name, "dir": child.is_dir(), "size": size})
    return {"ok": True, "path": str(p), "count": len(entries), "entries": entries}


# ----------------------------------------------------------------- apps & UI

# Resolution order matters. Start-Process handles anything on PATH or with a
# registered App Path (notepad, calc, code, chrome). Get-StartApps is the
# important second pass: it is the only way to reach Microsoft Store apps,
# whose real identity is an AppUserModelID, not an .exe anywhere on disk.
_OPEN_APP_SCRIPT = """
  $name = {name}
  try {{
    Start-Process -FilePath $name -ErrorAction Stop
    Write-Output "started:$name"
    exit 0
  }} catch {{ }}

  $apps = @(Get-StartApps -ErrorAction SilentlyContinue)
  $hit = $apps | Where-Object {{ $_.Name -ieq $name }} | Select-Object -First 1
  if (-not $hit) {{
    $hit = $apps | Where-Object {{ $_.Name -ilike "*$name*" }} | Select-Object -First 1
  }}
  if ($hit) {{
    Start-Process ("shell:AppsFolder\\" + $hit.AppID)
    Write-Output ("started:" + $hit.Name)
    exit 0
  }}
  [Console]::Error.WriteLine("No application matching '$name' was found.")
  exit 3
"""


@registry.tool(
    name="open_app",
    description=(
        "Launch or focus a Windows application by name, e.g. 'Notepad', "
        "'Calculator', 'Chrome', 'Visual Studio Code', 'Spotify', 'Telegram', "
        "'Explorer'. Works for Microsoft Store apps too. This ONLY brings the "
        "app window to the foreground — it cannot find a contact, type, click, "
        "or send anything inside it. Do NOT use this to message someone on "
        "Telegram/WhatsApp/etc — use send_telegram_message (or the matching "
        "dedicated tool) for that instead."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {"name": {"type": "STRING"}},
        "required": ["name"],
    },
)
async def open_app(name: str) -> dict:
    script = _OPEN_APP_SCRIPT.format(name=winplat.ps_quote(name))
    result = await _powershell(script, timeout=30)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("stderr") or result.get("error") or f"Could not open {name}",
        }
    return {"ok": True, "opened": name, "detail": result.get("stdout", "")}


# Matches on window title OR process name, case-insensitively, because a Store
# app's process name routinely differs from what a person calls it — e.g.
# Calculator shows up as CalculatorApp, WhatsApp as WhatsApp.exe under a
# WhatsAppDesktop-prefixed AppX process, etc. open_app resolves the same way.
_CLOSE_APP_SCRIPT = """
  $name = {name}
  $procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {{
    $_.MainWindowTitle -ieq $name -or $_.ProcessName -ieq $name -or
    $_.MainWindowTitle -ilike "*$name*" -or $_.ProcessName -ilike "*$name*"
  }})
  if (-not $procs) {{
    [Console]::Error.WriteLine("No running process matching '$name' was found.")
    exit 3
  }}
  $names = ($procs | Select-Object -ExpandProperty ProcessName -Unique) -join ", "
  $procs | Stop-Process -Force -ErrorAction Stop
  Write-Output "closed:$names"
"""


@registry.tool(
    name="close_app",
    description=(
        "Close a running Windows application by name, e.g. 'Calculator', "
        "'Notepad', 'Telegram', 'Chrome'. Matches by window title or process "
        "name, so it works even when the process name differs from the "
        "app's display name."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {"name": {"type": "STRING"}},
        "required": ["name"],
    },
)
async def close_app(name: str, ctx: dict) -> dict:
    refusal = await _confirm_if_needed(ctx, f"{name} ilovasini yopaymi?", True)
    if refusal:
        return {"ok": False, "error": refusal}

    script = _CLOSE_APP_SCRIPT.format(name=winplat.ps_quote(name))
    result = await _powershell(script, timeout=20)
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("stderr") or result.get("error") or f"Could not close {name}",
        }
    return {"ok": True, "closed": name, "detail": result.get("stdout", "")}


@registry.tool(
    name="open_url",
    description="Open a URL in the default browser.",
    parameters={
        "type": "OBJECT",
        "properties": {"url": {"type": "STRING"}},
        "required": ["url"],
    },
)
async def open_url(url: str) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        # startfile is the native shell "open" verb; no subprocess, no quoting.
        os.startfile(url)  # type: ignore[attr-defined]  # Windows-only
        return {"ok": True, "url": url}
    except AttributeError:
        # Non-Windows (tests, CI). Fall through to the shell.
        result = await _powershell(f"Start-Process {winplat.ps_quote(url)}", timeout=20)
        return {"ok": result.get("ok", False), "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not open {url}: {exc}"}


@registry.tool(
    name="notify",
    description=(
        "Show a Windows notification toast. Use to surface a result on screen "
        "instead of reading a long list out loud."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {"title": {"type": "STRING"}, "message": {"type": "STRING"}},
        "required": ["title", "message"],
    },
)
async def notify(title: str, message: str) -> dict:
    shown = await asyncio.to_thread(winplat.toast, title, message)
    if not shown:
        return {"ok": False, "error": "Bildirishnoma ko'rsatilmadi."}
    return {"ok": True}


# ----------------------------------------------------------------- clipboard

@registry.tool(
    name="clipboard_read",
    description="Read the current contents of the Windows clipboard.",
    parameters={"type": "OBJECT", "properties": {}},
)
async def clipboard_read() -> dict:
    try:
        import pyperclip

        content = await asyncio.to_thread(pyperclip.paste)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Clipboard unavailable: {exc}"}
    return {"ok": True, "content": _clip(content or "")}


@registry.tool(
    name="clipboard_write",
    description="Put text on the Windows clipboard so the user can paste it.",
    parameters={
        "type": "OBJECT",
        "properties": {"text": {"type": "STRING"}},
        "required": ["text"],
    },
)
async def clipboard_write(text: str) -> dict:
    try:
        import pyperclip

        await asyncio.to_thread(pyperclip.copy, text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Clipboard unavailable: {exc}"}
    return {"ok": True, "copied_chars": len(text)}


# ----------------------------------------------------------------- vision

def _grab_screen(target: Path) -> str | None:
    """Capture the primary monitor to a PNG. Returns an error string or None."""
    try:
        import mss
        import mss.tools
    except ImportError:
        return "mss is not installed — run: pip install mss"

    try:
        with mss.MSS() as sct:
            # monitors[0] is the union of every display; [1] is the primary.
            # Grabbing the union on a multi-monitor desk produces a very wide,
            # mostly-empty image that the vision model reads poorly.
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=str(target))
    except Exception as exc:  # noqa: BLE001
        return f"Screen capture failed: {exc}"
    return None


@registry.tool(
    name="see_screen",
    description=(
        "Take a screenshot of the user's screen and answer a question about it. "
        "Use whenever the user refers to what they are looking at: 'what's this "
        "error', 'read this to me', 'what's on my screen', 'ekranimda nima bor'."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "question": {
                "type": "STRING",
                "description": "What to determine from the screen. Be specific.",
            }
        },
        "required": ["question"],
    },
)
async def see_screen(question: str, ctx: dict) -> dict:
    """Screenshot -> Gemini vision -> text description.

    Deliberately a separate non-Live vision call: the Live session is a realtime
    audio pipe, and pushing full screenshots through it burns the context window
    fast. A one-shot flash call is cheaper and answers just what was asked.
    """
    client = ctx.get("genai_client")
    if client is None:
        return {"ok": False, "error": "Gemini client unavailable."}

    shot = Path(tempfile.gettempdir()) / "jarvis_screen.png"
    error = await asyncio.to_thread(_grab_screen, shot)
    if error or not shot.exists():
        return {"ok": False, "error": error or "Screenshot produced no file."}

    from google.genai import types as gt

    from ..genai_util import generate

    cfg = ctx["config"]
    try:
        data = shot.read_bytes()
        resp = await generate(
            client,
            cfg,
            contents=[
                gt.Part.from_bytes(data=data, mime_type="image/png"),
                gt.Part.from_text(
                    text=(
                        f"Foydalanuvchining ekrani. Savolga aniq va qisqa javob ber "
                        f"(o'zbek tilida): {question}"
                    )
                ),
            ],
        )
        return {"ok": True, "answer": _clip(resp.text or "(bo'sh javob)")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Vision call failed: {exc}"}
    finally:
        shot.unlink(missing_ok=True)
