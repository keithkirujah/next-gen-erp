"""NextGen Library — motion toolkit.

Everything that moves lives here so the rest of the app stays readable:
  * Motion            global on/off switch ("Reduce motion" setting)
  * run_anim          safe wrapper around QVariantAnimation
  * AnimatedNumber    label that counts up/down to a value
  * reveal / stagger  fade-in (optionally staggered) for cards and panels
  * PageTransition    slide + fade between pages (snapshot based, no live effects)
  * ThemeRipple       circular reveal when switching light <-> dark
  * SlidingPill       the active-page highlight that glides between sidebar buttons
  * GlobalFx          app-wide hover glow, focus glow, dialog fade-in
  * ToastHost         non-blocking notifications (replaces short "OK" pop-ups)
"""
from __future__ import annotations

import math
import sys

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, QRectF, Qt, QTimer,
                            QVariantAnimation)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QDialog, QFrame, QGraphicsDropShadowEffect,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QWidget)


class Motion:
    """Global motion switches — defaults tuned for low-end machines.

    * enabled     — one-shot entrance fades, KPI count-ups, page transitions
    * continuous  — looping pulses / live dashboard ticks (off by default)
    * effects     — QGraphicsDropShadow hover glows (off by default; costly on weak GPUs)
    """
    enabled = True
    continuous = False
    effects = False


_live: set = set()          # keeps running animations alive until they finish


def run_anim(start, end, ms, on_value, curve=QEasingCurve.OutCubic, on_done=None, delay=0, owner=None):
    """Animate start->end calling on_value(v). Instant when motion is disabled. Never raises on dead widgets."""
    def safe(fn, *a):
        try:
            fn(*a)
        except RuntimeError:        # the widget was deleted while we were animating
            pass

    if not Motion.enabled or ms <= 0:
        safe(on_value, end)
        if on_done:
            safe(on_done)
        return None
    a = QVariantAnimation(owner)
    a.setStartValue(start)
    a.setEndValue(end)
    a.setDuration(int(ms))
    a.setEasingCurve(curve)
    a.valueChanged.connect(lambda v: safe(on_value, v))

    def fin():
        _live.discard(a)
        if on_done:
            safe(on_done)
    a.finished.connect(fin)
    _live.add(a)

    def go():
        try:
            a.start()
        except RuntimeError:
            _live.discard(a)
    if delay:
        QTimer.singleShot(int(delay), go)
    else:
        go()
    return a


def ease(name):
    return {"out": QEasingCurve.OutCubic, "inout": QEasingCurve.InOutCubic, "back": QEasingCurve.OutBack,
            "expo": QEasingCurve.OutExpo, "quad": QEasingCurve.OutQuad}[name]


# ----------------------------------------------------------------------------- numbers
class AnimatedNumber(QLabel):
    """A label whose text counts smoothly to the value you give it."""
    def __init__(self, value=0, fmt=None, parent=None):
        super().__init__(parent)
        self._v = 0.0
        self._fmt = fmt or (lambda v: f"{int(round(v)):,}")
        self._anim = None
        self.setText(self._fmt(0))
        if value:
            self.set_value(value, animate=False)

    def set_value(self, v, ms=450, animate=True, delay=0):
        v = float(v or 0)
        if self._anim is not None:
            try:
                self._anim.stop()
            except RuntimeError:
                pass
        if animate and Motion.enabled and abs(v - self._v) >= 0.5:
            self._anim = run_anim(self._v, v, min(int(ms), 500), self._tick, ease("out"), delay=delay, owner=self)
        else:
            self._tick(v)

    def _tick(self, x):
        self._v = float(x)
        self.setText(self._fmt(self._v))


# ----------------------------------------------------------------------------- reveals
def reveal(widget, delay=0, ms=460):
    """Fade a widget in (then drop the effect so it costs nothing afterwards)."""
    if not Motion.enabled or widget.graphicsEffect() is not None:
        return
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)

    def done():
        if widget.graphicsEffect() is eff:
            widget.setGraphicsEffect(None)
    run_anim(0.0, 1.0, ms, eff.setOpacity, ease("out"), done, delay, owner=eff)


