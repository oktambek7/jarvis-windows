"""The dynamic HUD overlay — Jarvis's visual presence.

Everything in this package is optional. `jarvis.ui.bus` has zero third-party
dependencies and is imported unconditionally by app.py/live.py so they can
publish state changes without caring whether a screen is attached. The
overlay itself (`jarvis.ui.overlay`, `jarvis.ui.runtime`) needs PySide6 and
is only imported when `ui.overlay.enabled` is true AND that package is
installed — see `jarvis.ui.runtime.overlay_available()`.
"""

from __future__ import annotations
