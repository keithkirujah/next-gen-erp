"""
Immersive cashier-grade Point of Sale for NextGen ERP.

- Vibrant, animated till experience
- Paystack-powered M-Pesa with auto-detect (poll until paid)
- Discounts, hold/recall, admin-configured payment methods
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QComboBox, QFrame, QAbstractItemView, QDoubleSpinBox, QInputDialog,
    QDialog, QGraphicsOpacityEffect, QProgressBar,
)

from operations import OperationsService, OperationError
from payment_providers import (
    enabled_payment_methods,
    provider_requires_reference,
    provider_supports_change,
    provider_supports_auto_detect,
    provider_credentials,
    paystack_secret_key,
    load_providers_config,
)


class CartLine:
    __slots__ = ("product_id", "sku", "name", "unit_price", "quantity", "stock", "serial")

    def __init__(self, product_id, sku, name, unit_price, quantity=Decimal("1"), stock=Decimal("0"), serial=""):
        self.product_id = int(product_id)
        self.sku = str(sku or "")
        self.name = str(name or "")
        self.unit_price = Decimal(str(unit_price)).quantize(Decimal("0.01"))
        self.quantity = Decimal(str(quantity))
        self.stock = Decimal(str(stock))
        self.serial = str(serial or "")

    @property
    def line_total(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PaymentWaitDialog(QDialog):
    """Waiting room while the customer completes M-Pesa; polls Paystack for auto-detect."""

    def __init__(self, parent, currency: str, amount: Decimal, phone: str, reference: str,
                 secret_key: str, timeout_sec: int = 90):
        super().__init__(parent)
        self.setWindowTitle("Waiting for payment")
        self.setModal(True)
        self.setMinimumSize(520, 420)
        self.currency = currency
        self.amount = amount
        self.phone = phone
        self.reference = reference
        self.secret_key = secret_key
        self.timeout_sec = max(30, int(timeout_sec))
        self.elapsed = 0
        self.final_status = "pending"
        self._pulse = 0

        self.setStyleSheet("""
            QDialog { background: #0B1220; color: #F4F7FF; }
            QLabel { color: #F4F7FF; }
            QProgressBar {
                border: none; border-radius: 10px; background: #1A2440;
                height: 18px; text-align: center; color: #fff; font-weight: 700;
            }
            QProgressBar::chunk {
                border-radius: 10px;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #00E5A8, stop:0.5 #3B82F6, stop:1 #A855F7);
            }
            QPushButton {
                background: #1A2440; color: #F4F7FF; border: 1px solid #2A3A5C;
                border-radius: 12px; padding: 12px 18px; font-weight: 700;
            }
            QPushButton:hover { border-color: #3B82F6; }
            QPushButton#cancelBtn { color: #FF8B9A; }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(14)

        self.badge = QLabel("LISTENING FOR PAYMENT")
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setStyleSheet("font-size: 11pt; font-weight: 800; letter-spacing: 2px; color: #00E5A8;")
        root.addWidget(self.badge)

        self.title = QLabel(f"{currency} {amount:.2f}")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-size: 42pt; font-weight: 900; color: #FFFFFF;")
        root.addWidget(self.title)

        self.subtitle = QLabel(f"Prompt sent to  {phone}\nCustomer should enter their M-Pesa PIN")
        self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setStyleSheet("font-size: 13pt; color: #9FB0D0;")
        root.addWidget(self.subtitle)

        self.status = QLabel("Waiting for confirmation...")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet("font-size: 14pt; font-weight: 700; color: #7DD3FC; padding: 12px;")
        root.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setRange(0, self.timeout_sec)
        self.bar.setValue(0)
        self.bar.setFormat("%v s")
        root.addWidget(self.bar)

        self.ref_lbl = QLabel(f"Ref  {reference}")
        self.ref_lbl.setAlignment(Qt.AlignCenter)
        self.ref_lbl.setStyleSheet("color: #5B6B8C; font-size: 9pt;")
        root.addWidget(self.ref_lbl)
        root.addStretch()

        btn_row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel wait")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._tick)
        self.poll_timer.start(1000)
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._animate_badge)
        self.anim_timer.start(400)

    def _animate_badge(self):
        self._pulse = (self._pulse + 1) % 4
        if self.final_status == "pending":
            self.badge.setText("LISTENING FOR PAYMENT" + ("." * (self._pulse + 1)))

    def _tick(self):
        if self.final_status != "pending":
            return
        self.elapsed += 1
        self.bar.setValue(min(self.elapsed, self.timeout_sec))
        if self.elapsed >= self.timeout_sec:
            self.final_status = "timeout"
            self.status.setText("Timed out — payment not confirmed")
            self.status.setStyleSheet("font-size: 14pt; font-weight: 700; color: #FFB020;")
            self.badge.setText("TIMED OUT")
            self.badge.setStyleSheet("font-size: 11pt; font-weight: 800; letter-spacing: 2px; color: #FFB020;")
            self.poll_timer.stop()
            self.anim_timer.stop()
            return
        if self.elapsed % 2 == 0:
            try:
                from paystack_client import verify_transaction
                result = verify_transaction(self.secret_key, self.reference)
            except Exception as exc:
                self.status.setText(f"Checking... ({exc})")
                return
            if result.status == "success":
                self.final_status = "success"
                self.status.setText("Payment confirmed")
                self.status.setStyleSheet("font-size: 16pt; font-weight: 800; color: #00E5A8;")
                self.badge.setText("PAID")
                self.badge.setStyleSheet("font-size: 12pt; font-weight: 800; letter-spacing: 2px; color: #00E5A8;")
                self.poll_timer.stop()
                self.anim_timer.stop()
                QTimer.singleShot(700, self.accept)
            elif result.status in {"failed", "abandoned", "reversed"}:
                self.final_status = "failed"
                self.status.setText(f"Payment {result.status}")
                self.status.setStyleSheet("font-size: 14pt; font-weight: 700; color: #FF647C;")
                self.badge.setText("FAILED")
                self.badge.setStyleSheet("font-size: 11pt; font-weight: 800; letter-spacing: 2px; color: #FF647C;")
                self.poll_timer.stop()
                self.anim_timer.stop()
            else:
                self.status.setText(result.message or "Waiting for customer to complete payment...")

    def _cancel(self):
        self.final_status = "cancelled"
        self.poll_timer.stop()
        self.anim_timer.stop()
        self.reject()


class POSPage(QWidget):
    sale_completed = Signal(int)

    def __init__(
        self, ops: OperationsService, currency: str = "KES", tax_label: str = "Tax",
        tax_rate: Decimal = Decimal("0"), allow_negative_stock: bool = False, parent=None,
        tax_inclusive: bool = False, pre_sale_check=None, held_loader=None, held_saver=None,
        payment_config=None, manager_pin_checker=None, pos_config=None, app_theme: str = "light",
    ):
        super().__init__(parent)
        self.pre_sale_check = pre_sale_check
        self._held_memory = []
        self.held_loader = held_loader or (lambda: list(self._held_memory))
        self.held_saver = held_saver or (lambda items: setattr(self, "_held_memory", list(items)))
        self.ops = ops
        self.currency = currency
        self.tax_label = tax_label
        self.tax_rate = tax_rate
        self.tax_inclusive = bool(tax_inclusive)
        self.allow_negative_stock = allow_negative_stock
        if isinstance(payment_config, dict):
            import json as _json
            self.payment_config = load_providers_config(_json.dumps(payment_config))
        else:
            self.payment_config = load_providers_config(payment_config)
        self.manager_pin_checker = manager_pin_checker
        from pos_config import load_pos_config, DEFAULT_POS_CONFIG
        self.pos_config = pos_config if isinstance(pos_config, dict) else load_pos_config(None)
        for k, v in DEFAULT_POS_CONFIG.items():
            self.pos_config.setdefault(k, v)
        self.app_theme = app_theme if app_theme in {"light", "dark"} else "light"
        appearance = self.pos_config.get("appearance") or "immersive_dark"
        if appearance == "match_app":
            self.ui_mode = "immersive_dark" if self.app_theme == "dark" else "vibrant_light"
        else:
            self.ui_mode = appearance
        self.cart: List[CartLine] = []
        self.customer_name = str(self.pos_config.get("default_customer") or "Walk-in Customer")
        self.customer_id: Optional[int] = None
        self.discount_percent = Decimal("0")
        self.discount_amount = Decimal("0")
        self._pay_pulse = 0
        self._build_ui()
        self._wire_shortcuts()
        self._refresh_totals()
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_pay_button)
        if self.pos_config.get("pulse_pay_button", True):
            self._pulse_timer.start(900)
        self.search_input.setFocus()

    def _pulse_pay_button(self):
        if not self.cart:
            self.btn_pay.setStyleSheet(self._pay_style(False))
            return
        self._pay_pulse = 1 - self._pay_pulse
        self.btn_pay.setStyleSheet(self._pay_style(True, glow=bool(self._pay_pulse)))

    def _pay_style(self, enabled: bool, glow: bool = False) -> str:
        if not enabled:
            return ("QPushButton { background: #1E2A44; color: #6B7A99; border: none; "
                    "border-radius: 16px; font-size: 14pt; font-weight: 800; }")
        if glow:
            return ("QPushButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, "
                    "stop:0 #00E5A8, stop:1 #3B82F6); color: #041018; border: none; "
                    "border-radius: 16px; font-size: 15pt; font-weight: 900; padding: 8px; }"
                    "QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, "
                    "stop:0 #3B82F6, stop:1 #A855F7); color: white; }")
        return ("QPushButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, "
                "stop:0 #00C896, stop:1 #2563EB); color: white; border: none; "
                "border-radius: 16px; font-size: 15pt; font-weight: 900; padding: 8px; }"
                "QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, "
                "stop:0 #3B82F6, stop:1 #A855F7); }")


    def _stylesheet_for_mode(self) -> str:
        compact = bool(self.pos_config.get("compact_mode"))
        pad = "8px 10px" if compact else "10px 12px"
        radius = "12px" if compact else "18px"
        if self.ui_mode == "vibrant_light":
            return f"""
            QWidget#posRoot {{ background: #F3F5FA; }}
            QLineEdit, QComboBox, QDoubleSpinBox {{
                background: #FFFFFF; color: #0F172A; border: 1px solid #D0D7E6;
                border-radius: 12px; padding: {pad}; font-size: 11pt;
                selection-background-color: #3B82F6;
            }}
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border: 1px solid #3B82F6; }}
            QTableWidget {{
                background: #FFFFFF; color: #0F172A; border: 1px solid #D0D7E6;
                border-radius: 14px; gridline-color: #E6EBF5; font-size: 10.5pt;
            }}
            QTableWidget::item:selected {{ background: #DBEAFE; color: #0F172A; }}
            QHeaderView::section {{
                background: #EEF2FF; color: #475569; border: none;
                padding: 8px; font-weight: 700; font-size: 9pt;
            }}
            QLabel#heroTotal {{ color: #0F172A; font-size: 34pt; font-weight: 900; }}
            QLabel#sectionTitle {{ color: #0F172A; font-size: 15pt; font-weight: 800; }}
            QLabel#muted {{ color: #64748B; font-size: 9.5pt; }}
            QPushButton#ghost {{
                background: #FFFFFF; color: #1E293B; border: 1px solid #CBD5E1;
                border-radius: 12px; padding: 10px 14px; font-weight: 700;
            }}
            QPushButton#ghost:hover {{ border-color: #3B82F6; color: #1D4ED8; }}
            QPushButton#danger {{
                background: #FEF2F2; color: #B91C1C; border: 1px solid #FECACA;
                border-radius: 12px; padding: 10px 14px; font-weight: 700;
            }}
            QFrame#glass {{
                background: #FFFFFF; border: 1px solid #D0D7E6; border-radius: {radius};
            }}
            QFrame#neon {{
                background: #FFFFFF; border: 1px solid #BFDBFE; border-radius: {radius};
            }}
            """
        # immersive_dark (default)
        return f"""
            QWidget#posRoot {{ background: #070B16; }}
            QLineEdit, QComboBox, QDoubleSpinBox {{
                background: #121A2E; color: #F4F7FF; border: 1px solid #243049;
                border-radius: 12px; padding: {pad}; font-size: 11pt;
                selection-background-color: #3B82F6;
            }}
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border: 1px solid #3B82F6; }}
            QTableWidget {{
                background: #0C1324; color: #E8EEFF; border: 1px solid #1C2940;
                border-radius: 14px; gridline-color: #1C2940; font-size: 10.5pt;
            }}
            QTableWidget::item:selected {{ background: #1D3A6E; color: white; }}
            QHeaderView::section {{
                background: #121A2E; color: #8BA0C4; border: none;
                padding: 8px; font-weight: 700; font-size: 9pt;
            }}
            QLabel#heroTotal {{ color: #FFFFFF; font-size: 36pt; font-weight: 900; }}
            QLabel#sectionTitle {{ color: #F4F7FF; font-size: 15pt; font-weight: 800; }}
            QLabel#muted {{ color: #7E90B0; font-size: 9.5pt; }}
            QPushButton#ghost {{
                background: #121A2E; color: #C7D4F0; border: 1px solid #2A3A5C;
                border-radius: 12px; padding: 10px 14px; font-weight: 700;
            }}
            QPushButton#ghost:hover {{ border-color: #3B82F6; color: white; }}
            QPushButton#danger {{
                background: #2A1520; color: #FF8B9A; border: 1px solid #5A2434;
                border-radius: 12px; padding: 10px 14px; font-weight: 700;
            }}
            QFrame#glass {{
                background: #10182B; border: 1px solid #243049; border-radius: {radius};
            }}
            QFrame#neon {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #101B33, stop:1 #152038);
                border: 1px solid #2C4A7C; border-radius: {radius};
            }}
            """

    def _build_ui(self):
        self.setStyleSheet(self._stylesheet_for_mode())
        self.setObjectName("posRoot")
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(14)

        left_wrap = QFrame(); left_wrap.setObjectName("glass")
        left = QVBoxLayout(left_wrap)
        left.setContentsMargins(14, 14, 14, 14); left.setSpacing(10)
        search_lbl = QLabel("SCAN / SEARCH")
        search_lbl.setStyleSheet("color: #5EEAD4; font-size: 9pt; font-weight: 800; letter-spacing: 1.5px;")
        left.addWidget(search_lbl)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Scan barcode or type product name / SKU...")
        self.search_input.setMinimumHeight(46)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self._on_search_enter)
        self.search_input.textChanged.connect(self._on_search_text)
        left.addWidget(self.search_input)
        self.results = QTableWidget(0, 5)
        self.results.setHorizontalHeaderLabels(["SKU", "Product", "Price", "Stock", ""])
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.setAlternatingRowColors(True)
        self.results.cellDoubleClicked.connect(self._add_from_results)
        self.results.verticalHeader().setVisible(False)
        left.addWidget(self.results, 1)
        root.addWidget(left_wrap, 3)

        right_wrap = QFrame(); right_wrap.setObjectName("neon")
        right = QVBoxLayout(right_wrap)
        right.setContentsMargins(16, 16, 16, 16); right.setSpacing(10)
        top = QHBoxLayout()
        title = QLabel("CURRENT SALE"); title.setObjectName("sectionTitle")
        top.addWidget(title); top.addStretch()
        self.live_dot = QLabel("LIVE TILL")
        self.live_dot.setStyleSheet("color: #00E5A8; font-weight: 800; font-size: 9pt; letter-spacing: 1px;")
        if self.pos_config.get("show_live_badge", True):
            top.addWidget(self.live_dot)
        right.addLayout(top)
        self.customer_edit = QLineEdit(self.customer_name)
        self.customer_edit.setPlaceholderText("Customer name")
        self.customer_edit.editingFinished.connect(self._update_customer)
        right.addWidget(self.customer_edit)
        self.cart_table = QTableWidget(0, 6)
        self.cart_table.setHorizontalHeaderLabels(["SKU", "Item", "Price", "Qty", "Total", ""])
        self.cart_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.cart_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cart_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.cart_table.verticalHeader().setVisible(False)
        right.addWidget(self.cart_table, 2)

        disc = QHBoxLayout()
        self.discount_pct = QDoubleSpinBox(); self.discount_pct.setRange(0, 100); self.discount_pct.setSuffix(" % off")
        self.discount_pct.valueChanged.connect(self._on_discount_changed); disc.addWidget(self.discount_pct)
        self.discount_amt = QDoubleSpinBox(); self.discount_amt.setRange(0, 10_000_000)
        self.discount_amt.setPrefix(f"{self.currency} ")
        self.discount_amt.valueChanged.connect(self._on_discount_amount_changed); disc.addWidget(self.discount_amt)
        self.discount_reason_edit = QLineEdit(); self.discount_reason_edit.setPlaceholderText("Discount reason")
        disc.addWidget(self.discount_reason_edit, 1)
        if self.pos_config.get("show_discount_row", True):
            right.addLayout(disc)
        if not self.payment_config.get("allow_discounts", True):
            self.discount_pct.setEnabled(False); self.discount_amt.setEnabled(False)

        hero = QFrame(); hero.setObjectName("glass")
        hero_l = QVBoxLayout(hero); hero_l.setContentsMargins(16, 12, 16, 12)
        self.lbl_sub_meta = QLabel("Subtotal 0.00 · Tax 0.00 · Discount 0.00"); self.lbl_sub_meta.setObjectName("muted")
        hero_l.addWidget(self.lbl_sub_meta)
        self.lbl_total = QLabel(f"{self.currency} 0.00"); self.lbl_total.setObjectName("heroTotal")
        self.lbl_total.setAlignment(Qt.AlignRight | Qt.AlignVCenter); hero_l.addWidget(self.lbl_total)
        right.addWidget(hero)

        pay_grid = QGridLayout()
        pay_grid.addWidget(QLabel("Tendered"), 0, 0)
        self.tendered = QDoubleSpinBox(); self.tendered.setRange(0, 10_000_000); self.tendered.setDecimals(2)
        self.tendered.setPrefix(f"{self.currency} "); self.tendered.valueChanged.connect(self._update_change)
        pay_grid.addWidget(self.tendered, 0, 1)
        pay_grid.addWidget(QLabel("Method"), 1, 0)
        self.payment_method = QComboBox()
        methods = list(enabled_payment_methods(self.payment_config))
        if "Credit / unpaid" not in methods and "Customer account (credit)" not in methods:
            methods.append("Credit / unpaid")
        self.payment_method.addItems(methods)
        self.payment_method.currentTextChanged.connect(self._payment_method_changed)
        pay_grid.addWidget(self.payment_method, 1, 1)
        pay_grid.addWidget(QLabel("M-Pesa phone"), 2, 0)
        self.phone_input = QLineEdit(); self.phone_input.setPlaceholderText("07XX XXX XXX — for Paystack prompt")
        pay_grid.addWidget(self.phone_input, 2, 1)
        pay_grid.addWidget(QLabel("Reference"), 3, 0)
        self.payment_reference = QLineEdit(); self.payment_reference.setPlaceholderText("Code / auth (if needed)")
        pay_grid.addWidget(self.payment_reference, 3, 1)
        pay_grid.addWidget(QLabel("Warranty *"), 4, 0)
        self.warranty = QComboBox()
        self.warranty.setEditable(True)
        self.warranty.addItems(["1 month", "3 months", "6 months", "1 year", "N/A"])
        self.warranty.setCurrentText("N/A")
        self.warranty.setToolTip("Mandatory. Choose a standard period or type a custom one (e.g. 18 months).")
        pay_grid.addWidget(self.warranty, 4, 1)
        right.addLayout(pay_grid)

        self.lbl_change = QLabel("Change: 0.00")
        self.lbl_change.setStyleSheet("font-size: 13pt; font-weight: 800; color: #5EEAD4;")
        right.addWidget(self.lbl_change)

        self.btn_pay = QPushButton("COMPLETE SALE  ·  F9")
        self.btn_pay.setMinimumHeight(56 if self.pos_config.get("large_pay_button", True) else 44); self.btn_pay.setCursor(Qt.PointingHandCursor)
        self.btn_pay.clicked.connect(self._complete_sale)
        self.btn_pay.setStyleSheet(self._pay_style(False)); right.addWidget(self.btn_pay)

        actions = QHBoxLayout()
        self.btn_hold = QPushButton("Hold  F6"); self.btn_hold.setObjectName("ghost"); self.btn_hold.clicked.connect(self._hold_cart); actions.addWidget(self.btn_hold)
        self.btn_recall = QPushButton("Recall  F7"); self.btn_recall.setObjectName("ghost"); self.btn_recall.clicked.connect(self._recall_cart); actions.addWidget(self.btn_recall)
        self.btn_clear = QPushButton("Clear  Esc"); self.btn_clear.setObjectName("danger"); self.btn_clear.clicked.connect(self._clear_cart); actions.addWidget(self.btn_clear)
        right.addLayout(actions)
        hint = QLabel("Enter adds · F9 pays · Paystack M-Pesa auto-detects when customer confirms on phone")
        hint.setObjectName("muted"); right.addWidget(hint)
        root.addWidget(right_wrap, 2)
        self._payment_method_changed(self.payment_method.currentText())

    def _payment_method_changed(self, method: str):
        self.phone_input.setVisible("M-Pesa (Paystack)" in (method or ""))
        if provider_requires_reference(method, self.payment_config):
            self.payment_reference.setPlaceholderText(f"Required — {method} reference")
        else:
            self.payment_reference.setPlaceholderText("Optional reference")
        self._update_change()

    def _wire_shortcuts(self):
        QShortcut(QKeySequence("F9"), self, self._complete_sale)
        QShortcut(QKeySequence("F6"), self, self._hold_cart)
        QShortcut(QKeySequence("F7"), self, self._recall_cart)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._clear_cart)

    def _on_search_text(self, text: str):
        text = (text or "").strip()
        if len(text) < 1:
            self.results.setRowCount(0); return
        try:
            hits = self.ops.search_products(text, limit=40)
        except Exception:
            hits = []
        # Defensive: ensure mapping objects (sqlite3.Row has no .get).
        hits = [dict(p) if not isinstance(p, dict) else p for p in hits]
        self.results.setRowCount(len(hits))
        for row, product in enumerate(hits):
            sku = str(product.get("sku") or ""); name = str(product.get("name") or "")
            price = Decimal(str(product.get("selling_price") or product.get("unit_price") or 0))
            stock = Decimal(str(product.get("stock_on_hand") or product.get("quantity_on_hand") or product.get("stock") or 0))
            self.results.setItem(row, 0, QTableWidgetItem(sku))
            self.results.setItem(row, 1, QTableWidgetItem(name))
            self.results.setItem(row, 2, QTableWidgetItem(f"{price:.2f}"))
            self.results.setItem(row, 3, QTableWidgetItem(f"{stock:.0f}"))
            btn = QPushButton("ADD"); btn.setObjectName("ghost")
            btn.clicked.connect(lambda checked=False, p=product: self._add_product(p))
            self.results.setCellWidget(row, 4, btn)
            for c in range(4):
                item = self.results.item(row, c)
                if item: item.setData(Qt.UserRole, product)

    def _on_search_enter(self):
        text = self.search_input.text().strip()
        if not text: return
        try:
            hits = self.ops.search_products(text, limit=10)
        except Exception:
            hits = []
        hits = [dict(p) if not isinstance(p, dict) else p for p in hits]
        if not hits:
            QMessageBox.information(self, "Not found", f"No product matched '{text}'."); return
        exact = [p for p in hits if str(p.get("sku") or "").lower() == text.lower()]
        pick = exact[0] if len(exact) == 1 else (hits[0] if len(hits) == 1 else None)
        if pick:
            self._add_product(pick); self.search_input.clear(); return
        self._on_search_text(text); self.results.selectRow(0)

    def _add_from_results(self, row: int, _col: int):
        item = self.results.item(row, 0)
        if item and item.data(Qt.UserRole):
            self._add_product(item.data(Qt.UserRole))

    def _add_product(self, product: dict):
        product_id = int(product.get("id") or product.get("product_id") or 0)
        sku = str(product.get("sku") or ""); name = str(product.get("name") or "")
        price = Decimal(str(product.get("unit_price") or product.get("selling_price") or 0)).quantize(Decimal("0.01"))
        stock = Decimal(str(product.get("stock_on_hand") or product.get("quantity_on_hand") or product.get("stock") or 0))
        if product_id <= 0: return
        if not self.allow_negative_stock and stock <= 0:
            QMessageBox.warning(self, "Out of stock", f"{name} has no sellable stock."); return
        track_serials = bool(product.get("track_serials"))
        serial = ""
        if track_serials:
            serial, ok = QInputDialog.getText(
                self, "Serial / IMEI",
                f"{name}\nScan or type the serial / IMEI for this unit:",
            )
            if not ok:
                return
            serial = "".join(ch for ch in serial.strip().upper() if ch.isalnum() or ch in "-_")
            if len(serial) < 3:
                QMessageBox.warning(self, "Serial required", "Enter a valid serial / IMEI (at least 3 characters).")
                return
            # Serialised items are always qty 1 and never merge
            for line in self.cart:
                if line.serial and line.serial.upper() == serial:
                    QMessageBox.warning(self, "Already in cart", f"Serial {serial} is already on this sale.")
                    return
            self.cart.append(CartLine(product_id, sku, name, price, Decimal("1"), stock, serial))
            self._rebuild_cart_table(); self._refresh_totals(); self._flash_total()
            self.search_input.clear(); self.search_input.setFocus(); return
        for line in self.cart:
            if line.product_id == product_id and not line.serial:
                new_qty = line.quantity + Decimal("1")
                if not self.allow_negative_stock and new_qty > stock:
                    QMessageBox.warning(self, "Stock limit", f"Only {stock:.0f} available for {name}."); return
                line.quantity = new_qty
                self._rebuild_cart_table(); self._refresh_totals(); self._flash_total()
                self.search_input.clear(); self.search_input.setFocus(); return
        self.cart.append(CartLine(product_id, sku, name, price, Decimal("1"), stock))
        self._rebuild_cart_table(); self._refresh_totals(); self._flash_total()
        self.search_input.clear(); self.search_input.setFocus()

    def _flash_total(self):
        effect = QGraphicsOpacityEffect(self.lbl_total)
        self.lbl_total.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(280); anim.setStartValue(0.35); anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic); anim.start(); self._total_anim = anim

    def _rebuild_cart_table(self):
        self.cart_table.setRowCount(len(self.cart))
        for idx, line in enumerate(self.cart):
            self.cart_table.setItem(idx, 0, QTableWidgetItem(line.sku))
            self.cart_table.setItem(idx, 1, QTableWidgetItem(line.name + (f" [{line.serial}]" if line.serial else "")))
            self.cart_table.setItem(idx, 2, QTableWidgetItem(f"{line.unit_price:.2f}"))
            self.cart_table.setItem(idx, 3, QTableWidgetItem(f"{line.quantity:.0f}"))
            self.cart_table.setItem(idx, 4, QTableWidgetItem(f"{line.line_total:.2f}"))
            btn = QPushButton("X"); btn.setFixedWidth(34); btn.setObjectName("danger")
            btn.clicked.connect(lambda checked=False, i=idx: self._remove_line(i))
            self.cart_table.setCellWidget(idx, 5, btn)

    def _remove_line(self, index: int):
        if 0 <= index < len(self.cart):
            del self.cart[index]; self._rebuild_cart_table(); self._refresh_totals()

    def _clear_cart(self):
        if not self.cart: return
        if QMessageBox.question(self, "Clear cart", "Remove all items from this sale?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.cart.clear(); self.discount_pct.setValue(0); self.discount_amt.setValue(0)
        self.discount_reason_edit.clear(); self._rebuild_cart_table(); self._refresh_totals()
        self.search_input.setFocus()

    def _update_customer(self):
        name = self.customer_edit.text().strip()
        self.customer_name = name or "Walk-in Customer"
        if not name: self.customer_edit.setText(self.customer_name)

    def _on_discount_changed(self, value: float):
        self.discount_percent = Decimal(str(value)).quantize(Decimal("0.01"))
        if self.discount_percent > 0:
            self.discount_amt.blockSignals(True); self.discount_amt.setValue(0); self.discount_amt.blockSignals(False)
            self.discount_amount = Decimal("0")
        self._refresh_totals()

    def _on_discount_amount_changed(self, value: float):
        self.discount_amount = Decimal(str(value)).quantize(Decimal("0.01"))
        if self.discount_amount > 0:
            self.discount_pct.blockSignals(True); self.discount_pct.setValue(0); self.discount_pct.blockSignals(False)
            self.discount_percent = Decimal("0")
        self._refresh_totals()

    def _discount_value(self, subtotal: Decimal) -> Decimal:
        if self.discount_percent > 0:
            return (subtotal * self.discount_percent / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return min(self.discount_amount, subtotal)

    def _require_manager_for_discount(self, discount: Decimal, subtotal: Decimal) -> bool:
        if discount <= 0: return True
        max_pct = Decimal(str(self.payment_config.get("max_discount_percent") or 25))
        pct = (discount / subtotal * Decimal("100")) if subtotal else Decimal("0")
        pin = str(self.payment_config.get("manager_pin") or "").strip()
        if not pin:
            if pct > max_pct:
                QMessageBox.warning(self, "Discount too high", f"Maximum allowed discount is {max_pct:.0f}%."); return False
            return True
        if pct <= max_pct and not self.payment_config.get("require_discount_reason"):
            return True
        entered, ok = QInputDialog.getText(self, "Manager approval", f"Discount {pct:.1f}%. Enter manager PIN:", echo=QLineEdit.Password)
        if not ok: return False
        if self.manager_pin_checker:
            if not self.manager_pin_checker(entered):
                QMessageBox.warning(self, "Denied", "Incorrect manager PIN."); return False
        elif entered != pin:
            QMessageBox.warning(self, "Denied", "Incorrect manager PIN."); return False
        return True

    def _cart_totals(self):
        subtotal = sum((line.line_total for line in self.cart), Decimal("0")).quantize(Decimal("0.01"))
        discount = self._discount_value(subtotal)
        after = (subtotal - discount).quantize(Decimal("0.01"))
        if self.tax_inclusive and self.tax_rate:
            net = (after / (Decimal("1") + self.tax_rate / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            tax = (after - net).quantize(Decimal("0.01"))
            return subtotal, discount, tax, after
        tax = (after * self.tax_rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if self.tax_rate else Decimal("0.00")
        total = (after + tax).quantize(Decimal("0.01"))
        return subtotal, discount, tax, total

    def _refresh_totals(self):
        subtotal, discount, tax, total = self._cart_totals()
        self.lbl_sub_meta.setText(
            f"Subtotal {self.currency} {subtotal:.2f}   ·   Discount {self.currency} {discount:.2f}   ·   {self.tax_label} {self.currency} {tax:.2f}")
        self.lbl_total.setText(f"{self.currency} {total:.2f}")
        if self.cart:
            self.tendered.blockSignals(True); self.tendered.setValue(float(total)); self.tendered.blockSignals(False)
        self._update_change()
        self.btn_pay.setEnabled(bool(self.cart))
        self.btn_pay.setStyleSheet(self._pay_style(bool(self.cart)))

    def _update_change(self):
        _s, _d, _t, total = self._cart_totals()
        tendered = Decimal(str(self.tendered.value())).quantize(Decimal("0.01"))
        change = (tendered - total).quantize(Decimal("0.01"))
        method = self.payment_method.currentText()
        if provider_supports_auto_detect(method):
            self.lbl_change.setText("Phone prompt · auto-detects payment")
            self.lbl_change.setStyleSheet("font-size: 13pt; font-weight: 800; color: #7DD3FC;")
        elif not provider_supports_change(method):
            self.lbl_change.setText(f"Confirm {method}: {self.currency} {total:.2f}")
            self.lbl_change.setStyleSheet("font-size: 13pt; font-weight: 800; color: #9FB0D0;")
        elif change < 0:
            self.lbl_change.setText(f"Still due: {self.currency} {abs(change):.2f}")
            self.lbl_change.setStyleSheet("font-size: 13pt; font-weight: 800; color: #FF647C;")
        else:
            self.lbl_change.setText(f"Change: {self.currency} {change:.2f}")
            self.lbl_change.setStyleSheet("font-size: 13pt; font-weight: 800; color: #00E5A8;")

    def _hold_cart(self):
        if not self.cart:
            QMessageBox.information(self, "Nothing to hold", "Add items first."); return
        held = self.held_loader()
        held.append({
            "at": __import__("datetime").datetime.now().strftime("%H:%M"),
            "customer": self.customer_name,
            "discount_percent": str(self.discount_percent), "discount_amount": str(self.discount_amount),
            "discount_reason": self.discount_reason_edit.text().strip(),
            "lines": [{"product_id": l.product_id, "sku": l.sku, "name": l.name,
                       "unit_price": str(l.unit_price), "quantity": str(l.quantity),
                       "stock": str(l.stock), "serial": l.serial} for l in self.cart],
        })
        self.held_saver(held); self.cart.clear(); self.discount_pct.setValue(0); self.discount_amt.setValue(0)
        self.discount_reason_edit.clear(); self._rebuild_cart_table(); self._refresh_totals()
        self.btn_recall.setText(f"Recall ({len(held)})  F7"); self.search_input.setFocus()

    def _recall_cart(self):
        held = self.held_loader()
        if not held:
            QMessageBox.information(self, "No held sales", "Nothing parked."); return
        if self.cart:
            QMessageBox.information(self, "Finish current sale", "Hold or clear before recalling."); return
        ticket = held.pop(); self.held_saver(held)
        self.customer_name = ticket.get("customer") or "Walk-in Customer"
        self.customer_edit.setText(self.customer_name)
        self.cart = [CartLine(int(l["product_id"]), l["sku"], l["name"], Decimal(str(l["unit_price"])),
                              Decimal(str(l["quantity"])), Decimal(str(l.get("stock") or 0)), l.get("serial") or "")
                     for l in ticket.get("lines") or []]
        try:
            self.discount_pct.setValue(float(ticket.get("discount_percent") or 0))
            self.discount_amt.setValue(float(ticket.get("discount_amount") or 0))
        except (TypeError, ValueError):
            pass
        self.discount_reason_edit.setText(ticket.get("discount_reason") or "")
        self._rebuild_cart_table(); self._refresh_totals()
        self.btn_recall.setText(f"Recall ({len(held)})  F7" if held else "Recall  F7")
        self.search_input.setFocus()

    def _complete_sale(self):
        if not self.cart: return
        if callable(self.pre_sale_check):
            block = self.pre_sale_check() or ""
            if block:
                QMessageBox.warning(self, "Sale blocked", block); return
        subtotal, discount, tax, total = self._cart_totals()
        if discount > 0:
            if self.payment_config.get("require_discount_reason") and not self.discount_reason_edit.text().strip():
                QMessageBox.warning(self, "Reason required", "Enter a discount reason.")
                self.discount_reason_edit.setFocus(); return
            if not self._require_manager_for_discount(discount, subtotal): return

        method = self.payment_method.currentText()
        reference = self.payment_reference.text().strip()
        tendered = Decimal(str(self.tendered.value())).quantize(Decimal("0.01"))

        if provider_supports_auto_detect(method) and "M-Pesa" in method:
            secret = paystack_secret_key(self.payment_config)
            if not secret:
                QMessageBox.warning(self, "Paystack not configured",
                    "Add your Paystack Secret Key under System Settings → Configure payment providers."); return
            phone = self.phone_input.text().strip()
            if not phone:
                QMessageBox.warning(self, "Phone required", "Enter the customer's M-Pesa number.")
                self.phone_input.setFocus(); return
            try:
                from paystack_client import initiate_mpesa_charge
                creds = provider_credentials(method, self.payment_config)
                email = creds.get("callback_email") or "customer@pos.local"
                result = initiate_mpesa_charge(secret, total, phone, email=email, currency=self.currency)
            except Exception as exc:
                QMessageBox.warning(self, "Paystack error", str(exc)); return
            if not result.ok and result.status == "error":
                QMessageBox.warning(self, "Could not start payment", result.message); return
            timeout = int(self.payment_config.get("auto_detect_timeout_sec") or 90)
            wait = PaymentWaitDialog(self, self.currency, total, phone, result.reference, secret, timeout_sec=timeout)
            wait.exec()
            if wait.final_status != "success":
                if wait.final_status == "cancelled":
                    QMessageBox.information(self, "Cancelled", "Payment wait cancelled. Sale not recorded.")
                elif wait.final_status == "timeout":
                    QMessageBox.warning(self, "Not confirmed", "No payment was detected in time. Sale not recorded.")
                else:
                    QMessageBox.warning(self, "Payment failed", "Customer did not complete payment.")
                return
            reference = result.reference
            tendered = total
        is_credit = method in {"Credit / unpaid", "Customer account (credit)", "Credit", "Account", "Unpaid"}
        if is_credit:
            if self.customer_name.strip().lower() in {"", "walk-in customer"}:
                QMessageBox.warning(self, "Customer required", "Credit sales need a named customer.")
                self.customer_edit.setFocus(); return
            # On credit: tendered is treated as a deposit (can be zero)
            amount_paid = min(tendered, total)
        else:
            if tendered < total:
                QMessageBox.warning(self, "Insufficient payment",
                    f"Tendered ({self.currency} {tendered:.2f}) is less than total ({self.currency} {total:.2f})."); return
            amount_paid = total
        if provider_requires_reference(method, self.payment_config) and not reference and not is_credit:
            QMessageBox.warning(self, "Reference required", f"{method} needs a reference / code.")
            self.payment_reference.setFocus(); return

        warranty_period = (self.warranty.currentText() or "").strip()
        if not warranty_period:
            QMessageBox.warning(self, "Warranty required",
                "Select or type a warranty period (1 month, 3 months, 6 months, 1 year, N/A, or custom).")
            self.warranty.setFocus(); return

        lines = [{"product_id": line.product_id, "quantity": line.quantity, "unit_price": line.unit_price,
                  "name": line.name, "sku": line.sku, "serial": line.serial} for line in self.cart]
        note_bits = []
        if discount > 0:
            note_bits.append(f"Discount {self.currency} {discount:.2f}")
            if self.discount_reason_edit.text().strip(): note_bits.append(self.discount_reason_edit.text().strip())
        if reference: note_bits.append(reference)
        payment_reference = " · ".join(note_bits)

        # Served-by snapshot from the logged-in operator (passed by the host App when available)
        served_name = getattr(self, "served_by_name", "") or ""
        served_code = getattr(self, "served_by_code", "") or ""

        try:
            sale_id = self.ops.record_sale(
                customer_name=self.customer_name, lines=lines, amount_paid=float(amount_paid),
                customer_id=self.customer_id, payment_method=method, payment_reference=payment_reference,
                warranty_period=warranty_period, served_by_name=served_name, served_by_code=served_code)
        except OperationError as exc:
            QMessageBox.warning(self, "Sale failed", str(exc)); return
        except Exception as exc:
            QMessageBox.warning(self, "Sale failed", f"Unexpected error: {exc}"); return

        change = (tendered - total).quantize(Decimal("0.01")) if not is_credit else Decimal("0.00")
        balance = (total - amount_paid).quantize(Decimal("0.01"))
        msg = f"Sale #{sale_id}\nTotal {self.currency} {total:.2f}\n{method}"
        if reference: msg += f"\nRef {reference}"
        if is_credit and balance > 0:
            msg += f"\nBalance due {self.currency} {balance:.2f}"
        if provider_supports_change(method) and change > 0: msg += f"\nChange {self.currency} {change:.2f}"
        msg += f"\nWarranty: {warranty_period}"
        QMessageBox.information(self, "Sale complete", msg)
        # Persist serial / IMEI units against the sale
        try:
            for line in list(self.cart):
                if line.serial:
                    self.ops.mark_serial_sold(line.product_id, line.serial, int(sale_id))
        except Exception:
            pass
        self.sale_completed.emit(int(sale_id) if sale_id else 0)
        self.cart.clear(); self.discount_pct.setValue(0); self.discount_amt.setValue(0)
        self.discount_reason_edit.clear(); self.payment_reference.clear(); self.phone_input.clear()
        self.warranty.setCurrentText("N/A")
        self.payment_method.setCurrentIndex(0)
        self.customer_name = "Walk-in Customer"; self.customer_edit.setText(self.customer_name)
        self._rebuild_cart_table(); self._refresh_totals(); self.search_input.setFocus()
