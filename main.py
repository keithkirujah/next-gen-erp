import sys, os, json, sqlite3, hashlib, secrets, shutil, urllib.request, urllib.parse, threading, re, tempfile, math, csv, time, uuid
import logging, time
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from PySide6.QtCore import Qt, QSize, QTimer, QRectF, QPointF, QUrl, QDate, QTime, QThread, Signal, QObject, QEvent, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QShortcut, QKeySequence, QPixmap, QColor, QFont, QPainter, QPen, QBrush, QLinearGradient, QAction, QTextDocument, QPageSize, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QFrame, QStackedWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QFormLayout, QComboBox, QDialogButtonBox, QCheckBox,
    QScrollArea, QSizePolicy, QTextEdit, QPlainTextEdit, QSpinBox, QAbstractItemView, QGraphicsDropShadowEffect, QFileDialog, QDateEdit, QDoubleSpinBox, QInputDialog, QColorDialog, QTabWidget, QTimeEdit
)
from PySide6.QtPrintSupport import QPrinter, QPrintDialog, QPrintPreviewDialog
from profiles import BUSINESS_PROFILES, CORE_MODULES, MODULE_DEPENDENCIES, get_profile, missing_dependencies, workspace_navigation_names, profile_role_module_map
from custom_module_schema import normalize_custom_fields, validate_custom_field_evolution
from operations import OperationsService, OperationError
from operations_ui import StockTransactionDialog, SaleReturnDialog, PurchaseOrderReceiptDialog, RepairJobDialog, RepairDiagnosisDialog, RepairPartDialog, RepairPaymentDialog
from document_templates import render_invoice, render_quote, render_purchase_order, render_goods_received, render_repair_job, render_record, render_shift_report, render_student_report_card, render_student_transcript, normalize_website_url
from insights import PulseService
from shifts import ShiftService, ShiftError
import reports as report_engine
from pulse_ui import PulsePanel, RecapDialog
from command_palette import CommandPalette
from branding import load_logo_b64, logo_data_uri
from app_logging import setup_logging, install_crash_handler, log_dir
from session_guard import IdleWatcher, LockDialog
from backup_service import BackupManager, Toast, INTERVAL_OPTIONS, REALTIME, format_bytes, ago
from printing import PrintService, PrintOptionsDialog, save_settings as save_print_settings, load_settings as load_print_settings
from workspace_isolation import workspace_path, ensure_workspace_metadata, migrate_legacy_workspace, WorkspaceIdentityError
from website_catalog import fetch_catalog, initialize_catalog_cache, replace_catalog_cache, list_catalog_cache
from website_catalog_ui import WebsiteCatalogWorker, WebsiteCatalogDialog
import zipfile
import urllib.error
from pos_ui import POSPage
from returns_ui import ReturnsPage

POS_UNAVAILABLE_PROFILES = frozenset({"School / training centre", "Professional services"})

APP_NAME = "NextGen ERP"
APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "NextGenERP"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "nextgen_erp.db"

C = {
    "bg": "#080D1B", "sidebar": "#0A1123", "panel": "#101A30", "panel2": "#14213B",
    "border": "#243654", "text": "#F2F6FF", "muted": "#8B9AB8", "blue": "#3978FF",
    "cyan": "#30DDF0", "purple": "#9A6BFF", "green": "#16D9A2", "orange": "#FFB454",
    "red": "#FF647C", "pink": "#FF62BC"
}
MODULES = {
    "Dashboard": ("⌂", "Business overview"),
    "Sales": ("↗", "Customers, quotations, orders and invoices"),
    "Point of Sale": ("▣", "Fast counter checkout using the stock-backed sales engine"),
    "Returns Desk": ("↺", "Process refunds and restock returned goods"),
    "Purchases": ("⇩", "Suppliers and purchase orders"),
    "Inventory": ("⬡", "Products, stock and movements"),
    "Finance": ("◉", "Cashbook, expenses, payment channels and daily cashflow"),
    "HR & Payroll": ("♙", "Employees, leave and payroll records"),
    "CRM": ("◎", "Customer relationships and follow-ups"),
    "Projects": ("▦", "Project planning and delivery"),
    "Repair Tickets": ("⚒", "Device repairs, approvals, parts, and collection"),
    "Reports": ("▤", "Operational reports"),
    "Fleet & Assets": ("⬢", "Vehicles, assets and service schedules"),
    "Job Cards": ("⚒", "Field and workshop job cards"),
    "Library": ("▤", "Library catalogue and loans"),
    "Classes & Timetable": ("▦", "Classes, streams and teaching assignments"),
    "Fees & Billing": ("₵", "Student fees, invoices and collections"),
    "User Management": ("♧", "Users and access roles"),
    "Role Permissions": ("⚿", "Role-based access control"),
    "Module Manager": ("✣", "Enable, disable and configure modules"),
    "Feature Store": ("✧", "Future trusted extension catalog"),
    "Integrations": ("⇄", "External system connectors"),
    "System Settings": ("⚙", "Business, tax and printer settings"),
    "Database and Backup": ("▤", "Local data and backup tools"),
}
ROLE_CAPABILITIES = ("view", "create", "edit", "delete", "export", "print", "configure", "manage_users", "backup", "sync", "manage_inventory", "manage_finance", "process_returns")
DASHBOARD_WIDGETS = {
    "sales_count": "Sales count",
    "purchase_count": "Purchase count",
    "product_count": "Product count",
    "low_stock": "Low-stock products",
    "legacy_count": "Legacy record count",
    "sync_count": "Backup status",
    "finance_summary": "Finance summary",
    "sales_trend": "Seven-day sales chart",
    "quick_actions": "Quick actions",
    "recent_activity": "Recent activity",
}
DEFAULT_DASHBOARD_WIDGETS = {
    "Admin": set(DASHBOARD_WIDGETS),
    "Manager": set(DASHBOARD_WIDGETS)-{"sync_count"},
    "Staff": {"sales_count","product_count","low_stock","legacy_count","sales_trend","quick_actions","recent_activity"},
}
ROLE_DEFAULTS = {
    "Admin": set(ROLE_CAPABILITIES),
    "Manager": {"view", "create", "edit", "export", "print", "manage_inventory", "manage_finance", "process_returns", "manage_users"},
    "Staff": {"view", "create", "edit", "print"},
}
PROTECTED_CAPABILITIES = {"configure", "backup", "sync", "delete"}
DEFAULT_FIELDS = {
    "Sales": ["Customer", "Reference", "Amount (KES)", "Status"],
    "Point of Sale": ["Receipt", "Customer", "Items", "Total", "Payment status"],
    "Returns Desk": ["Return #", "Sale", "Refund", "Reason", "Status"],
    "Purchases": ["Supplier", "Reference", "Amount (KES)", "Status"],
    "Inventory": ["Product", "SKU", "Quantity", "Unit Price (KES)"],
    "Finance": ["Type", "Description", "Amount (KES)", "Date"],
    "HR & Payroll": ["Employee", "Department", "Amount (KES)", "Status"],
    "CRM": ["Customer", "Contact", "Email", "Status"],
    "Projects": ["Project", "Owner", "Due Date", "Status"],
    "Reports": ["Report Name", "Period", "Notes", "Status"],
    "Fleet & Assets": ["Asset", "Reg", "Service date", "Status"],
    "Job Cards": ["Client", "Job", "Due Date", "Status"],
    "Library": ["Title", "Author", "Copies", "Status"],
    "Classes & Timetable": ["Class", "Teacher", "Room", "Status"],
    "Fees & Billing": ["Student", "Fee type", "Amount (KES)", "Status"],
}
# These settings belong to one business workspace. The active identity and transport
# endpoint remain installation-wide because they select/open a workspace and its sync service.
WORKSPACE_SETTING_KEYS = {
    "theme", "business_tagline", "business_address", "business_phone", "business_email",
    "business_tax_id", "business_tax_label", "business_tax_rate", "business_tax_inclusive", "business_logo_b64", "business_payment_details", "business_invoice_footer", "pulse_goal_daily", "pulse_achievements", "pulse_last_recap", "pos_held_carts", "require_shift", "business_currency", "payment_providers_json", "pos_config_json", "hardware_profile_json", "branch_sync_json", "etims_config_json", "period_lock_json", "locations_config_json", "fiscal_config_json",
    "business_accent", "business_accent_custom", "pos_enabled", "role_permissions",
    "role_modules", "dashboard_widgets", "custom_roles", "applied_profile",
    "website_catalog_url", "website_catalog_source", "website_catalog_last_sync",
    "product_form_fields",
    "auto_backup_enabled", "auto_backup_interval_minutes",
    "auto_backup_folder", "auto_backup_last_run", "auto_backup_keep", "auto_backup_mode",
    "github_backup_enabled", "github_backup_repo", "github_backup_token", "github_backup_path", "github_backup_last_status",
}

def is_workspace_setting(key):
    return key in WORKSPACE_SETTING_KEYS or (key.startswith("business_") and key not in {"business_name", "business_type"})

LEGACY_PROFILE_ALIASES = {
    "Computers and Electronics Sales and Repairs": ("Laptop & computer sales / repair",),
}

def normalize_profile_name(profile_name):
    return {old: new for new, aliases in LEGACY_PROFILE_ALIASES.items() for old in aliases}.get(profile_name, profile_name)

def stamp(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 240_000).hex()
def check_password(pw, saved):
    try:
        salt, digest = saved.split(":", 1)
        return secrets.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000).hex(), digest)
    except Exception: return False

class Store:
    SYSTEM_TABLES = {"users", "features", "settings", "activity", "sync_queue"}

    def __init__(self):
        self.actor_role = "Admin"
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.setup()
        self.workspace_conn = None
        self.workspace_id = None
        saved_profile = self.setting("business_type", "General business")
        profile = normalize_profile_name(saved_profile)
        business_name = self.setting("business_name", "Default business")
        if profile != saved_profile:
            self.set_setting("business_type", profile)
        self._activate_workspace(profile, business_name, migrate_legacy=True)
        # Older installs have an unscoped audit log. Assign that history once to
        # the business that was active during upgrade so other workspaces do not
        # inherit it as global dashboard activity.
        scoped = self.conn.execute("SELECT value FROM settings WHERE key='activity_workspace_scoped'").fetchone()
        if not scoped or scoped["value"] != "1":
            self.conn.execute("UPDATE activity SET workspace_id=? WHERE workspace_id='__global__'", (self.workspace_id,))
            self.conn.execute("INSERT INTO settings(key,value) VALUES('activity_workspace_scoped','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            self.conn.commit()
        self._configure_operations()

    def _configure_operations(self):
        self.operations = OperationsService(self.workspace_conn)
        self.operations.permission_provider = self.role_permissions
        self.operations.module_access_provider = self.role_has_module
        self.operations.tax_provider = self.tax_configuration
        # Queue writes are attached to the system database so the IDs stay globally
        # unique and the queue row is committed with the operational transaction.
        self.operations.conn.execute("ATTACH DATABASE ? AS systemdb", (str(DB_PATH),))
        self.operations.queue_writer = self._queue_workspace_event
        self.operations.actor_role = self.actor_role
        self.operations.actor_username = getattr(self, "actor_username", "system")

    def _queue_workspace_event(self, entity, entity_id, payload, timestamp):
        self.operations.conn.execute(
            "INSERT INTO systemdb.sync_queue(entity,entity_id,operation,payload_json,queued_at,workspace_id) VALUES(?,?,?,?,?,?)",
            (entity, int(entity_id), "upsert", json.dumps(payload, ensure_ascii=False), timestamp, self.workspace_id),
        )

    def _activate_workspace(self, profile_name, business_name, migrate_legacy=False):
        workspace_id, path, identity_key = workspace_path(APP_DIR / "workspaces", business_name, profile_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve workspaces created with the previous computer-repair profile label.
        # Validate the old metadata before renaming; never claim an unrelated file.
        if not path.exists():
            for legacy_profile in LEGACY_PROFILE_ALIASES.get(profile_name, ()):
                legacy_id, legacy_path, legacy_key = workspace_path(APP_DIR / "workspaces", business_name, legacy_profile)
                if not legacy_path.exists():
                    continue
                legacy_conn = sqlite3.connect(str(legacy_path))
                try:
                    metadata = legacy_conn.execute("SELECT workspace_id,identity_key FROM workspace_metadata WHERE singleton=1").fetchone()
                    if not metadata or tuple(metadata) != (legacy_id, legacy_key):
                        raise WorkspaceIdentityError("Legacy computer-repair workspace identity could not be verified; the file was not renamed or opened.")
                    legacy_conn.execute(
                        "UPDATE workspace_metadata SET workspace_id=?,identity_key=?,business_name=?,profile_name=?,schema_version=2 WHERE singleton=1",
                        (workspace_id, identity_key, business_name or "Default business", profile_name))
                    legacy_conn.commit()
                    legacy_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    legacy_conn.close()
                os.replace(legacy_path, path)
                self.conn.execute("UPDATE sync_queue SET workspace_id=? WHERE workspace_id=?", (workspace_id, legacy_id))
                self.conn.commit()
                break
        path_existed = path.exists()
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        legacy_tables = tables - self.SYSTEM_TABLES
        has_legacy_operations = bool(legacy_tables)
        queue_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sync_queue)")}
        if "workspace_id" not in queue_cols:
            self.conn.execute("ALTER TABLE sync_queue ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '__global__'")
        if has_legacy_operations and migrate_legacy and not path.exists():
            migrate_legacy_workspace(self.conn, path, workspace_id, identity_key,
                                     business_name or "Default business", profile_name, self.SYSTEM_TABLES)
        elif path.exists() and has_legacy_operations and migrate_legacy:
            check_conn = sqlite3.connect(str(path))
            try:
                row = check_conn.execute("SELECT workspace_id,identity_key FROM workspace_metadata WHERE singleton=1").fetchone()
                if not row or row[0] != workspace_id or row[1] != identity_key:
                    raise WorkspaceIdentityError("Existing workspace file does not match the requested business identity.")
            finally:
                check_conn.close()
            self._drop_legacy_tables_from_master()
        self.workspace_id = workspace_id
        self.workspace_path = path
        self.workspace_conn = sqlite3.connect(str(path))
        self.workspace_conn.row_factory = sqlite3.Row
        self.workspace_conn.execute("PRAGMA foreign_keys=ON")
        self.workspace_conn.execute("PRAGMA journal_mode=WAL")
        ensure_workspace_metadata(self.workspace_conn, workspace_id, identity_key, business_name or "Default business", profile_name)
        metadata = self.workspace_conn.execute("SELECT schema_version FROM workspace_metadata WHERE singleton=1").fetchone()
        schema_version = int(metadata[0]) if metadata else 1

        # Workspace configuration is local to this business database, not shared in the
        # installation-wide account/settings database.
        self.workspace_conn.execute("CREATE TABLE IF NOT EXISTS workspace_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.workspace_conn.execute("""CREATE TABLE IF NOT EXISTS features(
            id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '',
            fields_json TEXT NOT NULL, enabled INTEGER DEFAULT 1, custom INTEGER DEFAULT 0,
            created_at TEXT NOT NULL)""")
        self.workspace_conn.execute("""CREATE TABLE IF NOT EXISTS workspace_records(
            id INTEGER PRIMARY KEY, module TEXT NOT NULL, data_json TEXT NOT NULL,
            status TEXT DEFAULT 'Open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

        # One-time migration for workspace files created by the earlier prototype:
        # move this currently-active business' settings/features once, without copying
        # those shared values into every other workspace the user may open later.
        if path_existed and schema_version < 2 and migrate_legacy:
            local_count = self.workspace_conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
            if local_count == 0:
                legacy_features = self.conn.execute("SELECT name,description,fields_json,enabled,custom,created_at FROM features ORDER BY id").fetchall()
                for row in legacy_features:
                    self.workspace_conn.execute(
                        "INSERT OR IGNORE INTO features(name,description,fields_json,enabled,custom,created_at) VALUES(?,?,?,?,?,?)",
                        tuple(row))
            setting_rows = self.conn.execute("SELECT key,value FROM settings").fetchall()
            for row in setting_rows:
                if is_workspace_setting(row["key"]):
                    self.workspace_conn.execute("INSERT OR IGNORE INTO workspace_settings(key,value) VALUES(?,?)", (row["key"], row["value"]))

        for n, fields in DEFAULT_FIELDS.items():
            self.workspace_conn.execute(
                "INSERT OR IGNORE INTO features(name,description,fields_json,enabled,custom,created_at) VALUES(?,?,?,?,0,?)",
                (n, MODULES[n][1], json.dumps(fields), 0, stamp()))
        self.workspace_conn.execute("UPDATE workspace_metadata SET schema_version=2 WHERE singleton=1")
        self.workspace_conn.commit()

    def _drop_legacy_tables_from_master(self):
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        tables = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        for name in tables:
            if name not in self.SYSTEM_TABLES:
                safe = name.replace('"', '""')
                self.conn.execute(f'DROP TABLE IF EXISTS "{safe}"')
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.commit()
    def setup(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS features(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', fields_json TEXT NOT NULL, enabled INTEGER DEFAULT 1, custom INTEGER DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_queue(id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER NOT NULL, operation TEXT NOT NULL, payload_json TEXT NOT NULL, queued_at TEXT NOT NULL, attempts INTEGER DEFAULT 0, last_error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, username TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT '__global__');
        """)
        activity_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(activity)").fetchall()}
        if "workspace_id" not in activity_columns:
            self.conn.execute("ALTER TABLE activity ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '__global__'")
        queue_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sync_queue)").fetchall()}
        if "workspace_id" not in queue_columns:
            self.conn.execute("ALTER TABLE sync_queue ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '__global__'")
        # Employee codes for receipt privacy / served-by lines
        user_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if "employee_code" not in user_columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN employee_code TEXT NOT NULL DEFAULT ''")
            existing = self.conn.execute("SELECT id FROM users ORDER BY id").fetchall()
            for idx, row in enumerate(existing, start=1):
                code = f"EMP-{idx:04d}"
                self.conn.execute("UPDATE users SET employee_code=? WHERE id=?", (code, row["id"]))
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_employee_code ON users(employee_code) WHERE employee_code <> ''")
        self.conn.commit()
        for n, fields in DEFAULT_FIELDS.items():
            self.conn.execute("INSERT OR IGNORE INTO features(name,description,fields_json,enabled,custom,created_at) VALUES(?,?,?,?,0,?)",
                              (n, MODULES[n][1], json.dumps(fields), 1, stamp()))
        self.conn.commit()

    def next_employee_code(self):
        """Allocate the next EMP-#### code for a new user."""
        row = self.conn.execute(
            "SELECT employee_code FROM users WHERE employee_code LIKE 'EMP-%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        next_num = 1
        if row and row["employee_code"]:
            try:
                next_num = int(str(row["employee_code"]).split("-", 1)[-1]) + 1
            except ValueError:
                next_num = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] + 1
        return f"EMP-{next_num:04d}"

    def user_served_snapshot(self, username):
        """Return (display_name, employee_code) for the logged-in user (for receipts)."""
        row = self.conn.execute(
            "SELECT display_name, employee_code FROM users WHERE username=? COLLATE NOCASE",
            (str(username or ""),)
        ).fetchone()
        if not row:
            return str(username or "Staff"), ""
        return row["display_name"] or str(username), row["employee_code"] or ""
    def require(self, capability):
        if capability not in self.role_permissions(self.actor_role):
            raise PermissionError(f"The {self.actor_role} role is not allowed to {capability.replace('_', ' ')}.")
    def require_admin(self):
        """Guard owner-only configuration even if future role rules change."""
        if str(getattr(self, "actor_role", "")) != "Admin":
            raise PermissionError("Only an administrator can manage modules or reset shop data.")
    def needs_initial_setup(self):
        # Unauthenticated setup is permitted only on a genuinely empty user table.
        # Existing installations must authenticate before changing admin identity.
        return self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
    def configure_initial_admin(self, username, display_name, password):
        if not self.needs_initial_setup():
            raise PermissionError("Administrator setup is already complete.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", str(username)) or not str(display_name).strip() or len(str(password)) < 6:
            raise ValueError("Use a valid username, display name, and a password of at least 6 characters.")
        row = self.conn.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", ("admin",)).fetchone()
        code = self.next_employee_code()
        if row:
            self.conn.execute(
                "UPDATE users SET username=?,display_name=?,role='Admin',password_hash=?,active=1,employee_code=COALESCE(NULLIF(employee_code,''),?) WHERE id=?",
                (username, display_name, hash_password(password), code, row["id"]))
        else:
            self.conn.execute(
                "INSERT INTO users(username,display_name,role,password_hash,active,created_at,employee_code) VALUES(?,?, 'Admin', ?,1,?,?)",
                (username, display_name, hash_password(password), stamp(), code))
        self.conn.commit()
    def log(self,u,a,d=""):
        self.conn.execute("INSERT INTO activity(username,action,details,created_at,workspace_id) VALUES(?,?,?,?,?)",(u,a,d,stamp(),self.workspace_id or "__global__")); self.conn.commit()
    def queue(self, entity, entity_id, op, payload):
        timestamp = stamp()
        self.operations.conn.execute(
            "INSERT INTO systemdb.sync_queue(entity,entity_id,operation,payload_json,queued_at,workspace_id) VALUES(?,?,?,?,?,?)",
            (entity, int(entity_id), op, json.dumps(payload, ensure_ascii=False), timestamp, self.workspace_id),
        )
        self.operations.conn.commit()
    def _validate_record_data(self, module, data):
        if not isinstance(data, dict):
            raise ValueError("Record data must be a field/value object.")
        feature = self.feature(module)
        if not feature:
            raise ValueError(f"The {module} module is not configured.")
        try:
            definitions = json.loads(feature["fields_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"The field configuration for {module} is invalid.") from exc
        for definition in definitions:
            if isinstance(definition, str):
                continue
            if not isinstance(definition, dict) or not definition.get("name"):
                continue
            name = str(definition["name"])
            value = data.get(name)
            if definition.get("required") and (value is None or (isinstance(value, str) and not value.strip())):
                raise ValueError(f"{name} is required.")
            if value is None or value == "":
                continue
            field_type = definition.get("type", "text")
            if field_type == "text" and (not isinstance(value, str) or len(value) > 10000):
                raise ValueError(f"{name} must be text up to 10,000 characters.")
            if field_type == "notes" and (not isinstance(value, str) or len(value) > 100000):
                raise ValueError(f"{name} must be notes up to 100,000 characters.")
            if field_type == "number":
                try:
                    numeric = Decimal(str(value))
                except Exception as exc:
                    raise ValueError(f"{name} must be a number.") from exc
                if isinstance(value, bool) or not numeric.is_finite():
                    raise ValueError(f"{name} must be a finite number.")
            if field_type == "date":
                try:
                    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError as exc:
                    raise ValueError(f"{name} must be a valid date in YYYY-MM-DD format.") from exc
            if field_type == "choice":
                options = definition.get("options", [])
                if str(value) not in options:
                    raise ValueError(f"{name} must be one of the configured options.")
        try:
            json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Record data contains a value that cannot be stored safely.") from exc
        return data

    def create_record(self, module, data):
        self.require_module(module)
        self.require("create")
        data = self._validate_record_data(module, data)
        status = str(data.get("Status", data.get("status", "Open")))[:100]
        timestamp=stamp()
        with self.workspace_conn:
            cur=self.workspace_conn.execute("INSERT INTO workspace_records(module,data_json,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                                  (module,json.dumps(data,ensure_ascii=False),status,timestamp,timestamp))
            rid=cur.lastrowid
            payload={"id":rid,"module":module,"data":data,"status":status,"created_at":timestamp}
            self.queue("records", rid, "upsert", payload)
        return rid
    def update_record(self, record_id, module, data):
        self.require_module(module)
        self.require("edit")
        data = self._validate_record_data(module, data)
        existing=self.workspace_conn.execute("SELECT status FROM workspace_records WHERE id=? AND module=?",(record_id,module)).fetchone()
        if not existing or existing["status"] == "Cancelled":
            raise ValueError("The record does not exist or has already been cancelled.")
        status = str(data.get("Status", data.get("status", "Open")))[:100]
        timestamp=stamp()
        payload={"id":record_id,"module":module,"data":data,"status":status,"updated_at":timestamp}
        with self.workspace_conn:
            self.workspace_conn.execute("UPDATE workspace_records SET data_json=?,status=?,updated_at=? WHERE id=? AND module=?",
                              (json.dumps(data,ensure_ascii=False),status,timestamp,record_id,module))
            self.queue("records", record_id, "upsert", payload)
    def cancel_record(self, record_id, module):
        self.require_module(module)
        self.require("delete")
        row=self.workspace_conn.execute("SELECT data_json,status FROM workspace_records WHERE id=? AND module=?",(record_id,module)).fetchone()
        if not row or row["status"] == "Cancelled":
            return False
        data=json.loads(row["data_json"])
        if "Status" in data:
            data["Status"]="Cancelled"
        elif "status" in data:
            data["status"]="Cancelled"
        timestamp=stamp()
        with self.workspace_conn:
            self.workspace_conn.execute("UPDATE workspace_records SET data_json=?,status='Cancelled',updated_at=? WHERE id=? AND module=?",
                              (json.dumps(data,ensure_ascii=False),timestamp,record_id,module))
            payload={"id":record_id,"module":module,"data":data,"status":"Cancelled","updated_at":timestamp}
            self.queue("records", record_id, "upsert", payload)
        return True
    def records(self,module=None,limit=500):
        if module:
            self.require_module(module)
            return self.workspace_conn.execute("SELECT * FROM workspace_records WHERE module=? ORDER BY id DESC LIMIT ?",(module,limit)).fetchall()
        allowed=sorted(name for name in self._known_modules() if self.role_has_module(self.actor_role,name))
        if not allowed:
            return []
        marks=",".join("?" for _ in allowed)
        return self.workspace_conn.execute(f"SELECT * FROM workspace_records WHERE module IN ({marks}) ORDER BY id DESC LIMIT ?",(*allowed,limit)).fetchall()
    def features(self, enabled_only=False):
        return self.workspace_conn.execute("SELECT * FROM features"+(" WHERE enabled=1" if enabled_only else "")+" ORDER BY custom,name").fetchall()
    def feature(self,name): return self.workspace_conn.execute("SELECT * FROM features WHERE name=?",(name,)).fetchone()
    def add_feature(self,name,desc,fields):
        self.require_admin()
        self.require("configure")
        name = str(name).strip()
        desc = str(desc).strip()
        if not 2 <= len(name) <= 40 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &()_-]*", name):
            raise ValueError("Module names must be 2–40 characters and use letters, numbers, spaces, ampersands, parentheses, dashes, or underscores.")
        if len(desc) > 200:
            raise ValueError("Module purpose must be 200 characters or fewer.")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
            raise ValueError("A custom module must define between 1 and 20 fields.")
        clean_fields=[]
        seen=set()
        for item in fields:
            if not isinstance(item, dict):
                raise ValueError("Each custom field must have a valid definition.")
            field_name=str(item.get("name", "")).strip()
            field_type=item.get("type", "text")
            required=item.get("required", False)
            options=item.get("options", [])
            if not field_name or len(field_name) > 60:
                raise ValueError("Field names must contain 1–60 characters.")
            if field_name.casefold() in seen:
                raise ValueError("Field names must be unique within a module.")
            seen.add(field_name.casefold())
            if field_type not in ("text", "number", "date", "choice", "notes"):
                raise ValueError(f"Unsupported field type for {field_name}.")
            if not isinstance(required, bool):
                raise ValueError(f"Required status for {field_name} must be true or false.")
            if field_type == "choice":
                if not isinstance(options, list) or not 1 <= len(options) <= 30:
                    raise ValueError(f"Choice field {field_name} needs 1–30 options.")
                options=[str(option).strip() for option in options]
                if any(not option or len(option) > 60 for option in options) or len({option.casefold() for option in options}) != len(options):
                    raise ValueError(f"Options for {field_name} must be unique, non-empty values of 60 characters or fewer.")
            else:
                options=[]
            clean_fields.append({"name":field_name,"type":field_type,"required":required,"options":options})
        fields=clean_fields
        timestamp=stamp()
        with self.workspace_conn:
            cur=self.workspace_conn.execute("INSERT INTO features(name,description,fields_json,enabled,custom,created_at) VALUES(?,?,?,?,1,?)",
                                  (name,desc,json.dumps(fields),1,timestamp))
            payload={"name":name,"description":desc,"fields":fields,"enabled":True,"custom":True}
            self.queue("features", cur.lastrowid, "upsert", payload)
    def update_custom_feature(self,name,desc,fields):
        """Safely revise an admin-created module without stranding existing records."""
        self.require_admin()
        self.require("configure")
        row=self.feature(str(name))
        if not row or not bool(row["custom"]):
            raise ValueError("Only administrator-created custom modules can be edited here.")
        desc=str(desc or "").strip()
        if len(desc)>200:
            raise ValueError("Module purpose must be 200 characters or fewer.")
        clean=normalize_custom_fields(fields)
        old=json.loads(row["fields_json"] or "[]")
        has_records=bool(self.workspace_conn.execute("SELECT 1 FROM workspace_records WHERE module=? LIMIT 1",(name,)).fetchone())
        validate_custom_field_evolution(name,old,clean,has_records)
        with self.workspace_conn:
            self.workspace_conn.execute("UPDATE features SET description=?,fields_json=? WHERE id=? AND custom=1",
                (desc,json.dumps(clean,ensure_ascii=False),int(row["id"])))
            self.queue("features",row["id"],"upsert",{"name":name,"description":desc,"fields":clean,"enabled":bool(row["enabled"]),"custom":True})
        return True
    def set_enabled(self,name,value):
        self.require_admin()
        self.require("configure")
        row=self.feature(name)
        if not row:
            return False
        if not value and self.workspace_conn.execute("SELECT 1 FROM workspace_records WHERE module=? LIMIT 1",(name,)).fetchone():
            raise ValueError(f"{name} has saved records, so it cannot be disabled. Keep the workspace enabled to preserve access to its history.")
        current={r["name"] for r in self.features(True)}
        desired=set(current)
        changed=set()
        if value:
            # Enabling a feature enables its complete dependency chain atomically.
            # Previously the UI simply rejected an otherwise valid request when a
            # prerequisite was off, making the module manager appear broken.
            pending=[name]
            visited=set()
            while pending:
                module=pending.pop()
                if module in visited:
                    continue
                visited.add(module)
                if not self.feature(module):
                    raise ValueError(f"Cannot enable {name}: required module {module} is not installed in this build.")
                if module not in desired:
                    desired.add(module)
                    changed.add(module)
                pending.extend(MODULE_DEPENDENCIES.get(module, ()))
        else:
            desired.discard(name)
            changed.add(name)
            missing=missing_dependencies(desired)
            if missing:
                dependents=sorted({module for module,dependency in missing if dependency==name})
                detail="; ".join(f"{module} requires {dependency}" for module,dependency in missing)
                raise ValueError("Cannot disable this module while dependent workflows are enabled: "+detail+". Disable dependent modules first.")
        timestamp=stamp()
        with self.workspace_conn:
            for changed_name in sorted(changed):
                changed_row=self.feature(changed_name)
                enabled_now=changed_name in desired
                self.workspace_conn.execute("UPDATE features SET enabled=? WHERE name=?",(int(enabled_now),changed_name))
                payload={"name":changed_name,"enabled":enabled_now}
                self.queue("features", changed_row["id"], "upsert", payload)
        return True
    def modules_preserved_by_profile(self, profile_name):
        profile=get_profile(profile_name)
        target=set(profile.modules)
        # A profile change inside the same workspace must not hide existing history.
        # Cross-business/profile isolation is enforced by choosing a different database
        # in switch_workspace; this preservation rule only applies to records already
        # owned by the currently open workspace.
        recorded={row["module"] for row in self.workspace_conn.execute("SELECT DISTINCT module FROM workspace_records")}
        installed={row["name"] for row in self.workspace_conn.execute("SELECT name FROM features")}
        preserved=((recorded-target) & installed)
        pending=list(preserved)
        while pending:
            module=pending.pop()
            for dependency in MODULE_DEPENDENCIES.get(module, ()):
                if dependency not in target and dependency not in preserved:
                    preserved.add(dependency)
                    pending.append(dependency)
        return sorted(preserved)
    def apply_profile(self, profile_name):
        self.require("configure")
        profile=get_profile(profile_name)
        enabled=set(profile.modules) | set(self.modules_preserved_by_profile(profile_name))
        if self.setting("pos_enabled", "0") == "1" and profile_name not in POS_UNAVAILABLE_PROFILES:
            enabled.add("Point of Sale")
        if getattr(profile, "pos_recommended", False) and profile_name not in POS_UNAVAILABLE_PROFILES:
            # Profile strongly expects a till — enable POS module; setting may still be off until admin toggles
            enabled.add("Point of Sale")
            enabled.add("Returns Desk")
        missing=missing_dependencies(enabled)
        if missing:
            detail="; ".join(f"{module} requires {dependency}" for module,dependency in missing)
            raise ValueError("This business profile has an incomplete module dependency configuration: "+detail)
        workspace_fields={}
        for blueprint in BUSINESS_PROFILES.values():
            workspace_fields.update(blueprint.workspaces)
        timestamp=stamp()
        with self.workspace_conn:
            for name, fields in workspace_fields.items():
                row=self.workspace_conn.execute("SELECT id FROM features WHERE name=?",(name,)).fetchone()
                if not row:
                    self.workspace_conn.execute("INSERT INTO features(name,description,fields_json,enabled,custom,created_at) VALUES(?,?,?,?,0,?)",
                                      (name,f"Profile workspace · {profile.name}",json.dumps(list(fields),ensure_ascii=False),0,timestamp))
            configured=set(CORE_MODULES) | set(workspace_fields) | {"Point of Sale","Returns Desk","Fees & Billing","Classes & Timetable","Library","Job Cards","Fleet & Assets"}
            for row in self.workspace_conn.execute("SELECT id,name,enabled,custom FROM features").fetchall():
                name=row["name"]
                if name not in configured or (row["custom"] and name not in workspace_fields):
                    continue
                desired=int(name in enabled)
                if row["enabled"] == desired:
                    continue
                self.workspace_conn.execute("UPDATE features SET enabled=? WHERE id=?",(desired,row["id"]))
                payload={"name":name,"enabled":bool(desired)}
                self.queue("features", row["id"], "upsert", payload)
            self.workspace_conn.execute("INSERT INTO workspace_settings(key,value) VALUES('applied_profile',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(profile.name,))
        self.install_profile_roles(profile_name)
        return profile.name

    def install_profile_roles(self, profile_name):
        """Seed industry roles and module maps for the selected business type.

        Existing custom roles with the same names are updated to the profile's
        recommended modules so a school gets Principal / HOD / Finance Officer
        access patterns automatically.
        """
        profile = get_profile(profile_name)
        if not profile.roles:
            return
        try:
            roles = json.loads(self.setting("custom_roles", "[]"))
            if not isinstance(roles, list):
                roles = []
        except (TypeError, ValueError):
            roles = []
        try:
            grants = json.loads(self.setting("role_permissions", "{}"))
            modules = json.loads(self.setting("role_modules", "{}"))
            widgets = json.loads(self.setting("dashboard_widgets", "{}"))
        except (TypeError, ValueError):
            grants, modules, widgets = {}, {}, {}
        if not all(isinstance(value, dict) for value in (grants, modules, widgets)):
            grants, modules, widgets = {}, {}, {}

        existing_names = {str(row.get("name")).casefold() for row in roles if isinstance(row, dict)}
        for role in profile.roles:
            # Keep Admin reserved; skip colliding system names
            if role.name.casefold() in {"admin", "manager", "staff"}:
                # Still set module map for Manager/Staff defaults when profile is applied
                if role.name in {"Manager", "Staff"}:
                    mods = set(role.modules) & set(profile.modules)
                    # Staff always get the till when this business has Point of Sale available.
                    if role.name == "Staff" and "Point of Sale" in profile.modules:
                        mods.add("Point of Sale")
                        if "Returns Desk" in profile.modules:
                            mods.add("Returns Desk")
                    modules[role.name] = sorted(mods)
                continue
            if role.name.casefold() not in existing_names:
                roles.append({"name": role.name, "template": role.template if role.template in {"Manager", "Staff"} else "Staff"})
                existing_names.add(role.name.casefold())
            template = role.template if role.template in {"Manager", "Staff"} else "Staff"
            # Capabilities from template
            grants[role.name] = sorted(self.role_permissions(template) if template in {"Manager", "Staff"} else self.role_permissions("Staff"))
            # Modules intersect profile-enabled set
            modules[role.name] = sorted(set(role.modules) & set(profile.modules))
            widgets[role.name] = sorted(self.dashboard_widgets(template) if template in {"Manager", "Staff"} else self.dashboard_widgets("Staff"))

        with self.workspace_conn:
            for key, value in (
                ("custom_roles", roles),
                ("role_permissions", grants),
                ("role_modules", modules),
                ("dashboard_widgets", widgets),
            ):
                self.workspace_conn.execute(
                    "INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, sort_keys=True)),
                )

    def tax_configuration(self):
        return {"label":self.setting("business_tax_label","Tax"),"rate":self.setting("business_tax_rate","0.00"),"inclusive":self.setting("business_tax_inclusive","0")}
    def require_module(self, module):
        if not self.role_has_module(self.actor_role,module):
            raise PermissionError(f"The {self.actor_role} role cannot access the {module} module.")
    def _known_modules(self):
        return {row["name"] for row in self.features()}
    def custom_roles(self):
        try:
            rows=json.loads(self.setting("custom_roles","[]"))
            return [row["name"] for row in rows if isinstance(row,dict) and isinstance(row.get("name"),str)]
        except (TypeError,ValueError,AttributeError):
            return []
    def assignable_roles(self):
        return ["Manager","Staff"]+self.custom_roles()
    def role_template(self, role):
        if role in {"Manager","Staff"}:
            return role
        try:
            rows=json.loads(self.setting("custom_roles","[]"))
            return next((row.get("template","Staff") for row in rows if isinstance(row,dict) and row.get("name")==role),"Staff")
        except (TypeError,ValueError,AttributeError):
            return "Staff"
    def create_custom_role(self, name, template_role="Staff"):
        self.require("configure")
        name=" ".join(str(name or "").split())
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 &_-]{1,31}",name):
            raise ValueError("Use a role name of 2–32 letters, numbers, spaces, underscores or dashes, starting with a letter.")
        if template_role not in {"Manager","Staff"}:
            raise ValueError("Choose Manager or Staff as the starting permission template.")
        if name.casefold()=="admin" or any(existing.casefold()==name.casefold() for existing in ["Admin"]+self.assignable_roles()):
            raise ValueError("A role with that name already exists or is reserved.")
        try:
            roles=json.loads(self.setting("custom_roles","[]"))
            if not isinstance(roles,list): roles=[]
        except (TypeError,ValueError): roles=[]
        try:
            grants=json.loads(self.setting("role_permissions","{}"))
            modules=json.loads(self.setting("role_modules","{}"))
            widgets=json.loads(self.setting("dashboard_widgets","{}"))
        except (TypeError,ValueError):
            grants,modules,widgets={},{},{}
        if not all(isinstance(value,dict) for value in (grants,modules,widgets)):
            grants,modules,widgets={},{},{}
        roles.append({"name":name,"template":template_role})
        grants[name]=sorted(self.role_permissions(template_role))
        modules[name]=sorted(self.role_modules(template_role))
        widgets[name]=sorted(self.dashboard_widgets(template_role))
        with self.workspace_conn:
            for key,value in (("custom_roles",roles),("role_permissions",grants),("role_modules",modules),("dashboard_widgets",widgets)):
                self.workspace_conn.execute("INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(value,sort_keys=True)))
        return name
    def delete_custom_role(self, role):
        self.require("configure")
        if role not in self.custom_roles():
            raise ValueError("Only a custom role can be removed.")
        if self.conn.execute("SELECT 1 FROM users WHERE role=? LIMIT 1",(role,)).fetchone():
            raise ValueError("Reassign or remove every account using this role before deleting it.")
        roles=[row for row in json.loads(self.setting("custom_roles","[]")) if not isinstance(row,dict) or row.get("name")!=role]
        with self.workspace_conn:
            for key,value in (("custom_roles",roles),):
                self.workspace_conn.execute("INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(value,sort_keys=True)))
            for key in ("role_permissions","role_modules","dashboard_widgets"):
                try: saved=json.loads(self.setting(key,"{}"))
                except (TypeError,ValueError): saved={}
                if isinstance(saved,dict):
                    saved.pop(role,None)
                    self.workspace_conn.execute("INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(saved,sort_keys=True)))
        return True
    def role_modules(self, role):
        enabled={row["name"] for row in self.features(True)}
        if role == "Admin":
            return enabled
        if role not in self.assignable_roles():
            return set()
        # Staff get the floor tools: sales, stock, CRM, and the till when POS is enabled.
        staff_modules = {"Sales", "Inventory", "CRM", "Point of Sale", "Returns Desk", "Repair Tickets"}
        defaults = {"Manager": enabled, "Staff": enabled.intersection(staff_modules)}
        default = defaults.get(role, set())
        try:
            saved = json.loads(self.setting("role_modules", "{}"))
            if role in saved:
                assigned = set(saved.get(role, []))
            else:
                assigned = set(default)
            # Existing installs may have Staff saved without POS — grant the till whenever
            # Point of Sale is enabled for this business (profile configured with a counter).
            if role == "Staff" and "Point of Sale" in enabled:
                assigned.add("Point of Sale")
                if "Returns Desk" in enabled:
                    assigned.add("Returns Desk")
            return assigned.intersection(self._known_modules()).intersection(enabled)
        except (TypeError, ValueError, AttributeError):
            return default
    def role_has_module(self, role, module):
        feature=self.feature(module)
        return bool(feature and feature["enabled"] and module in self.role_modules(role))
    def dashboard_widgets(self, role):
        if role!="Admin" and role not in self.assignable_roles():
            return set()
        fallback=DEFAULT_DASHBOARD_WIDGETS.get(role,DEFAULT_DASHBOARD_WIDGETS["Staff"])
        try:
            saved=json.loads(self.setting("dashboard_widgets","{}"))
            requested=set(saved.get(role,fallback))
            return requested.intersection(DASHBOARD_WIDGETS)
        except (TypeError,ValueError,AttributeError):
            return set(fallback)
    def set_dashboard_widgets(self, role, widgets):
        self.require("configure")
        if role!="Admin" and role not in self.assignable_roles():
            raise ValueError("Unknown dashboard role.")
        requested=set(widgets)
        if not requested.issubset(DASHBOARD_WIDGETS):
            raise ValueError("The widget selection contains an unsupported item.")
        try:
            saved=json.loads(self.setting("dashboard_widgets","{}"))
        except (TypeError,ValueError):
            saved={}
        if not isinstance(saved,dict):
            saved={}
        saved[role]=sorted(requested)
        self.set_setting("dashboard_widgets",json.dumps(saved,sort_keys=True))
    def set_role_modules(self, role, modules):
        self.require("configure")
        if role not in self.assignable_roles():
            raise ValueError("Only non-administrator roles can be configured.")
        requested=set(modules)
        unknown=requested-self._known_modules()
        if unknown:
            raise ValueError("Unknown modules: "+", ".join(sorted(unknown)))
        try:
            saved=json.loads(self.setting("role_modules","{}"))
        except (TypeError,ValueError):
            saved={}
        if not isinstance(saved,dict):
            saved={}
        saved[role]=sorted(requested)
        self.set_setting("role_modules",json.dumps(saved,sort_keys=True))
    def setting(self,k,default=""):
        if is_workspace_setting(k) and getattr(self, "workspace_conn", None) is not None:
            row=self.workspace_conn.execute("SELECT value FROM workspace_settings WHERE key=?",(k,)).fetchone()
        else:
            row=self.conn.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
        return row["value"] if row else default
    def set_setting(self,k,v):
        if (k.startswith("business_") and k not in {"business_name", "business_type"}) or k in {"business_name", "business_type", "role_permissions", "role_modules", "dashboard_widgets", "custom_roles", "applied_profile", "sync_url", "device_name", "website_catalog_url", "website_catalog_source", "website_catalog_last_sync"}:
            self.require("configure")
        if k == "business_tax_rate":
            try:
                rate=Decimal(str(v))
            except Exception as exc:
                raise ValueError("Tax rate must be a number from 0 to 100%.") from exc
            if not rate.is_finite() or rate < 0 or rate > 100:
                raise ValueError("Tax rate must be between 0 and 100%.")
            v=format(rate.quantize(Decimal("0.01")),"f")
        if is_workspace_setting(k) and getattr(self, "workspace_conn", None) is not None:
            self.workspace_conn.execute("INSERT INTO workspace_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v)))
            self.workspace_conn.commit()
        else:
            self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v)))
            self.conn.commit()
    def role_permissions(self, role):
        if role == "Admin":
            return set(ROLE_CAPABILITIES)
        if role not in self.assignable_roles():
            return set()
        fallback=ROLE_DEFAULTS.get(role,set())
        try:
            saved=json.loads(self.setting("role_permissions", "{}"))
            requested=set(fallback) | set(saved.get(role, []))
        except (TypeError, ValueError, AttributeError):
            requested=set(fallback)
        if role == "Manager":
            requested.add("manage_users")
        return requested.intersection(set(ROLE_CAPABILITIES)-PROTECTED_CAPABILITIES)
    def set_role_permissions(self, role, capabilities):
        self.require("configure")
        if role not in self.assignable_roles():
            raise ValueError("Only non-administrator role permissions can be configured.")
        allowed=set(ROLE_CAPABILITIES)-PROTECTED_CAPABILITIES
        requested=set(capabilities)
        if not requested.issubset(allowed):
            raise ValueError("The permission set contains an unknown or administrator-only capability.")
        try:
            saved=json.loads(self.setting("role_permissions", "{}"))
        except (TypeError, ValueError):
            saved={}
        if not isinstance(saved, dict):
            saved={}
        saved[role]=sorted(requested)
        self.set_setting("role_permissions", json.dumps(saved, sort_keys=True))
    def write_backup_archive(self, destination_path):
        """Back up the system DB and every isolated business workspace."""
        destination_path = Path(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="nextgen_backup_") as tmp_dir:
            tmp = Path(tmp_dir)
            system_snapshot = tmp / "system.sqlite3"
            self.conn.commit()
            snapshot = sqlite3.connect(str(system_snapshot))
            try:
                self.conn.backup(snapshot)
                snapshot.commit()
            finally:
                snapshot.close()
            check_conn = sqlite3.connect(str(system_snapshot))
            try:
                check = check_conn.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise RuntimeError(f"System backup integrity check failed: {check}")
            finally:
                check_conn.close()

            workspace_files = sorted((APP_DIR / "workspaces").glob("workspace_*.sqlite3"))
            active_path = Path(self.workspace_path).resolve()
            if active_path not in {p.resolve() for p in workspace_files}:
                workspace_files.append(active_path)
            workspace_entries = []
            snapshot_paths = []
            for source_path in sorted({p.resolve() for p in workspace_files}):
                conn = self.operations.conn if source_path == active_path else sqlite3.connect(str(source_path))
                try:
                    conn.commit()
                    metadata = conn.execute(
                        "SELECT workspace_id,identity_key,business_name,profile_name FROM workspace_metadata WHERE singleton=1"
                    ).fetchone()
                    if not metadata:
                        raise RuntimeError(f"Workspace database has no identity metadata: {source_path.name}")
                    wid = str(metadata["workspace_id"] if isinstance(metadata, sqlite3.Row) else metadata[0])
                    identity_key = str(metadata["identity_key"] if isinstance(metadata, sqlite3.Row) else metadata[1])
                    business = str(metadata["business_name"] if isinstance(metadata, sqlite3.Row) else metadata[2])
                    profile = str(metadata["profile_name"] if isinstance(metadata, sqlite3.Row) else metadata[3])
                    snapshot_path = tmp / f"workspace_{wid}.sqlite3"
                    snap = sqlite3.connect(str(snapshot_path))
                    try:
                        conn.backup(snap)
                        snap.commit()
                    finally:
                        snap.close()
                    verify = sqlite3.connect(str(snapshot_path))
                    try:
                        check = verify.execute("PRAGMA quick_check").fetchone()[0]
                        stored = verify.execute("SELECT workspace_id,identity_key,business_name,profile_name FROM workspace_metadata WHERE singleton=1").fetchone()
                        if check != "ok" or not stored or tuple(stored) != (wid, identity_key, business, profile):
                            raise RuntimeError(f"Workspace backup validation failed: {source_path.name}")
                    finally:
                        verify.close()
                    archive_member = f"workspaces/workspace_{wid}.sqlite3"
                    workspace_entries.append({"workspace_id":wid,"identity_key":identity_key,"business_name":business,
                                              "profile_name":profile,"file":archive_member})
                    snapshot_paths.append((snapshot_path, archive_member))
                finally:
                    if conn is not self.operations.conn:
                        conn.close()

            active_metadata = self.operations.conn.execute(
                "SELECT workspace_id,identity_key,business_name,profile_name FROM workspace_metadata WHERE singleton=1"
            ).fetchone()
            manifest = {
                "format": "nextgen-erp-workspace-backup-v2",
                "created_at": stamp(),
                "active_workspace_id": active_metadata["workspace_id"],
                "workspace_id": active_metadata["workspace_id"],
                "identity_key": active_metadata["identity_key"],
                "business_name": active_metadata["business_name"],
                "profile_name": active_metadata["profile_name"],
                "workspaces": workspace_entries,
            }
            with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(system_snapshot, "system.sqlite3")
                for snapshot_path, member in snapshot_paths:
                    archive.write(snapshot_path, member)
                archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        return str(destination_path)

    def reset_shop_data(self, confirmation):
        """Clear operational rows in the active workspace, retaining setup/configuration.

        A verified full backup is created before any live rows are deleted. User
        accounts and global application settings live in the system DB and are not
        modified. Product IDs/SKU sequence settings are retained to avoid identifier
        reuse, while the website catalogue mirror and its connection settings survive.
        """
        self.require_admin()
        self.require("configure")
        business_name = str(self.setting("business_name", "Default business")).strip() or "Default business"
        expected = f"RESET {business_name}"
        if not isinstance(confirmation, str) or confirmation != expected:
            raise ValueError(f"Confirmation did not match. Type exactly: {expected}")

        backup_path = APP_DIR / f"nextgen_erp_pre_reset_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}.zip"
        self.write_backup_archive(backup_path)
        conn = self.workspace_conn
        keep_tables = {
            "workspace_metadata", "workspace_settings", "features", "sku_settings", "sku_sequences",
            "website_catalog_cache", "website_catalog_sync_state",
        }
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        deleted = {}
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            for name in tables:
                if name in keep_tables:
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                count = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
                conn.execute(f"DELETE FROM {quoted}")
                if count:
                    deleted[name] = count
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

        # Clear only operational queued changes for this workspace. Preserve pending
        # module-definition events because custom module definitions themselves stay.
        with self.conn:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sync_queue)").fetchall()}
            if "workspace_id" in columns:
                self.conn.execute(
                    "DELETE FROM sync_queue WHERE workspace_id=? AND entity NOT IN ('features','settings')",
                    (self.workspace_id,),
                )
            else:
                self.conn.execute("DELETE FROM sync_queue WHERE entity NOT IN ('features','settings')")
            activity_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(activity)").fetchall()}
            if "workspace_id" in activity_columns:
                self.conn.execute("DELETE FROM activity WHERE workspace_id=?", (self.workspace_id,))
        return {"backup_path": str(backup_path), "deleted_rows": deleted, "total_deleted": sum(deleted.values())}


    def auto_backup_folder_path(self):
        """Directory where scheduled and manual package backups are stored."""
        raw = (self.setting("auto_backup_folder", "") or "").strip()
        if raw:
            path = Path(raw)
        else:
            path = APP_DIR / "backups"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Real-time backups
    # ------------------------------------------------------------------
    def change_counter(self):
        """Monotonic count of rows written through this process's database connections.

        Used to decide whether anything changed since the last successful backup.
        """
        seen, total = set(), 0
        for conn in (self.conn, getattr(getattr(self, "operations", None), "conn", None), getattr(self, "workspace_conn", None)):
            if conn is not None and id(conn) not in seen:
                seen.add(id(conn))
                total += conn.total_changes
        return total

    def max_queue_id(self):
        row = self.conn.execute("SELECT MAX(id) FROM sync_queue").fetchone()
        return int(row[0] or 0)

    def mark_backed_up(self, max_queue_id):
        """Everything queued up to ``max_queue_id`` is now inside a verified backup."""
        self.conn.execute("DELETE FROM sync_queue WHERE id<=?", (int(max_queue_id),))
        self.conn.commit()

    def backup_history(self):
        try:
            data = json.loads(self.setting("backup_history", "[]") or "[]")
            return data if isinstance(data, list) else []
        except (TypeError, ValueError):
            return []

    def add_backup_history(self, entry, keep=30):
        history = self.backup_history()
        history.insert(0, entry)
        self.set_setting("backup_history", json.dumps(history[:keep], ensure_ascii=False))

    def github_credentials(self):
        """Settings needed by the (background) GitHub upload, read on the UI thread."""
        from secret_store import unprotect
        return {
            "enabled": self.setting("github_backup_enabled", "0") == "1",
            "repo": (self.setting("github_backup_repo", "") or "").strip().strip("/"),
            "token": unprotect(self.setting("github_backup_token", "") or "").strip(),
            "path": (self.setting("github_backup_path", "backups/nextgen_erp_latest.zip") or "backups/nextgen_erp_latest.zip").strip().lstrip("/"),
        }

    def set_github_token(self, token):
        from secret_store import protect
        self.set_setting("github_backup_token", protect((token or "").strip()))

    def github_token_is_set(self):
        return bool(self.setting("github_backup_token", ""))

    @staticmethod
    def verify_backup_archive(path):
        """Re-open the finished ZIP and confirm it is complete before it is trusted."""
        path = Path(path)
        if not path.is_file() or path.stat().st_size < 1024:
            raise RuntimeError("The backup file is missing or empty.")
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            names = set(archive.namelist())
            if bad is not None:
                raise RuntimeError(f"The backup file is corrupt ({bad}).")
            if "system.sqlite3" not in names or "manifest.json" not in names:
                raise RuntimeError("The backup file is incomplete.")

    def run_local_backup(self, reason="scheduled"):
        """Write and verify one full package in the backup folder (fast; no network).

        Returns ``{"path","bytes","mode","queue_mark"}``. Bookkeeping (history, last-run, queue purge)
        is done by ``finish_backup`` so a failed write never marks data as protected.
        """
        folder = self.auto_backup_folder_path()
        mode = (self.setting("auto_backup_mode", "overwrite") or "overwrite").strip().casefold()
        if mode not in {"overwrite", "rotate"}:
            mode = "overwrite"
        queue_mark = self.max_queue_id()
        staging = folder / f"nextgen_erp_staging_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.zip"
        try:
            self.write_backup_archive(staging)
            self.verify_backup_archive(staging)
            if mode == "overwrite":
                destination = folder / "nextgen_erp_latest.zip"
                previous = folder / "nextgen_erp_previous.zip"
                if destination.exists():
                    if previous.exists():
                        previous.unlink()
                    destination.replace(previous)
                staging.replace(destination)
            else:
                destination = folder / f"nextgen_autobackup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
                staging.replace(destination)
                try:
                    keep = int(self.setting("auto_backup_keep", "5") or "5")
                except (TypeError, ValueError):
                    keep = 5
                keep = max(2, min(keep, 50))
                archives = sorted(folder.glob("nextgen_autobackup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
                for old_file in archives[keep:]:
                    try:
                        old_file.unlink()
                    except OSError:
                        pass
        except Exception:
            try:
                if staging.exists():
                    staging.unlink()
            except OSError:
                pass
            raise
        return {"path": str(destination), "bytes": destination.stat().st_size, "mode": mode, "queue_mark": queue_mark}

    def finish_backup(self, result, reason, upload_note=""):
        """Record a successful backup (call on the thread that owns the database)."""
        self.set_setting("auto_backup_last_run", datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" · {reason} · {result['mode']}")
        self.mark_backed_up(result["queue_mark"])
        self.add_backup_history({"t": stamp(), "ok": True, "reason": reason, "path": result["path"],
                                 "bytes": result["bytes"], "note": upload_note})

    def run_auto_backup(self, reason="scheduled"):
        """Synchronous backup (local + optional GitHub). Kept for scripts/tests; the UI uses BackupManager."""
        result = self.run_local_backup(reason)
        note = ""
        creds = self.github_credentials()
        if creds["enabled"]:
            try:
                note = self.push_backup_to_github(result["path"], **{k: creds[k] for k in ("repo", "token", "path")}) or ""
                self.set_setting("github_backup_last_status", datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" · ok · {note}")
            except Exception as exc:
                self.set_setting("github_backup_last_status", datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" · failed · {str(exc)[:160]}")
                self.finish_backup(result, reason, "GitHub upload failed")
                raise RuntimeError(f"Local backup saved to {result['path']}, but GitHub upload failed: {exc}") from exc
        self.finish_backup(result, reason, note)
        return Path(result["path"])

    @staticmethod
    def push_backup_to_github(file_path, repo="", token="", path="backups/nextgen_erp_latest.zip"):
        """Overwrite one file in a private GitHub repo (HTTPS + token). Thread-safe: touches no database.

        Returns a short description of where the file went.
        """
        import base64

        repo = (repo or "").strip().strip("/")
        remote_path = (path or "backups/nextgen_erp_latest.zip").strip().lstrip("/")
        if not repo or "/" not in repo:
            raise ValueError("GitHub repo must look like owner/repository (private repo recommended).")
        if not token:
            raise ValueError("Add a GitHub personal access token with Contents: Read and write on that repository.")
        file_path = Path(file_path)
        if not file_path.is_file():
            raise ValueError("Backup file is missing; nothing to upload.")
        size = file_path.stat().st_size
        if size > 95 * 1024 * 1024:
            raise ValueError("Backup is over ~95 MB; GitHub's file API will reject it. Use a cloud-synced folder instead.")
        content_b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
        api_base = f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(remote_path)}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "NextGenERP-Backup",
            "Content-Type": "application/json",
        }

        def api_request(method, url, body=None):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return resp.status, json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"GitHub HTTP {exc.code}: {detail}") from exc

        sha = None
        try:
            status, existing = api_request("GET", api_base)
            if status == 200 and isinstance(existing, dict):
                sha = existing.get("sha")
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
        last_error = None
        for branch in ("main", "master"):
            payload = {"message": f"NextGen ERP backup {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                       "content": content_b64, "branch": branch}
            if sha:
                payload["sha"] = sha
            try:
                api_request("PUT", api_base, json.dumps(payload).encode("utf-8"))
                return f"{repo}/{remote_path} ({branch})"
            except RuntimeError as exc:
                last_error = exc
        raise RuntimeError(str(last_error) if last_error else "GitHub upload failed.")

    def restore_backup(self, source_path):
        self.require("backup")
        source_path = Path(source_path).resolve()
        if not source_path.is_file():
            raise ValueError("The selected backup file does not exist.")
        if source_path.suffix.lower() == ".zip":
            return self._restore_workspace_archive(source_path)
        return self._restore_legacy_database(source_path)

    def _restore_workspace_archive(self, source_path):
        stamp_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safety_path = APP_DIR / f"nextgen_erp_pre_restore_{stamp_id}.zip"
        token = secrets.token_hex(6)
        temp_dir = Path(tempfile.mkdtemp(prefix=f"nextgen_restore_{token}_", dir=str(APP_DIR)))
        candidate_system = temp_dir / "system.sqlite3"
        old_system_path = APP_DIR / f"nextgen_erp_system_preserved_{stamp_id}.sqlite3"
        workspaces_dir = APP_DIR / "workspaces"
        old_workspaces_dir = APP_DIR / f"workspaces_preserved_{stamp_id}"
        new_workspace_candidates = []
        moved_old_system = False
        moved_old_workspaces = False
        try:
            with zipfile.ZipFile(source_path, "r") as archive:
                members = set(archive.namelist())
                if not {"manifest.json", "system.sqlite3"}.issubset(members):
                    raise ValueError("This archive is missing the system database or its manifest.")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if manifest.get("format") not in {"nextgen-erp-workspace-backup-v1", "nextgen-erp-workspace-backup-v2"}:
                    raise ValueError("Unsupported NextGen ERP backup format.")
                candidate_system.write_bytes(archive.read("system.sqlite3"))
                entries = manifest.get("workspaces")
                if not isinstance(entries, list) or not entries:
                    # Backward compatibility with the first split-DB backup format.
                    if "workspace.sqlite3" not in members:
                        raise ValueError("The backup contains no workspace database.")
                    entries = [{"workspace_id":manifest.get("workspace_id"),
                                "identity_key":manifest.get("identity_key"),
                                "business_name":manifest.get("business_name"),
                                "profile_name":manifest.get("profile_name"),
                                "file":"workspace.sqlite3"}]
                for entry in entries:
                    wid = str(entry.get("workspace_id", ""))
                    if not re.fullmatch(r"[0-9a-f]{32}", wid):
                        raise ValueError("The backup contains an invalid workspace identifier.")
                    member = entry.get("file")
                    if member not in members or not isinstance(member, str) or not (member == "workspace.sqlite3" or member == f"workspaces/workspace_{wid}.sqlite3"):
                        raise ValueError("The backup references an invalid workspace file.")
                    candidate = temp_dir / f"workspace_{wid}.sqlite3"
                    candidate.write_bytes(archive.read(member))
                    verify = sqlite3.connect(str(candidate))
                    try:
                        check = verify.execute("PRAGMA quick_check").fetchone()[0]
                        row = verify.execute("SELECT workspace_id,identity_key,business_name,profile_name FROM workspace_metadata WHERE singleton=1").fetchone()
                        expected = (entry.get("workspace_id"), entry.get("identity_key"), entry.get("business_name"), entry.get("profile_name"))
                        if check != "ok" or not row or tuple(row) != expected:
                            raise ValueError(f"Workspace {wid} failed integrity or identity validation.")
                    finally:
                        verify.close()
                    new_workspace_candidates.append((candidate, wid))

            active_id = manifest.get("active_workspace_id", manifest.get("workspace_id"))
            active_entry = next((entry for entry in entries if entry.get("workspace_id") == active_id), None)
            if not active_entry:
                raise ValueError("The backup's active workspace is missing from its manifest.")
            system_conn = sqlite3.connect(str(candidate_system))
            try:
                check = system_conn.execute("PRAGMA quick_check").fetchone()[0]
                tables = {r[0] for r in system_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required_system = {"users", "features", "settings", "activity", "sync_queue"}
                if check != "ok" or not required_system.issubset(tables):
                    raise ValueError("The archive does not contain a valid NextGen ERP system database.")
                queue_columns = {r[1] for r in system_conn.execute("PRAGMA table_info(sync_queue)")}
                if "workspace_id" not in queue_columns:
                    raise ValueError("The archive's sync queue has no workspace scope.")
                system_conn.execute("INSERT INTO settings(key,value) VALUES('business_name',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (active_entry.get("business_name") or "Default business",))
                system_conn.execute("INSERT INTO settings(key,value) VALUES('business_type',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (active_entry.get("profile_name") or "General business",))
                system_conn.commit()
            finally:
                system_conn.close()
            expected_active_id, _, expected_active_key = workspace_path(
                APP_DIR / "workspaces", active_entry.get("business_name") or "Default business", active_entry.get("profile_name") or "General business")
            if expected_active_id != active_id or expected_active_key != active_entry.get("identity_key"):
                raise ValueError("The archive's active workspace identity is inconsistent.")

            # Save a full rollback bundle before replacing any live file.
            self.write_backup_archive(safety_path)
            self.close()
            os.replace(DB_PATH, old_system_path)
            moved_old_system = True
            try:
                if workspaces_dir.exists():
                    os.replace(workspaces_dir, old_workspaces_dir)
                    moved_old_workspaces = True
                os.replace(candidate_system, DB_PATH)
                workspaces_dir.mkdir(parents=True, exist_ok=True)
                for candidate, wid in new_workspace_candidates:
                    os.replace(candidate, workspaces_dir / f"workspace_{wid}.sqlite3")
            except Exception:
                if DB_PATH.exists():
                    DB_PATH.unlink()
                if moved_old_system and old_system_path.exists():
                    os.replace(old_system_path, DB_PATH)
                if moved_old_workspaces and workspaces_dir.exists():
                    failed_dir = APP_DIR / f"workspaces_failed_restore_{stamp_id}"
                    os.replace(workspaces_dir, failed_dir)
                if moved_old_workspaces and old_workspaces_dir.exists():
                    os.replace(old_workspaces_dir, workspaces_dir)
                raise
            # Keep the previous system DB and workspace directory as local recovery
            # artefacts rather than deleting a user's earlier business data.
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.setup()
            self.workspace_conn = None
            self.operations = None
            self._activate_workspace(active_entry.get("profile_name") or "General business",
                                     active_entry.get("business_name") or "Default business", migrate_legacy=False)
            self._configure_operations()
            return str(safety_path)
        except Exception:
            if getattr(self, "conn", None) is None:
                # If restoration failed before replacement, the old files remain.
                # If an OS-level failure occurred, the rollback block above restores them.
                self.conn = sqlite3.connect(DB_PATH)
                self.conn.row_factory = sqlite3.Row
                self.setup()
                if moved_old_workspaces and not workspaces_dir.exists() and old_workspaces_dir.exists():
                    os.replace(old_workspaces_dir, workspaces_dir)
                self._activate_workspace(self.setting("business_type", "General business"), self.setting("business_name", "Default business"), migrate_legacy=False)
                self._configure_operations()
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _restore_legacy_database(self, source_path):
        # Backward compatibility for old one-file backups. Those contain both the
        # application tables and unscoped operational tables; migration isolates
        # them into the workspace selected inside that backup.
        stamp_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safety_path = APP_DIR / f"nextgen_erp_pre_restore_{stamp_id}.zip"
        candidate_path = APP_DIR / f"restore_legacy_{secrets.token_hex(6)}.db"
        check_conn = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        try:
            check = check_conn.execute("PRAGMA quick_check").fetchone()[0]
            tables = {r[0] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if check != "ok" or not {"users", "features", "records", "sync_queue", "settings", "activity"}.issubset(tables):
                raise ValueError("The selected file is not a valid legacy NextGen ERP database backup.")
        finally:
            check_conn.close()
        self.write_backup_archive(safety_path)
        shutil.copy2(source_path, candidate_path)
        try:
            self.close()
            os.replace(candidate_path, DB_PATH)
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.setup()
            profile = self.setting("business_type", "General business")
            business_name = self.setting("business_name", "Default business")
            target_id, target_path, _ = workspace_path(APP_DIR / "workspaces", business_name, profile)
            if target_path.exists():
                preserved = target_path.with_name(target_path.stem + f"_preserved_{stamp_id}" + target_path.suffix)
                os.replace(target_path, preserved)
            self.workspace_conn = None
            self.operations = None
            self._activate_workspace(profile, business_name, migrate_legacy=True)
            self._configure_operations()
            return str(safety_path)
        finally:
            candidate_path.unlink(missing_ok=True)

    def close(self):
        connections = []
        operations = getattr(self, "operations", None)
        if operations is not None:
            connections.append(operations.conn)
        if getattr(self, "workspace_conn", None) is not None:
            connections.append(self.workspace_conn)
        if getattr(self, "conn", None) is not None:
            connections.append(self.conn)
        seen = set()
        for connection in connections:
            if connection is None or id(connection) in seen:
                continue
            seen.add(id(connection))
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self.operations = None
        self.workspace_conn = None
        self.conn = None

    def pending(self):
        # Global configuration events plus active-workspace events only. Historical
        # events for other business identities never appear in this queue view.
        return self.conn.execute(
            "SELECT * FROM sync_queue WHERE workspace_id IN ('__global__', ?) ORDER BY id", (self.workspace_id,)
        ).fetchall()

    def switch_workspace(self, profile_name, business_name):
        profile_name = profile_name or "General business"
        business_name = business_name or "Default business"
        self.conn.commit()
        old_conn = self.workspace_conn
        old_ops = getattr(self, "operations", None)
        old_id, old_path = self.workspace_id, getattr(self, "workspace_path", None)
        detached = False
        if old_ops is not None:
            try:
                old_ops.conn.commit()
                old_ops.conn.execute("DETACH DATABASE systemdb")
                detached = True
            except sqlite3.Error:
                pass
        try:
            self._activate_workspace(profile_name, business_name, migrate_legacy=False)
            self._configure_operations()
        except Exception:
            # Fail closed: a workspace identity, SQLite, or schema error must not
            # leave the application attached to a partially opened database.
            candidate = getattr(self, "workspace_conn", None)
            if candidate is not None and candidate is not old_conn:
                try:
                    candidate.close()
                except sqlite3.Error:
                    pass
            self.workspace_conn = old_conn
            self.workspace_id = old_id
            if old_path is not None:
                self.workspace_path = old_path
            self.operations = old_ops
            if old_ops is not None and detached:
                try:
                    old_ops.conn.execute("ATTACH DATABASE ? AS systemdb", (str(DB_PATH),))
                except sqlite3.Error:
                    # The old connection remains local-only if it cannot reattach;
                    # do not open the rejected workspace instead.
                    pass
            raise
        if old_conn is not None and old_conn is not self.workspace_conn:
            old_conn.close()

DARK_C = dict(C)
# Warm vanilla palette: deliberately avoids the harsh pure-white look.
LIGHT_C = {
    "bg":"#F5F3EE", "sidebar":"#FFFFFF", "panel":"#FFFFFF", "panel2":"#F0EDE6",
    "border":"#E2DCD1", "text":"#1C2433", "muted":"#6B7280", "blue":"#2F6FED",
    "cyan":"#0F8A8A", "purple":"#6B5CE7", "green":"#1B8A5A", "orange":"#C47A1A",
    "red":"#C23B45", "pink":"#B14B86"
}

ACCENT_COLORS = {
    "Ocean blue": ("#2F6FED", "#4B8BFF"),
    "Forest green": ("#1B8A5A", "#16D9A2"),
    "Warm amber": ("#C47A1A", "#FFB454"),
    "Royal violet": ("#6B5CE7", "#9A6BFF"),
    "Teal": ("#0F8A8A", "#30DDF0"),
}


def _relative_luminance(color):
    value=color.lstrip("#")
    channels=[int(value[index:index+2],16)/255 for index in (0,2,4)]
    linear=[channel/12.92 if channel<=0.04045 else ((channel+0.055)/1.055)**2.4 for channel in channels]
    return 0.2126*linear[0]+0.7152*linear[1]+0.0722*linear[2]

def _contrast_ratio(first,second):
    high,low=sorted((_relative_luminance(first),_relative_luminance(second)),reverse=True)
    return (high+0.05)/(low+0.05)

def _contrast_text(background):
    candidates=("#FFFFFF","#172234")
    return max(candidates,key=lambda candidate:_contrast_ratio(candidate,background))

def palette_for(store, theme=None):
    mode=theme or store.setting("theme", "light")
    palette=dict(LIGHT_C if mode == "light" else DARK_C)
    accent=store.setting("business_accent", "Ocean blue")
    custom=store.setting("business_accent_custom", "")
    if accent == "Custom" and re.fullmatch(r"#[0-9A-Fa-f]{6}", custom):
        # Use the administrator's brand colour in both themes; foregrounds are
        # calculated separately so the primary action remains legible.
        light_color=dark_color=custom.upper()
    else:
        light_color,dark_color=ACCENT_COLORS.get(accent, ACCENT_COLORS["Ocean blue"])
    palette["blue"]=light_color if mode == "light" else dark_color
    palette["on_blue"]=_contrast_text(palette["blue"])
    palette["on_purple"]=_contrast_text(palette["purple"])
    # Semantic roles keep widgets and popups on the same theme tokens.
    palette.update({
        "input":palette["panel"], "input_text":palette["text"], "placeholder":palette["muted"],
        "hover":palette["panel2"], "selection":palette["blue"], "focus":palette["blue"],
        "table_alt":palette["panel2"], "tooltip_bg":palette["panel2"],
        "disabled":palette["muted"], "success":palette["green"], "warning":palette["orange"],
        "error":palette["red"], "info":palette["cyan"],
    })
    return palette

def themed_style(palette):
    return f"""
QWidget {{ background:{palette['bg']}; color:{palette['text']}; font-family:'Segoe UI', 'Inter', sans-serif; font-size:10.5pt; }}
QDialog {{ background:{palette['panel']}; color:{palette['text']}; }}
QMenu {{ background:{palette['panel']}; color:{palette['text']}; border:1px solid {palette['border']}; padding:6px; border-radius:10px; }}
QLabel {{ background:transparent; }}
QFrame#panel {{ background:{palette['panel']}; border:1px solid {palette['border']}; border-radius:16px; }}
QFrame#panelAlt {{ background:{palette['panel2']}; border:1px solid {palette['border']}; border-radius:16px; }}
QPushButton {{
    background:{palette['panel2']}; color:{palette['text']}; border:1px solid {palette['border']};
    border-radius:10px; padding:8px 14px; font-weight:600; min-height:18px;
}}
QPushButton:hover {{ background:{palette['hover']}; border-color:{palette['blue']}; }}
QPushButton:pressed {{ padding-top:9px; padding-bottom:7px; }}
QPushButton:disabled {{ background:{palette['panel2']}; color:{palette['disabled']}; border-color:{palette['border']}; }}
QPushButton#primary {{ background:{palette['blue']}; border:0; color:{palette['on_blue']}; }}
QPushButton#primary:hover {{ background:{palette['purple']}; color:{palette['on_purple']}; }}
QPushButton#danger {{ background:transparent; color:{palette['red']}; border:1px solid {palette['red']}; }}
QPushButton#danger:hover {{ background:{palette['red']}; color:#FFFFFF; }}
QPushButton#nav {{ text-align:left; padding:8px 12px; background:transparent; border:0; border-radius:8px; color:{palette['text']}; font-weight:600; min-height:20px; }}
QPushButton#nav:hover {{ background:{palette['hover']}; }}
QPushButton#navActive {{ text-align:left; padding:8px 12px; background:{palette['blue']}; border:0; border-left:3px solid {palette['cyan']}; border-radius:8px; color:{palette['on_blue']}; font-weight:700; min-height:20px; }}
QPushButton#sidebarToggle {{ background:{palette['panel2']}; border:1px solid {palette['border']}; border-radius:10px; padding:6px 10px; font-weight:700; min-width:40px; max-width:48px; }}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit {{
    background:{palette['input']}; border:1px solid {palette['border']}; border-radius:10px;
    padding:8px 10px; color:{palette['input_text']}; selection-background-color:{palette['selection']}; min-height:20px;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QTimeEdit:focus {{
    border:1px solid {palette['focus']};
}}
QLineEdit::placeholder, QPlainTextEdit::placeholder {{ color:{palette['placeholder']}; }}
QComboBox::drop-down {{ border:0; width:28px; }}
QComboBox QAbstractItemView {{ background:{palette['input']}; color:{palette['input_text']}; selection-background-color:{palette['selection']}; selection-color:{palette['on_blue']}; border:1px solid {palette['border']}; padding:4px; }}
QMenu::item {{ padding:8px 16px; border-radius:6px; }}
QMenu::item:selected {{ background:{palette['selection']}; color:{palette['on_blue']}; }}
QTableWidget {{
    background:{palette['panel']}; alternate-background-color:{palette['table_alt']}; color:{palette['text']};
    border:1px solid {palette['border']}; border-radius:12px; gridline-color:transparent;
    selection-background-color:{palette['selection']}; selection-color:{palette['on_blue']}; outline:0;
}}
QTableWidget::item {{ padding:7px 12px; border:0; }}
QTableWidget::item:hover {{ background:{palette['hover']}; }}
QTableWidget::item:selected {{ background:{palette['selection']}; color:{palette['on_blue']}; }}
QHeaderView::section {{
    background:{palette['panel2']}; color:{palette['muted']}; padding:9px 12px; border:0;
    border-bottom:1px solid {palette['border']}; font-weight:700; font-size:9pt;
}}
QHeaderView::section:horizontal {{ border-right:1px solid {palette['border']}; }}
QTableWidget QTableCornerButton::section {{ background:{palette['panel2']}; border:0; }}
QScrollArea {{ border:0; background:transparent; }}
QScrollBar:vertical {{ background:transparent; width:10px; margin:4px 2px; }}
QScrollBar::handle:vertical {{ background:{palette['border']}; border-radius:5px; min-height:28px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px 4px; }}
QScrollBar::handle:horizontal {{ background:{palette['border']}; border-radius:5px; min-width:28px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
QCheckBox, QRadioButton {{ spacing:8px; background:transparent; color:{palette['text']}; font-weight:600; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width:22px; height:22px;
    border:2px solid {palette['border']};
    background:{palette['panel2']};
    border-radius:6px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background:{palette['blue']};
    border:2px solid {palette['blue']};
    image: none;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color:{palette['blue']};
}}
QCheckBox:disabled, QRadioButton:disabled {{ color:{palette['muted']}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background:{palette['panel2']};
    border-color:{palette['border']};
}}
QTabWidget::pane {{ border:1px solid {palette['border']}; border-radius:12px; top:-1px; background:{palette['panel']}; }}
QTabBar::tab {{ background:transparent; color:{palette['muted']}; padding:8px 16px; border-radius:8px; margin:2px; font-weight:600; }}
QTabBar::tab:selected {{ background:{palette['panel2']}; color:{palette['text']}; }}
QToolTip {{ background:{palette['tooltip_bg']}; color:{palette['text']}; border:1px solid {palette['border']}; padding:8px; border-radius:8px; }}
QMessageBox {{ background:{palette['panel']}; }}
QStatusBar {{ background:{palette['sidebar']}; color:{palette['muted']}; }}
"""


class InitialSetupDialog(QDialog):
    """Create or rotate the local administrator credential before opening the ERP."""
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.setWindowTitle("Set up NextGen ERP")
        self.setModal(True)
        self.setMinimumWidth(460)
        palette = palette_for(store)
        self.setStyleSheet(themed_style(palette))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(13)
        title = QLabel("⬡  Set up your business workspace")
        title.setWordWrap(True)
        title.setStyleSheet("font-size:18pt;font-weight:900;padding:4px 0 6px;")
        layout.addWidget(title)
        intro = QLabel("Create the owner account and choose a starter business setup. You can refine business details and modules later in Settings.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.business_name_input = QLineEdit(store.setting("business_name", ""))
        self.business_name_input.setPlaceholderText("Business or organization name")
        self.business_type_input = QComboBox()
        self.business_type_input.addItems(list(BUSINESS_PROFILES))
        saved_profile = store.setting("business_type", "Computers and Electronics Sales and Repairs")
        profile_index = self.business_type_input.findText(saved_profile)
        self.business_type_input.setCurrentIndex(profile_index if profile_index >= 0 else 0)
        self.currency_input = QComboBox()
        self.currency_input.addItems(["KES - Kenyan Shilling", "USD - US Dollar", "TZS - Tanzanian Shilling", "UGX - Ugandan Shilling", "GBP - British Pound", "EUR - Euro"])
        saved_currency = store.setting("business_currency", "KES - Kenyan Shilling")
        currency_index = self.currency_input.findText(saved_currency)
        self.currency_input.setCurrentIndex(currency_index if currency_index >= 0 else 0)
        self.theme_input = QComboBox()
        self.theme_input.addItem("Warm vanilla light", "light")
        self.theme_input.addItem("Coordinated dark", "dark")
        saved_theme = store.setting("theme", "light")
        theme_index = self.theme_input.findData(saved_theme)
        self.theme_input.setCurrentIndex(theme_index if theme_index >= 0 else 0)
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("3–32 letters, digits, dots, dashes, or underscores")
        self.display_input = QLineEdit()
        self.display_input.setPlaceholderText("Your name")
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("At least 6 characters")
        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.Password)
        self.confirm_input.setPlaceholderText("Re-enter your password")
        form.addRow("Business name", self.business_name_input)
        form.addRow("Starter business profile", self.business_type_input)
        self.pos_input = QCheckBox("Enable fast counter checkout (recommended for this shop)")
        self.pos_input.setChecked(store.setting("pos_enabled", "1" if saved_profile == "Computers and Electronics Sales and Repairs" else "0") == "1")
        form.addRow("Optional workspace", self.pos_input)
        form.addRow("Default currency", self.currency_input)
        form.addRow("Theme", self.theme_input)
        form.addRow("Login username (for sign-in)", self.username_input)
        form.addRow("Display name (shown in app)", self.display_input)
        form.addRow("Password", self.password_input)
        form.addRow("Confirm password", self.confirm_input)
        layout.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color:{palette['red']};")
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Exit")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Create administrator")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def save(self):
        username = self.username_input.text().strip()
        display_name = self.display_input.text().strip()
        password = self.password_input.text()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            self.error_label.setText("Use 3–32 letters, digits, dots, dashes, or underscores for the username.")
            return
        if not display_name or len(display_name) > 80:
            self.error_label.setText("Enter a display name of up to 80 characters.")
            return
        if len(password) < 6:
            self.error_label.setText("Use a password with at least 6 characters.")
            return
        if password != self.confirm_input.text():
            self.error_label.setText("The passwords do not match.")
            return
        business_name = self.business_name_input.text().strip()
        if not business_name or len(business_name) > 120:
            self.error_label.setText("Enter a business name of up to 120 characters.")
            return
        profile = self.business_type_input.currentText()
        if profile not in BUSINESS_PROFILES:
            self.error_label.setText("Choose a valid starter business profile.")
            return
        try:
            self.store.configure_initial_admin(username, display_name, password)
            self.store.set_setting("business_name", business_name)
            self.store.set_setting("business_type", profile)
            self.store.switch_workspace(profile, business_name)
            self.store.set_setting("pos_enabled", "1" if self.pos_input.isChecked() else "0")
            self.store.set_setting("business_currency", self.currency_input.currentText())
            self.store.set_setting("theme", self.theme_input.currentData())
            self.store.apply_profile(profile)
            if self.pos_input.isChecked() and profile not in POS_UNAVAILABLE_PROFILES:
                self.store.set_enabled("Point of Sale", True)
            application = QApplication.instance()
            if application:
                application.setStyleSheet(themed_style(palette_for(self.store)))
        except sqlite3.IntegrityError:
            self.error_label.setText("That username already exists. Choose another one.")
            return
        except (PermissionError, ValueError, sqlite3.DatabaseError) as exc:
            self.error_label.setText(f"The setup could not finish: {exc}")
            return
        self.accept()

class LoginDialog(QDialog):
    """Local account login dialog. Validates credentials against the SQLite user store."""
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.username = ""
        self.role = ""
        self.display_name = ""
        palette = palette_for(store)
        self.setWindowTitle("Sign in | NextGen ERP")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet(themed_style(palette))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)
        brand = QLabel("⬡  NextGen ERP")
        brand.setStyleSheet("font-size:22pt;font-weight:900;padding:4px 0 8px;")
        subtitle = QLabel("Sign in to your business workspace")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{palette['muted']};")
        layout.addWidget(brand)
        layout.addWidget(subtitle)
        form = QFormLayout()
        form.setSpacing(12)
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Username")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.returnPressed.connect(self.attempt_login)
        form.addRow("Username", self.user_input)
        form.addRow("Password", self.password_input)
        layout.addLayout(form)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color:{palette['red']};")
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton("Exit")
        self.cancel_button.clicked.connect(self.reject)
        self.login_button = QPushButton("Sign in")
        self.login_button.setObjectName("primary")
        self.login_button.clicked.connect(self.attempt_login)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.login_button)
        layout.addLayout(buttons)
        self._failed_attempts = 0
        self._locked_until = 0.0
        self.user_input.setFocus()

    def attempt_login(self):
        now = time.monotonic()
        if now < self._locked_until:
            remaining = max(1, int(self._locked_until - now))
            self.error_label.setText(f"Too many failed attempts. Try again in {remaining} seconds.")
            return
        username = self.user_input.text().strip()
        password = self.password_input.text()
        row = self.store.conn.execute(
            "SELECT username, display_name, role, password_hash, active FROM users WHERE username=? COLLATE NOCASE",
            (username,)
        ).fetchone()
        if not row or not row["active"] or not check_password(password, row["password_hash"]):
            self._failed_attempts += 1
            if self._failed_attempts >= 5:
                self._locked_until = time.monotonic() + 30
                self._failed_attempts = 0
                self.error_label.setText("Too many failed attempts. Sign-in is paused for 30 seconds.")
            else:
                self.error_label.setText("The username or password is incorrect, or this account is inactive.")
            self.password_input.selectAll()
            self.password_input.setFocus()
            return
        self._failed_attempts = 0
        self.username = row["username"]
        self.role = row["role"]
        self.display_name = str(row["display_name"] or row["username"]).strip()
        self.accept()

class MetricCard(QFrame):
    """Glass-style dashboard metric tile with animated number."""
    def __init__(self, title, value, status, accent, subtitle, parent=None, palette=None, numeric_value=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumHeight(128)
        self.setSizePolicy(__import__('PySide6.QtWidgets', fromlist=['QSizePolicy']).QSizePolicy.Policy.Expanding,
                           __import__('PySide6.QtWidgets', fromlist=['QSizePolicy']).QSizePolicy.Policy.Minimum)
        colors = palette or DARK_C
        # Soft glass border + subtle left accent bar via stylesheet
        self.setStyleSheet(
            f"QFrame#panel {{ background:{colors['panel']}; border:1px solid {colors['border']}; "
            f"border-radius:14px; border-left:4px solid {accent}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        top = QHBoxLayout()
        heading = QLabel(str(title)); heading.setWordWrap(True)
        heading.setStyleSheet(f"font-size:10pt;font-weight:700;color:{colors['muted']};background:transparent;border:none;")
        dot = QLabel("●"); dot.setStyleSheet(f"color:{accent};font-size:11pt;background:transparent;border:none;")
        top.addWidget(heading, 1); top.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        # Prefer animated count-up when a raw number is provided
        try:
            from fx import AnimatedNumber
            if numeric_value is not None:
                number = AnimatedNumber(0, fmt=lambda v: f"{int(round(v)):,}")
                number.setStyleSheet(f"color:{accent};font-size:24pt;font-weight:900;background:transparent;border:none;")
                number.set_value(float(numeric_value), ms=900, animate=True, delay=80)
            else:
                number = QLabel(str(value)); number.setWordWrap(True)
                number.setStyleSheet(f"color:{accent};font-size:23pt;font-weight:900;background:transparent;border:none;")
        except Exception:
            number = QLabel(str(value)); number.setWordWrap(True)
            number.setStyleSheet(f"color:{accent};font-size:23pt;font-weight:900;background:transparent;border:none;")
        layout.addWidget(number)
        detail = QLabel(str(subtitle)); detail.setWordWrap(True)
        detail.setStyleSheet(f"font-size:9pt;color:{colors['muted']};background:transparent;border:none;")
        layout.addWidget(detail)
        if status:
            st = QLabel(str(status)); st.setWordWrap(True)
            st.setStyleSheet(f"font-size:8.5pt;color:{accent};font-weight:600;background:transparent;border:none;")
            layout.addWidget(st)
        layout.addStretch(1)


class ChartPanel(QFrame):
    """Seven-day booked-sales chart rendered with the active theme palette."""
    def __init__(self, title, values=None, currency="KES", palette=None, parent=None):
        super().__init__(parent)
        self.title=str(title)
        self.values=list(values or [])
        self.currency=str(currency)
        self.colors=palette or DARK_C
        self.setObjectName("panel")
        self.setMinimumHeight(220)
        self.setSizePolicy(__import__('PySide6.QtWidgets', fromlist=['QSizePolicy']).QSizePolicy.Policy.Expanding,
                           __import__('PySide6.QtWidgets', fromlist=['QSizePolicy']).QSizePolicy.Policy.Minimum)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left,right,top,bottom=18,16,16,16
        title_font=QFont(self.font()); title_font.setPointSize(12); title_font.setBold(True)
        painter.setFont(title_font); painter.setPen(QColor(self.colors["text"]))
        painter.drawText(QRectF(left,top,self.width()-left-right,24),Qt.AlignLeft|Qt.AlignVCenter,self.title)
        plot=QRectF(left,top+40,max(0,self.width()-left-right),max(0,self.height()-top-bottom-50))
        valid=[(str(label),Decimal(str(value))) for label,value in self.values]
        if not valid or all(value == 0 for _,value in valid):
            painter.setPen(QColor(self.colors["muted"]))
            painter.drawText(plot,Qt.AlignCenter,"Completed sales will appear here as you trade.")
            painter.end(); return
        maximum=max(value for _,value in valid)
        if maximum <= 0: maximum=Decimal("1")
        baseline=plot.bottom()-22
        painter.setPen(QPen(QColor(self.colors["border"]),1))
        painter.drawLine(QPointF(plot.left(),baseline),QPointF(plot.right(),baseline))
        gap=10
        slot=max(1,(plot.width()-gap*(len(valid)-1))/len(valid))
        bar_width=min(42,slot*0.56)
        label_font=QFont(self.font()); label_font.setPointSize(8)
        painter.setFont(label_font)
        for i,(label,value) in enumerate(valid):
            x=plot.left()+i*(slot+gap)+(slot-bar_width)/2
            height=float(value/maximum)*max(1,plot.height()-46)
            bar=QRectF(x,baseline-height,bar_width,height)
            painter.setPen(Qt.NoPen); painter.setBrush(QColor(self.colors["blue"]))
            painter.drawRoundedRect(bar,4,4)
            painter.setPen(QColor(self.colors["muted"]))
            painter.drawText(QRectF(plot.left()+i*(slot+gap),baseline+4,slot,18),Qt.AlignHCenter|Qt.AlignTop,label)
            if value:
                painter.setPen(QColor(self.colors["text"]))
                painter.drawText(QRectF(x-12,baseline-height-18,bar_width+24,16),Qt.AlignCenter,f"{self.currency} {value:,.0f}")
        painter.end()


class App(QMainWindow):
    def __init__(self, store, username, role, display_name=""):
        super().__init__(); self.store=store; self.username=username; self.role=role; self.display_name=(str(display_name or username).strip() or username); self.active="Dashboard"; self.nav={}
        self.store.actor_role=role; self.store.operations.actor_role=role; self.store.operations.actor_username=username; self.setWindowTitle((self.store.setting("business_name", "Your Business").strip() or "Your Business") + " | NextGen ERP"); self.resize(1480,930); self.setMinimumSize(1000,650); self.theme = self.store.setting("theme", "light"); self.palette = palette_for(self.store, self.theme); self.setStyleSheet(themed_style(self.palette))
        root=QWidget(); self.setCentralWidget(root); main=QHBoxLayout(root); main.setContentsMargins(0,0,0,0); main.setSpacing(0)
        self.sidebar_expanded = True
        self.sidebar_width_expanded = 238
        self.sidebar_width_collapsed = 72
        self.sidebar=QFrame(); self.sidebar.setFixedWidth(self.sidebar_width_expanded); self.sidebar.setStyleSheet(f"background:{self.palette['sidebar']};border-right:1px solid {self.palette['border']};")
        sv=QVBoxLayout(self.sidebar); sv.setContentsMargins(10,14,10,12); sv.setSpacing(4)
        self.brand_label=QLabel()
        self.brand_label.setWordWrap(True)
        self.brand_label.setMaximumHeight(58)
        self.brand_label.setStyleSheet("font-size:14pt;font-weight:900;padding:3px 5px;")
        self.update_branding()
        sv.addWidget(self.brand_label)
        self.tagline_label=QLabel("SMARTER BUSINESS. BUILT TO ADAPT."); self.tagline_label.setStyleSheet(f"color:{self.palette['muted']};font-size:7pt;font-weight:700;padding:0 7px 6px;"); sv.addWidget(self.tagline_label)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        navhost=QWidget(); self.navlay=QVBoxLayout(navhost); self.navlay.setContentsMargins(0,8,0,8); self.navlay.setSpacing(3)
        # The scroll area's child must size to its content, not stretch to the
        # viewport. Align the child widget itself to the top; do not align the
        # layout, which can vertically centre the menu inside the sidebar.
        navhost.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        section_name="SCHOOL OPERATIONS" if self.store.setting("business_type","")=="School / training centre" else "WORKSPACE"
        self.add_nav_section(section_name, self.workspace_navigation())
        if self.role == "Admin":
            self.add_nav_section("ADMINISTRATION",["User Management","Role Permissions","Module Manager","Feature Store","Integrations","System Settings","Database and Backup"])
        elif self.role == "Manager" or "manage_users" in self.store.role_permissions(self.role):
            self.add_nav_section("ADMINISTRATION",["User Management","Database and Backup"])
        scroll.setWidget(navhost); sv.addWidget(scroll,1)
        if self.store.setting("business_type","")=="School / training centre" and "Students & Enrolment" in self.nav:
            shortcuts=QFrame(); shortcuts.setObjectName("panelAlt")
            shortcut_layout=QVBoxLayout(shortcuts); shortcut_layout.setContentsMargins(8,8,8,8); shortcut_layout.setSpacing(5)
            shortcut_heading=QLabel("LEARNER OPERATIONS")
            shortcut_heading.setStyleSheet(f"color:{self.palette['muted']};font-size:8pt;font-weight:800;padding:0 3px;")
            shortcut_layout.addWidget(shortcut_heading)
            shortcut_grid=QGridLayout(); shortcut_grid.setContentsMargins(0,0,0,0); shortcut_grid.setHorizontalSpacing(5); shortcut_grid.setVerticalSpacing(5)
            def open_student_tab(label):
                self.show_page("Students & Enrolment")
                page=self.pages.get("Students & Enrolment")
                if page:
                    for tabs in page.findChildren(QTabWidget):
                        for index in range(tabs.count()):
                            if tabs.tabText(index)==label:
                                tabs.setCurrentIndex(index); return
            for index,(caption,tab_label) in enumerate((("Register","Students & admissions"),("Admissions","Admissions"),("Attendance","Attendance"),("Fees","Fee accounts"))):
                shortcut=QPushButton(caption); shortcut.setToolTip(f"Open {tab_label}")
                shortcut.clicked.connect(lambda checked=False,target=tab_label:open_student_tab(target))
                shortcut_grid.addWidget(shortcut,index//2,index%2)
            shortcut_layout.addLayout(shortcut_grid); sv.addWidget(shortcuts)
        status=QFrame(); status.setObjectName("panelAlt"); sl=QVBoxLayout(status); sl.setContentsMargins(12,10,12,10)
        sl.addWidget(QLabel(f"<span style='color:{self.palette['green']}'>●</span>  LOCAL DATABASE"))
        workspace_display = str(getattr(self.store, "workspace_path", DB_PATH))[:34] + "…"
        sl.addWidget(QLabel(f"<span style='color:{self.palette['muted']};font-size:8pt'>{workspace_display}</span>"))
        sv.addWidget(status)
        main.addWidget(self.sidebar)
        right=QWidget(); rv=QVBoxLayout(right); rv.setContentsMargins(0,0,0,0); rv.setSpacing(0); main.addWidget(right,1)
        top=QFrame(); self.topbar=top; top.setFixedHeight(66); top.setStyleSheet(f"background:{self.palette['sidebar']};border-bottom:1px solid {self.palette['border']};")
        th=QHBoxLayout(top); th.setContentsMargins(16,8,18,8)
        self.sidebar_toggle=QPushButton("☰"); self.sidebar_toggle.setObjectName("sidebarToggle"); self.sidebar_toggle.setToolTip("Collapse or expand the sidebar"); self.sidebar_toggle.clicked.connect(self.toggle_sidebar); th.addWidget(self.sidebar_toggle)
        self.search=QLineEdit(); self.search.setMaximumWidth(390); self.search.setPlaceholderText("⌕   Search records, modules, customers…"); self.search.returnPressed.connect(self.search_records); th.addWidget(self.search)
        th.addStretch()
        self.theme_btn=QPushButton("☀ Light mode" if self.theme == "dark" else "☾ Dark mode"); self.theme_btn.clicked.connect(self.toggle_theme); th.addWidget(self.theme_btn)
        self.syncpill=QPushButton(); self.syncpill.setObjectName("statusChip"); self.syncpill.setFlat(True)
        self.syncpill.setToolTip("Backup status — click to open Database and Backup")
        self.syncpill.clicked.connect(lambda:self.show_page("Database and Backup") if self._can_open_backup_page() else self.backups.run_now("manual") if "backup" in self.store.role_permissions(self.role) else None)
        th.addWidget(self.syncpill)
        self.profile_chip=QLabel(f"  {self.display_name}  ·  {role}  "); self.profile_chip.setStyleSheet(f"font-weight:700;padding:7px 12px;background:{self.palette['panel2']};border:1px solid {self.palette['border']};border-radius:999px;color:{self.palette['text']};"); th.addWidget(self.profile_chip)
        out=QPushButton("Sign out"); out.clicked.connect(self.sign_out); th.addWidget(out)
        rv.addWidget(top)
        self.stack=QStackedWidget(); rv.addWidget(self.stack,1)
        self.pages={}
        self.toast=Toast(self)
        self.backups=BackupManager(self.store,self.toast,parent=self)
        self.backups.state_changed.connect(self.refresh_syncpill)
        QShortcut(QKeySequence("Ctrl+K"),self,self.open_palette)
        QShortcut(QKeySequence("Ctrl+L"),self,self.lock_screen)
        self.idle_watcher=IdleWatcher(lambda:self.store.setting("idle_lock_minutes","10"),self)
        self.idle_watcher.idle.connect(self.lock_screen)
        self.refresh_syncpill()
        self.show_page("Dashboard")
        self.showMaximized()
        self.timer=QTimer(self); self.timer.timeout.connect(self.refresh_syncpill); self.timer.start(8000)
        self.backups.start()
    def closeEvent(self,event):
        timer=getattr(self,"timer",None)
        if timer is not None:
            timer.stop()
        watcher=getattr(self,"idle_watcher",None)
        if watcher is not None: watcher.stop()
        manager=getattr(self,"backups",None)
        if manager is not None:
            manager.stop()
            try: manager.backup_on_exit()
            except Exception: pass
        super().closeEvent(event)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        toast=getattr(self,"toast",None)
        if toast is not None and toast.isVisible(): toast.reposition()

    def lock_screen(self):
        if getattr(self,"_locked",False) or not self.isVisible():
            return
        self._locked=True
        self.idle_watcher.paused=True
        try:
            def verify(password):
                row=self.store.conn.execute("SELECT password_hash,active FROM users WHERE username=? COLLATE NOCASE",(self.username,)).fetchone()
                return bool(row and row["active"] and check_password(password,row["password_hash"]))
            dlg=LockDialog(self,self.display_name,verify,themed_style(self.palette))
            dlg.exec()
            self.store.log(self.username,"Screen unlocked" if not dlg.signed_out else "Signed out from lock screen","")
            if dlg.signed_out:
                self._locked=False; self.idle_watcher.paused=False
                self.sign_out(); return
        finally:
            self._locked=False
            self.idle_watcher.paused=False
            self.idle_watcher.last_activity=time.monotonic()

    # ------------------------------------------------------------------ Business Pulse
    def currency_code(self):
        return self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]

    def pulse_service(self):
        return PulseService(self.store.operations.conn,self.store.setting,self.store.set_setting,self.currency_code())

    def set_daily_goal(self,pulse=None):
        svc=self.pulse_service(); pulse=pulse or svc.compute()
        start=float(pulse.goal or pulse.suggested_goal or 10000)
        value,ok=QInputDialog.getDouble(self,"Daily sales goal",f"How much do you want to sell each day? ({self.currency_code()})\n0 removes the goal.",start,0,100000000,0)
        if not ok: return
        self.store.set_setting("pulse_goal_daily",str(int(value)))
        self.pages.pop("Dashboard",None); self.show_page("Dashboard")

    def celebrate(self):
        """Toast any newly earned achievement once (goal reached, streaks, milestones, best day)."""
        try:
            svc=self.pulse_service(); pulse=svc.compute()
            fresh=pulse.new_achievements[:3]
            if not fresh: return
            self.toast.show_message("\n".join(f"{a.icon}  {a.title} — {a.detail}" for a in fresh),ok=True,seconds=10)
            svc.mark_seen(pulse.new_achievements)
        except Exception:
            logging.getLogger("nextgen").exception("achievement check failed")

    def maybe_show_recap(self,pulse,svc):
        if os.environ.get("NEXTGEN_NO_POPUPS"): return
        if not svc.recap_due(): return
        svc.mark_recap_shown()
        if pulse.lifetime_sales==0: return
        first=(self.display_name.split() or [self.username])[0]
        RecapDialog(self,pulse,first,self.palette,self.currency_code(),themed_style(self.palette)).exec()

    # ------------------------------------------------------------------ Command palette
    def open_palette(self):
        if getattr(self,"_locked",False): return
        items=[(f"Go to {name}",name.lower(),("page",name)) for name in self.nav]
        extra=[("Back up now","backup save data",("call",lambda:self.backups.run_now("manual"))),
               ("Printer setup","print paper receipt thermal",("call",self.open_printer_setup)),
               ("Lock screen","lock secure away",("call",self.lock_screen)),
               ("Switch light / dark theme","theme colour dark light",("call",self.toggle_theme)),
               ("Set daily sales goal","goal target pulse",("call",self.set_daily_goal)),
               ("Open or close shift (Z-report)","shift till cash drawer z report float",("call",self.shift_action)),
               ("Open log folder","log support help",("call",lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir(APP_DIR))))))]
        if "Point of Sale" in self.nav: extra.insert(0,("New sale — Point of Sale","pos checkout till sell",("page","Point of Sale")))
        if "Reports" in self.nav: extra.append(("Report centre — export CSV / print","reports export csv profit tax debtors",("page","Reports")))
        dlg=CommandPalette(self,extra+items,themed_style(self.palette)); dlg.exec()
        if dlg.chosen:
            kind,target=dlg.chosen
            self.show_page(target) if kind=="page" else target()

    # ------------------------------------------------------------------ Shifts / Z-report
    def shift_service(self):
        return ShiftService(self.store.operations.conn,actor=lambda:self.username,log=lambda action,detail:self.store.log(self.username,action,detail))

    def shift_action(self):
        svc=self.shift_service()
        (self.close_shift_dialog if svc.current() else self.open_shift_dialog)()

    def open_shift_dialog(self):
        svc=self.shift_service()
        if svc.current():
            QMessageBox.information(self,"Shift already open","A shift is already open."); return
        value,ok=QInputDialog.getDouble(self,"Open shift",f"Cash already in the drawer (opening float, {self.currency_code()}):",0,0,100000000,2)
        if not ok: return
        try: svc.open_shift(str(value))
        except ShiftError as exc: QMessageBox.warning(self,"Cannot open shift",str(exc)); return
        self.pages.pop("Point of Sale",None)
        if self.active=="Point of Sale": self.show_page("Point of Sale")
        self.toast.show_message(f"✔  Shift opened with a float of {self.currency_code()} {value:,.2f}. Have a great day!",ok=True,seconds=6)

    def close_shift_dialog(self):
        svc=self.shift_service(); shift=svc.current()
        if not shift:
            QMessageBox.information(self,"No open shift","There is no open shift to close."); return
        value,ok=QInputDialog.getDouble(self,"Close shift","Count the cash in the drawer and enter the total "+f"({self.currency_code()}).\nThe system compares it with what the till should hold.",0,0,100000000,2)
        if not ok: return
        notes,_=QInputDialog.getText(self,"Close shift","Notes (optional):")
        try: shift_id=svc.close_shift(str(value),notes)
        except ShiftError as exc: QMessageBox.warning(self,"Cannot close shift",str(exc)); return
        summary=svc.summary(shift_id); var=summary["variance"]
        verdict="balanced ✔" if var==0 else (f"OVER by {self.currency_code()} {var:,.2f}" if var>0 else f"SHORT by {self.currency_code()} {abs(var):,.2f}")
        self.pages.pop("Point of Sale",None)
        if self.active=="Point of Sale": self.show_page("Point of Sale")
        if QMessageBox.question(self,"Shift closed",f"Sales: {summary['sales_count']} · {self.currency_code()} {summary['gross_sales']:,.2f}\nExpected cash: {summary['expected_cash']:,.2f}\nCounted: {summary['counted_cash']:,.2f}\nResult: {verdict}\n\nPrint the Z-report now?")==QMessageBox.Yes:
            self.print_z_report(shift_id)

    def print_z_report(self,shift_id):
        svc=self.shift_service(); summary=svc.summary(shift_id)
        render=lambda layout:render_shift_report(self.store,summary,layout)[1]
        self._print_service().print_document(self,f"Z-Report #{shift_id}",render,f"Z-report #{shift_id}")

    def shift_problem(self):
        """Used by the POS: text explaining why a sale is blocked, or '' when it may proceed."""
        if self.store.setting("require_shift","0")!="1": return ""
        return "" if self.shift_service().current() else "No shift is open. Open a shift (Point of Sale → Open shift) before selling."

    def _can_open_backup_page(self):
        return self.role in {"Admin","Manager"} or "manage_users" in self.store.role_permissions(self.role)

    def toggle_sidebar(self):
        self.sidebar_expanded = not getattr(self, "sidebar_expanded", True)
        if self.sidebar_expanded:
            self.sidebar.setFixedWidth(self.sidebar_width_expanded)
            self.tagline_label.show()
            self.brand_label.setStyleSheet("font-size:14pt;font-weight:900;padding:3px 5px;")
            self.sidebar_toggle.setText("☰")
        else:
            self.sidebar.setFixedWidth(self.sidebar_width_collapsed)
            self.tagline_label.hide()
            self.brand_label.setStyleSheet("font-size:11pt;font-weight:900;padding:2px;")
            self.sidebar_toggle.setText("»")
        self.rebuild_nav_labels()

    def setup_auto_backup_timer(self):
        """Settings changed: re-read the schedule."""
        manager=getattr(self,"backups",None)
        if manager is not None:
            manager.reconfigure()

    def rebuild_nav_labels(self):
        for name, btn in list(getattr(self, "nav", {}).items()):
            icon = MODULES.get(name, ("◇", ""))[0]
            if getattr(self, "sidebar_expanded", True):
                display = {
                    "Sales": "Sales and Receipts", "Purchases": "Purchases and Suppliers",
                    "Inventory": "Stock and Catalogue", "Finance": "Cashbook and Cashflow",
                    "CRM": "Customers", "Repair Tickets": "Service and Repairs",
                    "Reports": "Business Reports", "Point of Sale": "Point of Sale",
                    "HR & Payroll": "HR and Payroll", "Batches & Expiry": "Batches and Expiry",
                    "Menu & Recipes": "Menu and Recipes", "Students & Enrolment": "Students and Enrolment",
                    "Database and Backup": "Database and Backup",
                }.get(name, name)
                btn.setText(f"{icon}    {display}")
                btn.setToolTip(MODULES.get(name, ("", name))[1] if name in MODULES else name)
            else:
                btn.setText(icon)
                btn.setToolTip(name)

    def workspace_navigation(self):
        """Build navigation only from enabled modules; admin pages remain separate."""
        pos_on = self.store.setting("pos_enabled", "0") == "1"
        profile = self.store.setting("business_type", "")
        # Keep the POS feature row in sync with the high-level switch. Older
        # workspaces (or ones created before Point of Sale was added to
        # DEFAULT_FIELDS) can have pos_enabled=1 while the features row is still
        # disabled; that made the item vanish from the sidebar after Dashboard.
        if pos_on and profile not in POS_UNAVAILABLE_PROFILES:
            feature = self.store.feature("Point of Sale")
            if feature and not feature["enabled"]:
                try:
                    self.store.set_enabled("Point of Sale", True)
                except (ValueError, PermissionError):
                    pass  # leave as-is if records/dependency rules block the change
            elif not feature:
                # Extremely old workspace: ensure the row exists so the rest of the app can see it.
                try:
                    self.store.workspace_conn.execute(
                        "INSERT OR IGNORE INTO features(name,description,fields_json,enabled,custom,created_at) VALUES(?,?,?,?,0,?)",
                        ("Point of Sale", MODULES["Point of Sale"][1],
                         json.dumps(DEFAULT_FIELDS.get("Point of Sale", [])), 0, stamp()))
                    self.store.workspace_conn.commit()
                    self.store.set_enabled("Point of Sale", True)
                except Exception:
                    pass
        enabled = {row["name"] for row in self.store.features(True)}
        custom = [row["name"] for row in self.store.features(True) if row["name"] not in MODULES]
        return workspace_navigation_names(enabled, pos_on, custom)

    def add_nav_section(self,heading,names):
        """Add only authorised, enabled destinations; never render empty groups."""
        visible=[]
        admin_only={"Role Permissions","Module Manager","Feature Store","Integrations","System Settings"}
        for name in names:
            if self.role != "Admin" and name in admin_only:
                continue
            if name == "Database and Backup":
                if self.role != "Admin" and self.role != "Manager" and "manage_users" not in self.store.role_permissions(self.role):
                    continue
            if name == "User Management":
                if self.role != "Admin" and "manage_users" not in self.store.role_permissions(self.role):
                    continue
            else:
                feature=self.store.feature(name)
                if feature and not self.store.role_has_module(self.role,name):
                    continue
            visible.append(name)

        # A role with no destinations in a section should not leave a dangling
        # heading or a large blank area in the sidebar.
        if not visible:
            return

        lab=QLabel(heading)
        lab.setStyleSheet(
            f"color:{self.palette['muted']};font-size:8pt;font-weight:800;"
            "letter-spacing:1px;padding:9px 9px 5px;"
        )
        self.navlay.addWidget(lab)

        display_names={
            "Sales":"Sales and Receipts",
            "Purchases":"Purchases and Suppliers",
            "Inventory":"Stock and Catalogue",
            "Finance":"Cashbook and Cashflow",
            "CRM":"Customers",
            "Repair Tickets":"Service and Repairs",
            "Reports":"Business Reports",
            "Point of Sale":"Point of Sale",
            "Fees & Billing":"Fees and Billing",
            "Classes & Timetable":"Classes and Timetable",
            "Library":"Library",
            "Job Cards":"Job Cards",
            "Fleet & Assets":"Fleet and Assets",
            "HR & Payroll":"HR and Payroll",
            "Batches & Expiry":"Batches and Expiry",
            "Menu & Recipes":"Menu and Recipes",
            "Students & Enrolment":"Students and Enrolment",
        }
        for name in visible:
            icon=MODULES.get(name,("◇",""))[0]
            b=QPushButton(f"{icon}    {display_names.get(name,name)}")
            b.setObjectName("nav")
            b.setMinimumHeight(40)
            b.setMaximumHeight(42)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(MODULES.get(name,("",name))[1] if name in MODULES else name)
            # Give every navigation destination a visible surface in both themes.
            # Explicit per-button styling avoids stale QSS object-name state after
            # theme changes and keeps inactive items legible on the light sidebar.
            b.setStyleSheet(
                f"QPushButton {{ text-align:left; padding:8px 12px; "
                f"background:{self.palette['panel2']}; color:{self.palette['text']}; "
                f"border:1px solid {self.palette['border']}; border-radius:8px; "
                "font-weight:600; min-height:20px; }"
                f"QPushButton:hover {{ background:{self.palette['hover']}; "
                f"border-color:{self.palette['blue']}; }}"
            )
            b.clicked.connect(lambda checked=False,n=name:self.show_page(n))
            self.navlay.addWidget(b)
            self.nav[name]=b

    def refresh_syncpill(self):
        manager=getattr(self,"backups",None)
        if manager is None or getattr(self,"syncpill",None) is None:
            return
        level,text=manager.status()
        color={"ok":self.palette["green"],"busy":self.palette["cyan"],"warn":self.palette["orange"],"bad":self.palette["red"]}.get(level,self.palette["muted"])
        self.syncpill.setText(text)
        self.syncpill.setStyleSheet(f"color:{color};font-size:9pt;font-weight:700;padding:7px 12px;background:{self.palette['panel']};border:1px solid {color if level=='bad' else self.palette['border']};border-radius:999px;")
    def _authorize(self, capability):
        if capability in self.store.role_permissions(self.role):
            return True
        QMessageBox.warning(self, "Access denied", "Your account does not have permission to perform this action.")
        return False
    def show_page(self,name):
        super_admin_pages = {"Role Permissions", "Module Manager", "Feature Store", "Integrations", "System Settings", "Database and Backup"}
        admin_pages = super_admin_pages | {"User Management"}
        valid_pages=set(MODULES) | {r["name"] for r in self.store.features()}
        if name not in valid_pages:
            QMessageBox.warning(self, "Unknown workspace", "That workspace is not available in this installation.")
            return
        if self.role != "Admin" and self.role not in self.store.assignable_roles():
            QMessageBox.warning(self, "Access denied", "Your account does not have access to this workspace.")
            return
        if name in super_admin_pages and self.role != "Admin":
            # Managers may open Database and Backup for auto-backup schedule only
            if not (name == "Database and Backup" and (self.role == "Manager" or "manage_users" in self.store.role_permissions(self.role))):
                QMessageBox.warning(self, "Access denied", "Only an administrator can open this workspace.")
                return
        if name == "User Management" and self.role != "Admin" and "manage_users" not in self.store.role_permissions(self.role):
            QMessageBox.warning(self, "Access denied", "Your role cannot manage users.")
            return
        # Admin tools are capability-gated above — not floor modules in role_modules
        admin_tools = {"User Management", "Role Permissions", "Module Manager", "Feature Store", "Integrations", "System Settings", "Database and Backup"}
        feature = self.store.feature(name)
        if name not in admin_tools:
            if feature and not self.store.role_has_module(self.role,name):
                QMessageBox.warning(self, "Access denied", f"Your role has not been granted access to {name}.")
                return
            if feature and not feature["enabled"] and name != "Dashboard":
                QMessageBox.information(self, "Module disabled", f"{name} is currently disabled by the administrator.")
                return
        self.active=name
        for n,b in self.nav.items():
            active_button=(n==name)
            b.setObjectName("navActive" if active_button else "nav")
            if active_button:
                # Strong, accessible selected state in light and dark themes.
                b.setStyleSheet(
                    f"QPushButton {{ text-align:left; padding:8px 12px; "
                    f"background:{self.palette['blue']}; color:{self.palette['on_blue']}; "
                    f"border:1px solid {self.palette['blue']}; "
                    f"border-left:4px solid {self.palette['cyan']}; "
                    "border-radius:8px; font-weight:700; min-height:20px; }"
                    f"QPushButton:hover {{ background:{self.palette['blue']}; "
                    f"border-color:{self.palette['cyan']}; }}"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ text-align:left; padding:8px 12px; "
                    f"background:{self.palette['panel2']}; color:{self.palette['text']}; "
                    f"border:1px solid {self.palette['border']}; border-radius:8px; "
                    "font-weight:600; min-height:20px; }"
                    f"QPushButton:hover {{ background:{self.palette['hover']}; "
                    f"border-color:{self.palette['blue']}; }}"
                )
        if name in self.pages:
            self.stack.setCurrentWidget(self.pages[name])
            if not getattr(self, "_theme_switching", False):
                self._fade_page(self.pages[name])
            return
        if name=="Dashboard": page=self.dashboard()
        elif name=="Module Manager": page=self.module_manager()
        elif name=="User Management": page=self.user_management()
        elif name=="System Settings": page=self.settings_page()
        elif name=="Database and Backup": page=self.database_page()
        elif name=="Role Permissions": page=self.role_permissions()
        elif name=="Feature Store": page=self.feature_store()
        elif name=="Integrations": page=self.integrations()
        elif name=="Inventory": page=self.inventory_page()
        elif name=="Sales": page=self.sales_page()
        elif name=="Point of Sale": page=self.pos_page()
        elif name=="Returns Desk": page=self.returns_page()
        elif name=="Purchases": page=self.purchases_page()
        elif name=="Finance": page=self.finance_page()
        elif name=="CRM": page=self.crm_page()
        elif name=="Reports": page=self.reports_page()
        elif name=="Repair Tickets": page=self.repair_page()
        elif name=="Students & Enrolment": page=self.students_page()
        elif name=="Appointments": page=self.appointments_page()
        elif name=="Batches & Expiry": page=self.batches_page()
        elif name=="Menu & Recipes": page=self.restaurant_page()
        else: page=self.records_page(name)
        # Apply consistent sizing to every data table, including bespoke dialogs/pages
        # that do not use the shared _table() factory. Long tables remain horizontally
        # scrollable rather than silently clipping the last column/header.
        for data_table in page.findChildren(QTableWidget):
            header = data_table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeToContents)
            header.setStretchLastSection(True)
            data_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            data_table.setWordWrap(False)
            data_table.setMinimumWidth(0)
        self.pages[name]=page; self.stack.addWidget(page); self.stack.setCurrentWidget(page); self._fade_page(page)
        saved=getattr(self,"_pending_theme_states",{}).pop(name,None)
        if saved:
            QApplication.processEvents()
            self._restore_page_state(page,saved)
    def _fade_page(self, page):
        """Short, subtle fade-in when switching ERP pages; no blocking timers."""
        try:
            from PySide6.QtWidgets import QGraphicsOpacityEffect
            old_animation = getattr(self, "_page_fade_animation", None)
            if old_animation is not None:
                old_animation.stop()
            effect = QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(effect)
            effect.setOpacity(0.35)
            animation = QPropertyAnimation(effect, b"opacity", self)
            animation.setDuration(150)
            animation.setStartValue(0.35)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            animation.finished.connect(lambda p=page: p.setGraphicsEffect(None) if p is not None else None)
            self._page_fade_animation = animation
            animation.start()
        except Exception:
            # Visual polish must never prevent a page from opening.
            pass

    def page_container(self,title,subtitle):
        # Every page uses the same scrollable content host, eliminating clipped forms/cards.
        outer=QWidget(); outer.setObjectName("pageOuter")
        outer_layout=QVBoxLayout(outer); outer_layout.setContentsMargins(0,0,0,0); outer_layout.setSpacing(0)
        scroll=QScrollArea(); scroll.setObjectName("pageScroll"); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded); scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content=QWidget(); content.setObjectName("pageContent"); content.setMinimumWidth(0)
        v=QVBoxLayout(content); v.setContentsMargins(26,24,26,28); v.setSpacing(18); v.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        head=QHBoxLayout(); head.setSpacing(16); col=QVBoxLayout(); col.setSpacing(7)
        h=QLabel(title); h.setWordWrap(True); h.setStyleSheet("font-size:20pt;font-weight:800;letter-spacing:-0.3px;"); col.addWidget(h)
        sub=QLabel(subtitle); sub.setWordWrap(True); sub.setStyleSheet(f"color:{self.palette['muted']};font-size:9pt;"); col.addWidget(sub)
        head.addLayout(col,1); v.addLayout(head)
        scroll.setWidget(content); outer_layout.addWidget(scroll)
        return outer,v
    def panel(self):
        f=QFrame(); f.setObjectName("panel"); return f

    def dashboard(self):
        """Immersive glass dashboard (Library-inspired): hero, KPIs, charts, heat-map, rankings, insights."""
        from erp_dashboard import DashboardView
        currency = self.currency_code()
        dark = self.theme == "dark"
        view = DashboardView(
            self.store,
            self.palette,
            dark=dark,
            user_name=self.display_name or self.username,
            currency=currency,
            parent=self,
        )
        view.navigate.connect(self.show_page)
        view.search.connect(lambda q: self.open_palette() if not q else self.open_palette())
        view.set_goal.connect(lambda: self.set_daily_goal())
        # Pulse celebrations still fire once when dashboard opens
        try:
            if "Sales" in self.nav or "Point of Sale" in self.nav:
                pulse_svc = self.pulse_service()
                pulse = pulse_svc.compute()
                QTimer.singleShot(400, lambda: self.maybe_show_recap(pulse, pulse_svc))
                QTimer.singleShot(1000, self.celebrate)
        except Exception:
            pass
        return view


    def _field_defs(self, fields):
        defs=[]
        for item in fields:
            if isinstance(item, dict):
                d={"name":str(item.get("name","Field"))[:60],"type":item.get("type","text"),"required":bool(item.get("required",False)),"options":item.get("options",[])}
            else:
                d={"name":str(item),"type":"text","required":False,"options":[]}
            if d["type"] not in ("text","number","date","choice","notes"): d["type"]="text"
            defs.append(d)
        return defs
    def _table(self, headers):
        table=QTableWidget()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        return table

    def _fill_table(self, table, rows, values, record_id_key="id", hide_id=False):
        table.setRowCount(len(rows))
        for row_index,row in enumerate(rows):
            row_values=values(row)
            for col,value in enumerate(row_values):
                item=QTableWidgetItem(str(value))
                if col == 0 and record_id_key in row.keys():
                    item.setData(Qt.UserRole,int(row[record_id_key]))
                table.setItem(row_index,col,item)
        if hide_id:
            table.setColumnHidden(0,True)

    def _selected_table_id(self, table):
        row=table.currentRow()
        if row < 0:
            return None
        item=table.item(row,0)
        return item.data(Qt.UserRole) if item else None

    def _show_legacy_records(self, module):
        key=f"Legacy records · {module}"
        old=self.pages.pop(key,None)
        if old:
            self.stack.removeWidget(old)
            old.deleteLater()
        page=self.records_page(module)
        self.pages[key]=page
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)

    def _refresh_operation_page(self, module):
        self.reload_page(module)

    def _product_rows(self, limit=None, offset=0, search=None, active_only=False):
        # products() already returns dicts with stock_on_hand from a single SQL aggregate.
        return list(self.store.operations.products(
            active_only=active_only, limit=limit, offset=offset, search=search
        ))

    def _base_stock_display(self, product):
        try:
            quantity, unit = self.store.operations.stock_in_base_units(product["id"])
            value = f"{quantity:,.3f}".rstrip("0").rstrip(".")
            return value + (f" {unit}" if unit else "")
        except (OperationError, ValueError, TypeError):
            return "—"

    def sku_labels_dialog(self, preselected=None):
        """Choose multiple SKUs and copy counts, then export scannable labels or label data."""
        permissions=self.store.role_permissions(self.role)
        can_export="export" in permissions
        can_print="print" in permissions
        if not (can_export or can_print):
            QMessageBox.information(self,"Permission required","Your role cannot export or print SKU labels.")
            return
        dlg=QDialog(self); dlg.setWindowTitle("Batch SKU labels")
        dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumSize(980,680)
        outer=QVBoxLayout(dlg)
        intro=QLabel("Select products, then set the number of labels to make for each. Each label includes a Code 128 barcode encoding the internal SKU, so scanners that type the scanned value can find the product in POS. This is not a GS1/EAN registration.")
        intro.setWordWrap(True); outer.addWidget(intro)
        searchrow=QHBoxLayout(); search=QLineEdit(); search.setPlaceholderText("Find by SKU, barcode, name, category, brand or model")
        search_button=QPushButton("Search catalogue"); search_button.setObjectName("primary")
        searchrow.addWidget(search,1); searchrow.addWidget(search_button); outer.addLayout(searchrow)
        results=QTableWidget(0,5); results.setHorizontalHeaderLabels(["ADD","SKU","PRODUCT","STOCK / UNIT","PRICE"])
        results.verticalHeader().setVisible(False); results.setSelectionBehavior(QAbstractItemView.SelectRows)
        results.setEditTriggers(QAbstractItemView.NoEditTriggers); results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); results.horizontalHeader().setStretchLastSection(True)
        outer.addWidget(QLabel("Matching products (up to 100 per search)")); outer.addWidget(results,3)
        selected_bar=QHBoxLayout(); selected_bar.addWidget(QLabel("Selected label batch")); selected_bar.addStretch()
        add_checked=QPushButton("Add checked products to batch"); selected_bar.addWidget(add_checked)
        remove_selected=QPushButton("Remove selected batch row"); selected_bar.addWidget(remove_selected); outer.addLayout(selected_bar)
        batch=QTableWidget(0,5); batch.setHorizontalHeaderLabels(["SKU","PRODUCT","STOCK UNIT","COPIES TO PRINT","PRICE"])
        batch.verticalHeader().setVisible(False); batch.setSelectionBehavior(QAbstractItemView.SelectRows)
        batch.setEditTriggers(QAbstractItemView.NoEditTriggers); batch.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); batch.horizontalHeader().setStretchLastSection(True)
        outer.addWidget(batch,2)
        summary=QLabel("0 products · 0 labels"); summary.setStyleSheet(f"color:{self.palette['muted']};font-weight:700;")
        outer.addWidget(summary)
        chosen={}
        result_rows={}

        def row_dict(row):
            return dict(row)

        def display_stock(product):
            stock=Decimal(str(product.get("stock_on_hand",0)))
            return f"{stock:,.3f}".rstrip("0").rstrip(".")+" "+self.store.operations.product_unit_description(product)

        def run_search():
            term=search.text().strip()
            try:
                if term:
                    rows=self.store.operations.search_products(term,limit=100)
                else:
                    rows=self.store.operations.conn.execute("""SELECT p.*, COALESCE((SELECT SUM(CAST(m.quantity_delta AS REAL)) FROM stock_movements m WHERE m.product_id=p.id),0) AS stock_on_hand
                        FROM products p WHERE p.active=1 ORDER BY p.name COLLATE NOCASE,p.id LIMIT 100""").fetchall()
            except (OperationError, sqlite3.Error) as exc:
                QMessageBox.warning(dlg,"Search unavailable",str(exc)); return
            mapped={int(r["id"]):row_dict(r) for r in rows}
            result_rows.clear(); result_rows.update(mapped)
            results.setRowCount(len(rows))
            for ri,p in enumerate(mapped.values()):
                chk=QCheckBox(); chk.setChecked(p["id"] in chosen); chk.setToolTip("Add this product to the selected batch")
                chk.setProperty("product_id",int(p["id"]))
                results.setCellWidget(ri,0,chk)
                vals=[p["sku"],p["name"],display_stock(p),p["selling_price"]]
                for ci,value in enumerate(vals,1):
                    item=QTableWidgetItem(str(value)); item.setData(Qt.UserRole,int(p["id"])); results.setItem(ri,ci,item)

        def update_copies(product_id,value):
            if product_id in chosen:
                chosen[product_id]["copies"]=int(value)
                total=sum(item["copies"] for item in chosen.values())
                summary.setText(f"{len(chosen)} products · {total:,} labels (maximum 5,000 per batch)")

        def refresh_batch():
            ordered=sorted(chosen.items(),key=lambda pair:(str(pair[1]["product"].get("brand","")).casefold(),pair[1]["product"]["sku"].casefold(),pair[1]["product"]["name"].casefold()))
            batch.setRowCount(len(ordered)); total=0
            for ri,(pid,item) in enumerate(ordered):
                p=item["product"]; copies=item["copies"]; total+=copies
                for ci,value in enumerate([p["sku"],p["name"],display_stock(p),"",p["selling_price"]]):
                    cell=QTableWidgetItem(str(value)); cell.setData(Qt.UserRole,pid); batch.setItem(ri,ci,cell)
                spin=QSpinBox(); spin.setRange(1,500); spin.setValue(int(copies)); spin.setToolTip("Physical labels to print for this SKU; this does not change stock")
                spin.valueChanged.connect(lambda value, product_id=pid: update_copies(product_id,value))
                batch.setCellWidget(ri,3,spin)
            summary.setText(f"{len(chosen)} products · {total:,} labels (maximum 5,000 per batch)")

        def add_checked_rows():
            mapped=result_rows
            previous=dict(chosen)
            for ri in range(results.rowCount()):
                chk=results.cellWidget(ri,0)
                if not chk or not chk.isChecked(): continue
                pid=int(chk.property("product_id")); p=mapped.get(pid)
                if not p: continue
                stock=Decimal(str(p.get("stock_on_hand",0)))
                suggested=max(1,min(500,int(math.ceil(stock))))
                if pid not in chosen:
                    chosen[pid]={"product":p,"copies":suggested}
            total=sum(item["copies"] for item in chosen.values())
            if total>5000:
                chosen.clear(); chosen.update(previous)
                QMessageBox.warning(dlg,"Batch too large","Adding those products would exceed 5,000 labels. Existing quantities were preserved; reduce other quantities or add fewer products.")
            refresh_batch()

        def remove_batch_row():
            rows=batch.selectionModel().selectedRows()
            if not rows:
                QMessageBox.information(dlg,"Select a batch row","Select a product in the lower batch table first."); return
            item=batch.item(rows[0].row(),0); pid=item.data(Qt.UserRole) if item else None
            if pid is not None: chosen.pop(int(pid),None)
            refresh_batch(); run_search()

        def collect_copies():
            mapping={pid:int(item["copies"]) for pid,item in chosen.items()}
            if not mapping:
                QMessageBox.information(dlg,"No products selected","Search the catalogue and add products to the label batch first.")
                return None
            if sum(mapping.values())>5000:
                QMessageBox.warning(dlg,"Batch too large","A single batch cannot exceed 5,000 labels.")
                return None
            return mapping

        def export_csv():
            if not can_export:
                QMessageBox.information(dlg,"Export permission required","Your role can print labels but cannot export files."); return
            mapping=collect_copies()
            if mapping is None:return
            path,_=QFileDialog.getSaveFileName(dlg,"Export SKU label data","sku-labels.csv","CSV files (*.csv)")
            if not path:return
            if not path.casefold().endswith(".csv"):path+=".csv"
            try:
                count=self.store.operations.export_sku_labels_csv(path,mapping)
                self.store.log(self.username,"Exported SKU label batch",f"{count} labels across {len(mapping)} products")
                QMessageBox.information(dlg,"Label data exported",f"Exported {count:,} rows (one row per physical label) to:\n{path}")
            except (OperationError,OSError,ValueError) as exc:
                QMessageBox.warning(dlg,"Export failed",str(exc))

        def make_pdf(open_after=False):
            mapping=collect_copies()
            if mapping is None:return
            if open_after and not can_print:
                QMessageBox.information(dlg,"Print permission required","Your role cannot print labels."); return
            if not open_after and not can_export:
                QMessageBox.information(dlg,"Export permission required","Your role can print labels but cannot export a PDF file."); return
            if open_after:
                path=str(Path(tempfile.gettempdir())/f"NextGen_ERP_SKU_labels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
            else:
                path,_=QFileDialog.getSaveFileName(dlg,"Save printable SKU label sheet","sku-labels.pdf","PDF files (*.pdf)")
                if not path:return
                if not path.casefold().endswith(".pdf"):path+=".pdf"
            try:
                result=self.store.operations.export_sku_labels_pdf(path,mapping,capability="print" if open_after else "export")
                self.store.log(self.username,"Prepared SKU label sheet",f"{result['labels']} labels; {result['pages']} pages")
                if open_after:
                    opened=QDesktopServices.openUrl(QUrl.fromLocalFile(path))
                    QMessageBox.information(dlg,"Print-ready labels",f"Created {result['labels']:,} labels on {result['pages']} A4 page(s).\nPDF: {path}\n\nUse your PDF viewer's Print command and select the matching label stock.\nOpened in the default PDF viewer: {'yes' if opened else 'no'}")
                else:
                    QMessageBox.information(dlg,"PDF exported",f"Created {result['labels']:,} labels for {result['products']} products on {result['pages']} A4 page(s).\n\nThe barcode encodes the internal SKU in Code 128; it is not a GS1/EAN registration.\n\n{path}")
            except (OperationError,OSError,ValueError,ImportError) as exc:
                QMessageBox.warning(dlg,"Label PDF failed",str(exc))

        search_button.clicked.connect(run_search); search.returnPressed.connect(run_search)
        add_checked.clicked.connect(add_checked_rows); remove_selected.clicked.connect(remove_batch_row)
        run_search()
        if preselected:
            for raw_id,raw_copies in preselected.items():
                try:
                    product=self.store.operations.get_product(int(raw_id))
                    if product:
                        chosen[int(raw_id)]={"product":product,"copies":max(1,min(500,int(raw_copies)))}
                except (OperationError,ValueError,TypeError):
                    pass
            refresh_batch(); run_search()
        buttons=QHBoxLayout()
        if can_export:
            csv_button=QPushButton("Export label CSV"); csv_button.clicked.connect(export_csv); buttons.addWidget(csv_button)
            pdf_button=QPushButton("Download printable PDF"); pdf_button.clicked.connect(lambda:make_pdf(False)); buttons.addWidget(pdf_button)
        if can_print:
            print_button=QPushButton("Open PDF to print"); print_button.setObjectName("primary"); print_button.clicked.connect(lambda:make_pdf(True)); buttons.addWidget(print_button)
        close=QPushButton("Close"); close.clicked.connect(dlg.accept); buttons.addWidget(close)
        outer.addLayout(buttons)
        dlg.exec()

    # --- Admin-configurable product form ---------------------------------
    PRODUCT_FORM_FIELD_DEFS = (
        ("sku", "SKU"),
        ("barcode", "Barcode"),
        ("name", "Product name"),
        ("description", "Description"),
        ("brand", "Brand"),
        ("model", "Model"),
        ("category", "Category"),
        ("supplier_code", "Supplier code"),
        ("unit", "Stock / selling unit"),
        ("selling_price", "Selling price"),
        ("cost_price", "Unit cost"),
        ("reorder_level", "Reorder level"),
        ("opening_stock", "Opening stock"),
        ("active", "Active"),
        ("track_batches", "Batch tracking"),
    )
    PRODUCT_FORM_ALWAYS_ON = frozenset({"name", "selling_price", "category", "brand"})

    def product_form_fields(self):
        """Return the set of product-form field keys Admin has left enabled."""
        defaults = {key for key, _ in self.PRODUCT_FORM_FIELD_DEFS}
        raw = self.store.setting("product_form_fields", "")
        if not raw:
            return defaults
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                enabled = {k for k, v in data.items() if v} | set(self.PRODUCT_FORM_ALWAYS_ON)
                return enabled.intersection(defaults) | set(self.PRODUCT_FORM_ALWAYS_ON)
            if isinstance(data, list):
                return set(data).intersection(defaults) | set(self.PRODUCT_FORM_ALWAYS_ON)
        except (TypeError, ValueError):
            pass
        return defaults

    def product_field_visible(self, key):
        return key in self.product_form_fields()


    def inventory_page(self):
        w,v=self.page_container("Stock & Catalogue","Track laptops, phones, spares, accessories and networking equipment. Sales and purchase receipts update stock through audited movements.")
        actions=QHBoxLayout()
        tools=QHBoxLayout()
        if "manage_inventory" in self.store.role_permissions(self.role):
            add=QPushButton("＋ Add product"); add.setObjectName("primary"); add.clicked.connect(self.create_product_dialog); actions.addWidget(add)
            edit=QPushButton("Edit selected"); edit.clicked.connect(self.edit_selected_product); actions.addWidget(edit)
            adjust=QPushButton("Adjust selected stock"); adjust.clicked.connect(self.adjust_selected_stock); actions.addWidget(adjust)
            receive=QPushButton("Receive stock"); receive.clicked.connect(lambda:self.open_stock_transaction("purchase")); actions.addWidget(receive)
            imported=QPushButton("Import CSV"); imported.clicked.connect(self.import_products_csv_dialog); tools.addWidget(imported)
            identifiers=QPushButton("Identifiers & history"); identifiers.clicked.connect(self.product_identifiers_dialog); tools.addWidget(identifiers)
            repair_skus=QPushButton("Repair missing SKUs"); repair_skus.clicked.connect(self.repair_missing_skus_dialog); tools.addWidget(repair_skus)
            if "export" in self.store.role_permissions(self.role):
                exported=QPushButton("Export CSV"); exported.clicked.connect(self.export_products_csv_dialog); tools.addWidget(exported)
                template=QPushButton("CSV template"); template.clicked.connect(self.export_products_template_dialog); tools.addWidget(template)
                labels=QPushButton("Batch SKU labels · PDF / CSV"); labels.setToolTip("Choose products and copies, then export a scannable Code 128 SKU label sheet or a CSV.")
                labels.clicked.connect(lambda:self.sku_labels_dialog()); tools.addWidget(labels)
            elif "print" in self.store.role_permissions(self.role):
                labels=QPushButton("Batch SKU labels · print"); labels.clicked.connect(lambda:self.sku_labels_dialog()); tools.addWidget(labels)
            if "configure" in self.store.role_permissions(self.role):
                rules=QPushButton("SKU rules"); rules.clicked.connect(self.sku_numbering_dialog); tools.addWidget(rules)
        website_catalog=QPushButton("Website catalogue"); website_catalog.setToolTip("Browse the read-only product catalogue mirrored from the shop website."); website_catalog.clicked.connect(self.open_website_catalog); actions.addWidget(website_catalog)
        legacy=QPushButton("View legacy inventory records"); legacy.clicked.connect(lambda:self._show_legacy_records("Inventory")); actions.addWidget(legacy)
        actions.addStretch(); v.addLayout(actions)
        if tools.count():
            tools.addStretch(); v.addLayout(tools)

        # Search + paging — load only one page of the catalogue at a time.
        page_size = 75
        state = {"page": 0, "search": "", "total": 0}

        filter_row = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Search SKU, name, brand, category, model or barcode…")
        search.setClearButtonEnabled(True)
        filter_row.addWidget(search, 1)
        find_btn = QPushButton("Find")
        find_btn.setObjectName("primary")
        filter_row.addWidget(find_btn)
        show_all = QCheckBox("Include inactive")
        filter_row.addWidget(show_all)
        v.addLayout(filter_row)

        table = self._table(["ID", "SKU", "PRODUCT", "BRAND", "CATEGORY", "ON HAND", "SELL PRICE", "COST", "REORDER", "ACTIVE"])
        page_bar = QHBoxLayout()
        prev_btn = QPushButton("← Previous")
        next_btn = QPushButton("Next →")
        page_label = QLabel("—")
        page_label.setStyleSheet(f"color:{self.palette['muted']};font-weight:700;")
        page_bar.addWidget(prev_btn)
        page_bar.addWidget(page_label, 1)
        page_bar.addWidget(next_btn)

        def load_page():
            active_only = not show_all.isChecked()
            term = state["search"]
            try:
                state["total"] = self.store.operations.product_count(active_only=active_only, search=term or None)
            except Exception:
                state["total"] = 0
            total_pages = max(1, (state["total"] + page_size - 1) // page_size) if state["total"] else 1
            if state["page"] >= total_pages:
                state["page"] = max(0, total_pages - 1)
            offset = state["page"] * page_size
            try:
                rows = self._product_rows(limit=page_size, offset=offset, search=term or None, active_only=active_only)
            except Exception as exc:
                QMessageBox.warning(self, "Catalogue unavailable", str(exc))
                rows = []
            self._fill_table(
                table, rows,
                lambda r: [
                    r["id"], r["sku"],
                    r["name"] + (f" · {r['model']}" if r.get("model") else ""),
                    r["brand"], r["category"],
                    f"{float(r['stock_on_hand']):g}",
                    r["selling_price"], r["cost_price"], r["reorder_level"],
                    "Yes" if r["active"] else "No",
                ],
                hide_id=True,
            )
            start = offset + 1 if state["total"] else 0
            end = min(offset + len(rows), state["total"])
            page_label.setText(
                f"Showing {start:,}–{end:,} of {state['total']:,} products · page {state['page'] + 1} of {total_pages}"
            )
            prev_btn.setEnabled(state["page"] > 0)
            next_btn.setEnabled(state["page"] + 1 < total_pages)

        def run_search():
            state["search"] = search.text().strip()
            state["page"] = 0
            load_page()

        def go_prev():
            if state["page"] > 0:
                state["page"] -= 1
                load_page()

        def go_next():
            state["page"] += 1
            load_page()

        find_btn.clicked.connect(run_search)
        search.returnPressed.connect(run_search)
        show_all.stateChanged.connect(lambda *_: run_search())
        prev_btn.clicked.connect(go_prev)
        next_btn.clicked.connect(go_next)
        load_page()

        v.addWidget(QLabel("Products and current stock"))
        v.addWidget(table, 1)
        v.addLayout(page_bar)

        moves = self.store.operations.stock_movements(100)
        movement_table = self._table(["DATE", "SKU", "PRODUCT", "CHANGE", "TYPE", "REFERENCE", "NOTE"])
        self._fill_table(
            movement_table, moves,
            lambda r: [
                r["created_at"], r["sku"], r["name"], r["quantity_delta"], r["movement_type"],
                f"{r['reference_type']} {r['reference_id'] or ''}".strip(), r["note"],
            ],
        )
        v.addWidget(QLabel("Recent stock movements"))
        v.addWidget(movement_table, 1)
        return w

    def _product_dialog(self, existing=None):
        """Add or edit a product. Add mode always starts blank; Admin can hide optional fields."""
        editing = existing is not None
        visible = self.product_form_fields()
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit product" if editing else "Add product")
        dlg.setStyleSheet(themed_style(self.palette))
        dlg.setMinimumWidth(560)
        layout = QVBoxLayout(dlg)
        form = QFormLayout()
        profile_name = self.store.setting("business_type", "Computers and Electronics Sales and Repairs")

        sku = QLineEdit(str(existing["sku"]) if editing else "")
        barcode = QLineEdit(str(existing["barcode"]) if editing else "")
        if profile_name == "Computers and Electronics Sales and Repairs":
            category = QComboBox(); category.setEditable(True)
            category.addItems(["Laptops", "Desktop computers", "Mobile phones", "Tablets", "Computer accessories", "Phone accessories", "Laptop spares", "Computer spares", "Networking equipment", "Routers & switches", "Cables & connectors", "Storage & memory", "Power & chargers", "Printers & peripherals", "Gaming & PlayStation", "Other"])
            category.setCurrentText(str(existing["category"]) if editing else "")
        else:
            category = QLineEdit(str(existing["category"]) if editing else "")
        name = QLineEdit(str(existing["name"]) if editing else "")
        description = QLineEdit(str(existing["description"]) if editing else "")
        brand = QLineEdit(str(existing["brand"]) if editing else "")
        model = QLineEdit(str(existing["model"]) if editing else "")
        supplier_code = QLineEdit(str(existing["supplier_code"]) if editing else "")
        unit = QLineEdit(str(existing["unit"]) if editing else "each")
        currency = self.store.setting("business_currency", "KES - Kenyan Shilling").split(" - ", 1)[0]
        selling = QDoubleSpinBox(); selling.setDecimals(2); selling.setMaximum(999999999999.99); selling.setPrefix(currency + " ")
        selling.setValue(float(existing["selling_price"]) if editing else 0)
        cost = QDoubleSpinBox(); cost.setDecimals(2); cost.setMaximum(999999999999.99); cost.setPrefix(currency + " ")
        cost.setValue(float(existing["cost_price"]) if editing else 0)
        reorder = QDoubleSpinBox(); reorder.setDecimals(3); reorder.setMaximum(999999999.999)
        reorder.setValue(float(existing["reorder_level"]) if editing else 0)
        active = QCheckBox("Active product (available to sell)"); active.setChecked(bool(existing["active"]) if editing else True)
        track_batches = QCheckBox("Track batches / lots / expiry on this product")
        track_batches.setChecked(bool(existing["track_batches"]) if editing else False)
        opening_batch_number = QLineEdit()
        opening_expiry = QLineEdit(); opening_expiry.setPlaceholderText("YYYY-MM-DD")
        batch_fields = QWidget(); bf = QFormLayout(batch_fields); bf.setContentsMargins(0, 0, 0, 0)
        bf.addRow("Opening batch number", opening_batch_number); bf.addRow("Opening expiry", opening_expiry)
        batch_fields.setVisible(False)
        track_batches.toggled.connect(batch_fields.setVisible)
        opening = QDoubleSpinBox(); opening.setDecimals(3); opening.setMaximum(999999999.999)
        if editing:
            opening.setValue(float(existing["stock_on_hand"])); opening.setReadOnly(True)

        sku.setPlaceholderText("Leave blank to auto-generate from brand + category")
        barcode.setPlaceholderText("Optional scan code")
        name.setPlaceholderText("Product name as staff should see it")
        brand.setPlaceholderText("e.g. Dell, HP, Samsung")
        model.setPlaceholderText("Optional model number")
        supplier_code.setPlaceholderText("Optional supplier part number")
        description.setPlaceholderText("Optional notes for staff")
        if isinstance(category, QComboBox) and category.lineEdit():
            category.lineEdit().setPlaceholderText("Choose or type a category")

        sku_hint = QLabel("")
        sku_hint.setStyleSheet(f"color:{self.palette['muted']};font-size:9pt;")

        def update_sku_hint(*_):
            if editing:
                sku_hint.setText("SKU identifies this product. Changes are logged in identifier history.")
            elif sku.text().strip():
                sku_hint.setText("Manual SKU will be used if unique.")
            else:
                try:
                    cat_val = category.currentText() if isinstance(category, QComboBox) else category.text()
                    suggestion = self.store.operations.suggest_sku(cat_val, brand=brand.text())
                    sku_hint.setText(f"Leave blank to auto-generate: {suggestion}")
                except Exception:
                    sku_hint.setText("Leave blank to auto-generate a brand-matched SKU.")

        sku.textChanged.connect(update_sku_hint)
        brand.textChanged.connect(update_sku_hint)
        if isinstance(category, QComboBox):
            category.editTextChanged.connect(update_sku_hint)
        else:
            category.textChanged.connect(update_sku_hint)

        def add_row(key, label, widget):
            if key in self.PRODUCT_FORM_ALWAYS_ON or key in visible:
                form.addRow(label, widget)
                return True
            widget.hide()
            return False

        add_row("sku", "SKU", sku)
        form.addRow("", sku_hint)
        add_row("barcode", "Barcode", barcode)
        add_row("name", "Product name", name)
        add_row("description", "Description", description)
        add_row("brand", "Brand", brand)
        add_row("model", "Model", model)
        add_row("category", "Category", category)
        add_row("supplier_code", "Supplier code", supplier_code)
        add_row("unit", "Stock / selling unit", unit)
        add_row("selling_price", "Selling price", selling)
        add_row("cost_price", "Unit cost", cost)
        add_row("reorder_level", "Reorder level", reorder)
        add_row("active", "", active)
        if self.product_field_visible("track_batches"):
            form.addRow("", track_batches)
            form.addRow("Batch settings", batch_fields)
        else:
            track_batches.setChecked(False)
        if editing:
            form.addRow("On hand (read only)", opening)
        elif self.product_field_visible("opening_stock"):
            form.addRow("Opening stock", opening)

        form_host = QWidget(); form_host.setLayout(form)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(form_host); scroll.setMinimumHeight(280)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox()
        save_btn = buttons.addButton("Save", QDialogButtonBox.AcceptRole)
        save_btn.setObjectName("primary")
        add_another_btn = None
        if not editing:
            add_another_btn = buttons.addButton("Save and add another", QDialogButtonBox.ActionRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)
        update_sku_hint()

        created = {"id": None}
        keep_open = {"flag": False}

        def clear_form():
            """Reset every widget so the next Add starts empty — no manual wiping."""
            sku.clear(); barcode.clear(); name.clear(); description.clear()
            brand.clear(); model.clear(); supplier_code.clear()
            if isinstance(category, QComboBox):
                category.setCurrentIndex(-1)
                category.setEditText("")
            else:
                category.clear()
            unit.setText("each")
            selling.setValue(0); cost.setValue(0); reorder.setValue(0); opening.setValue(0)
            active.setChecked(True); track_batches.setChecked(False)
            opening_batch_number.clear(); opening_expiry.clear(); batch_fields.setVisible(False)
            created["id"] = None
            update_sku_hint()
            name.setFocus()

        def save(add_another=False):
            try:
                category_value = (category.currentText() if isinstance(category, QComboBox) else category.text()).strip()
                if not name.text().strip():
                    raise ValueError("Product name is required.")
                if editing:
                    self.store.operations.update_product(
                        existing["id"], sku.text(), name.text(), selling.value(), cost.value(),
                        reorder.value(), unit.text(), active.isChecked(), category_value, barcode.text(),
                        description.text(), brand.text(), model.text(), supplier_code.text(),
                        "", "", base_unit="", units_per_stock_unit=1,
                        track_batches=track_batches.isChecked(),
                        opening_batch_number=opening_batch_number.text(), opening_expiry_date=opening_expiry.text())
                    saved_product = self.store.operations.get_product(existing["id"])
                    self.store.log(self.username, "Updated product", f"{saved_product['sku']} · {name.text().strip()}")
                else:
                    product_id = self.store.operations.create_product(
                        sku.text(), name.text(), selling.value(), cost.value(), opening.value(),
                        unit.text(), reorder.value(), category_value, barcode.text(), description.text(),
                        brand.text(), model.text(), supplier_code.text(), "", "",
                        "", 1, track_batches.isChecked(), opening_batch_number.text(), opening_expiry.text())
                    created["id"] = int(product_id)
                    saved_product = self.store.operations.get_product(product_id)
                    self.store.log(self.username, "Created product", f"{saved_product['sku']} · {name.text().strip()} · id {product_id}")
                    if not add_another:
                        msg = (f"{name.text().strip()} was saved.\nSKU: {saved_product['sku']}\n"
                               f"Opening stock: {opening.value():g} {unit.text().strip() or 'each'}")
                        QMessageBox.information(dlg, "Product saved", msg)
            except (OperationError, ValueError) as exc:
                QMessageBox.warning(dlg, "Product not saved", str(exc))
                return
            if add_another and not editing:
                keep_open["flag"] = True
                clear_form()
                self._refresh_operation_page("Inventory")
            else:
                dlg.accept()

        save_btn.clicked.connect(lambda: save(False))
        if add_another_btn is not None:
            add_another_btn.clicked.connect(lambda: save(True))

        if dlg.exec():
            self._refresh_operation_page("Inventory")
            product_id = created["id"]
            if (not editing and product_id and self.store.operations.stock_on_hand(product_id) > 0
                    and ({"export", "print"} & self.store.role_permissions(self.role))):
                answer = QMessageBox.question(
                    self, "Print SKU labels now?",
                    f"Opening stock was recorded. Create SKU labels now?\nDefault copies: {min(500, max(1, int(math.ceil(opening.value()))))}",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if answer == QMessageBox.Yes:
                    self.sku_labels_dialog({int(product_id): min(500, max(1, int(math.ceil(opening.value()))))})
        elif keep_open["flag"]:
            self._refresh_operation_page("Inventory")

    def product_identifiers_dialog(self):
        if not self._authorize("view"): return
        table=self.pages["Inventory"].findChild(QTableWidget)
        product_id=self._selected_table_id(table) if table else None
        if product_id is None:
            QMessageBox.information(self,"Select a product","Select a product row first."); return
        product=self.store.operations.get_product(product_id)
        if not product: return
        dlg=QDialog(self); dlg.setWindowTitle(f"Identifiers · {product['name']}"); dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumWidth(540)
        v=QVBoxLayout(dlg)
        current=QPlainTextEdit(); current.setReadOnly(True); current.setMinimumHeight(150)
        v.addWidget(QLabel(f"Product: {product['name']}\nCurrent SKU: {product['sku']}\nPrimary barcode: {product['barcode'] or 'Not set'}"))
        v.addWidget(QLabel("Known barcodes and SKU change history")); v.addWidget(current)
        def refresh():
            lines=[]
            for alias in self.store.operations.product_barcodes(product_id):
                label=alias['label'] or "Barcode"
                lines.append(f"BARCODE  {alias['barcode']}    {label}    added {alias['created_at']}")
            history=self.store.operations.conn.execute("SELECT old_sku,new_sku,changed_by,reason,changed_at FROM product_sku_history WHERE product_id=? ORDER BY id DESC",(product_id,)).fetchall()
            for item in history:
                lines.append(f"SKU  {item['old_sku']} → {item['new_sku']}    by {item['changed_by'] or 'unknown'}    {item['reason']}    {item['changed_at']}")
            current.setPlainText("\n".join(lines) if lines else "No additional identifiers or SKU changes have been recorded.")
        refresh()
        actions=QHBoxLayout()
        add=None
        if "manage_inventory" in self.store.role_permissions(self.role):
            add=QPushButton("Add barcode alias"); add.setObjectName("primary"); actions.addWidget(add)
        close=QPushButton("Close"); close.clicked.connect(dlg.reject); actions.addStretch(); actions.addWidget(close); v.addLayout(actions)
        def add_barcode():
            code,ok=QInputDialog.getText(dlg,"Add barcode","Enter a barcode / scan identifier:")
            if not ok: return
            label,ok=QInputDialog.getText(dlg,"Barcode label","Optional label (e.g. inner pack, carton):")
            if not ok: return
            try:
                self.store.operations.add_product_barcode(product_id,code,label)
                self.store.log(self.username,"Added product barcode",f"{product['sku']} · {code.strip()}")
            except (OperationError,ValueError) as exc:
                QMessageBox.warning(dlg,"Barcode not saved",str(exc)); return
            refresh()
        if add is not None:
            add.clicked.connect(add_barcode)
        dlg.exec()

    def sku_numbering_dialog(self):
        if not self._authorize("configure"): return
        settings=self.store.operations.sku_numbering_settings()
        dlg=QDialog(self); dlg.setWindowTitle("SKU numbering rules"); dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumWidth(520)
        v=QVBoxLayout(dlg); form=QFormLayout()
        prefix=QLineEdit(settings["default_prefix"])
        padding=QSpinBox(); padding.setRange(2,10); padding.setValue(settings["padding"])
        rules=QPlainTextEdit(); rules.setPlainText(json.dumps(settings["category_prefixes"],indent=2,ensure_ascii=False)); rules.setPlaceholderText('{"Electronics":"ELEC", "Food":"FOOD"}')
        form.addRow("Default prefix",prefix); form.addRow("Sequence padding",padding); form.addRow("Category prefix rules (JSON)",rules)
        hint=QLabel("New product SKUs use BRAND-CATEGORY-000001 style (e.g. DELL-LAP-0001). Brand is matched first so all Dell codes group together. Category rules remain optional."); hint.setWordWrap(True); form.addRow("",hint)
        v.addLayout(form); buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); v.addWidget(buttons); buttons.rejected.connect(dlg.reject)
        def save():
            try:
                parsed=json.loads(rules.toPlainText() or "{}")
                self.store.operations.configure_sku_numbering(prefix.text(),padding.value(),parsed)
            except (ValueError,OperationError) as exc:
                QMessageBox.warning(dlg,"SKU rules not saved",str(exc)); return
            dlg.accept()
        buttons.accepted.connect(save)
        if dlg.exec(): QMessageBox.information(self,"SKU rules saved","New products will use the updated SKU numbering rules.")

    def repair_missing_skus_dialog(self):
        if not self._authorize("manage_inventory"): return
        try:
            preview=self.store.operations.generate_missing_skus(dry_run=True)
        except OperationError as exc:
            QMessageBox.warning(self,"SKU review failed",str(exc)); return
        if not preview["count"]:
            QMessageBox.information(self,"SKU check","No products with missing SKUs were found."); return
        lines="\n".join(f"#{row['id']} {row['name']}: {row['old_sku']!r} → {row['suggested_sku']}" for row in preview["rows"][:15])
        message=f"{preview['count']} product(s) need an SKU. Product IDs and stock will remain unchanged.\n\nPreview:\n{lines}"
        if preview["count"]>15: message+=f"\n…and {preview['count']-15} more."
        if QMessageBox.question(self,"Repair missing SKUs",message+"\n\nApply these generated identifiers?",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
        try:
            result=self.store.operations.generate_missing_skus(dry_run=False)
        except OperationError as exc:
            QMessageBox.warning(self,"SKU repair failed",str(exc)); return
        QMessageBox.information(self,"SKU repair complete",f"Assigned SKUs to {result['count']} products. Changes were recorded in SKU history.")
        self._refresh_operation_page("Inventory")

    def export_products_template_dialog(self):
        if not self._authorize("export"): return
        path,_=QFileDialog.getSaveFileName(self,"Save product import template","products_template.csv","CSV files (*.csv)")
        if not path: return
        try:
            fields=self.store.operations.export_products_template_csv(path)
            QMessageBox.information(self,"Template saved",f"Created a CSV template with {len(fields)} columns. Leave SKU blank to request automatic generation.\n{path}")
        except OperationError as exc:
            QMessageBox.warning(self,"Template export failed",str(exc))

    def export_products_csv_dialog(self):
        if not self._authorize("export"): return
        path,_=QFileDialog.getSaveFileName(self,"Export product catalogue","products.csv","CSV files (*.csv)")
        if not path: return
        try:
            count=self.store.operations.export_products_csv(path)
            QMessageBox.information(self,"Catalogue exported",f"Exported {count} products to:\n{path}")
        except OperationError as exc:
            QMessageBox.warning(self,"Export failed",str(exc))

    def import_products_csv_dialog(self):
        if not self._authorize("manage_inventory"): return
        path,_=QFileDialog.getOpenFileName(self,"Import product catalogue","","CSV files (*.csv)")
        if not path: return
        answer=QMessageBox.question(self,"Import mode","Update existing products when their SKU matches?\nChoose No to skip existing SKUs and only import new products.",QMessageBox.Yes|QMessageBox.No|QMessageBox.Cancel,QMessageBox.No)
        if answer==QMessageBox.Cancel: return
        update_existing=(answer==QMessageBox.Yes)
        try:
            preview=self.store.operations.import_products_csv(path,dry_run=True,update_existing=update_existing)
        except OperationError as exc:
            QMessageBox.warning(self,"Import validation failed",str(exc)); return
        errors=[item for item in preview["rows"] if item["status"]=="error"]
        details="\n".join(f"Row {item['line']}: {item['message']}" for item in errors[:8])
        prompt=(f"Validation preview:\nNew products ready: {preview['ready']}\nExisting products to update: {preview['update']}\nExisting products to skip: {preview['skip']}\nRows with errors: {preview['error']}\n\n"
                "Stock balances will not be overwritten. Valid rows are imported in recoverable per-product operations.")
        if details: prompt += "\n\nFirst errors:\n"+details
        prompt += "\n\nContinue with valid rows?"
        if QMessageBox.question(self,"Confirm catalogue import",prompt,QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
        result=self.store.operations.import_products_csv(path,dry_run=False,update_existing=update_existing)
        QMessageBox.information(self,"Import complete",f"Created: {result['created']}\nUpdated: {result['updated']}\nSkipped: {result['skipped']}\nFailed: {result['failed']}")
        self._refresh_operation_page("Inventory")

    def create_product_dialog(self):
        if not self._authorize("manage_inventory"): return
        self._product_dialog()

    def edit_selected_product(self):
        if not self._authorize("manage_inventory"): return
        table=self.pages["Inventory"].findChild(QTableWidget)
        product_id=self._selected_table_id(table) if table else None
        if product_id is None:
            QMessageBox.information(self,"Select a product","Select a product row first."); return
        product=self.store.operations.get_product(product_id)
        if product: self._product_dialog(product)
        else: QMessageBox.information(self,"Product not found","That product is no longer in the catalogue.")

    def adjust_selected_stock(self):
        if not self._authorize("manage_inventory"): return
        table=self.pages["Inventory"].findChild(QTableWidget)
        product_id=self._selected_table_id(table) if table else None
        if product_id is None:
            QMessageBox.information(self,"Select a product","Select a product row first."); return
        product=self.store.operations.get_product(product_id)
        if not product:
            QMessageBox.information(self,"Product not found","That product is no longer in the catalogue.")
            return
        quantity,ok=QInputDialog.getDouble(self,"Adjust stock",f"Signed quantity change for {product['sku']}:",0,-999999999,999999999,3)
        if not ok: return
        reason,ok=QInputDialog.getText(self,"Adjustment reason","Reason for the stock adjustment:")
        if not ok: return
        try:
            self.store.operations.adjust_stock(product_id,quantity,reason)
            self.store.log(self.username,"Adjusted stock",f"{product['sku']}: {quantity:+.3f} · {reason.strip()}")
            self._refresh_operation_page("Inventory")
        except OperationError as exc:
            QMessageBox.warning(self,"Stock adjustment rejected",str(exc))


    def returns_page(self):
        w,v=self.page_container("Returns Desk","Refunds · restock · customer care")
        currency = self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        page = ReturnsPage(self.store.operations, currency=currency, parent=w)
        v.addWidget(page, 1)
        return w
    def pos_page(self):
        if self.store.setting("pos_enabled", "0") != "1":
            w,v=self.page_container("Point of Sale","Optional checkout workspace")
            panel=self.panel(); layout=QVBoxLayout(panel); layout.setContentsMargins(24,24,24,24)
            layout.addWidget(QLabel("Point of Sale is currently turned off for this business."))
            enable=QPushButton("Enable Point of Sale in Business Settings"); enable.clicked.connect(lambda:self.show_page("System Settings")); layout.addWidget(enable)
            v.addWidget(panel); v.addStretch(); return w
        w,v=self.page_container("Point of Sale","Cashier terminal · barcode-ready · stock-backed · offline-first")
        currency = self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        tax_label = self.store.setting("business_tax_label", "Tax") or "Tax"
        try:
            tax_rate = Decimal(self.store.setting("business_tax_rate", "0") or "0")
        except Exception:
            tax_rate = Decimal("0")
        def load_held():
            try: return json.loads(self.store.setting("pos_held_carts","[]") or "[]")
            except (TypeError,ValueError): return []
        pay_cfg = self.store.setting("payment_providers_json", "")
        def pin_ok(entered):
            from payment_providers import load_providers_config
            cfg = load_providers_config(self.store.setting("payment_providers_json", ""))
            expected = str(cfg.get("manager_pin") or "")
            return bool(expected) and entered == expected
        from pos_config import load_pos_config
        pos_ui_cfg = load_pos_config(self.store.setting("pos_config_json", ""))
        # Resolve appearance against current app theme when set to match_app
        app_theme = self.store.setting("theme", "light")
        pos = POSPage(
            pre_sale_check=self.shift_problem,
            held_loader=load_held,
            held_saver=lambda items:self.store.set_setting("pos_held_carts",json.dumps(items)),
            tax_inclusive=self.store.setting("business_tax_inclusive", "0") == "1",
            ops=self.store.operations,
            currency=currency,
            tax_label=tax_label,
            tax_rate=tax_rate,
            allow_negative_stock=False,
            parent=w,
            payment_config=pay_cfg or None,
            manager_pin_checker=pin_ok,
            pos_config=pos_ui_cfg,
            app_theme=app_theme,
        )
        # Snapshot the logged-in operator for "served by" on invoices/receipts
        try:
            sname, scode = self.store.user_served_snapshot(self.username)
            pos.served_by_name = sname
            pos.served_by_code = scode
        except Exception:
            pos.served_by_name = getattr(self, "username", "Staff") or "Staff"
            pos.served_by_code = ""
        def on_sale_done(sale_id):
            # Invalidate cached Sales page so the next visit shows the new sale
            self.pages.pop("Sales", None)
            self.pages.pop("Returns Desk", None)
            try:
                from advanced_features import load_flags, load_loyalty, points_earn, try_kick_cash_drawer
                flags = load_flags(self.store.setting("feature_flags_json", ""))
                if flags.get("loyalty"):
                    sale = self.store.operations.conn.execute("SELECT customer_name,total FROM sales WHERE id=?", (int(sale_id),)).fetchone()
                    if sale:
                        cfg = load_loyalty(self.store.setting("loyalty_config_json", ""))
                        pts = points_earn(sale["total"], cfg)
                        if pts:
                            self.store.operations.loyalty_earn(sale["customer_name"], pts, reason="POS sale", source_type="sale", source_id=int(sale_id))
                if flags.get("cash_drawer"):
                    ps = load_print_settings(self.store)
                    try_kick_cash_drawer(getattr(ps, "printer", "") or "")
                # eTIMS / fiscal control unit
                from etims_agent import load_etims_config, document_from_sale_row, sign_document, persist_fiscal_result
                etims_cfg = load_etims_config(self.store.setting("etims_config_json", ""))
                if etims_cfg.get("enabled") and etims_cfg.get("auto_sign_on_sale"):
                    sale = self.store.operations.conn.execute("SELECT * FROM sales WHERE id=?", (int(sale_id),)).fetchone()
                    lines = self.store.operations.conn.execute("SELECT * FROM sale_lines WHERE sale_id=?", (int(sale_id),)).fetchall()
                    if sale:
                        from hardware_profiles import load_hardware_profile
                        hw = load_hardware_profile(self.store.setting("hardware_profile_json", ""))
                        doc = document_from_sale_row(dict(sale), [dict(l) for l in lines], etims_cfg, till_id=hw.get("till_id",""))
                        result = sign_document(doc, etims_cfg)
                        persist_fiscal_result(self.store.operations.conn, int(sale_id), result)
            except Exception:
                pass
            if load_print_settings(self.store).auto_receipt:
                QTimer.singleShot(0, lambda: self.print_sale_receipt_silent(sale_id))
            QTimer.singleShot(900, self.celebrate)
        pos.sale_completed.connect(on_sale_done)
        bar=QHBoxLayout()
        ps=load_print_settings(self.store)
        receipt_toggle=QCheckBox("Receipt")
        receipt_toggle.setObjectName("posReceiptAutoPrint")
        receipt_toggle.setToolTip("Automatically print a receipt after every completed POS sale")
        receipt_toggle.setChecked(ps.auto_receipt)
        receipt_toggle.toggled.connect(self.set_pos_receipt_printing)
        status=QLabel(f"{ps.describe_paper()} · {('PDF file' if ps.printer=='__pdf__' else ps.printer or 'default printer')}")
        status.setStyleSheet(f"color:{self.palette['muted']};")
        shift=self.shift_service().current()
        shift_label=QLabel((f"🟢 Shift open since {shift['opened_at'][11:16]} · float {self.currency_code()} {Decimal(shift['opening_float']):,.2f}") if shift else "⚪ No shift open")
        shift_label.setStyleSheet(f"color:{self.palette['green'] if shift else self.palette['muted']};font-weight:700;")
        shift_btn=QPushButton("Close shift / Z-report" if shift else "Open shift"); shift_btn.clicked.connect(self.shift_action)
        bar.addWidget(shift_label); bar.addWidget(shift_btn)
        setup=QPushButton("🖨  Printer setup")
        def open_setup():
            self.open_printer_setup(); self.pages.pop("Point of Sale",None); self.show_page("Point of Sale")
        setup.clicked.connect(open_setup)
        bar.addWidget(receipt_toggle); bar.addWidget(status); bar.addStretch(); bar.addWidget(setup)
        v.addLayout(bar)
        v.addWidget(pos, 1)
        return w

    def sales_page(self):
        w,v=self.page_container("Sales & Receipts","Complete stock-backed sales, record payments and returns, and keep a searchable transaction history.")
        row=QHBoxLayout()
        if "create" in self.store.role_permissions(self.role):
            create=QPushButton("＋ New sale"); create.setObjectName("primary"); create.clicked.connect(lambda:self.open_stock_transaction("sale")); row.addWidget(create)
        if "delete" in self.store.role_permissions(self.role):
            cancel=QPushButton("Cancel selected sale"); cancel.clicked.connect(self.cancel_selected_sale); row.addWidget(cancel)
        if "process_returns" in self.store.role_permissions(self.role):
            return_button=QPushButton("Return selected items"); return_button.clicked.connect(self.return_selected_sale); row.addWidget(return_button)
        if "create" in self.store.role_permissions(self.role):
            quote_button=QPushButton("＋ Create quotation"); quote_button.clicked.connect(self.create_quote); row.addWidget(quote_button)
            convert_button=QPushButton("Convert accepted quote"); convert_button.clicked.connect(self.convert_quote_selected); row.addWidget(convert_button)
        if "print" in self.store.role_permissions(self.role):
            print_invoice=QPushButton("Print selected invoice / receipt"); print_invoice.clicked.connect(lambda:self.print_selected_business_document("invoice")); row.addWidget(print_invoice)
            print_quote=QPushButton("Print selected quotation"); print_quote.clicked.connect(lambda:self.print_selected_business_document("quote")); row.addWidget(print_quote)
        if "edit" in self.store.role_permissions(self.role):
            send_quote=QPushButton("Send selected quote"); send_quote.clicked.connect(lambda:self.change_selected_quote_status("Sent")); row.addWidget(send_quote)
            accept_quote=QPushButton("Accept selected quote"); accept_quote.clicked.connect(lambda:self.change_selected_quote_status("Accepted")); row.addWidget(accept_quote)
        if "delete" in self.store.role_permissions(self.role):
            cancel_quote=QPushButton("Cancel selected quote"); cancel_quote.clicked.connect(self.cancel_selected_quote); row.addWidget(cancel_quote)
        legacy=QPushButton("View legacy sales records"); legacy.clicked.connect(lambda:self._show_legacy_records("Sales")); row.addWidget(legacy); row.addStretch(); v.addLayout(row)
        table=self._table(["ID","SALE NUMBER","CUSTOMER","STATUS","PAYMENT","TOTAL","PAID","INVOICE DATE","RECORDED"])
        rows=self.store.operations.sales()
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        def _sale_row(r):
            inv = str(r["invoice_date"] if "invoice_date" in r.keys() else "") or ""
            inv = inv.strip() or "—"
            return [r["id"], r["sale_number"], r["customer_name"], r["status"], r["payment_status"],
                    f"{currency} {r['total']}", f"{currency} {r['paid_total']}", inv, r["created_at"]]
        self._fill_table(table, rows, _sale_row, hide_id=True)
        self.sales_table=table
        v.addWidget(table,1)
        v.addWidget(QLabel("Returns and refunds"))
        return_table=self._table(["RETURN","SALE","CUSTOMER","REFUND","REASON","CREATED","PROCESSED BY"])
        return_rows=self.store.operations.sales_returns()
        self._fill_table(return_table,return_rows,lambda r:[r["return_number"],r["sale_number"],r["customer_name"],f"{currency} {r['refund_amount']}",r["reason"],r["created_at"],r["created_by"]])
        v.addWidget(return_table,1)
        v.addWidget(QLabel("Quotations"))
        quote_table=self._table(["ID","QUOTE NUMBER","CUSTOMER","STATUS","TOTAL","EXPIRES","CREATED"])
        quote_rows=self.store.operations.quotes()
        self._fill_table(quote_table,quote_rows,lambda r:[r["id"],r["quote_number"],r["customer_name"],r["status"],f"{currency} {r['total']}",r["expires_on"] or "—",r["created_at"]],hide_id=True)
        self.quote_table=quote_table
        v.addWidget(quote_table,1)
        return w

    def purchases_page(self):
        w,v=self.page_container("Purchases & Suppliers","Track suppliers and purchase orders, receive delivered quantities, and connect inventory costs with payments.")
        row=QHBoxLayout()
        permissions=self.store.role_permissions(self.role)
        if "manage_inventory" in permissions:
            order_button=QPushButton("＋ Create purchase order"); order_button.setObjectName("primary"); order_button.clicked.connect(self.create_purchase_order); row.addWidget(order_button)
            receive_order=QPushButton("Receive selected order"); receive_order.clicked.connect(self.receive_selected_purchase_order); row.addWidget(receive_order)
            receive=QPushButton("＋ Receive unplanned purchase"); receive.clicked.connect(lambda:self.open_stock_transaction("purchase")); row.addWidget(receive)
            supplier=QPushButton("＋ Add supplier"); supplier.clicked.connect(lambda:self.create_party_dialog("supplier")); row.addWidget(supplier)
        if "delete" in permissions:
            cancel=QPushButton("Cancel selected purchase"); cancel.clicked.connect(self.cancel_selected_purchase); row.addWidget(cancel)
            cancel_order=QPushButton("Cancel selected order"); cancel_order.clicked.connect(self.cancel_selected_purchase_order); row.addWidget(cancel_order)
        if "print" in permissions:
            print_order=QPushButton("Print selected purchase order"); print_order.clicked.connect(lambda:self.print_selected_business_document("purchase_order")); row.addWidget(print_order)
            print_receipt=QPushButton("Print selected goods receipt"); print_receipt.clicked.connect(lambda:self.print_selected_business_document("goods_received")); row.addWidget(print_receipt)
        legacy=QPushButton("View legacy purchase records"); legacy.clicked.connect(lambda:self._show_legacy_records("Purchases")); row.addWidget(legacy); row.addStretch(); v.addLayout(row)
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        v.addWidget(QLabel("Purchase receipts"))
        receipt_table=self._table(["ID","PURCHASE NUMBER","SUPPLIER","STATUS","PAYMENT","TOTAL","PAID","CREATED"])
        rows=self.store.operations.purchases()
        self._fill_table(receipt_table,rows,lambda r:[r["id"],r["purchase_number"],r["supplier_name"],r["status"],r["payment_status"],f"{currency} {r['total']}",f"{currency} {r['paid_total']}",r["created_at"]],hide_id=True)
        self.purchase_receipts_table=receipt_table
        v.addWidget(receipt_table,1)
        v.addWidget(QLabel("Purchase orders"))
        order_table=self._table(["ID","ORDER NUMBER","SUPPLIER","STATUS","CREATED","UPDATED"])
        self._fill_table(order_table,self.store.operations.purchase_orders(),lambda r:[r["id"],r["order_number"],r["supplier_name"],r["status"],r["created_at"],r["updated_at"]],hide_id=True)
        self.purchase_orders_table=order_table
        v.addWidget(order_table,1)
        return w

    def repair_page(self):
        w,v=self.page_container("Service & Repairs","Manage phone, laptop, desktop, PlayStation and electronics intake; hardware/software servicing, estimates, parts, payments, collection dates and customer service history.")
        permissions=self.store.role_permissions(self.role)
        bar=QHBoxLayout()
        if "create" in permissions:
            new=QPushButton("＋ Receive device"); new.setObjectName("primary"); new.clicked.connect(self.create_repair_ticket); bar.addWidget(new)
        if "edit" in permissions:
            diagnose=QPushButton("Diagnosis & estimate"); diagnose.clicked.connect(self.diagnose_repair_ticket); bar.addWidget(diagnose)
            approve=QPushButton("Approve estimate"); approve.clicked.connect(lambda:self.approve_repair_ticket(True)); bar.addWidget(approve)
            decline=QPushButton("Decline estimate"); decline.clicked.connect(lambda:self.approve_repair_ticket(False)); bar.addWidget(decline)
            ready=QPushButton("Mark ready"); ready.clicked.connect(self.mark_repair_ready); bar.addWidget(ready)
            collect=QPushButton("Collect device"); collect.clicked.connect(self.collect_repair_job); bar.addWidget(collect)
        if "manage_inventory" in permissions:
            part=QPushButton("Issue part from stock"); part.clicked.connect(self.issue_repair_part); bar.addWidget(part)
        if "manage_finance" in permissions:
            payment=QPushButton("Record payment"); payment.clicked.connect(self.record_repair_payment); bar.addWidget(payment)
        if "delete" in permissions:
            cancel=QPushButton("Cancel ticket"); cancel.clicked.connect(self.cancel_repair_ticket); bar.addWidget(cancel)
        if "print" in permissions:
            print_job=QPushButton("Print job sheet"); print_job.clicked.connect(lambda:self.print_selected_business_document("repair_job")); bar.addWidget(print_job)
        refresh=QPushButton("↻ Refresh"); refresh.clicked.connect(lambda:self.reload_page("Repair Tickets")); bar.addWidget(refresh)
        bar.addStretch(); v.addLayout(bar)
        note=QLabel("Parts are taken from on-hand inventory when issued. A technician’s pre-tax estimate must be approved before work and parts can proceed. Collection requires the final balance to be paid.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{self.palette['muted']};padding:10px;background:{self.palette['panel2']};border-radius:8px;"); v.addWidget(note)
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        table=self._table(["ID","TICKET","CUSTOMER","DEVICE / PRODUCT","WORK","SERVICE TYPE","STATUS","EST. COST","TOTAL","PAID","RECEIVED","EXPECTED COLLECTION"])
        rows=self.store.operations.repair_jobs()
        self._fill_table(table,rows,lambda r:[r["id"],r["ticket_number"],r["customer_name"],r["device"],r["work_kind"],r["service_type"],r["status"],f"{currency} {r['estimate_amount']}",f"{currency} {r['total']}",f"{currency} {r['paid_total']}",r["received_date"],r["expected_collection_date"]],hide_id=True)
        table.currentCellChanged.connect(lambda *_:self.refresh_repair_details())
        self.repair_jobs_table=table
        v.addWidget(table,1)
        details=QGridLayout(); details.setSpacing(14)
        parts_panel=self.panel(); parts_layout=QVBoxLayout(parts_panel); parts_layout.setContentsMargins(12,10,12,10)
        parts_layout.addWidget(QLabel("Parts issued"))
        self.repair_parts_table=self._table(["SKU","PART","QTY","UNIT PRICE","LINE TOTAL","ISSUED BY","DATE"])
        parts_layout.addWidget(self.repair_parts_table)
        events_panel=self.panel(); events_layout=QVBoxLayout(events_panel); events_layout.setContentsMargins(12,10,12,10)
        events_layout.addWidget(QLabel("Service history"))
        self.repair_events_table=self._table(["DATE","EVENT","DETAILS","ACTOR"])
        events_layout.addWidget(self.repair_events_table)
        details.addWidget(parts_panel,0,0); details.addWidget(events_panel,0,1)
        v.addLayout(details)
        if rows:
            table.selectRow(0); self.refresh_repair_details()
        return w

    def _selected_repair_job(self):
        table=getattr(self,"repair_jobs_table",None)
        job_id=self._selected_table_id(table) if table else None
        if job_id is None:
            QMessageBox.information(self,"Select a repair ticket","Select a repair ticket row first.")
            return None
        return self.store.operations.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()

    def refresh_repair_details(self):
        table=getattr(self,"repair_jobs_table",None)
        job_id=self._selected_table_id(table) if table else None
        if job_id is None: return
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        parts=self.store.operations.repair_job_parts(job_id)
        self._fill_table(self.repair_parts_table,parts,lambda r:[r["sku"],r["description"],r["quantity"],f"{currency} {r['unit_price']}",f"{currency} {r['line_total']}",r["created_by"],r["created_at"]])
        events=self.store.operations.repair_job_events(job_id)
        self._fill_table(self.repair_events_table,events,lambda r:[r["created_at"],r["event"],r["details"],r["actor"]])

    def create_repair_ticket(self):
        if not self._authorize("create"): return
        dialog=RepairJobDialog(self.store.operations.customers(),self.store.operations.products(active_only=True),self)
        if not dialog.exec(): return
        try:
            data=dialog.data()
            job_id=self.store.operations.create_repair_job(data["customer_name"],data["device"],data["reported_fault"],data["serial_number"],data["customer_id"],data["warranty_notes"],data["work_kind"],data["service_type"],data["received_date"],data["expected_collection_date"],data["product_id"],data["service_notes"])
            if data["estimated_cost"] > 0:
                self.store.operations.update_repair_diagnosis(job_id, data["reported_fault"], self.username, data["estimated_cost"], data["estimated_cost"])
            job=self.store.operations.conn.execute("SELECT ticket_number FROM repair_jobs WHERE id=?",(job_id,)).fetchone()
            self.store.log(self.username,"Received repair device",job["ticket_number"])
            self.reload_page("Repair Tickets")
        except (OperationError,ValueError,TypeError) as exc:
            QMessageBox.warning(self,"Ticket not created",str(exc))

    def diagnose_repair_ticket(self):
        if not self._authorize("edit"): return
        job=self._selected_repair_job()
        if not job: return
        dialog=RepairDiagnosisDialog(job,self.store.setting("business_currency","KES").split(" - ",1)[0],self)
        if not dialog.exec(): return
        try:
            data=dialog.data()
            self.store.operations.update_repair_diagnosis(job["id"],data["diagnosis"],data["technician"],data["labor_amount"],data["estimate_amount"])
            self.store.log(self.username,"Requested repair estimate approval",job["ticket_number"])
            self.reload_page("Repair Tickets")
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Diagnosis not saved",str(exc))

    def approve_repair_ticket(self,approved):
        if not self._authorize("edit"): return
        job=self._selected_repair_job()
        if not job: return
        decision="approve" if approved else "decline"
        answer=QMessageBox.question(self,f"{decision.title()} repair estimate",f"{decision.title()} the pre-tax estimate for {job['ticket_number']}?",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes: return
        try:
            self.store.operations.approve_repair_estimate(job["id"],approved)
            self.store.log(self.username,"Customer estimate decision",f"{job['ticket_number']}: {decision}")
            self.reload_page("Repair Tickets")
        except OperationError as exc:
            QMessageBox.warning(self,"Estimate unchanged",str(exc))

    def issue_repair_part(self):
        if not self._authorize("manage_inventory"): return
        job=self._selected_repair_job()
        if not job: return
        products=list(self.store.operations.products())
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        dialog=RepairPartDialog(products,currency,self)
        if not dialog.exec(): return
        try:
            data=dialog.data()
            self.store.operations.add_repair_part(job["id"],data["product_id"],data["quantity"])
            self.store.log(self.username,"Issued repair part",f"{job['ticket_number']} · product {data['product_id']}")
            self.reload_page("Repair Tickets")
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Part not issued",str(exc))

    def mark_repair_ready(self):
        if not self._authorize("edit"): return
        job=self._selected_repair_job()
        if not job: return
        try:
            self.store.operations.mark_repair_ready(job["id"])
            self.store.log(self.username,"Repair ready for collection",job["ticket_number"])
            self.reload_page("Repair Tickets")
        except OperationError as exc:
            QMessageBox.warning(self,"Repair not ready",str(exc))

    def record_repair_payment(self):
        if not self._authorize("manage_finance"): return
        job=self._selected_repair_job()
        if not job: return
        balance=Decimal(job["total"])-Decimal(job["paid_total"])
        if balance<=0:
            QMessageBox.information(self,"No balance due","This repair has no outstanding amount.")
            return
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        dialog=RepairPaymentDialog(balance,currency,self)
        if not dialog.exec(): return
        try:
            data=dialog.data()
            self.store.operations.record_repair_payment(job["id"],data["amount"],data["reference"],data["payment_method"])
            self.store.log(self.username,"Recorded repair payment",f"{job['ticket_number']} · {data['amount']:.2f}")
            self.reload_page("Repair Tickets")
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Payment not saved",str(exc))

    def collect_repair_job(self):
        if not self._authorize("edit"): return
        job=self._selected_repair_job()
        if not job: return
        try:
            self.store.operations.collect_repair_job(job["id"])
            self.store.log(self.username,"Device collected",job["ticket_number"])
            self.reload_page("Repair Tickets")
        except OperationError as exc:
            QMessageBox.warning(self,"Device not collected",str(exc))

    def cancel_repair_ticket(self):
        if not self._authorize("delete"): return
        job=self._selected_repair_job()
        if not job: return
        answer=QMessageBox.question(self,"Cancel repair ticket",f"Cancel {job['ticket_number']}? Tickets with work or parts issued cannot be cancelled.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes: return
        try:
            self.store.operations.cancel_repair_job(job["id"],"Cancelled by administrator")
            self.store.log(self.username,"Cancelled repair ticket",job["ticket_number"])
            self.reload_page("Repair Tickets")
        except OperationError as exc:
            QMessageBox.warning(self,"Ticket not cancelled",str(exc))

    def create_purchase_order(self):
        if not self._authorize("manage_inventory"): return
        products=list(self.store.operations.products())
        suppliers=self.store.operations.suppliers()
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        dlg=StockTransactionDialog(products,suppliers,"purchase",currency,self,document_type="order")
        if not dlg.exec(): return
        try:
            data=dlg.transaction_data()
            order_id=self.store.operations.create_purchase_order(data["party_name"],data["lines"],data["party_id"])
            order=self.store.operations.conn.execute("SELECT order_number FROM purchase_orders WHERE id=?",(order_id,)).fetchone()
            self.store.log(self.username,"Created purchase order",order["order_number"])
            self.reload_page("Purchases")
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Purchase order not saved",str(exc))

    def receive_selected_purchase_order(self):
        if not self._authorize("manage_inventory"): return
        table=getattr(self,"purchase_orders_table",None)
        order_id=self._selected_table_id(table) if table else None
        if order_id is None:
            QMessageBox.information(self,"Select an order","Select a purchase order first."); return
        order=self.store.operations.conn.execute("SELECT * FROM purchase_orders WHERE id=?",(order_id,)).fetchone()
        if not order or order["status"] not in {"Ordered","Partially received"}:
            QMessageBox.information(self,"Order unavailable","Only open orders can receive goods."); return
        lines=self.store.operations.purchase_order_lines(order_id)
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        dlg=PurchaseOrderReceiptDialog(order,lines,currency,self)
        if not dlg.exec(): return
        try:
            data=dlg.receipt_data()
            purchase_id=self.store.operations.receive_purchase_order(order_id,data["lines"],data["amount_paid"],data["payment_method"],data["payment_reference"])
            purchase=self.store.operations.conn.execute("SELECT purchase_number FROM purchases WHERE id=?",(purchase_id,)).fetchone()
            self.store.log(self.username,"Received purchase order",f"{order['order_number']} · {purchase['purchase_number']}")
            self.reload_page("Purchases")
            self.invalidate_page("Inventory")
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Goods not received",str(exc))

    def cancel_selected_purchase_order(self):
        if not self._authorize("delete"): return
        table=getattr(self,"purchase_orders_table",None)
        order_id=self._selected_table_id(table) if table else None
        if order_id is None:
            QMessageBox.information(self,"Select an order","Select a purchase order first."); return
        order=self.store.operations.conn.execute("SELECT order_number,status FROM purchase_orders WHERE id=?",(order_id,)).fetchone()
        if not order: return
        reason,ok=QInputDialog.getText(self,"Cancel purchase order",f"Reason for cancelling {order['order_number']}:")
        if not ok: return
        answer=QMessageBox.question(self,"Confirm cancellation",f"Cancel {order['order_number']} and its outstanding quantity? Already received goods and receipts remain in history.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes: return
        try:
            self.store.operations.cancel_purchase_order(order_id,reason)
            self.store.log(self.username,"Cancelled purchase order",f"{order['order_number']} · {reason.strip()}")
            self.reload_page("Purchases")
        except OperationError as exc:
            QMessageBox.warning(self,"Order not cancelled",str(exc))

    def open_stock_transaction(self, mode):
        capability="create"
        if mode=="purchase" and not self._authorize("manage_inventory"): return
        if mode=="sale" and not self._authorize(capability): return
        products=list(self.store.operations.products())
        parties=self.store.operations.customers() if mode=="sale" else self.store.operations.suppliers()
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        dlg=StockTransactionDialog(products,parties,mode,currency,self,tax_rate=self.store.setting("business_tax_rate","0.00") if mode=="sale" else 0,tax_label=self.store.setting("business_tax_label","Tax"))
        if not dlg.exec(): return
        try:
            data=dlg.transaction_data()
            if mode=="sale":
                record_id=self.store.operations.record_sale(data["party_name"],data["lines"],data["amount_paid"],data["party_id"],data["payment_method"],data["payment_reference"])
                action,number="Completed sale",f"SALE-{record_id:08d}"
                page="Sales"
            else:
                if not data["party_name"]:
                    raise OperationError("Enter a supplier name before receiving stock.")
                record_id=self.store.operations.receive_purchase(data["party_name"],data["lines"],data["amount_paid"],data["party_id"],data["payment_method"],data["payment_reference"])
                action,number="Received purchase",f"PUR-{record_id:08d}"
                page="Purchases"
            self.store.log(self.username,action,number)
            self._refresh_operation_page(page)
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Transaction not saved",str(exc))

    def create_quote(self):
        if not self._authorize("create"): return
        products=list(self.store.operations.products())
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        dlg=StockTransactionDialog(products,self.store.operations.customers(),"sale",currency,self,document_type="quote",tax_rate=self.store.setting("business_tax_rate","0.00"),tax_label=self.store.setting("business_tax_label","Tax"))
        if not dlg.exec(): return
        try:
            data=dlg.transaction_data()
            quote_id=self.store.operations.create_quote(data["party_name"],data["lines"],data["party_id"],data["expires_on"])
            quote=self.store.operations.conn.execute("SELECT quote_number FROM quotes WHERE id=?",(quote_id,)).fetchone()
            self.store.log(self.username,"Created quotation",quote["quote_number"])
            self.reload_page("Sales")
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Quotation not saved",str(exc))

    def _selected_quote(self):
        table=getattr(self,"quote_table",None)
        quote_id=self._selected_table_id(table) if table else None
        if quote_id is None:
            QMessageBox.information(self,"Select a quotation","Select a quotation row first.")
            return None
        return self.store.operations.conn.execute("SELECT * FROM quotes WHERE id=?",(quote_id,)).fetchone()

    def change_selected_quote_status(self,status):
        if not self._authorize("edit"): return
        quote=self._selected_quote()
        if not quote: return
        try:
            self.store.operations.set_quote_status(quote["id"],status)
            self.store.log(self.username,"Updated quotation status",f"{quote['quote_number']}: {status}")
            self.reload_page("Sales")
        except OperationError as exc:
            QMessageBox.warning(self,"Quotation status unchanged",str(exc))

    def convert_quote_selected(self):
        if not self._authorize("create"): return
        quote=self._selected_quote()
        if not quote: return
        if quote["status"]!="Accepted":
            QMessageBox.information(self,"Quote not accepted","Mark the quotation as accepted before converting it to a sale.")
            return
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        total=float(quote["total"])
        paid,ok=QInputDialog.getDouble(self,"Convert quotation",f"Amount received for {quote['quote_number']}:",total,0,total,2)
        if not ok: return
        try:
            sale_id=self.store.operations.convert_quote(quote["id"],paid)
            sale=self.store.operations.conn.execute("SELECT sale_number FROM sales WHERE id=?",(sale_id,)).fetchone()
            self.store.log(self.username,"Converted quotation to sale",f"{quote['quote_number']} → {sale['sale_number']}")
            QMessageBox.information(self,"Quotation converted",f"Sale {sale['sale_number']} was created. Inventory and payment records were updated.")
            self.reload_page("Sales")
        except OperationError as exc:
            QMessageBox.warning(self,"Quotation not converted",str(exc))

    def cancel_selected_quote(self):
        if not self._authorize("delete"): return
        quote=self._selected_quote()
        if not quote: return
        answer=QMessageBox.question(self,"Cancel quotation",f"Cancel {quote['quote_number']}? Its history will be kept.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes: return
        try:
            self.store.operations.set_quote_status(quote["id"],"Cancelled")
            self.store.log(self.username,"Cancelled quotation",quote["quote_number"])
            self.reload_page("Sales")
        except OperationError as exc:
            QMessageBox.warning(self,"Quotation not cancelled",str(exc))

    def return_selected_sale(self):
        if not self._authorize("process_returns"): return
        table=self.pages["Sales"].findChild(QTableWidget)
        sale_id=self._selected_table_id(table) if table else None
        if sale_id is None:
            QMessageBox.information(self,"Select a sale","Select a sale first."); return
        sale=self.store.operations.conn.execute("SELECT * FROM sales WHERE id=?",(sale_id,)).fetchone()
        if not sale or sale["status"] not in {"Completed","Partially returned"}:
            QMessageBox.information(self,"Sale unavailable","Only sales with unreturned items can be returned."); return
        source_lines=self.store.operations.conn.execute("SELECT * FROM sale_lines WHERE sale_id=? ORDER BY id",(sale_id,)).fetchall()
        details=[]
        for row in source_lines:
            item=dict(row); item["sale_line_id"]=int(row["id"]); item["sold_quantity"]=row["quantity"]
            item["returned_quantity"]=self.store.operations.returned_quantity(row["id"])
            details.append(item)
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        dlg=SaleReturnDialog(sale["sale_number"],details,currency,self)
        if not dlg.exec(): return
        try:
            data=dlg.return_data()
            return_id=self.store.operations.record_sale_return(sale_id,data["lines"],data["reason"],data["payment_method"],data["payment_reference"])
            self.store.log(self.username,"Returned sale items",f"{sale['sale_number']} · RET-{return_id:08d} · {data['reason']}")
            QMessageBox.information(self,"Return recorded",f"Return RET-{return_id:08d} was saved. The cash refund equals the returned items' original selling value.")
            self._refresh_operation_page("Sales")
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Return not saved",str(exc))

    def cancel_selected_sale(self):
        if not self._authorize("delete"): return
        table=self.pages["Sales"].findChild(QTableWidget)
        sale_id=self._selected_table_id(table) if table else None
        if sale_id is None:
            QMessageBox.information(self,"Select a sale","Select a sale first."); return
        sale=self.store.operations.conn.execute("SELECT paid_total,sale_number FROM sales WHERE id=?",(sale_id,)).fetchone()
        if not sale: return
        refundable=max(Decimal("0.00"),Decimal(sale["paid_total"]))
        reason,ok=QInputDialog.getText(self,"Cancel sale",f"Reason for cancelling {sale['sale_number']}:")
        if not ok: return
        refund,ok=QInputDialog.getDouble(self,"Sale refund","Remaining amount paid to refund:",float(refundable),0,float(refundable),2)
        if not ok: return
        try:
            self.store.operations.cancel_sale(sale_id,reason,refund)
            self.store.log(self.username,"Cancelled sale",f"{sale['sale_number']} · refund {refund:.2f}")
            self._refresh_operation_page("Sales")
        except OperationError as exc:
            QMessageBox.warning(self,"Sale cancellation rejected",str(exc))

    def cancel_selected_purchase(self):
        if not self._authorize("delete"): return
        table=self.pages["Purchases"].findChild(QTableWidget)
        purchase_id=self._selected_table_id(table) if table else None
        if purchase_id is None:
            QMessageBox.information(self,"Select a purchase","Select a purchase first."); return
        purchase=self.store.operations.conn.execute("SELECT paid_total,purchase_number FROM purchases WHERE id=?",(purchase_id,)).fetchone()
        if not purchase: return
        reason,ok=QInputDialog.getText(self,"Cancel purchase",f"Reason for cancelling {purchase['purchase_number']}:")
        if not ok: return
        refund,ok=QInputDialog.getDouble(self,"Supplier refund","Amount refunded:",float(purchase["paid_total"]),0,float(purchase["paid_total"]),2)
        if not ok: return
        try:
            self.store.operations.cancel_purchase(purchase_id,reason,refund)
            self.store.log(self.username,"Cancelled purchase",f"{purchase['purchase_number']} · refund {refund:.2f}")
            self._refresh_operation_page("Purchases")
        except OperationError as exc:
            QMessageBox.warning(self,"Purchase cancellation rejected",str(exc))

    def finance_page(self):
        w,v=self.page_container("Cashbook & Cashflow","Track actual money received and paid, separate payment channels, record shop expenses, and reconcile M-Pesa references. This is an operational cashbook, not a double-entry general ledger.")
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        top=QHBoxLayout(); top.setSpacing(10)
        start_date=QDateEdit(); start_date.setCalendarPopup(True); start_date.setDisplayFormat("yyyy-MM-dd"); start_date.setDate(QDate.currentDate().addDays(-30))
        end_date=QDateEdit(); end_date.setCalendarPopup(True); end_date.setDisplayFormat("yyyy-MM-dd"); end_date.setDate(QDate.currentDate())
        top.addWidget(QLabel("From")); top.addWidget(start_date); top.addWidget(QLabel("To")); top.addWidget(end_date)
        refresh=QPushButton("↻ Update report"); refresh.clicked.connect(lambda:self._refresh_finance_view(start_date,end_date)); top.addWidget(refresh)
        if "manage_finance" in self.store.role_permissions(self.role):
            add=QPushButton("＋ Record money in / out"); add.setObjectName("primary"); add.clicked.connect(self.create_cash_entry_dialog); top.addWidget(add)
        export=QPushButton("Export CSV"); export.clicked.connect(lambda:self.export_cashbook_csv(start_date,end_date)); top.addWidget(export); top.addStretch()
        v.addLayout(top)
        cards=QGridLayout(); cards.setSpacing(12)
        self.finance_summary_labels={}
        for col,(key,title,detail,accent) in enumerate([
            ("money_in","Money received","Sales, repairs & other receipts",self.palette["green"]),
            ("money_out","Money paid","Suppliers, refunds & expenses",self.palette["orange"]),
            ("net_movement","Net movement","Received minus paid in selected period",self.palette["blue"]),
        ]):
            frame=self.panel(); box=QVBoxLayout(frame); box.setContentsMargins(16,14,16,14)
            label=QLabel(title); label.setStyleSheet("font-weight:700;")
            amount=QLabel(f"{currency} 0.00"); amount.setStyleSheet(f"font-size:18pt;font-weight:900;color:{accent};")
            desc=QLabel(detail); desc.setWordWrap(True); desc.setStyleSheet(f"color:{self.palette['muted']};font-size:9pt;")
            box.addWidget(label); box.addWidget(amount); box.addWidget(desc); cards.addWidget(frame,0,col)
            self.finance_summary_labels[key]=amount
        v.addLayout(cards)
        v.addWidget(QLabel("Movement by payment channel"))
        self.finance_channels_table=self._table(["PAYMENT CHANNEL","MONEY IN","MONEY OUT","NET","ENTRIES"])
        v.addWidget(self.finance_channels_table)
        v.addWidget(QLabel("Cashbook entries · newest first"))
        self.finance_entries_table=self._table(["ID","DATE","DIRECTION","CATEGORY","AMOUNT","CHANNEL","REFERENCE","DESCRIPTION","SOURCE"])
        v.addWidget(self.finance_entries_table,1)
        self._refresh_finance_view(start_date,end_date)
        return w

    def _refresh_finance_view(self,start_date,end_date):
        if end_date.date() < start_date.date():
            QMessageBox.warning(self,"Invalid date range","The start date must be on or before the end date.")
            return
        start=start_date.date().toString("yyyy-MM-dd"); end=end_date.date().toString("yyyy-MM-dd")
        try:
            report=self.store.operations.cashflow_summary(start,end)
        except (OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Cashflow report unavailable",str(exc)); return
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        for key in ("money_in","money_out","net_movement"):
            self.finance_summary_labels[key].setText(f"{currency} {report[key]:,.2f}")
        methods=report["by_method"]
        ordered=sorted(methods.items(),key=lambda item:(item[0]=="Unspecified",item[0].casefold()))
        self.finance_channels_table.setRowCount(len(ordered))
        for row,(method,values) in enumerate(ordered):
            cells=[method,f"{currency} {values['in']:,.2f}",f"{currency} {values['out']:,.2f}",f"{currency} {values['net']:,.2f}",values["count"]]
            for col,value in enumerate(cells): self.finance_channels_table.setItem(row,col,QTableWidgetItem(str(value)))
        entries=report["entries"]
        self.finance_entries_table.setRowCount(len(entries))
        for row,entry in enumerate(entries):
            category=(entry["category"] or "").strip() or entry["source_type"] or "Other"
            cells=[entry["id"],entry["created_at"][:19].replace("T"," "),"Money in" if entry["direction"]=="in" else "Money out",category,
                   f"{currency} {Decimal(entry['amount']):,.2f}",entry["payment_method"] or "Unspecified",entry["reference"],entry["description"],f"{entry['source_type']} #{entry['source_id']}"]
            for col,value in enumerate(cells): self.finance_entries_table.setItem(row,col,QTableWidgetItem(str(value)))
            if entry["direction"]=="out":
                self.finance_entries_table.item(row,2).setForeground(QColor(self.palette["orange"]))
            else:
                self.finance_entries_table.item(row,2).setForeground(QColor(self.palette["green"]))

    def export_cashbook_csv(self,start_date,end_date):
        start=start_date.date().toString("yyyy-MM-dd"); end=end_date.date().toString("yyyy-MM-dd")
        if end < start:
            QMessageBox.warning(self,"Invalid date range","The start date must be on or before the end date."); return
        path,_=QFileDialog.getSaveFileName(self,"Export cashbook",f"cashbook_{start}_to_{end}.csv","CSV files (*.csv)")
        if not path: return
        try:
            report=self.store.operations.cashflow_summary(start,end)
            with open(path,"w",newline="",encoding="utf-8-sig") as handle:
                writer=csv.writer(handle)
                writer.writerow(["Date","Direction","Category","Amount","Payment method","Reference","Description","Source type","Source ID"])
                for entry in reversed(report["entries"]):
                    writer.writerow([entry["created_at"],entry["direction"],(entry["category"] or "").strip() or entry["source_type"],entry["amount"],entry["payment_method"],entry["reference"],entry["description"],entry["source_type"],entry["source_id"]])
            self.store.log(self.username,"Exported cashbook CSV",f"{start} to {end}")
            QMessageBox.information(self,"Cashbook exported",f"Saved {len(report['entries'])} entries to:\n{path}")
        except (OSError,OperationError,ValueError) as exc:
            QMessageBox.warning(self,"Cashbook export failed",str(exc))

    def create_cash_entry_dialog(self):
        if not self._authorize("manage_finance"): return
        dlg=QDialog(self); dlg.setWindowTitle("Record money in / out"); dlg.setMinimumWidth(470); dlg.setStyleSheet(themed_style(self.palette))
        form=QFormLayout(dlg)
        direction=QComboBox(); direction.addItem("Money in","in"); direction.addItem("Money out","out")
        amount=QDoubleSpinBox(); amount.setDecimals(2); amount.setRange(0.01,999999999999.99); amount.setSingleStep(100); amount.setPrefix(self.store.setting("business_currency","KES").split(" - ",1)[0]+" ")
        method=QComboBox(); method.addItems(["Cash","M-Pesa","Bank transfer","Card","Other"])
        reference=QLineEdit(); reference.setPlaceholderText("Required for M-Pesa (e.g. transaction code)")
        category=QComboBox(); category.setEditable(True); category.addItems(["Operating expense","Rent","Utilities","Transport","Supplier payment","Owner withdrawal","Owner contribution","Refund","Other income","General"]); category.setCurrentText("Operating expense")
        description=QLineEdit(); description.setPlaceholderText("What was received or paid? (e.g. shop rent, delivery)")
        note=QLabel("Enter money that is not already recorded by a sale, purchase or repair payment. This avoids double-counting transactions."); note.setWordWrap(True); note.setStyleSheet(f"color:{self.palette['muted']};")
        form.addRow("Direction",direction); form.addRow("Category",category); form.addRow("Amount",amount); form.addRow("Payment channel",method); form.addRow("Transaction reference",reference); form.addRow("Description",description); form.addRow(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); form.addRow(buttons); buttons.rejected.connect(dlg.reject)
        method.currentTextChanged.connect(lambda value:reference.setPlaceholderText("Enter the confirmed M-Pesa transaction code" if value=="M-Pesa" else "Bank / card reference (optional)" if value in {"Bank transfer","Card"} else "Reference (optional)"))
        def save():
            try:
                entry_id=self.store.operations.record_cash_entry(direction.currentData(),amount.value(),description.text(),method.currentText(),reference.text(),category.currentText())
                self.store.log(self.username,"Recorded cashbook movement",f"Entry {entry_id}: {direction.currentData()} {amount.value():.2f} · {method.currentText()} · {category.currentText()}")
                dlg.accept()
            except (OperationError,ValueError) as exc:
                QMessageBox.warning(dlg,"Cash movement not saved",str(exc))
        buttons.accepted.connect(save)
        if dlg.exec():
            self.invalidate_page("Dashboard")
            current=self.stack.currentWidget()
            if current is self.pages.get("Finance"):
                self._refresh_operation_page("Finance")

    def crm_page(self):
        w,v=self.page_container("Customers","Customer directory, negotiated pricing, credit facilities and service-history follow-up.")
        row=QHBoxLayout()
        if "create" in self.store.role_permissions(self.role):
            add=QPushButton("＋ Add customer"); add.setObjectName("primary"); add.clicked.connect(lambda:self.create_party_dialog("customer")); row.addWidget(add)
        legacy=QPushButton("View legacy CRM records"); legacy.clicked.connect(lambda:self._show_legacy_records("CRM")); row.addWidget(legacy)
        if self.role=="Admin":
            credit=QPushButton("Manage credit facility"); credit.clicked.connect(lambda:self._manage_customer_credit(table)); row.addWidget(credit)
            audit=QPushButton("Activity audit"); audit.clicked.connect(self._show_audit_events); row.addWidget(audit)
            credit_report=QPushButton("Credit activity report"); credit_report.clicked.connect(self._show_credit_activity); row.addWidget(credit_report)
        row.addStretch(); v.addLayout(row)
        table=self._table(["ID","CUSTOMER","CONTACT","EMAIL","CREDIT","LIMIT (KES)","BALANCE (KES)","CREATED"])
        rows=self.store.operations.customers()
        enriched=[]
        for r in rows:
            try:
                summary=self.store.operations.customer_credit_summary(r["id"])
            except Exception: summary={"enabled":False,"limit":Decimal("0"),"outstanding":Decimal("0")}
            enriched.append((r,summary))
        table.setRowCount(len(enriched))
        for i,(r,summary) in enumerate(enriched):
            values=[r["id"],r["name"],r["contact"],r["email"],"Enabled" if summary["enabled"] else "Disabled",
                    f"{summary['limit']:.2f}",f"{summary['outstanding']:.2f}",r["created_at"]]
            for j,val in enumerate(values):
                item=QTableWidgetItem(str(val)); table.setItem(i,j,item)
        table.setColumnHidden(0,True)
        table.setSortingEnabled(True)
        v.addWidget(table,1); return w

    def _manage_customer_credit(self, table):
        if self.role!="Admin": return
        row=table.currentRow()
        if row<0:
            QMessageBox.information(self,"Select customer","Select a customer row first."); return
        customer_id=int(table.item(row,0).text())
        customer=self.store.operations.conn.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()
        if not customer: return
        enabled,ok=QInputDialog.getItem(self,"Credit facility",f"{customer['name']} · enable credit?",["Enabled","Disabled"],0 if customer["credit_enabled"] else 1,False)
        if not ok:return
        limit,ok=QInputDialog.getDouble(self,"Credit limit",f"Maximum outstanding for {customer['name']} (KES):",float(customer["credit_limit"] or 0),0,100000000,2)
        if not ok:return
        days,ok=QInputDialog.getInt(self,"Repayment terms","Credit duration in days (0 = due immediately):",int(customer["credit_days"] or 0),0,3650)
        if not ok:return
        try:
            self.store.operations.configure_customer_credit(customer_id,enabled=="Enabled",limit,days)
            self.store.log(self.username,"Credit facility changed",f"{customer['name']}: {enabled}, KES {limit:.2f}, {days} days")
            self.show_page("CRM")
        except Exception as exc: QMessageBox.warning(self,"Credit update failed",str(exc))

    def _show_credit_activity(self):
        if self.role!="Admin": return
        period,ok=QInputDialog.getItem(self,"Credit activity","Reporting period:",["Today","Last 7 days","All time"],0,False)
        if not ok:return
        if period=="Today": since=datetime.now().strftime("%Y-%m-%d")
        elif period=="Last 7 days": since=(datetime.now()-__import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")
        else: since=None
        dlg=QDialog(self); dlg.setWindowTitle("Customer credit activity · KES"); dlg.resize(1100,620)
        root=QVBoxLayout(dlg); table=QTableWidget(0,7)
        table.setHorizontalHeaderLabels(["CUSTOMER","CREDIT LIMIT","USED / OUTSTANDING","AVAILABLE","CREDIT CREATED IN PERIOD","REPAID IN PERIOD","TERMS"])
        customers=self.store.operations.customers(); table.setRowCount(len(customers))
        for i,c in enumerate(customers):
            summary=self.store.operations.customer_credit_summary(c["id"])
            activity=self.store.operations.credit_activity_summary(c["id"],since)
            vals=[c["name"],f"{summary['limit']:.2f}",f"{summary['outstanding']:.2f}",f"{summary['available']:.2f}",
                  f"{activity['credit_extended']:.2f}",f"{activity['repaid']:.2f}",f"{summary['credit_days']} days"]
            for j,val in enumerate(vals):table.setItem(i,j,QTableWidgetItem(str(val)))
        table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch); table.setAlternatingRowColors(True)
        root.addWidget(table)
        close=QPushButton("Close");close.clicked.connect(dlg.accept);root.addWidget(close);dlg.exec()

    def _show_audit_events(self):
        if self.role!="Admin": return
        dlg=QDialog(self); dlg.setWindowTitle("Activity audit log"); dlg.resize(1050,600)
        root=QVBoxLayout(dlg); table=QTableWidget(0,7)
        table.setHorizontalHeaderLabels(["ID","USER","ROLE","ACTION","ENTITY","DETAILS","TIMESTAMP"])
        audit_rows=self.store.operations.conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1000").fetchall()
        try:
            legacy_rows=self.store.conn.execute("SELECT username,action,details,created_at FROM activity WHERE workspace_id=? ORDER BY id DESC LIMIT 1000",(self.store.workspace_id or "__global__",)).fetchall()
        except Exception:
            legacy_rows=[]
        rows=[(r["id"],r["username"],r["role"],r["action"],r["entity_type"],r["details"],r["created_at"]) for r in audit_rows]
        rows += [(f"legacy-{r['created_at']}",r["username"],"—",r["action"],"activity",r["details"],r["created_at"]) for r in legacy_rows]
        rows.sort(key=lambda x:str(x[6]),reverse=True)
        table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            for j,val in enumerate(r):
                table.setItem(i,j,QTableWidgetItem(str(val)))
        table.horizontalHeader().setSectionResizeMode(5,QHeaderView.Stretch)
        table.setAlternatingRowColors(True); root.addWidget(table)
        close=QPushButton("Close"); close.clicked.connect(dlg.accept); root.addWidget(close); dlg.exec()

    def create_party_dialog(self, kind):
        if not self._authorize("create"): return
        is_customer=kind=="customer"
        dlg=QDialog(self); dlg.setWindowTitle("Add customer" if is_customer else "Add supplier"); dlg.setStyleSheet(themed_style(self.palette))
        form=QFormLayout(dlg); name=QLineEdit(); contact=QLineEdit(); email=QLineEdit()
        form.addRow("Name",name); form.addRow("Phone / contact",contact); form.addRow("Email",email)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); form.addRow(buttons); buttons.rejected.connect(dlg.reject)
        def save():
            try:
                if is_customer: party_id=self.store.operations.create_customer(name.text(),contact.text(),email.text())
                else: party_id=self.store.operations.create_supplier(name.text(),contact.text(),email.text())
                self.store.log(self.username,"Created customer" if is_customer else "Created supplier",f"{name.text().strip()} · id {party_id}")
                dlg.accept()
            except OperationError as exc:
                QMessageBox.warning(dlg,"Contact not saved",str(exc))
        buttons.accepted.connect(save)
        if dlg.exec():
            page="CRM" if is_customer else "Purchases"
            self._refresh_operation_page(page)

    def reports_page(self):
        w,v=self.page_container("Reports","Report centre — pick a report and a period, then view, export to CSV or print.")
        ops=self.store.operations; cur=self.currency_code()
        cards=[("Completed sales",sum(1 for row in ops.sales(100000) if row["status"]=="Completed")),
               ("Inventory value",f"{cur} {ops.inventory_value():,.2f}"),
               ("Net cashbook movement",f"{cur} {ops.cash_balance():,.2f}")]
        grid=QHBoxLayout()
        for label,value in cards:
            card=self.panel(); layout=QVBoxLayout(card); layout.setContentsMargins(18,12,18,12)
            layout.addWidget(QLabel(label)); number=QLabel(str(value)); number.setStyleSheet(f"color:{self.palette['blue']};font-size:16pt;font-weight:800;"); layout.addWidget(number)
            grid.addWidget(card)
        v.addLayout(grid)
        controls=QHBoxLayout()
        which=QComboBox()
        for key,label in report_engine.REPORT_NAMES: which.addItem(label,key)
        period=QComboBox()
        for label in ("Today","Last 7 days","This month","Last 30 days","This year","All time","Custom…"): period.addItem(label)
        period.setCurrentText("Last 30 days")
        d_from=QDateEdit(QDate.currentDate().addDays(-30)); d_from.setCalendarPopup(True); d_from.setDisplayFormat("yyyy-MM-dd")
        d_to=QDateEdit(QDate.currentDate()); d_to.setCalendarPopup(True); d_to.setDisplayFormat("yyyy-MM-dd")
        run=QPushButton("Run report"); run.setObjectName("primary")
        export=QPushButton("Export CSV"); printb=QPushButton("🖨  Print")
        for widget in (QLabel("Report"),which,QLabel("Period"),period,d_from,QLabel("to"),d_to,run,export,printb): controls.addWidget(widget)
        controls.addStretch(); v.addLayout(controls)
        table=self._table(["—"]); v.addWidget(table,1)
        summary=QLabel(""); summary.setStyleSheet(f"color:{self.palette['muted']};"); v.addWidget(summary)
        state={"report":None,"period":""}
        def apply_period():
            today=QDate.currentDate(); choice=period.currentText()
            ranges={"Today":(today,today),"Last 7 days":(today.addDays(-6),today),"This month":(QDate(today.year(),today.month(),1),today),
                    "Last 30 days":(today.addDays(-29),today),"This year":(QDate(today.year(),1,1),today),"All time":(QDate(2000,1,1),today)}
            if choice in ranges: d_from.setDate(ranges[choice][0]); d_to.setDate(ranges[choice][1])
            custom=choice=="Custom…"; d_from.setEnabled(custom); d_to.setEnabled(custom)
        def do_run():
            start=d_from.date().toString("yyyy-MM-dd"); end=d_to.date().toString("yyyy-MM-dd")
            try: rep=report_engine.build(ops.conn,which.currentData(),start,end)
            except Exception as exc:
                QMessageBox.warning(self,"Report failed",str(exc)); return
            state["report"]=rep; state["period"]=f"{start} to {end}"
            table.setColumnCount(len(rep.headers)); table.setHorizontalHeaderLabels([h.upper() for h in rep.headers])
            rows=list(rep.rows)+([rep.footer] if rep.footer else [])
            table.setRowCount(len(rows))
            for i,row in enumerate(rows):
                for j,value in enumerate(row):
                    item=QTableWidgetItem(str(value))
                    if rep.footer and i==len(rows)-1:
                        f=item.font(); f.setBold(True); item.setFont(f)
                    table.setItem(i,j,item)
            summary.setText(f"{len(rep.rows)} row(s) · {state['period']}")
        def do_export():
            if state["report"] is None: do_run()
            rep=state["report"]
            path,_=QFileDialog.getSaveFileName(self,"Export report",str(Path.home()/"Documents"/f"{which.currentData()}.csv"),"CSV files (*.csv)")
            if not path: return
            try: count=report_engine.to_csv(rep,path)
            except OSError as exc: QMessageBox.warning(self,"Export failed",str(exc)); return
            self.toast.show_message(f"✔  Exported {count} row(s) to {path}",ok=True,seconds=6)
        def do_print():
            if state["report"] is None: do_run()
            rep=state["report"]; business={"name":self.store.setting("business_name","")}
            self._print_service().print_document(self,rep.title,lambda layout:report_engine.to_html(rep,business,state["period"],layout),f"report {which.currentData()}")
        period.currentTextChanged.connect(lambda *_:(apply_period(),do_run())); which.currentIndexChanged.connect(lambda *_:do_run())
        run.clicked.connect(do_run); export.clicked.connect(do_export); printb.clicked.connect(do_print)
        d_from.dateChanged.connect(lambda *_:do_run() if period.currentText()=="Custom…" else None)
        d_to.dateChanged.connect(lambda *_:do_run() if period.currentText()=="Custom…" else None)
        apply_period(); do_run()
        legacy=QPushButton("View legacy report records"); legacy.clicked.connect(lambda:self._show_legacy_records("Reports")); v.addWidget(legacy,0,Qt.AlignLeft)
        return w

    def batches_page(self):
        """Lot-level stock receiving, expiry alerts, quarantine and disposal."""
        ops=self.store.operations
        w,v=self.page_container("Batches & Expiry · lot traceability",
            "For pharmacy, food, cosmetics and other expiry-sensitive inventory. Each lot remains linked to the shared purchase, stock ledger and sale allocations.")
        toolbar=QHBoxLayout()
        receive=QPushButton("＋ Receive tracked batch"); receive.setObjectName("primary")
        enable=QPushButton("Enable tracking / allocate existing stock")
        change=QPushButton("Change selected lot status")
        export=QPushButton("Export batch register CSV")
        refresh=QPushButton("↻ Refresh")
        for control in (receive,enable,change,export,refresh): toolbar.addWidget(control)
        toolbar.addStretch(); v.addLayout(toolbar)
        alert=QLabel(); alert.setWordWrap(True); alert.setStyleSheet(f"padding:12px;border:1px solid {self.palette['border']};border-radius:8px;color:{self.palette['orange']};")
        v.addWidget(alert)
        table=self._table(["ID","SKU","PRODUCT","LOT / BATCH","EXPIRY","RECEIVED","REMAINING","UNIT","SUPPLIER","STATUS"])
        table.setColumnHidden(0,True); table.setSelectionBehavior(QAbstractItemView.SelectRows); table.setSelectionMode(QAbstractItemView.SingleSelection)
        v.addWidget(table,1)

        def refresh_table():
            try:
                rows=ops.inventory_batches(include_empty=True,limit=10000)
                alerts=ops.batch_expiry_alerts(90)
            except (OperationError,sqlite3.Error) as exc:
                QMessageBox.warning(w,"Batch register unavailable",str(exc)); return
            alert.setText(f"{len(alerts)} lot(s) are expired or expire within 90 days. Expired or quarantined stock is not sellable.")
            table.setRowCount(len(rows))
            for ri,row in enumerate(rows):
                remaining=Decimal(str(row["remaining_quantity"])); received=Decimal(str(row["received_quantity"]))
                vals=[row["id"],row["sku"],row["product_name"],row["lot_number"],row["expiry_date"] or "—",
                      f"{received:,.3f}".rstrip("0").rstrip("."),f"{remaining:,.3f}".rstrip("0").rstrip("."),row["unit"],row["supplier_name"] or "—",row["status"]]
                for ci,value in enumerate(vals):
                    cell=QTableWidgetItem(str(value)); cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                    table.setItem(ri,ci,cell)
            table.resizeColumnsToContents()

        def tracked_products(only_tracked=False):
            found=[]
            for item in ops.products():
                if not item.get("active",1): continue
                if only_tracked and not item.get("track_batches",0): continue
                found.append(item)
            return found

        def receive_batch():
            if not self._authorize("manage_inventory"): return
            products=tracked_products(only_tracked=True)
            if not products:
                QMessageBox.information(w,"No batch-tracked products","Edit a product in Inventory and enable ‘Track stock by batch / expiry’, or use ‘Enable tracking / allocate existing stock’ to allocate current stock to an opening lot.")
                return
            dlg=QDialog(w); dlg.setWindowTitle("Receive inventory batch / lot"); dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumWidth(500)
            layout=QVBoxLayout(dlg); form=QFormLayout(); layout.addLayout(form)
            product=QComboBox()
            for item in products:
                unit=ops.product_unit_description(item)
                product.addItem(f"{item['sku']} — {item['name']} · {item['stock_on_hand']:,.3f} {unit} in stock",int(item["id"]))
            lot=QLineEdit(); lot.setPlaceholderText("Supplier lot / batch identifier")
            qty=QDoubleSpinBox(); qty.setDecimals(3); qty.setRange(0.001,999999999.999); qty.setValue(1); qty.setSingleStep(1)
            expiry=QLineEdit(); expiry.setPlaceholderText("YYYY-MM-DD; optional if not expiry-controlled")
            supplier=QLineEdit(); supplier.setPlaceholderText("Supplier name")
            cost=QDoubleSpinBox(); cost.setDecimals(2); cost.setRange(0,999999999999.99)
            paid=QDoubleSpinBox(); paid.setDecimals(2); paid.setRange(0,999999999999.99); paid.setPrefix(self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]+" ")
            notes=QLineEdit(); notes.setPlaceholderText("Optional traceability notes")
            def sync_cost(index=0):
                item=next((p for p in products if int(p["id"])==product.currentData()),None)
                if item: cost.setValue(float(item["cost_price"]))
            form.addRow("Product / SKU",product); form.addRow("Lot / batch number",lot); form.addRow("Quantity received",qty)
            form.addRow("Expiry date",expiry); form.addRow("Supplier",supplier); form.addRow("Unit cost",cost); form.addRow("Amount paid now",paid); form.addRow("Notes",notes)
            product.currentIndexChanged.connect(sync_cost); sync_cost()
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); layout.addWidget(buttons); buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
            if not dlg.exec(): return
            try:
                if not lot.text().strip(): raise OperationError("Enter the supplier's lot / batch number.")
                product_item=next(p for p in products if int(p["id"])==product.currentData())
                expected_total=Decimal(str(qty.value()))*Decimal(str(cost.value()))
                if Decimal(str(paid.value()))>expected_total.quantize(Decimal("0.01")): raise OperationError("Amount paid cannot exceed quantity × unit cost.")
                purchase_id=ops.receive_inventory_batch(product.currentData(),lot.text(),qty.value(),expiry.text(),supplier.text(),cost.value(),paid.value(),notes.text())
                self.store.log(self.username,"Received inventory batch",f"{product_item['sku']} · {lot.text()} · {qty.value():g} · purchase #{purchase_id}")
                refresh_table()
            except (OperationError,sqlite3.Error,ValueError,StopIteration) as exc: QMessageBox.warning(w,"Batch not received",str(exc))

        def enable_tracking():
            if not self._authorize("manage_inventory"): return
            products=tracked_products(only_tracked=False)
            products=[item for item in products if not bool(item.get("track_batches",0))]
            if not products:
                QMessageBox.information(w,"No products to configure","All active products already have batch tracking enabled."); return
            dlg=QDialog(w); dlg.setWindowTitle("Enable batch tracking"); dlg.setStyleSheet(themed_style(self.palette)); layout=QVBoxLayout(dlg); form=QFormLayout(); layout.addLayout(form)
            choice=QComboBox()
            for item in products: choice.addItem(f"{item['sku']} — {item['name']} · current stock {item['stock_on_hand']:,.3f} {ops.product_unit_description(item)}",int(item["id"]))
            lot=QLineEdit(); lot.setPlaceholderText("Required when current stock is above zero")
            expiry=QLineEdit(); expiry.setPlaceholderText("Opening lot expiry, YYYY-MM-DD")
            note=QLabel("Existing stock will be allocated to one opening lot without changing the stock ledger. For multiple legacy lots, allocate the stock with a reviewed migration before enabling this product."); note.setWordWrap(True)
            form.addRow("Product",choice); form.addRow("Opening lot number",lot); form.addRow("Opening expiry",expiry); form.addRow(note)
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); layout.addWidget(buttons); buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
            if not dlg.exec(): return
            item=next(p for p in products if int(p["id"])==choice.currentData())
            try:
                ops.enable_product_batch_tracking(item["id"],True,lot.text(),expiry.text())
                self.store.log(self.username,"Enabled batch tracking",f"{item['sku']} · opening lot {lot.text()}"); refresh_table()
            except (OperationError,sqlite3.Error,ValueError) as exc: QMessageBox.warning(w,"Tracking not enabled",str(exc))

        def update_status():
            if not self._authorize("manage_inventory"): return
            row=table.currentRow()
            if row<0: QMessageBox.information(w,"Select a lot","Select a batch / lot from the table first."); return
            batch_id=int(table.item(row,0).text()); current=table.item(row,9).text()
            dlg=QDialog(w); dlg.setWindowTitle("Update lot status"); dlg.setStyleSheet(themed_style(self.palette)); layout=QVBoxLayout(dlg); form=QFormLayout(); layout.addLayout(form)
            status=QComboBox(); status.addItems(["In stock","Quarantined","Disposed"]); status.setCurrentText(current if current in {"In stock","Quarantined","Disposed"} else "Quarantined")
            reason=QLineEdit(); reason.setPlaceholderText("Reason / audit note, required for disposal")
            form.addRow("New status",status); form.addRow("Reason",reason)
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); layout.addWidget(buttons); buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
            if not dlg.exec(): return
            if status.currentText()=="Disposed" and not reason.text().strip(): QMessageBox.warning(w,"Reason required","Enter a reason before disposing stock."); return
            try:
                ops.update_inventory_batch_status(batch_id,status.currentText(),reason.text())
                self.store.log(self.username,"Updated inventory batch status",f"#{batch_id} → {status.currentText()} · {reason.text()}"); refresh_table()
            except (OperationError,sqlite3.Error,ValueError) as exc: QMessageBox.warning(w,"Status not updated",str(exc))

        def export_csv():
            if not self._authorize("export"): return
            path,_=QFileDialog.getSaveFileName(w,"Export batch register","inventory_batches.csv","CSV files (*.csv)")
            if not path: return
            try:
                import csv
                rows=ops.inventory_batches(include_empty=True,limit=20000)
                with open(path,"w",newline="",encoding="utf-8-sig") as stream:
                    writer=csv.writer(stream); writer.writerow(["Batch ID","SKU","Product","Lot number","Expiry date","Received quantity","Remaining quantity","Unit","Supplier","Status","Source type","Source ID"])
                    for row in rows: writer.writerow([row["id"],row["sku"],row["product_name"],row["lot_number"],row["expiry_date"],row["received_quantity"],row["remaining_quantity"],row["unit"],row["supplier_name"],row["status"],row["source_type"],row["source_id"]])
                QMessageBox.information(w,"Export complete",f"Exported {len(rows)} batch records to {path}")
            except (OSError,OperationError,sqlite3.Error) as exc: QMessageBox.warning(w,"Export failed",str(exc))

        receive.clicked.connect(receive_batch); enable.clicked.connect(enable_tracking); change.clicked.connect(update_status); export.clicked.connect(export_csv); refresh.clicked.connect(refresh_table)
        refresh_table(); return w

    def appointments_page(self):
        """Purpose-built appointment booking and status workflow for service businesses."""
        ops=self.store.operations
        w,v=self.page_container("Appointments & Service Bookings","Book customers, prevent staff schedule conflicts, and move bookings through a controlled service lifecycle.")
        toolbar=QHBoxLayout(); add=QPushButton("＋ Book appointment"); add.setObjectName("primary"); day=QDateEdit(); day.setCalendarPopup(True); day.setDisplayFormat("yyyy-MM-dd")
        from PySide6.QtCore import QDate
        day.setDate(QDate.currentDate()); filter_status=QComboBox(); filter_status.addItems(["All statuses","Booked","Confirmed","Arrived","In service","Completed","No show","Cancelled"])
        refresh=QPushButton("↻ Refresh"); toolbar.addWidget(add); toolbar.addWidget(QLabel("Appointment date")); toolbar.addWidget(day); toolbar.addWidget(QLabel("Status")); toolbar.addWidget(filter_status); toolbar.addWidget(refresh); toolbar.addStretch(); v.addLayout(toolbar)
        table=QTableWidget(0,8); table.setHorizontalHeaderLabels(["ID","START","CUSTOMER","CONTACT","SERVICE","STAFF","DURATION","STATUS"])
        table.setColumnHidden(0,True); table.setSelectionBehavior(QAbstractItemView.SelectRows); table.setSelectionMode(QAbstractItemView.SingleSelection); table.setEditTriggers(QAbstractItemView.NoEditTriggers); table.setAlternatingRowColors(True); table.verticalHeader().setVisible(False); table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); table.horizontalHeader().setStretchLastSection(True); v.addWidget(table,1)
        statusbar=QHBoxLayout(); next_status=QComboBox(); next_status.addItems(["Confirmed","Arrived","In service","Completed","No show","Cancelled"]); update=QPushButton("Update selected booking status"); update.setObjectName("primary"); statusbar.addWidget(QLabel("Move selected booking to")); statusbar.addWidget(next_status); statusbar.addWidget(update); statusbar.addStretch(); v.addLayout(statusbar)
        def selected_id():
            selected=table.selectionModel().selectedRows()
            if not selected:return None
            cell=table.item(selected[0].row(),0)
            return cell.data(Qt.UserRole) if cell else None
        def refresh_table():
            try:
                status="" if filter_status.currentText()=="All statuses" else filter_status.currentText()
                rows=ops.appointments(date_from=day.date().toString("yyyy-MM-dd"),date_to=day.date().toString("yyyy-MM-dd"),status=status)
            except (OperationError,sqlite3.Error) as exc:
                QMessageBox.warning(w,"Appointments unavailable",str(exc)); rows=[]
            table.setRowCount(len(rows))
            for ri,row in enumerate(rows):
                values=[row["id"],row["starts_at"],row["customer_name"],row["customer_contact"],row["service_name"],row["staff_member"],f"{row['duration_minutes']} min",row["status"]]
                for ci,val in enumerate(values):
                    item=QTableWidgetItem(str(val))
                    if ci==0:item.setData(Qt.UserRole,int(row["id"]))
                    table.setItem(ri,ci,item)
            table.resizeRowsToContents()
        def new_appointment():
            if not self._authorize("create"): return
            dlg=QDialog(w); dlg.setWindowTitle("Book appointment"); dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumWidth(500); form=QFormLayout(dlg)
            customer=QLineEdit(); contact=QLineEdit(); service=QLineEdit(); staff=QLineEdit(); date=QDateEdit(); date.setCalendarPopup(True); date.setDisplayFormat("yyyy-MM-dd"); date.setDate(day.date()); time=QTimeEdit(); time.setDisplayFormat("HH:mm"); duration=QSpinBox(); duration.setRange(5,1440); duration.setSingleStep(15); duration.setValue(30); notes=QLineEdit()
            for label,control in [("Customer name",customer),("Contact",contact),("Service",service),("Staff member",staff),("Date",date),("Start time",time),("Duration (minutes)",duration),("Notes",notes)]:form.addRow(label,control)
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); form.addRow(buttons); buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
            if dlg.exec()!=QDialog.Accepted:return
            starts_at=f"{date.date().toString('yyyy-MM-dd')} {time.time().toString('HH:mm')}"
            try:
                ops.create_appointment(customer.text(),service.text(),starts_at,staff.text(),duration.value(),contact.text(),notes.text())
                self.store.log(self.username,"Booked appointment",f"{customer.text()} · {service.text()} · {starts_at}"); day.setDate(date.date()); refresh_table()
            except (OperationError,sqlite3.Error,ValueError) as exc:QMessageBox.warning(w,"Booking failed",str(exc))
        def update_selected():
            if not self._authorize("edit"):return
            appointment_id=selected_id()
            if appointment_id is None:QMessageBox.information(w,"Select booking","Select an appointment first.");return
            try:
                ops.update_appointment_status(int(appointment_id),next_status.currentText()); self.store.log(self.username,"Updated appointment status",f"#{appointment_id} → {next_status.currentText()}"); refresh_table()
            except (OperationError,sqlite3.Error,ValueError) as exc:QMessageBox.warning(w,"Status update failed",str(exc))
        add.clicked.connect(new_appointment); update.clicked.connect(update_selected); refresh.clicked.connect(refresh_table); day.dateChanged.connect(lambda *_:refresh_table()); filter_status.currentTextChanged.connect(lambda *_:refresh_table())
        refresh_table(); return w

    def students_page(self):
        """Dedicated student operations; this is not the legacy generic-record workspace."""
        ops=self.store.operations
        currency=self.currency_code()
        w,v=self.page_container("Students & Enrolment","Admissions, academic setup, attendance, assessments, family contact history, and fee accounts. M-Pesa entries are recorded references, not live gateway confirmations.")
        tabs=QTabWidget(); tabs.setDocumentMode(True); v.addWidget(tabs,1)

        def warn(error, title="Unable to complete action"):
            QMessageBox.warning(w,title,str(error))

        def make_dialog(title, fields, submit_label="Save"):
            dlg=QDialog(w); dlg.setWindowTitle(title); dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumWidth(480)
            layout=QVBoxLayout(dlg); form=QFormLayout(); controls={}
            for key,label,kind,default,options in fields:
                if kind=="select":
                    control=QComboBox(); control.addItems(list(options or []))
                    if default in (options or []): control.setCurrentText(default)
                elif kind=="date":
                    control=QDateEdit(); control.setCalendarPopup(True); control.setDisplayFormat("yyyy-MM-dd")
                    from PySide6.QtCore import QDate
                    control.setDate(QDate.fromString(str(default),"yyyy-MM-dd") if default else QDate.currentDate())
                elif kind=="number":
                    control=QDoubleSpinBox(); control.setRange(0,1000000000); control.setDecimals(3); control.setValue(float(default or 0))
                elif kind=="notes":
                    control=QTextEdit(); control.setPlainText(str(default or "")); control.setMinimumHeight(78)
                else:
                    control=QLineEdit(str(default or ""))
                    if kind=="password": control.setEchoMode(QLineEdit.Password)
                controls[key]=control; form.addRow(label,control)
            layout.addLayout(form); buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); layout.addWidget(buttons)
            buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
            if dlg.exec()!=QDialog.Accepted: return None
            values={}
            for key,control in controls.items():
                if isinstance(control,QComboBox): values[key]=control.currentText()
                elif isinstance(control,QDateEdit): values[key]=control.date().toString("yyyy-MM-dd")
                elif isinstance(control,QDoubleSpinBox): values[key]=control.value()
                elif isinstance(control,QTextEdit): values[key]=control.toPlainText().strip()
                else: values[key]=control.text().strip()
            return values

        def new_table(headers):
            table=QTableWidget(0,len(headers)); table.setHorizontalHeaderLabels(headers)
            table.setSelectionBehavior(QAbstractItemView.SelectRows); table.setSelectionMode(QAbstractItemView.SingleSelection)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers); table.setAlternatingRowColors(True); table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); table.horizontalHeader().setStretchLastSection(True); table.setSortingEnabled(False)
            return table

        def populate(table,rows,columns):
            table.setRowCount(len(rows))
            for r,row in enumerate(rows):
                for c,(key,label,formatter) in enumerate(columns):
                    value=formatter(row) if formatter else row[key]
                    item=QTableWidgetItem("" if value is None else str(value))
                    if c==0: item.setData(Qt.UserRole,int(row["id"]))
                    table.setItem(r,c,item)
            table.resizeRowsToContents()

        def selected_id(table):
            rows=table.selectionModel().selectedRows()
            if not rows: return None
            cell=table.item(rows[0].row(),0)
            return cell.data(Qt.UserRole) if cell else None

        # -- Students / admissions
        student_tab=QWidget(); sv=QVBoxLayout(student_tab); sv.setContentsMargins(12,12,12,12)
        sbar=QHBoxLayout(); new_student=QPushButton("＋ Register student"); new_student.setObjectName("primary")
        edit_student=QPushButton("Edit selected"); assign_class_btn=QPushButton("Assign class")
        export_students=QPushButton("Export student list (CSV)"); refresh_students_btn=QPushButton("↻ Refresh")
        for btn in (new_student,edit_student,assign_class_btn,export_students,refresh_students_btn): sbar.addWidget(btn)
        sbar.addStretch(); sv.addLayout(sbar)
        students_table=new_table(["ID","ADMISSION NO.","STUDENT NAME","CLASS / COURSE","GUARDIAN","GUARDIAN CONTACT","STATUS"])
        students_table.setColumnHidden(0,True); sv.addWidget(students_table,1)

        def refresh_students():
            try: rows=ops.students()
            except (OperationError,sqlite3.Error) as exc: warn(exc); rows=[]
            populate(students_table,rows,[("id","ID",lambda r:r["id"]),("admission_number","Admission",None),("full_name","Name",None),("class_course","Class / course",None),("guardian_name","Guardian",None),("guardian_contact","Contact",None),("status","Status",None)])
            return rows

        def student_dialog(existing=None):
            existing=dict(existing) if existing else {}
            fields=[("admission","Admission number","text",existing.get("admission_number",""),None),
                    ("name","Full name","text",existing.get("full_name",""),None),
                    ("class_course","Class / course / programme","text",existing.get("class_course",""),None),
                    ("dob","Date of birth (YYYY-MM-DD)","text",existing.get("date_of_birth",""),None),
                    ("guardian","Guardian / next of kin","text",existing.get("guardian_name",""),None),
                    ("contact","Guardian contact","text",existing.get("guardian_contact",""),None),
                    ("email","Email","text",existing.get("email",""),None)]
            if existing: fields.append(("status","Student status","select",existing.get("status","Active"),["Active","On leave","Completed","Withdrawn"]))
            values=make_dialog("Edit student" if existing else "Register student",fields)
            if not values: return
            try:
                if existing:
                    ops.update_student(existing["id"],values["admission"],values["name"],values["class_course"],values["dob"],values["guardian"],values["contact"],values["email"],values["status"])
                else:
                    ops.create_student(values["admission"],values["name"],values["class_course"],values["dob"],values["guardian"],values["contact"],values["email"])
                self.store.log(self.username,"Saved student",values["admission"]); refresh_students(); refresh_attendance_controls(); refresh_fee_accounts()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)

        def on_new_student():
            if self._authorize("create"): student_dialog()
        def on_edit_student():
            if not self._authorize("edit"): return
            sid=selected_id(students_table)
            if sid is None: QMessageBox.information(w,"Select student","Choose a student to edit."); return
            row=ops.conn.execute("SELECT * FROM academic_students WHERE id=?",(int(sid),)).fetchone()
            if row: student_dialog(row)
        def do_export_students():
            if not self._authorize("export"): return
            path,_=QFileDialog.getSaveFileName(w,"Export student list","students.csv","CSV files (*.csv)")
            if not path:return
            import csv
            try:
                with open(path,"w",newline="",encoding="utf-8-sig") as stream:
                    writer=csv.writer(stream); writer.writerow(["Admission number","Full name","Class/course","Date of birth","Guardian","Guardian contact","Email","Status"])
                    for r in ops.students(limit=10000): writer.writerow([r["admission_number"],r["full_name"],r["class_course"],r["date_of_birth"],r["guardian_name"],r["guardian_contact"],r["email"],r["status"]])
                QMessageBox.information(w,"Export complete",f"Student list exported to:\n{path}")
            except (OSError,sqlite3.Error) as exc: warn(exc,"Export failed")
        new_student.clicked.connect(on_new_student); edit_student.clicked.connect(on_edit_student); export_students.clicked.connect(do_export_students); refresh_students_btn.clicked.connect(refresh_students)
        students_table.cellDoubleClicked.connect(lambda r,c:on_edit_student()); tabs.addTab(student_tab,"Students & admissions")

        # -- Academic years, terms and class register
        setup_tab=QWidget(); uv=QVBoxLayout(setup_tab); uv.setContentsMargins(12,12,12,12)
        setup_bar=QHBoxLayout(); year_combo=QComboBox(); add_year_btn=QPushButton("＋ Add academic year"); add_year_btn.setObjectName("primary")
        edit_year_btn=QPushButton("Edit year"); add_term_btn=QPushButton("＋ Add term"); edit_term_btn=QPushButton("Edit term")
        add_class_btn=QPushButton("＋ Add class/course"); edit_class_btn=QPushButton("Edit class"); roster_btn=QPushButton("View roster")
        setup_bar.addWidget(QLabel("Academic year")); setup_bar.addWidget(year_combo,1)
        for btn in (add_year_btn,edit_year_btn,add_term_btn,edit_term_btn,add_class_btn,edit_class_btn,roster_btn): setup_bar.addWidget(btn)
        uv.addLayout(setup_bar)
        terms_table=new_table(["ID","TERM","START","END","STATUS"]); terms_table.setColumnHidden(0,True)
        classes_table=new_table(["ID","CODE","CLASS / COURSE","SECTION","HOMEROOM TEACHER","CAPACITY","STATUS"]); classes_table.setColumnHidden(0,True)
        uv.addWidget(QLabel("Terms")); uv.addWidget(terms_table,1); uv.addWidget(QLabel("Classes and courses")); uv.addWidget(classes_table,1)

        def refresh_setup_years(select_id=None):
            try: years=ops.academic_years()
            except (OperationError,sqlite3.Error) as exc: warn(exc); years=[]
            current=select_id if select_id is not None else year_combo.currentData()
            year_combo.blockSignals(True); year_combo.clear()
            for row in years: year_combo.addItem(f"{row['name']} · {row['start_date']} to {row['end_date']} · {row['status']}",int(row["id"]))
            idx=year_combo.findData(current) if current is not None else -1
            if idx<0 and year_combo.count(): idx=0
            if idx>=0: year_combo.setCurrentIndex(idx)
            year_combo.blockSignals(False); refresh_setup_tables()

        def refresh_setup_tables():
            year_id=year_combo.currentData()
            if year_id is None:
                terms_table.setRowCount(0); classes_table.setRowCount(0); return
            try:
                terms=ops.academic_terms(int(year_id)); classes=ops.academic_classes(int(year_id))
            except (OperationError,sqlite3.Error) as exc: warn(exc); terms=[]; classes=[]
            populate(terms_table,terms,[("id","ID",lambda r:r["id"]),("name","Term",None),("start_date","Start",None),("end_date","End",None),("status","Status",None)])
            populate(classes_table,classes,[("id","ID",lambda r:r["id"]),("class_code","Code",None),("name","Name",None),("section","Section",None),("homeroom_teacher","Teacher",None),("capacity","Capacity",None),("status","Status",None)])

        def add_academic_year():
            if not self._authorize("create"): return
            current_year=datetime.now().year
            values=make_dialog("Add academic year",[("name","Name","text",f"{current_year}-{current_year+1}",None),
                ("start","Start date","date",f"{current_year}-01-01",None),("end","End date","date",f"{current_year}-12-31",None),
                ("status","Status","select","Planning",["Planning","Active","Closed"])])
            if not values:return
            try:
                year_id=ops.save_academic_year(values["name"],values["start"],values["end"],values["status"])
                self.store.log(self.username,"Created academic year",values["name"]); refresh_setup_years(year_id)
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Academic year not saved")

        def edit_academic_year():
            if not self._authorize("edit"): return
            year_id=year_combo.currentData()
            row=next((dict(item) for item in ops.academic_years() if int(item["id"])==int(year_id)),None) if year_id is not None else None
            if not row: QMessageBox.information(w,"No academic year","Choose an academic year to edit."); return
            values=make_dialog("Edit academic year",[("name","Name","text",row["name"],None),("start","Start date","date",row["start_date"],None),
                ("end","End date","date",row["end_date"],None),("status","Status","select",row["status"],["Planning","Active","Closed"])])
            if not values:return
            try:
                ops.save_academic_year(values["name"],values["start"],values["end"],values["status"],int(year_id))
                self.store.log(self.username,"Updated academic year",values["name"]); refresh_setup_years(year_id)
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Academic year not saved")

        def add_academic_term():
            if not self._authorize("create"): return
            year_id=year_combo.currentData()
            if year_id is None: QMessageBox.information(w,"Set up an academic year","Create an academic year before adding terms."); return
            year=next((dict(row) for row in ops.academic_years() if int(row["id"])==int(year_id)),None)
            if not year:return
            values=make_dialog("Add term",[("name","Term name","text","Term 1",None),
                ("start","Start date","date",year["start_date"],None),("end","End date","date",year["end_date"],None),
                ("status","Status","select","Planning",["Planning","Active","Closed"])])
            if not values:return
            try:
                ops.save_academic_term(int(year_id),values["name"],values["start"],values["end"],values["status"])
                self.store.log(self.username,"Created academic term",values["name"]); refresh_setup_tables()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Term not saved")

        def edit_academic_term():
            if not self._authorize("edit"): return
            term_id=selected_id(terms_table)
            row=ops.conn.execute("SELECT * FROM academic_terms WHERE id=?",(int(term_id),)).fetchone() if term_id is not None else None
            if not row: QMessageBox.information(w,"Select a term","Choose a term to edit."); return
            values=make_dialog("Edit term",[("name","Term name","text",row["name"],None),("start","Start date","date",row["start_date"],None),
                ("end","End date","date",row["end_date"],None),("status","Status","select",row["status"],["Planning","Active","Closed"])])
            if not values:return
            try:
                ops.save_academic_term(int(year_combo.currentData()),values["name"],values["start"],values["end"],values["status"],int(term_id))
                self.store.log(self.username,"Updated academic term",values["name"]); refresh_setup_tables()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Term not saved")

        def add_academic_class():
            if not self._authorize("create"): return
            year_id=year_combo.currentData()
            if year_id is None: QMessageBox.information(w,"Set up an academic year","Create an academic year before adding classes."); return
            values=make_dialog("Add class or course",[("code","Class code","text","",None),("name","Class / course name","text","",None),
                ("section","Section","text","",None),("teacher","Homeroom teacher","text","",None),
                ("capacity","Capacity (0 = unlimited)","number",0,None)])
            if not values:return
            try:
                ops.save_academic_class(int(year_id),values["code"],values["name"],values["section"],values["teacher"],int(values["capacity"]))
                self.store.log(self.username,"Created academic class",values["code"]); refresh_setup_tables()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Class not saved")

        def edit_academic_class():
            if not self._authorize("edit"): return
            class_id=selected_id(classes_table)
            row=ops.conn.execute("SELECT * FROM academic_classes WHERE id=?",(int(class_id),)).fetchone() if class_id is not None else None
            if not row: QMessageBox.information(w,"Select a class","Choose a class or course to edit."); return
            values=make_dialog("Edit class or course",[("code","Class code","text",row["class_code"],None),("name","Class / course name","text",row["name"],None),
                ("section","Section","text",row["section"],None),("teacher","Homeroom teacher","text",row["homeroom_teacher"],None),
                ("capacity","Capacity (0 = unlimited)","number",row["capacity"],None),
                ("status","Status","select",row["status"],["Active","Archived"])])
            if not values:return
            try:
                ops.save_academic_class(int(year_combo.currentData()),values["code"],values["name"],values["section"],values["teacher"],int(values["capacity"]),values["status"],int(class_id))
                self.store.log(self.username,"Updated academic class",values["code"]); refresh_setup_tables()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Class not saved")

        def assign_selected_student_class():
            if not self._authorize("edit"):return
            student_id=selected_id(students_table)
            if student_id is None: QMessageBox.information(w,"Select a student","Choose a student to assign to a class."); return
            try: classes=ops.academic_classes(active_only=True)
            except (OperationError,sqlite3.Error) as exc: warn(exc); return
            if not classes: QMessageBox.information(w,"No classes","Create an active class in Academic setup first."); return
            labels=[f"{row['academic_year']} · {row['class_code']} · {row['name']} {row['section']} (capacity {row['capacity'] or 'unlimited'})" for row in classes]
            choice,ok=QInputDialog.getItem(w,"Assign student to class","Choose a class section:",labels,0,False)
            if not ok:return
            class_row=classes[labels.index(choice)]
            try:
                ops.assign_student_to_class(int(student_id),int(class_row["id"]))
                self.store.log(self.username,"Assigned student to class",f"{student_id} → {class_row['class_code']}")
                refresh_students(); refresh_fee_accounts(); refresh_report_choices(int(student_id))
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Student not assigned")

        def view_class_roster():
            class_id=selected_id(classes_table)
            if class_id is None: QMessageBox.information(w,"Select a class","Choose a class or course to view its roster."); return
            row=ops.conn.execute("SELECT name,section FROM academic_classes WHERE id=?",(int(class_id),)).fetchone()
            if not row:return
            try: roster=ops.academic_class_roster(int(class_id))
            except (OperationError,sqlite3.Error) as exc: warn(exc); return
            dlg=QDialog(w); dlg.setWindowTitle(f"Class roster · {row['name']} {row['section']}"); dlg.resize(850,520)
            layout=QVBoxLayout(dlg); roster_table=new_table(["ID","ADMISSION","STUDENT","DATE OF BIRTH","GUARDIAN","CONTACT","STATUS"])
            roster_table.setColumnHidden(0,True)
            populate(roster_table,roster,[("id","ID",lambda r:r["id"]),("admission_number","Admission",None),
                ("full_name","Student",None),("date_of_birth","Date of birth",None),("guardian_name","Guardian",None),
                ("guardian_contact","Contact",None),("status","Status",None)])
            layout.addWidget(roster_table,1)
            close=QPushButton("Close"); close.clicked.connect(dlg.accept); layout.addWidget(close)
            dlg.exec()

        add_year_btn.clicked.connect(add_academic_year); edit_year_btn.clicked.connect(edit_academic_year)
        add_term_btn.clicked.connect(add_academic_term); edit_term_btn.clicked.connect(edit_academic_term)
        add_class_btn.clicked.connect(add_academic_class); edit_class_btn.clicked.connect(edit_academic_class); roster_btn.clicked.connect(view_class_roster)
        assign_class_btn.clicked.connect(assign_selected_student_class)
        year_combo.currentIndexChanged.connect(lambda *_:refresh_setup_tables())
        tabs.addTab(setup_tab,"Academic setup")

        # -- Admissions pipeline
        admissions_tab=QWidget(); nv=QVBoxLayout(admissions_tab); nv.setContentsMargins(12,12,12,12)
        admission_bar=QHBoxLayout(); new_application_btn=QPushButton("＋ New application"); new_application_btn.setObjectName("primary")
        review_application_btn=QPushButton("Review / decide"); enroll_application_btn=QPushButton("Enroll accepted applicant"); refresh_applications_btn=QPushButton("↻ Refresh")
        for btn in (new_application_btn,review_application_btn,enroll_application_btn,refresh_applications_btn): admission_bar.addWidget(btn)
        admission_bar.addStretch(); nv.addLayout(admission_bar)
        admission_table=new_table(["ID","APPLICATION","APPLICANT","DESIRED CLASS","GUARDIAN","CONTACT","APPLIED","STATUS","DECISION NOTES"])
        admission_table.setColumnHidden(0,True); nv.addWidget(admission_table,1)

        def refresh_admissions():
            try: rows=ops.admission_applications()
            except (OperationError,sqlite3.Error) as exc: warn(exc); rows=[]
            populate(admission_table,rows,[("id","ID",lambda r:r["id"]),("application_number","Application",None),
                ("applicant_name","Applicant",None),("desired_class","Desired class",None),("guardian_name","Guardian",None),
                ("guardian_contact","Contact",None),("applied_on","Applied",None),("status","Status",None),("decision_notes","Notes",None)])

        def create_application():
            if not self._authorize("create"): return
            values=make_dialog("New admission application",[("number","Application number","text",f"APP-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}",None),
                ("name","Applicant full name","text","",None),("dob","Date of birth (YYYY-MM-DD)","text","",None),
                ("class","Desired class / course","text","",None),("guardian","Parent / guardian","text","",None),
                ("contact","Guardian contact","text","",None),("email","Guardian email","text","",None),
                ("date","Application date","date",datetime.now().strftime("%Y-%m-%d"),None)])
            if not values:return
            try:
                application_id=ops.create_admission_application(values["number"],values["name"],values["class"],values["dob"],values["guardian"],values["contact"],values["email"],values["date"])
                self.store.log(self.username,"Created admission application",values["number"]); refresh_admissions()
                for row_index in range(admission_table.rowCount()):
                    if admission_table.item(row_index,0).data(Qt.UserRole)==application_id: admission_table.selectRow(row_index); break
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Application not saved")

        def review_application():
            if not self._authorize("edit"): return
            application_id=selected_id(admission_table)
            if application_id is None: QMessageBox.information(w,"Select an application","Choose an applicant to review."); return
            current=admission_table.item(admission_table.currentRow(),7).text()
            states=["Submitted","Under review","Waitlisted","Accepted","Rejected","Withdrawn"]
            status,ok=QInputDialog.getItem(w,"Application decision","Status",states,max(0,states.index(current)) if current in states else 0,False)
            if not ok:return
            notes,ok=QInputDialog.getText(w,"Decision notes","Record the decision or follow-up note:")
            if not ok:return
            try:
                ops.update_admission_status(int(application_id),status,notes)
                self.store.log(self.username,"Updated admission application",f"{application_id} · {status}"); refresh_admissions()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Decision not saved")

        def enroll_applicant():
            if not self._authorize("create"): return
            application_id=selected_id(admission_table)
            if application_id is None: QMessageBox.information(w,"Select an application","Choose an accepted applicant to enroll."); return
            number,ok=QInputDialog.getText(w,"Enroll applicant","Admission number:")
            if not ok:return
            class_id=None
            try: classes=ops.academic_classes(active_only=True)
            except (OperationError,sqlite3.Error): classes=[]
            if classes:
                labels=["Keep desired class text as entered"]+[f"{row['academic_year']} · {row['class_code']} · {row['name']} {row['section']} (capacity {row['capacity'] or 'unlimited'})" for row in classes]
                choice,ok=QInputDialog.getItem(w,"Assign class","Choose a formal class section or keep the applicant's requested class:",labels,0,False)
                if not ok:return
                if choice!=labels[0]: class_id=int(classes[labels.index(choice)-1]["id"])
            try:
                student_id=ops.enroll_admission_application(int(application_id),number,class_id)
                self.store.log(self.username,"Enrolled applicant",f"{application_id} → {number}")
                refresh_admissions(); refresh_students(); refresh_attendance_controls(); refresh_fee_accounts()
                QMessageBox.information(w,"Enrollment complete",f"Student record created for admission {number}.")
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Applicant not enrolled")

        new_application_btn.clicked.connect(create_application); review_application_btn.clicked.connect(review_application)
        enroll_application_btn.clicked.connect(enroll_applicant); refresh_applications_btn.clicked.connect(refresh_admissions)
        tabs.addTab(admissions_tab,"Admissions")

        # -- Guardian directory and communication history
        guardians_tab=QWidget(); gv=QVBoxLayout(guardians_tab); gv.setContentsMargins(12,12,12,12)
        guardian_bar=QHBoxLayout(); guardian_student=QComboBox(); add_guardian_btn=QPushButton("＋ Add guardian"); add_guardian_btn.setObjectName("primary")
        edit_guardian_btn=QPushButton("Edit guardian"); primary_guardian_btn=QPushButton("Set primary"); log_contact_btn=QPushButton("Log communication"); refresh_guardians_btn=QPushButton("↻ Refresh")
        guardian_bar.addWidget(QLabel("Student")); guardian_bar.addWidget(guardian_student,1)
        for btn in (add_guardian_btn,edit_guardian_btn,primary_guardian_btn,log_contact_btn,refresh_guardians_btn): guardian_bar.addWidget(btn)
        gv.addLayout(guardian_bar)
        guardians_table=new_table(["ID","GUARDIAN","RELATIONSHIP","CONTACT","EMAIL","PRIMARY"]); guardians_table.setColumnHidden(0,True)
        communications_table=new_table(["ID","DATE","GUARDIAN","DIRECTION","CHANNEL","SUBJECT","FOLLOW-UP","NOTES"]); communications_table.setColumnHidden(0,True)
        gv.addWidget(QLabel("Linked guardians")); gv.addWidget(guardians_table,1); gv.addWidget(QLabel("Communication history")); gv.addWidget(communications_table,1)

        def refresh_guardian_students(select_id=None):
            current=select_id if select_id is not None else guardian_student.currentData()
            try: rows=ops.students(limit=10000)
            except (OperationError,sqlite3.Error): rows=[]
            guardian_student.blockSignals(True); guardian_student.clear()
            for row in rows: guardian_student.addItem(f"{row['admission_number']} · {row['full_name']}",int(row["id"]))
            index=guardian_student.findData(current) if current is not None else -1
            guardian_student.setCurrentIndex(index if index>=0 else (0 if guardian_student.count() else -1))
            guardian_student.blockSignals(False); refresh_guardian_records()

        def refresh_guardian_records():
            student_id=guardian_student.currentData()
            if student_id is None:
                guardians_table.setRowCount(0); communications_table.setRowCount(0); return
            try:
                guardians=ops.student_guardians(int(student_id)); communications=ops.guardian_communications(int(student_id))
            except (OperationError,sqlite3.Error) as exc: warn(exc); guardians=[]; communications=[]
            populate(guardians_table,guardians,[("id","ID",lambda r:r["id"]),("full_name","Name",None),("relationship","Relationship",None),
                ("contact","Contact",None),("email","Email",None),("is_primary","Primary",lambda r:"Yes" if r["is_primary"] else "No")])
            populate(communications_table,communications,[("id","ID",lambda r:r["id"]),("created_at","Date",None),
                ("guardian_name","Guardian",lambda r:r["guardian_name"] or "Student record"),("direction","Direction",None),
                ("channel","Channel",None),("subject","Subject",None),("follow_up_date","Follow-up",None),("notes","Notes",None)])

        def add_guardian():
            if not self._authorize("create"): return
            student_id=guardian_student.currentData()
            if student_id is None: QMessageBox.information(w,"No student","Register or enroll a student first."); return
            values=make_dialog("Add guardian",[("name","Full name","text","",None),("relationship","Relationship","text","Parent / guardian",None),
                ("contact","Phone","text","",None),("email","Email","text","",None),("primary","Primary contact","select","No",["No","Yes"])])
            if not values:return
            try:
                ops.save_student_guardian(int(student_id),values["name"],values["relationship"],values["contact"],values["email"],values["primary"]=="Yes")
                self.store.log(self.username,"Added student guardian",values["name"]); refresh_guardian_records()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Guardian not saved")

        def set_primary_guardian():
            if not self._authorize("edit"): return
            student_id=guardian_student.currentData(); guardian_id=selected_id(guardians_table)
            if student_id is None or guardian_id is None: QMessageBox.information(w,"Select a guardian","Choose a guardian to set as primary."); return
            row=ops.conn.execute("SELECT * FROM student_guardians WHERE id=? AND student_id=?",(int(guardian_id),int(student_id))).fetchone()
            if not row:return
            try:
                ops.save_student_guardian(int(student_id),row["full_name"],row["relationship"],row["contact"],row["email"],True,int(guardian_id))
                self.store.log(self.username,"Changed primary guardian",row["full_name"]); refresh_guardian_records(); refresh_students()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)

        def edit_guardian():
            if not self._authorize("edit"):return
            student_id=guardian_student.currentData(); guardian_id=selected_id(guardians_table)
            if student_id is None or guardian_id is None: QMessageBox.information(w,"Select a guardian","Choose a guardian to edit."); return
            row=ops.conn.execute("SELECT * FROM student_guardians WHERE id=? AND student_id=?",(int(guardian_id),int(student_id))).fetchone()
            if not row:return
            values=make_dialog("Edit guardian",[("name","Full name","text",row["full_name"],None),("relationship","Relationship","text",row["relationship"],None),
                ("contact","Phone","text",row["contact"],None),("email","Email","text",row["email"],None),
                ("primary","Primary contact","select","Yes" if row["is_primary"] else "No",["No","Yes"])])
            if not values:return
            try:
                ops.save_student_guardian(int(student_id),values["name"],values["relationship"],values["contact"],values["email"],values["primary"]=="Yes",int(guardian_id))
                self.store.log(self.username,"Updated student guardian",values["name"]); refresh_guardian_records(); refresh_students()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Guardian not updated")

        def log_communication():
            if not self._authorize("create"): return
            student_id=guardian_student.currentData()
            if student_id is None: QMessageBox.information(w,"No student","Choose a student first."); return
            guardian_id=selected_id(guardians_table)
            values=make_dialog("Log guardian communication",[("channel","Channel","select","Phone",["Phone","Email","SMS","Meeting","Letter","Other"]),
                ("direction","Direction","select","Outgoing",["Outgoing","Incoming"]),("subject","Subject","text","",None),
                ("notes","Notes","notes","",None),("follow","Follow-up date (YYYY-MM-DD)","text","",None)])
            if not values:return
            try:
                ops.log_guardian_communication(int(student_id),values["channel"],values["direction"],values["subject"],values["notes"],guardian_id,values["follow"])
                self.store.log(self.username,"Logged guardian communication",values["subject"]); refresh_guardian_records()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Communication not saved")

        add_guardian_btn.clicked.connect(add_guardian); edit_guardian_btn.clicked.connect(edit_guardian)
        primary_guardian_btn.clicked.connect(set_primary_guardian)
        log_contact_btn.clicked.connect(log_communication); refresh_guardians_btn.clicked.connect(refresh_guardian_records)
        guardian_student.currentIndexChanged.connect(lambda *_:refresh_guardian_records())
        tabs.addTab(guardians_tab,"Guardians & contact log")

        # -- Term report cards and cumulative transcripts
        reports_tab=QWidget(); rv=QVBoxLayout(reports_tab); rv.setContentsMargins(12,12,12,12)
        report_bar=QHBoxLayout(); report_student=QComboBox(); report_term=QComboBox()
        generate_report_btn=QPushButton("Generate report card"); generate_report_btn.setObjectName("primary")
        print_report_btn=QPushButton("Print report card"); print_transcript_btn=QPushButton("Print transcript"); export_transcript_btn=QPushButton("Export transcript CSV")
        report_bar.addWidget(QLabel("Student")); report_bar.addWidget(report_student,2); report_bar.addWidget(QLabel("Term")); report_bar.addWidget(report_term,2)
        for btn in (generate_report_btn,print_report_btn,print_transcript_btn,export_transcript_btn): report_bar.addWidget(btn)
        rv.addLayout(report_bar)
        report_summary=QLabel("Choose a student and term to generate a report card."); report_summary.setObjectName("academicReportSummary"); rv.addWidget(report_summary)
        report_table=new_table(["SUBJECT","ASSESSMENT","SCORE","MAXIMUM","GRADE","REMARKS"]); rv.addWidget(report_table,1)

        def refresh_report_choices(student_id=None,term_id=None):
            old_student=student_id if student_id is not None else report_student.currentData()
            old_term=term_id if term_id is not None else report_term.currentData()
            report_student.blockSignals(True); report_student.clear()
            try: students=ops.students(limit=10000)
            except (OperationError,sqlite3.Error): students=[]
            for row in students: report_student.addItem(f"{row['admission_number']} · {row['full_name']}",int(row["id"]))
            idx=report_student.findData(old_student) if old_student is not None else -1
            report_student.setCurrentIndex(idx if idx>=0 else (0 if report_student.count() else -1)); report_student.blockSignals(False)
            report_term.blockSignals(True); report_term.clear()
            try: terms=ops.academic_terms()
            except (OperationError,sqlite3.Error): terms=[]
            for row in terms: report_term.addItem(f"{row['academic_year']} · {row['name']}",int(row["id"]))
            idx=report_term.findData(old_term) if old_term is not None else -1
            report_term.setCurrentIndex(idx if idx>=0 else (0 if report_term.count() else -1)); report_term.blockSignals(False)

        def show_report_card():
            student_id=report_student.currentData(); term_id=report_term.currentData()
            if student_id is None or term_id is None:
                report_summary.setText("Create a student, academic year, and term first."); report_table.setRowCount(0); return
            try: report=ops.student_term_report(int(student_id),int(term_id))
            except (OperationError,sqlite3.Error) as exc: warn(exc); return
            average="No scored assessments" if report["average_percent"] is None else f"{report['average_percent']:.1f}%"
            counts=report["attendance"]; present=counts.get("Present",0)+counts.get("Late",0); total=sum(counts.values())
            balance=report["fees_charged"]-report["fees_paid"]
            report_summary.setText(f"{report['student']['full_name']} · {report['term']['academic_year']} / {report['term']['name']} · Average {average} · Attendance {present}/{total} · Fees balance {self.currency_code()} {balance:,.2f}")
            rows=report["results"]; report_table.setRowCount(len(rows))
            for row_index,row in enumerate(rows):
                values=[row["subject"],row["name"],row["score"] if row["score"] is not None else "Not recorded",row["max_score"],row["grade"] or "—",row["remarks"] or ""]
                for column,value in enumerate(values): report_table.setItem(row_index,column,QTableWidgetItem(str(value)))
            report_table.resizeRowsToContents()

        def print_report_card():
            if not self._authorize("print"):return
            student_id=report_student.currentData(); term_id=report_term.currentData()
            if student_id is None or term_id is None: QMessageBox.information(w,"Choose a report","Select a student and term first."); return
            title,_=render_student_report_card(self.store,int(student_id),int(term_id))
            self._print_service().print_document(w,title,lambda layout:render_student_report_card(self.store,int(student_id),int(term_id),layout)[1],f"student report card #{student_id}")

        def print_transcript():
            if not self._authorize("print"):return
            student_id=report_student.currentData()
            if student_id is None: QMessageBox.information(w,"Choose a student","Select a student first."); return
            title,_=render_student_transcript(self.store,int(student_id))
            self._print_service().print_document(w,title,lambda layout:render_student_transcript(self.store,int(student_id))[1],f"student transcript #{student_id}")

        def export_transcript():
            if not self._authorize("export"):return
            student_id=report_student.currentData()
            if student_id is None: QMessageBox.information(w,"Choose a student","Select a student first."); return
            path,_=QFileDialog.getSaveFileName(w,"Export academic transcript","transcript.csv","CSV files (*.csv)")
            if not path:return
            try:
                import csv
                with open(path,"w",newline="",encoding="utf-8-sig") as stream:
                    writer=csv.writer(stream); writer.writerow(["Term","Date","Subject","Assessment","Score","Maximum","Grade","Remarks"])
                    for row in ops.student_transcript(int(student_id)):
                        writer.writerow([row["term"],row["assessment_date"],row["subject"],row["name"],row["score"],row["max_score"],row["grade"],row["remarks"]])
                QMessageBox.information(w,"Transcript exported",f"Academic transcript exported to:\n{path}")
            except (OSError,sqlite3.Error,OperationError) as exc: warn(exc,"Transcript export failed")

        generate_report_btn.clicked.connect(show_report_card); print_report_btn.clicked.connect(print_report_card)
        print_transcript_btn.clicked.connect(print_transcript); export_transcript_btn.clicked.connect(export_transcript)
        report_student.currentIndexChanged.connect(lambda *_:show_report_card()); report_term.currentIndexChanged.connect(lambda *_:show_report_card())
        tabs.addTab(reports_tab,"Report cards & transcripts")

        # -- Attendance
        attendance_tab=QWidget(); av=QVBoxLayout(attendance_tab); av.setContentsMargins(12,12,12,12)
        att_form=QHBoxLayout(); att_date=QDateEdit(); att_date.setCalendarPopup(True); att_date.setDisplayFormat("yyyy-MM-dd")
        from PySide6.QtCore import QDate
        att_date.setDate(QDate.currentDate()); att_student=QComboBox(); att_status=QComboBox(); att_status.addItems(["Present","Absent","Late","Excused"]); att_note=QLineEdit(); att_note.setPlaceholderText("Optional attendance note")
        mark_btn=QPushButton("Save attendance"); mark_btn.setObjectName("primary")
        att_form.addWidget(QLabel("Date")); att_form.addWidget(att_date); att_form.addWidget(QLabel("Student")); att_form.addWidget(att_student,2); att_form.addWidget(att_status); att_form.addWidget(att_note,2); att_form.addWidget(mark_btn); av.addLayout(att_form)
        attendance_table=new_table(["ID","DATE","ADMISSION","STUDENT","CLASS / COURSE","STATUS","NOTE"]); attendance_table.setColumnHidden(0,True); av.addWidget(attendance_table,1)
        def refresh_attendance_controls():
            current=att_student.currentData(); att_student.clear()
            try: rows=ops.students(active_only=True)
            except (OperationError,sqlite3.Error): rows=[]
            for row in rows: att_student.addItem(f"{row['admission_number']} · {row['full_name']} · {row['class_course']}",int(row["id"]))
            if current is not None:
                ix=att_student.findData(current)
                if ix>=0: att_student.setCurrentIndex(ix)
        def refresh_attendance():
            try: rows=ops.student_attendance(att_date.date().toString("yyyy-MM-dd"))
            except (OperationError,sqlite3.Error): rows=[]
            populate(attendance_table,rows,[("id","ID",lambda r:r["id"]),("attendance_date","Date",None),("admission_number","Admission",None),("full_name","Student",None),("class_course","Class",None),("status","Status",None),("note","Note",None)])
        def save_attendance():
            sid=att_student.currentData()
            if sid is None: QMessageBox.information(w,"No students","Register a student first."); return
            try:
                ops.mark_student_attendance(int(sid),att_date.date().toString("yyyy-MM-dd"),att_status.currentText(),att_note.text())
                self.store.log(self.username,"Recorded attendance",str(att_student.currentText())); refresh_attendance()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)
        mark_btn.clicked.connect(save_attendance); att_date.dateChanged.connect(lambda *_:refresh_attendance()); tabs.addTab(attendance_tab,"Attendance")

        # -- Class timetable
        timetable_tab=QWidget(); tv=QVBoxLayout(timetable_tab); tv.setContentsMargins(12,12,12,12)
        tbar=QHBoxLayout(); add_period_btn=QPushButton("＋ Add class"); add_period_btn.setObjectName("primary")
        edit_period_btn=QPushButton("Edit selected"); cancel_period_btn=QPushButton("Cancel / restore selected")
        refresh_timetable_btn=QPushButton("↻ Refresh")
        for btn in (add_period_btn,edit_period_btn,cancel_period_btn,refresh_timetable_btn): tbar.addWidget(btn)
        tbar.addStretch(); tv.addLayout(tbar)
        timetable_table=new_table(["ID","DAY","TIME","CLASS / COURSE","SUBJECT","TEACHER","ROOM","TERM","STATUS"])
        timetable_table.setColumnHidden(0,True); tv.addWidget(timetable_table,1)

        def refresh_timetable():
            try: rows=ops.academic_timetable()
            except (OperationError,sqlite3.Error) as exc: warn(exc); rows=[]
            populate(timetable_table,rows,[
                ("id","ID",lambda r:r["id"]),("weekday","Day",None),
                ("start_time","Time",lambda r:f"{r['start_time']}–{r['end_time']}"),
                ("class_course","Class / course",None),("subject","Subject",None),
                ("teacher","Teacher",None),("room","Room",None),("term","Term",None),
                ("status","Status",None),
            ])

        def timetable_dialog(existing=None):
            existing=dict(existing) if existing else {}
            weekdays=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
            fields=[("class_course","Class / course","text",existing.get("class_course",""),None),
                    ("subject","Subject","text",existing.get("subject",""),None),
                    ("teacher","Teacher","text",existing.get("teacher",""),None),
                    ("room","Room","text",existing.get("room",""),None),
                    ("weekday","Weekday","select",existing.get("weekday","Monday"),weekdays),
                    ("start","Start time (24-hour HH:MM)","text",existing.get("start_time","08:00"),None),
                    ("end","End time (24-hour HH:MM)","text",existing.get("end_time","09:00"),None),
                    ("term","Term / semester","text",existing.get("term",""),None)]
            values=make_dialog("Edit class timetable" if existing else "Add class to timetable",fields)
            if not values:return
            status=existing.get("status","Scheduled")
            try:
                entry_id=ops.save_academic_timetable_entry(
                    values["class_course"],values["subject"],values["weekday"],values["start"],values["end"],
                    values["teacher"],values["room"],values["term"],existing.get("id"),status)
                self.store.log(self.username,"Saved timetable class",f"{values['class_course']} · {values['subject']} · {values['weekday']} {values['start']}")
                refresh_timetable()
                for row_index in range(timetable_table.rowCount()):
                    if timetable_table.item(row_index,0).data(Qt.UserRole)==entry_id:
                        timetable_table.selectRow(row_index); break
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc,"Timetable conflict or invalid entry")

        def add_timetable_entry():
            if self._authorize("create"): timetable_dialog()
        def edit_timetable_entry():
            if not self._authorize("edit"): return
            entry_id=selected_id(timetable_table)
            if entry_id is None: QMessageBox.information(w,"Select a class","Choose a timetable entry to edit."); return
            row=ops.conn.execute("SELECT * FROM academic_timetable WHERE id=?",(int(entry_id),)).fetchone()
            if row: timetable_dialog(row)
        def toggle_timetable_entry():
            if not self._authorize("edit"): return
            entry_id=selected_id(timetable_table)
            if entry_id is None: QMessageBox.information(w,"Select a class","Choose a timetable entry first."); return
            row=ops.conn.execute("SELECT * FROM academic_timetable WHERE id=?",(int(entry_id),)).fetchone()
            if not row:return
            entry=dict(row)
            entry["status"]="Scheduled" if entry["status"]=="Cancelled" else "Cancelled"
            try:
                ops.save_academic_timetable_entry(entry["class_course"],entry["subject"],entry["weekday"],entry["start_time"],entry["end_time"],entry["teacher"],entry["room"],entry["term"],entry["id"],entry["status"])
                self.store.log(self.username,"Changed timetable status",f"{entry['class_course']} · {entry['weekday']} · {entry['status']}")
                refresh_timetable()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)
        add_period_btn.clicked.connect(add_timetable_entry); edit_period_btn.clicked.connect(edit_timetable_entry)
        cancel_period_btn.clicked.connect(toggle_timetable_entry); refresh_timetable_btn.clicked.connect(refresh_timetable)
        timetable_table.cellDoubleClicked.connect(lambda r,c:edit_timetable_entry())
        tabs.addTab(timetable_tab,"Class timetable")

        # -- Assessment and results
        assess_tab=QWidget(); ev=QVBoxLayout(assess_tab); ev.setContentsMargins(12,12,12,12)
        ebar=QHBoxLayout(); create_assess_btn=QPushButton("＋ Create assessment"); create_assess_btn.setObjectName("primary"); result_btn=QPushButton("Record / update student result"); result_btn.setObjectName("primary"); ebar.addWidget(create_assess_btn); ebar.addWidget(result_btn); ebar.addStretch(); ev.addLayout(ebar)
        assessments_table=new_table(["ID","ASSESSMENT","SUBJECT","CLASS / COURSE","TERM","DATE","MAX SCORE"]); assessments_table.setColumnHidden(0,True); ev.addWidget(QLabel("Assessments")); ev.addWidget(assessments_table,2)
        results_table=new_table(["ID","ADMISSION","STUDENT","SCORE","GRADE","REMARKS"]); results_table.setColumnHidden(0,True); ev.addWidget(QLabel("Results for selected assessment")); ev.addWidget(results_table,2)
        def refresh_results():
            aid=selected_id(assessments_table)
            try: rows=ops.assessment_results(aid) if aid is not None else []
            except (OperationError,sqlite3.Error): rows=[]
            populate(results_table,rows,[("id","ID",lambda r:r["id"]),("admission_number","Admission",None),("full_name","Student",None),("score","Score",None),("grade","Grade",None),("remarks","Remarks",None)])
        def refresh_assessments(select_id=None):
            try: rows=ops.assessments()
            except (OperationError,sqlite3.Error) as exc: warn(exc); rows=[]
            populate(assessments_table,rows,[("id","ID",lambda r:r["id"]),("name","Name",None),("subject","Subject",None),("class_course","Class",None),("term","Term",None),("assessment_date","Date",None),("max_score","Max",None)])
            if select_id is not None:
                for rr in range(assessments_table.rowCount()):
                    if assessments_table.item(rr,0).data(Qt.UserRole)==select_id: assessments_table.selectRow(rr); break
            refresh_results()
        def create_assessment():
            if not self._authorize("create"): return
            class_labels=sorted({row["class_course"].strip() for row in ops.students() if row["class_course"].strip()},key=str.casefold)
            class_options=["All classes"]+class_labels
            terms=ops.academic_terms()
            term_options=[""]+list(dict.fromkeys(row["name"] for row in terms))
            fields=[("name","Assessment name","text","",None),("subject","Subject","text","",None),
                ("class","Class/course","select",class_options[0],class_options),
                ("term","Term","select",term_options[-1] if len(term_options)>1 else "",term_options),
                ("date","Assessment date","date",datetime.now().strftime("%Y-%m-%d"),None),
                ("maximum","Maximum score","number",100,None)]
            values=make_dialog("Create assessment",fields)
            if not values:return
            try:
                class_course="" if values["class"]=="All classes" else values["class"]
                aid=ops.create_assessment(values["name"],values["subject"],class_course,values["term"],values["date"],values["maximum"])
                self.store.log(self.username,"Created assessment",values["name"]); refresh_assessments(aid)
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)
        def record_result():
            if not self._authorize("edit"): return
            aid=selected_id(assessments_table)
            if aid is None: QMessageBox.information(w,"Select assessment","Choose an assessment first."); return
            assessment=ops.conn.execute("SELECT * FROM academic_assessments WHERE id=?",(int(aid),)).fetchone()
            candidates=[r for r in ops.students(active_only=True) if not assessment["class_course"].strip() or r["class_course"].strip().casefold()==assessment["class_course"].strip().casefold()]
            if not candidates: QMessageBox.information(w,"No eligible students","No active students match the assessment's class/course."); return
            dlg=QDialog(w); dlg.setWindowTitle("Record assessment result"); dlg.setStyleSheet(themed_style(self.palette)); form=QFormLayout(dlg)
            stu_combo=QComboBox()
            for student in candidates: stu_combo.addItem(f"{student['admission_number']} · {student['full_name']}",int(student["id"]))
            score=QDoubleSpinBox(); score.setRange(0,float(assessment["max_score"])); score.setDecimals(3); score.setValue(0)
            grade=QLineEdit(); remarks=QLineEdit()
            form.addRow("Student",stu_combo); form.addRow(f"Score (max {assessment['max_score']})",score); form.addRow("Grade",grade); form.addRow("Remarks",remarks)
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); form.addRow(buttons); buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
            if dlg.exec()!=QDialog.Accepted:return
            try: ops.record_assessment_result(int(aid),int(stu_combo.currentData()),score.value(),grade.text(),remarks.text()); self.store.log(self.username,"Recorded assessment result",assessment["name"]); refresh_results()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)
        create_assess_btn.clicked.connect(create_assessment); result_btn.clicked.connect(record_result); assessments_table.itemSelectionChanged.connect(refresh_results); tabs.addTab(assess_tab,"Assessments & results")

        # -- Student fee accounts tied to the same local cash ledger
        fees_tab=QWidget(); fv=QVBoxLayout(fees_tab); fv.setContentsMargins(12,12,12,12)
        fbar=QHBoxLayout(); add_charge_btn=QPushButton("＋ Add fee charge"); add_charge_btn.setObjectName("primary"); payment_btn=QPushButton("Record fee payment"); payment_btn.setObjectName("primary"); refresh_fees_btn=QPushButton("↻ Refresh"); fbar.addWidget(add_charge_btn); fbar.addWidget(payment_btn); fbar.addWidget(refresh_fees_btn); fbar.addStretch(); fv.addLayout(fbar)
        fees_table=new_table(["ID","ADMISSION","STUDENT","CLASS / COURSE","CHARGED","PAID","BALANCE"]); fees_table.setColumnHidden(0,True); fv.addWidget(fees_table,1)
        def refresh_fee_accounts():
            try: rows=ops.student_fee_accounts()
            except (OperationError,sqlite3.Error) as exc: warn(exc); rows=[]
            populated=[]
            for row in rows:
                d=dict(row); d["balance"]=round(float(d["charged"])-float(d["paid"]),2); populated.append(d)
            populate(fees_table,populated,[("id","ID",lambda r:r["id"]),("admission_number","Admission",None),("full_name","Student",None),("class_course","Class",None),("charged","Charged",lambda r:f"{currency} {float(r['charged']):,.2f}"),("paid","Paid",lambda r:f"{currency} {float(r['paid']):,.2f}"),("balance","Balance",lambda r:f"{currency} {float(r['balance']):,.2f}")])
        def choose_student_id():
            rows=ops.students(active_only=True)
            if not rows: QMessageBox.information(w,"No students","Register an active student first."); return None
            dlg=QDialog(w); dlg.setWindowTitle("Choose student"); dlg.setStyleSheet(themed_style(self.palette)); lay=QVBoxLayout(dlg); combo=QComboBox()
            for row in rows: combo.addItem(f"{row['admission_number']} · {row['full_name']} · {row['class_course']}",int(row["id"]))
            lay.addWidget(combo); buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel); lay.addWidget(buttons); buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
            if dlg.exec()!=QDialog.Accepted:return None
            return int(combo.currentData())
        def add_fee_charge():
            if not self._authorize("manage_finance"): return
            sid=choose_student_id()
            if sid is None:return
            values=make_dialog("Add student fee charge",[("description","Description","text","Tuition / fees",None),("term","Term / semester","text","",None),("amount","Amount (KES)","number",0,None),("due","Due date","date",datetime.now().strftime("%Y-%m-%d"),None)])
            if not values:return
            try: ops.create_student_fee_charge(sid,values["description"],values["amount"],values["term"],values["due"]); self.store.log(self.username,"Added student fee charge",values["description"]); refresh_fee_accounts()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)
        def record_fee_payment():
            if not self._authorize("manage_finance"):return
            sid=choose_student_id()
            if sid is None:return
            values=make_dialog("Record fee payment",[("amount","Amount received (KES)","number",0,None),("method","Method","select","Cash",["Cash","M-Pesa (record only)","Bank transfer","Card","Other"]),("reference","Payment reference / receipt no.","text","",None)])
            if not values:return
            try: ops.record_student_fee_payment(sid,values["amount"],values["method"],values["reference"]); self.store.log(self.username,"Recorded student fee payment",values["method"]); refresh_fee_accounts(); self.refresh_syncpill()
            except (OperationError,sqlite3.Error,ValueError) as exc: warn(exc)
        add_charge_btn.clicked.connect(add_fee_charge); payment_btn.clicked.connect(record_fee_payment); refresh_fees_btn.clicked.connect(refresh_fee_accounts); tabs.addTab(fees_tab,"Fee accounts")

        refresh_students(); refresh_attendance_controls(); refresh_attendance(); refresh_timetable(); refresh_assessments(); refresh_fee_accounts()
        refresh_setup_years(); refresh_admissions(); refresh_guardian_students(); refresh_report_choices(); show_report_card()
        return w

    def restaurant_page(self):
        """Purpose-built menu, recipe and kitchen-ticket workflow for food-service profiles."""
        ops=self.store.operations
        currency=self.store.setting("business_currency","KES - Kenyan Shilling").split(" - ",1)[0]
        w,v=self.page_container("Menu & Recipes","Manage sellable menu items, ingredients, recipe consumption, and the kitchen queue. Menu sales use the shared sales engine; recipe ingredients are reduced automatically.")
        tabs=QTabWidget(); tabs.setDocumentMode(True); v.addWidget(tabs,1)

        def warn(exc,title="Unable to complete restaurant action"):
            QMessageBox.warning(w,title,str(exc))

        def selected_id(table):
            rows=table.selectionModel().selectedRows()
            if not rows:return None
            cell=table.item(rows[0].row(),0)
            return cell.data(Qt.UserRole) if cell else None

        def make_table(headers):
            table=QTableWidget(0,len(headers)); table.setHorizontalHeaderLabels(headers)
            table.setSelectionBehavior(QAbstractItemView.SelectRows); table.setSelectionMode(QAbstractItemView.SingleSelection)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers); table.setAlternatingRowColors(True)
            table.verticalHeader().setVisible(False); table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); table.horizontalHeader().setStretchLastSection(True)
            return table

        # Menu catalogue
        menu_tab=QWidget(); mv=QVBoxLayout(menu_tab); mv.setContentsMargins(12,12,12,12)
        intro=QLabel("Register each dish or drink as a sellable menu item. Configure its recipe using stock-tracked ingredients; when sold, the shared transaction reduces ingredient stock and creates a kitchen ticket.")
        intro.setWordWrap(True); mv.addWidget(intro)
        bar=QHBoxLayout(); add_menu=QPushButton("＋ Add menu item"); add_menu.setObjectName("primary")
        recipe_btn=QPushButton("Configure recipe"); availability_btn=QPushButton("Toggle availability"); refresh_menu_btn=QPushButton("↻ Refresh")
        for button in (add_menu,recipe_btn,availability_btn,refresh_menu_btn):bar.addWidget(button)
        bar.addStretch(); mv.addLayout(bar)
        menu_table=make_table(["ID","SKU","MENU ITEM","CATEGORY","PRICE","KITCHEN STATION","PREP MIN","RECIPE","AVAILABLE"])
        menu_table.setColumnHidden(0,True); mv.addWidget(menu_table,1)

        def refresh_menu(select_id=None):
            try:rows=ops.restaurant_menu_items(include_unavailable=True)
            except (OperationError,sqlite3.Error) as exc:warn(exc);rows=[]
            menu_table.setRowCount(len(rows))
            for ri,row in enumerate(rows):
                vals=[row["id"],row["sku"],row["name"],row["category"],f"{currency} {float(row['selling_price']):,.2f}",row["kitchen_station"],row["prep_minutes"],"Required" if row["recipe_required"] else "Optional", "Yes" if row["available"] and row["active"] else "No"]
                for ci,val in enumerate(vals):
                    cell=QTableWidgetItem(str(val))
                    if ci==0:cell.setData(Qt.UserRole,int(row["id"]))
                    menu_table.setItem(ri,ci,cell)
                if select_id is not None and int(row["id"])==int(select_id):menu_table.selectRow(ri)
            return rows

        def create_menu_dialog():
            if not self._authorize("create"):return
            dlg=QDialog(w);dlg.setWindowTitle("Add menu item");dlg.setStyleSheet(themed_style(self.palette));form=QFormLayout(dlg)
            name=QLineEdit();name.setPlaceholderText("e.g. Chips masala")
            price=QDoubleSpinBox();price.setRange(0,1000000000);price.setDecimals(2);price.setPrefix(f"{currency} ")
            category=QLineEdit();category.setPlaceholderText("e.g. Main dishes, Hot drinks")
            sku=QLineEdit();sku.setPlaceholderText("Leave blank to generate SKU")
            station=QComboBox();station.setEditable(True);station.addItems(["Kitchen","Grill","Bar","Pastry","Cold station"])
            prep=QSpinBox();prep.setRange(0,1440);prep.setValue(10);prep.setSuffix(" min")
            require_recipe=QCheckBox("This item must have a recipe before it can be sold");require_recipe.setChecked(True)
            notes=QLineEdit();notes.setPlaceholderText("Optional preparation note")
            form.addRow("Menu item name *",name);form.addRow("Selling price *",price);form.addRow("Category",category);form.addRow("SKU",sku);form.addRow("Kitchen station",station);form.addRow("Preparation time",prep);form.addRow("Recipe control",require_recipe);form.addRow("Notes",notes)
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);form.addRow(buttons);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject)
            if dlg.exec()!=QDialog.Accepted:return
            try:
                menu_id=ops.create_menu_item(name.text(),price.value(),category.text(),sku.text(),station.currentText(),prep.value(),require_recipe.isChecked(),notes.text())
                self.store.log(self.username,"Created restaurant menu item",name.text());refresh_menu(menu_id)
            except (OperationError,sqlite3.Error,ValueError) as exc:warn(exc,"Menu item not created")

        def configure_recipe():
            menu_id=selected_id(menu_table)
            if menu_id is None:QMessageBox.information(w,"Select a menu item","Select a menu item before configuring its recipe.");return
            if not self._authorize("edit"):return
            menu=next((dict(r) for r in ops.restaurant_menu_items(True) if int(r["id"])==int(menu_id)),None)
            if not menu:return
            dlg=QDialog(w);dlg.setWindowTitle(f"Recipe · {menu['name']}");dlg.setStyleSheet(themed_style(self.palette));dlg.resize(720,520)
            layout=QVBoxLayout(dlg)
            help_label=QLabel("Recipe quantities are consumed per serving, using each ingredient's stock unit. Unit conversions are not currently configured; select the exact unit shown in Inventory.");help_label.setWordWrap(True);layout.addWidget(help_label)
            addrow=QHBoxLayout();ingredient=QComboBox();ingredient.setMinimumWidth(260);qty=QDoubleSpinBox();qty.setRange(0.001,1000000);qty.setDecimals(3);qty.setValue(1);qty.setPrefix("Qty ");add_line=QPushButton("Add ingredient");addrow.addWidget(ingredient,1);addrow.addWidget(qty);addrow.addWidget(add_line);layout.addLayout(addrow)
            recipe_table=make_table(["PRODUCT ID","INGREDIENT","QTY / SERVING","UNIT"]);recipe_table.setColumnHidden(0,True);layout.addWidget(recipe_table,1)
            remove_line=QPushButton("Remove selected ingredient");layout.addWidget(remove_line)
            lines=[]
            try:
                for row in ops.restaurant_recipe_lines(menu_id):lines.append({"ingredient_product_id":int(row["ingredient_product_id"]),"quantity_per_serving":str(row["quantity_per_serving"]),"unit":str(row["unit"]),"notes":str(row["notes"] or "")})
            except (OperationError,sqlite3.Error) as exc:warn(exc);return
            try:products=[dict(r) for r in ops.products(active_only=True) if r["track_stock"] and int(r["id"])!=int(menu["product_id"])]
            except (OperationError,sqlite3.Error) as exc:warn(exc);return
            product_map={int(p["id"]):p for p in products}
            for product in products:ingredient.addItem(f"{product['sku']} · {product['name']} · {product['unit']}",int(product["id"]))
            def refresh_lines():
                recipe_table.setRowCount(len(lines))
                for ri,line in enumerate(lines):
                    p=product_map.get(int(line["ingredient_product_id"]))
                    pname=p["name"] if p else f"Product #{line['ingredient_product_id']}"
                    for ci,val in enumerate([line["ingredient_product_id"],pname,line["quantity_per_serving"],line["unit"]]):
                        cell=QTableWidgetItem(str(val))
                        if ci==0:cell.setData(Qt.UserRole,int(line["ingredient_product_id"]))
                        recipe_table.setItem(ri,ci,cell)
            def add_ingredient():
                pid=ingredient.currentData()
                if pid is None:QMessageBox.information(dlg,"No stock ingredients","Create a stock-tracked ingredient in Inventory first.");return
                if any(int(x["ingredient_product_id"])==int(pid) for x in lines):QMessageBox.information(dlg,"Already in recipe","An ingredient can appear only once. Remove it first if you need to change its quantity.");return
                p=product_map.get(int(pid))
                if not p:return
                lines.append({"ingredient_product_id":int(pid),"quantity_per_serving":f"{qty.value():.3f}","unit":str(p["unit"]),"notes":""});refresh_lines()
            def remove_ingredient():
                rid=selected_id(recipe_table)
                if rid is None:return
                lines[:]=[line for line in lines if int(line["ingredient_product_id"])!=int(rid)];refresh_lines()
            add_line.clicked.connect(add_ingredient);remove_line.clicked.connect(remove_ingredient)
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);layout.addWidget(buttons);buttons.accepted.connect(dlg.accept);buttons.rejected.connect(dlg.reject)
            refresh_lines()
            if dlg.exec()!=QDialog.Accepted:return
            try:
                count=ops.set_restaurant_recipe(menu_id,lines);self.store.log(self.username,"Updated restaurant recipe",f"{menu['name']} · {count} ingredients");refresh_menu(menu_id)
                QMessageBox.information(w,"Recipe saved",f"Saved {count} ingredient line(s) for {menu['name']}.")
            except (OperationError,sqlite3.Error,ValueError) as exc:warn(exc,"Recipe not saved")

        def toggle_availability():
            menu_id=selected_id(menu_table)
            if menu_id is None:QMessageBox.information(w,"Select a menu item","Select a menu item first.");return
            row=next((r for r in ops.restaurant_menu_items(True) if int(r["id"])==int(menu_id)),None)
            if not row:return
            try:ops.set_menu_item_available(menu_id,not bool(row["available"]));self.store.log(self.username,"Changed menu availability",f"{row['name']} → {'available' if not row['available'] else 'unavailable'}");refresh_menu(menu_id)
            except (OperationError,sqlite3.Error,ValueError) as exc:warn(exc)
        add_menu.clicked.connect(create_menu_dialog);recipe_btn.clicked.connect(configure_recipe);availability_btn.clicked.connect(toggle_availability);refresh_menu_btn.clicked.connect(lambda:refresh_menu())
        refresh_menu();tabs.addTab(menu_tab,"Menu & recipes")

        # Kitchen tickets arise from completed POS/sales transactions and follow controlled status transitions.
        kitchen_tab=QWidget();kv=QVBoxLayout(kitchen_tab);kv.setContentsMargins(12,12,12,12)
        note=QLabel("Tickets are created automatically when a configured menu item is sold. Use this queue to move an order through preparation and service.");note.setWordWrap(True);kv.addWidget(note)
        controls=QHBoxLayout();status_filter=QComboBox();status_filter.addItems(["All","Queued","Preparing","Ready","Served","Cancelled"]);refresh_kitchen=QPushButton("↻ Refresh");controls.addWidget(QLabel("Filter"));controls.addWidget(status_filter);controls.addWidget(refresh_kitchen);controls.addStretch();kv.addLayout(controls)
        kitchen_table=make_table(["ID","SALE","ITEM","QTY","STATION","STATUS","NOTES","CREATED"]);kitchen_table.setColumnHidden(0,True);kv.addWidget(kitchen_table,1)
        movebar=QHBoxLayout();next_status=QComboBox();next_status.addItems(["Preparing","Ready","Served","Cancelled"]);kitchen_note=QLineEdit();kitchen_note.setPlaceholderText("Optional note (e.g. no onions)");move=QPushButton("Update selected ticket");move.setObjectName("primary");movebar.addWidget(QLabel("Move to"));movebar.addWidget(next_status);movebar.addWidget(kitchen_note,1);movebar.addWidget(move);kv.addLayout(movebar)
        def refresh_kitchen_table():
            state=status_filter.currentText();
            try:rows=ops.restaurant_kitchen_tickets(status="" if state=="All" else state)
            except (OperationError,sqlite3.Error) as exc:warn(exc);rows=[]
            kitchen_table.setRowCount(len(rows))
            for ri,row in enumerate(rows):
                vals=[row["id"],row["sale_number"],row["item_name"],row["quantity"],row["kitchen_station"],row["status"],row["notes"],row["created_at"]]
                for ci,val in enumerate(vals):
                    cell=QTableWidgetItem(str(val))
                    if ci==0:cell.setData(Qt.UserRole,int(row["id"]))
                    kitchen_table.setItem(ri,ci,cell)
        def update_ticket():
            ticket_id=selected_id(kitchen_table)
            if ticket_id is None:QMessageBox.information(w,"Select a kitchen ticket","Select a ticket first.");return
            if not self._authorize("edit"):return
            try:
                state=ops.update_restaurant_kitchen_ticket(ticket_id,next_status.currentText(),kitchen_note.text());self.store.log(self.username,"Updated kitchen ticket",f"#{ticket_id} → {state}");kitchen_note.clear();refresh_kitchen_table()
            except (OperationError,sqlite3.Error,ValueError) as exc:warn(exc,"Kitchen status not updated")
        refresh_kitchen.clicked.connect(refresh_kitchen_table);status_filter.currentTextChanged.connect(lambda *_:refresh_kitchen_table());move.clicked.connect(update_ticket)
        refresh_kitchen_table();tabs.addTab(kitchen_tab,"Kitchen queue")
        return w

    def records_page(self,module):
        feature=self.store.feature(module)
        raw_fields=json.loads(feature["fields_json"]) if feature else ["Name","Description","Status"]
        defs=self._field_defs(raw_fields)
        fields=[d["name"] for d in defs]
        w,v=self.page_container(module, MODULES.get(module,("◇","Custom workspace"))[1] + "  ·  Saved locally first")
        if module in ("Sales", "Purchases", "Inventory", "Finance"):
            workflow = QFrame(); workflow.setObjectName("panelAlt")
            wl = QVBoxLayout(workflow); wl.setContentsMargins(14,10,14,10); wl.setSpacing(4)
            titles = {"Sales":"Sales workflow workspace", "Purchases":"Purchasing workflow workspace", "Inventory":"Inventory operations workspace", "Finance":"Finance records workspace"}
            descriptions = {"Sales":"Keep customer, quotation, invoice and payment references in each record. Print a selected record using receipt or standard paper sizes.", "Purchases":"Record supplier, purchase reference, amounts and status; connect receiving records through matching references.", "Inventory":"Track product/SKU and quantity changes as records. For now, stock totals depend on the fields and entries you maintain.", "Finance":"Record income and expenses consistently; use Type values Income/Receipt or Expense/Payment for dashboard summaries."}
            title_label=QLabel(titles[module]); title_label.setStyleSheet("font-weight:800;font-size:11pt;")
            desc_label=QLabel(descriptions[module]); desc_label.setWordWrap(True); desc_label.setStyleSheet(f"color:{self.palette['muted']};")
            wl.addWidget(title_label); wl.addWidget(desc_label); v.addWidget(workflow)
        bar=QHBoxLayout()
        new=QPushButton("＋  New record"); new.setObjectName("primary"); new.clicked.connect(lambda:self.new_record(module,defs))
        bar.addWidget(new)
        edit=QPushButton("Edit selected"); edit.clicked.connect(lambda:self.edit_selected_record(module,table,defs)); bar.addWidget(edit)
        if self.role == "Admin":
            delete=QPushButton("Cancel selected"); delete.clicked.connect(lambda:self.delete_selected_record(module,table)); bar.addWidget(delete)
        if "export" in self.store.role_permissions(self.role):
            export=QPushButton("Export CSV"); export.clicked.connect(lambda:self.export_module_csv(module,defs)); bar.addWidget(export)
        printbtn=QPushButton("⎙  Print selected"); printbtn.clicked.connect(lambda:self.print_selected_record(module,table,defs)); bar.addWidget(printbtn)
        refresh=QPushButton("↻ Refresh"); refresh.clicked.connect(lambda:self.reload_page(module)); bar.addWidget(refresh); bar.addStretch()
        count=QLabel(f"{len(self.store.records(module))} records"); count.setStyleSheet(f"color:{self.palette['muted']}"); bar.addWidget(count)
        v.addLayout(bar)
        table=QTableWidget(); table.setColumnCount(3+len(fields)); table.setHorizontalHeaderLabels(["ID","CREATED","STATUS"]+[f["name"].upper() for f in defs])
        table.setSelectionBehavior(QAbstractItemView.SelectRows); table.setEditTriggers(QAbstractItemView.NoEditTriggers); table.setAlternatingRowColors(True); table.verticalHeader().setVisible(False); table.setSortingEnabled(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); table.horizontalHeader().setStretchLastSection(True)
        rows=self.store.records(module); table.setRowCount(len(rows))
        for ri,row in enumerate(rows):
            data=json.loads(row["data_json"]); vals=[row["id"],row["created_at"],row["status"]]+[data.get(f,"") for f in fields]
            for ci,val in enumerate(vals):
                item=QTableWidgetItem(str(val)); item.setData(Qt.UserRole,row["id"]); table.setItem(ri,ci,item)
        table.cellDoubleClicked.connect(lambda r,c:self.edit_selected_record(module,table,defs))
        v.addWidget(table,1)
        if not rows:
            empty=QLabel("No records yet. Create your first entry to get started."); empty.setStyleSheet(f"color:{self.palette['muted']};padding:4px;"); v.addWidget(empty)
        return w
    def _selected_record_id(self, table):
        rows=table.selectionModel().selectedRows()
        if not rows: return None
        item=table.item(rows[0].row(),0)
        return int(item.text()) if item else None
    def edit_selected_record(self,module,table,defs):
        if not self._authorize('edit'):
            return
        rid=self._selected_record_id(table)
        if rid is None:
            QMessageBox.information(self,"Select a record","Select a row first, or double-click a row to edit it."); return
        row=self.store.operations.conn.execute("SELECT * FROM workspace_records WHERE id=? AND module=?",(rid,module)).fetchone()
        if not row or row["status"] == "Cancelled": return
        if row["status"] == "Cancelled":
            QMessageBox.information(self,"Cancelled record","Cancelled records are read-only to preserve history.")
            return
        data=json.loads(row["data_json"])
        self.new_record(module,defs,record_id=rid,initial=data)
    def delete_selected_record(self,module,table):
        if not self._authorize('delete'):
            return
        rid=self._selected_record_id(table)
        if rid is None:
            QMessageBox.information(self,"Select a record","Select a row first."); return
        answer=QMessageBox.question(self,"Cancel record",f"Mark {module} record #{rid} as Cancelled? The record will remain in history.",QMessageBox.Yes|QMessageBox.No)
        if answer==QMessageBox.Yes:
            if self.store.cancel_record(rid,module):
                self.store.log(self.username,"Cancelled record",f"{module} #{rid}")
            self.reload_page(module)
    def export_module_csv(self,module,defs):
        if not self._authorize('export'):
            return
        path,_=QFileDialog.getSaveFileName(self,"Export records",f"{module.lower().replace(' ','_')}.csv","CSV files (*.csv)")
        if not path: return
        import csv
        rows=self.store.records(module,limit=100000)
        try:
            with open(path,"w",newline="",encoding="utf-8-sig") as f:
                writer=csv.writer(f); writer.writerow(["ID","Created","Status"]+[d["name"] for d in defs])
                for row in rows:
                    data=json.loads(row["data_json"]); writer.writerow([row["id"],row["created_at"],row["status"]]+[data.get(d["name"],"") for d in defs])
            self.store.log(self.username,"Exported CSV",f"{module}: {len(rows)} records")
            QMessageBox.information(self,"Export complete",f"Exported {len(rows)} records to:\n{path}")
        except Exception as e: QMessageBox.critical(self,"Export failed",str(e))
    def _print_service(self):
        return PrintService(self.store, themed_style(self.palette), self.username)

    def set_pos_receipt_printing(self, enabled):
        settings=load_print_settings(self.store)
        settings.auto_receipt=bool(enabled)
        save_print_settings(self.store,settings)
        self.store.log(self.username,"POS receipt printing", "Enabled" if enabled else "Disabled")

    def print_selected_record(self, module, table, defs):
        if not self._authorize('print'):
            return
        rid = self._selected_record_id(table)
        if rid is None:
            QMessageBox.information(self, "Select a record", "Select a row first, then choose Print selected.")
            return
        row = self.store.operations.conn.execute("SELECT * FROM workspace_records WHERE id=? AND module=?", (rid, module)).fetchone()
        if not row or row["status"] == "Cancelled": return
        data = json.loads(row["data_json"])
        names = [d["name"] for d in defs]
        render = lambda layout: render_record(self.store, module, rid, row["created_at"], data, names, layout)
        self._print_service().print_document(self, f"{module} #{rid}", render, f"{module} #{rid}")

    def _confirm_invoice_date(self, sale_id):
        """Let the user confirm or backdate the date shown on the printed invoice.

        The system ``created_at`` is never changed — only the printed invoice date.
        Returns False if the user cancels.
        """
        try:
            sale = self.store.operations.conn.execute(
                "SELECT sale_number, created_at, invoice_date, status FROM sales WHERE id=?",
                (int(sale_id),),
            ).fetchone()
        except Exception as exc:
            QMessageBox.warning(self, "Sale unavailable", str(exc))
            return False
        if not sale:
            QMessageBox.warning(self, "Sale unavailable", "That sale no longer exists.")
            return False
        if sale["status"] == "Cancelled":
            QMessageBox.warning(self, "Cancelled sale", "Cannot print an invoice for a cancelled sale.")
            return False

        existing = str(sale["invoice_date"] or "").strip()
        base = existing or str(sale["created_at"] or "")
        # Parse date + time from stored value (supports "YYYY-MM-DD", "YYYY-MM-DD HH:MM", ISO)
        qdate = QDate.currentDate()
        qtime = QTime.currentTime()
        try:
            cleaned = base.replace("T", " ").strip()
            dpart = cleaned[:10]
            parsed_d = QDate.fromString(dpart, "yyyy-MM-dd")
            if parsed_d.isValid():
                qdate = parsed_d
            tpart = ""
            if len(cleaned) >= 16:
                tpart = cleaned[11:16]
            elif len(cleaned) >= 13 and ":" in cleaned[11:]:
                tpart = cleaned[11:16]
            if tpart:
                parsed_t = QTime.fromString(tpart, "HH:mm")
                if parsed_t.isValid():
                    qtime = parsed_t
        except Exception:
            pass

        dlg = QDialog(self)
        dlg.setWindowTitle("Invoice date & time")
        dlg.setStyleSheet(themed_style(self.palette))
        dlg.setMinimumWidth(440)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            f"<b>{sale['sale_number']}</b><br>"
            f"System recorded: {sale['created_at']}<br><br>"
            "Choose the <b>date and time printed on the invoice</b>. "
            "You can backdate without changing the sale’s system timestamp."
        ))
        form = QFormLayout()
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        date_edit.setDisplayFormat("yyyy-MM-dd")
        date_edit.setDate(qdate)
        time_edit = QTimeEdit()
        time_edit.setDisplayFormat("HH:mm")
        time_edit.setTime(qtime)
        form.addRow("Invoice date", date_edit)
        form.addRow("Invoice time", time_edit)
        lay.addLayout(form)
        note = QLabel("Tip: set both date and time for backdated invoices. Leave as-is for a normal same-day print.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{self.palette['muted']};")
        lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Save & print")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return False
        chosen = f"{date_edit.date().toString('yyyy-MM-dd')} {time_edit.time().toString('HH:mm')}"
        try:
            self.store.operations.set_invoice_date(sale_id, chosen)
            self.store.log(self.username, "Invoice date set", f"{sale['sale_number']}: {chosen}")
        except Exception as exc:
            QMessageBox.warning(self, "Could not save invoice date", str(exc))
            return False
        return True

    def print_selected_business_document(self, document_type):
        if not self._authorize("print"): return
        table_map={"invoice":getattr(self,"sales_table",None),"quote":getattr(self,"quote_table",None),
                   "purchase_order":getattr(self,"purchase_orders_table",None),"goods_received":getattr(self,"purchase_receipts_table",None),"repair_job":getattr(self,"repair_jobs_table",None)}
        table=table_map.get(document_type)
        record_id=self._selected_table_id(table) if table else None
        if record_id is None:
            QMessageBox.information(self,"Select a document","Select a matching transaction, quotation, or order row first.")
            return
        renderers={"invoice":render_invoice,"quote":render_quote,"purchase_order":render_purchase_order,"goods_received":render_goods_received,"repair_job":render_repair_job}
        # Invoices: optional backdate before printing (saved on the sale for reprints).
        if document_type == "invoice":
            if not self._confirm_invoice_date(record_id):
                return
        try:
            title,_html=renderers[document_type](self.store,record_id)
        except (ValueError,sqlite3.Error) as exc:
            QMessageBox.warning(self,"Document unavailable",str(exc)); return
        render=lambda layout: renderers[document_type](self.store,record_id,layout)[1]
        self._print_service().print_document(self,title.title(),render,f"{document_type} #{record_id}")

    def print_sale_receipt_silent(self, sale_id):
        """Receipt for a just-completed POS sale using the saved printer/paper (no dialogs)."""
        try:
            title,_html=render_invoice(self.store,sale_id)
        except (ValueError,sqlite3.Error) as exc:
            QMessageBox.warning(self,"Receipt unavailable",str(exc)); return
        render=lambda layout: render_invoice(self.store,sale_id,layout)[1]
        self._print_service().print_silent(self,title.title(),render,f"POS sale #{sale_id}")

    def open_printer_setup(self):
        """Choose printer/paper defaults for this computer, or print a test page."""
        svc=self._print_service()
        dlg=PrintOptionsDialog(self,"Printer setup",svc.settings(),svc.style,setup_mode=True)
        if dlg.exec():
            save_print_settings(self.store,dlg.settings)
            self.store.log(self.username,"Printer setup",f"{dlg.settings.describe_paper()}; printer={dlg.settings.printer or 'default'}")
            if dlg.action=="test":
                svc.print_test_page(self,dlg.settings)
            else:
                QMessageBox.information(self,"Printer setup saved",f"Saved: {dlg.settings.describe_paper()}.\nThese choices are now the default for every document on this computer.")

    def _html_escape(self, value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")

    def reload_page(self,name):
        if name in self.pages:
            old=self.pages.pop(name); self.stack.removeWidget(old); old.deleteLater()
        self.refresh_syncpill(); self.show_page(name)
    def invalidate_page(self,name):
        if name in self.pages:
            old=self.pages.pop(name); self.stack.removeWidget(old); old.deleteLater()
    def new_record(self,module,fields,record_id=None,initial=None):
        if not self._authorize('edit' if record_id else 'create'):
            return
        defs=self._field_defs(fields); initial=initial or {}
        dlg=QDialog(self); dlg.setWindowTitle(("Edit " if record_id else "Create ")+module+" record"); dlg.setMinimumWidth(560); dlg.setMinimumHeight(300); dlg.resize(700, min(820, 300 + len(defs)*62)); dlg.setStyleSheet(themed_style(self.palette))
        v=QVBoxLayout(dlg); v.addWidget(QLabel(f"<span style='font-size:17pt;font-weight:800'>{'Edit' if record_id else 'New'} {module} record</span>"))
        v.addWidget(QLabel(f"<span style='color:{self.palette['muted']}'>This entry is stored on this device first. Required fields are marked *.</span>"))
        form=QFormLayout(); edits={}
        for d in defs:
            name=d["name"]; typ=d["type"]
            label=name+(" *" if d["required"] else "")
            if typ=="choice":
                e=QComboBox(); e.setEditable(False); e.addItems([str(x) for x in d.get("options",[]) if str(x)])
                if not e.count(): e.addItems(["Open","In Progress","Completed","On Hold"])
                ix=e.findText(str(initial.get(name,""))); e.setCurrentIndex(ix if ix>=0 else 0)
            elif typ=="date":
                e=QDateEdit(); e.setCalendarPopup(True); e.setDisplayFormat("yyyy-MM-dd")
                if initial.get(name):
                    from PySide6.QtCore import QDate
                    dt=QDate.fromString(str(initial[name]),"yyyy-MM-dd")
                    if dt.isValid(): e.setDate(dt)
                else: e.setDate(e.date().currentDate())
            elif typ=="number":
                e=QDoubleSpinBox(); e.setRange(-999999999,999999999); e.setDecimals(2); e.setValue(float(initial.get(name,0) or 0))
            elif typ=="notes":
                e=QTextEdit(); e.setMaximumHeight(90); e.setPlainText(str(initial.get(name,"")))
            else:
                e=QLineEdit(str(initial.get(name,""))); e.setPlaceholderText("Enter "+name.lower())
            form.addRow(label,e); edits[name]=(e,d)
        v.addLayout(form); buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); v.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)
        def save():
            data={}
            for name,(e,d) in edits.items():
                if isinstance(e,QComboBox): val=e.currentText().strip()
                elif isinstance(e,QDateEdit): val=e.date().toString("yyyy-MM-dd")
                elif isinstance(e,QDoubleSpinBox): val=e.value()
                elif isinstance(e,QTextEdit): val=e.toPlainText().strip()
                else: val=e.text().strip()
                if d["required"] and (val=="" or val is None):
                    QMessageBox.warning(dlg,"Required field",f"Please complete {name}."); return
                data[name]=val
            if not any(str(x).strip() for x in data.values()): QMessageBox.warning(dlg,"Missing data","Enter at least one value."); return
            if record_id:
                self.store.update_record(record_id,module,data); self.store.log(self.username,"Updated record",f"{module} #{record_id}")
            else:
                rid=self.store.create_record(module,data); self.store.log(self.username,"Created record",f"{module} #{rid}")
            dlg.accept()
        buttons.accepted.connect(save)
        if dlg.exec():
            self.pages.pop(module,None); self.show_page(module); self.refresh_syncpill()
    def module_manager(self):
        if self.role != "Admin":
            QMessageBox.warning(self, "Access denied", "Only an administrator can create or configure modules.")
            return None
        if not self._authorize('configure'):
            return None
        w,v=self.page_container("Module Manager","Adapt the workspace to your business. Enable modules or create custom data workspaces.")
        info=QLabel("Existing modules can be enabled or disabled here. Custom modules define their own fields; installing arbitrary code extensions will require a trusted plugin framework. Toggle a module, then confirm it appears (or disappears) in the left sidebar.")
        info.setWordWrap(True); info.setStyleSheet(f"color:{self.palette['muted']};background:{self.palette['panel2']};padding:14px;border-radius:10px;"); v.addWidget(info)
        for row in self.store.features():
            card=QFrame(); card.setObjectName("panel"); card.setMinimumHeight(76); h=QHBoxLayout(card); h.setContentsMargins(15,12,15,12); h.setSpacing(12)
            icon = MODULES.get(row["name"], ("◇", ""))[0]
            h.addWidget(QLabel(f"<span style='font-size:17pt;color:{self.palette['purple']}'>{icon}</span>"))
            labels=QVBoxLayout(); labels.setContentsMargins(0,2,0,2); labels.setSpacing(5)
            title_label=QLabel(f"<b>{row['name']}</b>"); title_label.setMinimumHeight(22)
            desc_label=QLabel(f"<span style='color:{self.palette['muted']}'>{row['description'] or 'Custom workspace'}</span>")
            desc_label.setWordWrap(True); desc_label.setMinimumHeight(20)
            labels.addWidget(title_label); labels.addWidget(desc_label); h.addLayout(labels,1)
            custom=QLabel("CUSTOM MODULE" if row["custom"] else "CORE MODULE")
            custom.setStyleSheet(f"color:{self.palette['muted']};font-size:8pt;"); h.addWidget(custom)
            # Explicit toggle: block signals while applying the DB state so the
            # initial setChecked does not fire toggle_feature. Visual state is
            # driven only by the current features row.
            is_on = bool(row["enabled"])
            cb = QCheckBox("On" if is_on else "Off")
            cb.setChecked(is_on)
            cb.blockSignals(True)
            cb.setChecked(is_on)
            cb.blockSignals(False)
            if is_on:
                cb.setStyleSheet(
                    f"QCheckBox {{ color:{self.palette['blue']}; font-weight:700; }}"
                    f"QCheckBox::indicator {{ width:22px; height:22px; border:2px solid {self.palette['blue']}; "
                    f"background:{self.palette['blue']}; border-radius:6px; }}"
                )
            else:
                cb.setStyleSheet(
                    f"QCheckBox {{ color:{self.palette['muted']}; font-weight:600; }}"
                    f"QCheckBox::indicator {{ width:22px; height:22px; border:2px solid {self.palette['border']}; "
                    f"background:{self.palette['panel2']}; border-radius:6px; }}"
                )
            module_name = row["name"]
            cb.stateChanged.connect(lambda state, n=module_name: self.toggle_feature(n, state == Qt.Checked))
            h.addWidget(cb)
            if row["custom"]:
                edit=QPushButton("Edit"); edit.clicked.connect(lambda checked=False, n=module_name: self.edit_custom_module(n)); h.addWidget(edit)
            v.addWidget(card)
        add=QPushButton("＋  Create custom module"); add.setObjectName("primary"); add.clicked.connect(self.create_module); v.addWidget(add,0,Qt.AlignLeft); v.addStretch()
        return w
    def toggle_feature(self, name, enabled):
        if self.role != "Admin":
            QMessageBox.warning(self, "Access denied", "Only an administrator can configure modules.")
            return
        if not self._authorize('configure'):
            return
        # POS high-level switch must stay aligned with the Module Manager toggle.
        if name == "Point of Sale":
            try:
                self.store.set_setting("pos_enabled", "1" if enabled else "0")
            except Exception:
                pass
        try:
            self.store.set_enabled(name, enabled)
        except ValueError as exc:
            QMessageBox.warning(self, "Module change blocked", str(exc))
            self.reload_page("Module Manager")
            return
        except PermissionError as exc:
            QMessageBox.warning(self, "Access denied", str(exc))
            self.reload_page("Module Manager")
            return
        self.store.log(self.username, "Feature changed", f"{name}: {'enabled' if enabled else 'disabled'}")
        # Full rebuild so sidebar and Module Manager both reflect the new state.
        self.pages.clear()
        self.rebuild_nav()
        self.show_page("Module Manager")
        self.refresh_syncpill()
    def rebuild_nav(self):
        for i in reversed(range(self.navlay.count())):
            item=self.navlay.itemAt(i); widget=item.widget()
            if widget: widget.deleteLater()
        self.nav.clear()
        self.add_nav_section("WORKSPACE", self.workspace_navigation())
        if self.role == "Admin":
            self.add_nav_section("ADMINISTRATION",["User Management","Role Permissions","Module Manager","Feature Store","Integrations","System Settings","Database and Backup"])
        self.navlay.addStretch()
    def edit_custom_module(self,name):
        if self.role != "Admin":
            QMessageBox.warning(self, "Access denied", "Only an administrator can edit custom modules.")
            return
        if not self._authorize("configure"): return
        row=self.store.feature(name)
        if not row or not row["custom"]:
            QMessageBox.warning(self,"Cannot edit module","Only custom modules can be edited with this form."); return
        dlg=QDialog(self); dlg.setWindowTitle(f"Edit custom module · {name}"); dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumWidth(650)
        v=QVBoxLayout(dlg); v.addWidget(QLabel(f"<span style='font-size:16pt;font-weight:800'>Edit {name}</span>"))
        desc=QLineEdit(row["description"] or ""); desc.setMaxLength(200)
        fields=QTextEdit(); fields.setMinimumHeight(190)
        definitions=json.loads(row["fields_json"] or "[]")
        field_lines=[]
        for field in definitions:
            parts=[field["name"],field.get("type","text"),"required" if field.get("required") else "optional"]
            if field.get("type")=="choice": parts.append(",".join(field.get("options",[])))
            field_lines.append(" | ".join(parts))
        fields.setPlainText("\n".join(field_lines))
        form=QFormLayout(); form.addRow("Module purpose",desc); form.addRow("Field definitions",fields); v.addLayout(form)
        existing=self.store.workspace_conn.execute("SELECT COUNT(*) FROM workspace_records WHERE module=?",(name,)).fetchone()[0]
        note=QLabel("Saved records exist: existing fields must be preserved; you may add optional fields and extend choice options. Remove/change fields only after the module is empty." if existing else "No saved records exist, so this module's fields can be revised.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{self.palette['muted']};padding:8px;background:{self.palette['panel2']};border-radius:8px;"); v.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); v.addWidget(buttons); buttons.rejected.connect(dlg.reject)
        def save():
            lines=[line.strip() for line in fields.toPlainText().splitlines() if line.strip()]
            if len(lines)>20: QMessageBox.warning(dlg,"Too many fields","A custom module can contain up to 20 fields."); return
            defs=[]
            for line in lines:
                parts=[part.strip() for part in line.split("|")]
                fname=parts[0][:60]; kind=(parts[1].lower() if len(parts)>1 and parts[1] else "text")
                if kind not in ("text","number","date","choice","notes"):
                    QMessageBox.warning(dlg,"Invalid field type",f"Unsupported type '{kind}' for {fname}."); return
                required=len(parts)>2 and "required" in parts[2].lower()
                options=[item.strip()[:60] for item in (parts[3].split(",") if len(parts)>3 else []) if item.strip()][:30] if kind=="choice" else []
                defs.append({"name":fname,"type":kind,"required":required,"options":options})
            try:
                self.store.update_custom_feature(name,desc.text(),defs)
                self.store.log(self.username,"Updated custom module",name)
                dlg.accept()
            except ValueError as exc:
                QMessageBox.warning(dlg,"Module update blocked",str(exc))
        buttons.accepted.connect(save)
        if dlg.exec():
            self.pages.clear(); self.rebuild_nav(); self.show_page("Module Manager"); self.refresh_syncpill()

    def create_module(self):
        if self.role != "Admin":
            QMessageBox.warning(self, "Access denied", "Only an administrator can create custom modules.")
            return
        if not self._authorize('configure'):
            return
        dlg=QDialog(self); dlg.setWindowTitle("No-code module builder"); dlg.setStyleSheet(themed_style(self.palette)); dlg.setMinimumWidth(650)
        v=QVBoxLayout(dlg); v.addWidget(QLabel("<span style='font-size:18pt;font-weight:800'>Build a custom workspace</span>"))
        v.addWidget(QLabel("Create a module with typed fields and validation—no Python editing required."))
        form=QFormLayout(); name=QLineEdit(); desc=QLineEdit(); fields=QTextEdit()
        fields.setPlaceholderText("One field per line: Field name | type | required | options (for choice)\nCustomer | text | required\nAmount | number | required\nDue date | date\nStatus | choice | required | Open,In Progress,Completed,On Hold\nNotes | notes")
        fields.setMinimumHeight(175)
        form.addRow("Module name",name); form.addRow("Purpose",desc); form.addRow("Field definitions",fields); v.addLayout(form)
        helptext=QLabel("Types: text, number, date, choice, notes. Add 'required' to enforce a value. For choice fields, supply comma-separated options in column four. Maximum 20 fields.")
        helptext.setWordWrap(True); helptext.setStyleSheet(f"color:{self.palette['muted']};padding:8px;background:{self.palette['panel2']};border-radius:8px;"); v.addWidget(helptext)
        bb=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); v.addWidget(bb); bb.rejected.connect(dlg.reject)
        created_name = {"value": ""}
        def save():
            n=name.text().strip()
            if len(n)<2 or len(n)>40: QMessageBox.warning(dlg,"Invalid name","Module name must be 2–40 characters."); return
            defs=[]
            raw=fields.toPlainText().strip()
            lines=[line.strip() for line in raw.splitlines() if line.strip()]
            if not lines: lines=["Name | text | required","Description | notes"]
            if len(lines) > 20:
                QMessageBox.warning(dlg,"Too many fields","A custom module can contain up to 20 fields. Remove extra lines and try again."); return
            for line in lines:
                parts=[x.strip() for x in line.split("|")]
                fname=parts[0][:60]
                if len(fname)<1: continue
                typ=(parts[1].lower() if len(parts)>1 and parts[1] else "text")
                if typ not in ("text","number","date","choice","notes"):
                    QMessageBox.warning(dlg,"Invalid field type",f"Unsupported type '{typ}' for {fname}. Use text, number, date, choice, or notes."); return
                required=len(parts)>2 and "required" in parts[2].lower()
                options=[]
                if typ=="choice":
                    options=[x.strip()[:60] for x in (parts[3].split(",") if len(parts)>3 else ["Open","In Progress","Completed"]) if x.strip()][:30]
                    if not options: options=["Open","In Progress","Completed"]
                defs.append({"name":fname,"type":typ,"required":required,"options":options})
            if not defs: QMessageBox.warning(dlg,"No fields","Add at least one field definition."); return
            if len({x['name'].lower() for x in defs})!=len(defs): QMessageBox.warning(dlg,"Duplicate fields","Each field name must be unique."); return
            try:
                self.store.add_feature(n, desc.text().strip()[:200], defs)
                self.store.log(self.username, "Created module", n)
                created_name["value"] = n
                dlg.accept()
            except sqlite3.IntegrityError:
                QMessageBox.warning(dlg, "Already exists", "That module name already exists.")
            except ValueError as exc:
                QMessageBox.warning(dlg, "Invalid module definition", str(exc))
        bb.accepted.connect(save)
        if dlg.exec() and created_name["value"]:
            self.pages.clear()
            self.rebuild_nav()
            self.show_page("Module Manager")
            self.refresh_syncpill()
            QMessageBox.information(
                self,
                "Module created",
                f"“{created_name['value']}” is enabled and should now appear in the left sidebar and in this list. "
                "Open it to add records."
            )
    def user_management(self):
        if not self._authorize('manage_users'):
            return
        subtitle = "Manage Staff and Manager accounts." if self.role != "Admin" else "Manage local accounts, status and credentials. Administrator accounts are only visible to Admin."
        w,v=self.page_container("User Management", subtitle)
        actions=QHBoxLayout()
        btn=QPushButton("＋ Add user"); btn.setObjectName("primary"); btn.clicked.connect(self.add_user); actions.addWidget(btn)
        toggle=QPushButton("Enable / disable selected"); toggle.clicked.connect(self.toggle_selected_user); actions.addWidget(toggle)
        reset=QPushButton("Reset selected password"); reset.clicked.connect(self.reset_selected_user_password); actions.addWidget(reset)
        delete=QPushButton("Delete selected permanently"); delete.clicked.connect(self.delete_selected_user); actions.addWidget(delete)
        actions.addStretch(); v.addLayout(actions)
        table=self._table(["ID","USERNAME","DISPLAY NAME","EMPLOYEE CODE","ROLE","ACTIVE"])
        # Managers never see Admin accounts — only Admin can view/manage administrators
        if self.role == "Admin":
            rows=self.store.conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        else:
            rows=self.store.conn.execute("SELECT * FROM users WHERE role<>'Admin' ORDER BY id").fetchall()
        table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            code = r["employee_code"] if "employee_code" in r.keys() else ""
            for j,val in enumerate([r["id"], r["username"], r["display_name"], code or "—", r["role"], "Yes" if r["active"] else "No"]):
                table.setItem(i,j,QTableWidgetItem(str(val)))
        table.setColumnHidden(0,True)
        v.addWidget(table,1)
        self.user_table=table
        return w
    def add_user(self):
        if not self._authorize('manage_users'):
            return
        dlg=QDialog(self); dlg.setWindowTitle("Add user"); dlg.setStyleSheet(themed_style(self.palette)); form=QFormLayout(dlg)
        user=QLineEdit(); disp=QLineEdit(); pw=QLineEdit(); pw.setEchoMode(QLineEdit.Password); role=QComboBox()
        # Admin is the only true super-user. Managers may only create Manager or Staff (plus custom non-Admin roles).
        if self.role == "Admin":
            role.addItems(["Staff", "Manager"] + self.store.custom_roles() + ["Admin"])
        else:
            role.addItems(["Staff", "Manager"] + [r for r in self.store.custom_roles() if str(r).casefold() != "admin"])
        form.addRow("Username",user); form.addRow("Display name",disp); form.addRow("Temporary password",pw); form.addRow("Role",role)
        bb=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); form.addRow(bb); bb.rejected.connect(dlg.reject)
        def save():
            username=user.text().strip(); display_name=disp.text().strip(); password=pw.text()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}",username) or not display_name or len(display_name)>100 or len(password)<6:
                QMessageBox.warning(dlg,"Check details","Use a 3–40 character username (letters, numbers, dot, underscore or dash), a display name, and a password of at least 6 characters."); return
            try:
                chosen_role = role.currentText()
                if chosen_role == "Admin" and self.role != "Admin":
                    QMessageBox.warning(dlg, "Not allowed", "Only an administrator can create another administrator.")
                    return
                if self.role != "Admin" and chosen_role not in (["Staff", "Manager"] + list(self.store.custom_roles())):
                    QMessageBox.warning(dlg, "Not allowed", "Managers can only create Staff or Manager accounts.")
                    return
                code = self.store.next_employee_code()
                with self.store.conn:
                    self.store.conn.execute(
                        "INSERT INTO users(username,display_name,role,password_hash,active,created_at,employee_code) VALUES(?,?,?,?,1,?,?)",
                        (username, display_name, chosen_role, hash_password(password), stamp(), code))
                self.store.log(self.username, "Added user", f"{username} · {chosen_role} · {code}")
                QMessageBox.information(dlg, "User created", f"{display_name} created.\nEmployee code: {code}")
                dlg.accept()
            except sqlite3.IntegrityError:
                QMessageBox.warning(dlg, "Duplicate username", "That username already exists.")
        bb.accepted.connect(save)
        if dlg.exec(): self.pages.pop("User Management", None); self.show_page("User Management")
    def _selected_user(self):
        table=getattr(self,"user_table",None)
        if table is None or table.currentRow()<0:
            QMessageBox.information(self,"Select a user","Select a user row first.")
            return None
        item=table.item(table.currentRow(),0)
        if not item: return None
        row = self.store.conn.execute("SELECT * FROM users WHERE id=?",(int(item.text()),)).fetchone()
        if row and row["role"] == "Admin" and self.role != "Admin":
            QMessageBox.warning(self, "Not allowed", "Administrator accounts are only visible to Admin.")
            return None
        return row
    def toggle_selected_user(self):
        if not self._authorize("manage_users"): return
        user=self._selected_user()
        if not user: return
        if user["role"]=="Admin" and self.role != "Admin":
            QMessageBox.warning(self,"Not allowed","Only an administrator can change administrator accounts.")
            return
        if user["username"].casefold()==self.username.casefold() and user["active"]:
            QMessageBox.warning(self,"Cannot disable current account","Sign in as another administrator before disabling this account.")
            return
        if user["role"]=="Admin" and user["active"]:
            active_admins=self.store.conn.execute("SELECT COUNT(*) FROM users WHERE role='Admin' AND active=1").fetchone()[0]
            if active_admins<=1:
                QMessageBox.warning(self,"Keep an administrator","At least one active administrator account is required.")
                return
        next_active=not bool(user["active"])
        answer=QMessageBox.question(self,"Confirm account status",f"{('Enable' if next_active else 'Disable')} account {user['username']}?",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes: return
        with self.store.conn:
            self.store.conn.execute("UPDATE users SET active=? WHERE id=?",(int(next_active),user["id"]))
        self.store.log(self.username,"Changed user status",f"{user['username']}: {'active' if next_active else 'disabled'}")
        self.pages.pop("User Management",None); self.show_page("User Management")
    def reset_selected_user_password(self):
        if not self._authorize("manage_users"): return
        user=self._selected_user()
        if not user: return
        if user["role"]=="Admin" and self.role != "Admin":
            QMessageBox.warning(self,"Not allowed","Only an administrator can reset an administrator password.")
            return
        password,ok=QInputDialog.getText(self,"Reset password",f"Enter a temporary password for {user['username']} (6 or more characters):",QLineEdit.Password)
        if not ok: return
        if len(password)<6:
            QMessageBox.warning(self,"Password too short","Use a password with at least 6 characters.")
            return
        with self.store.conn:
            self.store.conn.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_password(password),user["id"]))
        self.store.log(self.username,"Reset user password",user["username"])
        QMessageBox.information(self,"Password reset","The new password was saved. Share it with the account owner through a secure channel.")

    def delete_selected_user(self):
        """Permanently remove a user account. Admin accounts are Admin-only; last Admin cannot be removed."""
        if not self._authorize("manage_users"):
            return
        user = self._selected_user()
        if not user:
            return
        if user["role"] == "Admin" and self.role != "Admin":
            QMessageBox.warning(self, "Not allowed", "Only an administrator can delete administrator accounts.")
            return
        if user["username"].casefold() == self.username.casefold():
            QMessageBox.warning(self, "Cannot delete current account", "You cannot delete the account you are signed in with. Sign in as another administrator first.")
            return
        if user["role"] == "Admin":
            active_admins = self.store.conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='Admin' AND active=1"
            ).fetchone()[0]
            # Count all Admin rows — do not allow removing the last Admin account at all
            total_admins = self.store.conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='Admin'"
            ).fetchone()[0]
            if total_admins <= 1:
                QMessageBox.warning(self, "Keep an administrator", "At least one administrator account must remain on this system.")
                return
        answer = QMessageBox.question(
            self,
            "Delete user permanently?",
            f"Permanently delete account “{user['username']}” ({user['role']})?\n\n"
            "This cannot be undone. Sales and activity history that mention this username stay in the log, but the login is removed.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        with self.store.conn:
            self.store.conn.execute("DELETE FROM users WHERE id=?", (int(user["id"]),))
        self.store.log(self.username, "Deleted user", f"{user['username']} · {user['role']}")
        QMessageBox.information(self, "User deleted", f"Account “{user['username']}” was permanently removed.")
        self.pages.pop("User Management", None)
        self.show_page("User Management")
    def role_permissions(self):
        if not self._authorize("configure"):
            return
        role_names=self.store.assignable_roles()
        w,v=self.page_container("Role Permissions","Create focused staff roles and assign their actions, modules, and dashboard widgets. Protected administrator powers remain unavailable to every custom role.")
        toolbar=QHBoxLayout()
        add=QPushButton("＋ Create custom role"); add.setObjectName("primary")
        def create_role():
            dialog=QDialog(self); dialog.setWindowTitle("Create custom role"); dialog.setStyleSheet(themed_style(self.palette))
            form=QFormLayout(dialog); name=QLineEdit(); name.setPlaceholderText("e.g. Technician")
            template=QComboBox(); template.addItems(["Staff","Manager"])
            form.addRow("Role name",name); form.addRow("Starting grants",template)
            buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); form.addRow(buttons)
            buttons.rejected.connect(dialog.reject); buttons.accepted.connect(dialog.accept)
            if not dialog.exec(): return
            try:
                created=self.store.create_custom_role(name.text(),template.currentText())
                self.store.log(self.username,"Created role",created)
                self.pages.pop("Role Permissions",None); self.show_page("Role Permissions")
            except ValueError as exc:
                QMessageBox.warning(self,"Could not create role",str(exc))
        add.clicked.connect(create_role); toolbar.addWidget(add)
        remove=QPushButton("Remove unused custom role")
        def delete_role():
            roles=self.store.custom_roles()
            if not roles:
                QMessageBox.information(self,"No custom roles","Create a custom role first."); return
            role,ok=QInputDialog.getItem(self,"Remove custom role","Select an unused custom role:",roles,0,False)
            if not ok: return
            answer=QMessageBox.question(self,"Confirm role removal",f"Remove role {role}? Accounts must be reassigned first.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
            if answer!=QMessageBox.Yes: return
            try:
                self.store.delete_custom_role(role); self.store.log(self.username,"Removed role",role)
                self.pages.pop("Role Permissions",None); self.show_page("Role Permissions")
            except ValueError as exc:
                QMessageBox.warning(self,"Role still in use",str(exc))
        remove.clicked.connect(delete_role); toolbar.addWidget(remove); toolbar.addStretch(); v.addLayout(toolbar)
        editable=[cap for cap in ROLE_CAPABILITIES if cap not in PROTECTED_CAPABILITIES]
        table=QTableWidget(len(role_names),len(editable)+1)
        table.setHorizontalHeaderLabels(["ROLE"]+[cap.replace("_"," ").title() for cap in editable])
        table.verticalHeader().setVisible(False); table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        editors={}
        for row,role in enumerate(role_names):
            table.setItem(row,0,QTableWidgetItem(role))
            current=self.store.role_permissions(role)
            for col,cap in enumerate(editable,1):
                check=QCheckBox(); check.setChecked(cap in current); check.setToolTip(f"Allow {role} to {cap.replace('_',' ')}")
                table.setCellWidget(row,col,check); editors[(role,cap)]=check
        v.addWidget(QLabel("Allowed actions")); v.addWidget(table)
        v.addWidget(QLabel("Module access"))
        module_names=[row["name"] for row in self.store.features()]
        module_table=QTableWidget(len(role_names),len(module_names)+1)
        module_table.setHorizontalHeaderLabels(["ROLE"]+module_names)
        module_table.verticalHeader().setVisible(False); module_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        module_editors={}
        for row,role in enumerate(role_names):
            module_table.setItem(row,0,QTableWidgetItem(role))
            assigned=self.store.role_modules(role)
            for col,module in enumerate(module_names,1):
                box=QCheckBox(); box.setChecked(module in assigned); box.setToolTip(f"Allow {role} to open {module}")
                module_table.setCellWidget(row,col,box); module_editors[(role,module)]=box
        v.addWidget(module_table)
        v.addWidget(QLabel("Dashboard widgets"))
        widget_names=list(DASHBOARD_WIDGETS); dashboard_roles=["Admin"]+role_names
        widget_table=QTableWidget(len(dashboard_roles),len(widget_names)+1)
        widget_table.setHorizontalHeaderLabels(["ROLE"]+[DASHBOARD_WIDGETS[name] for name in widget_names])
        widget_table.verticalHeader().setVisible(False); widget_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        widget_editors={}
        for row,role in enumerate(dashboard_roles):
            widget_table.setItem(row,0,QTableWidgetItem(role)); selected=self.store.dashboard_widgets(role)
            for col,widget in enumerate(widget_names,1):
                box=QCheckBox(); box.setChecked(widget in selected); box.setToolTip(f"Show {DASHBOARD_WIDGETS[widget]} for {role}")
                widget_table.setCellWidget(row,col,box); widget_editors[(role,widget)]=box
        v.addWidget(widget_table)
        note=QLabel("Custom roles start with Staff-level grants. You can add only non-privileged actions. Configure, user administration, backup, synchronization, and destructive deletion are reserved for the built-in Admin role. A role in use cannot be removed.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{self.palette['muted']};padding:12px;"); v.addWidget(note)
        save=QPushButton("Save role permissions"); save.setObjectName("primary")
        def save_permissions():
            try:
                for role in role_names:
                    self.store.set_role_permissions(role,[cap for cap in editable if editors[(role,cap)].isChecked()])
                    self.store.set_role_modules(role,[module for module in module_names if module_editors[(role,module)].isChecked()])
                for role in dashboard_roles:
                    self.store.set_dashboard_widgets(role,[widget for widget in widget_names if widget_editors[(role,widget)].isChecked()])
                self.pages.clear(); self.rebuild_nav()
                self.store.log(self.username,"Updated role permissions",", ".join(role_names))
                QMessageBox.information(self,"Saved","Role actions, module access, and dashboard widgets were saved locally.")
            except (TypeError,ValueError) as exc:
                QMessageBox.warning(self,"Could not save permissions",str(exc))
        save.clicked.connect(save_permissions); v.addWidget(save,0,Qt.AlignLeft); v.addStretch(); return w
    def feature_store(self):
        if self.role != "Admin":
            QMessageBox.warning(self, "Access denied", "Only an administrator can manage extensibility settings.")
            return None
        if not self._authorize('configure'):
            return
        w,v=self.page_container("Feature Store","A future catalog for trusted, signed add-ons.")
        p=self.panel(); l=QVBoxLayout(p); l.setContentsMargins(20,20,20,20)
        l.addWidget(QLabel("<span style='font-size:16pt;font-weight:800'>Extensible by design</span>"))
        l.addWidget(QLabel("The current build supports configurable custom data modules. A secure feature store needs a package format, signing, version compatibility, permissions, migrations, and rollback before third-party code can be installed."))
        l.addWidget(QLabel(f"<span style='color:{self.palette['cyan']}'>Planned:</span> versioned modules · permission manifests · signed packages · one-click upgrades · rollback"))
        v.addWidget(p); v.addStretch(); return w
    def settings_page(self):
        if not self._authorize('configure'):
            return
        w,v=self.page_container("Business & System Settings","Personalize the ERP for your organization. Settings are saved on this computer.")
        business=self.panel(); bf=QFormLayout(business); bf.setContentsMargins(24,24,24,24); bf.setSpacing(16); bf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        company=QLineEdit(self.store.setting("business_name", "")); company.setPlaceholderText("e.g. Acme Enterprises Ltd")
        tagline=QLineEdit(self.store.setting("business_tagline", "")); tagline.setPlaceholderText("A short description of your business")
        address=QLineEdit(self.store.setting("business_address", "")); phone=QLineEdit(self.store.setting("business_phone", ""))
        email=QLineEdit(self.store.setting("business_email", "")); tax=QLineEdit(self.store.setting("business_tax_id", ""))
        website=QLineEdit(self.store.setting("business_website", "")); website.setPlaceholderText("https://www.example.com")
        website.setToolTip("Optional public shop or business website; shown on invoices and receipts.")
        tax_label=QLineEdit(self.store.setting("business_tax_label","Tax")); tax_label.setMaxLength(60)
        tax_inclusive=QCheckBox("Prices already include tax (VAT-inclusive — normal shelf prices in Kenya)"); tax_inclusive.setChecked(self.store.setting("business_tax_inclusive","0")=="1")
        tax_rate=QDoubleSpinBox(); tax_rate.setRange(0,100); tax_rate.setDecimals(2); tax_rate.setSuffix(" %")
        try: tax_rate.setValue(float(self.store.setting("business_tax_rate","0.00")))
        except ValueError: tax_rate.setValue(0)
        currency=QComboBox(); currency.addItems(["KES - Kenyan Shilling","USD - US Dollar","TZS - Tanzanian Shilling","UGX - Ugandan Shilling","GBP - British Pound","EUR - Euro"])
        saved_currency=self.store.setting("business_currency", "KES - Kenyan Shilling"); ix=currency.findText(saved_currency); currency.setCurrentIndex(ix if ix >= 0 else 0)
        bf.addRow("Business / organization name",company); bf.addRow("Tagline",tagline); bf.addRow("Business address",address); bf.addRow("Phone number",phone); bf.addRow("Business email",email); bf.addRow("Website",website); bf.addRow("Tax / registration number",tax); bf.addRow("Sales tax label",tax_label); bf.addRow("Sales tax rate",tax_rate); bf.addRow("Price entry",tax_inclusive); bf.addRow("Default currency",currency)
        business_type=QComboBox(); business_type.addItems(list(BUSINESS_PROFILES))
        saved_type=self.store.setting("business_type", "General business"); ti=business_type.findText(saved_type); business_type.setCurrentIndex(ti if ti>=0 else 0)
        accent=QComboBox(); accent.addItems(["Ocean blue", "Forest green", "Warm amber", "Royal violet", "Teal", "Custom"])
        saved_accent=self.store.setting("business_accent", "Ocean blue"); ai=accent.findText(saved_accent); accent.setCurrentIndex(ai if ai>=0 else 0)
        custom_color=self.store.setting("business_accent_custom", "#426B8A")
        color_button=QPushButton("Choose custom colour…")
        color_swatch=QLabel("  "); color_swatch.setFixedWidth(42); color_swatch.setFixedHeight(28)
        def paint_color_swatch():
            candidate=custom_color if re.fullmatch(r"#[0-9A-Fa-f]{6}",custom_color) else "#426B8A"
            color_swatch.setStyleSheet(f"background:{candidate};border:1px solid {self.palette['border']};border-radius:6px;")
        def choose_custom_color():
            nonlocal custom_color
            initial=QColor(custom_color if re.fullmatch(r"#[0-9A-Fa-f]{6}",custom_color) else "#426B8A")
            chosen=QColorDialog.getColor(initial,self,"Choose brand accent")
            if chosen.isValid():
                custom_color=chosen.name().upper(); accent.setCurrentText("Custom"); paint_color_swatch()
        color_button.clicked.connect(choose_custom_color); paint_color_swatch()
        color_row=QHBoxLayout(); color_row.addWidget(accent,1); color_row.addWidget(color_button); color_row.addWidget(color_swatch)
        pos_enabled=QCheckBox("Enable fast Point of Sale checkout for counter sales")
        pos_enabled.setChecked(self.store.setting("pos_enabled", "0") == "1")
        pos_enabled.setToolTip("Best for retail, supermarkets, restaurants, pharmacies, hardware and electronics. Schools and professional services normally leave this off.")
        def update_pos_option(profile_name):
            available=profile_name not in POS_UNAVAILABLE_PROFILES
            pos_enabled.setEnabled(available)
            if not available:
                pos_enabled.setChecked(False)
        business_type.currentTextChanged.connect(update_pos_option)
        update_pos_option(business_type.currentText())
        bf.addRow("Optional checkout", pos_enabled)
        pay_btn=QPushButton("Configure payment providers, discounts & manager PIN…")
        pay_btn.setToolTip("Enable Cash, M-Pesa, Paystack, Card, Bank transfer, Customer account — and set API keys")
        pay_btn.clicked.connect(self.open_payment_providers_dialog)
        bf.addRow("Payments & POS controls", pay_btn)
        pos_cfg_btn=QPushButton("Point of Sale screen & behaviour…")
        pos_cfg_btn.setToolTip("Till appearance, layout density, default customer")
        pos_cfg_btn.clicked.connect(self.open_pos_config_dialog)
        bf.addRow("POS screen", pos_cfg_btn)
        infra_btn=QPushButton("Branches, eTIMS, hardware profiles…")
        infra_btn.setToolTip("Multi-branch sync, fiscal control unit, till & scale profiles")
        infra_btn.clicked.connect(self.open_infrastructure_dialog)
        bf.addRow("Infrastructure", infra_btn)
        logo_state={"b64":self.store.setting("business_logo_b64","")}
        logo_preview=QLabel(); logo_preview.setMinimumHeight(64)
        def show_logo():
            if logo_state["b64"]:
                pm=QPixmap(); pm.loadFromData(__import__("base64").b64decode(logo_state["b64"]))
                logo_preview.setPixmap(pm.scaledToHeight(64,Qt.SmoothTransformation)); logo_preview.setText("")
            else:
                logo_preview.clear(); logo_preview.setText("No logo — the business name is printed instead")
        def choose_logo():
            path,_=QFileDialog.getOpenFileName(self,"Choose your business logo","","Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
            if not path: return
            try: logo_state["b64"]=load_logo_b64(path)
            except ValueError as exc: QMessageBox.warning(self,"Logo not used",str(exc)); return
            show_logo()
        def remove_logo():
            logo_state["b64"]=""; show_logo()
        logo_row=QHBoxLayout(); pick=QPushButton("Choose logo…"); pick.clicked.connect(choose_logo)
        rm=QPushButton("Remove"); rm.clicked.connect(remove_logo)
        logo_row.addWidget(logo_preview,1); logo_row.addWidget(pick); logo_row.addWidget(rm)
        show_logo()
        payment_details=QPlainTextEdit(self.store.setting("business_payment_details","")); payment_details.setMaximumHeight(80)
        payment_details.setPlaceholderText("e.g. M-Pesa Paybill 123456, Account: your invoice number\nBank: KCB 1234567890")
        invoice_footer=QPlainTextEdit(self.store.setting("business_invoice_footer","")); invoice_footer.setMaximumHeight(70)
        invoice_footer.setPlaceholderText("e.g. Goods once sold are not returnable. Thank you for shopping with us!")
        bf.addRow("Logo (printed on invoices & receipts)", logo_row)
        bf.addRow("Payment details (printed on invoices)", payment_details)
        bf.addRow("Invoice footer message", invoice_footer)
        bf.addRow("Business profile", business_type); bf.addRow("Brand accent", color_row)
        v.addWidget(business)
        sync=self.panel(); sf=QFormLayout(sync); sf.setContentsMargins(24,24,24,24); sf.setSpacing(16); sf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        device=QLineEdit(self.store.setting("device_name",os.environ.get("COMPUTERNAME","Desktop")))
        idle_lock=QSpinBox(); idle_lock.setRange(0,240); idle_lock.setSuffix(" min  (0 = never lock)")
        try: idle_lock.setValue(int(self.store.setting("idle_lock_minutes","10") or "10"))
        except ValueError: idle_lock.setValue(10)
        req_shift=QCheckBox("Require an open shift before POS sales"); req_shift.setChecked(self.store.setting("require_shift","0")=="1")
        sf.addRow("Device name",device); sf.addRow("Auto-lock when idle",idle_lock); sf.addRow("Cash control",req_shift); v.addWidget(sync)
        note=QLabel("Your records are stored on this computer and protected by Real-time Backups (Database and Backup page): choose a backup folder and how often the app saves a verified copy, and it tells you after every backup.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{self.palette['muted']};padding:8px;"); v.addWidget(note)

        # Admin "god mode" — show/hide optional product form fields for this shop
        fields_panel=self.panel(); fl=QVBoxLayout(fields_panel); fl.setContentsMargins(24,20,24,20); fl.setSpacing(10)
        fl.addWidget(QLabel("<b>Product form fields</b> · choose what staff see on Add / Edit product"))
        fl.addWidget(QLabel("Name, brand, category and selling price always stay on. Uncheck anything this shop does not use (e.g. supplier code)."))
        field_checks={}
        current_fields=self.product_form_fields()
        grid=QGridLayout(); grid.setHorizontalSpacing(18); grid.setVerticalSpacing(8)
        for i,(key,label) in enumerate(self.PRODUCT_FORM_FIELD_DEFS):
            cb=QCheckBox(label)
            always=key in self.PRODUCT_FORM_ALWAYS_ON
            cb.setChecked(key in current_fields or always)
            if always:
                cb.setEnabled(False)
                cb.setToolTip("Required field — always available")
            field_checks[key]=cb
            grid.addWidget(cb, i // 3, i % 3)
        fl.addLayout(grid)
        v.addWidget(fields_panel)

        row=QHBoxLayout(); save=QPushButton("Save business settings"); save.setObjectName("primary")
        def do_save():
            values={"business_name":company.text().strip()[:120],"business_tagline":tagline.text().strip()[:200],"business_address":address.text().strip()[:250],"business_phone":phone.text().strip()[:60],"business_email":email.text().strip()[:120],"business_website":website.text().strip()[:250],"business_tax_id":tax.text().strip()[:100],"business_tax_label":tax_label.text().strip()[:60] or "Tax","business_tax_inclusive":"1" if tax_inclusive.isChecked() else "0","business_logo_b64":logo_state["b64"],"business_payment_details":payment_details.toPlainText().strip()[:400],"business_invoice_footer":invoice_footer.toPlainText().strip()[:300],"business_tax_rate":format(Decimal(str(tax_rate.value())).quantize(Decimal("0.01")),"f"),"business_currency":currency.currentText(),"business_type":business_type.currentText(),"pos_enabled":"1" if pos_enabled.isChecked() else "0","business_accent":accent.currentText(),"business_accent_custom":custom_color if accent.currentText()=="Custom" else self.store.setting("business_accent_custom", "#426B8A"),"device_name":device.text().strip()[:100]}
            try:
                values["business_website"]=normalize_website_url(values["business_website"])
            except ValueError as exc:
                QMessageBox.warning(self,"Invalid website",str(exc)); return
            previous_type=self.store.setting("business_type", "General business")
            previous_name=self.store.setting("business_name", "Default business")
            target_workspace_id, target_workspace_path, _ = workspace_path(
                APP_DIR / "workspaces", values["business_name"] or "Default business", values["business_type"])
            identity_changed = target_workspace_path != self.store.workspace_path
            if identity_changed:
                answer=QMessageBox.question(self,"Open isolated business workspace?",
                    "NextGen ERP stores each business name + business profile in a separate database. "
                    "Opening this identity will not copy records from the currently active business workspace; "
                    "the old workspace remains on this computer and can be reopened by selecting its identity again. "
                    "Continue?",
                    QMessageBox.Yes|QMessageBox.No, QMessageBox.Yes)
                if answer != QMessageBox.Yes:
                    values["business_type"]=previous_type
                    values["business_name"]=previous_name
                    business_type.setCurrentText(previous_type)
                    company.setText(previous_name)
                    identity_changed=False
            preserved_modules=[]
            # Workspace selectors and connection settings are installation-wide.
            # Save them before switching, then write branding, tax, roles and modules
            # only into the newly active workspace.
            self.store.set_setting("business_name", values["business_name"])
            self.store.set_setting("business_type", values["business_type"])
            self.store.set_setting("device_name", values["device_name"])
            self.store.set_setting("idle_lock_minutes", str(idle_lock.value()))
            self.store.set_setting("require_shift", "1" if req_shift.isChecked() else "0")
            try:
                requested_workspace_path = workspace_path(
                    APP_DIR / "workspaces", values["business_name"] or "Default business", values["business_type"])[1]
                if requested_workspace_path != self.store.workspace_path:
                    self.store.switch_workspace(values["business_type"], values["business_name"])
            except (sqlite3.Error, RuntimeError, WorkspaceIdentityError) as exc:
                QMessageBox.critical(self, "Workspace switch failed", f"The business workspace could not be opened safely. No other workspace data was loaded.\n\n{exc}")
                # Restore selector to the workspace which is still active.
                self.store.set_setting("business_name", previous_name)
                self.store.set_setting("business_type", previous_type)
                return
            profile_applied=self.store.setting("applied_profile", "") == values["business_type"]
            if not profile_applied:
                answer=QMessageBox.question(self,"Apply business profile?",
                    "This workspace has not had its starter module layout applied. Apply the selected profile's modules now?",
                    QMessageBox.Yes|QMessageBox.No, QMessageBox.Yes)
                if answer == QMessageBox.Yes:
                    try:
                        # POS preference is written before the profile is applied.
                        self.store.set_setting("pos_enabled", values["pos_enabled"])
                        self.store.apply_profile(values["business_type"])
                        profile_applied=True
                    except (ValueError, PermissionError) as exc:
                        QMessageBox.warning(self,"Profile not applied",str(exc))
                        return
            preserved_modules=self.store.modules_preserved_by_profile(values["business_type"])
            for k,val in values.items():
                if k not in {"business_name", "business_type", "sync_url", "device_name"}:
                    self.store.set_setting(k,val)
            field_map={key: cb.isChecked() for key,cb in field_checks.items()}
            for key in self.PRODUCT_FORM_ALWAYS_ON:
                field_map[key]=True
            self.store.set_setting("product_form_fields", json.dumps(field_map, sort_keys=True))

            if values["pos_enabled"] == "1" and values["business_type"] not in POS_UNAVAILABLE_PROFILES:
                try:
                    self.store.set_enabled("Point of Sale", True)
                except ValueError as exc:
                    QMessageBox.warning(self,"POS not enabled",str(exc))
                    return
            elif values["pos_enabled"] == "0":
                try:
                    self.store.set_enabled("Point of Sale", False)
                except ValueError as exc:
                    QMessageBox.warning(self,"POS remains enabled",str(exc))
            self.store.log(self.username,"Updated business settings",values["business_name"] or "Business profile")
            self.active = "Dashboard"
            self.refresh_appearance()
            self.profile_switch_note = f"Active isolated workspace: {values['business_name'] or 'Default business'} · {values['business_type']}"
            profile_note=("The profile's starter workspace layout is active." if profile_applied else "The selected profile is saved; its workspace layout has not been applied yet.")
            preserved_note=(" Workspaces kept enabled to retain existing records: " + ", ".join(preserved_modules) + ".") if profile_applied and preserved_modules else ""
            QMessageBox.information(self,"Saved",f"Business identity and settings were saved locally. {profile_note}{preserved_note} Profile workspaces use configurable records; connected industry workflows are still in progress.")

        save.clicked.connect(do_save); row.addWidget(save); printbtn=QPushButton("🖨  Printer setup"); printbtn.clicked.connect(self.open_printer_setup); row.addWidget(printbtn); row.addStretch(); v.addLayout(row); v.addStretch(); return w


    def open_infrastructure_dialog(self):
        """Configure multi-branch sync, eTIMS agent, and till/scale hardware."""
        from etims_agent import load_etims_config, save_etims_config
        from branch_sync import load_branch_config, save_branch_config, build_envelope_from_queue, export_envelope_to_folder, import_envelopes_from_folder, apply_envelope, post_envelope_to_relay, pull_envelopes_from_relay
        from hardware_profiles import load_hardware_profile, save_hardware_profile
        from advanced_features import load_flags, save_flags

        etims = load_etims_config(self.store.setting("etims_config_json", ""))
        branch = load_branch_config(self.store.setting("branch_sync_json", ""))
        hw = load_hardware_profile(self.store.setting("hardware_profile_json", ""))
        flags = load_flags(self.store.setting("feature_flags_json", ""))

        dlg = QDialog(self)
        dlg.setWindowTitle("Infrastructure · branches · eTIMS · hardware")
        dlg.setStyleSheet(themed_style(self.palette))
        dlg.setMinimumSize(720, 640)
        root = QVBoxLayout(dlg)
        tabs = QTabWidget(); root.addWidget(tabs, 1)

        # --- Branch tab
        btab = QWidget(); bl = QFormLayout(btab)
        b_en = QCheckBox("Enable multi-branch sync"); b_en.setChecked(bool(branch.get("enabled"))); bl.addRow(b_en)
        b_id = QLineEdit(str(branch.get("branch_id") or "")); bl.addRow("Branch ID", b_id)
        b_name = QLineEdit(str(branch.get("branch_name") or "")); bl.addRow("Branch name", b_name)
        b_relay = QLineEdit(str(branch.get("relay_url") or "")); b_relay.setPlaceholderText("https://relay.example.com/v1/sync"); bl.addRow("Relay URL", b_relay)
        b_token = QLineEdit(str(branch.get("relay_token") or "")); b_token.setEchoMode(QLineEdit.Password); bl.addRow("Relay token", b_token)
        b_folder = QLineEdit(str(branch.get("shared_folder") or "")); b_folder.setPlaceholderText(r"D:\NextGenSync or \\server\share\sync"); bl.addRow("Shared folder", b_folder)
        b_btns = QHBoxLayout()
        exp = QPushButton("Export pack now"); imp = QPushButton("Import packs from folder")
        b_btns.addWidget(exp); b_btns.addWidget(imp); bl.addRow(b_btns)
        def do_export():
            env = build_envelope_from_queue(self.store.operations.conn, b_id.text().strip(), b_name.text().strip())
            if not env:
                QMessageBox.information(dlg, "Nothing to export", "Sync queue is empty."); return
            folder = b_folder.text().strip()
            if not folder:
                QMessageBox.warning(dlg, "Folder required", "Set a shared folder path first."); return
            try:
                path = export_envelope_to_folder(env, folder)
                if b_relay.text().strip():
                    ok, msg = post_envelope_to_relay(env, b_relay.text().strip(), b_token.text().strip())
                    QMessageBox.information(dlg, "Exported", f"Wrote {path}\nRelay: {msg if ok else 'failed — '+msg}")
                else:
                    QMessageBox.information(dlg, "Exported", f"Wrote {path}")
            except Exception as exc:
                QMessageBox.warning(dlg, "Export failed", str(exc))
        def do_import():
            folder = b_folder.text().strip()
            if not folder:
                QMessageBox.warning(dlg, "Folder required", "Set a shared folder path first."); return
            packs = import_envelopes_from_folder(folder, b_id.text().strip())
            total_a = total_s = 0
            for pack in packs:
                a, s = apply_envelope(self.store.operations.conn, pack)
                total_a += a; total_s += s
            if b_relay.text().strip():
                ok, remote, msg = pull_envelopes_from_relay(b_relay.text().strip(), b_id.text().strip(), b_token.text().strip())
                if ok:
                    for pack in remote:
                        a, s = apply_envelope(self.store.operations.conn, pack)
                        total_a += a; total_s += s
            QMessageBox.information(dlg, "Import finished", f"Applied {total_a} · skipped {total_s}")
        exp.clicked.connect(do_export); imp.clicked.connect(do_import)
        tabs.addTab(btab, "Branches")

        # --- eTIMS tab
        etab = QWidget(); el = QFormLayout(etab)
        e_en = QCheckBox("Enable fiscal / eTIMS agent"); e_en.setChecked(bool(etims.get("enabled"))); el.addRow(e_en)
        e_mode = QComboBox(); e_mode.addItems(["preparation", "live"]); e_mode.setCurrentText(str(etims.get("mode") or "preparation")); el.addRow("Mode", e_mode)
        e_url = QLineEdit(str(etims.get("bridge_url") or "")); e_url.setPlaceholderText("http://127.0.0.1:8080/sign or portal URL"); el.addRow("Control unit bridge URL", e_url)
        e_key = QLineEdit(str(etims.get("api_key") or "")); e_key.setEchoMode(QLineEdit.Password); el.addRow("API key", e_key)
        e_pin = QLineEdit(str(etims.get("seller_pin") or "")); el.addRow("Seller PIN (KRA)", e_pin)
        e_dev = QLineEdit(str(etims.get("device_serial") or "")); el.addRow("Device serial", e_dev)
        e_cu = QLineEdit(str(etims.get("control_unit_id") or "")); el.addRow("Control unit ID", e_cu)
        e_br = QLineEdit(str(etims.get("branch_id") or "")); el.addRow("Fiscal branch ID", e_br)
        e_auto = QCheckBox("Auto-sign each POS sale"); e_auto.setChecked(bool(etims.get("auto_sign_on_sale", True))); el.addRow(e_auto)
        tabs.addTab(etab, "eTIMS")

        # --- Hardware tab
        htab = QWidget(); hl = QFormLayout(htab)
        h_tid = QLineEdit(str(hw.get("till_id") or "TILL-1")); hl.addRow("Till ID", h_tid)
        h_tname = QLineEdit(str(hw.get("till_name") or "")); hl.addRow("Till name", h_tname)
        h_drawer = QCheckBox("Cash drawer attached"); h_drawer.setChecked(bool(hw.get("cash_drawer_attached", True))); hl.addRow(h_drawer)
        h_serial = QCheckBox("Require serial/IMEI for tracked products"); h_serial.setChecked(bool(hw.get("require_serial_for_tracked", True))); hl.addRow(h_serial)
        scale = hw.get("scale") or {}
        h_scale = QCheckBox("Enable scale"); h_scale.setChecked(bool(scale.get("enabled"))); hl.addRow(h_scale)
        h_smode = QComboBox(); h_smode.addItems(["manual", "simulated", "serial"]); h_smode.setCurrentText(str(scale.get("mode") or "manual")); hl.addRow("Scale mode", h_smode)
        h_port = QLineEdit(str(scale.get("port") or "COM3")); hl.addRow("Scale COM port", h_port)
        h_sim = QLineEdit(str(scale.get("simulated_weight") or "0.000")); hl.addRow("Simulated weight", h_sim)
        tabs.addTab(htab, "Hardware")

        # --- Flags
        ftab = QWidget(); fl = QFormLayout(ftab)
        flag_boxes = {}
        for key, label in [
            ("serial_tracking", "Serial / IMEI tracking"),
            ("loyalty", "Loyalty points"),
            ("multi_location", "Multi-location stock"),
            ("fiscal_etims", "Fiscal / eTIMS"),
            ("cash_drawer", "Cash drawer kick"),
            ("returns_desk", "Returns desk"),
        ]:
            box = QCheckBox(label); box.setChecked(bool(flags.get(key))); fl.addRow(box); flag_boxes[key] = box
        tabs.addTab(ftab, "Feature flags")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        root.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)
        def save():
            self.store.set_setting("branch_sync_json", save_branch_config({
                "enabled": b_en.isChecked(), "branch_id": b_id.text().strip(), "branch_name": b_name.text().strip(),
                "relay_url": b_relay.text().strip(), "relay_token": b_token.text().strip(),
                "shared_folder": b_folder.text().strip(), "auto_export_minutes": branch.get("auto_export_minutes") or 0,
                "last_sync_at": branch.get("last_sync_at") or "", "peer_branches": branch.get("peer_branches") or [],
            }))
            self.store.set_setting("etims_config_json", save_etims_config({
                "enabled": e_en.isChecked(), "mode": e_mode.currentText(), "bridge_url": e_url.text().strip(),
                "api_key": e_key.text().strip(), "seller_pin": e_pin.text().strip(), "device_serial": e_dev.text().strip(),
                "control_unit_id": e_cu.text().strip(), "branch_id": e_br.text().strip(),
                "auto_sign_on_sale": e_auto.isChecked(), "timeout_sec": etims.get("timeout_sec") or 45,
            }))
            self.store.set_setting("hardware_profile_json", save_hardware_profile({
                "till_id": h_tid.text().strip() or "TILL-1", "till_name": h_tname.text().strip(),
                "cash_drawer_attached": h_drawer.isChecked(), "require_serial_for_tracked": h_serial.isChecked(),
                "scale": {"enabled": h_scale.isChecked(), "mode": h_smode.currentText(), "port": h_port.text().strip(),
                          "baud": 9600, "unit": "kg", "simulated_weight": h_sim.text().strip() or "0.000"},
            }))
            new_flags = dict(flags)
            for k, box in flag_boxes.items():
                new_flags[k] = box.isChecked()
            self.store.set_setting("feature_flags_json", save_flags(new_flags))
            self.store.log(self.username, "Updated infrastructure", "branches/eTIMS/hardware")
            dlg.accept()
            QMessageBox.information(self, "Saved", "Infrastructure settings saved for this business / till.")
        buttons.accepted.connect(save)
        dlg.exec()


    def open_pos_config_dialog(self):
        """Choose how the Point of Sale screen looks and behaves."""
        from pos_config import APPEARANCE_OPTIONS, load_pos_config, save_pos_config
        cfg = load_pos_config(self.store.setting("pos_config_json", ""))
        dlg = QDialog(self)
        dlg.setWindowTitle("Point of Sale configuration")
        dlg.setStyleSheet(themed_style(self.palette))
        dlg.setMinimumWidth(520)
        root = QVBoxLayout(dlg)
        root.addWidget(QLabel("<span style='font-size:16pt;font-weight:800'>POS screen</span>"))
        root.addWidget(QLabel("These options apply to this business workspace. Open Point of Sale again after saving to see changes."))
        form = QFormLayout()
        appearance = QComboBox()
        for key, label in APPEARANCE_OPTIONS:
            appearance.addItem(label, key)
        idx = appearance.findData(cfg.get("appearance") or "immersive_dark")
        appearance.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Till appearance", appearance)
        live = QCheckBox("Show LIVE TILL badge"); live.setChecked(bool(cfg.get("show_live_badge", True)))
        form.addRow(live)
        large = QCheckBox("Large Complete Sale button"); large.setChecked(bool(cfg.get("large_pay_button", True)))
        form.addRow(large)
        pulse = QCheckBox("Pulse animation on pay button"); pulse.setChecked(bool(cfg.get("pulse_pay_button", True)))
        form.addRow(pulse)
        disc = QCheckBox("Show discount row on till"); disc.setChecked(bool(cfg.get("show_discount_row", True)))
        form.addRow(disc)
        compact = QCheckBox("Compact layout (smaller padding)"); compact.setChecked(bool(cfg.get("compact_mode", False)))
        form.addRow(compact)
        customer = QLineEdit(str(cfg.get("default_customer") or "Walk-in Customer"))
        form.addRow("Default customer name", customer)
        root.addLayout(form)
        tip = QLabel("Immersive dark = neon till. Match app theme = follows light/dark mode. Vibrant light = bright retail look.")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{self.palette['muted']};")
        root.addWidget(tip)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        root.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)
        def save():
            self.store.set_setting("pos_config_json", save_pos_config({
                "appearance": appearance.currentData(),
                "show_live_badge": live.isChecked(),
                "large_pay_button": large.isChecked(),
                "pulse_pay_button": pulse.isChecked(),
                "show_discount_row": disc.isChecked(),
                "compact_mode": compact.isChecked(),
                "default_customer": customer.text().strip() or "Walk-in Customer",
            }))
            self.store.log(self.username, "Updated POS configuration", str(appearance.currentData()))
            self.pages.pop("Point of Sale", None)
            dlg.accept()
            QMessageBox.information(self, "Saved", "POS configuration saved. Open Point of Sale to use the new look.")
        buttons.accepted.connect(save)
        dlg.exec()

    def open_payment_providers_dialog(self):
        """Admin UI for payment channels, credentials, discounts and manager PIN."""
        from payment_providers import (
            PROVIDER_CATALOGUE, load_providers_config, save_providers_config, mask_secret,
        )
        cfg = load_providers_config(self.store.setting("payment_providers_json", ""))
        dlg = QDialog(self)
        dlg.setWindowTitle("Payment providers & POS controls")
        dlg.setStyleSheet(themed_style(self.palette))
        dlg.setMinimumWidth(720)
        dlg.setMinimumHeight(560)
        root = QVBoxLayout(dlg)
        root.addWidget(QLabel("<span style='font-size:16pt;font-weight:800'>Payment channels</span>"))
        root.addWidget(QLabel("Turn channels on for the till and fill the credentials your provider requires. Secrets stay in this business workspace only."))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget(); form_host = QVBoxLayout(host); form_host.setSpacing(10)
        widgets = {}
        for pid, meta in PROVIDER_CATALOGUE.items():
            row = cfg["providers"].get(pid, {"enabled": False, "credentials": {}})
            card = QFrame(); card.setObjectName("panel")
            cl = QVBoxLayout(card); cl.setContentsMargins(12,10,12,10)
            head = QHBoxLayout()
            en = QCheckBox(meta["label"]); en.setChecked(bool(row.get("enabled")))
            head.addWidget(en); head.addStretch()
            head.addWidget(QLabel(meta["description"]))
            cl.addLayout(head)
            fields_map = {}
            if meta["fields"]:
                grid = QFormLayout()
                for field in meta["fields"]:
                    edit = QLineEdit(str((row.get("credentials") or {}).get(field["key"], "")))
                    edit.setPlaceholderText(field.get("placeholder") or "")
                    if field.get("secret"):
                        edit.setEchoMode(QLineEdit.Password)
                        edit.setPlaceholderText(edit.placeholderText() or "Stored securely in this workspace")
                    grid.addRow(field["label"], edit)
                    fields_map[field["key"]] = edit
                cl.addLayout(grid)
            form_host.addWidget(card)
            widgets[pid] = {"enabled": en, "fields": fields_map}
        scroll.setWidget(host); root.addWidget(scroll, 1)
        # Global POS controls
        controls = QFrame(); controls.setObjectName("panelAlt")
        cf = QFormLayout(controls)
        pin = QLineEdit(str(cfg.get("manager_pin") or "")); pin.setEchoMode(QLineEdit.Password)
        pin.setPlaceholderText("Leave blank to disable manager PIN gates")
        cf.addRow("Manager override PIN", pin)
        allow_disc = QCheckBox("Allow discounts on the till"); allow_disc.setChecked(bool(cfg.get("allow_discounts", True)))
        cf.addRow(allow_disc)
        max_disc = QDoubleSpinBox(); max_disc.setRange(0, 100); max_disc.setSuffix(" %")
        max_disc.setValue(float(cfg.get("max_discount_percent") or 25))
        cf.addRow("Max discount without PIN", max_disc)
        req_reason = QCheckBox("Require a reason when a discount is applied"); req_reason.setChecked(bool(cfg.get("require_discount_reason")))
        cf.addRow(req_reason)
        allow_ret = QCheckBox("Allow returns / refunds from sales history"); allow_ret.setChecked(bool(cfg.get("allow_returns", True)))
        cf.addRow(allow_ret)
        root.addWidget(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        root.addWidget(buttons)
        buttons.rejected.connect(dlg.reject)
        def save():
            new_cfg = {
                "providers": {},
                "manager_pin": pin.text().strip()[:20],
                "allow_discounts": allow_disc.isChecked(),
                "max_discount_percent": max_disc.value(),
                "require_discount_reason": req_reason.isChecked(),
                "allow_returns": allow_ret.isChecked(),
                "require_return_reason": True,
            }
            for pid, meta in PROVIDER_CATALOGUE.items():
                w = widgets[pid]
                creds = {key: edit.text().strip()[:500] for key, edit in w["fields"].items()}
                new_cfg["providers"][pid] = {"enabled": w["enabled"].isChecked(), "label": meta["label"], "credentials": creds}
            self.store.set_setting("payment_providers_json", save_providers_config(new_cfg))
            self.store.log(self.username, "Updated payment providers", "POS payment configuration")
            # Force POS page rebuild so new methods appear
            self.pages.pop("Point of Sale", None)
            dlg.accept()
            QMessageBox.information(self, "Saved", "Payment providers and POS controls were saved for this business.")
        buttons.accepted.connect(save)
        dlg.exec()

    def update_branding(self):
        business_name=self.store.setting("business_name", "NextGen").strip() or "NextGen"
        escaped=self._html_escape(business_name)
        self.brand_label.setText(f"⬡  {escaped} <span style='color:{self.palette['blue']}'>ERP</span>")
        self.setWindowTitle((self.store.setting("business_name", "Your Business").strip() or "Your Business") + " | NextGen ERP")

    def _capture_page_state(self,page):
        state={}
        fields=((QLineEdit,"line_edits"),(QTextEdit,"text_edits"),(QPlainTextEdit,"plain_text_edits"),
                (QComboBox,"combos"),(QSpinBox,"spin_boxes"),(QDoubleSpinBox,"double_spin_boxes"),
                (QDateEdit,"date_edits"),(QCheckBox,"checks"),(QTableWidget,"tables"),(QScrollArea,"scroll_areas"))
        for widget_type,key in fields:
            values=[]
            for widget in page.findChildren(widget_type):
                if widget_type is QLineEdit:
                    values.append((widget.text(),widget.cursorPosition()))
                elif widget_type in {QTextEdit,QPlainTextEdit}:
                    values.append((widget.toPlainText(),widget.textCursor().position()))
                elif widget_type is QComboBox:
                    values.append((widget.currentIndex(),widget.currentData(),widget.currentText(),widget.isEditable()))
                elif widget_type in {QSpinBox,QDoubleSpinBox}:
                    values.append(widget.value())
                elif widget_type is QDateEdit:
                    values.append(widget.date())
                elif widget_type is QCheckBox:
                    values.append(widget.isChecked())
                elif widget_type is QTableWidget:
                    values.append((widget.currentRow(),widget.currentColumn(),[row.row() for row in widget.selectionModel().selectedRows()]))
                elif widget_type is QScrollArea:
                    values.append((widget.verticalScrollBar().value(),widget.horizontalScrollBar().value()))
            state[key]=values
        return state

    def _restore_page_state(self,page,state):
        fields=((QLineEdit,"line_edits"),(QTextEdit,"text_edits"),(QPlainTextEdit,"plain_text_edits"),
                (QComboBox,"combos"),(QSpinBox,"spin_boxes"),(QDoubleSpinBox,"double_spin_boxes"),
                (QDateEdit,"date_edits"),(QCheckBox,"checks"),(QTableWidget,"tables"),(QScrollArea,"scroll_areas"))
        for widget_type,key in fields:
            for widget,value in zip(page.findChildren(widget_type),state.get(key,[])):
                signals=widget.blockSignals(True)
                try:
                    if widget_type is QLineEdit:
                        text,cursor=value; widget.setText(text); widget.setCursorPosition(min(cursor,len(text)))
                    elif widget_type in {QTextEdit,QPlainTextEdit}:
                        text,cursor=value; widget.setPlainText(text)
                        text_cursor=widget.textCursor(); text_cursor.setPosition(min(cursor,len(text))); widget.setTextCursor(text_cursor)
                    elif widget_type is QComboBox:
                        index,data,text,editable=value
                        if 0<=index<widget.count(): widget.setCurrentIndex(index)
                        if data is not None:
                            data_index=widget.findData(data)
                            if data_index>=0: widget.setCurrentIndex(data_index)
                        elif text:
                            text_index=widget.findText(text)
                            if text_index>=0: widget.setCurrentIndex(text_index)
                        if editable: widget.setEditText(text)
                    elif widget_type in {QSpinBox,QDoubleSpinBox}:
                        widget.setValue(value)
                    elif widget_type is QDateEdit:
                        widget.setDate(value)
                    elif widget_type is QCheckBox:
                        widget.setChecked(value)
                    elif widget_type is QTableWidget:
                        row,column,selected=value
                        if row>=0 and column>=0 and row<widget.rowCount() and column<widget.columnCount(): widget.setCurrentCell(row,column)
                        else: widget.clearSelection()
                        for selected_row in selected:
                            if 0<=selected_row<widget.rowCount(): widget.selectRow(selected_row)
                    elif widget_type is QScrollArea:
                        vertical,horizontal=value
                        widget.verticalScrollBar().setValue(vertical); widget.horizontalScrollBar().setValue(horizontal)
                finally:
                    widget.blockSignals(signals)

    def refresh_appearance(self):
        # Recolour existing widgets and preserve page instances: avoids expensive
        # teardown/rebuild and restores the exact current workspace on theme changes.
        self.palette=palette_for(self.store,self.theme)
        stylesheet=themed_style(self.palette)
        self.setStyleSheet(stylesheet)
        QApplication.instance().setStyleSheet(stylesheet)
        self.sidebar.setStyleSheet(f"background:{self.palette['sidebar']};border-right:1px solid {self.palette['border']};")
        self.topbar.setStyleSheet(f"background:{self.palette['sidebar']};border-bottom:1px solid {self.palette['border']};")
        self.theme_btn.setText("☀ Light mode" if self.theme=="dark" else "☾ Dark mode")
        if hasattr(self,"profile_chip"):
            self.profile_chip.setStyleSheet(
                f"font-weight:700;padding:7px 12px;background:{self.palette['panel2']};"
                f"border:1px solid {self.palette['border']};border-radius:999px;color:{self.palette['text']};"
            )
        self.update_branding()
        for page in self.pages.values():
            if hasattr(page,"apply_palette"):
                page.apply_palette(self.palette,self.theme=="dark")
        active=self.active
        self.rebuild_nav()
        self.show_page(active)
        self.refresh_syncpill()

    def toggle_theme(self):
        next_theme = "light" if self.theme == "dark" else "dark"
        # Capture a snapshot of the current look so ThemeRipple can reveal the new theme
        # with a circular wipe from the theme button (or window centre as fallback).
        origin = None
        try:
            if hasattr(self, "theme_btn") and self.theme_btn is not None:
                origin = self.theme_btn.mapTo(self, self.theme_btn.rect().center())
        except Exception:
            origin = None
        if origin is None:
            origin = self.rect().center()
        try:
            snapshot = self.grab()
        except Exception:
            snapshot = None

        # Theme is a local presentation preference; non-admin users may toggle it
        # without needing business-configuration rights. Only Admin persists it.
        if self.role == "Admin":
            try:
                self.store.set_setting("theme", next_theme)
            except (PermissionError, sqlite3.Error) as exc:
                QMessageBox.warning(self, "Theme not saved", f"The theme will change for this session only.\n\n{exc}")
        self.theme = next_theme
        ripple = None
        try:
            from fx import ThemeRipple, Motion
            if Motion.enabled and snapshot is not None and not snapshot.isNull():
                ripple = ThemeRipple(self, snapshot, origin, ms=720, autostart=False)
                ripple.repaint()               # old look is on screen BEFORE anything changes
        except Exception:
            ripple = None
        self._theme_switching = True
        self.setUpdatesEnabled(False)          # no half-themed frames while styles are re-polished
        try:
            self.refresh_appearance()
        finally:
            self.setUpdatesEnabled(True)
            self._theme_switching = False
        if ripple is not None:
            QTimer.singleShot(40, ripple.start)   # let the new theme paint underneath first

    def integrations(self):
        if self.role != "Admin" or not self._authorize('configure'):
            return None
        w,v=self.page_container("Integrations","Connect the product catalogue from the shop website. Connections are read-only by default and never change local stock or prices.")
        panel=self.panel(); form=QFormLayout(panel); form.setContentsMargins(22,20,22,20); form.setSpacing(14)
        source=QComboBox()
        source.addItem("99 COMPUTERS · live site catalogue (recommended)", "99computers")
        source.addItem("WooCommerce · public product catalogue", "woocommerce")
        source.addItem("Public JSON catalogue feed", "json")
        saved_source=self.store.setting("website_catalog_source","99computers")
        idx=source.findData(saved_source)
        source.setCurrentIndex(idx if idx >= 0 else 0)
        default_catalog_url = "https://www.99computers.co.ke/catalog.json" if saved_source in {"99computers", "99_computers", "site_catalog"} else ""
        url=QLineEdit(self.store.setting("website_catalog_url",default_catalog_url))
        url.setPlaceholderText("https://www.99computers.co.ke/catalog.json")
        def update_feed_hint(index=None):
            if str(source.currentData() or "") == "99computers" and not url.text().strip():
                url.setText("https://www.99computers.co.ke/catalog.json")
        source.currentIndexChanged.connect(update_feed_hint)
        form.addRow("Website platform",source)
        form.addRow("Website / feed URL",url)
        note=QLabel("99 COMPUTERS: reads the site's public catalog.json and resolves product photos from /images/shop/. Product names, descriptions, brands, categories, regular prices, sale prices, availability and images are mirrored read-only. WooCommerce: enter the website home URL. Generic JSON: enter a public HTTPS feed. Website values are a catalogue reference only; local stock, costs, selling prices and sales remain under ERP control.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{self.palette['muted']};padding:10px;background:{self.palette['panel2']};border-radius:8px;")
        form.addRow("How it works",note)
        last_sync=self.store.setting("website_catalog_last_sync","") or "Not synchronized yet"
        self.catalog_sync_status=QLabel("Last successful sync: " + last_sync)
        self.catalog_sync_status.setWordWrap(True)
        form.addRow("Status",self.catalog_sync_status)
        v.addWidget(panel)
        actions=QHBoxLayout()
        save=QPushButton("Save connection"); save.setObjectName("primary")
        sync=QPushButton("Test connection & sync catalogue")
        browse=QPushButton("Browse cached catalogue")
        actions.addWidget(save); actions.addWidget(sync); actions.addWidget(browse); actions.addStretch(); v.addLayout(actions)
        self._catalog_integration_widgets=(source,url,save,sync)
        def save_connection(show_message=True):
            selected=str(source.currentData() or "woocommerce")
            target=url.text().strip()
            try:
                if target:
                    parsed=urllib.parse.urlsplit(target)
                    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError("Enter a valid public HTTPS website/feed URL without embedded credentials.")
                self.store.set_setting("website_catalog_source",selected)
                self.store.set_setting("website_catalog_url",target)
                self.store.log(self.username,"Updated website catalogue connection",selected + (" · " + target if target else " · URL cleared"))
                if show_message:
                    QMessageBox.information(self,"Connection saved","Website catalogue settings were saved for this shop workspace.")
                return True
            except (ValueError,PermissionError) as exc:
                QMessageBox.warning(self,"Connection not saved",str(exc)); return False
        save.clicked.connect(lambda:save_connection(True))
        def start_sync():
            if not save_connection(False): return
            target=url.text().strip(); selected=str(source.currentData() or "woocommerce")
            if not target:
                QMessageBox.information(self,"Website URL needed","Enter the shop website or public JSON feed URL first."); return
            existing=getattr(self,"_catalog_worker",None)
            if existing is not None and existing.isRunning():
                QMessageBox.information(self,"Sync already running","The catalogue connection is already being checked."); return
            sync.setEnabled(False); sync.setText("Connecting…")
            self._catalog_sync_source = selected  # bind the response to the source used for this request
            worker=WebsiteCatalogWorker(selected,target,self)
            self._catalog_worker=worker
            worker.succeeded.connect(self._on_catalog_sync_success)
            worker.failed.connect(self._on_catalog_sync_failure)
            worker.finished.connect(lambda: (sync.setEnabled(True),sync.setText("Test connection & sync catalogue")))
            worker.start()
        sync.clicked.connect(start_sync)
        browse.clicked.connect(self.open_website_catalog)
        other=self.panel(); ov=QVBoxLayout(other); ov.setContentsMargins(18,16,18,16)
        ov.addWidget(QLabel("Other connections"))
        ov.addWidget(QLabel("Payment gateways, M-Pesa statement reconciliation, eTIMS and email delivery require their own credentials, endpoints and/or certification. They are not activated merely by saving the product catalogue connection."))
        v.addWidget(other); v.addStretch(); return w

    def _on_catalog_sync_success(self, items):
        # Use the source captured when the request started, even if the settings UI changes.
        source=str(getattr(self, "_catalog_sync_source", self.store.setting("website_catalog_source","woocommerce")) or "woocommerce")
        try:
            count=replace_catalog_cache(self.store.workspace_conn,source,items)
            synced_at=stamp()
            self.store.set_setting("website_catalog_last_sync",synced_at)
            self.store.log(self.username,"Synchronized website catalogue",f"{count} website products from {source}")
            status=getattr(self,"catalog_sync_status",None)
            if status is not None:
                status.setText(f"Last successful sync: {synced_at} · {count:,} products cached")
            QMessageBox.information(self,"Website catalogue synchronized",f"Loaded {count:,} products from the website.\n\nThis was a read-only catalogue sync; local stock and prices were not changed.")
        except Exception as exc:
            QMessageBox.critical(self,"Catalogue cache failed",f"The website responded, but the local catalogue could not be saved.\n\n{exc}")

    def _on_catalog_sync_failure(self, error):
        QMessageBox.warning(self,"Website connection failed",str(error))

    def open_website_catalog(self):
        if not self._authorize("view"):
            return
        dialog=WebsiteCatalogDialog(self.store,self)
        dialog.showMaximized()
        dialog.exec()

    def database_page(self):
        is_admin = self.role == "Admin"
        can_schedule = is_admin or self.role == "Manager" or "manage_users" in self.store.role_permissions(self.role)
        if not can_schedule:
            QMessageBox.warning(self, "Access denied", "Only an administrator or manager can open backup settings.")
            return
        if is_admin and not self._authorize("backup"):
            return
        w, v = self.page_container(
            "Database and Backup",
            "Automatic and manual backups stay on this computer (or a folder you choose). Restore and reset remain Admin-only.",
        )

        # --- Auto-backup schedule (Admin + Manager) ---
        auto = self.panel()
        af = QVBoxLayout(auto)
        af.setContentsMargins(20, 20, 20, 20)
        af.setSpacing(12)
        af.addWidget(QLabel("<b>Real-time Backups</b>"))
        intro = QLabel(
            "NextGen ERP watches your data and saves a verified full backup for you — you choose how often. "
            "After every backup the app tells you it worked (or warns you if it did not). "
            "Point the folder at a USB drive, a network share, or a Google Drive / OneDrive / Dropbox folder "
            "so your records survive if this computer fails."
        )
        intro.setWordWrap(True)
        af.addWidget(intro)

        enabled = QCheckBox("Turn on Real-time Backups")
        enabled.setChecked(self.store.setting("auto_backup_enabled", "1") == "1")
        af.addWidget(enabled)

        mode = QComboBox()
        mode.addItem("Overwrite one recovery file (recommended)", "overwrite")
        mode.addItem("Keep several dated copies", "rotate")
        saved_mode = (self.store.setting("auto_backup_mode", "overwrite") or "overwrite").strip().casefold()
        mode.setCurrentIndex(0 if saved_mode != "rotate" else 1)
        mode.setToolTip(
            "Overwrite keeps nextgen_erp_latest.zip (plus one previous copy for safety). "
            "Rotate keeps multiple timestamped ZIPs."
        )
        row_m = QHBoxLayout()
        row_m.addWidget(QLabel("Backup style"))
        row_m.addWidget(mode, 1)
        af.addLayout(row_m)

        freq = QComboBox()
        for minutes, label in INTERVAL_OPTIONS:
            freq.addItem(label, minutes)
        try:
            current_m = int(self.store.setting("auto_backup_interval_minutes", str(REALTIME)) or str(REALTIME))
        except (TypeError, ValueError):
            current_m = REALTIME
        idx_f = next((i for i, (m, _) in enumerate(INTERVAL_OPTIONS) if m == current_m), 0)
        freq.setCurrentIndex(idx_f)
        row_f = QHBoxLayout()
        row_f.addWidget(QLabel("How often"))
        row_f.addWidget(freq, 1)
        af.addLayout(row_f)

        folder = QLineEdit(self.store.setting("auto_backup_folder", "") or str(APP_DIR / "backups"))
        folder.setPlaceholderText(str(APP_DIR / "backups"))
        browse = QPushButton("Choose folder…")
        def pick_folder():
            path = QFileDialog.getExistingDirectory(self, "Backup folder", folder.text() or str(APP_DIR))
            if path:
                folder.setText(path)
        browse.clicked.connect(pick_folder)
        folder.textChanged.connect(lambda *_: _check_drive() if "_check_drive" in locals() else None)
        row_d = QHBoxLayout()
        row_d.addWidget(QLabel("Save to"))
        row_d.addWidget(folder, 1)
        row_d.addWidget(browse)
        af.addLayout(row_d)

        keep = QSpinBox()
        keep.setRange(2, 50)
        try:
            keep.setValue(int(self.store.setting("auto_backup_keep", "5") or "5"))
        except (TypeError, ValueError):
            keep.setValue(5)
        row_k = QHBoxLayout()
        row_k.addWidget(QLabel("If using dated copies, keep last"))
        row_k.addWidget(keep)
        row_k.addWidget(QLabel("files"))
        row_k.addStretch()
        af.addLayout(row_k)
        def _sync_keep_enabled():
            keep.setEnabled(mode.currentData() == "rotate")
        mode.currentIndexChanged.connect(lambda *_: _sync_keep_enabled())
        _sync_keep_enabled()

        on_exit = QCheckBox("Also save a final backup when I close NextGen ERP (if anything changed)")
        on_exit.setChecked(self.store.setting("backup_on_exit", "1") == "1")
        af.addWidget(on_exit)

        level, status_text = self.backups.status()
        last_ok = self.backups.last_success()
        last_lab = QLabel("Status: " + status_text.replace("●  ", "") + (f"\nLast successful backup: {last_ok['t']}  ·  {format_bytes(last_ok['bytes'])}  ·  {last_ok['path']}" if last_ok else ""))
        last_lab.setWordWrap(True)
        last_lab.setStyleSheet(f"color:{self.palette['muted']};")
        af.addWidget(last_lab)
        same_drive = QLabel("⚠ This folder is on the same drive as your business data. If the drive fails you lose both — pick a USB drive, network folder or cloud-synced folder.")
        same_drive.setWordWrap(True)
        same_drive.setStyleSheet(f"color:{self.palette['orange']};")
        def _check_drive():
            try:
                same = Path(folder.text().strip() or str(APP_DIR / "backups")).resolve().drive.lower() == APP_DIR.resolve().drive.lower()
            except OSError:
                same = False
            same_drive.setVisible(same)
        af.addWidget(same_drive)

        save_sched = QPushButton("Save backup settings")
        save_sched.setObjectName("primary")
        def save_schedule():
            target = folder.text().strip() or str(APP_DIR / "backups")
            try:
                Path(target).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "Folder not usable", str(exc))
                return
            self.store.set_setting("auto_backup_enabled", "1" if enabled.isChecked() else "0")
            self.store.set_setting("auto_backup_interval_minutes", str(int(freq.currentData())))
            self.store.set_setting("auto_backup_folder", target)
            self.store.set_setting("auto_backup_keep", str(keep.value()))
            self.store.set_setting("auto_backup_mode", str(mode.currentData()))
            self.store.set_setting("backup_on_exit", "1" if on_exit.isChecked() else "0")
            self.setup_auto_backup_timer()
            self.store.log(
                self.username,
                "Backup schedule",
                f"enabled={enabled.isChecked()} {mode.currentText()} · {freq.currentText()} → {target}",
            )
            QMessageBox.information(
                self,
                "Backup settings saved",
                f"Real-time Backups are {'ON' if enabled.isChecked() else 'OFF'}.\n"
                f"How often: {freq.currentText()}\nStyle: {mode.currentText()}\nFolder: {target}\n\n"
                "You will get a notice after every backup. Backups run while NextGen ERP is open "
                "(and once more when you close it).",
            )
            self.pages.pop("Database and Backup", None)
            self.show_page("Database and Backup")
        save_sched.clicked.connect(save_schedule)

        run_now = QPushButton("Back up now")
        def do_run_now():
            if folder.text().strip():
                self.store.set_setting("auto_backup_folder", folder.text().strip())
            self.backups.run_now("manual")
        run_now.clicked.connect(do_run_now)

        brow = QPushButton("Open backup folder")
        def open_folder():
            path = self.store.auto_backup_folder_path()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        brow.clicked.connect(open_folder)

        row_a = QHBoxLayout()
        row_a.addWidget(save_sched)
        row_a.addWidget(run_now)
        row_a.addWidget(brow)
        logs_btn = QPushButton("Open log folder")
        logs_btn.setToolTip("Send nextgen_erp.log to support if something goes wrong.")
        logs_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir(APP_DIR)))))
        row_a.addWidget(logs_btn)
        row_a.addStretch()
        af.addLayout(row_a)
        v.addWidget(auto)

        # --- Optional private GitHub mirror ---
        gh = self.panel()
        gf = QVBoxLayout(gh)
        gf.setContentsMargins(20, 20, 20, 20)
        gf.setSpacing(10)
        gf.addWidget(QLabel("<b>Private GitHub copy (optional)</b>"))
        gf.addWidget(QLabel(
            "After each local backup, overwrite one file in a private repository. "
            "Create an empty private repo, then a fine-grained token with Contents read/write on that repo only. "
            "Best with overwrite mode so the cloud always has the latest package — not a long git history of ZIPs."
        ))
        gh_enabled = QCheckBox("Also upload to private GitHub after each backup")
        gh_enabled.setChecked(self.store.setting("github_backup_enabled", "0") == "1")
        gf.addWidget(gh_enabled)
        gh_repo = QLineEdit(self.store.setting("github_backup_repo", ""))
        gh_repo.setPlaceholderText("owner/repository  e.g. mycompany/nextgen-erp-backups")
        gh_token = QLineEdit("")
        gh_token.setEchoMode(QLineEdit.Password)
        gh_token.setPlaceholderText("Token saved securely on this PC — leave blank to keep it" if self.store.github_token_is_set() else "GitHub personal access token")
        gh_path = QLineEdit(self.store.setting("github_backup_path", "backups/nextgen_erp_latest.zip"))
        gh_path.setPlaceholderText("backups/nextgen_erp_latest.zip")
        form_gh = QFormLayout()
        form_gh.addRow("Repository", gh_repo)
        form_gh.addRow("Access token", gh_token)
        form_gh.addRow("File path in repo", gh_path)
        gf.addLayout(form_gh)
        gh_status = QLabel("Last GitHub upload: " + (self.store.setting("github_backup_last_status", "") or "Never"))
        gh_status.setStyleSheet(f"color:{self.palette['muted']};")
        gh_status.setWordWrap(True)
        gf.addWidget(gh_status)
        save_gh = QPushButton("Save GitHub settings")
        def save_github():
            self.store.set_setting("github_backup_enabled", "1" if gh_enabled.isChecked() else "0")
            self.store.set_setting("github_backup_repo", gh_repo.text().strip())
            if gh_token.text().strip():
                self.store.set_github_token(gh_token.text())
            self.store.set_setting("github_backup_path", (gh_path.text().strip() or "backups/nextgen_erp_latest.zip"))
            self.store.log(self.username, "GitHub backup settings", f"enabled={gh_enabled.isChecked()} repo={gh_repo.text().strip()}")
            QMessageBox.information(self, "Saved", "GitHub backup settings saved. The token is encrypted for this Windows user and is only sent to api.github.com.")
            self.pages.pop("Database and Backup", None)
            self.show_page("Database and Backup")
        save_gh.clicked.connect(save_github)
        test_gh = QPushButton("Test upload now")
        def test_github():
            save_github()
            try:
                if folder.text().strip():
                    self.store.set_setting("auto_backup_folder", folder.text().strip())
                path = self.store.run_auto_backup(reason="github-test")
                QMessageBox.information(self, "Upload OK", f"Local + GitHub backup finished.\n{path}")
                self.backups._counter_at_last_backup = self.store.change_counter()
            except Exception as exc:
                QMessageBox.warning(self, "GitHub test failed", str(exc))
            self.pages.pop("Database and Backup", None)
            self.show_page("Database and Backup")
        test_gh.clicked.connect(test_github)
        row_gh = QHBoxLayout()
        row_gh.addWidget(save_gh)
        row_gh.addWidget(test_gh)
        row_gh.addStretch()
        gf.addLayout(row_gh)
        v.addWidget(gh)

        hist_panel = self.panel()
        hv = QVBoxLayout(hist_panel); hv.setContentsMargins(20, 20, 20, 20); hv.setSpacing(8)
        hv.addWidget(QLabel("<b>Backup history</b> (last 30 runs)"))
        history = self.store.backup_history()
        htable = self._table(["WHEN", "RESULT", "WHY", "SIZE", "DETAILS"])
        self._fill_table(htable, history, lambda e: [e.get("t", ""), "✔ Saved" if e.get("ok") else "✖ Failed", e.get("reason", ""),
                                                     format_bytes(e.get("bytes", 0)) if e.get("ok") else "", (e.get("note") or e.get("path") or "")[:140]])
        htable.setMinimumHeight(200)
        hv.addWidget(htable)
        v.addWidget(hist_panel)

        if is_admin:
            p = self.panel()
            f = QVBoxLayout(p)
            f.setContentsMargins(20, 20, 20, 20)
            vals = [
                ("System database", str(DB_PATH)),
                ("Active workspace database", str(self.store.workspace_path)),
                ("Stored records", str(self.store.operations.conn.execute("SELECT COUNT(*) FROM workspace_records").fetchone()[0])),
                ("Queued changes", str(len(self.store.pending()))),
                ("Enabled modules", str(len(self.store.features(True)))),
            ]
            for k, val in vals:
                f.addWidget(QLabel(f"<span style='color:{self.palette['muted']};font-size:8pt'>{k.upper()}</span>"))
                lab = QLabel(val)
                lab.setWordWrap(True)
                f.addWidget(lab)
            v.addWidget(p)
            actions = QHBoxLayout()
            b = QPushButton("Export full database package")
            b.setObjectName("primary")
            b.setToolTip("Exports system settings, users, module configuration, and all business workspaces.")
            b.clicked.connect(self.backup)
            actions.addWidget(b)
            restore = QPushButton("Import / restore database package")
            restore.setToolTip("Replaces local databases from a verified package after making a safety backup.")
            restore.clicked.connect(self.restore_backup)
            actions.addWidget(restore)
            reset = QPushButton("Reset shop data…")
            reset.setObjectName("danger")
            reset.setToolTip("Clear operational data after creating a verified backup. Keeps users and shop configuration.")
            reset.clicked.connect(self.reset_shop_data)
            actions.addWidget(reset)
            actions.addStretch()
            v.addLayout(actions)
            note = QLabel(
                "Admin-only export/import: Export creates a verified ZIP of the system database and every workspace. "
                "Import validates the package, creates a safety backup, then replaces local data (full restore, not a merge). "
                "Automatic backups above use the same package format."
            )
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{self.palette['muted']};padding:8px;")
            v.addWidget(note)
        else:
            note = QLabel(
                "As Manager you can set the schedule, folder, and run a backup now. "
                "Restoring a package or resetting shop data requires an Administrator."
            )
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{self.palette['muted']};padding:8px;")
            v.addWidget(note)

        v.addStretch()
        return w


    def backup(self):
        if self.role != "Admin":
            QMessageBox.warning(self, "Access denied", "Only an administrator can export a full database package.")
            return
        if not self._authorize('backup'):
            return
        default_path = APP_DIR / f"nextgen_erp_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Save full system + all business workspaces backup", str(default_path), "NextGen ERP backup (*.zip)")
        if not path:
            return
        if Path(path).resolve() in {Path(DB_PATH).resolve(), Path(self.store.workspace_path).resolve()}:
            QMessageBox.warning(self,"Invalid backup destination","Choose a separate backup file, not an active database file.")
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            self.store.write_backup_archive(path)
            self.store.log(self.username, "Database backup", path)
            QMessageBox.information(self, "Backup complete", "A verified backup containing the system database and all isolated business workspaces was saved to:\n" + path)
        except Exception as e:
            QMessageBox.critical(self, "Backup failed", str(e))
    def reset_shop_data(self):
        if self.role != "Admin":
            QMessageBox.warning(self,"Access denied","Only an administrator can reset shop data.")
            return
        if not self._authorize("configure"):
            return
        business_name=(self.store.setting("business_name","Default business").strip() or "Default business")
        expected=f"RESET {business_name}"
        detail=("This will permanently clear the ACTIVE SHOP WORKSPACE operational records: products/stock, customers, suppliers, sales, purchases, quotes, cashbook entries, service/repair jobs, and custom-module records.\n\n"
                "It will KEEP: all user accounts and passwords, the active business/profile, branding/tax settings, roles and permissions, module definitions, SKU numbering configuration, website connection settings and the cached website catalogue.\n\n"
                "A full backup of the system and all workspaces will be created first. Data already synchronized to an external server is not deleted by this local reset.\n\n"
                f"To confirm, type exactly:\n{expected}")
        typed,ok=QInputDialog.getText(self,"Reset shop data",detail,QLineEdit.Normal,"")
        if not ok:
            return
        try:
            result=self.store.reset_shop_data(typed)
            self.store.log(self.username,"Reset shop data",f"Cleared {result['total_deleted']} row(s); safety backup {result['backup_path']}")
            self.pages.clear()
            self.rebuild_nav()
            self.show_page("Database and Backup")
            self.refresh_syncpill()
            breakdown="\n".join(f"{name}: {count}" for name,count in sorted(result["deleted_rows"].items()) if count)
            QMessageBox.information(self,"Shop data reset complete",
                f"Operational data was cleared.\nRows cleared: {result['total_deleted']:,}\n\nBackup saved to:\n{result['backup_path']}\n\n"
                + ("Deleted-row summary:\n"+breakdown if breakdown else "No existing operational rows needed clearing.")
                + "\n\nYour users, shop settings, module definitions and website catalogue connection were preserved.")
        except (ValueError,PermissionError,sqlite3.Error,RuntimeError,OSError) as exc:
            QMessageBox.critical(self,"Reset failed",f"The shop data reset did not complete. If the backup was created, it remains available.\n\n{exc}")

    def restore_backup(self):
        if self.role != "Admin":
            QMessageBox.warning(self, "Access denied", "Only an administrator can import/restore a full database package.")
            return
        if not self._authorize("backup"): return
        path,_=QFileDialog.getOpenFileName(self,"Choose a NextGen ERP backup",str(APP_DIR),"NextGen ERP backup (*.zip *.db);;Backup bundle (*.zip);;Legacy database (*.db)")
        if not path: return
        answer=QMessageBox.question(self,"Restore database backup",
            "The selected backup will replace the current database. A safety copy of the current database will be created first. Continue?",
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer != QMessageBox.Yes: return
        try:
            safety_path=self.store.restore_backup(path)
            self.store.log(self.username,"Restored database backup",path)
            QMessageBox.information(self,"Restore complete",
                f"The backup was restored. Your previous database is preserved at:\\n{safety_path}\\n\\nSign in again to continue.")
            self.sign_out()
        except (ValueError,sqlite3.Error,RuntimeError) as exc:
            QMessageBox.critical(self,"Restore failed",f"The live database was not replaced.\\n\\n{exc}")

    def _ui(self,fn): QTimer.singleShot(0,fn)
    def search_records(self):
        q=self.search.text().strip().lower()
        if not q: return
        allowed_modules={r["name"] for r in self.store.features(True) if self.store.role_has_module(self.role,r["name"])}
        rows=[r for r in self.store.records(limit=1000) if r["module"] in allowed_modules and (q in r["module"].lower() or q in r["data_json"].lower() or q in str(r["id"]))]
        w,v=self.page_container("Search results",f"{len(rows)} local records match “{self.search.text().strip()}”.")
        table=QTableWidget(); table.setColumnCount(4); table.setHorizontalHeaderLabels(["ID","MODULE","DETAILS","CREATED"]); table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents); table.horizontalHeader().setStretchLastSection(True)
        table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            vals=[r["id"],r["module"],r["data_json"][:200],r["created_at"]]
            for j,val in enumerate(vals): table.setItem(i,j,QTableWidgetItem(str(val)))
        v.addWidget(table,1)
        self.pages["Search results"]=w; self.stack.addWidget(w); self.stack.setCurrentWidget(w)
    def sign_out(self):
        self.store.log(self.username,"Sign out","Desktop session ended"); self.close()
        self.login_again()
    def login_again(self):
        if self.store.needs_initial_setup():
            setup=InitialSetupDialog(self.store)
            if not setup.exec():
                QApplication.quit()
                return
        self.login_dlg=LoginDialog(self.store)
        if self.login_dlg.exec():
            self.username=self.login_dlg.username; self.role=self.login_dlg.role; self.display_name=getattr(self.login_dlg,'display_name',self.login_dlg.username); self.store.actor_role=self.role; self.store.operations.actor_role=self.role; self.store.operations.actor_username=self.username; self.profile_chip.setText(f'  {self.display_name}  ·  {self.role}  ')
            self.pages.clear(); self.rebuild_nav(); self.show_page("Dashboard")
            self.show()
        else: QApplication.quit()


class ButtonCursorFilter(QObject):
    """Use the familiar hand pointer whenever the user hovers an actionable button."""
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Enter and isinstance(watched, QPushButton):
            watched.setCursor(Qt.PointingHandCursor)
        return False


def main():
    setup_logging(APP_DIR)
    app=QApplication(sys.argv); app.setApplicationName(APP_NAME); app.setStyle("Fusion")
    brand_icon=Path(__file__).resolve().parent/"assets"/"nextgen-erp-mark.svg"
    if brand_icon.exists(): app.setWindowIcon(QIcon(str(brand_icon)))
    def crash_dialog(summary, log_path):
        QMessageBox.critical(None,"NextGen ERP hit a problem",
            f"Something unexpected happened:\n\n{summary[:300]}\n\nYour saved data is safe and Real-time Backups are unaffected. "
            f"Details were written to:\n{log_path}\n\nIf it keeps happening, send that file to support.")
    install_crash_handler(APP_DIR,crash_dialog)
    cursor_filter = ButtonCursorFilter(app)
    app.installEventFilter(cursor_filter)
    store=Store()
    app.aboutToQuit.connect(store.close)
    app.setStyleSheet(themed_style(palette_for(store)))
    if store.needs_initial_setup():
        setup = InitialSetupDialog(store)
        if not setup.exec():
            store.conn.close()
            return
    login=LoginDialog(store)
    if login.exec():
        store.log(login.username,"Sign in","Desktop session opened")
        window=App(store,login.username,login.role,getattr(login,'display_name',login.username)); window.show()
        sys.exit(app.exec())
    store.conn.close()
if __name__=="__main__": main()





