"""Real-time backups for NextGen ERP.

Replaces the old "cloud sync" idea (which needed a server nobody had built) with something that
works on day one: the app watches for changes and writes a verified full backup package to a
folder the owner picks (an external drive, a Google Drive / OneDrive folder, a network share…),
optionally also uploading it to a private GitHub repository in the background.

The owner chooses how often:
    * Real-time — a few seconds after the last change (never more often than every 30 s)
    * every 5 / 15 / 30 minutes, hourly, every 4 hours, daily or weekly
and the app tells them after EVERY run: a toast on success ("Backup saved 14:32 · 2.4 MB"),
a persistent red notice with a Retry button on failure, plus a status pill in the top bar and a
history list on the Database and Backup page.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from PySide6.QtCore import QObject, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QWidget

log = logging.getLogger("nextgen.backup")

REALTIME = 0
INTERVAL_OPTIONS = [
    (REALTIME, "Real-time — a few seconds after every change"),
    (5, "Every 5 minutes"),
    (15, "Every 15 minutes"),
    (30, "Every 30 minutes"),
    (60, "Every hour"),
    (240, "Every 4 hours"),
    (1440, "Once a day"),
    (10080, "Once a week"),
]
REALTIME_SETTLE_S = 8      # wait this long after the latest change (so a busy till is not interrupted per item)
REALTIME_MIN_GAP_S = 30    # never back up more often than this in real-time mode
POLL_MS = 4000
RETRY_DELAYS_S = (30, 60, 180, 600)   # then every 15 minutes until it works


def format_bytes(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def ago(iso_stamp: str) -> str:
    try:
        then = datetime.strptime(iso_stamp[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "never"
    seconds = max(0, int((datetime.now() - then).total_seconds()))
    if seconds < 15:
        return "just now"
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


class Toast(QFrame):
    """Small non-blocking notice in the bottom-right corner of the main window."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("backupToast")
        self.hide()
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 12, 12, 12)
        row.setSpacing(12)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setMinimumWidth(260)
        self.label.setMaximumWidth(420)
        self.action = QPushButton()
        self.action.hide()
        self.close_button = QPushButton("✕")
        self.close_button.setFixedWidth(30)
        self.close_button.clicked.connect(self.dismiss)
        row.addWidget(self.label, 1)
        row.addWidget(self.action)
        row.addWidget(self.close_button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(220)

    def show_message(self, text: str, ok: bool = True, seconds: int = 7, action_text: str = "", action=None):
        colour = "#16D9A2" if ok else "#FF647C"
        self.setStyleSheet(
            f"#backupToast{{background:#101A30;border:1px solid {colour};border-left:5px solid {colour};border-radius:10px;}}"
            "#backupToast QLabel{color:#F2F6FF;font-size:10pt;} #backupToast QPushButton{padding:6px 10px;}")
        self.label.setText(text)
        if getattr(self, "_action_connected", False):
            self.action.clicked.disconnect()
            self._action_connected = False
        if action_text and action:
            self.action.setText(action_text)
            self.action.clicked.connect(lambda: (self.dismiss(), action()))
            self._action_connected = True
            self.action.show()
        else:
            self.action.hide()
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.stop()
        if seconds > 0:
            self._timer.start(seconds * 1000)

    def reposition(self):
        parent = self.parentWidget()
        if parent:
            self.adjustSize()
            self.move(max(8, parent.width() - self.width() - 24), max(8, parent.height() - self.height() - 24))

    def dismiss(self):
        self._timer.stop()
        self.hide()


class BackupManager(QObject):
    """Owns the backup schedule. Lives on the UI thread; only the GitHub upload runs in a worker thread."""

    state_changed = Signal()
    succeeded = Signal(dict)
    failed = Signal(str)
    _upload_done = Signal(dict, str, str)   # result, upload_note, upload_error

    def __init__(self, store, toast: Toast | None = None, can_run=lambda: True, parent=None):
        super().__init__(parent)
        self.store = store
        self.toast = toast
        self.can_run = can_run
        self.running = False
        self.uploading = False
        self.last_error = ""
        self.failures = 0
        self._next_retry_at = 0.0
        self._last_change_seen = store.change_counter()
        self._last_change_time = time.monotonic()
        self._counter_at_last_backup = None   # None → unknown, treat as "changed" once
        self._last_backup_monotonic = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._upload_done.connect(self._finish_after_upload)

    # ---- configuration ------------------------------------------------
    def enabled(self) -> bool:
        return self.store.setting("auto_backup_enabled", "1") == "1"

    def interval_minutes(self) -> int:
        try:
            return max(0, int(self.store.setting("auto_backup_interval_minutes", "0") or "0"))
        except (TypeError, ValueError):
            return REALTIME

    def start(self):
        self._timer.start(POLL_MS)
        self.state_changed.emit()

    def stop(self):
        self._timer.stop()

    def reconfigure(self):
        self.failures = 0
        self._next_retry_at = 0.0
        self.last_error = ""
        self.state_changed.emit()

    # ---- status for the UI --------------------------------------------
    def has_unsaved_changes(self) -> bool:
        if self._counter_at_last_backup is None:
            return True
        return self.store.change_counter() != self._counter_at_last_backup

    def last_success(self) -> dict | None:
        for entry in self.store.backup_history():
            if entry.get("ok"):
                return entry
        return None

    def status(self) -> tuple[str, str]:
        """(level, text) with level in ok | busy | warn | bad | off"""
        if self.running:
            return "busy", "●  BACKING UP…"
        if self.uploading:
            return "busy", "●  UPLOADING BACKUP…"
        if not self.enabled():
            return "warn", "●  BACKUPS OFF"
        if self.last_error:
            return "bad", "●  BACKUP FAILED · click to fix"
        last = self.last_success()
        when = ago(last["t"]) if last else "never"
        if self.has_unsaved_changes() and last is not None:
            return "warn", f"●  CHANGES WAITING · last backup {when}"
        if last is None:
            return "warn", "●  NO BACKUP YET"
        return "ok", f"●  BACKED UP · {when}"

    # ---- scheduling ---------------------------------------------------
    def tick(self):
        counter = self.store.change_counter()
        if counter != self._last_change_seen:
            self._last_change_seen = counter
            self._last_change_time = time.monotonic()
        self.state_changed.emit()
        if self.running or self.uploading or not self.enabled() or not self.can_run():
            return
        now = time.monotonic()
        if self.last_error and now < self._next_retry_at:
            return
        interval = self.interval_minutes()
        due = False
        if interval == REALTIME:
            settled = now - self._last_change_time >= REALTIME_SETTLE_S
            gap_ok = self._last_backup_monotonic is None or now - self._last_backup_monotonic >= REALTIME_MIN_GAP_S
            due = self.has_unsaved_changes() and settled and gap_ok
        else:
            if self._last_backup_monotonic is None:
                # first run after the app opens: back up when the last success is older than the interval
                last = self.last_success()
                due = last is None or _minutes_since(last["t"]) >= interval
                if not due:
                    self._last_backup_monotonic = now - _minutes_since(last["t"]) * 60
            else:
                due = now - self._last_backup_monotonic >= interval * 60
            if due and not self.has_unsaved_changes() and self.last_success() is not None:
                self._last_backup_monotonic = now          # nothing changed: skip quietly, check again next interval
                due = False
        if due or (self.last_error and now >= self._next_retry_at):
            self.run_now("scheduled")

    # ---- running ------------------------------------------------------
    def run_now(self, reason: str = "manual"):
        if self.running or self.uploading:
            return
        self.running = True
        self.state_changed.emit()
        QApplication.processEvents()
        try:
            result = self.store.run_local_backup(reason)
        except Exception as exc:
            self.running = False
            self._record_failure(reason, f"Could not write the backup: {exc}")
            return
        self.running = False
        result["reason"] = reason
        creds = self.store.github_credentials()
        if creds["enabled"]:
            self.uploading = True
            self.state_changed.emit()
            args = (result["path"], creds["repo"], creds["token"], creds["path"])

            def worker():
                note, error = "", ""
                try:
                    note = self.store.push_backup_to_github(*args)
                except Exception as exc:  # network down, bad token…
                    error = str(exc)
                self._upload_done.emit(result, note, error)
            threading.Thread(target=worker, name="github-backup", daemon=True).start()
        else:
            self._finish(result, reason, "", "")

    def _finish_after_upload(self, result: dict, note: str, error: str):
        self.uploading = False
        self._finish(result, result.get("reason", "scheduled"), note, error)

    def _finish(self, result: dict, reason: str, upload_note: str, upload_error: str):
        stamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.store.setting("github_backup_enabled", "0") == "1":
                self.store.set_setting("github_backup_last_status",
                                       stamp_now + (f" · failed · {upload_error[:160]}" if upload_error else f" · ok · {upload_note}"))
            self.store.finish_backup(result, reason, upload_note if not upload_error else f"saved locally; GitHub upload failed: {upload_error[:120]}")
        except Exception as exc:
            self._record_failure(reason, f"The backup was written but could not be recorded: {exc}")
            return
        self.failures = 0
        self.last_error = ""
        self._last_backup_monotonic = time.monotonic()
        self._counter_at_last_backup = self.store.change_counter()
        self._last_change_seen = self._counter_at_last_backup
        try:
            self.store.log("system", "Backup", f"{result['path']} ({format_bytes(result['bytes'])}){' · GitHub ok' if upload_note else ''}")
            self._counter_at_last_backup = self.store.change_counter()   # the log line above is not "new work"
        except Exception:
            pass
        message = f"✔  Backup saved at {datetime.now():%H:%M} · {format_bytes(result['bytes'])}\n{result['path']}"
        if upload_note:
            message += f"\nAlso uploaded to GitHub ({upload_note})"
        if upload_error:
            message += f"\n⚠ GitHub upload failed: {upload_error[:140]}"
        if self.toast is not None:
            self.toast.show_message(message, ok=not upload_error, seconds=8 if not upload_error else 14)
        self.succeeded.emit(result)
        self.state_changed.emit()

    def _record_failure(self, reason: str, message: str):
        self.failures += 1
        self.last_error = message
        delay = RETRY_DELAYS_S[min(self.failures - 1, len(RETRY_DELAYS_S) - 1)] if self.failures <= len(RETRY_DELAYS_S) else 900
        self._next_retry_at = time.monotonic() + delay
        log.error("backup failed: %s", message)
        try:
            self.store.add_backup_history({"t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": False,
                                           "reason": reason, "path": "", "bytes": 0, "note": message[:300]})
        except Exception:
            pass
        if self.toast is not None:
            self.toast.show_message(f"⚠  Backup failed — your data is NOT yet protected.\n{message[:220]}\nNext automatic retry in about {delay // 60 or 1} min.",
                                    ok=False, seconds=0, action_text="Retry now", action=lambda: self.run_now("retry"))
        self.failed.emit(message)
        self.state_changed.emit()

    def backup_on_exit(self):
        """Last-chance backup when the app closes with unsaved changes. Synchronous, local only."""
        if not self.enabled() or not self.has_unsaved_changes() or self.running:
            return
        if self.store.setting("backup_on_exit", "1") != "1":
            return
        try:
            result = self.store.run_local_backup("on exit")
            self.store.finish_backup(result, "on exit", "")
        except Exception:
            log.exception("exit backup failed")


def _minutes_since(iso_stamp: str) -> float:
    try:
        then = datetime.strptime(iso_stamp[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 10 ** 9
    return max(0.0, (datetime.now() - then).total_seconds() / 60.0)