def stagger(widgets, base=0, step=55, ms=460):
    for i, w in enumerate(widgets):
        reveal(w, base + i * step, ms)


# ----------------------------------------------------------------------------- page transition
class PageTransition(QWidget):
    """Slide-up + fade for page changes. Paints a snapshot of the new page over it, so the page itself is untouched."""
    def __init__(self, host):
        super().__init__(host)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()
        self._pix, self._t, self._bg, self._dy, self._anim = None, 1.0, QColor("#000"), 0, None

    def play(self, area, bg, ms=340, dy=22):
        """area: the widget (stack) whose current page just changed."""
        if not Motion.enabled or not area.isVisible():
            return
        self._pix = area.grab()
        self._bg, self._dy, self._t = QColor(bg), dy, 0.0
        self.setParent(area.parentWidget())
        self.setGeometry(area.geometry())
        self.raise_()
        self.show()
        if self._anim:
            self._anim.stop()
        self._anim = run_anim(0.0, 1.0, ms, self._set, ease("out"), self._finish, owner=self)

    def _set(self, t):
        self._t = float(t)
        self.update()

    def _finish(self):
        self.hide()
        self._pix = None

    def paintEvent(self, e):
        if not self._pix:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), self._bg)
        p.setOpacity(min(1.0, self._t * 1.4))
        p.drawPixmap(0, int((1.0 - self._t) * self._dy), self._pix)


# ----------------------------------------------------------------------------- theme ripple
class ThemeRipple(QWidget):
    """Shows the OLD look with a circular hole that grows from `origin`, revealing the new theme underneath.

    Create with autostart=False, paint it, change the theme behind it, then call start(): the window
    never shows a half-themed frame."""
    def __init__(self, window, snapshot, origin, ms=720, autostart=True):
        super().__init__(window)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._snap, self._o, self._r, self._ms = snapshot, origin, 0.0, ms
        self._started = False
        self.setGeometry(window.rect())
        self.raise_()
        self.show()
        w, h = window.width(), window.height()
        self._far = max(math.hypot(origin.x() - x, origin.y() - y) for x in (0, w) for y in (0, h))
        if autostart:
            self.start()

    def start(self):
        if self._started:
            return
        self._started = True
        try:
            self.setGeometry(self.parentWidget().rect())
        except RuntimeError:
            return
        run_anim(0.0, self._far + 40, self._ms, self._set, ease("inout"), self.deleteLater, owner=self)

    def _set(self, r):
        self._r = float(r)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        full = QPainterPath()
        full.addRect(QRectF(self.rect()))
        hole = QPainterPath()
        hole.addEllipse(QRectF(self._o.x() - self._r, self._o.y() - self._r, self._r * 2, self._r * 2))
        p.setClipPath(full.subtracted(hole))
        p.drawPixmap(0, 0, self._snap)
        if self._r > 2:
            # soft glowing rim along the expanding edge
            p.setClipping(False)
            p.setBrush(Qt.NoBrush)
            for i, a in enumerate((16, 30, 70)):
                p.setPen(QPen(QColor(255, 255, 255, a), 7 - i * 2.5))
                p.drawEllipse(QRectF(self._o.x() - self._r, self._o.y() - self._r, self._r * 2, self._r * 2))


# ----------------------------------------------------------------------------- sliding nav pill
class SlidingPill(QFrame):
    """Highlight that glides to whichever sidebar button is active."""
    def __init__(self, sidebar):
        super().__init__(sidebar)
        self.setObjectName("navPill")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._target, self._anim = None, None
        sidebar.installEventFilter(self)
        self.lower()

    def move_to(self, btn, animate=True):
        self._target = btn
        rect = btn.geometry()
        if rect.width() < 10:           # layout not ready yet
            QTimer.singleShot(0, lambda: self.snap())
            return
        if not self.isVisible():
            self.setGeometry(rect)
            self.show()
            self.lower()
            return
        if animate and Motion.enabled:
            if self._anim:
                self._anim.stop()
            self._anim = QPropertyAnimation(self, b"geometry", self)
            self._anim.setDuration(380)
            self._anim.setStartValue(self.geometry())
            self._anim.setEndValue(rect)
            self._anim.setEasingCurve(QEasingCurve.OutBack)
            self._anim.start()
        else:
            self.setGeometry(rect)

    def snap(self):
        if self._target is not None:
            try:
                self.setGeometry(self._target.geometry())
                self.show()
                self.lower()
            except RuntimeError:
                pass

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Resize, QEvent.LayoutRequest) and (self._anim is None or self._anim.state() != QPropertyAnimation.Running):
            self.snap()
        return False


