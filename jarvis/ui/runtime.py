"""Runs the HUD overlay's Qt loop and Jarvis's asyncio loop on separate
threads, bridged only by the one-way Qt signal in `overlay.Overlay` that
carries state updates from `jarvis.ui.bus`. Only touched when
`ui.overlay.enabled` is true; everything else in the codebase runs exactly
as it did before this package existed.

This used to merge the two loops onto one thread via qasync.QEventLoop, which
hit an unresolved upstream qasync/Windows reentrancy bug: "Cannot enter into
task X while another task Y is being executed", whenever Qt's own event
processing and asyncio's task stepping interleaved on the same call stack
(typically Qt draining its event queue mid-await, re-entering a task that
was already mid-step). Two independent native loops, on two threads that
never call into each other's dispatch loop, have no shared call stack for
that bug to occur on.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
from collections.abc import Awaitable, Callable


def overlay_available() -> bool:
    """True if the optional GUI stack is actually installed."""
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


def run_with_overlay(cfg, coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Run `coro_factory()` to completion on its own asyncio loop/thread,
    with the HUD overlay shown on the main thread for the duration.

    Raises the same way `asyncio.run(coro_factory())` would — callers that
    already catch KeyboardInterrupt around that call don't need to change.
    """
    from PySide6.QtCore import QMetaObject, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from .overlay import Overlay

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Qt's own loop swallows Ctrl+C on Windows unless the interpreter gets a
    # chance to run periodically — this timer's only job is to wake it up so
    # the SIGINT handler below actually gets serviced.
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)

    interrupted = False

    def on_sigint(*_args) -> None:
        nonlocal interrupted
        interrupted = True
        app.quit()

    previous_handler = signal.signal(signal.SIGINT, on_sigint)

    overlay = Overlay(cfg)
    overlay.show()

    loop = asyncio.new_event_loop()
    outcome: dict[str, BaseException | None] = {"exc": None}

    def worker() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro_factory())
        except BaseException as exc:  # noqa: BLE001
            outcome["exc"] = exc
        finally:
            loop.close()
            # app.quit() is a plain method call, not a signal/slot — calling
            # it directly from this (non-GUI) thread is not safe by Qt's own
            # rules. invokeMethod's whole job is exactly this: post the call
            # onto app's own thread (the GUI one) instead of running it here.
            QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)

    thread = threading.Thread(target=worker, name="jarvis-asyncio", daemon=True)
    thread.start()

    try:
        app.exec()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        if thread.is_alive():
            # Ctrl+C, or anything else that ended the Qt loop while the
            # asyncio side was still running — ask it to unwind instead of
            # leaving the worker thread (and thread.join below) hanging.
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # already closed by worker()'s own finally
        thread.join(timeout=5.0)

    if interrupted:
        raise KeyboardInterrupt
    if outcome["exc"] is not None:
        raise outcome["exc"]
