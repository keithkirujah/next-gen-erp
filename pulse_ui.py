"""Qt widgets for the Business Pulse dashboard card."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from insights import Pulse, money


class Sparkline(QWidget):
    """Last-14-days bars; today highlighted; hover shows the day's total."""

    def __init__(self, series, palette, currency="KES", parent=None):
        super().__init__(parent)
        self.series, self.palette, self.currency = series, palette, currency
        self.setMinimumSize(160, 90)
        self.setMouseTracking(True)
        self._hover = -1
        self._reveal = 0.0
        self._reveal_animation = QVariantAnimation(self)
        self._reveal_animation.setDuration(850)
        self._reveal_animation.setStartValue(0.0)
        self._reveal_animation.setEndValue(1.0)
        self._reveal_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._reveal_animation.valueChanged.connect(self._set_reveal)
        self._reveal_animation.start()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _set_reveal(self, value):
        self._reveal = float(value)
        self.update()

    def _geometry(self):
        n = max(1, len(self.series))
        w = self.width() - 8
        gap = 4
        bar = max(4, (w - gap * (n - 1)) / n)
        return bar, gap

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        peak = max([float(v) for _, v in self.series] + [1.0])
        bar, gap = self._geometry()
        h = self.height() - 18
        for i, (day, value) in enumerate(self.series):
            if value > 0:
                reveal = min(1.0, max(0.0, (self._reveal - i * 0.035) / 0.55))
                height = max(3.0, float(value) / peak * (h - 4)) * reveal
            else:
                height = 3.0
            x = 4 + i * (bar + gap)
            today = i == len(self.series) - 1
            colour = QColor(self.palette["cyan"] if today else self.palette["blue"])
            if value <= 0:
                colour = QColor(self.palette["border"])
            if i == self._hover:
                colour = colour.lighter(130)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(QRectF(x, h - height, bar, height), 3, 3)
        p.setPen(QColor(self.palette["muted"]))
        f = QFont(); f.setPointSize(7); p.setFont(f)
        p.drawText(QRectF(0, h + 2, self.width(), 14), Qt.AlignmentFlag.AlignLeft, self.series[0][0][5:] if self.series else "")
        p.drawText(QRectF(0, h + 2, self.width(), 14), Qt.AlignmentFlag.AlignRight, "today")

    def mouseMoveEvent(self, e):
        bar, gap = self._geometry()
        i = int((e.position().x() - 4) // (bar + gap))
        if 0 <= i < len(self.series):
            self._hover = i
            day, value = self.series[i]
            self.setToolTip(f"{day}: {money(value, self.currency)}")
            self.update()

    def leaveEvent(self, _):
        self._hover = -1
        self.update()


class ProgressRing(QWidget):
    clicked = Signal()

    def __init__(self, progress, label, palette, parent=None):
        super().__init__(parent)
        self.progress, self.label, self.palette = 0.0, label, palette
        self.target_progress = min(max(float(progress), 0.0), 1.0)
        self.setFixedSize(112, 112)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to set your daily sales goal")
        if self.target_progress > 0:
            self._progress_animation = QVariantAnimation(self)
            self._progress_animation.setDuration(720)
            self._progress_animation.setStartValue(0.0)
            self._progress_animation.setEndValue(self.target_progress)
            self._progress_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._progress_animation.valueChanged.connect(self._set_progress)
            self._progress_animation.start()

    def _set_progress(self, value):
        self.progress = float(value)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(10, 10, self.width() - 20, self.height() - 20)
        pen = QPen(QColor(self.palette["border"]), 10); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.drawArc(rect, 0, 360 * 16)
        done = self.progress >= 1.0
        pen.setColor(QColor(self.palette["green"] if done else self.palette["cyan"]))
        p.setPen(pen)
        p.drawArc(rect, 90 * 16, -int(min(self.progress, 1.0) * 360 * 16))
        p.setPen(QColor(self.palette["text"]))
        f = QFont(); f.setPointSize(15); f.setBold(True); p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.label)

    def mousePressEvent(self, _):
        self.clicked.emit()


