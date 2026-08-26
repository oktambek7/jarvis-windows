"""Every Windows-specific quirk, in one place.

The rest of the codebase stays platform-neutral by importing from here. This
module is deliberately importable on non-Windows machines (it degrades to
sensible no-ops) so the test suite and CI can run anywhere.

Three things in here are load-bearing and worth reading before you change them:

1. POWERSHELL IS INVOKED VIA -EncodedCommand. Windows has no argv — every
   process receives one flat command-line string and parses it itself. Passing
   a user-dictated script through that is a quoting minefield: a stray quote in
   a voice-transcribed command silently changes what runs. Base64 UTF-16LE
   sidesteps the parser completely.

2. .cmd SHIMS CANNOT BE EXECUTED DIRECTLY. npm installs `claude` as claude.cmd,
   and CreateProcess refuses batch files. They must go through cmd /c.

3. CONSOLE ENCODING. Uzbek Latin uses ' and ' constantly ("o'zbek", "g'alaba").
   Windows consoles still default to cp1252, which cannot represent them, and
   Rich raises UnicodeEncodeError mid-render.
"""

from __future__ import annotations

import base64
import ctypes
import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------- console

def setup_console() -> None:
    """Force UTF-8 on stdout/stderr so Uzbek text does not crash Rich.

    Must run before any output is written. Safe to call more than once.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if IS_WINDOWS:
        try:
            # Also set the console code page, so anything that bypasses Python's
            # own encoding (a subprocess writing to our console) stays readable.
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001 - cosmetic only, never fatal
            pass


# --------------------------------------------------------------- powershell

def find_powershell() -> str | None:
    """PowerShell 7 if it is installed, else the built-in Windows PowerShell.

    pwsh is preferred: better UTF-8 behaviour and saner error handling. But it
    is not installed by default on any Windows version, so powershell.exe is
    the guaranteed fallback and works fine for everything Jarvis does.
    """
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found

    # shutil.which misses it if PATH is unusual; the location is fixed.
    if IS_WINDOWS:
        fallback = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        fallback = fallback / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if fallback.is_file():
            return str(fallback)
    return None


def ps_quote(value: str) -> str:
    """Quote a Python string as a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def wrap_script(body: str) -> str:
    """Wrap a user script so exit codes and encoding behave predictably.

    PowerShell does NOT propagate a native command's exit code by default: run
    a failing exe via -Command and PowerShell still exits 0, so every tool call
    would report success. The trailing exit logic fixes that, and the try/catch
    turns PowerShell's own terminating errors into exit 1 with the message on
    stderr instead of a silent nothing.
    """
    return (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$ErrorActionPreference = 'Continue'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "try {\n"
        f"{body}\n"
        "  if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }\n"
        "  exit 0\n"
        "} catch {\n"
        "  [Console]::Error.WriteLine($_.Exception.Message)\n"
        "  exit 1\n"
        "}\n"
    )


def encode_command(script: str) -> str:
    """Base64 UTF-16LE, the format -EncodedCommand expects."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def powershell_argv(script: str, wrap: bool = True) -> list[str]:
    """Full argv to run `script` under PowerShell, or raise if none is found."""
    shell = find_powershell()
    if shell is None:
        raise RuntimeError(
            "No PowerShell found. Windows ships powershell.exe by default — if "
            "this fails, your PATH or SystemRoot is unusual. Install PowerShell 7 "
            "from https://aka.ms/powershell to fix it."
        )
    return [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encode_command(wrap_script(script) if wrap else script),
    ]


def execution_policy() -> str:
    """Current effective execution policy, for --doctor. Best effort."""
    if not IS_WINDOWS:
        return "n/a"
    import subprocess

    try:
        argv = powershell_argv("Get-ExecutionPolicy", wrap=False)
        out = subprocess.run(argv, capture_output=True, timeout=20, check=False)
        return out.stdout.decode("utf-8", "replace").strip() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc})"


# --------------------------------------------------------------- executables

def resolve_executable(name: str) -> str | None:
    """Absolute path to an executable, including .cmd/.bat shims."""
    return shutil.which(name)


def launch_argv(executable: str, args: list[str]) -> list[str]:
    """Build argv that actually launches `executable`, batch shims included.

    npm global installs on Windows are .cmd wrappers, and CreateProcess — which
    is what asyncio's subprocess_exec ends up calling — cannot run a batch file.
    It raises a bare "[WinError 193] %1 is not a valid Win32 application", which
    is a genuinely unhelpful way to learn that Claude Code is installed fine.
    """
    if IS_WINDOWS and executable.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        return [comspec, "/c", executable, *args]
    return [executable, *args]


def is_admin() -> bool:
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------- notifications

def toast(title: str, message: str) -> bool:
    """Show a Windows toast. Returns False if it could not be displayed.

    winotify is pure Python and drives the real Windows notification system, so
    toasts land in Action Center like any other app's.
    """
    if not IS_WINDOWS:
        return False
    try:
        from winotify import Notification

        note = Notification(
            app_id="Jarvis",
            title=title[:64],
            msg=message[:200],
        )
        note.show()
        return True
    except Exception:  # noqa: BLE001 - a failed notification must never raise
        return False
