"""NextGen ERP — "Command Deck" dashboard.

One painted, theme-aware surface: living aurora background, glass cards with cursor spotlight,
a hero with a daily-goal ring, count-up KPIs, a draw-in sales trend with previous-period ghost line,
interactive payment donut, heat calendar, product ranking, clickable attention list and insights.

Design rules
  * Everything is painted from one palette (`self.pal`). `DashboardView.apply_palette()` swaps the
    palette in place: no widgets are rebuilt, no intro animation replays, no stylesheet re-polish of
    the cards. That is what removes the flicker when switching light <-> dark.
  * Entrance animation plays once. Later refreshes only tween values.
  * Data comes from PulseService + OperationsService; nothing is invented.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from fx import Motion, ease, run_anim
from insights import PulseService, money as fmt_money


# ── helpers ───────────────────────────────────────────────────────────────────

class _Ticker(QObject):
    ticked = Signal()

    def __init__(self):
        super().__init__()
        self._timer = QTimer(self)
        # Library-style: one shared heartbeat ~12 fps is enough for ring/chart pulse
        # without saturating the UI thread when several glass cards repaint.
        self._timer.setInterval(80)
        self._timer.timeout.connect(self.ticked)
        self._subs = 0
        self._app_active = True
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.applicationStateChanged.connect(self._on_app_state)
        except Exception:
            pass

    def _on_app_state(self, state):
        # Pause continuous motion while the window is minimised / occluded so the UI
        # thread is free for other work and battery/CPU load drops.
        try:
            from PySide6.QtCore import Qt as _Qt
            self._app_active = state == _Qt.ApplicationActive
        except Exception:
            self._app_active = True
        if self._app_active and self._subs > 0 and not self._timer.isActive():
            self._timer.start()
        elif not self._app_active and self._timer.isActive():
            self._timer.stop()

    def subscribe(self, slot):
        # Continuous ticks only when Motion.continuous is on (off by default for low-end PCs).
        if not Motion.enabled or not getattr(Motion, "continuous", False):
            return
        self.ticked.connect(slot)
        self._subs += 1
        if self._app_active and not self._timer.isActive():
            self._timer.start()

    def unsubscribe(self, slot):
        try:
            self.ticked.disconnect(slot)
        except (RuntimeError, TypeError):
            return
        self._subs = max(0, self._subs - 1)
        if self._subs == 0:
            self._timer.stop()


TICK = _Ticker()


def now():
    return time.monotonic()


def rgba(c, a):
    q = QColor(c)
    q.setAlpha(int(max(0, min(255, a))))
    return q


def mix(a, b, t):
    a, b = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


def is_dark(pal):
    return QColor(pal["bg"]).lightnessF() < 0.45


def lum(c):
    c = QColor(c)
    ch = [c.redF(), c.greenF(), c.blueF()]
    ch = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def ideal_text(bg):
    return QColor("#FFFFFF") if lum(bg) < 0.38 else QColor("#172234")


def smooth_path(pts, ymin=None, ymax=None):
    path = QPainterPath()
    if not pts:
        return path
    path.moveTo(pts[0])
    if len(pts) < 3:
        for p in pts[1:]:
            path.lineTo(p)
        return path
    for i in range(len(pts) - 1):
        p0, p1, p2, p3 = pts[max(0, i - 1)], pts[i], pts[i + 1], pts[min(len(pts) - 1, i + 2)]
        y1 = p1.y() + (p2.y() - p0.y()) / 6
        y2 = p2.y() - (p3.y() - p1.y()) / 6
        if ymin is not None:
            y1, y2 = max(ymin, min(ymax, y1)), max(ymin, min(ymax, y2))
        path.cubicTo(QPointF(p1.x() + (p2.x() - p0.x()) / 6, y1), QPointF(p2.x() - (p3.x() - p1.x()) / 6, y2), p2)
    return path


def nice_max(v):
    if v <= 4:
        return 4
    mag = 10 ** int(math.log10(max(v, 1)))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * mag:
            return m * mag
    return v


def compact(v):
    v = float(v)
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M".replace(".0M", "M")
    if a >= 1e3:
        return f"{v / 1e3:.1f}k".replace(".0k", "k")
    return f"{v:,.0f}"


def pct_change(cur, prev):
    if prev and prev > 0:
        return (cur - prev) / prev * 100.0
    return None


TONE = {"bad": "red", "warn": "orange", "info": "blue", "good": "green"}
SERIES_KEYS = ["blue", "green", "purple", "cyan", "orange", "pink", "red"]


# ── aurora background ─────────────────────────────────────────────────────────

class AuroraViewport(QWidget):
    """Scroll-area viewport with a static colour field behind the cards.

    Library-style performance: no continuous full-viewport timer. The aurora still
    looks vibrant; it is painted on show/resize/theme change only — not every frame.
    """

    def __init__(self, pal, parent=None):
        super().__init__(parent)
        self.pal = pal
        self.setAutoFillBackground(False)

    def start(self):
        self.update()

    def stop(self):
        pass

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self.pal
        dark = is_dark(pal)
        r = QRectF(self.rect())
        p.fillRect(r, QColor(pal["bg"]))
        # Fixed phase so repaints are stable and cheap (no continuous drift).
        t = 0.85
        w, h = r.width(), r.height()
        blobs = (
            ("blue", 0.18, 0.12, 0.62, 0.9, 0.0),
            ("purple", 0.82, 0.30, 0.55, 0.7, 1.7),
            ("cyan", 0.45, 0.88, 0.60, 0.6, 3.1),
            ("pink", 0.95, 0.92, 0.40, 0.5, 4.4),
        )
        base_a = 54 if dark else 38
        p.setPen(Qt.NoPen)
        for key, ox, oy, rad, spd, ph in blobs:
            cx = w * (ox + 0.07 * math.sin(t * spd + ph))
            cy = h * (oy + 0.06 * math.cos(t * spd * 0.8 + ph))
            rr = max(w, h) * rad
            g = QRadialGradient(QPointF(cx, cy), rr)
            g.setColorAt(0, rgba(pal[key], base_a))
            g.setColorAt(1, rgba(pal[key], 0))
            p.setBrush(g)
            p.drawEllipse(QPointF(cx, cy), rr, rr)
        # Sparse grid — previous dense point grid was expensive on large viewports.
        p.setPen(QPen(rgba(pal["text"], 9 if dark else 7), 1))
        step = 48
        for gx in range(0, int(w), step):
            for gy in range(0, int(h), step):
                p.drawPoint(gx, gy)


# ── glass card base ───────────────────────────────────────────────────────────

class Glass(QFrame):
    PAD = 8
    RAD = 18

    def __init__(self, pal, accent="blue", parent=None):
        super().__init__(parent)
        self.pal = pal
        self.accent = accent
        self.hover = 0.0
        self.mouse = QPointF(-1, -1)
        self.appear = 1.0
        self.intro = 1.0
        self.live = False
        self._hanim = self._aanim = self._ianim = None
        self._subscribed = False
        self._last_mouse_paint = QPointF(-999, -999)
        self.setObjectName("glass")
        self.setMouseTracking(False)  # no per-pixel tracking — enter/leave only
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def enter(self, delay=0, ms=400):
        if not Motion.enabled:
            self.appear = 1.0
            self.update()
            return
        # Shorter entrance for low-end machines; design is the same final state.
        self.appear = 0.0
        self.update()
        self._aanim = run_anim(0.0, 1.0, min(ms, 400), self._set_appear, ease("out"), delay=min(delay, 120), owner=self)

    def _set_appear(self, v):
        self.appear = float(v)
        self.update()

    def play(self, ms=1000, delay=0):
        self.intro = 0.0 if Motion.enabled else 1.0
        self._ianim = run_anim(0.0, 1.0, ms, self._set_intro, ease("out"), delay=delay, owner=self)
        self.update()

    def _set_intro(self, v):
        self.intro = float(v)
        self.update()

    def _set_hover(self, v):
        self.hover = float(v)
        self.update()

    def enterEvent(self, e):
        super().enterEvent(e)
        # Instant hover state — no multi-frame animation on every card under the cursor.
        self._set_hover(1.0)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.mouse = QPointF(-1, -1)
        self._set_hover(0.0)

    def mouseMoveEvent(self, e):
        # Hover border is driven by enter/leave animations only — no per-pixel repaints.
        self.mouse = e.position()

    def _on_tick(self):
        self.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self.live and not self._subscribed:
            self._subscribed = True
            TICK.subscribe(self._on_tick)

    def hideEvent(self, e):
        super().hideEvent(e)
        if self.live and self._subscribed:
            self._subscribed = False
            TICK.unsubscribe(self._on_tick)

    def card_rect(self):
        lift = 2.5 * self.hover
        return QRectF(self.rect()).adjusted(self.PAD, self.PAD - 2 - lift, -self.PAD, -self.PAD - 2 - lift)

    def font_(self, pt, bold=False):
        f = QFont(self.font())
        f.setPointSizeF(pt)
        f.setBold(bold)
        return f

    def text(self, p, rect, s, pt, color, bold=False, align=Qt.AlignLeft | Qt.AlignVCenter, elide=True):
        p.setFont(self.font_(pt, bold))
        p.setPen(QColor(color))
        s = str(s)
        if elide:
            s = QFontMetrics(p.font()).elidedText(s, Qt.ElideRight, int(rect.width()))
        p.drawText(rect, align, s)

    def begin(self, p):
        """Common start of every paintEvent. Returns the card rect with the entrance transform applied."""
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setOpacity(max(0.0, min(1.0, self.appear)))
        p.translate(0, (1.0 - self.appear) * 18)
        r = self.card_rect()
        self.paint_card(p, r)
        return r

    def paint_card(self, p, r):
        pal, dark = self.pal, is_dark(self.pal)
        acc = QColor(pal[self.accent])
        rad = self.RAD
        p.setPen(Qt.NoPen)
        # Single soft drop shadow — glass look without multi-pass cost.
        p.setBrush(QColor(0, 0, 0, 28 if dark else 16))
        p.drawRoundedRect(r.adjusted(-1, 1, 1, 4), rad + 1, rad + 1)
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        top = QColor(pal["panel"])
        bot = QColor(pal["panel"])
        if dark:
            top = mix(top, QColor("#FFFFFF"), 0.07)
            top.setAlpha(226)
            bot.setAlpha(214)
        else:
            top = QColor("#FFFFFF")
            top.setAlpha(232)
            bot = mix(QColor(pal["panel"]), QColor(pal["panel2"]), 0.35)
            bot.setAlpha(218)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, top)
        g.setColorAt(1, bot)
        p.fillPath(path, g)
        # Border
        bg = QLinearGradient(r.topLeft(), r.bottomRight())
        if dark:
            bg.setColorAt(0, rgba("#FFFFFF", 52))
            bg.setColorAt(0.5, rgba(pal["border"], 150))
            bg.setColorAt(1, rgba(pal["border"], 90))
        else:
            bg.setColorAt(0, rgba("#FFFFFF", 255))
            bg.setColorAt(0.5, rgba(pal["border"], 200))
            bg.setColorAt(1, rgba(pal["border"], 150))
        p.setPen(QPen(QBrush(bg), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, rad, rad)
        # Simple hover accent border (no radial spotlight — that was a major cost on mouse move).
        if self.hover > 0.01:
            p.setPen(QPen(rgba(acc, 140 * self.hover), 1.4))
            p.drawRoundedRect(r, rad, rad)

    def header(self, p, r, title, subtitle=""):
        self.text(p, QRectF(r.left() + 20, r.top() + 14, r.width() - 160, 22), title, 12, self.pal["text"], True)
        if subtitle:
            self.text(p, QRectF(r.left() + 20, r.top() + 36, r.width() - 160, 16), subtitle, 8.5, self.pal["muted"])

    def tooltip(self, p, anchor, lines, bounds):
        p.setFont(self.font_(9, True))
        fm = QFontMetrics(p.font())
        tw = max(fm.horizontalAdvance(t) for t in lines) + 26
        th = 12 + 17 * len(lines)
        x = anchor.x() + 16
        if x + tw > bounds.right():
            x = anchor.x() - tw - 16
        y = max(bounds.top(), min(anchor.y() - th / 2, bounds.bottom() - th))
        box = QRectF(x, y, tw, th)
        dark = is_dark(self.pal)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawRoundedRect(box.adjusted(-2, 2, 2, 5), 11, 11)
        p.setBrush(QColor("#0B1224") if dark else QColor("#1C2433"))
        p.setPen(QPen(rgba("#FFFFFF", 40), 1))
        p.drawRoundedRect(box, 10, 10)
        for j, t in enumerate(lines):
            p.setFont(self.font_(9, j == 0))
            p.setPen(QColor("#FFFFFF") if j == 0 else QColor("#C9D3E8"))
            p.drawText(QRectF(box.left() + 13, box.top() + 6 + j * 17, tw - 16, 17), Qt.AlignLeft | Qt.AlignVCenter, t)


# ── hero ──────────────────────────────────────────────────────────────────────

class Hero(Glass):
    searched = Signal(str)
    goalClicked = Signal()

    def __init__(self, pal, dark=False, parent=None):
        super().__init__(pal, "blue", parent)
        # No continuous repaint loop — design stays; clock updates on dashboard refresh.
        self.live = False
        self.setMinimumHeight(212)
        self.business, self.first_name, self.tagline = "Your Business", "", ""
        self.logo_pm = None
        self.pulse = None
        self.currency = "KES"
        self.ring = 0.0
        self._ring_anim = None
        self._ring_target = 0.0
        self.hmouse = QPointF(0.5, 0.5)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("⌕  Jump anywhere — product, sale, customer, page…   (Enter)")
        self.search.returnPressed.connect(lambda: self.searched.emit(self.search.text().strip()))
        self.restyle()

    def set_identity(self, business, first_name, tagline="", logo_b64=""):
        self.business = business or "Your Business"
        self.first_name = first_name or ""
        self.tagline = tagline or ""
        if logo_b64:
            try:
                import base64
                pm = QPixmap()
                pm.loadFromData(base64.b64decode(logo_b64))
                if not pm.isNull():
                    self.logo_pm = pm
            except Exception:
                pass
        self.update()

    def set_pulse(self, pulse, currency):
        self.pulse, self.currency = pulse, currency
        target = 0.0
        if pulse is not None and pulse.goal and pulse.goal > 0:
            target = float(pulse.goal_progress)
        self._ring_target = target
        self._ring_anim = run_anim(self.ring, target, 1400, self._set_ring, ease("expo"), owner=self)
        self.update()

    def _set_ring(self, v):
        self.ring = float(v)

    def colors(self):
        pal, dark = self.pal, is_dark(self.pal)
        c1 = mix(pal["blue"], "#000000", 0.55 if dark else 0.18)
        c2 = mix(pal["purple"], pal["cyan"], 0.45)
        c2 = mix(c2, "#000000", 0.50 if dark else 0.12)
        return c1, c2

    def fg(self):
        c1, c2 = self.colors()
        return ideal_text(mix(c1, c2, 0.5))

    def restyle(self):
        fg = self.fg()
        light_text = fg.lightnessF() > 0.5
        tone = "255,255,255" if light_text else "23,34,52"
        self.search.setStyleSheet(
            f"QLineEdit{{background:rgba({tone},0.16);border:1px solid rgba({tone},0.38);border-radius:18px;"
            f"padding:6px 16px;color:{fg.name()};selection-background-color:rgba({tone},0.30);}}"
            f"QLineEdit:focus{{background:rgba({tone},0.26);border:1px solid rgba({tone},0.75);}}"
        )

    def resizeEvent(self, e):
        super().resizeEvent(e)
        r = self.card_rect()
        stats_w = 330 if r.width() > 900 else 0
        self.search.setGeometry(int(r.left() + 118), int(r.bottom() - 58), int(max(160, min(470, r.width() - 150 - stats_w))), 38)

    def ring_rect(self):
        r = self.card_rect()
        d = 124
        return QRectF(r.right() - d - 150, r.top() + (r.height() - d) / 2, d, d)

    def _over_ring(self, pos):
        rr = self.ring_rect()
        return self.card_rect().width() > 900 and math.hypot(pos.x() - rr.center().x(), pos.y() - rr.center().y()) < rr.width() * 0.5

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        w, h = max(1, self.width()), max(1, self.height())
        self.hmouse = QPointF(e.position().x() / w, e.position().y() / h)
        self.setCursor(Qt.PointingHandCursor if self._over_ring(e.position()) else Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._over_ring(e.position()):
            self.goalClicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        path = QPainterPath()
        path.addRoundedRect(r, self.RAD, self.RAD)
        c1, c2 = self.colors()
        fg = self.fg()
        p.save()
        p.setClipPath(path)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, c1)
        g.setColorAt(1, c2)
        p.fillRect(r, g)
        t = now() if Motion.enabled else 0.0
        mx, my = self.hmouse.x(), self.hmouse.y()
        p.setPen(Qt.NoPen)
        for ox, oy, rad, spd, ph, a in ((0.12, 0.30, 150, 0.35, 0.0, 70), (0.62, 0.10, 190, 0.25, 1.4, 55),
                                        (0.92, 0.85, 140, 0.45, 2.6, 60), (0.40, 1.00, 110, 0.55, 0.9, 40)):
            cx = r.left() + r.width() * ox + math.sin(t * spd + ph) * 36 - (mx - 0.5) * 44
            cy = r.top() + r.height() * oy + math.cos(t * spd + ph) * 22 - (my - 0.5) * 30
            rg = QRadialGradient(QPointF(cx, cy), rad)
            rg.setColorAt(0, rgba("#FFFFFF", a * 0.55))
            rg.setColorAt(1, rgba("#FFFFFF", 0))
            p.setBrush(rg)
            p.drawEllipse(QPointF(cx, cy), rad, rad)
        for i in range(22):
            seed = (i * 97 % 101) / 101.0
            sp = 6 + (i % 5) * 3
            x = r.left() + ((seed * 1.7 + i * 0.173) % 1.0) * r.width() + (mx - 0.5) * (8 + i % 4 * 5)
            y = r.bottom() - ((t * sp + seed * 400) % (r.height() + 20))
            rad = 1.2 + (i % 3) * 0.8
            fade = 1 - abs((r.bottom() - y) / (r.height() + 20) - 0.5) * 1.6
            p.setBrush(rgba("#FFFFFF", max(0, 90 * fade)))
            p.drawEllipse(QPointF(x, y), rad, rad)
        cycle = (t % 9.0) / 9.0
        if Motion.enabled and cycle < 0.35:
            k = cycle / 0.35
            x = r.left() - 200 + (r.width() + 400) * k
            sg = QLinearGradient(x - 90, 0, x + 90, 0)
            sg.setColorAt(0, rgba("#FFFFFF", 0))
            sg.setColorAt(0.5, rgba("#FFFFFF", 26))
            sg.setColorAt(1, rgba("#FFFFFF", 0))
            p.fillRect(r, sg)
        p.restore()

        lr = QRectF(r.left() + 22, r.top() + 24, 80, 80)
        lp = QPainterPath()
        lp.addRoundedRect(lr, 20, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(rgba("#FFFFFF", 34))
        p.drawRoundedRect(lr.adjusted(-3, -3, 3, 3), 23, 23)
        if self.logo_pm is not None:
            p.save()
            p.setClipPath(lp)
            p.fillRect(lr, rgba("#FFFFFF", 235))
            pm = self.logo_pm.scaled(int(lr.width() - 10), int(lr.height() - 10), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap(int(lr.center().x() - pm.width() / 2), int(lr.center().y() - pm.height() / 2), pm)
            p.restore()
        else:
            lg = QLinearGradient(lr.topLeft(), lr.bottomRight())
            lg.setColorAt(0, rgba("#FFFFFF", 70))
            lg.setColorAt(1, rgba("#FFFFFF", 22))
            p.setBrush(lg)
            p.drawRoundedRect(lr, 20, 20)
            ini = "".join(w[0] for w in self.business.split()[:2]).upper() or "N"
            self.text(p, lr, ini, 24, fg, True, Qt.AlignCenter)

        tx = r.left() + 118
        hour = datetime.now().hour
        g_ = "GOOD MORNING" if hour < 12 else ("GOOD AFTERNOON" if hour < 17 else "GOOD EVENING")
        if self.first_name:
            g_ += f", {self.first_name.upper()}"
        soft = QColor(fg)
        soft.setAlpha(205)
        stats_w = 330 if r.width() > 900 else 0
        tw = max(100, r.width() - 150 - stats_w)
        self.text(p, QRectF(tx, r.top() + 22, tw, 18), g_, 9.5, soft, True)
        self.text(p, QRectF(tx, r.top() + 40, tw, 38), self.business, 22, fg, True)
        self.text(p, QRectF(tx, r.top() + 80, tw, 20), self.tagline or "Your connected workspace · offline-first · local data", 10, soft)
        if stats_w:
            self._paint_right(p, r, fg, soft)

    def _paint_right(self, p, r, fg, soft):
        pulse = self.pulse
        rr = self.ring_rect()
        t = now() if Motion.enabled else 0.0
        track = QColor(fg)
        track.setAlpha(46)
        w = 11
        inner = rr.adjusted(w, w, -w, -w)
        p.setBrush(Qt.NoBrush)
        has_goal = pulse is not None and pulse.goal and pulse.goal > 0
        pen = QPen(track, w)
        if not has_goal:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.drawEllipse(inner)
        if has_goal:
            prog = max(0.0, min(1.0, self.ring))
            if prog > 0.002:
                cg = QConicalGradient(inner.center(), 90)
                cg.setColorAt(0.0, QColor(fg))
                cg.setColorAt(1.0, mix(fg, self.pal["cyan"], 0.6))
                p.setPen(QPen(rgba(fg, 46), w + 8, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(inner, 90 * 16, int(-prog * 360 * 16))
                p.setPen(QPen(QBrush(cg), w, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(inner, 90 * 16, int(-prog * 360 * 16))
            self.text(p, QRectF(rr.left(), rr.center().y() - 24, rr.width(), 30), f"{self.ring * 100:.0f}%", 18, fg, True, Qt.AlignCenter)
            self.text(p, QRectF(rr.left(), rr.center().y() + 6, rr.width(), 16), "of daily goal", 8, soft, False, Qt.AlignCenter)
            if self._ring_target >= 1.0 and Motion.enabled:
                for i in range(8):
                    ang = t * 0.9 + i * math.pi / 4
                    rad = rr.width() / 2 + 5 + math.sin(t * 3 + i) * 3
                    p.setPen(Qt.NoPen)
                    p.setBrush(rgba("#FFFFFF", 150))
                    p.drawEllipse(QPointF(rr.center().x() + math.cos(ang) * rad, rr.center().y() + math.sin(ang) * rad), 2.2, 2.2)
        else:
            self.text(p, QRectF(rr.left(), rr.center().y() - 22, rr.width(), 28), "＋", 20, fg, True, Qt.AlignCenter)
            self.text(p, QRectF(rr.left(), rr.center().y() + 6, rr.width(), 16), "Set daily goal", 8.5, soft, True, Qt.AlignCenter)

        sx = rr.right() + 18
        rows = []
        if pulse is not None:
            rows.append(("TODAY", fmt_money(pulse.today, self.currency)))
            ch = pct_change(float(pulse.week_total), float(pulse.prev_week_total))
            rows.append(("WEEK VS LAST", "—" if ch is None else f"{'▲' if ch >= 0 else '▼'} {abs(ch):.0f}%"))
            rows.append(("STREAK", f"{pulse.streak_days} day{'s' if pulse.streak_days != 1 else ''}"))
        else:
            rows.append(("TODAY", "—"))
        for i, (k, v) in enumerate(rows):
            y = rr.top() + 6 + i * 38
            self.text(p, QRectF(sx, y, 130, 14), k, 7.5, soft, True)
            self.text(p, QRectF(sx, y + 13, 130, 22), v, 13, fg, True)
        clock = datetime.now().strftime("%a %d %b  ·  %H:%M:%S")
        self.text(p, QRectF(r.right() - 250, r.top() + 14, 232, 18), clock, 9.5, soft, True, Qt.AlignRight | Qt.AlignVCenter)


# ── quick tile ────────────────────────────────────────────────────────────────

class Tile(Glass):
    clicked = Signal(str)

    def __init__(self, pal, glyph, text, target, color, hint="", parent=None):
        super().__init__(pal, color, parent)
        self.glyph, self.label, self.target, self.hint = glyph, text, target, hint
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(86)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        col = QColor(self.pal[self.accent])
        d = 40 + 4 * self.hover
        cr = QRectF(r.left() + 14, r.center().y() - d / 2, d, d)
        g = QLinearGradient(cr.topLeft(), cr.bottomRight())
        g.setColorAt(0, col.lighter(118))
        g.setColorAt(1, col.darker(118))
        p.setPen(Qt.NoPen)
        if self.hover > 0.01:
            p.setBrush(rgba(col, 60 * self.hover))
            p.drawRoundedRect(cr.adjusted(-5, -5, 5, 5), 16, 16)
        p.setBrush(g)
        p.drawRoundedRect(cr, 13, 13)
        self.text(p, cr, self.glyph, 14, ideal_text(col), True, Qt.AlignCenter)
        x = cr.right() + 12
        self.text(p, QRectF(x, r.center().y() - 19, max(10, r.right() - x - 28), 20), self.label, 10, self.pal["text"], True)
        self.text(p, QRectF(x, r.center().y() + 1, max(10, r.right() - x - 28), 16), self.hint, 8, self.pal["muted"])
        self.text(p, QRectF(r.right() - 28, r.center().y() - 10, 18, 20), "›", 14, col, True, Qt.AlignCenter)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.target)


# ── KPI card ──────────────────────────────────────────────────────────────────

class KpiCard(Glass):
    clicked = Signal(str)

    def __init__(self, pal, d, currency="KES", parent=None):
        super().__init__(pal, d["color"], parent)
        self.d = d
        self.currency = currency
        self.val = 0.0
        self._vanim = None
        self.series = []
        self.setMinimumHeight(176)
        self.setCursor(Qt.PointingHandCursor)

    def apply(self, d, animate=True, delay=0):
        self.d = d
        self.accent = d["color"]
        target = float(d.get("value") or 0)
        if animate and Motion.enabled and abs(target - self.val) >= 0.5:
            self._vanim = run_anim(self.val, target, 450, self._set_val, ease("out"), delay=min(delay, 80), owner=self)
        else:
            self._set_val(target)
        new_series = list(d.get("series") or [])
        if new_series != self.series:
            self.series = new_series
            if animate and Motion.enabled:
                self.play(500, min(delay, 80))
        self.update()

    def _set_val(self, v):
        self.val = float(v)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        pal, d = self.pal, self.d
        col = QColor(pal[d["color"]])
        chip = QRectF(r.left() + 16, r.top() + 15, 30, 30)
        p.setPen(Qt.NoPen)
        p.setBrush(rgba(col, 44))
        p.drawRoundedRect(chip, 9, 9)
        self.text(p, chip, d["glyph"], 12, col, True, Qt.AlignCenter)
        self.text(p, QRectF(chip.right() + 10, chip.top(), max(10, r.width() - 130), 30), d["label"], 10, pal["muted"], True)
        dl = d.get("delta")
        if dl:
            good = dl > 0
            c = QColor(pal["green"] if good else pal["red"])
            s = f"{'▲' if good else '▼'} {compact(abs(dl))}"
            p.setFont(self.font_(8.5, True))
            w = QFontMetrics(p.font()).horizontalAdvance(s) + 18
            pill = QRectF(r.right() - w - 14, chip.top() + 4, w, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(rgba(c, 38))
            p.drawRoundedRect(pill, 11, 11)
            self.text(p, pill, s, 8.5, c, True, Qt.AlignCenter)
        s = f"{self.val:,.0f}"
        prefix = self.currency if d.get("money") else ""
        pw = QFontMetrics(self.font_(9, True)).horizontalAdvance(prefix) + 8 if prefix else 0
        avail = r.width() - 32 - pw
        pt = 24.0
        while pt > 12 and QFontMetrics(self.font_(pt, True)).horizontalAdvance(s) > avail:
            pt -= 1
        x = r.left() + 16
        if prefix:
            self.text(p, QRectF(x, r.top() + 60, 60, 30), prefix, 9, pal["muted"], True, Qt.AlignLeft | Qt.AlignVCenter, False)
            x += pw
        self.text(p, QRectF(x, r.top() + 52, r.right() - 16 - x, 40), s, pt, col, True, elide=False)
        self.text(p, QRectF(r.left() + 16, r.top() + 94, r.width() - 32, 16), d.get("sub", ""), 8.5, pal["muted"])
        if len(self.series) >= 2:
            self._spark(p, QRectF(r.left() + 14, r.top() + 116, r.width() - 28, r.height() - 128), col)
        else:
            self.text(p, QRectF(r.right() - 90, r.bottom() - 70, 80, 60), d["glyph"], 40, rgba(col, 34), True, Qt.AlignCenter, False)

    def _spark(self, p, sr, col):
        vals = [float(v) for v in self.series]
        peak = max(vals) or 1.0
        n = len(vals)
        if sr.height() < 14:
            return
        pts = [QPointF(sr.left() + i * sr.width() / (n - 1), sr.bottom() - 3 - (v / peak) * (sr.height() - 8) * self.intro)
               for i, v in enumerate(vals)]
        line = smooth_path(pts, sr.top(), sr.bottom())
        area = QPainterPath(line)
        area.lineTo(pts[-1].x(), sr.bottom())
        area.lineTo(pts[0].x(), sr.bottom())
        area.closeSubpath()
        g = QLinearGradient(0, sr.top(), 0, sr.bottom())
        g.setColorAt(0, rgba(col, 96))
        g.setColorAt(1, rgba(col, 0))
        p.fillPath(area, g)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rgba(col, 52), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(line)
        p.setPen(QPen(col, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(line)
        last = pts[-1]
        p.setPen(Qt.NoPen)
        p.setBrush(rgba(col, 60))
        p.drawEllipse(last, 6, 6)
        p.setBrush(col)
        p.drawEllipse(last, 3.2, 3.2)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.d.get("target"):
            self.clicked.emit(self.d["target"])


# ── sales trend ───────────────────────────────────────────────────────────────

class TrendChart(Glass):
    rangeChanged = Signal(int)

    def __init__(self, pal, currency="KES", parent=None):
        super().__init__(pal, "blue", parent)
        self.live = False  # static chart after intro draw — no per-frame pulse
        self.currency = currency
        self.rows, self.prev = [], []
        self.hover_i = -1
        self.range_idx = 1
        self._chips = []
        self.setMinimumHeight(330)

    def set_rows(self, rows, prev=None, animate=True):
        self.rows, self.prev = list(rows or []), list(prev or [])
        if animate:
            self.play(1300)
        else:
            self.update()

    def _plot(self, r):
        return QRectF(r.left() + 52, r.top() + 98, max(0, r.width() - 52 - 22), max(0, r.height() - 98 - 34))

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        pal = self.pal
        col = QColor(pal["blue"])
        rows = self.rows
        vals = [float(x[1]) for x in rows]
        total = sum(vals)
        ptotal = sum(float(v) for v in self.prev)
        days = (7, 30, 90)[self.range_idx]
        ch = pct_change(total, ptotal)
        self.header(p, r, "Sales trend", f"daily revenue · last {days} days")
        head = f"{self.currency} {total:,.0f}"
        p.setFont(self.font_(13, True))
        hw = QFontMetrics(p.font()).horizontalAdvance(head)
        self.text(p, QRectF(r.left() + 20, r.top() + 54, hw + 8, 22), head, 13, col, True, elide=False)
        if ch is not None:
            c = QColor(pal["green"] if ch >= 0 else pal["red"])
            s = f"{'▲' if ch >= 0 else '▼'} {abs(ch):.0f}% vs previous {days} days"
            self.text(p, QRectF(r.left() + 30 + hw, r.top() + 54, 260, 22), s, 8.5, c, True)
        self._chips = []
        x = r.right() - 16
        for i in (2, 1, 0):
            lab = ("7d", "30d", "90d")[i]
            cr = QRectF(x - 44, r.top() + 14, 44, 26)
            x -= 50
            self._chips.append((cr, i))
            on = i == self.range_idx
            p.setPen(Qt.NoPen)
            p.setBrush(col if on else rgba(pal["muted"], 26))
            p.drawRoundedRect(cr, 13, 13)
            self.text(p, cr, lab, 8.5, ideal_text(col) if on else pal["muted"], True, Qt.AlignCenter)

        pr = self._plot(r)
        if not rows or pr.width() < 40 or total <= 0:
            self.text(p, pr, "Sales will appear here as you trade.", 10, pal["muted"], False, Qt.AlignCenter)
            return
        n = len(rows)
        peak = nice_max(max(max(vals), max([float(v) for v in self.prev] or [0])))

        def X(i):
            return pr.left() + (i / max(1, n - 1)) * pr.width()

        def Y(v):
            return pr.bottom() - (float(v) / peak) * pr.height()

        for gi in range(5):
            y = pr.top() + gi * pr.height() / 4
            p.setPen(QPen(rgba(pal["muted"], 48), 1, Qt.DotLine))
            p.drawLine(QPointF(pr.left(), y), QPointF(pr.right(), y))
            self.text(p, QRectF(r.left() + 8, y - 8, 38, 16), compact(peak * (1 - gi / 4)), 8, pal["muted"], False, Qt.AlignRight | Qt.AlignVCenter)
        for k in range(5):
            i = int(round(k * (n - 1) / 4))
            try:
                lab = datetime.strptime(rows[i][0], "%Y-%m-%d").strftime("%d %b")
            except Exception:
                lab = rows[i][0]
            self.text(p, QRectF(X(i) - 30, pr.bottom() + 8, 60, 16), lab, 8, pal["muted"], False, Qt.AlignHCenter | Qt.AlignVCenter)

        p.save()
        p.setClipRect(QRectF(pr.left() - 6, r.top(), (pr.width() + 12) * self.intro, r.height()))
        if len(self.prev) == n:
            gp = [QPointF(X(i), Y(v)) for i, v in enumerate(self.prev)]
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(rgba(pal["muted"], 120), 1.5, Qt.DashLine))
            p.drawPath(smooth_path(gp, pr.top(), pr.bottom()))
        pts = [QPointF(X(i), Y(v)) for i, v in enumerate(vals)]
        line = smooth_path(pts, pr.top(), pr.bottom())
        area = QPainterPath(line)
        area.lineTo(pts[-1].x(), pr.bottom())
        area.lineTo(pts[0].x(), pr.bottom())
        area.closeSubpath()
        g = QLinearGradient(0, pr.top(), 0, pr.bottom())
        g.setColorAt(0, rgba(col, 120))
        g.setColorAt(0.7, rgba(col, 22))
        g.setColorAt(1, rgba(col, 0))
        p.fillPath(area, g)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rgba(col, 44), 9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(line)
        lg = QLinearGradient(pr.left(), 0, pr.right(), 0)
        lg.setColorAt(0, col)
        lg.setColorAt(1, mix(col, pal["cyan"], 0.55))
        p.setPen(QPen(QBrush(lg), 2.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(line)
        p.restore()

        pi = max(range(n), key=lambda i: vals[i])
        if vals[pi] > 0 and self.intro > 0.9:
            pp = QPointF(X(pi), Y(vals[pi]))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal["orange"]))
            p.drawEllipse(pp, 4, 4)
            self.text(p, QRectF(pp.x() - 40, pp.y() - 24, 80, 16), "peak", 8, pal["orange"], True, Qt.AlignCenter)

        if self.intro > 0.95:
            lp = pts[-1]
            ph = (now() * 1.2) % 1.0 if Motion.enabled else 0.0
            p.setPen(Qt.NoPen)
            p.setBrush(rgba(col, 90 * (1 - ph)))
            p.drawEllipse(lp, 4 + ph * 12, 4 + ph * 12)
            p.setBrush(QColor(pal["panel"]))
            p.drawEllipse(lp, 6, 6)
            p.setBrush(col)
            p.drawEllipse(lp, 4, 4)

        if 0 <= self.hover_i < n:
            i = self.hover_i
            hp = QPointF(X(i), Y(vals[i]))
            p.setPen(QPen(rgba(pal["muted"], 140), 1, Qt.DashLine))
            p.drawLine(QPointF(hp.x(), pr.top()), QPointF(hp.x(), pr.bottom()))
            p.setPen(Qt.NoPen)
            p.setBrush(rgba(col, 70))
            p.drawEllipse(hp, 10, 10)
            p.setBrush(QColor(pal["panel"]))
            p.drawEllipse(hp, 6.5, 6.5)
            p.setBrush(col)
            p.drawEllipse(hp, 4.2, 4.2)
            try:
                day = datetime.strptime(rows[i][0], "%Y-%m-%d").strftime("%a %d %b")
            except Exception:
                day = rows[i][0]
            lines = [day, f"Revenue  {vals[i]:,.0f}", f"Sales  {int(rows[i][2])}"]
            if len(self.prev) == n:
                lines.append(f"Before  {float(self.prev[i]):,.0f}")
            self.tooltip(p, hp, lines, r)

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        pos = e.position()
        over_chip = any(cr.contains(pos) for cr, _ in self._chips)
        self.setCursor(Qt.PointingHandCursor if over_chip else Qt.ArrowCursor)
        pr = self._plot(self.card_rect())
        n = len(self.rows)
        if n and pr.width() > 0 and pr.adjusted(-10, -10, 10, 10).contains(pos):
            self.hover_i = max(0, min(n - 1, int(round((pos.x() - pr.left()) / pr.width() * (n - 1)))))
        else:
            self.hover_i = -1
        self.update()

    def mousePressEvent(self, e):
        for cr, i in self._chips:
            if cr.contains(e.position()) and i != self.range_idx:
                self.range_idx = i
                self.rangeChanged.emit(i)
                self.update()
                return

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.hover_i = -1


# ── donut ─────────────────────────────────────────────────────────────────────

class Donut(Glass):
    def __init__(self, pal, currency="KES", parent=None):
        super().__init__(pal, "purple", parent)
        self.currency = currency
        self.data = []
        self.sel = -1
        self.setMinimumHeight(330)

    def set_data(self, data, animate=True):
        self.data = [(k, float(v)) for k, v in (data or []) if float(v) > 0]
        if animate:
            self.play(1200)
        else:
            self.update()

    def _geom(self, r):
        size = max(60.0, min(r.width() * 0.46, r.height() - 100))
        c = QPointF(r.left() + 24 + size / 2, r.top() + 64 + (r.height() - 64) / 2 - 6)
        return c, size / 2, 17.0

    def _hit(self, pos, r):
        if not self.data:
            return -1
        c, ro, th = self._geom(r)
        dx, dy = pos.x() - c.x(), pos.y() - c.y()
        dist = math.hypot(dx, dy)
        if not (ro - th - 8 <= dist <= ro + 8):
            return -1
        ang = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
        total = sum(v for _, v in self.data)
        acc = 0.0
        for i, (_, v) in enumerate(self.data):
            acc += v / total * 360
            if ang <= acc:
                return i
        return len(self.data) - 1

    def _legend_hit(self, pos, r):
        c, ro, _ = self._geom(r)
        x0 = c.x() + ro + 30
        y0 = max(r.top() + 70, c.y() - len(self.data) * 17)
        for i in range(len(self.data)):
            if QRectF(x0 - 6, y0 + i * 34 - 4, r.right() - x0, 32).contains(pos):
                return i
        return -1

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        pal = self.pal
        self.header(p, r, "Payment mix", "how customers paid · last 30 days")
        if not self.data:
            self.text(p, r.adjusted(0, 40, 0, 0), "Payment breakdown appears after sales.", 10, pal["muted"], False, Qt.AlignCenter)
            return
        c, ro, th = self._geom(r)
        total = sum(v for _, v in self.data)
        ring = QRectF(c.x() - ro + th / 2, c.y() - ro + th / 2, (ro - th / 2) * 2, (ro - th / 2) * 2)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rgba(pal["muted"], 28), th))
        p.drawEllipse(ring)
        start = 90.0
        gap = 3.0 if len(self.data) > 1 else 0.0
        for i, (label, val) in enumerate(self.data):
            span = val / total * 360 * self.intro
            col = QColor(pal[SERIES_KEYS[i % len(SERIES_KEYS)]])
            on = i == self.sel
            drawn = max(0.0, span - gap)
            if drawn <= 0.05:
                start -= span
                continue
            if on:
                p.setPen(QPen(rgba(col, 60), th + 12, Qt.SolidLine, Qt.FlatCap))
                p.drawArc(ring, int((start - gap / 2) * 16), int(-drawn * 16))
            p.setPen(QPen(col.lighter(112) if on else col, th + (4 if on else 0), Qt.SolidLine, Qt.FlatCap))
            p.drawArc(ring, int((start - gap / 2) * 16), int(-drawn * 16))
            start -= span
        if 0 <= self.sel < len(self.data):
            lab, val = self.data[self.sel]
            col = pal[SERIES_KEYS[self.sel % len(SERIES_KEYS)]]
            self.text(p, QRectF(c.x() - 60, c.y() - 28, 120, 20), lab, 9, col, True, Qt.AlignCenter)
            self.text(p, QRectF(c.x() - 60, c.y() - 10, 120, 24), compact(val), 15, pal["text"], True, Qt.AlignCenter)
            self.text(p, QRectF(c.x() - 60, c.y() + 14, 120, 16), f"{val / total * 100:.0f}% of total", 8, pal["muted"], False, Qt.AlignCenter)
        else:
            self.text(p, QRectF(c.x() - 60, c.y() - 22, 120, 26), compact(total * self.intro), 16, pal["text"], True, Qt.AlignCenter)
            self.text(p, QRectF(c.x() - 60, c.y() + 4, 120, 16), "collected", 8, pal["muted"], False, Qt.AlignCenter)
        x0 = c.x() + ro + 30
        y0 = max(r.top() + 70, c.y() - len(self.data) * 17)
        for i, (label, val) in enumerate(self.data):
            col = QColor(pal[SERIES_KEYS[i % len(SERIES_KEYS)]])
            y = y0 + i * 34
            if i == self.sel:
                p.setPen(Qt.NoPen)
                p.setBrush(rgba(col, 28))
                p.drawRoundedRect(QRectF(x0 - 8, y - 4, r.right() - x0 - 6, 32), 9, 9)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x0, y + 5, 11, 11), 3.5, 3.5)
            self.text(p, QRectF(x0 + 20, y - 1, max(10, r.right() - x0 - 30), 16), label, 9, pal["text"], True)
            self.text(p, QRectF(x0 + 20, y + 14, max(10, r.right() - x0 - 30), 14), f"{val:,.0f}  ·  {val / total * 100:.0f}%", 8, pal["muted"])

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        r = self.card_rect()
        i = self._hit(e.position(), r)
        if i < 0:
            i = self._legend_hit(e.position(), r)
        self.sel = i
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.sel = -1


# ── heat calendar ─────────────────────────────────────────────────────────────

class HeatCalendar(Glass):
    WEEKS = 13

    def __init__(self, pal, currency="KES", parent=None):
        super().__init__(pal, "green", parent)
        self.currency = currency
        self.days = {}
        self.cell_hover = None
        self.setMinimumHeight(268)

    def set_days(self, days, animate=True):
        self.days = dict(days or {})
        if animate:
            self.play(1500)
        else:
            self.update()

    def _layout(self, r):
        today = date.today()
        start = today - timedelta(days=(self.WEEKS - 1) * 7 + today.weekday())
        left, top = r.left() + 54, r.top() + 82
        aw, ah = r.right() - 20 - left, r.bottom() - 36 - top
        cw = max(8.0, min(aw / self.WEEKS - 4, 48))
        ch = max(8.0, min(ah / 7 - 4, 30))
        return start, left, top, (cw, ch), today

    def _cell_at(self, pos, r):
        start, left, top, (cw, ch), today = self._layout(r)
        col = int((pos.x() - left) // (cw + 4))
        row = int((pos.y() - top) // (ch + 4))
        if 0 <= col < self.WEEKS and 0 <= row < 7:
            d = start + timedelta(days=col * 7 + row)
            if d <= today:
                return d
        return None

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        pal = self.pal
        col = QColor(pal["green"])
        self.header(p, r, "Sales heat-map", "each square is a day — brighter means busier")
        start, left, top, (cw, ch), today = self._layout(r)
        peak = max(self.days.values()) if self.days else 0
        for ri, lab in enumerate(("Mon", "", "Wed", "", "Fri", "", "Sun")):
            if lab:
                self.text(p, QRectF(r.left() + 16, top + ri * (ch + 4), 34, ch), lab, 8, pal["muted"], False, Qt.AlignLeft | Qt.AlignVCenter)
        last_month = None
        for c in range(self.WEEKS):
            wk0 = start + timedelta(days=c * 7)
            if wk0.month != last_month:
                last_month = wk0.month
                self.text(p, QRectF(left + c * (cw + 4), top - 20, 40, 14), wk0.strftime("%b"), 8, pal["muted"], True, elide=False)
            ready = max(0.0, min(1.0, self.intro * (self.WEEKS + 5) - c))
            for rr in range(7):
                d = wk0 + timedelta(days=rr)
                if d > today:
                    continue
                v = self.days.get(d.isoformat(), 0.0)
                rect = QRectF(left + c * (cw + 4), top + rr * (ch + 4), cw, ch)
                lvl = (v / peak) if peak > 0 else 0
                if v <= 0:
                    fill = rgba(pal["muted"], 30)
                else:
                    fill = QColor(col)
                    fill.setAlpha(int(90 + 165 * lvl ** 0.75))
                fill.setAlpha(int(fill.alpha() * ready))
                sc = 0.55 + 0.45 * ready
                rect = QRectF(rect.center().x() - rect.width() * sc / 2, rect.center().y() - rect.height() * sc / 2, rect.width() * sc, rect.height() * sc)
                p.setPen(Qt.NoPen)
                if lvl > 0.7 and ready > 0.9:
                    p.setBrush(rgba(col, 40))
                    p.drawRoundedRect(rect.adjusted(-2.5, -2.5, 2.5, 2.5), 6, 6)
                p.setBrush(fill)
                p.drawRoundedRect(rect, 5, 5)
                if d == today:
                    ph = 0.5 + 0.5 * math.sin(now() * 3) if Motion.enabled else 1
                    p.setBrush(Qt.NoBrush)
                    p.setPen(QPen(rgba(pal["text"], 120 + 100 * ph), 1.6))
                    p.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 6, 6)
                if self.cell_hover == d:
                    p.setBrush(Qt.NoBrush)
                    p.setPen(QPen(QColor(pal["text"]), 1.6))
                    p.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 6, 6)
        ly = r.bottom() - 26
        self.text(p, QRectF(r.right() - 200, ly, 40, 14), "less", 8, pal["muted"], False, Qt.AlignRight | Qt.AlignVCenter)
        for i in range(5):
            f = QColor(col)
            f.setAlpha(int(90 + 165 * (i / 4.0)) if i else 40)
            p.setPen(Qt.NoPen)
            p.setBrush(f)
            p.drawRoundedRect(QRectF(r.right() - 152 + i * 18, ly + 1, 12, 12), 4, 4)
        self.text(p, QRectF(r.right() - 58, ly, 40, 14), "more", 8, pal["muted"], False, Qt.AlignLeft | Qt.AlignVCenter)
        if self.cell_hover is not None and self.mouse.x() >= 0:
            v = self.days.get(self.cell_hover.isoformat(), 0.0)
            self.tooltip(p, self.mouse, [self.cell_hover.strftime("%a %d %b %Y"), f"Revenue  {v:,.0f}" if v else "No sales"], r)

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        self.cell_hover = self._cell_at(e.position(), self.card_rect())
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.cell_hover = None


# ── ranking ───────────────────────────────────────────────────────────────────

class RankPanel(Glass):
    def __init__(self, pal, parent=None):
        super().__init__(pal, "cyan", parent)
        self.rows = []
        self.sel = -1
        self.setMinimumHeight(268)

    def set_rows(self, rows, animate=True):
        self.rows = list(rows or [])
        if animate:
            self.play(1200)
        else:
            self.update()

    def _row_h(self, r):
        return max(24.0, min(36.0, (r.height() - 84) / max(1, min(len(self.rows), 7))))

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        pal = self.pal
        self.header(p, r, "Top products", "best sellers · last 30 days · units")
        rows = self.rows[:7]
        if not rows:
            self.text(p, r.adjusted(0, 40, 0, 0), "Top products appear after sales.", 10, pal["muted"], False, Qt.AlignCenter)
            return
        peak = max(float(x.get("n") or 0) for x in rows) or 1
        top = r.top() + 66
        rh = self._row_h(r)
        for i, row in enumerate(rows):
            y = top + i * rh
            n = float(row.get("n") or 0)
            col = QColor(pal["cyan"] if i else pal["blue"])
            ready = max(0.0, min(1.0, self.intro * (len(rows) + 3) - i))
            if i == self.sel:
                p.setPen(Qt.NoPen)
                p.setBrush(rgba(col, 22))
                p.drawRoundedRect(QRectF(r.left() + 10, y, r.width() - 20, rh - 2), 9, 9)
            badge = QRectF(r.left() + 18, y + (rh - 22) / 2 - 1, 22, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(rgba(col, 40 if i else 70))
            p.drawRoundedRect(badge, 7, 7)
            self.text(p, badge, str(i + 1), 8.5, col, True, Qt.AlignCenter)
            lx = badge.right() + 10
            vw = 54
            bar_full = max(20.0, r.right() - lx - vw - 16)
            self.text(p, QRectF(lx, y, bar_full, rh * 0.52), str(row.get("label") or ""), 9, pal["text"], True, Qt.AlignLeft | Qt.AlignBottom)
            by = y + rh * 0.62
            p.setBrush(rgba(pal["muted"], 28))
            p.drawRoundedRect(QRectF(lx, by, bar_full, 5.0), 2.5, 2.5)
            bw = max(5.0, bar_full * (n / peak) * ready)
            bg = QLinearGradient(lx, 0, lx + bw, 0)
            bg.setColorAt(0, col)
            bg.setColorAt(1, mix(col, pal["purple"], 0.55))
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(lx, by, bw, 5.0), 2.5, 2.5)
            self.text(p, QRectF(r.right() - vw - 16, y, vw, rh), f"{n * ready:,.0f}", 9.5, pal["text"], True, Qt.AlignRight | Qt.AlignVCenter)

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        r = self.card_rect()
        i = int((e.position().y() - (r.top() + 66)) // max(1.0, self._row_h(r)))
        self.sel = i if 0 <= i < min(len(self.rows), 7) else -1
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.sel = -1


# ── attention list ────────────────────────────────────────────────────────────

class ActivityList(Glass):
    navigate = Signal(str)

    def __init__(self, pal, parent=None):
        super().__init__(pal, "orange", parent)
        self.rows = []
        self.sel = -1
        self.setMinimumHeight(310)

    def set_rows(self, rows, animate=True):
        self.rows = list(rows or [])
        if animate:
            self.play(900)
        else:
            self.update()

    def _rh(self, r):
        return max(34.0, min(46.0, (r.height() - 78) / max(1, min(len(self.rows), 7))))

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        pal = self.pal
        self.header(p, r, "Needs attention", "stock · credit · service — click to open")
        rows = self.rows[:7]
        if not rows:
            c = QColor(pal["green"])
            self.text(p, QRectF(r.left(), r.top() + 90, r.width(), 40), "✓", 26, c, True, Qt.AlignCenter)
            self.text(p, QRectF(r.left(), r.top() + 136, r.width(), 20), "All clear — nothing urgent right now.", 10, pal["muted"], False, Qt.AlignCenter)
            return
        top = r.top() + 62
        rh = self._rh(r)
        for i, row in enumerate(rows):
            ready = max(0.0, min(1.0, self.intro * (len(rows) + 2) - i))
            y = top + i * rh + (1 - ready) * 12
            col = QColor(pal[TONE.get(row.get("tone", "info"), "blue")])
            box = QRectF(r.left() + 12, y, r.width() - 24, rh - 4)
            p.setOpacity(self.appear * ready)
            p.setPen(Qt.NoPen)
            p.setBrush(rgba(col, 44 if i == self.sel else 24))
            p.drawRoundedRect(box, 11, 11)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(box.left(), box.top() + 8, 3.5, box.height() - 16), 1.7, 1.7)
            self.text(p, QRectF(box.left() + 10, box.top(), 30, box.height()), row.get("icon", "•"), 12, col, True, Qt.AlignCenter)
            self.text(p, QRectF(box.left() + 46, box.top() + 4, box.width() - 80, box.height() / 2 - 2), row.get("title", ""), 9.5, pal["text"], True, Qt.AlignLeft | Qt.AlignBottom)
            self.text(p, QRectF(box.left() + 46, box.top() + box.height() / 2 + 1, box.width() - 80, box.height() / 2 - 4), row.get("detail", ""), 8.5, pal["muted"], False, Qt.AlignLeft | Qt.AlignTop)
            if i == self.sel:
                self.text(p, QRectF(box.right() - 30, box.top(), 22, box.height()), "›", 15, col, True, Qt.AlignCenter)
        p.setOpacity(self.appear)

    def _idx(self, pos):
        r = self.card_rect()
        i = int((pos.y() - (r.top() + 62)) // max(1.0, self._rh(r)))
        return i if 0 <= i < min(len(self.rows), 7) and r.left() < pos.x() < r.right() else -1

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        self.sel = self._idx(e.position())
        self.setCursor(Qt.PointingHandCursor if self.sel >= 0 and self.rows[self.sel].get("target") else Qt.ArrowCursor)
        self.update()

    def mousePressEvent(self, e):
        i = self._idx(e.position())
        if e.button() == Qt.LeftButton and i >= 0 and self.rows[i].get("target"):
            self.navigate.emit(self.rows[i]["target"])

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.sel = -1


# ── insights ──────────────────────────────────────────────────────────────────

class InsightsPanel(Glass):
    navigate = Signal(str)

    def __init__(self, pal, parent=None):
        super().__init__(pal, "purple", parent)
        self.items = []
        self.sel = -1
        self.setMinimumHeight(310)

    def set_items(self, items, animate=True):
        self.items = list(items or [])
        if animate:
            self.play(1000)
        else:
            self.update()

    def _rh(self, r):
        return max(48.0, min(66.0, (r.height() - 78) / max(1, min(len(self.items), 5))))

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.begin(p)
        pal = self.pal
        self.header(p, r, "Smart insights", "what the system noticed for you")
        top = r.top() + 62
        rh = self._rh(r)
        for i, d in enumerate(self.items[:5]):
            ready = max(0.0, min(1.0, self.intro * 7 - i))
            y = top + i * rh + (1 - ready) * 10
            col = QColor(pal[TONE.get(d.get("level", "info"), "blue")])
            box = QRectF(r.left() + 12, y, r.width() - 24, rh - 4)
            p.setOpacity(self.appear * ready)
            p.setPen(Qt.NoPen)
            p.setBrush(rgba(col, 36 if i == self.sel else 16))
            p.drawRoundedRect(box, 12, 12)
            ic = QRectF(box.left() + 10, box.center().y() - 16, 32, 32)
            p.setBrush(rgba(col, 46))
            p.drawEllipse(ic)
            self.text(p, ic, d.get("icon", "✦"), 12, col, True, Qt.AlignCenter)
            tx = ic.right() + 12
            tw = max(40.0, box.right() - tx - 12)
            body = QRectF(tx, box.top() + 6, tw, box.height() - (22 if d.get("cta") else 12))
            p.setFont(self.font_(9.5, False))
            p.setPen(QColor(pal["text"]))
            p.drawText(body, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap, d.get("text", ""))
            if d.get("cta"):
                self.text(p, QRectF(tx, box.bottom() - 20, tw, 16), f"{d['cta']}  →", 8.5, col, True)
        p.setOpacity(self.appear)

    def _idx(self, pos):
        r = self.card_rect()
        i = int((pos.y() - (r.top() + 62)) // max(1.0, self._rh(r)))
        return i if 0 <= i < min(len(self.items), 5) and r.left() < pos.x() < r.right() else -1

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        self.sel = self._idx(e.position())
        self.setCursor(Qt.PointingHandCursor if self.sel >= 0 and self.items[self.sel].get("target") else Qt.ArrowCursor)
        self.update()

    def mousePressEvent(self, e):
        i = self._idx(e.position())
        if e.button() == Qt.LeftButton and i >= 0 and self.items[i].get("target"):
            self.navigate.emit(self.items[i]["target"])

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.sel = -1


# ── main dashboard view ───────────────────────────────────────────────────────

class DashboardView(QScrollArea):
    navigate = Signal(str)
    search = Signal(str)
    set_goal = Signal()

    def __init__(self, store, pal, dark=False, user_name="", currency="KES", parent=None):
        super().__init__(parent)
        self.store = store
        self.pal = pal
        self.dark = dark
        self.currency = currency
        self.user_name = user_name
        self.range_days = 30
        self._last_refresh = 0.0
        self._entered = False

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.aurora = AuroraViewport(pal)
        self.setViewport(self.aurora)
        body = QWidget()
        body.setObjectName("dashBody")
        body.setStyleSheet("QWidget#dashBody{background:transparent;}")
        self.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(6, 4, 8, 18)
        root.setSpacing(0)

        self.hero = Hero(pal, dark)
        first = (user_name or "").split(" ")[0]
        biz = store.setting("business_name", "Your Business").strip() or "Your Business"
        tag = store.setting("business_tagline", "") or store.setting("business_type", "General business")
        self.hero.set_identity(biz, first, tag, store.setting("business_logo_b64", ""))
        self.hero.searched.connect(self.search.emit)
        self.hero.goalClicked.connect(self.set_goal.emit)
        root.addWidget(self.hero)

        self.tiles_grid = QGridLayout()
        self.tiles_grid.setSpacing(0)
        defs = [("▣", "Point of Sale", "Point of Sale", "blue", "Open the till"),
                ("↗", "Sales", "Sales", "green", "Invoices & quotes"),
                ("⬡", "Inventory", "Inventory", "purple", "Stock & products"),
                ("⚒", "Repairs", "Repair Tickets", "orange", "Service jobs"),
                ("▤", "Reports", "Reports", "cyan", "Business reports"),
                ("◎", "Customers", "CRM", "pink", "Customer list")]
        self.tiles = []
        for i, (g, t, tg, c, h) in enumerate(defs):
            tile = Tile(pal, g, t, tg, c, h)
            tile.clicked.connect(self.navigate.emit)
            self.tiles.append(tile)
        root.addLayout(self.tiles_grid)

        kpi_defs = [
            ("today", "◈", "Today", "blue", True, "Sales"),
            ("week", "▣", "This week", "green", True, "Sales"),
            ("products", "⬡", "Products", "purple", False, "Inventory"),
            ("low", "⚠", "Low stock", "orange", False, "Inventory"),
            ("credit", "◷", "On credit", "pink", True, "Sales"),
        ]
        self.kpi_grid = QGridLayout()
        self.kpi_grid.setSpacing(0)
        self.kpis = {}
        for key, glyph, label, color, money, target in kpi_defs:
            card = KpiCard(pal, {"key": key, "glyph": glyph, "label": label, "color": color, "money": money,
                                 "value": 0, "sub": "", "series": [], "target": target}, currency)
            card.clicked.connect(self.navigate.emit)
            self.kpis[key] = card
        root.addLayout(self.kpi_grid)

        self.chart = TrendChart(pal, currency)
        self.chart.rangeChanged.connect(self._on_range)
        self.donut = Donut(pal, currency)
        self.heat = HeatCalendar(pal, currency)
        self.rank = RankPanel(pal)
        self.activity = ActivityList(pal)
        self.activity.navigate.connect(self.navigate.emit)
        self.insights = InsightsPanel(pal)
        self.insights.navigate.connect(self.navigate.emit)
        self.pair_grids = []
        for a, b, wa, wb in ((self.chart, self.donut, 3, 2), (self.heat, self.rank, 3, 2), (self.activity, self.insights, 2, 3)):
            g = QGridLayout()
            g.setSpacing(0)
            self.pair_grids.append((g, a, b, wa, wb))
            root.addLayout(g)
        root.addStretch(1)
        self._mode = None
        self._reflow(self.width() or 1300)

        self._animated = [self.hero, *self.tiles, *self.kpis.values(), self.chart, self.donut, self.heat, self.rank,
                          self.activity, self.insights]
        if Motion.enabled:
            for w in self._animated:
                w.appear = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(lambda: self.refresh(animate=False))
        QTimer.singleShot(30, self.refresh)

    # ── responsive layout ─────────────────────────────────────────────────
    @staticmethod
    def _clear(grid):
        while grid.count():
            grid.takeAt(0)
        for i in range(12):
            grid.setColumnStretch(i, 0)

    def _reflow(self, width):
        mode = "wide" if width >= 1120 else ("mid" if width >= 760 else "narrow")
        if mode == self._mode:
            return
        self._mode = mode
        tcols = {"wide": 6, "mid": 3, "narrow": 2}[mode]
        kcols = {"wide": 5, "mid": 3, "narrow": 2}[mode]
        for grid, items, cols in ((self.tiles_grid, self.tiles, tcols), (self.kpi_grid, list(self.kpis.values()), kcols)):
            self._clear(grid)
            for i, w in enumerate(items):
                grid.addWidget(w, i // cols, i % cols)
            for c in range(cols):
                grid.setColumnStretch(c, 1)
        for g, a, b, wa, wb in self.pair_grids:
            self._clear(g)
            if mode == "wide":
                g.addWidget(a, 0, 0)
                g.addWidget(b, 0, 1)
                g.setColumnStretch(0, wa)
                g.setColumnStretch(1, wb)
            else:
                g.addWidget(a, 0, 0)
                g.addWidget(b, 1, 0)
                g.setColumnStretch(0, 1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow(self.viewport().width())

    # ── palette (theme switch) ────────────────────────────────────────────
    def apply_palette(self, pal, dark=None):
        """Swap colours in place. No rebuild, no intro replay — this is what makes theme changes seamless."""
        self.pal = pal
        if dark is not None:
            self.dark = dark
        self.aurora.pal = pal
        glass = self.findChildren(Glass)
        for w in glass:
            w.pal = pal
        self.hero.restyle()
        self.aurora.update()
        for w in glass:
            w.update()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def showEvent(self, e):
        super().showEvent(e)
        self.aurora.start()
        self._timer.start()
        if not self._entered:
            self._entered = True
            if Motion.enabled:
                for i, w in enumerate(self._animated):
                    w.enter(delay=min(30 + i * 25, 150), ms=350)
            else:
                for w in self._animated:
                    w.appear = 1.0
                    w.update()
        elif now() - self._last_refresh > 15:
            QTimer.singleShot(0, lambda: self.refresh(animate=False))

    def hideEvent(self, e):
        super().hideEvent(e)
        self.aurora.stop()
        self._timer.stop()

    # ── data ──────────────────────────────────────────────────────────────
    def _ops(self):
        return self.store.operations

    def _pulse(self):
        return PulseService(self.store.operations.conn, self.store.setting, self.store.set_setting, self.currency)

    def refresh(self, animate=True):
        self._last_refresh = now()
        try:
            pulse = self._pulse().compute()
        except Exception:
            pulse = None
        self.hero.set_pulse(pulse, self.currency)
        self._fill_kpis(pulse, animate)
        self._fill_chart(animate)
        self._fill_donut(animate)
        self._fill_heat(animate)
        self._fill_rank(animate)
        self._fill_activity(pulse, animate)
        self._fill_insights(pulse, animate)

    def _on_range(self, idx):
        self.range_days = (7, 30, 90)[idx]
        self._fill_chart(True)

    def _fill_kpis(self, pulse, animate):
        series = [float(v) for _, v in (pulse.series if pulse else [])]
        try:
            products = self._ops().product_count(active_only=True)
        except Exception:
            products = 0
        goal_txt = fmt_money(pulse.goal, self.currency) if pulse and pulse.goal else "—"
        vals = {
            "today": dict(value=float(pulse.today) if pulse else 0, series=series[-14:],
                          delta=float(pulse.today - pulse.yesterday) if pulse else 0,
                          sub=f"{pulse.today_count if pulse else 0} sales · goal {goal_txt}"),
            "week": dict(value=float(pulse.week_total) if pulse else 0, series=series[-7:], delta=None,
                         sub="Completed sales revenue"),
            "products": dict(value=products, series=[], delta=None, sub="Active catalogue items"),
            "low": dict(value=int(pulse.low_stock) if pulse else 0, series=[], delta=None, sub="At or below reorder"),
            "credit": dict(value=float(pulse.unpaid_total) if pulse else 0, series=[], delta=None,
                           sub=f"{pulse.unpaid_count if pulse else 0} open balances"),
        }
        for i, (k, card) in enumerate(self.kpis.items()):
            d = dict(card.d)
            d.update(vals[k])
            card.apply(d, animate=animate, delay=120 + i * 70)

    def _series(self, start, end):
        rows = self._ops().conn.execute(
            """SELECT substr(created_at,1,10) AS d, SUM(CAST(total AS REAL)) AS revenue, COUNT(*) AS cnt
               FROM sales WHERE status='Completed' AND substr(created_at,1,10) >= ? AND substr(created_at,1,10) <= ?
               GROUP BY d ORDER BY d""", (start.isoformat(), end.isoformat())).fetchall()
        by = {r["d"]: (float(r["revenue"] or 0), int(r["cnt"] or 0)) for r in rows}
        out, d = [], start
        while d <= end:
            rev, cnt = by.get(d.isoformat(), (0.0, 0))
            out.append((d.isoformat(), rev, cnt))
            d += timedelta(days=1)
        return out

    def _fill_chart(self, animate=True):
        n = self.range_days
        end = date.today()
        start = end - timedelta(days=n - 1)
        try:
            cur = self._series(start, end)
            prev = [x[1] for x in self._series(start - timedelta(days=n), start - timedelta(days=1))]
        except Exception:
            cur, prev = [], []
        self.chart.set_rows(cur, prev, animate)

    def _fill_donut(self, animate=True):
        since = (date.today() - timedelta(days=29)).isoformat()
        try:
            rows = self._ops().conn.execute(
                """SELECT COALESCE(NULLIF(payment_method,''),'Cash') AS m, SUM(CAST(paid_total AS REAL)) AS amt
                   FROM sales WHERE status='Completed' AND substr(created_at,1,10) >= ?
                   GROUP BY m ORDER BY amt DESC LIMIT 6""", (since,)).fetchall()
            data = [(r["m"], float(r["amt"] or 0)) for r in rows if float(r["amt"] or 0) > 0]
        except Exception:
            data = []
        self.donut.set_data(data, animate)

    def _fill_heat(self, animate=True):
        since = (date.today() - timedelta(days=100)).isoformat()
        try:
            rows = self._ops().conn.execute(
                """SELECT substr(created_at,1,10) AS d, SUM(CAST(total AS REAL)) AS revenue FROM sales
                   WHERE status='Completed' AND substr(created_at,1,10) >= ? GROUP BY d""", (since,)).fetchall()
            days = {r["d"]: float(r["revenue"] or 0) for r in rows}
        except Exception:
            days = {}
        self.heat.set_days(days, animate)

    def _fill_rank(self, animate=True):
        since = (date.today() - timedelta(days=29)).isoformat()
        try:
            rows = self._ops().conn.execute(
                """SELECT sl.description AS name, SUM(CAST(sl.quantity AS REAL)) AS qty FROM sale_lines sl
                   JOIN sales s ON s.id = sl.sale_id WHERE s.status='Completed' AND substr(s.created_at,1,10) >= ?
                   GROUP BY sl.description ORDER BY qty DESC LIMIT 8""", (since,)).fetchall()
            data = [{"label": r["name"], "n": float(r["qty"] or 0)} for r in rows]
        except Exception:
            data = []
        self.rank.set_rows(data, animate)

    def _fill_activity(self, pulse, animate=True):
        rows = []
        try:
            for p in self._ops().low_stock_products(5)[:3]:
                rows.append({"icon": "⚠", "title": p["name"], "detail": f"SKU {p['sku']} · on hand {p['stock_on_hand']}",
                             "tone": "warn", "target": "Inventory"})
        except Exception:
            pass
        if pulse and pulse.unpaid_count:
            rows.append({"icon": "◷", "title": f"{pulse.unpaid_count} credit balances",
                         "detail": f"{fmt_money(pulse.unpaid_total, self.currency)} outstanding", "tone": "bad", "target": "Sales"})
        try:
            jobs = [j for j in self._ops().repair_jobs(50) if j["status"] not in {"Collected", "Cancelled"}]
            for j in jobs[:3]:
                rows.append({"icon": "⚒", "title": j["customer_name"], "detail": f"{j['device']} · {j['status']}",
                             "tone": "info", "target": "Repair Tickets"})
        except Exception:
            pass
        self.activity.set_rows(rows, animate)

    def _fill_insights(self, pulse, animate=True):
        items = []
        if pulse:
            for text in (pulse.insights or [])[:3]:
                items.append({"icon": "✦", "text": text, "level": "info", "target": "", "cta": ""})
            if pulse.low_stock:
                items.append({"icon": "⚠", "text": f"{pulse.low_stock} products are at or below reorder level.",
                              "level": "warn", "target": "Inventory", "cta": "Open inventory"})
            if pulse.unpaid_count:
                items.append({"icon": "◷", "text": f"{pulse.unpaid_count} customers still have open balances ({fmt_money(pulse.unpaid_total, self.currency)}).",
                              "level": "bad", "target": "Sales", "cta": "Review credit sales"})
            if pulse.goal and pulse.goal_progress >= 1:
                items.append({"icon": "★", "text": "Daily sales goal reached — well done.", "level": "good", "target": "", "cta": ""})
        if not items:
            items.append({"icon": "◈", "text": "Start selling to unlock live insights, streaks and goals.",
                          "level": "info", "target": "Point of Sale", "cta": "Open POS"})
        self.insights.set_items(items, animate)
