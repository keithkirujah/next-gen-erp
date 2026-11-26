"""Product Lending Centre — interactive, auditable product issue and settlement workspace."""
import hashlib
import secrets
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QDialog,
    QFormLayout, QComboBox, QDialogButtonBox, QDoubleSpinBox, QDateEdit,
    QLineEdit, QInputDialog, QTextEdit, QFrame, QSplitter, QAbstractItemView,
    QSizePolicy
)
from operations import OperationError


def _manager_role(store, role):
    if role in ("Manager", "Admin"):
        return True
    try:
        return {"manage_users", "manage_finance"}.issubset(set(store.role_permissions(role)))
    except Exception:
        return False


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
    if not row or not row["active"] or not _manager_role(store, row["role"]):
        QMessageBox.warning(parent, title, "An active Manager/Admin-authorised account is required.")
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


class _MetricCard(QFrame):
    def __init__(self, title, accent, palette):
        super().__init__()
        self.setObjectName("lendingMetric")
        self.setStyleSheet(
            f"QFrame#lendingMetric{{background:{palette['panel']};border:1px solid {palette['border']};"
            f"border-left:4px solid {accent};border-radius:13px;}}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 11, 15, 12)
        layout.setSpacing(3)
        self.value = QLabel("—")
        self.value.setStyleSheet(f"font-size:21pt;font-weight:900;color:{accent};background:transparent;border:0;")
        self.caption = QLabel(title.upper())
        self.caption.setStyleSheet(f"font-size:8pt;font-weight:800;letter-spacing:1px;color:{palette['muted']};background:transparent;border:0;")
        layout.addWidget(self.value)
        layout.addWidget(self.caption)


class ProductLendingPage(QWidget):
    """Searchable lending register with live customer exposure and guided actions."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = window.store
        self.ops = self.store.operations
        self.palette = getattr(window, "palette", {})
        self.rows = []
        self._selected_id = None
        self._build_ui()
        self.refresh()

    def _c(self, key, fallback):
        return self.palette.get(key, fallback)

    def _button(self, label, primary=False):
        button = QPushButton(label)
        button.setMinimumHeight(36)
        if primary:
            button.setObjectName("primary")
        return button

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 20)
        root.setSpacing(13)

        hero = QFrame()
        hero.setObjectName("lendingHero")
        hero.setStyleSheet(
            f"QFrame#lendingHero{{background:{self._c('panel2', '#eef3fb')};"
            f"border:1px solid {self._c('border', '#dce3ee')};border-radius:16px;}}"
        )
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(20, 15, 20, 15)
        copy = QVBoxLayout()
        title = QLabel("Product Lending Centre")
        title.setStyleSheet("font-size:21pt;font-weight:900;background:transparent;border:0;")
        subtitle = QLabel(
            "A live control room for temporary product issues, customer exposure, employee authority and settlements."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"font-size:9.5pt;color:{self._c('muted', '#758197')};background:transparent;border:0;")
        copy.addWidget(title)
        copy.addWidget(subtitle)
        hero_layout.addLayout(copy, 1)
        self.issue_btn = self._button("＋  Issue a product", True)
        self.issue_btn.setMinimumWidth(172)
        self.issue_btn.clicked.connect(self.issue_product)
        hero_layout.addWidget(self.issue_btn)
        root.addWidget(hero)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(10)
        self.metric_active = _MetricCard("Active agreements", self._c("blue", "#477de0"), self.palette)
        self.metric_exposure = _MetricCard("Outstanding exposure", self._c("amber", "#c28b32"), self.palette)
        self.metric_overdue = _MetricCard("Overdue agreements", self._c("red", "#d45b5b"), self.palette)
        self.metric_settled = _MetricCard("Settled / returned", self._c("green", "#2b9b73"), self.palette)
        for index, card in enumerate((self.metric_active, self.metric_exposure, self.metric_overdue, self.metric_settled)):
            metrics.addWidget(card, 0, index)
        root.addLayout(metrics)

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Search loan number, customer, product, serial / IMEI…")
        self.search.textChanged.connect(self._apply_filters)
        self.status_filter = QComboBox()
        self.status_filter.addItems([
            "All agreements", "On loan", "Part-paid", "Overdue",
            "Returned · balance due", "Settled", "Returned"
        ])
        self.status_filter.currentTextChanged.connect(self._apply_filters)
        self.refresh_btn = self._button("↻  Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.status_filter)
        toolbar.addWidget(self.refresh_btn)
        root.addLayout(toolbar)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        register = QFrame()
        register.setObjectName("lendingPanel")
        register.setStyleSheet(
            f"QFrame#lendingPanel{{background:{self._c('panel', '#ffffff')};"
            f"border:1px solid {self._c('border', '#dce3ee')};border-radius:13px;}}"
        )
        register_layout = QVBoxLayout(register)
        register_layout.setContentsMargins(10, 10, 10, 10)
        register_heading = QLabel("LENDING REGISTER")
        register_heading.setStyleSheet(f"font-size:8pt;font-weight:900;letter-spacing:1px;color:{self._c('muted', '#758197')};padding:2px 4px 7px;")
        register_layout.addWidget(register_heading)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "AGREEMENT", "CUSTOMER", "PRODUCT", "VALUE", "PAID", "BALANCE", "DUE", "STATUS"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(False)
        self.table.currentCellChanged.connect(self._selection_changed)
        register_layout.addWidget(self.table, 1)
        self.result_count = QLabel("0 agreements")
        self.result_count.setStyleSheet(f"color:{self._c('muted', '#758197')};font-size:8pt;padding:4px;")
        register_layout.addWidget(self.result_count)
        split.addWidget(register)

        detail = QFrame()
        detail.setObjectName("lendingPanel")
        detail.setMinimumWidth(300)
        detail.setMaximumWidth(390)
        detail.setStyleSheet(
            f"QFrame#lendingPanel{{background:{self._c('panel', '#ffffff')};"
            f"border:1px solid {self._c('border', '#dce3ee')};border-radius:13px;}}"
        )
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(15, 14, 15, 14)
        detail_layout.setSpacing(9)
        detail_title = QLabel("AGREEMENT DETAILS")
        detail_title.setStyleSheet(f"font-size:8pt;font-weight:900;letter-spacing:1px;color:{self._c('muted', '#758197')};")
        detail_layout.addWidget(detail_title)
        self.detail_number = QLabel("Select an agreement")
        self.detail_number.setWordWrap(True)
        self.detail_number.setStyleSheet("font-size:15pt;font-weight:900;")
        detail_layout.addWidget(self.detail_number)
        self.detail_status = QLabel("Choose a row in the register to inspect it.")
        self.detail_status.setWordWrap(True)
        detail_layout.addWidget(self.detail_status)
        self.detail_body = QLabel("")
        self.detail_body.setWordWrap(True)
        self.detail_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail_body.setStyleSheet(f"line-height:1.4;color:{self._c('muted', '#758197')};")
        detail_layout.addWidget(self.detail_body)
        actions = QGridLayout()
        self.pay_btn = self._button("＋ Record payment")
        self.pay_btn.clicked.connect(self.record_payment)
        self.return_btn = self._button("↩ Receive product")
        self.return_btn.clicked.connect(self.receive_return)
        self.pay_btn.setEnabled(False)
        self.return_btn.setEnabled(False)
        actions.addWidget(self.pay_btn, 0, 0)
        actions.addWidget(self.return_btn, 0, 1)
        detail_layout.addLayout(actions)
        history_title = QLabel("ACTIVITY TIMELINE")
        history_title.setStyleSheet(f"font-size:8pt;font-weight:900;letter-spacing:1px;color:{self._c('muted', '#758197')};padding-top:7px;")
        detail_layout.addWidget(history_title)
        self.history = QTableWidget(0, 2)
        self.history.setHorizontalHeaderLabels(["EVENT", "WHEN"])
        self.history.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.history.verticalHeader().setVisible(False)
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history.setSelectionMode(QAbstractItemView.NoSelection)
        detail_layout.addWidget(self.history, 1)
        split.addWidget(detail)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        self.notice = QLabel("Customer credit and employee lending authority are separate checks. Manager approval never bypasses a customer's credit ceiling.")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(
            f"padding:9px 12px;border-radius:9px;background:{self._c('panel2', '#eef3fb')};"
            f"color:{self._c('muted', '#758197')};font-size:8.5pt;"
        )
        root.addWidget(self.notice)

    def _money(self, value):
        return f"KSh {Decimal(str(value or '0')):,.2f}"

    def _balance(self, row):
        return max(
            Decimal(str(row["total_value"])) - Decimal(str(row["paid_total"]))
            - Decimal(str(row["waived_total"] or "0")), Decimal("0.00")
        )

    def refresh(self):
        try:
            summary = self.ops.product_lending_summary()
            self.rows = list(self.ops.product_lending_records(limit=5000))
            self.metric_active.value.setText(f"{summary['active']:,}")
            self.metric_exposure.value.setText(self._money(summary["outstanding"]))
            self.metric_overdue.value.setText(f"{summary['overdue']:,}")
            settled = sum(1 for row in self.rows if row["status"] in ("Settled", "Returned"))
            self.metric_settled.value.setText(f"{settled:,}")
            self._apply_filters()
        except Exception as exc:
            self.notice.setText(f"Could not refresh lending records: {exc}")

    def _apply_filters(self, *_):
        query = self.search.text().strip().casefold()
        selected_status = self.status_filter.currentText()
        filtered = []
        for row in self.rows:
            status = str(row["status"])
            if selected_status != "All agreements" and status != selected_status:
                continue
            searchable = " ".join(str(row[key] or "") for key in (
                "loan_number", "customer_name", "product_name", "sku",
                "serial_number", "issued_by", "approved_by"
            )).casefold()
            if query and query not in searchable:
                continue
            filtered.append(row)
        current_id = self._selected_id
        self.table.blockSignals(True)
        self.table.setRowCount(len(filtered))
        for i, row in enumerate(filtered):
            balance = self._balance(row)
            values = [
                row["loan_number"], row["customer_name"], row["product_name"],
                self._money(row["total_value"]), self._money(row["paid_total"]),
                self._money(balance), row["due_date"], row["status"]
            ]
            for j, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if j in (3, 4, 5):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if j == 0:
                    item.setData(Qt.UserRole, int(row["id"]))
                self.table.setItem(i, j, item)
            if row["status"] == "Overdue":
                self.table.item(i, 7).setForeground(QColor(self._c('red', '#d45b5b')))
        self.table.blockSignals(False)
        self.result_count.setText(f"{len(filtered):,} agreement(s)")
        selected_row = next((i for i, row in enumerate(filtered) if int(row["id"]) == current_id), -1)
        if selected_row < 0 and filtered:
            selected_row = 0
        if selected_row >= 0:
            self.table.selectRow(selected_row)
            self._show_details(filtered[selected_row])
        else:
            self._selected_id = None
            self._clear_details()

    def _selection_changed(self, current_row, *_):
        if current_row < 0 or not self.table.item(current_row, 0):
            self._clear_details()
            return
        loan_id = self.table.item(current_row, 0).data(Qt.UserRole)
        row = next((r for r in self.rows if int(r["id"]) == int(loan_id)), None)
        if row:
            self._show_details(row)

    def _clear_details(self):
        self.detail_number.setText("Select an agreement")
        self.detail_status.setText("Choose a row in the register to inspect it.")
        self.detail_body.setText("")
        self.history.setRowCount(0)
        self.pay_btn.setEnabled(False)
        self.return_btn.setEnabled(False)

    def _show_details(self, row):
        self._selected_id = int(row["id"])
        balance = self._balance(row)
        self.detail_number.setText(str(row["loan_number"]))
        self.detail_status.setText(f"{row['status']}   ·   Due {row['due_date']}")
        self.detail_body.setText(
            f"<b>Customer</b>  {row['customer_name']}<br>"
            f"<b>Product</b>  {row['product_name']} ({row['sku']})<br>"
            f"<b>Quantity</b>  {row['quantity']}<br>"
            f"<b>Agreement value</b>  {self._money(row['total_value'])}<br>"
            f"<b>Paid</b>  {self._money(row['paid_total'])}<br>"
            f"<b>Waived</b>  {self._money(row['waived_total'] or '0')}<br>"
            f"<b>Outstanding</b>  {self._money(balance)}<br>"
            f"<b>Serial / IMEI</b>  {row['serial_number'] or '—'}<br>"
            f"<b>Issued by</b>  {row['issued_by']}<br>"
            f"<b>Approved by</b>  {row['approved_by'] or '—'}<br>"
            f"<b>Condition out</b>  {row['condition_out'] or '—'}<br>"
            f"<b>Condition in</b>  {row['condition_in'] or 'Not returned'}"
        )
        self.pay_btn.setEnabled(
            balance > 0 and row["status"] in ("On loan", "Part-paid", "Overdue")
            and "manage_finance" in self.store.role_permissions(self.window.role)
        )
        self.return_btn.setEnabled(
            not row["returned_at"] and row["status"] in ("On loan", "Part-paid", "Overdue")
            and "edit" in self.store.role_permissions(self.window.role)
        )
        events = self.ops.conn.execute(
            "SELECT event,details,actor,created_at FROM product_lending_events WHERE loan_id=? ORDER BY id DESC LIMIT 30",
            (int(row["id"]),)
        ).fetchall()
        self.history.setRowCount(len(events))
        for i, event in enumerate(events):
            title = QTableWidgetItem(f"{event['event']} · {event['actor']}\n{event['details']}")
            title.setToolTip(str(event["details"]))
            when = QTableWidgetItem(str(event["created_at"]))
            self.history.setItem(i, 0, title)
            self.history.setItem(i, 1, when)

    def _selected_loan(self):
        if self._selected_id is None:
            QMessageBox.information(self, "Select an agreement", "Choose a lending record from the register first.")
            return None
        return self._selected_id

    def issue_product(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Create a product lending agreement")
        dlg.setMinimumWidth(590)
        dlg.setModal(True)
        outer = QVBoxLayout(dlg)
        intro = QLabel("Choose a registered customer and an in-stock product. The live check below shows the customer ceiling and your personal approval authority before anything is issued.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        customer = QComboBox()
        customer.setEditable(True)
        customer.setInsertPolicy(QComboBox.NoInsert)
        customer.setMaxVisibleItems(12)
        customers = self.ops.customers()
        for item in customers:
            customer.addItem(f"{item['name']}  ·  {item['contact']}", int(item["id"]))
        product = QComboBox()
        product.setEditable(True)
        product.setInsertPolicy(QComboBox.NoInsert)
        product.setMaxVisibleItems(12)
        product_map = {}
        for item in self.ops.products(active_only=True):
            available = self.ops.stock_on_hand(int(item["id"]))
            if available > 0:
                product_map[int(item["id"])] = item
                product.addItem(
                    f"{item['sku']}  ·  {item['name']}  ·  {available} available  ·  {self._money(item['selling_price'])}",
                    int(item["id"])
                )
        qty = QDoubleSpinBox()
        qty.setRange(0.001, 1000000)
        qty.setDecimals(3)
        qty.setValue(1)
        qty.setSingleStep(1)
        due = QDateEdit(QDate.currentDate().addDays(7))
        due.setCalendarPopup(True)
        due.setMinimumDate(QDate.currentDate())
        serial = QLineEdit()
        serial.setPlaceholderText("Required for individually tracked devices where applicable")
        condition = QComboBox()
        condition.addItems(["Good", "Minor wear", "Damaged", "Incomplete"])
        notes = QTextEdit()
        notes.setPlaceholderText("Agreement terms, accessories issued, customer acknowledgement…")
        notes.setMaximumHeight(75)
        preview = QLabel("Select a customer and product to calculate the agreement.")
        preview.setWordWrap(True)
        preview.setTextFormat(Qt.RichText)
        preview.setStyleSheet(
            f"padding:12px;border-radius:10px;background:{self._c('panel2', '#eef3fb')};"
            f"border:1px solid {self._c('border', '#dce3ee')};"
        )
        form.addRow("Registered customer", customer)
        form.addRow("Product / SKU", product)
        form.addRow("Quantity", qty)
        form.addRow("Due date", due)
        form.addRow("Serial / IMEI", serial)
        form.addRow("Condition at issue", condition)
        form.addRow("Notes / agreement", notes)
        outer.addLayout(form)
        outer.addWidget(preview)

        current_user = self.store.conn.execute(
            "SELECT lending_limit FROM users WHERE username=? COLLATE NOCASE", (self.window.username,)
        ).fetchone()
        staff_limit = Decimal(str(current_user["lending_limit"] or "0")) if current_user else Decimal("0")
        manager = _manager_role(self.store, self.window.role)
        if manager:
            staff_limit = Decimal("999999999.99")

        def selected_id(combo):
            index = combo.currentIndex()
            return combo.itemData(index) if index >= 0 else None

        def update_preview(*_):
            cid, pid = selected_id(customer), selected_id(product)
            if cid is None or pid is None or int(pid) not in product_map:
                preview.setText("Choose a registered customer and an available product.")
                return
            item = product_map[int(pid)]
            amount = (Decimal(str(item["selling_price"])) * Decimal(str(qty.value()))).quantize(Decimal("0.01"))
            try:
                credit = self.ops.customer_credit_summary(int(cid))
                credit_line = (
                    f"Customer limit: <b>{self._money(credit['limit'])}</b> &nbsp; · &nbsp; "
                    f"Already exposed: <b>{self._money(credit['outstanding'])}</b> &nbsp; · &nbsp; "
                    f"Available: <b>{self._money(credit['available'])}</b>"
                )
                if not credit["enabled"]:
                    credit_line = "<span style='color:#c04b4b'><b>Customer credit facility is disabled.</b> Enable it in CRM before issuing a loan.</span>"
                elif amount > credit["available"]:
                    credit_line += "<br><span style='color:#c04b4b'><b>Customer credit ceiling would be exceeded.</b></span>"
            except Exception as exc:
                credit_line = f"Customer credit check unavailable: {exc}"
            authority_line = (
                f"Your per-transaction authority: <b>{self._money(staff_limit)}</b>"
                if not manager else "Your session: <b>Manager/Admin approval authority</b>"
            )
            if not manager and amount > staff_limit:
                authority_line += "<br><span style='color:#b47a20'><b>Separate Manager/Admin approval will be required.</b></span>"
            preview.setText(
                f"<div style='font-size:10pt'><b>Agreement value</b><br>"
                f"<span style='font-size:19pt;font-weight:900'>{self._money(amount)}</span><br><br>"
                f"{credit_line}<br>{authority_line}<br>"
                f"<span style='color:{self._c('muted', '#758197')}'>This preview is advisory; the database transaction rechecks stock, customer exposure and authority when you confirm.</span></div>"
            )

        customer.currentIndexChanged.connect(update_preview)
        product.currentIndexChanged.connect(update_preview)
        qty.valueChanged.connect(update_preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Review & issue product")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        outer.addWidget(buttons)
        if not customer.count() or not product.count():
            QMessageBox.warning(self, "Cannot issue product", "A registered customer and available stock are required.")
            return
        update_preview()
        if not dlg.exec():
            return
        cid, pid = selected_id(customer), selected_id(product)
        if cid is None or pid is None:
            QMessageBox.warning(self, "Missing information", "Select both a registered customer and an available product.")
            return
        item = product_map[int(pid)]
        amount = (Decimal(str(item["selling_price"])) * Decimal(str(qty.value()))).quantize(Decimal("0.01"))
        approver = ("", "")
        if amount > staff_limit and not manager:
            approver = _manager_credentials(self, self.store, "Additional lending authority required")
            if not approver:
                return
        confirm = QMessageBox.question(
            self, "Confirm product issue",
            f"Issue {qty.value():g} × {item['name']} to {customer.currentText()}?\n\n"
            f"Agreement value: {self._money(amount)}\nDue date: {due.date().toString('dd MMM yyyy')}\n"
            f"Manager approval: {approver[0] or (self.window.username if manager else 'Not required')}\n\n"
            "Stock will be reduced immediately and the agreement will be recorded.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            loan_id = self.ops.issue_product_loan(
                int(cid), int(pid), qty.value(), due.date().toString("yyyy-MM-dd"),
                condition.currentText(), serial.text(), notes.toPlainText(), approver[0], approver[1]
            )
            self.store.log(self.window.username, "Issued product loan",
                           f"Loan ID {loan_id} · Customer {cid} · Product {pid} · {self._money(amount)}")
            self.refresh()
            for i in range(self.table.rowCount()):
                if int(self.table.item(i, 0).data(Qt.UserRole)) == int(loan_id):
                    self.table.selectRow(i)
                    break
            QMessageBox.information(self, "Product issued", f"The lending agreement has been created successfully.\n\nAgreement: PL-{loan_id:06d}\nValue: {self._money(amount)}")
        except Exception as exc:
            QMessageBox.warning(self, "Could not issue product", str(exc))

    def record_payment(self):
        loan_id = self._selected_loan()
        if loan_id is None:
            return
        row = self.ops.conn.execute("SELECT * FROM product_lending_loans WHERE id=?", (int(loan_id),)).fetchone()
        if not row:
            return
        balance = self._balance(row)
        if balance <= 0:
            QMessageBox.information(self, "No balance due", "This agreement has no remaining amount to collect.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Record a lending repayment")
        dlg.setMinimumWidth(430)
        layout = QVBoxLayout(dlg)
        info = QLabel(f"<b>{row['loan_number']}</b><br>Customer balance: <span style='font-size:17pt;font-weight:900'>{self._money(balance)}</span>")
        info.setTextFormat(Qt.RichText)
        layout.addWidget(info)
        form = QFormLayout()
        amount = QDoubleSpinBox()
        amount.setRange(0.01, float(balance))
        amount.setDecimals(2)
        amount.setValue(float(balance))
        method = QComboBox()
        method.addItems(["Cash", "M-Pesa", "Bank transfer", "Card"])
        reference = QLineEdit()
        reference.setPlaceholderText("M-Pesa receipt / bank reference")
        form.addRow("Amount received (KES)", amount)
        form.addRow("Payment channel", method)
        form.addRow("Reference", reference)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if not dlg.exec():
            return
        try:
            self.ops.record_product_loan_payment(loan_id, amount.value(), method.currentText(), reference.text())
            self.refresh()
            QMessageBox.information(self, "Payment recorded", f"{self._money(amount.value())} has been recorded against {row['loan_number']}.")
        except Exception as exc:
            QMessageBox.warning(self, "Payment not recorded", str(exc))

    def receive_return(self):
        loan_id = self._selected_loan()
        if loan_id is None:
            return
        row = self.ops.conn.execute("SELECT * FROM product_lending_loans WHERE id=?", (int(loan_id),)).fetchone()
        if not row:
            return
        if row["returned_at"]:
            QMessageBox.information(self, "Already received", "This product has already been received back.")
            return
        balance = self._balance(row)
        dlg = QDialog(self)
        dlg.setWindowTitle("Inspect and receive returned product")
        dlg.setMinimumWidth(470)
        layout = QVBoxLayout(dlg)
        notice = QLabel(
            f"<b>{row['loan_number']}</b><br>Outstanding balance: <b>{self._money(balance)}</b><br>"
            "A good-condition return with an unpaid balance requires Manager/Admin approval to waive that balance. "
            "Damaged or incomplete items are quarantined and do not automatically clear the debt."
        )
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.RichText)
        layout.addWidget(notice)
        form = QFormLayout()
        condition = QComboBox()
        condition.addItems(["Good", "Minor wear", "Damaged", "Incomplete"])
        notes = QTextEdit()
        notes.setPlaceholderText("Inspection findings, missing accessories, visible damage…")
        notes.setMaximumHeight(85)
        form.addRow("Condition received", condition)
        form.addRow("Inspection notes", notes)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if not dlg.exec():
            return
        approver = ("", "")
        if balance > 0 and condition.currentText() == "Good" and not _manager_role(self.store, self.window.role):
            approver = _manager_credentials(self, self.store, "Authorise balance clearance")
            if not approver:
                return
        try:
            self.ops.return_product_loan(
                loan_id, condition.currentText(), notes.toPlainText(), approver[0], approver[1]
            )
            self.refresh()
            QMessageBox.information(self, "Return recorded", "The product inspection and return have been recorded.")
        except Exception as exc:
            QMessageBox.warning(self, "Return not recorded", str(exc))
