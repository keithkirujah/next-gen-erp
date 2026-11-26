"""First-run, replayable guided tour for the NextGen ERP desktop workspace."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QFrame, QSizePolicy
)


class GuidedTourDialog(QDialog):
    """A skippable step-by-step introduction; completion is remembered locally."""

    def __init__(self, window, first_run=False):
        super().__init__(window)
        self.window = window
        self.store = window.store
        self.first_run = bool(first_run)
        self.palette = getattr(window, "palette", {})
        self.steps = self._build_steps()
        self.index = 0
        self.setWindowTitle("Welcome to NextGen ERP" if first_run else "NextGen ERP guided tour")
        self.setModal(True)
        self.setMinimumSize(650, 470)
        self.resize(760, 520)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 25, 28, 24)
        root.setSpacing(16)

        brand = QHBoxLayout()
        mark = QLabel("⬡")
        mark.setStyleSheet(f"font-size:24pt;font-weight:900;color:{self._c('blue', '#477de0')};")
        brand.addWidget(mark)
        brand_title = QLabel("NEXTGEN ERP  /  GUIDED EXPERIENCE")
        brand_title.setStyleSheet(f"font-size:8pt;font-weight:900;letter-spacing:1.4px;color:{self._c('muted', '#758197')};")
        brand.addWidget(brand_title)
        brand.addStretch()
        self.step_label = QLabel("")
        self.step_label.setStyleSheet(f"font-size:9pt;font-weight:800;color:{self._c('muted', '#758197')};")
        brand.addWidget(self.step_label)
        root.addLayout(brand)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(7)
        self.progress.setStyleSheet(
            f"QProgressBar{{background:{self._c('panel2', '#edf1f7')};border:0;border-radius:3px;}}"
            f"QProgressBar::chunk{{background:{self._c('blue', '#477de0')};border-radius:3px;}}"
        )
        root.addWidget(self.progress)

        self.eyebrow = QLabel("")
        self.eyebrow.setStyleSheet(f"font-size:8pt;font-weight:900;letter-spacing:1.3px;color:{self._c('blue', '#477de0')};")
        root.addWidget(self.eyebrow)
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size:23pt;font-weight:900;")
        root.addWidget(self.title)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setStyleSheet(f"font-size:11pt;line-height:1.55;color:{self._c('text', '#263247')};")
        root.addWidget(self.body, 1)

        tip_frame = QFrame()
        tip_frame.setObjectName("tourTip")
        tip_frame.setStyleSheet(
            f"QFrame#tourTip{{background:{self._c('panel2', '#edf1f7')};"
            f"border:1px solid {self._c('border', '#dce3ee')};border-radius:12px;}}"
        )
        tip_layout = QVBoxLayout(tip_frame)
        tip_layout.setContentsMargins(15, 12, 15, 12)
        tip_title = QLabel("QUICK TIP")
        tip_title.setStyleSheet(f"font-size:8pt;font-weight:900;letter-spacing:1px;color:{self._c('muted', '#758197')};")
        self.tip = QLabel("")
        self.tip.setWordWrap(True)
        self.tip.setStyleSheet("font-size:9.5pt;")
        tip_layout.addWidget(tip_title)
        tip_layout.addWidget(self.tip)
        root.addWidget(tip_frame)

        footer = QHBoxLayout()
        self.skip_btn = QPushButton("Skip tour")
        self.skip_btn.setMinimumHeight(38)
        self.skip_btn.clicked.connect(self.skip_tour)
        footer.addWidget(self.skip_btn)
        footer.addStretch()
        self.back_btn = QPushButton("←  Back")
        self.back_btn.setMinimumHeight(38)
        self.back_btn.clicked.connect(self.go_back)
        footer.addWidget(self.back_btn)
        self.next_btn = QPushButton("Next  →")
        self.next_btn.setObjectName("primary")
        self.next_btn.setMinimumHeight(38)
        self.next_btn.setMinimumWidth(125)
        self.next_btn.clicked.connect(self.go_next)
        footer.addWidget(self.next_btn)
        root.addLayout(footer)
        self._render()

    def _c(self, key, fallback):
        return self.palette.get(key, fallback)

    def _build_steps(self):
        role = getattr(self.window, "role", "Staff")
        enabled = {row["name"] for row in self.store.features(True)}
        steps = [
            ("WELCOME", "Your business, one connected workspace",
             "NextGen ERP brings your daily operations into one desktop system. It is designed for your business profile, your enabled modules and the permissions assigned to each team member.",
             "You are in control. You can move through this tour at your own pace, skip it completely, or replay it later using the Guided tour button."),
            ("YOUR WORKSPACE", "A system that adapts to your business",
             "Your business profile determines which specialist workspaces are available. The administrator can enable or disable modules and configure the system as the business evolves. Team members only see the areas permitted by their role.",
             "Keep the workspace focused: enable the modules your business actually uses. This helps keep navigation clear and prevents unrelated workflows from appearing."),
            ("NAVIGATION", "Find your way around quickly",
             "Use the left navigation to move between workspaces. The search field in the top bar helps locate local records. Your profile at the top shows the account currently signed in.",
             "Use Ctrl+K to open the quick command palette and Ctrl+L to lock the session when stepping away."),
            ("DASHBOARD", "Your operational overview",
             "The Dashboard gives you a high-level view of business activity. Depending on your enabled modules and permissions, it can surface sales, stock, cashflow, service work and other important indicators.",
             "Treat the dashboard as your daily starting point. Investigate the underlying records before making financial or stock decisions."),
        ]
        if "Inventory" in enabled:
            steps.append(("INVENTORY", "Know what you own and what is moving",
                "The Inventory workspace is your catalogue and stock control centre. Maintain product names, SKUs, cost and selling prices, reorder levels and stock movements. Lending a product also removes it from available stock until it is properly returned.",
                "For laptops, phones and other individually tracked equipment, keep serial numbers or IMEIs accurate. They make returns and investigations much easier."))
        if "Sales" in enabled or "Point of Sale" in enabled:
            steps.append(("SALES & POS", "Turn a transaction into a traceable record",
                "Use Point of Sale for counter transactions and Sales for the wider sales workflow. Select the correct customer, products, quantities and payment method. Credit or unpaid sales must follow the customer's configured credit facility.",
                "A credit sale is not the same as a temporary product loan. Use Product Lending when the shop expects the item itself to be returned."))
        if "CRM" in enabled:
            steps.append(("CUSTOMERS", "Build a reliable customer record",
                "CRM stores customer identity and contact details, purchase relationships and credit settings. Customer credit limits represent the customer's overall exposure to the business, not the amount one employee may approve.",
                "Check customer details before granting credit. Outstanding POS credit and product-lending balances share the customer's available credit ceiling."))
        if "Product Lending" in enabled:
            steps.append(("PRODUCT LENDING", "Issue products responsibly — and always know where they are",
                "The Product Lending Centre records temporary issues of shop-owned products. Select a registered customer, product, quantity and due date. The live preview checks the customer's remaining credit and the employee's per-transaction authority separately.",
                "Example: if John can approve KSh 5,000 and Jane KSh 2,000, a KSh 7,000 issue needs a separately authenticated Manager/Admin. Even then, the customer's own credit limit remains a hard ceiling."))
            steps.append(("LENDING SETTLEMENT", "Follow every agreement through to closure",
                "The lending register shows who received each item, its value, payments, balance, due date, approvals and activity history. Record repayments against the correct agreement. Inspect returned items and record their condition.",
                "A good-condition return with an unpaid balance needs Manager/Admin approval to clear the debt. Damaged or incomplete items are quarantined; the remaining balance is not silently erased. Full payment settles the debt while the customer keeps the product."))
        if "Repair Tickets" in enabled:
            steps.append(("REPAIRS & SERVICE", "Track every device from intake to collection",
                "Use the repair/service workspace to record the customer, device, serial number, reported fault, diagnosis, work performed, parts, charges and collection status.",
                "Record device condition and accessories at intake. A clear service history protects both the customer and the shop."))
        if "Purchases" in enabled:
            steps.append(("PURCHASING", "Keep supplier orders and incoming stock aligned",
                "Use purchasing workflows to record suppliers, purchase orders, received goods and supplier balances. Confirm quantities and costs when goods arrive so inventory and cashflow remain trustworthy.",
                "Do not treat an order as received stock until the goods have actually been checked in."))
        if "Finance" in enabled:
            steps.append(("FINANCE", "Understand where the money went",
                "Finance and cashbook tools track money in and out, payment channels and balances. Payments recorded against lending agreements are reflected as incoming cash entries with a reference to the loan.",
                "Reconcile cash, M-Pesa and bank activity regularly. Record the actual confirmed payment reference rather than an estimated or unverified code."))
        if "Reports" in enabled:
            steps.append(("REPORTS", "Turn activity into decisions",
                "Reports help you review sales, stock, finance and operational activity. Use the reporting periods and filters to investigate performance and exceptions.",
                "A report is only as reliable as the records entered into the system. Keep transactions and stock movements up to date."))
        if role in ("Admin", "Manager") or "manage_users" in self.store.role_permissions(role):
            steps.append(("TEAM & ACCESS", "Give people the right access — not everyone all access",
                "User Management controls employee accounts, roles, activation and lending authority. Role Permissions and Module Manager control which capabilities and workspaces are available to each role.",
                "Employee lending authority is a per-transaction approval ceiling. It is separate from the customer's overall credit limit. Above-limit approvals require a different authorised manager's credentials and are audited."))
        steps.extend([
            ("SETTINGS", "Make the system fit the way you work",
             "System Settings contains business identity, preferences, printing and other configuration. Keep the business logo and contact information accurate so customer-facing documents identify the correct business.",
             "Change configuration deliberately and test printing with the paper size and printer you actually use."),
            ("OFFLINE & SAFETY", "Work locally. Protect your records.",
             "NextGen ERP stores operational data locally so the desktop workflow can continue without an internet connection. Where synchronisation is configured, queued changes can be sent when connectivity returns. Database & Backup provides tools to protect and restore business data.",
             "Offline does not mean risk-free: keep regular backups, protect Windows sign-in credentials and avoid copying the live database while the application is writing to it."),
            ("YOU'RE READY", "Your workspace is ready for business",
             "That is the guided tour. You can now explore the modules available to your account. You can replay this walkthrough at any time from the Guided tour button in the top bar.",
             "Start with your customer records and opening stock. Then run a small test transaction before using the system for live business.")
        ])
        return steps

    def _render(self):
        total = len(self.steps)
        eyebrow, title, body, tip = self.steps[self.index]
        self.step_label.setText(f"{self.index + 1:02d}  /  {total:02d}")
        self.progress.setMaximum(total)
        self.progress.setValue(self.index + 1)
        self.eyebrow.setText(eyebrow)
        self.title.setText(title)
        self.body.setText(body)
        self.tip.setText(tip)
        self.back_btn.setEnabled(self.index > 0)
        self.next_btn.setText("Finish tour  ✓" if self.index == total - 1 else "Next  →")

    def go_back(self):
        if self.index > 0:
            self.index -= 1
            self._render()

    def go_next(self):
        if self.index < len(self.steps) - 1:
            self.index += 1
            self._render()
            return
        self.store.set_setting("onboarding_tour_completed", "1")
        self.accept()

    def skip_tour(self):
        self.store.set_setting("onboarding_tour_completed", "1")
        self.reject()


def show_guided_tour(window, first_run=False):
    dialog = GuidedTourDialog(window, first_run=first_run)
    dialog.exec()
