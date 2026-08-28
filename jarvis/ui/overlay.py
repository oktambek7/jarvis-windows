"""The HUD itself — a small frameless, always-on-top "arc reactor" widget
that lives in a screen corner and animates with Jarvis's state (asleep /
listening / working / speaking).

Deliberately NOT a full window: no taskbar entry, no border, no focus. It
exists purely as ambient feedback for something you already triggered with
your voice — you should never have to click it to use Jarvis.

Two things make this read as "alive" rather than a static icon that swaps
color: state transitions LERP (color, radius, glow) over a few hundred ms
instead of snapping, and it never goes fully inert — even asleep it has a
slow breathing pulse instead of sitting as a dead dot.
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
    State.SLEEPING: QColor(80, 130, 190),
    State.LISTENING: QColor(0, 210, 255),
    State.TOOL: QColor(255, 176, 46),
    State.SPEAKING: QColor(70, 205, 255),
    State.ERROR: QColor(255, 80, 80),
}

# Target "energy" per state — drives glow strength, ring radius and how fast
# things spin. Sleeping is a slow breath; everything else is wide awake.
_ENERGY: dict[State, float] = {
    State.SLEEPING: 0.22,
    State.LISTENING: 0.75,
    State.TOOL: 0.9,
    State.SPEAKING: 1.0,
    State.ERROR: 1.0,
}

_FRAME_MS = 16  # ~60 fps — smooth motion, still cheap enough to run forever
_LERP = 0.12    # per-frame interpolation factor for color/energy/radius


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        int(_lerp(a.red(), b.red(), t)),
        int(_lerp(a.green(), b.green(), t)),
        int(_lerp(a.blue(), b.blue(), t)),
    )


def _round_pen(color: QColor, width: float) -> QPen:
    """A QPen with round line caps.

    PySide6's QPen constructor does not accept `cap` as a keyword — passing
    one raises AttributeError from inside paintEvent, on every single frame.
    setCapStyle() after construction is the actual API.
    """
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    return pen


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
        self._state_since = time.monotonic()
        self._opacity = 1.0
        self._energy = _ENERGY[State.SLEEPING]
        self._color = QColor(_COLORS[State.SLEEPING])
        self._spin = 0.0  # accumulated rotation, radians — keeps spinning smoothly across states
        self._drag_origin: QPoint | None = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_FRAME_MS)
        self._last_tick = time.monotonic()

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

    # ------------------------------------------------------------- animation

    def _target_opacity(self) -> float:
        # Never fully inert: even a long sleep settles to a faint breathing
        # glow rather than vanishing, so the HUD still reads as "alive".
        if self._state == State.SLEEPING:
            idle_for = time.monotonic() - self._state_since
            settle = min(idle_for / 3.0, 1.0)
            return _lerp(1.0, 0.35, settle)
        return 1.0

    def _tick(self) -> None:
        now = time.monotonic()
        dt = max(now - self._last_tick, 0.0)
        self._last_tick = now

        target_color = _COLORS.get(self._state, _COLORS[State.SLEEPING])
        target_energy = _ENERGY.get(self._state, 0.3)

        self._opacity = _lerp(self._opacity, self._target_opacity(), _LERP)
        self._color = _lerp_color(self._color, target_color, _LERP)
        self._energy = _lerp(self._energy, target_energy, _LERP)
        # Spin speed scales with energy — idle drifts slowly, active states
        # sweep visibly faster. Always accumulating (never reset) so a state
        # change never causes a visible jump in rotation.
        self._spin += dt * (0.6 + self._energy * 2.2)

        self.setWindowOpacity(max(0.0, min(1.0, self._opacity)))
        self.update()

    # ------------------------------------------------------------- painting

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._opacity <= 0.01:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        center = self.rect().center()
        breath = 0.5 + 0.5 * math.sin(self._spin * 0.8)

        # Soft glow behind everything else, sized and brightened by energy.
        glow_radius = self._size * (0.32 + 0.14 * self._energy)
        glow = QRadialGradient(center, glow_radius)
        glow_color = QColor(self._color)
        glow_color.setAlpha(int(40 + 55 * self._energy + 15 * breath))
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(self._color.red(), self._color.green(), self._color.blue(), 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, glow_radius, glow_radius)

        if self._state == State.SPEAKING:
            self._paint_speaking(painter, center)
        elif self._state == State.TOOL:
            self._paint_tool(painter, center)
        else:
            self._paint_ring(painter, center, breath)

    def _paint_ring(self, painter, center, breath: float) -> None:
        # A slowly rotating double ring — subtle at idle, brighter and wider
        # once listening. Always spinning a little so it never looks frozen.
        base_radius = self._size * (0.24 + 0.05 * self._energy)
        wobble = math.sin(self._spin * 1.6) * 3 * self._energy
        radius = base_radius + wobble

        painter.setPen(_round_pen(self._color, 2.5 + 1.5 * self._energy))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, radius, radius)

        # A faint inner ring, counter-rotating, purely decorative texture.
        inner_color = QColor(self._color)
        inner_color.setAlpha(int(60 + 40 * breath))
        painter.setPen(QPen(inner_color, 1.2))
        painter.drawEllipse(center, radius * 0.72, radius * 0.72)

    def _paint_tool(self, painter, center) -> None:
        # A comet-trail rotating arc: several fading copies behind the
        # leading edge read as motion blur instead of a rigid spinner.
        radius = self._size * 0.3
        rect_size = radius * 2
        top_left = center - QPoint(int(radius), int(radius))
        lead = math.degrees(self._spin) % 360

        for offset, alpha, width in ((0, 255, 4.5), (26, 150, 4.0), (52, 90, 3.2), (78, 45, 2.4)):
            color = QColor(self._color)
            color.setAlpha(alpha)
            painter.setPen(_round_pen(color, width))
            start = int((lead - offset) * 16) % (360 * 16)
            painter.drawArc(
                top_left.x(), top_left.y(), int(rect_size), int(rect_size), start, 70 * 16
            )

    def _paint_speaking(self, painter, center) -> None:
        # A ring of bars pulsing at staggered phases — a cheap stand-in for a
        # real amplitude-driven equalizer (future work: feed actual playback
        # RMS from AudioIO instead of a sine wave).
        bars = 20
        inner = self._size * 0.2
        painter.setPen(_round_pen(self._color, 3))
        for i in range(bars):
            angle = (2 * math.pi / bars) * i
            wobble = 0.5 + 0.5 * math.sin(self._spin * 5 + i * 0.85)
            length = inner * 0.3 + inner * 0.55 * wobble
            x1 = center.x() + math.cos(angle) * inner
            y1 = center.y() + math.sin(angle) * inner
            x2 = center.x() + math.cos(angle) * (inner + length)
            y2 = center.y() + math.sin(angle) * (inner + length)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        # A steady core ring ties the bars together instead of leaving a hole.
        painter.setPen(QPen(self._color, 2))
        painter.drawEllipse(center, inner * 0.85, inner * 0.85)