# ----------------------------------------------------------------------------- app-wide effects
class GlobalFx(QObject):
    """Installed on the QApplication: hover glow on buttons, focus glow on inputs, fade-in for dialogs."""
    SKIP = {"navBtn", "collapseBtn", "toastClose"}

    def __init__(self, palette_getter):
        super().__init__()
        self.pal = palette_getter

    def _glow(self, w, on, color_key="blue", blur=26, alpha=110):
        if not Motion.enabled or not Motion.effects:
            return
        eff = w.graphicsEffect()
        if on:
            if eff is not None and not isinstance(eff, QGraphicsDropShadowEffect):
                return                      # something else (a reveal) owns the effect right now
            if eff is None:
                eff = QGraphicsDropShadowEffect(w)
                c = QColor(self.pal()[color_key])
                c.setAlpha(0)
                eff.setColor(c)
                eff.setBlurRadius(0)
                eff.setOffset(0, 3)
                w.setGraphicsEffect(eff)
            base = QColor(self.pal()[color_key])

            def step(v):
                c = QColor(base)
                c.setAlpha(int(alpha * v))
                eff.setColor(c)
                eff.setBlurRadius(blur * v)
            run_anim(0.0, 1.0, 220, step, ease("out"), owner=eff)
        else:
            if not isinstance(eff, QGraphicsDropShadowEffect):
                return
            base = QColor(eff.color())
            a0 = base.alpha()

            def step(v):
                c = QColor(base)
                c.setAlpha(int(a0 * (1 - v)))
                eff.setColor(c)
                eff.setBlurRadius(eff.blurRadius() * (1 - v * 0.5))

            def done():
                try:
                    if w.graphicsEffect() is eff:
                        w.setGraphicsEffect(None)
                except RuntimeError:
                    pass
            run_anim(0.0, 1.0, 260, step, ease("out"), done, owner=eff)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t not in (QEvent.Enter, QEvent.Leave, QEvent.FocusIn, QEvent.FocusOut, QEvent.Show):
            return False
        try:
            if isinstance(obj, QPushButton):
                if obj.objectName() not in self.SKIP and obj.isEnabled():
                    if t == QEvent.Enter:
                        self._glow(obj, True, "blue" if obj.objectName() != "danger" else "red")
                    elif t == QEvent.Leave:
                        self._glow(obj, False)
            elif isinstance(obj, QLineEdit) and not isinstance(obj.parent(), (QAbstractSpinBox, QComboBox)):
                if t == QEvent.FocusIn:
                    self._glow(obj, True, "blue", blur=22, alpha=95)
                elif t == QEvent.FocusOut:
                    self._glow(obj, False)
            elif obj.property("fxLift"):
                if t == QEvent.Enter:
                    self._glow(obj, True, obj.property("fxColor") or "blue", blur=34, alpha=90)
                elif t == QEvent.Leave:
                    self._glow(obj, False)
            elif t == QEvent.Show and isinstance(obj, QDialog) and obj.isWindow() and Motion.enabled:
                obj.setWindowOpacity(0.0)
                run_anim(0.0, 1.0, 170, obj.setWindowOpacity, ease("out"), owner=obj)
        except RuntimeError:
            pass
        return False


