"""Entry point.

  jarvis                      start the voice daemon ("Hey Jarvis")
  jarvis --text "..."         one-shot text request, no microphone needed
  jarvis --doctor             check the setup before you trust it with your PC
  jarvis --devices            list audio devices, to pin one in config.yaml
  jarvis --no-wake            skip the wake word; talk immediately
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

from . import winplat

# Before anything prints. Uzbek Latin ("o'zbek", "g'alaba") is unrepresentable
# in the cp1252 code page Windows consoles still default to, and Rich raises
# UnicodeEncodeError halfway through rendering a panel.
winplat.setup_console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis", description="Uzbek voice agent for Windows")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--text", type=str, default=None, help="Run one request as text and exit")
    parser.add_argument("--doctor", action="store_true", help="Check the setup and exit")
    parser.add_argument("--devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--no-wake", action="store_true", help="Skip the wake word; talk immediately")
    parser.add_argument(
        "--telegram-login",
        action="store_true",
        help="One-time interactive Telegram login (phone + code), then exit",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------- doctor

async def _doctor(config_path: Path | None) -> int:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="Jarvis — tekshiruv", show_lines=False)
    table.add_column("Tekshiruv")
    table.add_column("Holat")
    table.add_column("Izoh", overflow="fold")

    ok = True

    def row(name: str, passed: bool, note: str = "", fatal: bool = True) -> None:
        """Render one check.

        Optional checks that fail are shown in yellow as "ixtiyoriy" rather than
        red "XATO" — a wall of red for things Jarvis does not actually need
        (ffmpeg when you are not using Aisha) trains you to ignore the real
        failures directly above them.
        """
        nonlocal ok
        if passed:
            status = "[green]OK[/green]"
        elif fatal:
            ok = False
            status = "[red]XATO[/red]"
        else:
            status = "[yellow]ixtiyoriy[/yellow]"
        table.add_row(name, status, note)

    # -- platform --
    row(
        "Windows",
        winplat.IS_WINDOWS,
        f"{sys.platform} — bu qurilma Windows emas" if not winplat.IS_WINDOWS else "",
        fatal=False,
    )

    # -- config + key --
    try:
        from .config import load_config

        cfg = load_config(config_path)
        row("config.yaml", True, f"autonomy = {cfg.get('agent.autonomy')}")
    except Exception as exc:  # noqa: BLE001
        row("config.yaml", False, str(exc))
        console.print(table)
        return 1

    try:
        key = cfg.gemini_key
        row("GEMINI_API_KEY", True, f"...{key[-4:]}")
    except Exception as exc:  # noqa: BLE001
        row("GEMINI_API_KEY", False, str(exc).splitlines()[0])

    # -- powershell: everything the tools do goes through this --
    shell = winplat.find_powershell()
    row(
        "PowerShell",
        bool(shell),
        f"{shell}  (policy: {winplat.execution_policy()})" if shell
        else "topilmadi — https://aka.ms/powershell",
    )

    # -- microphone --
    try:
        import sounddevice as sd

        default_in = sd.query_devices(kind="input")
        row("Mikrofon", True, str(default_in["name"]))
    except Exception as exc:  # noqa: BLE001
        row(
            "Mikrofon",
            False,
            f"{exc} — Settings > Privacy & security > Microphone dan ruxsat bering",
        )

    # -- wake word --
    try:
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        wake_model = str(cfg.get("wake.model", "hey_jarvis"))
        download_models([wake_model])  # no-op once cached
        Model(wakeword_models=[wake_model], inference_framework="onnx")
        row("Uyg'otish so'zi", True, wake_model)
    except Exception as exc:  # noqa: BLE001
        row("Uyg'otish so'zi", False, str(exc)[:160])

    # -- screen capture --
    try:
        import mss

        with mss.MSS() as sct:
            count = max(len(sct.monitors) - 1, 0)
        row("Ekranni ko'rish", count > 0, f"{count} ta monitor")
    except Exception as exc:  # noqa: BLE001
        row("Ekranni ko'rish", False, str(exc)[:160], fatal=False)

    # -- clipboard --
    try:
        import pyperclip

        pyperclip.paste()
        row("Bufer (clipboard)", True, "", fatal=False)
    except Exception as exc:  # noqa: BLE001
        row("Bufer (clipboard)", False, str(exc)[:120], fatal=False)

    # -- toast notifications --
    try:
        import winotify  # noqa: F401

        row("Bildirishnomalar", True, "winotify", fatal=False)
    except ImportError:
        row("Bildirishnomalar", False, "pip install winotify", fatal=False)

    # -- HUD overlay: optional, degrades to plain console if missing --
    from .ui.runtime import overlay_available

    wants_overlay = bool(cfg.get("ui.overlay.enabled", True))
    row(
        "HUD overlay",
        overlay_available(),
        "" if overlay_available()
        else ("pip install -e .  (PySide6 + qasync yo'q)" if wants_overlay
              else "ui.overlay.enabled: false — o'chirilgan"),
        fatal=False,
    )

    # -- telegram: optional, send_telegram_message --
    from .tools.telegram import credentials as telegram_credentials
    from .tools.telegram import session_path as telegram_session_path

    tg_creds = telegram_credentials(cfg)
    if tg_creds is None:
        row("Telegram", False, "ixtiyoriy — TELEGRAM_API_ID/HASH .env da yo'q", fatal=False)
    elif not Path(telegram_session_path(cfg) + ".session").is_file():
        row("Telegram", False, "kalitlar bor, lekin kirish qilinmagan — python -m jarvis --telegram-login", fatal=False)
    else:
        row("Telegram", True, "seans topildi", fatal=False)

    # -- custom model: bring your own brain --
    from .tools.custom_brain import base_url as custom_model_base_url
    from .tools.custom_brain import configured as custom_model_configured

    if not cfg.get("custom_model.enabled", True):
        pass
    elif custom_model_configured(cfg):
        row("Custom model", True, custom_model_base_url(cfg), fatal=False)
    elif custom_model_base_url(cfg):
        row("Custom model", False, "ixtiyoriy — model nomi yoki API kaliti yo'q", fatal=False)
    else:
        row("Custom model", False, "ixtiyoriy — custom_model sozlanmagan", fatal=False)

    # -- claude cli: the hands --
    claude_bin = winplat.resolve_executable(str(cfg.get("claude.command", "claude")))
    row(
        "Claude Code CLI",
        bool(claude_bin),
        claude_bin or "topilmadi — npm install -g @anthropic-ai/claude-code",
        fatal=False,
    )

    # -- openclaw: the self-hosted second brain (docs/OPENCLAW.md) --
    from .tools.openclaw import container_name as openclaw_container_name
    from .tools.openclaw import container_running as openclaw_container_running
    from .tools.openclaw import docker_executable

    if docker_executable() is None:
        row("OpenClaw", False, "ixtiyoriy — Docker topilmadi", fatal=False)
    elif not openclaw_container_running(cfg):
        row(
            "OpenClaw",
            False,
            f"ixtiyoriy — konteyner ishlamayapti ({openclaw_container_name(cfg)})",
            fatal=False,
        )
    else:
        row("OpenClaw", True, openclaw_container_name(cfg), fatal=False)

    # -- ffmpeg (only needed for the Aisha Uzbek voice) --
    ffmpeg = shutil.which("ffmpeg")
    needs_ffmpeg = str(cfg.get("tts.backend", "gemini")).lower() == "aisha"
    row(
        "ffmpeg",
        bool(ffmpeg),
        ffmpeg or ("Aisha ovozi uchun SHART: winget install Gyan.FFmpeg"
                   if needs_ffmpeg else "faqat Aisha ovozi uchun kerak"),
        fatal=needs_ffmpeg,
    )

    if needs_ffmpeg:
        row(
            "AISHA_API_KEY",
            bool(cfg.aisha_key),
            "" if cfg.aisha_key else "tts.backend='aisha', lekin kalit .env da yo'q",
        )

    # -- admin: informational. Jarvis does NOT need it, and running the whole
    #    agent elevated would mean a misheard command runs elevated too.
    row(
        "Administrator",
        True,
        "ha — ehtiyot bo'ling, buyruqlar ham admin huquqi bilan bajariladi"
        if winplat.is_admin() else "yo'q (shunday bo'lgani yaxshi)",
        fatal=False,
    )

    # -- live api reachability --
    try:
        from google import genai

        client = genai.Client(api_key=cfg.gemini_key)
        models = await client.aio.models.list()
        count = 0
        async for _ in models:
            count += 1
            if count > 3:
                break
        row("Gemini API", True, "ulanish ishlayapti")
    except Exception as exc:  # noqa: BLE001
        row("Gemini API", False, str(exc)[:160])

    console.print(table)
    if ok:
        console.print("\n[green]Hammasi joyida. `python -m jarvis` deb ishga tushiring.[/green]")
    else:
        console.print("\n[red]Yuqoridagi XATO larni tuzating.[/red]")
    return 0 if ok else 1


# --------------------------------------------------------------------- telegram login

async def _telegram_login(config_path: Path | None) -> int:
    """One-time interactive login: phone number, then the code Telegram
    texts you. Telethon prompts for both with plain input() (and a 2FA
    password via getpass if you have one enabled) and saves the resulting
    session to disk, so this never has to run again outside of setup.
    """
    from rich.console import Console

    from .config import load_config
    from .tools.telegram import credentials, session_path

    console = Console()
    cfg = load_config(config_path)

    creds = credentials(cfg)
    if creds is None:
        console.print(
            "[red]TELEGRAM_API_ID / TELEGRAM_API_HASH .env faylida topilmadi.[/red]\n"
            "Bepul olish: https://my.telegram.org/apps"
        )
        return 1

    from telethon import TelegramClient

    api_id, api_hash = creds
    client = TelegramClient(session_path(cfg), api_id, api_hash)
    console.print("[cyan]Telefon raqamingiz va Telegram yuborgan kodni kiriting...[/cyan]")
    await client.start()  # prompts interactively for phone / code / 2FA password
    me = await client.get_me()
    console.print(f"[green]Kirish muvaffaqiyatli: {me.first_name} sifatida ulandingiz.[/green]")
    await client.disconnect()
    return 0


# --------------------------------------------------------------------- text mode

async def _one_shot(config_path: Path | None, text: str) -> int:
    """Run a single request through the tool loop. No mic, no Live socket.

    The fastest way to prove the tools and Claude delegation work before you
    start debugging audio on top of them.
    """
    from google.genai import types
    from rich.console import Console

    from .app import Jarvis
    from .config import load_config
    from .console import Log
    from .genai_util import agent_config, generate
    from .persona import build_system_prompt
    from .tools import registry

    console = Console()
    cfg = load_config(config_path)
    log = Log()
    jarvis = Jarvis(cfg, log)

    config = agent_config(cfg, build_system_prompt(jarvis.memory.context_block()))
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=text)])]
    jarvis.memory.add_turn("cli", "user", text)

    try:
        for _ in range(8):
            response = await generate(jarvis.client, cfg, contents, config, log=log)
            calls = response.function_calls or []
            if not calls:
                answer = (response.text or "").strip()
                console.print(f"\n[bold magenta]jarvis:[/bold magenta] {answer}\n")
                jarvis.memory.add_turn("cli", "assistant", answer)
                return 0

            contents.append(response.candidates[0].content)
            results = []
            for fc in calls:
                args = dict(fc.args or {})
                log.tool(fc.name, args)
                result = await registry.invoke(fc.name, args, surface="cli")
                results.append(types.Part.from_function_response(name=fc.name, response=result))
            contents.append(types.Content(role="user", parts=results))

        console.print("[yellow]Juda ko'p qadam — to'xtatildi.[/yellow]")
        return 1
    finally:
        jarvis.memory.close()


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0

    if args.doctor:
        return asyncio.run(_doctor(args.config))

    if args.telegram_login:
        return asyncio.run(_telegram_login(args.config))

    if args.text:
        return asyncio.run(_one_shot(args.config, args.text))

    if args.no_wake:
        os.environ["JARVIS_NO_WAKE"] = "1"

    try:
        _run_with_optional_overlay(args.config)
    except KeyboardInterrupt:
        print("\nXayr!")
    return 0


def _run_with_optional_overlay(config_path: Path | None) -> None:
    """Start the voice daemon, with the HUD overlay if it's enabled and
    installed. Falls back to the plain console daemon otherwise — a missing
    or misbehaving GUI stack must never stop Jarvis from listening.
    """
    from .app import run
    from .config import load_config

    cfg = load_config(config_path)
    if bool(cfg.get("ui.overlay.enabled", True)):
        from .ui.runtime import overlay_available, run_with_overlay

        if overlay_available():
            run_with_overlay(cfg, lambda: run(config_path))
            return
        print(
            "(HUD overlay uchun PySide6/qasync o'rnatilmagan — "
            "`pip install -e .` qiling. Hozircha konsolda davom etaman.)"
        )

    asyncio.run(run(config_path))


if __name__ == "__main__":
    sys.exit(main())
