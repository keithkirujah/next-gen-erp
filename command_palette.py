"""Ctrl+K command palette: type a few letters, jump to any page or action."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout


def rank(query: str, items):
    """items: [(label, keywords, payload)] -> best matches first. Empty query keeps the given order."""
    tokens = [t for t in query.casefold().split() if t]
    if not tokens:
        return list(items)
    scored = []
    for position, (label, keywords, payload) in enumerate(items):
        hay = (label + " " + keywords).casefold()
        score, ok = 0, True
        for t in tokens:
            if t in label.casefold():
                score += 3 if label.casefold().startswith(t) else 2
            elif t in hay:
                score += 1
            elif _subsequence(t, label.casefold()):
                score += 0.5
            else:
                ok = False
                break
        if ok:
            scored.append((-score, position, (label, keywords, payload)))
    scored.sort()
    return [item for _, _, item in scored]


def _subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


class CommandPalette(QDialog):
    def __init__(self, parent, items, style=""):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.items = items
        self.chosen = None
        self.setModal(True)
        self.setMinimumWidth(520)
        self.resize(600, 430)
        if style:
            self.setStyleSheet(style)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<b>Go to…</b>  <span style='color:#8B9AB8'>type a page or action · Enter to open · Esc to close</span>"))
        self.edit = QLineEdit(); self.edit.setPlaceholderText("e.g. sale, backup, printer, stock…")
        self.edit.textChanged.connect(self._refill)
        self.edit.returnPressed.connect(self._accept_current)
        v.addWidget(self.edit)
        self.list = QListWidget(); self.list.setMinimumHeight(280)
        self.list.itemActivated.connect(lambda *_: self._accept_current())
        v.addWidget(self.list)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#8290a6;font-size:9pt;padding:2px 3px;")
        v.addWidget(self.status)
        self._refill("")
        self.edit.setFocus()
        # Keep the palette anchored to the active application rather than
        # appearing at an arbitrary OS position.
        if parent is not None:
            frame = parent.frameGeometry()
            self.move(frame.center().x() - self.width() // 2,
                      frame.center().y() - self.height() // 2)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            row = self.list.currentRow() + (1 if e.key() == Qt.Key.Key_Down else -1)
            self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
            return
        super().keyPressEvent(e)

    def _refill(self, text):
        self.list.clear()
        for label, _kw, payload in rank(text, self.items)[:30]:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, payload)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
            self.status.setText(f"{self.list.count()} matches · ↑↓ navigate · Enter open")
        else:
            self.status.setText("No matching commands. Try a shorter keyword or module name.")

    def _accept_current(self):
        item = self.list.currentItem()
        if item is not None:
            self.chosen = item.data(Qt.ItemDataRole.UserRole)
            self.accept()
