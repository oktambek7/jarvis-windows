"""Terminal output. Jarvis is a voice agent, but you still want to watch it think."""

from __future__ import annotations

import time

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

_c = Console()


class Log:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self._session_started = 0.0

    def banner(self, cfg) -> None:
        autonomy = str(cfg.get("agent.autonomy", "guarded"))
        body = Text()
        body.append("Uyg'otish so'zi  ", style="dim")
        body.append('"Hey Jarvis"\n', style="bold cyan")
        body.append("Til              ", style="dim")
        body.append("O'zbekcha (Gemini Live avtomatik aniqlaydi)\n")
        body.append("Model            ", style="dim")
        body.append(f"{cfg.get('gemini.model')}\n")
        body.append("Ovoz             ", style="dim")
        body.append(f"{cfg.get('tts.backend')} / {cfg.get('gemini.voice')}\n")
        body.append("Qo'llar          ", style="dim")
        hands = "Gemini agent (asosiy)"
        if cfg.get("claude.enabled", True):
            hands += f" + Claude Code ({cfg.get('claude.model')}, zaxira)"
        body.append(f"{hands}\n")
        body.append("Avtonomiya       ", style="dim")
        body.append(
            autonomy,
            style="bold red" if autonomy == "full" else "bold yellow",
        )
        if autonomy == "full":
            body.append("  — hamma amal so'roqsiz bajariladi", style="dim red")

        _c.print(Panel(body, title="[bold]JARVIS[/bold]", border_style="cyan", expand=False))

    def listening(self) -> None:
        _c.print("[dim]💤 Uxlayapman — “Hey Jarvis” deb chaqiring…[/dim]")

    def wake(self) -> None:
        _c.print("\n[bold green]🎙  Eshitaman[/bold green]")

    def session_open(self) -> None:
        self._session_started = time.monotonic()

    def session_close(self) -> None:
        if self._session_started:
            secs = time.monotonic() - self._session_started
            _c.print(f"[dim]— suhbat tugadi ({secs:.0f}s)[/dim]\n")
        self._session_started = 0.0

    def user_partial(self, text: str) -> None:
        if not self.quiet:
            _c.print(f"[cyan]siz:[/cyan] {text}", highlight=False)

    def assistant(self, text: str) -> None:
        _c.print(f"[bold magenta]jarvis:[/bold magenta] {text}", highlight=False)

    def tool(self, name: str, args: dict) -> None:
        preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3])
        _c.print(f"[yellow]⚙  {name}[/yellow][dim]({preview})[/dim]", highlight=False)

    def interrupted(self) -> None:
        _c.print("[dim]⏸  to'xtatildi[/dim]")

    def info(self, msg: str) -> None:
        _c.print(f"[dim]{msg}[/dim]")

    def warn(self, msg: str) -> None:
        _c.print(f"[yellow]⚠  {msg}[/yellow]")

    def error(self, msg: str) -> None:
        _c.print(f"[red]✖  {msg}[/red]")
