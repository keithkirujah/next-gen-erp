"""Product Lending Centre — controlled temporary issue, repayment and return workflow."""
import hashlib
import secrets
from decimal import Decimal
from PySide6.QtCore import Qt, QDate
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QDialog,
    QFormLayout, QComboBox, QDialogButtonBox, QDoubleSpinBox, QDateEdit,
    QLineEdit, QInputDialog, QTextEdit
)
from operations import OperationError


def _manager_credentials(parent, store, title):
    username, ok = QInputDialog.getText(parent, title, "Manager/Admin username:")
    if not ok or not username.strip():
        return None
    password, ok = QInputDialog.getText(parent, title, "Manager/Admin password:", QLineEdit.Password)
    if not ok:
        return None
    row = store.conn.execute(
        "SELECT username,role,password_hash,active FROM users WHERE username=? COLLATE NOCASE",
        (username.strip(),)
    ).fetchone()
    if not row or not row["active"] or row["role"] not in ("Manager", "Admin"):
        QMessageBox.warning(parent, title, "An active Manager/Admin account is required.")
        return None
    try:
        salt, digest = str(row["password_hash"]).split(":", 1)
        valid = secrets.compare_digest(
            hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240000).hex(),
            digest
        )
    except Exception:
        valid = False
    if not valid:
        QMessageBox.warning(parent, title, "The Manager/Admin credentials could not be verified.")
        return None
    return row["username"], password


class ProductLendingPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = window.store
        self.ops = self.store.operations
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("Product Lending Centre")
        title.setStyleSheet("font-size:20pt;font-weight:800;")
        heading.addWidget(title)
        heading.addStretch()
        issue = QPushButton("＋ Issue product")
        issue.setObjectName("primary")
        issue.clicked.connect(self.issue_product)
        heading.addWidget(issue)
        root.addLayout(heading)
        intro = QLabel("Temporary issue of shop-owned stock. Customer exposure and employee approval authority are checked separately.")
        intro.setWordWrap(True)
        root.addWidget(intro)
        cards = QGridLayout()
        self.active_value = self._metric("0", "Active loans")
        self.outstanding_value = self._metric("KSh 0.00", "Outstanding value")
        self.overdue_value = self._metric("0", "Overdue")
        for col, widget in enumerate((self.active_value, self.outstanding_value, self.overdue_value)):
            cards.addWidget(widget, 0, col)
        root.addLayout(cards)
        actions = QHBoxLayout()
        self.payment_btn = QPushButton("Record payment")
        self.payment_btn.clicked.connect(self.record_payment)
        self.return_btn = QPushButton("Receive returned product")
        self.return_btn.clicked.connect(self.receive_return)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        actions.addWidget(self.payment_btn)
        actions.addWidget(self.return_btn)
        actions.addStretch()
        actions.addWidget(refresh)
        root.addLayout(actions)
        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels([
            "LOAN", "CUSTOMER", "PRODUCT", "QTY", "VALUE", "PAID",
            "BALANCE", "DUE DATE", "ISSUED BY", "APPROVED BY", "STATUS"
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self.refresh()

    def _metric(self, value, label):
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        number = QLabel(value)
        number.setStyleSheet("font-size:19pt;font-weight:800;")
        caption = QLabel(label)
        caption.setStyleSheet("color:#8B9AB8;font-size:9pt;")
        layout.addWidget(number)
        layout.addWidget(caption)
        frame.value_label = number
        return frame

    def refresh(self):
        try:
            summary = self.ops.product_lending_summary()
            self.active_value.value_label.setText(str(summary["active"]))
            self.outstanding_value.value_label.setText(f"KSh {summary['outstanding']:,.2f}")
            self.overdue_value.value_label.setText(str(summary["overdue"]))
            rows = self.ops.product_lending_records()
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                balance = max(Decimal(row["total_value"]) - Decimal(row["paid_total"]), Decimal("0"))
                values = [
                    row["loan_number"], row["customer_name"], row["product_name"],
                    row["quantity"], f"KSh {Decimal(row['total_value']):,.2f}",
                    f"KSh {Decimal(row['paid_total']):,.2f}", f"KSh {balance:,.2f}",
                    row["due_date"], row["issued_by"], row["approved_by"] or "—", row["status"]
                ]
                for j, value in enumerate(values):
                    self.table.setItem(i, j, QTableWidgetItem(str(value)))
                self.table.item(i, 0).setData(Qt.UserRole, int(row["id"]))
        except Exception as exc:
            QMessageBox.warning(self, "Product lending", str(exc))

    def _selected_loan(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            QMessageBox.information(self, "Select a loan", "Select a lending record first.")
            return None
        return self.table.item(row, 0).data(Qt.UserRole)

    def issue_product(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Issue product on loan")
        dlg.setMinimumWidth(460)
        form = QFormLayout(dlg)
        customer = QComboBox()
        for item in self.ops.customers():
            customer.addItem(f"{item['name']} · {item['contact']}", int(item["id"]))
        product = QComboBox()
        for item in self.ops.products(active_only=True):
            available = self.ops.stock_on_hand(int(item["id"]))
            if available > 0:
                product.addItem(f"{item['sku']} · {item['name']} · {available} available · KSh {Decimal(item['selling_price']):,.2f}",
                                int(item["id"]))
        qty = QDoubleSpinBox()
        qty.setRange(0.001, 1000000)
        qty.setDecimals(3)
        qty.setValue(1)
        due = QDateEdit(QDate.currentDate().addDays(7))
        due.setCalendarPopup(True)
        serial = QLineEdit()
        condition = QLineEdit("Good")
        notes = QTextEdit()
        notes.setMaximumHeight(70)
        form.addRow("Registered customer", customer)
        form.addRow("Product", product)
        form.addRow("Quantity", qty)
        form.addRow("Due date", due)
        form.addRow("Serial / IMEI (if applicable)", serial)
        form.addRow("Condition at issue", condition)
        form.addRow("Notes / agreement", notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if not customer.count() or not product.count():
            QMessageBox.warning(self, "Cannot issue", "A registered customer and available stock are required.")
            return
        if not dlg.exec():
            return
        approver = ("", "")
        customer_id = customer.currentData()
        product_id = product.currentData()
        product_row = self.ops.conn.execute("SELECT selling_price FROM products WHERE id=?", (product_id,)).fetchone()
        amount = Decimal(str(product_row["selling_price"])) * Decimal(str(qty.value()))
        user = self.store.conn.execute("SELECT lending_limit FROM users WHERE username=? COLLATE NOCASE",
                                       (self.window.username,)).fetchone()
        limit = Decimal(str(user["lending_limit"] or "0")) if user else Decimal("0")
        if amount > limit:
            approver = _manager_credentials(self, self.store, "Manager approval required")
            if not approver:
                return
        try:
            self.ops.issue_product_loan(
                customer_id, product_id, qty.value(), due.date().toString("yyyy-MM-dd"),
                condition.text(), serial.text(), notes.toPlainText(),
                approver[0], approver[1]
            )
            self.store.log(self.window.username, "Issued product loan",
                           f"Customer ID {customer_id} · Product ID {product_id} · KSh {amount:,.2f}")
            self.refresh()
        except (OperationError, Exception) as exc:
            QMessageBox.warning(self, "Could not issue product", str(exc))

    def record_payment(self):
        loan_id = self._selected_loan()
        if loan_id is None:
            return
        row = self.ops.conn.execute("SELECT * FROM product_lending_loans WHERE id=?", (loan_id,)).fetchone()
        if not row:
            return
        balance = Decimal(row["total_value"]) - Decimal(row["paid_total"])
        amount, ok = QInputDialog.getDouble(self, "Record loan payment",
            f"Outstanding balance: KSh {balance:,.2f}\nPayment amount (KES):",
            float(balance), 0.01, float(balance), 2)
        if not ok:
            return
        method, ok = QInputDialog.getItem(self, "Payment method", "Method:", ["Cash", "M-Pesa", "Bank", "Card"], 0, False)
        if not ok:
            return
        reference = ""
        if method == "M-Pesa":
            reference, ok = QInputDialog.getText(self, "M-Pesa reference", "Confirmed transaction code:")
            if not ok:
                return
        try:
            self.ops.record_product_loan_payment(loan_id, amount, method, reference)
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "Payment not recorded", str(exc))

    def receive_return(self):
        loan_id = self._selected_loan()
        if loan_id is None:
            return
        row = self.ops.conn.execute("SELECT * FROM product_lending_loans WHERE id=?", (loan_id,)).fetchone()
        if not row:
            return
        condition, ok = QInputDialog.getItem(self, "Receive returned product", "Condition on return:",
            ["Good", "Minor wear", "Damaged", "Incomplete"], 0, False)
        if not ok:
            return
        notes, ok = QInputDialog.getText(self, "Return notes", "Inspection notes (optional):")
        if not ok:
            return
        balance = Decimal(row["total_value"]) - Decimal(row["paid_total"])
        approver = ("", "")
        if balance > 0:
            approver = _manager_credentials(self, self.store, "Approve return and clear balance")
            if not approver:
                return
            if condition != "Good":
                QMessageBox.warning(self, "Inspection required",
                    "A product not returned in good condition cannot automatically clear the customer's balance. Record the financial settlement separately and investigate the damage.")
                return
        try:
            self.ops.return_product_loan(loan_id, condition, notes, approver[0], approver[1])
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "Return not recorded", str(exc))
