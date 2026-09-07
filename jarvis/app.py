"""Wiring and the wake-word loop.

The daemon spends almost all its life in one cheap loop: read 80 ms of mic
audio, ask openWakeWord if it heard "hey jarvis", discard it. No network, no
API cost, ~1% of a core. Only when the wake word fires does a Gemini Live
socket open — which is also why you aren't billed for sitting in silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from google import genai

from . import winplat
from .audio import AudioIO, WakeWordDetector
from .audit import AuditLog
from .config import Config, load_config
from .console import Log
from .live import LiveSession
from .memory import Memory
from .reflect import Reflector
from .tools import registry
from .ui.bus import State, bus
from .voice import build_tts


class Jarvis:
    def __init__(self, cfg: Config, log: Log | None = None) -> None:
        self.cfg = cfg
        self.log = log or Log()

        self.memory = Memory(cfg.path("storage.db", "data/jarvis.db"))
        self.audit = AuditLog(cfg.path("storage.audit_log", "logs/audit.jsonl"))
        self.audio = AudioIO(cfg)
        self.client = genai.Client(api_key=cfg.gemini_key)
        self.tts = build_tts(cfg, self.audio)

        self.reflector = Reflector(self.client, cfg, self.memory, self.log)

        self.session = LiveSession(
            cfg=cfg,
            client=self.client,
            registry=registry,
            memory=self.memory,
            audio=self.audio,
            audit=self.audit,
            tts=self.tts,
            log=self.log,
        )

        # Shared services every tool handler can reach.
        registry.ctx.update(
            {
                "config": cfg,
                "memory": self.memory,
                "audit": self.audit,
                "genai_client": self.client,
                "audio": self.audio,
                "announce": self.announce,
                "confirm": self.confirm,
                "jarvis": self,
                "log": self.log,
            }
        )

        self._hide_unconfigured_tools()

        self._detector: WakeWordDetector | None = None

    def _hide_unconfigured_tools(self) -> None:
        """Don't advertise capabilities that aren't set up.

        Offering Gemini a tool that always errors is worse than not having it:
        it burns a turn, confuses the model, and the user hears an apology
        instead of an answer.
        """
        from pathlib import Path

        from .tools.custom_brain import configured as custom_model_configured
        from .tools.delegate import claude_executable
        from .tools.telegram import credentials as telegram_credentials
        from .tools.telegram import session_path as telegram_session_path

        if not self.cfg.get("claude.enabled", True):
            registry.disable("delegate_to_claude", "check_jobs")
        elif claude_executable(self.cfg) is None:
            registry.disable("delegate_to_claude", "check_jobs")
            self.log.warn(
                "Claude Code CLI topilmadi — murakkab vazifalarni topshirib "
                "bo'lmaydi. O'rnatish: npm install -g @anthropic-ai/claude-code"
            )

        if not custom_model_configured(self.cfg):
            registry.disable("delegate_to_custom_model")

        if telegram_credentials(self.cfg) is None:
            registry.disable("send_telegram_message")
        elif not Path(telegram_session_path(self.cfg) + ".session").is_file():
            registry.disable("send_telegram_message")
            self.log.warn(
                "Telegram seansi topilmadi — xabar yuborib bo'lmaydi. "
                "Ishga tushiring: python -m jarvis --telegram-login"
            )

    # --------------------------------------------------------------- hooks

    async def announce(self, message: str) -> None:
        """Surface something Jarvis wasn't asked about — a finished background job.

        Prefers speaking if a conversation is live; otherwise a Windows toast,
        so results never vanish silently.
        """
        self.memory.add_turn("system", "assistant", message)
        if await self.session.announce(message):
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(winplat.toast, "Jarvis", message[:200])
        self.log.info(f"(xabar) {message}")

    async def confirm(self, question: str) -> bool:
        """Guarded-mode approval. Unreachable at autonomy: full."""
        self.log.warn(f"{question}  [y/N]")
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(None, input, "> ")
        return answer.strip().lower() in {"y", "yes", "ha", "ha'a", "xa"}

    # --------------------------------------------------------------- run

    async def start(self) -> None:
        await self.audio.start()
        self.log.banner(self.cfg)
        self.audit.note("system", "jarvis_started", autonomy=self.cfg.get("agent.autonomy"))

    async def voice_loop(self) -> None:
        """Sleep on the wake word; wake into a full Live conversation."""
        wake_enabled = bool(self.cfg.get("wake.enabled", True)) and not os.getenv("JARVIS_NO_WAKE")
        if not wake_enabled:
            self.log.info("Uyg'otish so'zi o'chirilgan — to'g'ridan-to'g'ri suhbat.")
            bus.publish(State.LISTENING)
            while True:
                await self.session.converse()
                await asyncio.sleep(0.2)

        self._detector = WakeWordDetector(self.cfg)
        chime = bool(self.cfg.get("wake.chime", True))

        while True:
            self.log.listening()
            bus.publish(State.SLEEPING)

            while True:
                chunk = await self.audio.mic_queue.get()
                # Don't let Jarvis's own voice trigger the wake word.
                if self.audio.is_speaking:
                    continue
                if self._detector.feed(chunk):
                    break

            self.log.wake()
            bus.publish(State.LISTENING)
            if chime:
                self.audio.chime()
                await self.audio.wait_until_quiet(timeout=2.0)

            await self.session.converse()
            self._detector.reset()

            # Mine the finished conversation for durable facts, in the
            # background — the wake loop must be listening again immediately.
            self.reflector.schedule(self.session.session_started_at, force=True)

    async def shutdown(self) -> None:
        self.audit.note("system", "jarvis_stopped")
        with contextlib.suppress(Exception):
            await self.tts.aclose()
        self.audio.stop()
        self.memory.close()


async def run(config_path=None) -> None:
    cfg = load_config(config_path)
    jarvis = Jarvis(cfg)
    try:
        await jarvis.start()
        await jarvis.voice_loop()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await jarvis.shutdown()