# ----------------------------------------------------------------------------- toasts
class Toast(QFrame):
    ICONS = {"success": "✔", "info": "ℹ", "warn": "!", "error": "✖"}

    def __init__(self, parent, text, kind, pal):
        super().__init__(parent)
        self.setObjectName("toast")
        color = {"success": pal["green"], "info": pal["blue"], "warn": pal["orange"], "error": pal["red"]}[kind]
        self.setStyleSheet(
            f"QFrame#toast {{ background:{pal['panel']}; border:1px solid {pal['border']}; border-left:4px solid {color}; border-radius:12px; }}"
            f"QLabel {{ background:transparent; color:{pal['text']}; font-size:10.5pt; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(10)
        ic = QLabel(self.ICONS[kind])
        ic.setStyleSheet(f"QLabel {{ color:{color}; font-weight:900; font-size:13pt; }}")
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setMaximumWidth(360)
        lay.addWidget(ic)
        lay.addWidget(lb, 1)
        self.setCursor(Qt.PointingHandCursor)
        self.adjustSize()

    def mousePressEvent(self, e):
        self.dismiss()

    def dismiss(self):
        self.hide()
        self.deleteLater()


class ToastHost(QObject):
    def __init__(self, window, palette_getter):
        super().__init__(window)
        self.win, self.pal, self.items = window, palette_getter, []
        window.installEventFilter(self)

    def show(self, text, kind="success", ms=3400):
        t = Toast(self.win, text, kind, self.pal())
        t.adjustSize()
        t.show()
        t.raise_()
        self.items.append(t)
        self._layout(animate_new=t)
        QTimer.singleShot(ms, lambda: self._expire(t))

    def _expire(self, t):
        try:
            if t not in self.items:
                return
            self.items.remove(t)
            eff = QGraphicsOpacityEffect(t)
            t.setGraphicsEffect(eff)
            run_anim(1.0, 0.0, 260, eff.setOpacity, ease("out"), t.dismiss, owner=eff)
            self._layout()
        except RuntimeError:
            pass

    def _layout(self, animate_new=None):
        w, h = self.win.width(), self.win.height()
        y = h - 52
        for t in reversed(self.items):
            try:
                y -= t.height() + 10
                target = QPoint(w - t.width() - 22, y)
                if t is animate_new and Motion.enabled:
                    t.move(target + QPoint(60, 0))
                    eff = QGraphicsOpacityEffect(t)
                    t.setGraphicsEffect(eff)
                    start = t.pos()

                    def step(v, t=t, eff=eff, start=start, target=target):
                        eff.setOpacity(float(v))
                        t.move(int(start.x() + (target.x() - start.x()) * v), target.y())

                    def done(t=t, eff=eff):
                        if t.graphicsEffect() is eff:
                            t.setGraphicsEffect(None)
                    run_anim(0.0, 1.0, 380, step, ease("back"), done, owner=eff)
                else:
                    t.move(target)
            except RuntimeError:
                pass

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize:
            self._layout()
        return False


def install_toast_patch(host_getter):
    """Short, harmless QMessageBox.information() pop-ups become toasts; long ones stay modal so nothing is lost."""
    original = QMessageBox.information

    def info(parent, title, text, *args, **kwargs):
        host = host_getter()
        if host is not None and len(str(text)) <= 120 and str(text).count("\n") <= 1:
            host.show(f"{title} — {text}" if title and title.lower() not in str(text).lower() else str(text), "success")
            return QMessageBox.Ok
        return original(parent, title, text, *args, **kwargs)
    QMessageBox.information = staticmethod(info)
    return original


# ----------------------------------------------------------------------------- windows dark title bar
def set_dark_titlebar(widget, dark):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):           # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+/Win11), older id as fallback
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def set_window_backdrop(widget, mode: str):
    """Best-effort Windows 11 Mica/Acrylic backdrop for glass/dark themes."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        # DWMWA_SYSTEMBACKDROP_TYPE = 38
        # 0=auto, 1=none, 2=Mica, 3=Acrylic, 4=Mica Alt
        backdrop = 0
        if mode == "glass":
            backdrop = 3  # Acrylic — closer to frosted glass
        elif mode == "dark":
            backdrop = 2  # Mica
        else:
            backdrop = 1  # None / solid
        val = ctypes.c_int(backdrop)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass

