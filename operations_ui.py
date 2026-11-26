"""Small reusable PySide6 dialogs for stock-backed documents."""
from decimal import Decimal, ROUND_HALF_UP
from service_catalog import SERVICE_OPTIONS, suggested_work_kind
from PySide6.QtCore import QDate, Qt, QDate
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame, QLineEdit,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QTableWidget, QTextEdit,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


class StockTransactionDialog(QDialog):
    def __init__(self, products, parties, mode, currency="KES", parent=None, document_type="transaction", tax_rate=0, tax_label="Tax"):
        super().__init__(parent)
        if mode not in {"sale","purchase"}:
            raise ValueError("mode must be sale or purchase")
        if document_type not in {"transaction", "order", "quote"}:
            raise ValueError("Unsupported stock document type")
        if (document_type == "order" and mode != "purchase") or (document_type == "quote" and mode != "sale"):
            raise ValueError("Quotes require sales mode and purchase orders require purchase mode")
        self.mode=mode
        self.document_type=document_type
        self.tax_rate=Decimal(str(tax_rate)) if mode=="sale" else Decimal("0")
        if not self.tax_rate.is_finite() or self.tax_rate<0 or self.tax_rate>100:
            raise ValueError("Tax rate must be between 0 and 100%.")
        self.tax_label=str(tax_label or "Tax")[:60]
        self.products=[dict(p) for p in products]
        self.product_by_id={int(p["id"]):p for p in self.products}
        is_sale=mode=="sale"
        self.setWindowTitle(("New quotation" if document_type == "quote" else "New sale") if is_sale else ("Create purchase order" if document_type == "order" else "Receive purchase"))
        self.setMinimumSize(760,520)
        outer=QVBoxLayout(self)
        form=QFormLayout()
        self.party=QComboBox()
        self.party.setEditable(True)
        self.party.setInsertPolicy(QComboBox.NoInsert)
        self.party.setPlaceholderText("Walk-in customer" if is_sale else "Supplier name")
        self.party.addItem("Walk-in customer" if is_sale else "",None)
        for row in parties:
            self.party.addItem(row["name"],int(row["id"]))
        form.addRow("Customer" if is_sale else "Supplier",self.party)
        outer.addLayout(form)

        self.has_batch_columns=(mode=="purchase" and document_type=="transaction")
        self.table=QTableWidget(0,7 if self.has_batch_columns else 5)
        headers=["SKU","PRODUCT","QUANTITY", "UNIT PRICE" if is_sale else "UNIT COST","LINE TOTAL"]
        if self.has_batch_columns: headers += ["BATCH / LOT","EXPIRY (YYYY-MM-DD)"]
        self.table.setHorizontalHeaderLabels(headers)
        self.batch_controls={}
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        outer.addWidget(self.table,1)

        toolbar=QHBoxLayout()
        add=QPushButton("Add product line")
        add.clicked.connect(self.add_line)
        remove=QPushButton("Remove selected line")
        remove.clicked.connect(self.remove_line)
        toolbar.addWidget(add); toolbar.addWidget(remove); toolbar.addStretch()
        outer.addLayout(toolbar)

        self.total_label=QLabel()
        self.total_label.setAlignment(Qt.AlignRight)
        self.total_label.setStyleSheet("font-size:16pt;font-weight:800;padding:6px;")
        outer.addWidget(self.total_label)

        self.payment_container=QWidget()
        payment=QFormLayout(self.payment_container)
        self.paid=QDoubleSpinBox()
        self.paid.setDecimals(2); self.paid.setMaximum(999999999999.99); self.paid.setPrefix(f"{currency} "); self.paid.setSingleStep(100)
        self._automatic_payment=True; self._setting_automatic_payment=False
        self.paid.valueChanged.connect(self._payment_changed)
        payment.addRow("Amount received" if is_sale else "Amount paid",self.paid)
        self.payment_method=QComboBox()
        self.payment_method.addItems(["Cash", "M-Pesa", "Bank transfer", "Card", "Other"])
        self.payment_reference=QLineEdit()
        self.payment_reference.setPlaceholderText("M-Pesa code / bank reference when applicable")
        self.payment_method.currentTextChanged.connect(self._payment_method_changed)
        payment.addRow("Payment method",self.payment_method)
        payment.addRow("Payment reference",self.payment_reference)
        outer.addWidget(self.payment_container)
        if document_type in {"order", "quote"}:
            self.payment_container.hide()
        self.expires_on=None
        if document_type=="quote":
            self.expires_on=QDateEdit()
            self.expires_on.setCalendarPopup(True)
            self.expires_on.setDisplayFormat("yyyy-MM-dd")
            self.expires_on.setDate(QDate.currentDate().addDays(30))
            expiry_form=QFormLayout()
            expiry_form.addRow("Quotation valid through",self.expires_on)
            outer.addLayout(expiry_form)

        buttons=QDialogButtonBox()
        submit_text=("Save quotation" if document_type == "quote" else "Complete sale") if is_sale else ("Save purchase order" if document_type == "order" else "Receive purchase")
        self.submit=buttons.addButton(submit_text,QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.add_line()

    def _payment_method_changed(self, method):
        if hasattr(self, "payment_reference"):
            self.payment_reference.setPlaceholderText(
                "Enter confirmed M-Pesa transaction code" if method == "M-Pesa" else
                "Bank / card reference (optional)" if method in {"Bank transfer", "Card"} else
                "Reference (optional)")

    def add_line(self):
        if not self.products:
            QMessageBox.information(self,"No products","Create a product before recording this transaction.")
            return
        row=self.table.rowCount()
        self.table.insertRow(row)
        product_box=QComboBox()
        product_box.setEditable(True)
        product_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        product_box.lineEdit().setPlaceholderText("Select product or scan barcode")
        for product in self.products:
            stock=Decimal(str(product.get("stock_on_hand","0")))
            barcode=str(product.get("barcode", "")).strip()
            prefix=f"{barcode} · " if barcode else ""
            product_box.addItem(f"{prefix}{product['sku']} — {product['name']} ({stock:g} in stock)",int(product["id"]))
        quantity=QDoubleSpinBox()
        quantity.setDecimals(3); quantity.setRange(0.001,999999999.999); quantity.setValue(1); quantity.setSingleStep(1)
        price=QDoubleSpinBox()
        price.setDecimals(2); price.setRange(0,999999999999.99); price.setSingleStep(1)
        sku_item=QTableWidgetItem()
        sku_item.setFlags(sku_item.flags() & ~Qt.ItemIsEditable)
        total_item=QTableWidgetItem()
        total_item.setTextAlignment(Qt.AlignRight|Qt.AlignVCenter)
        total_item.setFlags(total_item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row,0,sku_item)
        self.table.setCellWidget(row,1,product_box)
        self.table.setCellWidget(row,2,quantity)
        self.table.setCellWidget(row,3,price)
        self.table.setItem(row,4,total_item)
        if self.has_batch_columns:
            batch_number=QLineEdit(); batch_number.setPlaceholderText("Required if batch tracked")
            expiry_date=QLineEdit(); expiry_date.setPlaceholderText("Optional YYYY-MM-DD")
            self.table.setCellWidget(row,5,batch_number); self.table.setCellWidget(row,6,expiry_date)
            self.batch_controls[row]=(batch_number,expiry_date)
        product_box.currentIndexChanged.connect(lambda _=0, box=product_box:self._product_changed(box))
        product_box.lineEdit().returnPressed.connect(lambda box=product_box:self._select_scanned_product(box))
        quantity.valueChanged.connect(self.recalculate)
        price.valueChanged.connect(self.recalculate)
        self._product_changed(product_box)

    def _select_scanned_product(self, box):
        query=box.currentText().strip().casefold()
        product=next((item for item in self.products if query and query in {
            str(item.get("barcode", "")).strip().casefold(),
            str(item.get("sku", "")).strip().casefold(),
        }),None)
        if not product:
            box.setCurrentIndex(-1)
            return False
        index=box.findData(int(product["id"]))
        if index < 0:
            return False
        box.setCurrentIndex(index)
        return True

    def _product_changed(self, box):
        row=self.table.indexAt(box.pos()).row()
        if row < 0:
            return
        product_id=box.currentData()
        product=self.product_by_id.get(int(product_id)) if product_id is not None else None
        if not product:
            return
        self.table.item(row,0).setText(product["sku"])
        cost_key="selling_price" if self.mode=="sale" else "cost_price"
        self.table.cellWidget(row,3).setValue(float(product[cost_key]))
        if self.has_batch_columns:
            batch=self.table.cellWidget(row,5); expiry=self.table.cellWidget(row,6)
            tracked=bool(product.get("track_batches",0))
            batch.setEnabled(tracked); expiry.setEnabled(tracked)
            batch.setPlaceholderText("Required for tracked stock" if tracked else "Not required")
            expiry.setPlaceholderText("Optional YYYY-MM-DD" if tracked else "Not required")
        self.recalculate()

    def remove_line(self):
        row=self.table.currentRow()
        if row < 0:
            QMessageBox.information(self,"Select a line","Select a product line to remove.")
            return
        self.table.removeRow(row)
        self.recalculate()

    def recalculate(self):
        subtotal=Decimal("0")
        total=Decimal("0")
        for row in range(self.table.rowCount()):
            qty=self.table.cellWidget(row,2)
            price=self.table.cellWidget(row,3)
            amount=Decimal(str(qty.value()))*Decimal(str(price.value()))
            amount=amount.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            line_tax=(amount*self.tax_rate/Decimal("100")).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            line_total=amount+line_tax
            self.table.item(row,4).setText(f"{line_total:,.2f}")
            subtotal+=amount
            total+=line_total
        if self.mode=="sale" and self.tax_rate:
            self.total_label.setText(f"Subtotal {self.paid.prefix()}{subtotal:,.2f}  ·  {self.tax_label} {self.tax_rate:g}%  ·  Total {self.paid.prefix()}{total:,.2f}")
        else:
            self.total_label.setText(f"Total: {self.paid.prefix()}{total:,.2f}")
        if self.document_type == "transaction" and self._automatic_payment:
            self._setting_automatic_payment=True
            self.paid.setValue(float(total))
            self._setting_automatic_payment=False

    def _payment_changed(self,_value):
        if not self._setting_automatic_payment:
            self._automatic_payment=False

    def transaction_data(self):
        lines=[]
        for row in range(self.table.rowCount()):
            product=self.table.cellWidget(row,1)
            quantity=self.table.cellWidget(row,2)
            price=self.table.cellWidget(row,3)
            product_id=product.currentData()
            if product_id is None:
                raise ValueError("Choose a product on every line.")
            line={
                "product_id":int(product_id),
                "quantity":quantity.value(),
                "unit_price" if self.mode=="sale" else "unit_cost":price.value(),
            }
            if self.has_batch_columns and bool(self.product_by_id[int(product_id)].get("track_batches",0)):
                line["batch_number"]=self.table.cellWidget(row,5).text().strip()
                line["expiry_date"]=self.table.cellWidget(row,6).text().strip()
                if not line["batch_number"]:
                    raise ValueError(f"Enter a batch / lot number for {self.product_by_id[int(product_id)]['name']}.")
            lines.append(line)
        party_name=self.party.currentText().strip()
        party_id=self.party.currentData()
        if party_id is not None:
            try:
                matched=self.party.findText(party_name)
                if matched < 0 or self.party.itemData(matched) != party_id:
                    party_id=None
            except Exception:
                party_id=None
        return {
            "party_name":party_name,
            "party_id":party_id,
            "amount_paid":self.paid.value() if self.document_type == "transaction" else 0,
            "payment_method":self.payment_method.currentText() if self.document_type == "transaction" else "Cash",
            "payment_reference":self.payment_reference.text().strip() if self.document_type == "transaction" else "",
            "expires_on":self.expires_on.date().toString("yyyy-MM-dd") if self.expires_on is not None else "",
            "lines":lines,
        }


class PurchaseOrderReceiptDialog(QDialog):
    """Receive a partial quantity against an existing purchase order."""
    def __init__(self, order, lines, currency="KES", parent=None):
        super().__init__(parent)
        self.order=dict(order); self.lines=[dict(line) for line in lines]; self.currency=currency
        self.setWindowTitle(f"Receive goods · {self.order['order_number']}")
        self.setMinimumSize(760,480)
        outer=QVBoxLayout(self)
        outer.addWidget(QLabel(f"<b>{self.order['order_number']}</b> · {self.order['supplier_name']}"))
        note=QLabel("Enter quantities physically received. Only those quantities will affect inventory and the supplier payment ledger.")
        note.setWordWrap(True); outer.addWidget(note)
        self.has_batch_columns=any(bool(line.get("track_batches",0)) for line in self.lines)
        self.table=QTableWidget(0,7 if self.has_batch_columns else 5)
        headers=["SKU","PRODUCT","ORDERED","RECEIVED","RECEIVE NOW"]
        if self.has_batch_columns: headers += ["BATCH / LOT","EXPIRY DATE"]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False); self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers); self.table.horizontalHeader().setStretchLastSection(True)
        outer.addWidget(self.table,1)
        self.controls={}
        for line in self.lines:
            row=self.table.rowCount(); self.table.insertRow(row)
            ordered=Decimal(str(line["quantity"])); received=Decimal(str(line["received_quantity"]))
            remaining=max(Decimal("0"),ordered-received)
            for col,value in enumerate((line["sku"],line["description"],f"{ordered:g}",f"{received:g}")):
                cell=QTableWidgetItem(str(value)); cell.setFlags(cell.flags() & ~Qt.ItemIsEditable); self.table.setItem(row,col,cell)
            quantity=QDoubleSpinBox(); quantity.setDecimals(3); quantity.setRange(0,float(remaining)); quantity.setSingleStep(1)
            self.table.setCellWidget(row,4,quantity); self.controls[row]=(line,quantity)
            if self.has_batch_columns:
                batch=QLineEdit(); batch.setPlaceholderText("Required" if bool(line.get("track_batches",0)) else "—")
                expiry=QLineEdit(); expiry.setPlaceholderText("YYYY-MM-DD")
                batch.setEnabled(bool(line.get("track_batches",0))); expiry.setEnabled(bool(line.get("track_batches",0)))
                self.table.setCellWidget(row,5,batch); self.table.setCellWidget(row,6,expiry)
            quantity.valueChanged.connect(self.recalculate)
        self.paid=QDoubleSpinBox(); self.paid.setDecimals(2); self.paid.setRange(0,999999999999.99); self.paid.setPrefix(f"{currency} ")
        self.payment_method=QComboBox(); self.payment_method.addItems(["Cash", "M-Pesa", "Bank transfer", "Card", "Other"])
        self.payment_reference=QLineEdit(); self.payment_reference.setPlaceholderText("M-Pesa code / bank reference when applicable")
        self.payment_method.currentTextChanged.connect(lambda method:self.payment_reference.setPlaceholderText("Enter confirmed M-Pesa transaction code" if method=="M-Pesa" else "Bank / card reference (optional)" if method in {"Bank transfer","Card"} else "Reference (optional)"))
        form=QFormLayout(); form.addRow("Amount paid now",self.paid); form.addRow("Payment method",self.payment_method); form.addRow("Payment reference",self.payment_reference); outer.addLayout(form)
        self.total_label=QLabel(); self.total_label.setAlignment(Qt.AlignRight); outer.addWidget(self.total_label)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_if_valid); buttons.rejected.connect(self.reject); outer.addWidget(buttons)
        self.recalculate()

    def receipt_data(self):
        lines=[]; total=Decimal("0.00")
        for _row,(source,quantity) in self.controls.items():
            qty=Decimal(str(quantity.value())).quantize(Decimal("0.001"))
            if qty<=0: continue
            line={"purchase_order_line_id":int(source["id"]),"quantity":qty}
            if bool(source.get("track_batches",0)):
                batch=self.table.cellWidget(_row,5).text().strip()
                expiry=self.table.cellWidget(_row,6).text().strip()
                if not batch: raise ValueError(f"Enter a batch / lot number for {source['description']}.")
                line.update({"batch_number":batch,"expiry_date":expiry,"batch_notes":""})
            lines.append(line)
            total+=(qty*Decimal(str(source["unit_cost"]))).quantize(Decimal("0.01"))
        if not lines: raise ValueError("Enter a positive quantity for at least one line.")
        if Decimal(str(self.paid.value()))>total: raise ValueError("Amount paid now cannot exceed the goods received total.")
        return {"lines":lines,"amount_paid":self.paid.value(),"payment_method":self.payment_method.currentText(),"payment_reference":self.payment_reference.text().strip(),"total":total}

    def recalculate(self):
        total=Decimal("0.00")
        for _row,(source,quantity) in self.controls.items():
            total+=(Decimal(str(quantity.value()))*Decimal(str(source["unit_cost"]))).quantize(Decimal("0.01"))
        self.total_label.setText(f"Receipt total: {self.currency} {total:,.2f}")

    def _accept_if_valid(self):
        try: self.receipt_data()
        except ValueError as exc:
            QMessageBox.warning(self,"Receipt details required",str(exc)); return
        self.accept()


class SaleReturnDialog(QDialog):
    """Select sold quantities to return; refunds use each item's original price."""
    def __init__(self, sale_number, sale_lines, currency="KES", parent=None):
        super().__init__(parent)
        self.sale_number=str(sale_number)
        self.sale_lines=list(sale_lines)
        self.currency=currency
        self.setWindowTitle(f"Return items · {self.sale_number}")
        self.setMinimumSize(760,480)
        outer=QVBoxLayout(self)
        outer.addWidget(QLabel(f"<b>Return items from {self.sale_number}</b>"))
        note=QLabel("Choose quantities and whether each item can be returned to stock. The refund uses the original selling price. Returns exceeding the amount paid are blocked until customer credit is supported.")
        note.setWordWrap(True); outer.addWidget(note)
        self.table=QTableWidget(0,6)
        self.table.setHorizontalHeaderLabels(["SKU","PRODUCT","SOLD","ALREADY RETURNED","RETURN QTY","RESTOCK"])
        self.table.verticalHeader().setVisible(False); self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers); self.table.horizontalHeader().setStretchLastSection(True)
        outer.addWidget(self.table,1)
        self.controls={}
        for line in self.sale_lines:
            row=self.table.rowCount(); self.table.insertRow(row)
            sold=Decimal(str(line["sold_quantity"]))
            returned=Decimal(str(line.get("returned_quantity",0)))
            remaining=max(Decimal("0"),sold-returned)
            values=[line["sku"],line["description"],f"{sold:g}",f"{returned:g}"]
            for col,value in enumerate(values):
                item=QTableWidgetItem(str(value)); item.setFlags(item.flags() & ~Qt.ItemIsEditable); self.table.setItem(row,col,item)
            quantity=QDoubleSpinBox(); quantity.setDecimals(3); quantity.setRange(0,float(remaining)); quantity.setSingleStep(1)
            restock=QCheckBox(); restock.setChecked(True); restock.setToolTip("Add returned item back to sellable inventory")
            self.table.setCellWidget(row,4,quantity); self.table.setCellWidget(row,5,restock)
            self.controls[row]=(line,quantity,restock)
            quantity.valueChanged.connect(self.recalculate)
        self.reason=QLineEdit(); self.reason.setMaxLength(300); self.reason.setPlaceholderText("Required: reason for the return")
        self.payment_method=QComboBox(); self.payment_method.addItems(["Cash","M-Pesa","Bank transfer","Card","Other"])
        self.payment_reference=QLineEdit(); self.payment_reference.setPlaceholderText("Required for an M-Pesa refund")
        self.payment_method.currentTextChanged.connect(lambda value:self.payment_reference.setPlaceholderText("Enter the confirmed M-Pesa transaction code" if value=="M-Pesa" else "Bank / card reference (optional)" if value in {"Bank transfer","Card"} else "Reference (optional)"))
        form=QFormLayout(); form.addRow("Reason",self.reason); form.addRow("Refund channel",self.payment_method); form.addRow("Transaction reference",self.payment_reference); outer.addLayout(form)
        self.refund_label=QLabel(); self.refund_label.setAlignment(Qt.AlignRight); outer.addWidget(self.refund_label)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_if_valid); buttons.rejected.connect(self.reject); outer.addWidget(buttons)
        self.recalculate()

    def return_data(self):
        reason=self.reason.text().strip()
        if not reason:
            raise ValueError("Enter a reason for the return.")
        lines=[]
        total=Decimal("0.00")
        for _row,(source,quantity,restock) in self.controls.items():
            qty=Decimal(str(quantity.value())).quantize(Decimal("0.001"))
            if qty<=0:
                continue
            lines.append({"sale_line_id":int(source["sale_line_id"]),"quantity":qty,"restock":restock.isChecked()})
            total+=(qty*Decimal(str(source["unit_price"]))).quantize(Decimal("0.01"))
        if not lines:
            raise ValueError("Select a positive quantity for at least one item.")
        return {"lines":lines,"reason":reason,"refund_amount":total,"payment_method":self.payment_method.currentText(),"payment_reference":self.payment_reference.text().strip()}

    def recalculate(self):
        amount=Decimal("0.00")
        for _row,(source,quantity,_restock) in self.controls.items():
            amount+=(Decimal(str(quantity.value()))*Decimal(str(source["unit_price"]))).quantize(Decimal("0.01"))
        self.refund_label.setText(f"Cash refund: {self.currency} {amount:,.2f}")

    def _accept_if_valid(self):
        try:
            self.return_data()
        except ValueError as exc:
            QMessageBox.warning(self,"Return details required",str(exc))
            return
        self.accept()


class RepairJobDialog(QDialog):
    """Quick intake form for repairs, installations and other device services."""
    def __init__(self, customers, products=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Receive device · Repair or service")
        self.setMinimumSize(650, 630)
        self.resize(760, 760)

        outer = QVBoxLayout(self)
        intro = QLabel(
            "Record the device and choose the work requested. Standard services are listed; "
            "you can also type a custom service such as a specific software installation."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("padding: 6px 2px; color: palette(mid); font-size: 10pt;")
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(6, 8, 12, 12)
        form.setSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.customer = QComboBox()
        self.customer.setEditable(True)
        self.customer.setInsertPolicy(QComboBox.NoInsert)
        self.customer.setPlaceholderText("Choose an existing customer or enter a name")
        self.customer.addItem("Walk-in customer", None)
        for customer in customers:
            self.customer.addItem(customer["name"], int(customer["id"]))

        self.work_kind = QComboBox()
        self.work_kind.addItems(["Repair", "Servicing"])

        self.service_type = QComboBox()
        self.service_type.setEditable(True)
        self.service_type.setInsertPolicy(QComboBox.NoInsert)
        self.service_type.setMinimumWidth(420)
        self.service_type.setPlaceholderText("Search or type requested service")
        self.service_type.addItems(list(SERVICE_OPTIONS))
        self.service_type.setCurrentText("Hardware · Diagnostics / fault finding")
        completer = self.service_type.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseInsensitive)

        self.product = QComboBox()
        self.product.setEditable(True)
        self.product.setInsertPolicy(QComboBox.NoInsert)
        self.product.addItem("Not linked to catalogue", None)
        for product in (products or []):
            self.product.addItem(f'{product["sku"]} · {product["name"]}', int(product["id"]))

        self.device = QLineEdit()
        self.device.setPlaceholderText("e.g. HP EliteBook 840, Samsung A54, PS5")
        self.serial = QLineEdit()
        self.serial.setPlaceholderText("Serial / IMEI (optional)")
        self.fault = QTextEdit()
        self.fault.setMaximumHeight(80)
        self.fault.setPlaceholderText("Describe the fault or customer request (optional if service is selected)")
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(65)
        self.notes.setPlaceholderText("Condition, accessories, backup consent; never store passwords")
        self.warranty = QLineEdit()
        self.warranty.setPlaceholderText("Optional warranty or condition notes")
        self.received = QDateEdit()
        self.received.setCalendarPopup(True)
        self.received.setDisplayFormat("yyyy-MM-dd")
        self.received.setDate(QDate.currentDate())
        self.collection = QDateEdit()
        self.collection.setCalendarPopup(True)
        self.collection.setDisplayFormat("yyyy-MM-dd")
        self.collection.setDate(QDate.currentDate().addDays(3))
        self.cost = QDoubleSpinBox()
        self.cost.setRange(0, 999999999999.99)
        self.cost.setDecimals(2)
        self.cost.setPrefix("KES ")
        self.cost.setSpecialValueText("KES 0.00 — set later")
        self.cost.setToolTip("Optional intake estimate. Final price can be entered after diagnosis.")

        form.addRow("Customer", self.customer)
        form.addRow("Job category", self.work_kind)
        form.addRow("Requested service", self.service_type)
        form.addRow("Device / model", self.device)
        form.addRow("Serial / IMEI", self.serial)
        form.addRow("Catalogue product (optional)", self.product)
        form.addRow("Fault / customer request", self.fault)
        form.addRow("Work notes", self.notes)
        form.addRow("Warranty / condition", self.warranty)
        form.addRow("Date received", self.received)
        form.addRow("Expected collection", self.collection)
        form.addRow("Initial estimated charge", self.cost)

        scroll.setWidget(form_host)
        outer.addWidget(scroll, 1)

        self.work_kind.setToolTip("The system suggests a category based on the selected service. You can change it.")
        self.service_type.currentTextChanged.connect(self._service_type_changed)
        self.product.currentIndexChanged.connect(self._fill_device)
        buttons = QDialogButtonBox()
        buttons.addButton("Save service ticket", QDialogButtonBox.AcceptRole).clicked.connect(self._accept_if_valid)
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        outer.addWidget(buttons)

    def _service_type_changed(self, service_name):
        suggested = suggested_work_kind(service_name)
        if self.work_kind.currentText() != suggested:
            self.work_kind.setCurrentText(suggested)

    def _fill_device(self, *_):
        text = self.product.currentText().split(" · ", 1)
        if self.product.currentData() is not None and len(text) == 2 and not self.device.text().strip():
            self.device.setText(text[1])

    def _accept_if_valid(self):
        requested = self.service_type.currentText().strip()
        if not self.customer.currentText().strip() or not self.device.text().strip() or not requested:
            QMessageBox.warning(self, "Missing details", "Enter a customer, device, and requested service.")
            return
        if self.collection.date() < self.received.date():
            QMessageBox.warning(self, "Invalid dates", "Expected collection cannot be earlier than the received date.")
            return
        self.accept()

    def data(self):
        customer_id = self.customer.currentData()
        if customer_id is not None:
            idx = self.customer.findText(self.customer.currentText().strip())
            if idx < 0 or self.customer.itemData(idx) != customer_id:
                customer_id = None
        requested = self.service_type.currentText().strip()
        fault = self.fault.toPlainText().strip() or requested
        return {
            "customer_name": self.customer.currentText().strip(),
            "customer_id": customer_id,
            "device": self.device.text().strip(),
            "serial_number": self.serial.text().strip(),
            "reported_fault": fault,
            "warranty_notes": self.warranty.text().strip(),
            "work_kind": self.work_kind.currentText(),
            "service_type": requested,
            "received_date": self.received.date().toString("yyyy-MM-dd"),
            "expected_collection_date": self.collection.date().toString("yyyy-MM-dd"),
            "product_id": self.product.currentData(),
            "service_notes": self.notes.toPlainText().strip(),
            "estimated_cost": self.cost.value(),
        }


class RepairDiagnosisDialog(QDialog):
    def __init__(self, job, currency="KES", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Diagnosis and customer estimate")
        self.setMinimumWidth(520)
        layout=QVBoxLayout(self); form=QFormLayout()
        self.diagnosis=QTextEdit(); self.diagnosis.setMaximumHeight(120); self.diagnosis.setPlainText(job["diagnosis"] or "")
        self.technician=QLineEdit(job["technician"] or "")
        self.labor=QDoubleSpinBox(); self.labor.setRange(0,999999999999.99); self.labor.setDecimals(2); self.labor.setPrefix(f"{currency} "); self.labor.setValue(float(job["labor_amount"] or 0))
        self.estimate=QDoubleSpinBox(); self.estimate.setRange(0,999999999999.99); self.estimate.setDecimals(2); self.estimate.setPrefix(f"{currency} "); self.estimate.setValue(float(job["estimate_amount"] or 0))
        form.addRow("Diagnosis",self.diagnosis); form.addRow("Assigned technician",self.technician)
        form.addRow("Labor charge",self.labor); form.addRow("Pre-tax estimate",self.estimate)
        layout.addLayout(form)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_if_valid); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def _accept_if_valid(self):
        if not self.diagnosis.toPlainText().strip():
            QMessageBox.warning(self,"Diagnosis required","Enter the diagnosis before requesting approval.")
            return
        self.accept()

    def data(self):
        return {"diagnosis":self.diagnosis.toPlainText().strip(),"technician":self.technician.text().strip(),
                "labor_amount":self.labor.value(),"estimate_amount":self.estimate.value()}


class RepairPartDialog(QDialog):
    def __init__(self, products, currency="KES", parent=None):
        super().__init__(parent)
        self.products=[dict(p) for p in products]
        self.by_id={int(product["id"]):product for product in self.products}
        self.setWindowTitle("Issue inventory part")
        self.setMinimumWidth(500)
        layout=QVBoxLayout(self); form=QFormLayout()
        self.product=QComboBox()
        for product in self.products:
            stock=Decimal(str(product.get("stock_on_hand","0")))
            self.product.addItem(f"{product['sku']} — {product['name']} · {stock:g} in stock",int(product["id"]))
        self.quantity=QDoubleSpinBox(); self.quantity.setRange(0.001,999999999.999); self.quantity.setDecimals(3); self.quantity.setValue(1)
        self.price=QLabel()
        form.addRow("Inventory part",self.product); form.addRow("Quantity",self.quantity); form.addRow("Selling price",self.price)
        layout.addLayout(form)
        self.product.currentIndexChanged.connect(self._refresh_price); self._refresh_price()
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        if not self.products:
            self.product.addItem("No active inventory parts",None); buttons.button(QDialogButtonBox.Save).setEnabled(False)

    def _refresh_price(self):
        product=self.by_id.get(self.product.currentData())
        self.price.setText(f"{product['selling_price']} per {product['unit']}" if product else "No part selected")

    def data(self):
        product_id=self.product.currentData()
        if product_id is None: raise ValueError("Select an inventory part.")
        return {"product_id":int(product_id),"quantity":self.quantity.value()}


class RepairPaymentDialog(QDialog):
    def __init__(self, balance, currency="KES", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record repair payment")
        layout=QVBoxLayout(self); form=QFormLayout()
        self.amount=QDoubleSpinBox(); self.amount.setRange(0.01,max(0.01,float(balance))); self.amount.setDecimals(2); self.amount.setPrefix(f"{currency} "); self.amount.setValue(float(balance))
        self.method=QComboBox(); self.method.addItems(["Cash", "M-Pesa", "Bank transfer", "Card", "Other"])
        self.reference=QLineEdit(); self.reference.setPlaceholderText("Enter confirmed M-Pesa transaction code when applicable")
        form.addRow("Outstanding balance",QLabel(f"{currency} {Decimal(str(balance)):,.2f}")); form.addRow("Amount received",self.amount); form.addRow("Payment method",self.method); form.addRow("Reference",self.reference)
        layout.addLayout(form)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def data(self):
        return {"amount":self.amount.value(),"reference":self.reference.text().strip(),"payment_method":self.method.currentText()}
