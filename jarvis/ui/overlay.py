"""The HUD itself — a small frameless, always-on-top widget that lives in a
screen corner and animates with Jarvis's state (asleep / listening / working
/ speaking).

Deliberately NOT a full window: no taskbar entry, no border, no focus. It
exists purely as ambient feedback for something you already triggered with
your voice — you should never have to click it to use Jarvis.

Three things make this read as "alive" rather than a static icon that swaps
color: state transitions LERP (color, radius, glow, and now SIZE) over a few
hundred ms instead of snapping; it never goes fully inert — even asleep it
has a slow breathing pulse instead of sitting as a dead dot; and it physically
grows from a small idle dot into a larger translucent "glass panel" the
moment there's anything to show, holding a bigger, richer visualization
inside, then contracts back to the dot once the conversation ends. No text is
ever rendered on it — it stays a pure visual/motion indicator.
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
_LERP = 0.12    # per-frame interpolation factor for color/energy/radius/size


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
        # `ui.overlay.size` is now a base scale, not a fixed pixel size: idle
        # is roughly half of it (a small dot), the active panel is roughly
        # 1.7x wider and slightly shorter than it (a wide glass panel) — see
        # _target_size(). Bumped default 160 -> 200 so "bigger" actually
        # reads as bigger at both ends of that range.
        base = int(cfg.get("ui.overlay.size", 200))
        self._idle_size = max(40, int(base * 0.45))
        self._panel_w = int(base * 1.7)
        self._panel_h = int(base * 0.85)
        self._pos_file = Path(cfg.path("storage.db", "data/jarvis.db")).parent / "ui_overlay_pos.json"

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._state = State.SLEEPING
        self._state_since = time.monotonic()
        self._opacity = 1.0
        self._energy = _ENERGY[State.SLEEPING]
        self._color = QColor(_COLORS[State.SLEEPING])
        self._spin = 0.0  # accumulated rotation, radians — keeps spinning smoothly across states
        self._drag_origin: QPoint | None = None

        # Current animated size, always eased toward _target_size(). The
        # widget's actual geometry is derived from this plus a fixed anchor
        # CENTER point, so growing/shrinking expands around a stable point on
        # screen instead of drifting off a fixed top-left corner.
        self._cur_w = float(self._idle_size)
        self._cur_h = float(self._idle_size)
        self._anchor = QPoint(100, 100)
        self._place()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_FRAME_MS)
        self._last_tick = time.monotonic()

        bus.subscribe(self._on_event)

    # ------------------------------------------------------------- placement

    def _place(self) -> None:
        saved = self._load_saved_anchor()
        if saved is not None:
            self._anchor = QPoint(*saved)
        else:
            screen = QGuiApplication.primaryScreen()
            geo = screen.availableGeometry() if screen else None
            margin = int(self._cfg.get("ui.overlay.margin", 40))
            corner = str(self._cfg.get("ui.overlay.corner", "bottom-right")).lower()
            half = self._idle_size // 2

            if geo is None:
                self._anchor = QPoint(100 + half, 100 + half)
            else:
                x = geo.right() - half - margin if "right" in corner else geo.left() + half + margin
                y = geo.bottom() - half - margin if "bottom" in corner else geo.top() + half + margin
                self._anchor = QPoint(x, y)

        self._apply_geometry()

    def _apply_geometry(self) -> None:
        """Resize/move the real window to match (_cur_w, _cur_h) centered on
        the anchor point, clamped so a big active panel never runs off the
        edge of the screen it's docked against.
        """
        w, h = max(1, int(self._cur_w)), max(1, int(self._cur_h))
        x = self._anchor.x() - w // 2
        y = self._anchor.y() - h // 2

        screen = QGuiApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = max(geo.left(), min(x, geo.right() - w))
            y = max(geo.top(), min(y, geo.bottom() - h))

        self.setGeometry(x, y, w, h)

    def _load_saved_anchor(self) -> tuple[int, int] | None:
        try:
            data = json.loads(self._pos_file.read_text(encoding="utf-8"))
            return int(data["x"]), int(data["y"])
        except Exception:  # noqa: BLE001 - no saved position yet, or it's junk
            return None

    def _save_anchor(self) -> None:
        try:
            self._pos_file.parent.mkdir(parents=True, exist_ok=True)
            self._pos_file.write_text(
                json.dumps({"x": self._anchor.x(), "y": self._anchor.y()}), encoding="utf-8"
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
            # The anchor is the CENTER of wherever it was just dropped, at
            # whatever size it happened to be — so the next size change grows
            # around the spot you actually put it, not the old corner math.
            self._anchor = self.geometry().center()
            self._save_anchor()

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

    def _target_size(self) -> tuple[float, float]:
        # Idle stays a small dot; every active state grows into the same
        # wide glass panel so the transition always reads as "waking up"
        # regardless of which state triggered it.
        if self._state == State.SLEEPING:
            return float(self._idle_size), float(self._idle_size)
        return float(self._panel_w), float(self._panel_h)

    def _tick(self) -> None:
        now = time.monotonic()
        dt = max(now - self._last_tick, 0.0)
        self._last_tick = now

        target_color = _COLORS.get(self._state, _COLORS[State.SLEEPING])
        target_energy = _ENERGY.get(self._state, 0.3)
        target_w, target_h = self._target_size()

        self._opacity = _lerp(self._opacity, self._target_opacity(), _LERP)
        self._color = _lerp_color(self._color, target_color, _LERP)
        self._energy = _lerp(self._energy, target_energy, _LERP)
        self._cur_w = _lerp(self._cur_w, target_w, _LERP)
        self._cur_h = _lerp(self._cur_h, target_h, _LERP)
        # Spin speed scales with energy — idle drifts slowly, active states
        # sweep visibly faster. Always accumulating (never reset) so a state
        # change never causes a visible jump in rotation.
        self._spin += dt * (0.6 + self._energy * 2.2)

        self.setWindowOpacity(max(0.0, min(1.0, self._opacity)))
        # While the user is actively dragging it, don't fight their mouse by
        # re-centering on the (stale) anchor every frame.
        if self._drag_origin is None:
            self._apply_geometry()
        self.update()

    # ------------------------------------------------------------- painting

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._opacity <= 0.01:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width, height = self.width(), self.height()
        center = self.rect().center()
        breath = 0.5 + 0.5 * math.sin(self._spin * 0.8)
        # The visualization inside scales off the SHORT side, so it always
        # fits comfortably inside the panel regardless of its aspect ratio.
        scale = min(width, height)

        span = self._panel_w - self._idle_size
        panel_amount = 0.0 if span <= 0 else max(0.0, min(1.0, (width - self._idle_size) / span))
        self._paint_panel_bg(painter, panel_amount)

        # Soft glow behind everything else, sized and brightened by energy.
        glow_radius = scale * (0.32 + 0.14 * self._energy)
        glow = QRadialGradient(center, glow_radius)
        glow_color = QColor(self._color)
        glow_color.setAlpha(int(40 + 55 * self._energy + 15 * breath))
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(self._color.red(), self._color.green(), self._color.blue(), 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, glow_radius, glow_radius)

        if self._state == State.SPEAKING:
            self._paint_speaking(painter, center, scale)
        elif self._state == State.TOOL:
            self._paint_tool(painter, center, scale)
        else:
            self._paint_ring(painter, center, breath, scale)

    def _paint_panel_bg(self, painter: QPainter, amount: float) -> None:
        """A translucent rounded "glass" backdrop, faded in as the HUD grows
        from the idle dot into the active panel — invisible at rest, fully
        present once expanded, never sharp or window-like.
        """
        if amount <= 0.01:
            return

        rect = self.rect().adjusted(1, 1, -1, -1)
        radius = min(rect.height() / 2, 36)

        fill = QColor(10, 16, 26)
        fill.setAlphaF(0.5 * amount)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)

        border = QColor(self._color)
        border.setAlphaF(0.6 * amount)
        painter.setPen(QPen(border, 1.4))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

    def _paint_ring(self, painter: QPainter, center: QPoint, breath: float, scale: float) -> None:
        # A slowly rotating double ring — subtle at idle, brighter and wider
        # once listening. Always spinning a little so it never looks frozen.
        base_radius = scale * (0.24 + 0.05 * self._energy)
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

    def _paint_tool(self, painter: QPainter, center: QPoint, scale: float) -> None:
        # A comet-trail rotating arc: several fading copies behind the
        # leading edge read as motion blur instead of a rigid spinner.
        radius = scale * 0.3
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

    def _paint_speaking(self, painter: QPainter, center: QPoint, scale: float) -> None:
        # A ring of bars pulsing at staggered phases — a cheap stand-in for a
        # real amplitude-driven equalizer (future work: feed actual playback
        # RMS from AudioIO instead of a sine wave).
        bars = 24
        inner = scale * 0.2
        painter.setPen(_round_pen(self._color, 3))
        for i in range(bars):
            angle = (2 * math.pi / bars) * i
            wobble = 0.5 + 0.5 * math.sin(self._spin * 5 + i * 0.85)
            length = inner * 0.3 + inner * 0.6 * wobble
            x1 = center.x() + math.cos(angle) * inner
            y1 = center.y() + math.sin(angle) * inner
            x2 = center.x() + math.cos(angle) * (inner + length)
            y2 = center.y() + math.sin(angle) * (inner + length)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        # A steady core ring ties the bars together instead of leaving a hole.
        painter.setPen(QPen(self._color, 2))
        painter.drawEllipse(center, inner * 0.85, inner * 0.85)