class PulsePanel(QFrame):
    """The card at the top of the dashboard."""
    set_goal_requested = Signal()
    open_page = Signal(str)

    def __init__(self, pulse: Pulse, greeting: str, palette: dict, currency: str, parent=None):
        super().__init__(parent)
        self.pulse, self.palette, self.currency = pulse, palette, currency
        self.setObjectName("panelAlt")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(22, 18, 22, 18)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(14)
        self.left_w, self.mid_w, self.right_w = QWidget(), QWidget(), QWidget()
        for w in (self.left_w, self.mid_w, self.right_w):
            w.setStyleSheet("background:transparent;")
        self._narrow = None

        left = QVBoxLayout(self.left_w)
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(QLabel(f"<span style='font-size:10pt;color:{palette['muted']}'>Today so far</span>"))
        left.addWidget(QLabel(f"<span style='font-size:22pt;font-weight:900'>{money(pulse.today, currency)}</span>"))
        left.addWidget(QLabel(self._delta_html()))
        chips = QHBoxLayout(); chips.setSpacing(8)
        if pulse.streak_days >= 2:
            chips.addWidget(self._chip(f"🔥 {pulse.streak_days}-day streak"))
        chips.addWidget(self._chip(f"🛒 {pulse.today_count} sale{'s' if pulse.today_count != 1 else ''} today"))
        if pulse.is_best_day_ever:
            chips.addWidget(self._chip("🏆 Best day ever"))
        chips.addStretch()
        left.addLayout(chips)
        left.addStretch()

        mid = QVBoxLayout(self.mid_w)
        mid.setContentsMargins(0, 0, 0, 0)
        label = f"{min(int(pulse.goal_progress * 100), 999)}%" if pulse.goal > 0 else "Set\ngoal"
        ring = ProgressRing(pulse.goal_progress, label, palette)
        ring.clicked.connect(self.set_goal_requested)
        mid.addWidget(ring, 0, Qt.AlignmentFlag.AlignHCenter)
        caption = (f"Goal {money(pulse.goal, currency)}" if pulse.goal > 0
                   else (f"Try {money(pulse.suggested_goal, currency)}" if pulse.suggested_goal > 0 else "Daily goal"))
        cap = QLabel(caption); cap.setAlignment(Qt.AlignmentFlag.AlignHCenter); cap.setStyleSheet(f"color:{palette['muted']};")
        mid.addWidget(cap)

        right = QVBoxLayout(self.right_w)
        right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(QLabel(f"<span style='color:{palette['muted']}'>Last 14 days</span>"))
        right.addWidget(Sparkline(pulse.series, palette, currency))
        self.insight = QLabel(); self.insight.setWordWrap(True)
        self.insight.setStyleSheet(f"color:{palette['text']};padding-top:6px;")
        right.addWidget(self.insight)
        self._relayout(False)

        self._i = 0
        self._rotate()
        if len(pulse.insights) > 1:
            self._timer = QTimer(self); self._timer.timeout.connect(self._rotate); self._timer.start(9000)

    def _relayout(self, narrow: bool):
        if narrow == self._narrow:
            return
        self._narrow = narrow
        for w in (self.left_w, self.mid_w, self.right_w):
            self.grid.removeWidget(w)
        for c in range(3):
            self.grid.setColumnStretch(c, 0)
        if narrow:
            self.grid.addWidget(self.left_w, 0, 0)
            self.grid.addWidget(self.mid_w, 0, 1)
            self.grid.addWidget(self.right_w, 1, 0, 1, 2)
            self.grid.setColumnStretch(0, 1)
        else:
            self.grid.addWidget(self.left_w, 0, 0)
            self.grid.addWidget(self.mid_w, 0, 1)
            self.grid.addWidget(self.right_w, 0, 2)
            self.grid.setColumnStretch(0, 3); self.grid.setColumnStretch(2, 4)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout(self.width() < 820)

    def _chip(self, text):
        label = QLabel(text)
        label.setStyleSheet(f"background:{self.palette['bg']};border:1px solid {self.palette['border']};border-radius:10px;padding:3px 9px;font-size:9pt;")
        return label

    def _delta_html(self):
        p, muted = self.pulse, self.palette["muted"]
        d = p.delta_vs_yesterday
        if d is None:
            return f"<span style='color:{muted}'>No sales yesterday to compare with.</span>"
        colour = self.palette["green"] if d >= 0 else self.palette["orange"]
        arrow = "▲" if d >= 0 else "▼"
        return f"<span style='color:{colour};font-weight:700'>{arrow} {abs(d) * 100:.0f}%</span> <span style='color:{muted}'>vs yesterday ({money(p.yesterday, self.currency)})</span>"

    def _rotate(self):
        if not self.pulse.insights:
            return
        self.insight.setText("💡 " + self.pulse.insights[self._i % len(self.pulse.insights)])
        self._i += 1


class RecapDialog(QDialog):
    """Shown once per day on the first open: yesterday in review + a target for today."""

    def __init__(self, parent, pulse: Pulse, name: str, palette: dict, currency: str, style: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Your day at a glance")
        self.setMinimumWidth(460)
        if style:
            self.setStyleSheet(style)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<span style='font-size:18pt;font-weight:900'>Welcome back, {name} ✦</span>"))
        lines = []
        if pulse.yesterday > 0:
            lines.append(f"Yesterday you sold <b>{money(pulse.yesterday, currency)}</b>.")
            lines.append(f"Beat it today and you're on a roll — <b>{money(pulse.yesterday + 1, currency)}</b> does it.")
        else:
            lines.append("A fresh day. Your first sale is the hardest — let's get it on the board.")
        if pulse.streak_days:
            lines.append(f"🔥 Selling streak: <b>{pulse.streak_days} day{'s' if pulse.streak_days != 1 else ''}</b> — keep it alive today.")
        if pulse.week_total > 0 and pulse.prev_week_total > 0:
            change = (pulse.week_total - pulse.prev_week_total) / pulse.prev_week_total * 100
            lines.append(f"The last 7 days: <b>{money(pulse.week_total, currency)}</b> ({'+' if change >= 0 else ''}{change:.0f}% on the week before).")
        if pulse.low_stock:
            lines.append(f"📦 {pulse.low_stock} product(s) need restocking.")
        if pulse.unpaid_count:
            lines.append(f"🧾 {pulse.unpaid_count} unpaid invoice(s) — {money(pulse.unpaid_total, currency)} to collect.")
        for line in lines:
            label = QLabel(line); label.setWordWrap(True); label.setTextFormat(Qt.TextFormat.RichText); v.addWidget(label)
        go = QPushButton("Let's go"); go.setObjectName("primary"); go.setDefault(True); go.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(go); v.addLayout(row)
