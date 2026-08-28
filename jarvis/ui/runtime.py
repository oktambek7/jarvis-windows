"""Merges the Qt event loop into asyncio so the HUD and the voice loop can
share one thread. Only touched when `ui.overlay.enabled` is true; everything
else in the codebase runs exactly as it did before this package existed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


def overlay_available() -> bool:
    """True if the optional GUI stack is actually installed."""
    try:
        import PySide6  # noqa: F401
        import qasync  # noqa: F401
    except ImportError:
        return False
    return True


def run_with_overlay(cfg, coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Run `coro_factory()` to completion under a Qt-backed asyncio loop,
    with the HUD overlay shown for the duration.

    Raises the same way `asyncio.run(coro_factory())` would — callers that
    already catch KeyboardInterrupt around that call don't need to change.
    """
    import sys

    import qasync
    from PySide6.QtWidgets import QApplication

    from .overlay import Overlay

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    # Qt's own loop swallows Ctrl+C on Windows unless the interpreter gets a
    # chance to run periodically — this timer's only job is to wake it up.
    from PySide6.QtCore import QTimer

    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)

    overlay = Overlay(cfg)
    overlay.show()

    with loop:
        loop.run_until_complete(coro_factory())
