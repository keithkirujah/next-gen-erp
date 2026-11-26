"""Inactivity lock for shared tills: after N idle minutes the screen asks for the signed-in user's
password again. The open window (e.g. a half-built POS cart) stays exactly as it was."""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)

_ACTIVITY = {QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
             QEvent.Type.Wheel, QEvent.Type.TouchBegin}


class IdleWatcher(QObject):
    idle = Signal()

    def __init__(self, minutes_provider, parent=None):
        super().__init__(parent)
        self.minutes_provider = minutes_provider
        self.last_activity = time.monotonic()
        self.paused = False
        QApplication.instance().installEventFilter(self)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check)
        self._timer.start(5000)

    def eventFilter(self, obj, event):
        if event.type() in _ACTIVITY:
            self.last_activity = time.monotonic()
        return False

    def _check(self):
        if self.paused:
            return
        try:
            minutes = int(self.minutes_provider())
        except (TypeError, ValueError):
            minutes = 0
        if minutes > 0 and time.monotonic() - self.last_activity >= minutes * 60:
            self.last_activity = time.monotonic()
            self.idle.emit()

    def stop(self):
        self._timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)


class LockDialog(QDialog):
    """Password prompt. ``verify(password) -> bool``. Result: accepted = unlocked, ``signed_out`` = user chose to leave."""

    def __init__(self, parent, display_name: str, verify, style: str = ""):
        super().__init__(parent)
        self.verify = verify
        self.signed_out = False
        self._fails = 0
        self._locked_until = 0.0
        self.setWindowTitle("Locked | NextGen ERP")
        self.setModal(True)
        self.setMinimumWidth(380)
        if style:
            self.setStyleSheet(style)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<span style='font-size:18pt;font-weight:800'>🔒 Screen locked</span>"))
        v.addWidget(QLabel(f"{display_name}, you were inactive for a while. Enter your password to continue."))
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.EchoMode.Password); self.password.setPlaceholderText("Password")
        self.password.returnPressed.connect(self._try)
        v.addWidget(self.password)
        self.error = QLabel(""); self.error.setStyleSheet("color:#FF647C;"); v.addWidget(self.error)
        row = QHBoxLayout()
        out = QPushButton("Sign out"); out.clicked.connect(self._sign_out)
        go = QPushButton("Unlock"); go.setObjectName("primary"); go.setDefault(True); go.clicked.connect(self._try)
        row.addWidget(out); row.addStretch(); row.addWidget(go)
        v.addLayout(row)
        self.password.setFocus()

    def reject(self):          # Esc must not bypass the lock
        pass

    def _sign_out(self):
        self.signed_out = True
        super().accept()

    def _try(self):
        now = time.monotonic()
        if now < self._locked_until:
            self.error.setText(f"Too many attempts. Wait {int(self._locked_until - now) + 1} s.")
            return
        if self.verify(self.password.text()):
            super().accept()
            return
        self._fails += 1
        self.password.selectAll()
        if self._fails >= 5:
            self._fails = 0
            self._locked_until = time.monotonic() + 30
            self.error.setText("Too many attempts. Locked for 30 seconds.")
        else:
            self.error.setText("Incorrect password.")
