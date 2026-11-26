"""Returns desk — process refunds against completed sales with optional restock."""
from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QAbstractItemView,
    QComboBox, QFrame, QCheckBox, QTextEdit,
)

from operations import OperationError


class ReturnsPage(QWidget):
    def __init__(self, ops, currency="KES", parent=None):
        super().__init__(parent)
        self.ops = ops
        self.currency = currency
        self.sale = None
        self.lines = []
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        head = QLabel("Returns desk")
        head.setStyleSheet("font-size: 18pt; font-weight: 800;")
        root.addWidget(head)
        root.addWidget(QLabel("Look up a sale, choose lines to return, restock if needed, and refund through the original or another channel."))

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Sale number or ID (e.g. SAL-1042)")
        self.search.returnPressed.connect(self._load)
        row.addWidget(self.search, 1)
        btn = QPushButton("Load sale")
        btn.setObjectName("primary")
        btn.clicked.connect(self._load)
        row.addWidget(btn)
        root.addLayout(row)

        self.meta = QLabel("No sale loaded")
        self.meta.setStyleSheet("color: #7E90B0;")
        root.addWidget(self.meta)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Return", "SKU", "Item", "Sold", "Already returned", "Qty to return"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        root.addWidget(self.table, 1)

        form = QHBoxLayout()
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason for return")
        form.addWidget(self.reason, 2)
        self.method = QComboBox()
        self.method.addItems(["Cash", "M-Pesa", "Bank transfer", "Card", "Other"])
        form.addWidget(self.method)
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("Refund reference (if any)")
        form.addWidget(self.reference, 1)
        root.addLayout(form)

        actions = QHBoxLayout()
        go = QPushButton("Process return")
        go.setObjectName("primary")
        go.setMinimumHeight(42)
        go.clicked.connect(self._process)
        actions.addWidget(go)
        actions.addStretch()
        root.addLayout(actions)

    def _load(self):
        key = self.search.text().strip()
        if not key:
            return
        try:
            if key.isdigit():
                sale = self.ops.conn.execute("SELECT * FROM sales WHERE id=?", (int(key),)).fetchone()
            else:
                sale = self.ops.conn.execute("SELECT * FROM sales WHERE sale_number=? COLLATE NOCASE", (key,)).fetchone()
            if not sale:
                QMessageBox.information(self, "Not found", f"No sale matched “{key}”.")
                return
            lines = self.ops.conn.execute(
                "SELECT * FROM sale_lines WHERE sale_id=? ORDER BY id", (sale["id"],)).fetchall()
        except Exception as exc:
            QMessageBox.warning(self, "Lookup failed", str(exc))
            return
        self.sale = sale
        self.lines = list(lines)
        self.meta.setText(
            f"{sale['sale_number']}  ·  {sale['customer_name']}  ·  "
            f"Total {self.currency} {sale['total']}  ·  Paid {self.currency} {sale['paid_total']}  ·  {sale['created_at']}"
        )
        # Previously returned quantities
        returned = {}
        for row in self.ops.conn.execute(
            """SELECT sale_line_id, SUM(CAST(quantity AS REAL)) AS qty
               FROM sale_return_lines WHERE sale_line_id IN (
                 SELECT id FROM sale_lines WHERE sale_id=?)
               GROUP BY sale_line_id""",
            (sale["id"],),
        ).fetchall():
            returned[row["sale_line_id"]] = Decimal(str(row["qty"] or 0))

        self.table.setRowCount(len(self.lines))
        self._checks = []
        self._qtys = []
        self._restocks = []
        for i, line in enumerate(self.lines):
            sold = Decimal(str(line["quantity"]))
            prior = returned.get(line["id"], Decimal("0"))
            chk = QCheckBox()
            self.table.setCellWidget(i, 0, chk)
            self._checks.append(chk)
            self.table.setItem(i, 1, QTableWidgetItem(str(line["sku"])))
            self.table.setItem(i, 2, QTableWidgetItem(str(line["description"])))
            self.table.setItem(i, 3, QTableWidgetItem(f"{sold:.0f}"))
            self.table.setItem(i, 4, QTableWidgetItem(f"{prior:.0f}"))
            qty = QLineEdit(f"{max(sold - prior, 0):.0f}")
            self.table.setCellWidget(i, 5, qty)
            self._qtys.append(qty)

    def _process(self):
        if not self.sale:
            QMessageBox.information(self, "Load a sale", "Load a sale first.")
            return
        payload = []
        for i, line in enumerate(self.lines):
            if not self._checks[i].isChecked():
                continue
            try:
                qty = Decimal(self._qtys[i].text().strip() or "0")
            except Exception:
                QMessageBox.warning(self, "Invalid quantity", f"Check quantity for {line['sku']}.")
                return
            if qty <= 0:
                continue
            payload.append({"sale_line_id": line["id"], "quantity": qty, "restock": True})
        if not payload:
            QMessageBox.information(self, "Nothing selected", "Tick at least one line and set a quantity.")
            return
        reason = self.reason.text().strip()
        if not reason:
            QMessageBox.warning(self, "Reason required", "Enter a return reason.")
            return
        try:
            rid = self.ops.record_sale_return(
                self.sale["id"],
                payload,
                reason=reason,
                payment_method=self.method.currentText(),
                payment_reference=self.reference.text().strip(),
            )
        except OperationError as exc:
            QMessageBox.warning(self, "Return blocked", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "Return failed", str(exc))
            return
        QMessageBox.information(self, "Return processed", f"Return recorded (id {rid}). Stock restocked where applicable.")
        self._load()
