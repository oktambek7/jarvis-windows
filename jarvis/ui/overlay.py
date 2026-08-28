"""The HUD itself — a small frameless, click-through-free, always-on-top
"arc reactor" widget that lives in a screen corner and animates with
Jarvis's state (asleep / listening / working / speaking).

Deliberately NOT a full window: no taskbar entry, no border, no focus. It
exists purely as ambient feedback for something you already triggered with
your voice — you should never have to click it to use Jarvis.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from .bus import Event, State, bus

_COLORS: dict[State, QColor] = {
    State.SLEEPING: QColor(90, 140, 190),
    State.LISTENING: QColor(0, 200, 255),
    State.TOOL: QColor(255, 176, 46),
    State.SPEAKING: QColor(64, 200, 255),
    State.ERROR: QColor(255, 80, 80),
}

# How long Jarvis has to sit in SLEEPING before the ring fades out entirely,
# so it isn't a permanent glowing dot on your desktop between conversations.
_IDLE_FADE_AFTER = 2.5
_FRAME_MS = 33  # ~30 fps — smooth enough for a glow, cheap enough to idle forever


class Overlay(QWidget):
    def __init__(self, cfg) -> None:
        super().__init__()
        self._cfg = cfg
        self._size = int(cfg.get("ui.overlay.size", 160))
        self._pos_file = Path(cfg.path("storage.db", "data/jarvis.db")).parent / "ui_overlay_pos.json"

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self._size, self._size)
        self._place()

        self._state = State.SLEEPING
        self._detail = ""
        self._state_since = time.monotonic()
        self._opacity = 1.0
        self._drag_origin: QPoint | None = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_FRAME_MS)

        bus.subscribe(self._on_event)

    # ------------------------------------------------------------- placement

    def _place(self) -> None:
        saved = self._load_saved_pos()
        if saved is not None:
            self.move(*saved)
            return

        screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        margin = int(self._cfg.get("ui.overlay.margin", 40))
        corner = str(self._cfg.get("ui.overlay.corner", "bottom-right")).lower()

        if geo is None:
            self.move(100, 100)
            return

        x = geo.right() - self._size - margin if "right" in corner else geo.left() + margin
        y = geo.bottom() - self._size - margin if "bottom" in corner else geo.top() + margin
        self.move(x, y)

    def _load_saved_pos(self) -> tuple[int, int] | None:
        try:
            data = json.loads(self._pos_file.read_text(encoding="utf-8"))
            return int(data["x"]), int(data["y"])
        except Exception:  # noqa: BLE001 - no saved position yet, or it's junk
            return None

    def _save_pos(self) -> None:
        try:
            self._pos_file.parent.mkdir(parents=True, exist_ok=True)
            self._pos_file.write_text(
                json.dumps({"x": self.x(), "y": self.y()}), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - remembering position is a nicety, not critical
            pass

    # ------------------------------------------------------------- dragging

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._drag_origin = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._drag_origin is not None:
            self._drag_origin = None
            self._save_pos()

    # ------------------------------------------------------------- state

    def _on_event(self, ev: Event) -> None:
        if ev.state != self._state:
            self._state = ev.state
            self._state_since = time.monotonic()
        self._detail = ev.detail

    # ------------------------------------------------------------- animation

    def _target_opacity(self) -> float:
        if self._state == State.SLEEPING:
            idle_for = time.monotonic() - self._state_since
            if idle_for > _IDLE_FADE_AFTER:
                return 0.0
            return 1.0 - (idle_for / _IDLE_FADE_AFTER) * 0.7
        return 1.0

    def _tick(self) -> None:
        target = self._target_opacity()
        self._opacity += (target - self._opacity) * 0.15
        self.setWindowOpacity(max(0.0, min(1.0, self._opacity)))
        self.update()

    # ------------------------------------------------------------- painting

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._opacity <= 0.01:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        color = _COLORS.get(self._state, _COLORS[State.SLEEPING])
        center = self.rect().center()
        phase = (time.monotonic() * 2.0) % (2 * math.pi)

        # Soft glow behind everything else.
        glow = QRadialGradient(center, self._size / 2)
        glow_color = QColor(color)
        glow_color.setAlpha(70 if self._state != State.SLEEPING else 30)
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, self._size / 2, self._size / 2)

        if self._state == State.SPEAKING:
            self._paint_speaking(painter, center, color, phase)
        elif self._state == State.TOOL:
            self._paint_tool(painter, center, color, phase)
        else:
            self._paint_ring(painter, center, color, phase, pulsing=self._state == State.LISTENING)

    def _paint_ring(self, painter, center, color, phase, pulsing: bool) -> None:
        radius = self._size * 0.28
        if pulsing:
            radius += math.sin(phase * 2) * 4
        pen = QPen(color, 3)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, radius, radius)

    def _paint_tool(self, painter, center, color, phase) -> None:
        # A rotating broken ring — "thinking"/"working" without implying speech.
        radius = self._size * 0.28
        pen = QPen(color, 4, cap=Qt.RoundCap)
        painter.setPen(pen)
        span = 110 * 16  # Qt angles are in 1/16th of a degree
        start = int(math.degrees(phase) * 16) % (360 * 16)
        rect_size = radius * 2
        top_left = center - QPoint(int(radius), int(radius))
        painter.drawArc(top_left.x(), top_left.y(), int(rect_size), int(rect_size), start, span)

    def _paint_speaking(self, painter, center, color, phase) -> None:
        # A ring of short bars pulsing at staggered phases — a cheap stand-in
        # for a real amplitude-driven equalizer (future work: feed actual
        # playback RMS from AudioIO instead of a fake sine wave).
        bars = 16
        inner = self._size * 0.22
        pen = QPen(color, 3, cap=Qt.RoundCap)
        painter.setPen(pen)
        for i in range(bars):
            angle = (2 * math.pi / bars) * i
            wobble = 0.5 + 0.5 * math.sin(phase * 4 + i * 0.9)
            length = inner * 0.35 + inner * 0.5 * wobble
            x1 = center.x() + math.cos(angle) * inner
            y1 = center.y() + math.sin(angle) * inner
            x2 = center.x() + math.cos(angle) * (inner + length)
            y2 = center.y() + math.sin(angle) * (inner + length)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
