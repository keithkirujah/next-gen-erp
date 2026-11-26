"""Transactional offline operations for stock, sales returns, purchasing, and cash movements."""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import sqlite3
import json
import csv
import re
from pathlib import Path
from datetime import datetime


def _as_dict(row):
    """Convert sqlite3.Row (or None) to a plain dict so callers can safely use .get()."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


def _as_dicts(rows):
    """Convert a sequence of sqlite3.Row objects to a list of plain dicts."""
    return [dict(r) for r in rows]


class OperationError(ValueError):
    """A business rule or validation error suitable for showing to an operator."""


class TaxedLine(tuple):
    """A normalized sales line ``(product, qty, price, amount)`` that also carries its exact tax.

    Unpacks like a plain 4-tuple everywhere; ``.tax`` is used when prices include tax so that
    net + tax equals the shelf price to the cent.
    """
    def __new__(cls, product, qty, price, amount, tax):
        obj=super().__new__(cls,(product,qty,price,amount))
        obj.tax=tax
        return obj


class OperationsService:
    SCHEMA_VERSION = 21

    def __init__(self, connection):
        self.conn = connection
        self.actor_role = "Admin"
        self.queue_writer = None
        self.actor_username = "system"
        self.permission_provider = lambda role: {"view", "create", "edit", "delete", "export", "print", "configure", "manage_users", "backup", "sync", "manage_inventory", "manage_finance", "process_returns"} if role == "Admin" else set()
        self.module_access_provider = lambda role, module: True
        self.tax_provider = lambda: {"label":"Tax","rate":Decimal("0.00")}
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    def _require(self, capability):
        if capability not in self.permission_provider(self.actor_role):
            raise OperationError(f"The {self.actor_role} role is not allowed to {capability.replace('_', ' ')}.")

    def _require_module(self, module):
        if not self.module_access_provider(self.actor_role,module):
            raise OperationError(f"The {self.actor_role} role cannot access the {module} module.")

    def _require_any_module(self, modules):
        if not any(self.module_access_provider(self.actor_role,module) for module in modules):
            names=" or ".join(modules)
            raise OperationError(f"The {self.actor_role} role has no access to {names}.")

    def initialize(self):
        version=self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(f"Database version {version} is newer than this application.")
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS customers(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    contact TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS suppliers(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    contact TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS academic_students(
                    id INTEGER PRIMARY KEY,
                    admission_number TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    full_name TEXT NOT NULL,
                    class_course TEXT NOT NULL DEFAULT '',
                    date_of_birth TEXT NOT NULL DEFAULT '',
                    guardian_name TEXT NOT NULL DEFAULT '',
                    guardian_contact TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','On leave','Completed','Withdrawn')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_academic_students_class ON academic_students(class_course COLLATE NOCASE,status,id);
                CREATE TABLE IF NOT EXISTS academic_timetable(
                    id INTEGER PRIMARY KEY,
                    class_course TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    teacher TEXT NOT NULL DEFAULT '',
                    room TEXT NOT NULL DEFAULT '',
                    weekday TEXT NOT NULL CHECK(weekday IN ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')),
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    term TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Scheduled' CHECK(status IN ('Scheduled','Cancelled')),
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_academic_timetable_day_time ON academic_timetable(weekday,start_time,end_time,status);
                CREATE INDEX IF NOT EXISTS idx_academic_timetable_class_term ON academic_timetable(class_course COLLATE NOCASE,term COLLATE NOCASE,status);
                CREATE TABLE IF NOT EXISTS academic_years(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Planning' CHECK(status IN ('Planning','Active','Closed')),
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(start_date<=end_date)
                );
                CREATE TABLE IF NOT EXISTS academic_terms(
                    id INTEGER PRIMARY KEY,
                    academic_year_id INTEGER NOT NULL REFERENCES academic_years(id),
                    name TEXT NOT NULL COLLATE NOCASE,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Planning' CHECK(status IN ('Planning','Active','Closed')),
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(academic_year_id,name),
                    CHECK(start_date<=end_date)
                );
                CREATE INDEX IF NOT EXISTS idx_academic_terms_dates ON academic_terms(start_date,end_date,status);
                CREATE TABLE IF NOT EXISTS academic_classes(
                    id INTEGER PRIMARY KEY,
                    academic_year_id INTEGER NOT NULL REFERENCES academic_years(id),
                    class_code TEXT NOT NULL COLLATE NOCASE,
                    name TEXT NOT NULL,
                    section TEXT NOT NULL DEFAULT '',
                    homeroom_teacher TEXT NOT NULL DEFAULT '',
                    capacity INTEGER NOT NULL DEFAULT 0 CHECK(capacity>=0),
                    status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active','Archived')),
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(academic_year_id,class_code)
                );
                CREATE INDEX IF NOT EXISTS idx_academic_classes_year ON academic_classes(academic_year_id,status,name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS student_class_enrollments(
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL REFERENCES academic_students(id),
                    academic_class_id INTEGER NOT NULL REFERENCES academic_classes(id),
                    academic_year_id INTEGER NOT NULL REFERENCES academic_years(id),
                    status TEXT NOT NULL DEFAULT 'Enrolled' CHECK(status IN ('Enrolled','Transferred','Completed','Withdrawn')),
                    enrolled_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL DEFAULT '',
                    changed_by TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_student_current_class_per_year ON student_class_enrollments(student_id,academic_year_id) WHERE status='Enrolled';
                CREATE INDEX IF NOT EXISTS idx_class_roster ON student_class_enrollments(academic_class_id,status,student_id);
                CREATE TABLE IF NOT EXISTS admission_applications(
                    id INTEGER PRIMARY KEY,
                    application_number TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    applicant_name TEXT NOT NULL,
                    date_of_birth TEXT NOT NULL DEFAULT '',
                    desired_class TEXT NOT NULL DEFAULT '',
                    guardian_name TEXT NOT NULL DEFAULT '',
                    guardian_contact TEXT NOT NULL DEFAULT '',
                    guardian_email TEXT NOT NULL DEFAULT '',
                    applied_on TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Submitted' CHECK(status IN ('Submitted','Under review','Waitlisted','Accepted','Rejected','Withdrawn','Enrolled')),
                    decision_notes TEXT NOT NULL DEFAULT '',
                    converted_student_id INTEGER REFERENCES academic_students(id),
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admission_applications_status ON admission_applications(status,applied_on,id);
                CREATE TABLE IF NOT EXISTS student_guardians(
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL REFERENCES academic_students(id),
                    full_name TEXT NOT NULL,
                    relationship TEXT NOT NULL DEFAULT '',
                    contact TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_student_guardians_student ON student_guardians(student_id,is_primary,id);
                CREATE TABLE IF NOT EXISTS guardian_communications(
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL REFERENCES academic_students(id),
                    guardian_id INTEGER REFERENCES student_guardians(id),
                    channel TEXT NOT NULL CHECK(channel IN ('Phone','Email','SMS','Meeting','Letter','Other')),
                    direction TEXT NOT NULL CHECK(direction IN ('Outgoing','Incoming')),
                    subject TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    follow_up_date TEXT NOT NULL DEFAULT '',
                    logged_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_guardian_communications_student ON guardian_communications(student_id,created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS appointments(
                    id INTEGER PRIMARY KEY,
                    customer_name TEXT NOT NULL,
                    customer_contact TEXT NOT NULL DEFAULT '',
                    service_name TEXT NOT NULL,
                    staff_member TEXT NOT NULL DEFAULT '',
                    starts_at TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL DEFAULT 30 CHECK(duration_minutes BETWEEN 5 AND 1440),
                    status TEXT NOT NULL DEFAULT 'Booked' CHECK(status IN ('Booked','Confirmed','Arrived','In service','Completed','No show','Cancelled')),
                    notes TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_appointments_start ON appointments(starts_at,status,id);
                CREATE INDEX IF NOT EXISTS idx_appointments_staff_start ON appointments(staff_member,starts_at,status);
                CREATE TABLE IF NOT EXISTS student_attendance(
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL REFERENCES academic_students(id),
                    attendance_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('Present','Absent','Late','Excused')),
                    note TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(student_id,attendance_date)
                );
                CREATE INDEX IF NOT EXISTS idx_student_attendance_date ON student_attendance(attendance_date,student_id);
                CREATE TABLE IF NOT EXISTS academic_assessments(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    class_course TEXT NOT NULL DEFAULT '',
                    term TEXT NOT NULL DEFAULT '',
                    assessment_date TEXT NOT NULL DEFAULT '',
                    max_score TEXT NOT NULL DEFAULT '100',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_academic_assessments_class ON academic_assessments(class_course COLLATE NOCASE,term,id);
                CREATE TABLE IF NOT EXISTS academic_results(
                    id INTEGER PRIMARY KEY,
                    assessment_id INTEGER NOT NULL REFERENCES academic_assessments(id),
                    student_id INTEGER NOT NULL REFERENCES academic_students(id),
                    score TEXT NOT NULL,
                    grade TEXT NOT NULL DEFAULT '',
                    remarks TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    UNIQUE(assessment_id,student_id)
                );
                CREATE INDEX IF NOT EXISTS idx_academic_results_assessment ON academic_results(assessment_id,student_id);
                CREATE TABLE IF NOT EXISTS student_fee_charges(
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL REFERENCES academic_students(id),
                    term TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    due_date TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_student_fee_charges_student ON student_fee_charges(student_id,id);
                CREATE TABLE IF NOT EXISTS student_fee_payments(
                    id INTEGER PRIMARY KEY,
                    student_id INTEGER NOT NULL REFERENCES academic_students(id),
                    amount TEXT NOT NULL,
                    method TEXT NOT NULL DEFAULT 'Cash',
                    reference TEXT NOT NULL DEFAULT '',
                    received_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_student_fee_payments_student ON student_fee_payments(student_id,id);
                CREATE TABLE IF NOT EXISTS student_fee_allocations(
                    id INTEGER PRIMARY KEY,
                    payment_id INTEGER NOT NULL REFERENCES student_fee_payments(id),
                    charge_id INTEGER NOT NULL REFERENCES student_fee_charges(id),
                    amount TEXT NOT NULL,
                    UNIQUE(payment_id,charge_id)
                );
                CREATE TABLE IF NOT EXISTS products(
                    id INTEGER PRIMARY KEY,
                    sku TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT 'each',
                    category TEXT NOT NULL DEFAULT '',
                    barcode TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    brand TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    supplier_code TEXT NOT NULL DEFAULT '',
                    variant_group TEXT NOT NULL DEFAULT '',
                    variant_attributes_json TEXT NOT NULL DEFAULT '{}',
                    selling_price TEXT NOT NULL,
                    cost_price TEXT NOT NULL,
                    reorder_level TEXT NOT NULL DEFAULT '0',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    track_batches INTEGER NOT NULL DEFAULT 0,
                    track_stock INTEGER NOT NULL DEFAULT 1
                );
                CREATE TRIGGER IF NOT EXISTS trg_products_sku_unique_insert
                BEFORE INSERT ON products
                WHEN EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku COLLATE NOCASE)
                BEGIN
                    SELECT RAISE(ABORT, 'duplicate SKU (case-insensitive)');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_products_sku_unique_update
                BEFORE UPDATE OF sku ON products
                WHEN EXISTS(SELECT 1 FROM products WHERE sku=NEW.sku COLLATE NOCASE AND id<>OLD.id)
                BEGIN
                    SELECT RAISE(ABORT, 'duplicate SKU (case-insensitive)');
                END;
                CREATE TABLE IF NOT EXISTS product_barcodes(
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    barcode TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_product_barcodes_product ON product_barcodes(product_id,id);
                CREATE TABLE IF NOT EXISTS inventory_batches(
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    lot_number TEXT NOT NULL COLLATE NOCASE,
                    expiry_date TEXT NOT NULL DEFAULT '',
                    received_quantity TEXT NOT NULL,
                    remaining_quantity TEXT NOT NULL,
                    supplier_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'In stock' CHECK(status IN ('In stock','Quarantined','Disposed','Cancelled')),
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    source_id INTEGER,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(product_id,lot_number)
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_batches_expiry ON inventory_batches(expiry_date,status,product_id);
                CREATE INDEX IF NOT EXISTS idx_inventory_batches_product ON inventory_batches(product_id,status,id);
                CREATE TABLE IF NOT EXISTS sale_line_batch_allocations(
                    id INTEGER PRIMARY KEY,
                    sale_line_id INTEGER NOT NULL REFERENCES sale_lines(id),
                    batch_id INTEGER NOT NULL REFERENCES inventory_batches(id),
                    quantity TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sale_batch_allocation_line ON sale_line_batch_allocations(sale_line_id,id);
                CREATE TABLE IF NOT EXISTS sale_return_batch_allocations(
                    id INTEGER PRIMARY KEY,
                    return_line_id INTEGER NOT NULL REFERENCES sale_return_lines(id),
                    batch_id INTEGER NOT NULL REFERENCES inventory_batches(id),
                    quantity TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sku_settings(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    default_prefix TEXT NOT NULL DEFAULT 'GEN',
                    padding INTEGER NOT NULL DEFAULT 6,
                    category_prefixes_json TEXT NOT NULL DEFAULT '{}'
                );
                INSERT OR IGNORE INTO sku_settings(id,default_prefix,padding,category_prefixes_json) VALUES(1,'GEN',6,'{}');
                CREATE TABLE IF NOT EXISTS sku_sequences(
                    prefix TEXT PRIMARY KEY COLLATE NOCASE,
                    next_value INTEGER NOT NULL DEFAULT 1 CHECK(next_value >= 1)
                );
                CREATE TABLE IF NOT EXISTS product_sku_history(
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    old_sku TEXT NOT NULL,
                    new_sku TEXT NOT NULL,
                    changed_by TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    changed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_product_sku_history_product ON product_sku_history(product_id,id);
                CREATE TABLE IF NOT EXISTS stock_movements(
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    quantity_delta TEXT NOT NULL,
                    movement_type TEXT NOT NULL,
                    reference_type TEXT NOT NULL DEFAULT '',
                    reference_id INTEGER,
                    note TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sales(
                    id INTEGER PRIMARY KEY,
                    sale_number TEXT NOT NULL UNIQUE,
                    customer_id INTEGER REFERENCES customers(id),
                    customer_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payment_status TEXT NOT NULL,
                    total TEXT NOT NULL,
                    paid_total TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cancelled_at TEXT,
                    cancellation_reason TEXT DEFAULT '',
                    quote_id INTEGER,
                    subtotal TEXT NOT NULL DEFAULT '0.00',
                    tax_rate TEXT NOT NULL DEFAULT '0.00',
                    tax_total TEXT NOT NULL DEFAULT '0.00',
                    tax_label TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS quotes(
                    id INTEGER PRIMARY KEY,
                    quote_number TEXT NOT NULL UNIQUE,
                    customer_id INTEGER REFERENCES customers(id),
                    customer_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    total TEXT NOT NULL,
                    expires_on TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    converted_sale_id INTEGER,
                    created_by TEXT NOT NULL DEFAULT '',
                    subtotal TEXT NOT NULL DEFAULT '0.00',
                    tax_rate TEXT NOT NULL DEFAULT '0.00',
                    tax_total TEXT NOT NULL DEFAULT '0.00',
                    tax_label TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS quote_lines(
                    id INTEGER PRIMARY KEY,
                    quote_id INTEGER NOT NULL REFERENCES quotes(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    sku TEXT NOT NULL,
                    description TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    unit_price TEXT NOT NULL,
                    line_total TEXT NOT NULL,
                    tax_amount TEXT NOT NULL DEFAULT '0.00'
                );
                CREATE TABLE IF NOT EXISTS sale_lines(
                    id INTEGER PRIMARY KEY,
                    sale_id INTEGER NOT NULL REFERENCES sales(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    sku TEXT NOT NULL,
                    description TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    unit_price TEXT NOT NULL,
                    line_total TEXT NOT NULL,
                    tax_amount TEXT NOT NULL DEFAULT '0.00'
                );
                CREATE TABLE IF NOT EXISTS restaurant_menu_items(
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
                    category TEXT NOT NULL DEFAULT '',
                    kitchen_station TEXT NOT NULL DEFAULT 'Kitchen',
                    prep_minutes INTEGER NOT NULL DEFAULT 10 CHECK(prep_minutes BETWEEN 0 AND 1440),
                    available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0,1)),
                    recipe_required INTEGER NOT NULL DEFAULT 1 CHECK(recipe_required IN (0,1)),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_restaurant_menu_available ON restaurant_menu_items(available,category,id);
                CREATE TABLE IF NOT EXISTS restaurant_recipe_lines(
                    id INTEGER PRIMARY KEY,
                    menu_item_id INTEGER NOT NULL REFERENCES restaurant_menu_items(id) ON DELETE CASCADE,
                    ingredient_product_id INTEGER NOT NULL REFERENCES products(id),
                    quantity_per_serving TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    UNIQUE(menu_item_id,ingredient_product_id)
                );
                CREATE INDEX IF NOT EXISTS idx_restaurant_recipe_menu ON restaurant_recipe_lines(menu_item_id,id);
                CREATE TABLE IF NOT EXISTS restaurant_kitchen_tickets(
                    id INTEGER PRIMARY KEY,
                    sale_id INTEGER NOT NULL REFERENCES sales(id),
                    sale_line_id INTEGER NOT NULL REFERENCES sale_lines(id),
                    menu_item_id INTEGER NOT NULL REFERENCES restaurant_menu_items(id),
                    item_name TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    kitchen_station TEXT NOT NULL DEFAULT 'Kitchen',
                    status TEXT NOT NULL DEFAULT 'Queued' CHECK(status IN ('Queued','Preparing','Ready','Served','Cancelled')),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kitchen_ticket_status ON restaurant_kitchen_tickets(status,created_at,id);
                CREATE TABLE IF NOT EXISTS restaurant_kitchen_events(
                    id INTEGER PRIMARY KEY,
                    ticket_id INTEGER NOT NULL REFERENCES restaurant_kitchen_tickets(id),
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    changed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sale_recipe_consumptions(
                    id INTEGER PRIMARY KEY,
                    sale_line_id INTEGER NOT NULL REFERENCES sale_lines(id),
                    menu_item_id INTEGER NOT NULL REFERENCES restaurant_menu_items(id),
                    ingredient_product_id INTEGER NOT NULL REFERENCES products(id),
                    quantity TEXT NOT NULL,
                    stock_movement_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(sale_line_id,ingredient_product_id)
                );
                CREATE TABLE IF NOT EXISTS recipe_sale_batch_allocations(
                    id INTEGER PRIMARY KEY,
                    consumption_id INTEGER NOT NULL REFERENCES sale_recipe_consumptions(id),
                    batch_id INTEGER NOT NULL REFERENCES inventory_batches(id),
                    quantity TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recipe_consumption_line ON sale_recipe_consumptions(sale_line_id,id);
                CREATE TABLE IF NOT EXISTS sale_recipe_reversals(
                    id INTEGER PRIMARY KEY,
                    consumption_id INTEGER NOT NULL REFERENCES sale_recipe_consumptions(id),
                    sale_id INTEGER NOT NULL REFERENCES sales(id),
                    quantity TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recipe_sale_batch_reversals(
                    id INTEGER PRIMARY KEY,
                    consumption_id INTEGER NOT NULL REFERENCES sale_recipe_consumptions(id),
                    batch_id INTEGER NOT NULL REFERENCES inventory_batches(id),
                    sale_id INTEGER NOT NULL REFERENCES sales(id),
                    quantity TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS purchases(
                    id INTEGER PRIMARY KEY,
                    purchase_number TEXT NOT NULL UNIQUE,
                    supplier_id INTEGER REFERENCES suppliers(id),
                    supplier_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payment_status TEXT NOT NULL,
                    total TEXT NOT NULL,
                    paid_total TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cancelled_at TEXT,
                    cancellation_reason TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS purchase_lines(
                    id INTEGER PRIMARY KEY,
                    purchase_id INTEGER NOT NULL REFERENCES purchases(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    sku TEXT NOT NULL,
                    description TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    unit_cost TEXT NOT NULL,
                    line_total TEXT NOT NULL,
                    purchase_order_line_id INTEGER,
                    prior_cost TEXT
                );
                CREATE TABLE IF NOT EXISTS purchase_orders(
                    id INTEGER PRIMARY KEY,
                    order_number TEXT NOT NULL UNIQUE,
                    supplier_id INTEGER REFERENCES suppliers(id),
                    supplier_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cancelled_at TEXT,
                    cancellation_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS purchase_order_lines(
                    id INTEGER PRIMARY KEY,
                    purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    sku TEXT NOT NULL,
                    description TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    unit_cost TEXT NOT NULL,
                    received_quantity TEXT NOT NULL DEFAULT '0'
                );
                CREATE TABLE IF NOT EXISTS cash_entries(
                    id INTEGER PRIMARY KEY,
                    direction TEXT NOT NULL CHECK(direction IN ('in','out')),
                    amount TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    payment_method TEXT NOT NULL DEFAULT 'Unspecified',
                    reference TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_stock_movements_product ON stock_movements(product_id,id);
                CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at,id);
                CREATE INDEX IF NOT EXISTS idx_purchases_created ON purchases(created_at,id);
                CREATE INDEX IF NOT EXISTS idx_cash_entries_created ON cash_entries(created_at,id);
                CREATE TABLE IF NOT EXISTS sales_returns(
                    id INTEGER PRIMARY KEY,
                    return_number TEXT NOT NULL UNIQUE,
                    sale_id INTEGER NOT NULL REFERENCES sales(id),
                    refund_amount TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sale_return_lines(
                    id INTEGER PRIMARY KEY,
                    return_id INTEGER NOT NULL REFERENCES sales_returns(id),
                    sale_line_id INTEGER NOT NULL REFERENCES sale_lines(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    sku TEXT NOT NULL,
                    description TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    restocked INTEGER NOT NULL CHECK(restocked IN (0,1)),
                    unit_price TEXT NOT NULL,
                    line_total TEXT NOT NULL,
                    tax_amount TEXT NOT NULL DEFAULT '0.00'
                );
                CREATE INDEX IF NOT EXISTS idx_sales_returns_sale ON sales_returns(sale_id,id);
                CREATE INDEX IF NOT EXISTS idx_sale_return_lines_line ON sale_return_lines(sale_line_id,id);
                CREATE TABLE IF NOT EXISTS repair_jobs(
                    id INTEGER PRIMARY KEY,
                    ticket_number TEXT NOT NULL UNIQUE,
                    customer_id INTEGER REFERENCES customers(id),
                    customer_name TEXT NOT NULL,
                    device TEXT NOT NULL,
                    serial_number TEXT NOT NULL DEFAULT '',
                    reported_fault TEXT NOT NULL,
                    diagnosis TEXT NOT NULL DEFAULT '',
                    warranty_notes TEXT NOT NULL DEFAULT '',
                    technician TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    approval_status TEXT NOT NULL DEFAULT 'Pending',
                    estimate_amount TEXT NOT NULL DEFAULT '0.00',
                    labor_amount TEXT NOT NULL DEFAULT '0.00',
                    subtotal TEXT NOT NULL DEFAULT '0.00',
                    tax_label TEXT NOT NULL DEFAULT '',
                    tax_rate TEXT NOT NULL DEFAULT '0.00',
                    tax_total TEXT NOT NULL DEFAULT '0.00',
                    total TEXT NOT NULL DEFAULT '0.00',
                    paid_total TEXT NOT NULL DEFAULT '0.00',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_job_parts(
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES repair_jobs(id),
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    sku TEXT NOT NULL,
                    description TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    unit_price TEXT NOT NULL,
                    line_total TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_job_payments(
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES repair_jobs(id),
                    amount TEXT NOT NULL,
                    reference TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_job_events(
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES repair_jobs(id),
                    event TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_repair_jobs_created ON repair_jobs(created_at,id);
                CREATE INDEX IF NOT EXISTS idx_repair_jobs_customer ON repair_jobs(customer_id,id);
                CREATE INDEX IF NOT EXISTS idx_repair_parts_job ON repair_job_parts(job_id,id);
                CREATE INDEX IF NOT EXISTS idx_repair_events_job ON repair_job_events(job_id,id);
                CREATE TABLE IF NOT EXISTS sync_queue(
                    id INTEGER PRIMARY KEY,
                    entity TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT DEFAULT ''
                );
            """)
            self.conn.execute("""INSERT INTO student_guardians(
                student_id,full_name,relationship,contact,email,is_primary,created_by,created_at,updated_at
                ) SELECT s.id,s.guardian_name,'Guardian',s.guardian_contact,'',1,'migration',s.created_at,s.updated_at
                FROM academic_students s WHERE TRIM(s.guardian_name)<>'' AND NOT EXISTS(
                    SELECT 1 FROM student_guardians g WHERE g.student_id=s.id
                )""")
            # Extend repair tickets without breaking existing local databases.
            repair_columns={row[1] for row in self.conn.execute("PRAGMA table_info(repair_jobs)").fetchall()}
            for column, declaration in {
                "work_kind": "TEXT NOT NULL DEFAULT 'Repair'",
                "service_type": "TEXT NOT NULL DEFAULT ''",
                "received_date": "TEXT NOT NULL DEFAULT ''",
                "expected_collection_date": "TEXT NOT NULL DEFAULT ''",
                "product_id": "INTEGER",
                "service_notes": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in repair_columns:
                    self.conn.execute(f"ALTER TABLE repair_jobs ADD COLUMN {column} {declaration}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_repair_jobs_collection ON repair_jobs(expected_collection_date,status)")
            product_columns={row[1] for row in self.conn.execute("PRAGMA table_info(products)").fetchall()}
            if "category" not in product_columns:
                self.conn.execute("ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            if "barcode" not in product_columns:
                self.conn.execute("ALTER TABLE products ADD COLUMN barcode TEXT NOT NULL DEFAULT ''")
            product_columns={row[1] for row in self.conn.execute("PRAGMA table_info(products)").fetchall()}
            for column, declaration in {
                "description": "TEXT NOT NULL DEFAULT ''",
                "brand": "TEXT NOT NULL DEFAULT ''",
                "model": "TEXT NOT NULL DEFAULT ''",
                "supplier_code": "TEXT NOT NULL DEFAULT ''",
                "variant_group": "TEXT NOT NULL DEFAULT ''",
                "variant_attributes_json": "TEXT NOT NULL DEFAULT '{}'",
                # Stock is tracked in `unit`; these fields optionally describe
                # the contents of each sellable stock unit (e.g. 2 kg per bag).
                "base_unit": "TEXT NOT NULL DEFAULT ''",
                "units_per_stock_unit": "TEXT NOT NULL DEFAULT '1.000'",
                "track_batches": "INTEGER NOT NULL DEFAULT 0",
                "track_stock": "INTEGER NOT NULL DEFAULT 1",
            }.items():
                if column not in product_columns:
                    self.conn.execute(f"ALTER TABLE products ADD COLUMN {column} {declaration}")
            # A 1:1 conversion preserves existing records and their historic quantities.
            self.conn.execute("UPDATE products SET base_unit=unit WHERE TRIM(base_unit)='' OR base_unit IS NULL")
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode COLLATE NOCASE) WHERE barcode <> ''")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name COLLATE NOCASE,id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category COLLATE NOCASE,id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_products_supplier_code ON products(supplier_code COLLATE NOCASE)")
            # Identifiers already present in older databases are migrated as primary barcodes;
            # the alias table allows additional barcodes without conflating them with SKUs.
            self.conn.execute("""INSERT OR IGNORE INTO product_barcodes(product_id,barcode,label,created_at)
                SELECT id,barcode,'Primary barcode',created_at FROM products WHERE TRIM(barcode)<>''""")
            return_columns={row[1] for row in self.conn.execute("PRAGMA table_info(sales_returns)").fetchall()}
            if "created_by" not in return_columns:
                self.conn.execute("ALTER TABLE sales_returns ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
            sale_columns={row[1] for row in self.conn.execute("PRAGMA table_info(sales)").fetchall()}
            if "quote_id" not in sale_columns:
                self.conn.execute("ALTER TABLE sales ADD COLUMN quote_id INTEGER")
            for table, additions in {
                "sales": ("subtotal TEXT NOT NULL DEFAULT '0.00'", "tax_rate TEXT NOT NULL DEFAULT '0.00'", "tax_total TEXT NOT NULL DEFAULT '0.00'", "tax_label TEXT NOT NULL DEFAULT ''"),
                "quotes": ("subtotal TEXT NOT NULL DEFAULT '0.00'", "tax_rate TEXT NOT NULL DEFAULT '0.00'", "tax_total TEXT NOT NULL DEFAULT '0.00'", "tax_label TEXT NOT NULL DEFAULT ''"),
                "sale_lines": ("tax_amount TEXT NOT NULL DEFAULT '0.00'",),
                "quote_lines": ("tax_amount TEXT NOT NULL DEFAULT '0.00'",),
                "sale_return_lines": ("tax_amount TEXT NOT NULL DEFAULT '0.00'",),
            }.items():
                existing={row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for declaration in additions:
                    column=declaration.split()[0]
                    if column not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")
            self.conn.execute("UPDATE sales SET subtotal=total WHERE subtotal='0.00' AND CAST(total AS REAL)<>0")
            self.conn.execute("UPDATE quotes SET subtotal=total WHERE subtotal='0.00' AND CAST(total AS REAL)<>0")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_quotes_created ON quotes(created_at,id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_quote_lines_quote ON quote_lines(quote_id,id)")
            purchase_columns={row[1] for row in self.conn.execute("PRAGMA table_info(purchases)").fetchall()}
            if "order_id" not in purchase_columns:
                self.conn.execute("ALTER TABLE purchases ADD COLUMN order_id INTEGER")
            purchase_line_columns={row[1] for row in self.conn.execute("PRAGMA table_info(purchase_lines)").fetchall()}
            if "purchase_order_line_id" not in purchase_line_columns:
                self.conn.execute("ALTER TABLE purchase_lines ADD COLUMN purchase_order_line_id INTEGER")
            if "prior_cost" not in purchase_line_columns:
                self.conn.execute("ALTER TABLE purchase_lines ADD COLUMN prior_cost TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_orders_created ON purchase_orders(created_at,id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_order_lines_order ON purchase_order_lines(purchase_order_id,id)")
            # Cashbook enrichment is additive: historic entries remain valid and are
            # explicitly marked Unspecified rather than falsely assuming a payment channel.
            cash_columns={row[1] for row in self.conn.execute("PRAGMA table_info(cash_entries)").fetchall()}
            for column, declaration in {
                "payment_method": "TEXT NOT NULL DEFAULT 'Unspecified'",
                "reference": "TEXT NOT NULL DEFAULT ''",
                "category": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in cash_columns:
                    self.conn.execute(f"ALTER TABLE cash_entries ADD COLUMN {column} {declaration}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_cash_entries_method_date ON cash_entries(payment_method,created_at,id)")
            # Advanced hybrid pillars: serials, loyalty, location stock
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS serial_units(
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    serial_number TEXT NOT NULL COLLATE NOCASE,
                    status TEXT NOT NULL DEFAULT 'in_stock',
                    location_id TEXT NOT NULL DEFAULT 'main',
                    sale_id INTEGER,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(product_id, serial_number)
                );
                CREATE INDEX IF NOT EXISTS idx_serial_units_serial ON serial_units(serial_number);
                CREATE INDEX IF NOT EXISTS idx_serial_units_product ON serial_units(product_id, status);
                CREATE TABLE IF NOT EXISTS loyalty_accounts(
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER REFERENCES customers(id),
                    customer_name TEXT NOT NULL,
                    points INTEGER NOT NULL DEFAULT 0,
                    lifetime_points INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(customer_name COLLATE NOCASE)
                );
                CREATE TABLE IF NOT EXISTS loyalty_ledger(
                    id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES loyalty_accounts(id),
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL DEFAULT '',
                    source_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS location_stock(
                    id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    location_id TEXT NOT NULL,
                    quantity TEXT NOT NULL DEFAULT '0',
                    UNIQUE(product_id, location_id)
                );
            """)
            prod_cols={row[1] for row in self.conn.execute("PRAGMA table_info(products)").fetchall()}
            if "track_serials" not in prod_cols:
                self.conn.execute("ALTER TABLE products ADD COLUMN track_serials INTEGER NOT NULL DEFAULT 0")
            # RC2+ : credit sales metadata, served-by, and mandatory warranty
            sale_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(sales)").fetchall()}
            for column, declaration in {
                "payment_method": "TEXT NOT NULL DEFAULT 'Cash'",
                "payment_reference": "TEXT NOT NULL DEFAULT ''",
                "created_by": "TEXT NOT NULL DEFAULT ''",
                "served_by_name": "TEXT NOT NULL DEFAULT ''",
                "served_by_code": "TEXT NOT NULL DEFAULT ''",
                "warranty_period": "TEXT NOT NULL DEFAULT 'N/A'",
                # Printed invoice date (may differ from system created_at when backdating)
                "invoice_date": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in sale_cols:
                    self.conn.execute(f"ALTER TABLE sales ADD COLUMN {column} {declaration}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_customer_balance ON sales(customer_id, payment_status, status)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at, id)")
            self.conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    def _enqueue(self, entity, entity_id, payload, timestamp):
        # In the desktop app the queue is written to the shared system DB with an
        # explicit workspace scope, inside the same attached-database transaction.
        # Unit tests may leave queue_writer unset and use the local queue table.
        if self.queue_writer is not None:
            self.queue_writer(entity, entity_id, payload, timestamp)
        else:
            self.conn.execute("INSERT INTO sync_queue(entity,entity_id,operation,payload_json,queued_at) VALUES(?,?,?,?,?)",
                              (entity,entity_id,"upsert",json.dumps(payload,ensure_ascii=False),timestamp))

    @contextmanager
    def _transaction(self):
        """Atomic transaction with safe nesting via SQLite savepoints."""
        depth = int(getattr(self, "_transaction_depth", 0))
        savepoint = f"ngerp_sp_{depth}"
        if depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT {savepoint}")
        self._transaction_depth = depth + 1
        try:
            yield
            if depth == 0:
                self.conn.commit()
            else:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            if depth == 0:
                self.conn.rollback()
            else:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        finally:
            self._transaction_depth = depth

    @staticmethod
    def _decimal(value, label, *, minimum=None, places=2):
        try:
            number=Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise OperationError(f"{label} must be a valid number.")
        if not number.is_finite():
            raise OperationError(f"{label} must be finite.")
        quantum=Decimal("1").scaleb(-places)
        rounded=number.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded != number:
            raise OperationError(f"{label} supports at most {places} decimal places.")
        number=rounded
        if minimum is not None and number < minimum:
            raise OperationError(f"{label} must be at least {minimum}.")
        return number

    def _tax_config(self):
        try:
            config=self.tax_provider() or {}
            label=str(config.get("label","Tax")).strip()[:60] or "Tax"
            rate=self._decimal(config.get("rate",0),"Tax rate",minimum=Decimal("0"))
            if rate>Decimal("100"):
                raise OperationError("Tax rate cannot exceed 100%.")
            return label,rate
        except OperationError:
            raise
        except Exception as exc:
            raise OperationError("The configured tax settings are invalid.") from exc

    @staticmethod
    def _tax_amount(amount, rate):
        return (amount*rate/Decimal("100")).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)

    def _tax_inclusive(self):
        """True when selling prices already contain tax (normal for Kenyan VAT-inclusive shelf prices)."""
        try:
            return str((self.tax_provider() or {}).get("inclusive","0")).strip().lower() in {"1","true","yes"}
        except Exception:
            return False

    @staticmethod
    def _inclusive_split(gross, rate):
        """Split a tax-inclusive amount into (net, tax) so that net + tax == gross exactly."""
        net=(gross/(Decimal("1")+rate/Decimal("100"))).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
        return net,(gross-net).quantize(Decimal("0.01"))

    @staticmethod
    def _line_tax(line, rate):
        explicit=getattr(line,"tax",None)
        if explicit is not None:
            return explicit
        return OperationsService._tax_amount(line[3],rate)

    @staticmethod
    def _stamp():
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _money(value):
        return format(value, ".2f")

    @staticmethod
    def _quantity(value):
        return format(value, ".3f")

    @staticmethod
    def _next_number(prefix, row_id):
        return f"{prefix}-{row_id:08d}"

    def create_customer(self, name, contact="", email=""):
        self._require_module("CRM")
        self._require("create")
        name=str(name).strip()
        if not name or len(name)>160:
            raise OperationError("Customer name is required and must be 160 characters or fewer.")
        with self._transaction():
            timestamp=self._stamp()
            cur=self.conn.execute("INSERT INTO customers(name,contact,email,created_at) VALUES(?,?,?,?)",
                                  (name,str(contact).strip()[:100],str(email).strip()[:160],timestamp))
            self._enqueue("customers",cur.lastrowid,{"id":cur.lastrowid,"name":name,"contact":str(contact).strip()[:100],"email":str(email).strip()[:160]},timestamp)
        return cur.lastrowid

    def create_supplier(self, name, contact="", email=""):
        self._require_module("Purchases")
        self._require("manage_inventory")
        name=str(name).strip()
        if not name or len(name)>160:
            raise OperationError("Supplier name is required and must be 160 characters or fewer.")
        with self._transaction():
            timestamp=self._stamp()
            cur=self.conn.execute("INSERT INTO suppliers(name,contact,email,created_at) VALUES(?,?,?,?)",
                                  (name,str(contact).strip()[:100],str(email).strip()[:160],timestamp))
            self._enqueue("suppliers",cur.lastrowid,{"id":cur.lastrowid,"name":name,"contact":str(contact).strip()[:100],"email":str(email).strip()[:160]},timestamp)
        return cur.lastrowid

    # ------------------------------------------------------------------ Academic operations
    @staticmethod
    def _iso_date(value, label, allow_blank=False):
        value = str(value or "").strip()
        if not value and allow_blank:
            return ""
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise OperationError(f"{label} must use YYYY-MM-DD format.")
        return value

    def save_academic_year(self, name, start_date, end_date, status="Planning", year_id=None):
        self._require_module("Students & Enrolment")
        self._require("edit" if year_id is not None else "create")
        name=str(name or "").strip()[:100]
        start_date=self._iso_date(start_date,"Academic year start date")
        end_date=self._iso_date(end_date,"Academic year end date")
        if not name: raise OperationError("Academic year name is required.")
        if end_date<start_date: raise OperationError("Academic year end date must be on or after its start date.")
        if status not in {"Planning","Active","Closed"}: raise OperationError("Choose a valid academic year status.")
        if status=="Active":
            conflict=self.conn.execute("SELECT id,name FROM academic_years WHERE status='Active' AND id<>COALESCE(?,0) LIMIT 1",(year_id,)).fetchone()
            if conflict: raise OperationError(f"{conflict['name']} is already the active academic year. Close it before activating another.")
        timestamp=self._stamp()
        try:
            with self._transaction():
                if year_id is None:
                    cur=self.conn.execute("INSERT INTO academic_years(name,start_date,end_date,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                                          (name,start_date,end_date,status,self.actor_username,timestamp,timestamp))
                    year_id=int(cur.lastrowid)
                else:
                    cur=self.conn.execute("UPDATE academic_years SET name=?,start_date=?,end_date=?,status=?,updated_at=? WHERE id=?",
                                          (name,start_date,end_date,status,timestamp,int(year_id)))
                    if not cur.rowcount: raise OperationError("The academic year no longer exists.")
                    year_id=int(year_id)
                self._enqueue("academic_years",year_id,{"id":year_id,"name":name,"start_date":start_date,"end_date":end_date,"status":status},timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"Academic year {name!r} already exists.") from exc
        return year_id

    def academic_years(self, limit=100):
        self._require_module("Students & Enrolment")
        return self.conn.execute("SELECT * FROM academic_years ORDER BY start_date DESC,id DESC LIMIT ?",(max(1,min(int(limit),500)),)).fetchall()

    def save_academic_term(self, year_id, name, start_date, end_date, status="Planning", term_id=None):
        self._require_module("Students & Enrolment")
        self._require("edit" if term_id is not None else "create")
        year=self.conn.execute("SELECT * FROM academic_years WHERE id=?",(int(year_id),)).fetchone()
        if not year: raise OperationError("Choose an existing academic year.")
        name=str(name or "").strip()[:100]
        start_date=self._iso_date(start_date,"Term start date")
        end_date=self._iso_date(end_date,"Term end date")
        if not name: raise OperationError("Term name is required.")
        if end_date<start_date: raise OperationError("Term end date must be on or after its start date.")
        if start_date<year["start_date"] or end_date>year["end_date"]:
            raise OperationError("Term dates must fall within the selected academic year.")
        if status not in {"Planning","Active","Closed"}: raise OperationError("Choose a valid term status.")
        overlap=self.conn.execute("""SELECT name FROM academic_terms WHERE academic_year_id=? AND id<>COALESCE(?,0)
            AND start_date<=? AND end_date>=? LIMIT 1""",(int(year_id),term_id,end_date,start_date)).fetchone()
        if overlap: raise OperationError(f"Term dates overlap with {overlap['name']}.")
        timestamp=self._stamp()
        try:
            with self._transaction():
                if status=="Active":
                    conflict=self.conn.execute("SELECT id,name FROM academic_terms WHERE academic_year_id=? AND status='Active' AND id<>COALESCE(?,0) LIMIT 1",
                                               (int(year_id),term_id)).fetchone()
                    if conflict: raise OperationError(f"{conflict['name']} is already the active term for this academic year.")
                values=(int(year_id),name,start_date,end_date,status)
                if term_id is None:
                    cur=self.conn.execute("INSERT INTO academic_terms(academic_year_id,name,start_date,end_date,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                          values+(self.actor_username,timestamp,timestamp))
                    term_id=int(cur.lastrowid)
                else:
                    cur=self.conn.execute("UPDATE academic_terms SET academic_year_id=?,name=?,start_date=?,end_date=?,status=?,updated_at=? WHERE id=?",
                                          values+(timestamp,int(term_id)))
                    if not cur.rowcount: raise OperationError("The term no longer exists.")
                    term_id=int(term_id)
                self._enqueue("academic_terms",term_id,{"id":term_id,"academic_year_id":int(year_id),"name":name,"start_date":start_date,"end_date":end_date,"status":status},timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"Term {name!r} already exists in this academic year.") from exc
        return term_id

    def academic_terms(self, year_id=None, limit=500):
        self._require_module("Students & Enrolment")
        sql="SELECT t.*,y.name AS academic_year FROM academic_terms t JOIN academic_years y ON y.id=t.academic_year_id"
        params=[]
        if year_id is not None: sql+=" WHERE t.academic_year_id=?"; params.append(int(year_id))
        params.append(max(1,min(int(limit),2000)))
        return self.conn.execute(sql+" ORDER BY t.start_date,t.id LIMIT ?",params).fetchall()

    def save_academic_class(self, year_id, class_code, name, section="", homeroom_teacher="", capacity=0, status="Active", class_id=None):
        self._require_module("Students & Enrolment")
        self._require("edit" if class_id is not None else "create")
        if not self.conn.execute("SELECT 1 FROM academic_years WHERE id=?",(int(year_id),)).fetchone():
            raise OperationError("Choose an existing academic year.")
        class_code=str(class_code or "").strip()[:60]; name=str(name or "").strip()[:120]
        section=str(section or "").strip()[:60]; homeroom_teacher=str(homeroom_teacher or "").strip()[:160]
        if not class_code or not name: raise OperationError("Class code and class name are required.")
        try: capacity=int(capacity)
        except (TypeError,ValueError) as exc: raise OperationError("Class capacity must be a whole number.") from exc
        if capacity<0 or capacity>100000: raise OperationError("Class capacity must be between 0 and 100,000.")
        if status not in {"Active","Archived"}: raise OperationError("Choose Active or Archived for the class.")
        timestamp=self._stamp()
        try:
            with self._transaction():
                values=(int(year_id),class_code,name,section,homeroom_teacher,capacity,status)
                if class_id is None:
                    cur=self.conn.execute("INSERT INTO academic_classes(academic_year_id,class_code,name,section,homeroom_teacher,capacity,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                          values+(self.actor_username,timestamp,timestamp))
                    class_id=int(cur.lastrowid)
                else:
                    cur=self.conn.execute("UPDATE academic_classes SET academic_year_id=?,class_code=?,name=?,section=?,homeroom_teacher=?,capacity=?,status=?,updated_at=? WHERE id=?",
                                          values+(timestamp,int(class_id)))
                    if not cur.rowcount: raise OperationError("The class no longer exists.")
                    class_id=int(class_id)
                self._enqueue("academic_classes",class_id,{"id":class_id,"academic_year_id":int(year_id),"class_code":class_code,"name":name,"section":section,"homeroom_teacher":homeroom_teacher,"capacity":capacity,"status":status},timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"Class code {class_code!r} already exists for this academic year.") from exc
        return class_id

    def academic_classes(self, year_id=None, active_only=False, limit=1000):
        self._require_module("Students & Enrolment")
        sql="SELECT c.*,y.name AS academic_year FROM academic_classes c JOIN academic_years y ON y.id=c.academic_year_id"
        clauses=[]; params=[]
        if year_id is not None: clauses.append("c.academic_year_id=?"); params.append(int(year_id))
        if active_only: clauses.append("c.status='Active'")
        if clauses: sql+=" WHERE "+" AND ".join(clauses)
        params.append(max(1,min(int(limit),5000)))
        return self.conn.execute(sql+" ORDER BY y.start_date DESC,c.name COLLATE NOCASE,c.section COLLATE NOCASE LIMIT ?",params).fetchall()

    def assign_student_to_class(self, student_id, class_id):
        self._require_module("Students & Enrolment")
        student=self.conn.execute("SELECT * FROM academic_students WHERE id=? AND status<>'Withdrawn'",(int(student_id),)).fetchone()
        class_row=self.conn.execute("""SELECT c.*,y.status AS year_status FROM academic_classes c
            JOIN academic_years y ON y.id=c.academic_year_id WHERE c.id=?""",(int(class_id),)).fetchone()
        if not student: raise OperationError("Choose an active student to enroll in a class.")
        if not class_row or class_row["status"]!="Active" or class_row["year_status"]=="Closed":
            raise OperationError("Choose an active class in an open academic year.")
        current=self.conn.execute("SELECT * FROM student_class_enrollments WHERE student_id=? AND academic_year_id=? AND status='Enrolled'",
                                  (int(student_id),int(class_row["academic_year_id"]))).fetchone()
        if current and int(current["academic_class_id"])==int(class_id): return int(current["id"])
        self._require("edit" if current else "create")
        count=self.conn.execute("SELECT COUNT(*) FROM student_class_enrollments WHERE academic_class_id=? AND status='Enrolled' AND student_id<>?",
                                (int(class_id),int(student_id))).fetchone()[0]
        if class_row["capacity"] and int(count)>=int(class_row["capacity"]):
            raise OperationError(f"{class_row['name']} {class_row['section']} has reached its capacity of {class_row['capacity']}.")
        timestamp=self._stamp()
        class_label=class_row["name"]+(f" · {class_row['section']}" if class_row["section"] else "")
        with self._transaction():
            if current:
                self.conn.execute("UPDATE student_class_enrollments SET status='Transferred',ended_at=? WHERE id=?",(timestamp,int(current["id"])))
                self._enqueue("student_class_enrollments",int(current["id"]),{"id":int(current["id"]),"student_id":int(student_id),
                    "academic_class_id":int(current["academic_class_id"]),"academic_year_id":int(class_row["academic_year_id"]),"status":"Transferred","ended_at":timestamp},timestamp)
            cur=self.conn.execute("INSERT INTO student_class_enrollments(student_id,academic_class_id,academic_year_id,status,enrolled_at,changed_by) VALUES(?,?,?,'Enrolled',?,?)",
                                  (int(student_id),int(class_id),int(class_row["academic_year_id"]),timestamp,self.actor_username))
            enrollment_id=int(cur.lastrowid)
            self.conn.execute("UPDATE academic_students SET class_course=?,updated_at=? WHERE id=?",(class_label,timestamp,int(student_id)))
            payload={"id":enrollment_id,"student_id":int(student_id),"academic_class_id":int(class_id),
                     "academic_year_id":int(class_row["academic_year_id"]),"status":"Enrolled","enrolled_at":timestamp,"ended_at":""}
            self._enqueue("student_class_enrollments",enrollment_id,payload,timestamp)
            self._enqueue("academic_students",int(student_id),{"id":int(student_id),"admission_number":student["admission_number"],
                "full_name":student["full_name"],"class_course":class_label,"date_of_birth":student["date_of_birth"],
                "guardian_name":student["guardian_name"],"guardian_contact":student["guardian_contact"],"email":student["email"],
                "status":student["status"]},timestamp)
        return enrollment_id

    def academic_class_roster(self, class_id, include_inactive=False, limit=5000):
        self._require_module("Students & Enrolment")
        sql="""SELECT e.id AS enrollment_id,e.status AS enrollment_status,e.enrolled_at,s.*
            FROM student_class_enrollments e JOIN academic_students s ON s.id=e.student_id
            WHERE e.academic_class_id=?"""
        params=[int(class_id)]
        if not include_inactive: sql+=" AND e.status='Enrolled' AND s.status='Active'"
        params.append(max(1,min(int(limit),10000)))
        return self.conn.execute(sql+" ORDER BY s.full_name COLLATE NOCASE,s.id LIMIT ?",params).fetchall()

    def create_student(self, admission_number, full_name, class_course="", date_of_birth="", guardian_name="", guardian_contact="", email=""):
        self._require_module("Students & Enrolment")
        self._require("create")
        admission_number=str(admission_number or "").strip()[:80]
        full_name=str(full_name or "").strip()[:160]
        if not admission_number or not full_name:
            raise OperationError("Admission number and student name are required.")
        date_of_birth=self._iso_date(date_of_birth,"Date of birth",allow_blank=True)
        class_course=str(class_course or "").strip()[:160]
        guardian_name=str(guardian_name or "").strip()[:160]
        guardian_contact=str(guardian_contact or "").strip()[:100]
        email=str(email or "").strip()[:160]
        timestamp=self._stamp()
        try:
            with self._transaction():
                cur=self.conn.execute("""INSERT INTO academic_students(admission_number,full_name,class_course,date_of_birth,guardian_name,guardian_contact,email,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,'Active',?,?)""",(admission_number,full_name,class_course,date_of_birth,guardian_name,guardian_contact,email,timestamp,timestamp))
                payload={"id":cur.lastrowid,"admission_number":admission_number,"full_name":full_name,"class_course":class_course,
                    "date_of_birth":date_of_birth,"guardian_name":guardian_name,"guardian_contact":guardian_contact,"email":email,"status":"Active"}
                self._enqueue("academic_students",cur.lastrowid,payload,timestamp)
                if guardian_name:
                    guardian=self.conn.execute("""INSERT INTO student_guardians(
                        student_id,full_name,relationship,contact,email,is_primary,created_by,created_at,updated_at
                        ) VALUES(?,?, 'Guardian',?,'',1,?,?,?)""",
                        (int(cur.lastrowid),guardian_name,guardian_contact,self.actor_username,timestamp,timestamp))
                    self._enqueue("student_guardians",guardian.lastrowid,{"id":guardian.lastrowid,"student_id":int(cur.lastrowid),
                        "full_name":guardian_name,"relationship":"Guardian","contact":guardian_contact,"email":"","is_primary":1},timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"Admission number {admission_number!r} is already registered.") from exc
        return cur.lastrowid

    def update_student(self, student_id, admission_number, full_name, class_course="", date_of_birth="", guardian_name="", guardian_contact="", email="", status="Active"):
        self._require_module("Students & Enrolment")
        self._require("edit")
        admission_number=str(admission_number or "").strip()[:80]
        full_name=str(full_name or "").strip()[:160]
        if not admission_number or not full_name:
            raise OperationError("Admission number and student name are required.")
        if status not in {"Active","On leave","Completed","Withdrawn"}:
            raise OperationError("Choose a valid student status.")
        date_of_birth=self._iso_date(date_of_birth,"Date of birth",allow_blank=True)
        class_course=str(class_course or "").strip()[:160]
        guardian_name=str(guardian_name or "").strip()[:160]
        guardian_contact=str(guardian_contact or "").strip()[:100]
        email=str(email or "").strip()[:160]
        timestamp=self._stamp()
        try:
            with self._transaction():
                cur=self.conn.execute("""UPDATE academic_students SET admission_number=?,full_name=?,class_course=?,date_of_birth=?,guardian_name=?,guardian_contact=?,email=?,status=?,updated_at=? WHERE id=?""",
                    (admission_number,full_name,class_course,date_of_birth,guardian_name,guardian_contact,email,status,timestamp,int(student_id)))
                if not cur.rowcount:
                    raise OperationError("The student record no longer exists.")
                self._enqueue("academic_students",student_id,{"id":int(student_id),"admission_number":admission_number,"full_name":full_name,
                    "class_course":class_course,"date_of_birth":date_of_birth,"guardian_name":guardian_name,"guardian_contact":guardian_contact,
                    "email":email,"status":status},timestamp)
                primary=self.conn.execute("SELECT * FROM student_guardians WHERE student_id=? AND is_primary=1",(int(student_id),)).fetchone()
                if primary:
                    if guardian_name:
                        self.conn.execute("UPDATE student_guardians SET full_name=?,contact=?,updated_at=? WHERE id=?",
                                          (guardian_name,guardian_contact,timestamp,int(primary["id"])))
                    else:
                        self.conn.execute("UPDATE student_guardians SET is_primary=0,updated_at=? WHERE id=?",(timestamp,int(primary["id"])))
                    self._enqueue("student_guardians",int(primary["id"]),{"id":int(primary["id"]),"student_id":int(student_id),
                        "full_name":guardian_name or primary["full_name"],"relationship":primary["relationship"],
                        "contact":guardian_contact if guardian_name else primary["contact"],"email":primary["email"],
                        "is_primary":1 if guardian_name else 0},timestamp)
                elif guardian_name:
                    cur_guardian=self.conn.execute("""INSERT INTO student_guardians(
                        student_id,full_name,relationship,contact,email,is_primary,created_by,created_at,updated_at
                        ) VALUES(?,?, 'Guardian',?,'',1,?,?,?)""",
                        (int(student_id),guardian_name,guardian_contact,self.actor_username,timestamp,timestamp))
                    self._enqueue("student_guardians",cur_guardian.lastrowid,{"id":cur_guardian.lastrowid,"student_id":int(student_id),
                        "full_name":guardian_name,"relationship":"Guardian","contact":guardian_contact,"email":"","is_primary":1},timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"Admission number {admission_number!r} is already registered.") from exc
        return int(student_id)

    def students(self, limit=5000, active_only=False):
        self._require_module("Students & Enrolment")
        limit=max(1,min(int(limit),10000))
        where=" WHERE status='Active'" if active_only else ""
        return self.conn.execute("SELECT * FROM academic_students"+where+" ORDER BY full_name COLLATE NOCASE,id LIMIT ?",(limit,)).fetchall()

    def save_academic_timetable_entry(self, class_course, subject, weekday, start_time, end_time,
                                      teacher="", room="", term="", entry_id=None, status="Scheduled"):
        self._require_module("Students & Enrolment")
        self._require("edit" if entry_id is not None else "create")
        class_course=str(class_course or "").strip()[:160]
        subject=str(subject or "").strip()[:120]
        teacher=str(teacher or "").strip()[:160]
        room=str(room or "").strip()[:100]
        term=str(term or "").strip()[:100]
        weekday=str(weekday or "").strip()
        start_time=str(start_time or "").strip()
        end_time=str(end_time or "").strip()
        if not class_course or not subject:
            raise OperationError("Class/course and subject are required.")
        if weekday not in {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"}:
            raise OperationError("Choose a valid weekday.")
        try:
            start=datetime.strptime(start_time,"%H:%M").time()
            end=datetime.strptime(end_time,"%H:%M").time()
        except (TypeError,ValueError) as exc:
            raise OperationError("Timetable times must use 24-hour HH:MM format.") from exc
        start_time=start.strftime("%H:%M")
        end_time=end.strftime("%H:%M")
        if start >= end:
            raise OperationError("The class end time must be later than its start time.")
        if status not in {"Scheduled","Cancelled"}:
            raise OperationError("Choose Scheduled or Cancelled for the timetable entry.")
        timestamp=self._stamp()
        with self._transaction():
            if status == "Scheduled":
                sql="""SELECT class_course,subject,teacher,room,start_time,end_time
                    FROM academic_timetable WHERE weekday=? AND status='Scheduled'
                    AND start_time<? AND end_time>?
                    AND (term='' OR ?='' OR term=? COLLATE NOCASE)
                    AND (class_course=? COLLATE NOCASE OR (?<>'' AND teacher=? COLLATE NOCASE)
                         OR (?<>'' AND room=? COLLATE NOCASE))"""
                params=[weekday,end_time,start_time,term,term,class_course,teacher,teacher,room,room]
                if entry_id is not None:
                    sql+=" AND id<>?"; params.append(int(entry_id))
                conflict=self.conn.execute(sql+" LIMIT 1",params).fetchone()
                if conflict:
                    resources=[]
                    if conflict["class_course"].casefold()==class_course.casefold(): resources.append("class/course")
                    if teacher and conflict["teacher"].casefold()==teacher.casefold(): resources.append("teacher")
                    if room and conflict["room"].casefold()==room.casefold(): resources.append("room")
                    raise OperationError("Timetable conflict: this time overlaps for the "+" and ".join(resources)+".")
            values=(class_course,subject,teacher,room,weekday,start_time,end_time,term,status)
            if entry_id is None:
                cur=self.conn.execute("""INSERT INTO academic_timetable(
                    class_course,subject,teacher,room,weekday,start_time,end_time,term,status,created_by,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",values+(self.actor_username,timestamp,timestamp))
                entry_id=int(cur.lastrowid)
            else:
                cur=self.conn.execute("""UPDATE academic_timetable SET class_course=?,subject=?,teacher=?,room=?,weekday=?,
                    start_time=?,end_time=?,term=?,status=?,updated_at=? WHERE id=?""",values+(timestamp,int(entry_id)))
                if not cur.rowcount:
                    raise OperationError("The timetable entry no longer exists.")
                entry_id=int(entry_id)
            payload={"id":entry_id,"class_course":class_course,"subject":subject,"teacher":teacher,
                     "room":room,"weekday":weekday,"start_time":start_time,"end_time":end_time,
                     "term":term,"status":status}
            self._enqueue("academic_timetable",entry_id,payload,timestamp)
        return entry_id

    def academic_timetable(self, term="", class_course="", limit=2000):
        self._require_module("Students & Enrolment")
        sql="""SELECT * FROM academic_timetable"""
        clauses=[]; params=[]
        if term:
            clauses.append("term=? COLLATE NOCASE"); params.append(str(term).strip())
        if class_course:
            clauses.append("class_course=? COLLATE NOCASE"); params.append(str(class_course).strip())
        if clauses: sql+=" WHERE "+" AND ".join(clauses)
        params.append(max(1,min(int(limit),5000)))
        return self.conn.execute(sql+" ORDER BY CASE weekday WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2 WHEN 'Wednesday' THEN 3 WHEN 'Thursday' THEN 4 WHEN 'Friday' THEN 5 WHEN 'Saturday' THEN 6 ELSE 7 END,start_time,id LIMIT ?",params).fetchall()

    def create_admission_application(self, application_number, applicant_name, desired_class="", date_of_birth="",
                                     guardian_name="", guardian_contact="", guardian_email="", applied_on=""):
        self._require_module("Students & Enrolment")
        self._require("create")
        application_number=str(application_number or "").strip()[:80]
        applicant_name=str(applicant_name or "").strip()[:160]
        if not application_number or not applicant_name:
            raise OperationError("Application number and applicant name are required.")
        date_of_birth=self._iso_date(date_of_birth,"Date of birth",allow_blank=True)
        applied_on=self._iso_date(applied_on or datetime.now().strftime("%Y-%m-%d"),"Application date")
        desired_class=str(desired_class or "").strip()[:160]
        guardian_name=str(guardian_name or "").strip()[:160]
        guardian_contact=str(guardian_contact or "").strip()[:100]
        guardian_email=str(guardian_email or "").strip()[:160]
        timestamp=self._stamp()
        try:
            with self._transaction():
                cur=self.conn.execute("""INSERT INTO admission_applications(
                    application_number,applicant_name,date_of_birth,desired_class,guardian_name,guardian_contact,guardian_email,
                    applied_on,status,created_by,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,'Submitted',?,?,?)""",
                    (application_number,applicant_name,date_of_birth,desired_class,guardian_name,guardian_contact,guardian_email,
                     applied_on,self.actor_username,timestamp,timestamp))
                application_id=int(cur.lastrowid)
                payload={"id":application_id,"application_number":application_number,"applicant_name":applicant_name,
                    "date_of_birth":date_of_birth,"desired_class":desired_class,"guardian_name":guardian_name,
                    "guardian_contact":guardian_contact,"guardian_email":guardian_email,"applied_on":applied_on,
                    "status":"Submitted","decision_notes":"","converted_student_id":None}
                self._enqueue("admission_applications",application_id,payload,timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"Application number {application_number!r} already exists.") from exc
        return application_id

    def admission_applications(self, status="", limit=5000):
        self._require_module("Students & Enrolment")
        sql="SELECT * FROM admission_applications"; params=[]
        if status:
            sql+=" WHERE status=?"; params.append(str(status))
        params.append(max(1,min(int(limit),10000)))
        return self.conn.execute(sql+" ORDER BY applied_on DESC,id DESC LIMIT ?",params).fetchall()

    def update_admission_status(self, application_id, status, decision_notes=""):
        self._require_module("Students & Enrolment")
        self._require("edit")
        if status not in {"Submitted","Under review","Waitlisted","Accepted","Rejected","Withdrawn"}:
            raise OperationError("Choose a valid admissions status. Enrolled applications are converted from the accepted queue.")
        notes=str(decision_notes or "").strip()[:1000]
        timestamp=self._stamp()
        with self._transaction():
            row=self.conn.execute("SELECT * FROM admission_applications WHERE id=?",(int(application_id),)).fetchone()
            if not row: raise OperationError("The admission application no longer exists.")
            if row["status"]=="Enrolled": raise OperationError("An enrolled application cannot be changed.")
            self.conn.execute("UPDATE admission_applications SET status=?,decision_notes=?,updated_at=? WHERE id=?",
                              (status,notes,timestamp,int(application_id)))
            payload={key:row[key] for key in ("application_number","applicant_name","date_of_birth","desired_class","guardian_name","guardian_contact","guardian_email","applied_on")}
            payload.update({"id":int(application_id),"status":status,"decision_notes":notes,"converted_student_id":row["converted_student_id"]})
            self._enqueue("admission_applications",int(application_id),payload,timestamp)
        return int(application_id)

    def enroll_admission_application(self, application_id, admission_number, class_id=None):
        self._require_module("Students & Enrolment")
        self._require("create")
        with self._transaction():
            application=self.conn.execute("SELECT * FROM admission_applications WHERE id=?",(int(application_id),)).fetchone()
            if not application: raise OperationError("The admission application no longer exists.")
            if application["status"]=="Enrolled" or application["converted_student_id"]:
                raise OperationError("This application has already been enrolled.")
            if application["status"]!="Accepted":
                raise OperationError("Only accepted applicants can be enrolled.")
            student_id=self.create_student(admission_number,application["applicant_name"],application["desired_class"],
                application["date_of_birth"],application["guardian_name"],application["guardian_contact"],"")
            guardian=self.conn.execute("SELECT * FROM student_guardians WHERE student_id=? AND is_primary=1",(int(student_id),)).fetchone()
            if guardian and application["guardian_email"]:
                self.conn.execute("UPDATE student_guardians SET email=? WHERE id=?",(application["guardian_email"],int(guardian["id"])))
                self._enqueue("student_guardians",int(guardian["id"]),{"id":int(guardian["id"]),"student_id":int(student_id),
                    "full_name":guardian["full_name"],"relationship":guardian["relationship"],"contact":guardian["contact"],
                    "email":application["guardian_email"],"is_primary":1},self._stamp())
            if class_id is not None:
                self.assign_student_to_class(student_id,int(class_id))
            timestamp=self._stamp()
            self.conn.execute("UPDATE admission_applications SET status='Enrolled',converted_student_id=?,updated_at=? WHERE id=?",
                              (student_id,timestamp,int(application_id)))
            payload={key:application[key] for key in ("application_number","applicant_name","date_of_birth","desired_class","guardian_name","guardian_contact","guardian_email","applied_on")}
            payload.update({"id":int(application_id),"status":"Enrolled","decision_notes":application["decision_notes"],"converted_student_id":int(student_id)})
            self._enqueue("admission_applications",int(application_id),payload,timestamp)
        return int(student_id)

    def student_guardians(self, student_id):
        self._require_module("Students & Enrolment")
        if not self.conn.execute("SELECT 1 FROM academic_students WHERE id=?",(int(student_id),)).fetchone():
            raise OperationError("The selected student no longer exists.")
        return self.conn.execute("SELECT * FROM student_guardians WHERE student_id=? ORDER BY is_primary DESC,full_name COLLATE NOCASE,id",(int(student_id),)).fetchall()

    def save_student_guardian(self, student_id, full_name, relationship="", contact="", email="", is_primary=False, guardian_id=None):
        self._require_module("Students & Enrolment")
        self._require("edit" if guardian_id is not None else "create")
        if not self.conn.execute("SELECT 1 FROM academic_students WHERE id=?",(int(student_id),)).fetchone():
            raise OperationError("The selected student no longer exists.")
        full_name=str(full_name or "").strip()[:160]
        relationship=str(relationship or "").strip()[:80]
        contact=str(contact or "").strip()[:100]
        email=str(email or "").strip()[:160]
        if not full_name: raise OperationError("Guardian name is required.")
        timestamp=self._stamp()
        with self._transaction():
            if guardian_id is not None and not self.conn.execute(
                "SELECT 1 FROM student_guardians WHERE id=? AND student_id=?",(int(guardian_id),int(student_id))).fetchone():
                raise OperationError("The guardian record no longer exists for this student.")
            existing=self.conn.execute("SELECT * FROM student_guardians WHERE id=? AND student_id=?",(int(guardian_id),int(student_id))).fetchone() if guardian_id is not None else None
            if is_primary:
                previous=self.conn.execute("SELECT * FROM student_guardians WHERE student_id=? AND is_primary=1",(int(student_id),)).fetchall()
                self.conn.execute("UPDATE student_guardians SET is_primary=0,updated_at=? WHERE student_id=?",(timestamp,int(student_id)))
                for row in previous:
                    payload={key:row[key] for key in ("student_id","full_name","relationship","contact","email")}
                    payload.update({"id":int(row["id"]),"is_primary":0})
                    self._enqueue("student_guardians",int(row["id"]),payload,timestamp)
            values=(int(student_id),full_name,relationship,contact,email,int(bool(is_primary)))
            if guardian_id is None:
                cur=self.conn.execute("INSERT INTO student_guardians(student_id,full_name,relationship,contact,email,is_primary,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                      values+(self.actor_username,timestamp,timestamp))
                guardian_id=int(cur.lastrowid)
            else:
                self.conn.execute("UPDATE student_guardians SET student_id=?,full_name=?,relationship=?,contact=?,email=?,is_primary=?,updated_at=? WHERE id=?",
                                  values+(timestamp,int(guardian_id)))
                guardian_id=int(guardian_id)
            if is_primary:
                self.conn.execute("UPDATE academic_students SET guardian_name=?,guardian_contact=?,updated_at=? WHERE id=?",
                                  (full_name,contact,timestamp,int(student_id)))
            elif existing and existing["is_primary"]:
                self.conn.execute("UPDATE academic_students SET guardian_name='',guardian_contact='',updated_at=? WHERE id=?",
                                  (timestamp,int(student_id)))
                student=self.conn.execute("SELECT * FROM academic_students WHERE id=?",(int(student_id),)).fetchone()
                self._enqueue("academic_students",int(student_id),{"id":int(student_id),"admission_number":student["admission_number"],
                    "full_name":student["full_name"],"class_course":student["class_course"],"date_of_birth":student["date_of_birth"],
                    "guardian_name":"","guardian_contact":"","email":student["email"],"status":student["status"]},timestamp)
            payload={"id":guardian_id,"student_id":int(student_id),"full_name":full_name,"relationship":relationship,
                     "contact":contact,"email":email,"is_primary":int(bool(is_primary))}
            self._enqueue("student_guardians",guardian_id,payload,timestamp)
        return guardian_id

    def log_guardian_communication(self, student_id, channel, direction, subject, notes="", guardian_id=None, follow_up_date=""):
        self._require_module("Students & Enrolment")
        self._require("create")
        if not self.conn.execute("SELECT 1 FROM academic_students WHERE id=?",(int(student_id),)).fetchone():
            raise OperationError("The selected student no longer exists.")
        if channel not in {"Phone","Email","SMS","Meeting","Letter","Other"}:
            raise OperationError("Choose a valid communication channel.")
        if direction not in {"Outgoing","Incoming"}:
            raise OperationError("Choose Incoming or Outgoing communication.")
        subject=str(subject or "").strip()[:180]
        if not subject: raise OperationError("Communication subject is required.")
        if guardian_id is not None and not self.conn.execute(
            "SELECT 1 FROM student_guardians WHERE id=? AND student_id=?",(int(guardian_id),int(student_id))).fetchone():
            raise OperationError("Choose a guardian linked to this student.")
        follow_up_date=self._iso_date(follow_up_date,"Follow-up date",allow_blank=True)
        notes=str(notes or "").strip()[:2000]
        timestamp=self._stamp()
        with self._transaction():
            cur=self.conn.execute("INSERT INTO guardian_communications(student_id,guardian_id,channel,direction,subject,notes,follow_up_date,logged_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (int(student_id),int(guardian_id) if guardian_id is not None else None,channel,direction,subject,notes,follow_up_date,self.actor_username,timestamp))
            communication_id=int(cur.lastrowid)
            payload={"id":communication_id,"student_id":int(student_id),"guardian_id":int(guardian_id) if guardian_id is not None else None,
                     "channel":channel,"direction":direction,"subject":subject,"notes":notes,"follow_up_date":follow_up_date}
            self._enqueue("guardian_communications",communication_id,payload,timestamp)
        return communication_id

    def guardian_communications(self, student_id, limit=500):
        self._require_module("Students & Enrolment")
        return self.conn.execute("""SELECT c.*,g.full_name AS guardian_name FROM guardian_communications c
            LEFT JOIN student_guardians g ON g.id=c.guardian_id WHERE c.student_id=?
            ORDER BY c.created_at DESC,c.id DESC LIMIT ?""",(int(student_id),max(1,min(int(limit),2000)))).fetchall()

    def mark_student_attendance(self, student_id, attendance_date, status, note=""):
        self._require_module("Students & Enrolment")
        self._require("edit")
        attendance_date=self._iso_date(attendance_date,"Attendance date")
        if status not in {"Present","Absent","Late","Excused"}:
            raise OperationError("Attendance status must be Present, Absent, Late, or Excused.")
        note=str(note or "").strip()[:500]
        student=self.conn.execute("SELECT id,admission_number FROM academic_students WHERE id=? AND status<>'Withdrawn'",(int(student_id),)).fetchone()
        if not student:
            raise OperationError("The selected student is unavailable or withdrawn.")
        timestamp=self._stamp()
        with self._transaction():
            cur=self.conn.execute("""INSERT INTO student_attendance(student_id,attendance_date,status,note,recorded_by,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(student_id,attendance_date) DO UPDATE SET status=excluded.status,note=excluded.note,recorded_by=excluded.recorded_by,created_at=excluded.created_at""",
                (int(student_id),attendance_date,status,note,self.actor_username,timestamp))
            record=self.conn.execute("SELECT id FROM student_attendance WHERE student_id=? AND attendance_date=?",(int(student_id),attendance_date)).fetchone()
            self._enqueue("student_attendance",record["id"],{"id":record["id"],"student_id":int(student_id),"attendance_date":attendance_date,"status":status,"note":note},timestamp)
        return int(record["id"])

    def student_attendance(self, attendance_date="", student_id=None, class_course="", limit=5000):
        self._require_module("Students & Enrolment")
        sql="""SELECT a.*,s.admission_number,s.full_name,s.class_course FROM student_attendance a
            JOIN academic_students s ON s.id=a.student_id"""
        clauses=[]; params=[]
        if attendance_date:
            clauses.append("a.attendance_date=?"); params.append(self._iso_date(attendance_date,"Attendance date"))
        if student_id is not None:
            clauses.append("a.student_id=?"); params.append(int(student_id))
        if class_course:
            clauses.append("s.class_course=? COLLATE NOCASE"); params.append(str(class_course).strip())
        if clauses: sql+=" WHERE "+" AND ".join(clauses)
        params.append(max(1,min(int(limit),10000)))
        return self.conn.execute(sql+" ORDER BY a.attendance_date DESC,s.class_course COLLATE NOCASE,s.full_name COLLATE NOCASE LIMIT ?",params).fetchall()

    def create_assessment(self, name, subject, class_course="", term="", assessment_date="", max_score=100):
        self._require_module("Students & Enrolment")
        self._require("create")
        name=str(name or "").strip()[:160]; subject=str(subject or "").strip()[:100]
        if not name or not subject:
            raise OperationError("Assessment name and subject are required.")
        assessment_date=self._iso_date(assessment_date,"Assessment date",allow_blank=True)
        maximum=self._decimal(max_score,"Maximum score",minimum=Decimal("0.001"),places=3)
        timestamp=self._stamp()
        with self._transaction():
            cur=self.conn.execute("""INSERT INTO academic_assessments(name,subject,class_course,term,assessment_date,max_score,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",(name,subject,str(class_course or "").strip()[:160],str(term or "").strip()[:100],assessment_date,self._quantity(maximum),self.actor_username,timestamp))
            self._enqueue("academic_assessments",cur.lastrowid,{"id":cur.lastrowid,"name":name,"subject":subject,"class_course":class_course,"term":term,"assessment_date":assessment_date,"max_score":self._quantity(maximum)},timestamp)
        return cur.lastrowid

    def assessments(self, limit=1000):
        self._require_module("Students & Enrolment")
        return self.conn.execute("SELECT * FROM academic_assessments ORDER BY assessment_date DESC,id DESC LIMIT ?",(max(1,min(int(limit),5000)),)).fetchall()

    def record_assessment_result(self, assessment_id, student_id, score, grade="", remarks=""):
        self._require_module("Students & Enrolment")
        self._require("edit")
        assessment=self.conn.execute("SELECT * FROM academic_assessments WHERE id=?",(int(assessment_id),)).fetchone()
        student=self.conn.execute("SELECT * FROM academic_students WHERE id=? AND status<>'Withdrawn'",(int(student_id),)).fetchone()
        if not assessment or not student:
            raise OperationError("Choose an existing assessment and available student.")
        if assessment["class_course"].strip() and student["class_course"].strip().casefold()!=assessment["class_course"].strip().casefold():
            raise OperationError("This student is not enrolled in the assessment's class/course.")
        mark=self._decimal(score,"Score",minimum=Decimal("0"),places=3)
        if mark>Decimal(assessment["max_score"]):
            raise OperationError(f"Score cannot exceed the maximum of {assessment['max_score']}.")
        grade=str(grade or "").strip()[:40]; remarks=str(remarks or "").strip()[:500]; timestamp=self._stamp()
        with self._transaction():
            self.conn.execute("""INSERT INTO academic_results(assessment_id,student_id,score,grade,remarks,recorded_by,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(assessment_id,student_id) DO UPDATE SET score=excluded.score,grade=excluded.grade,remarks=excluded.remarks,recorded_by=excluded.recorded_by,updated_at=excluded.updated_at""",
                (int(assessment_id),int(student_id),self._quantity(mark),grade,remarks,self.actor_username,timestamp))
            record=self.conn.execute("SELECT id FROM academic_results WHERE assessment_id=? AND student_id=?",(int(assessment_id),int(student_id))).fetchone()
            self._enqueue("academic_results",record["id"],{"id":record["id"],"assessment_id":int(assessment_id),"student_id":int(student_id),"score":self._quantity(mark),"grade":grade,"remarks":remarks},timestamp)
        return int(record["id"])

    def assessment_results(self, assessment_id):
        self._require_module("Students & Enrolment")
        return self.conn.execute("""SELECT r.*,s.admission_number,s.full_name,s.class_course,a.name AS assessment_name,a.subject,a.term,a.max_score
            FROM academic_results r JOIN academic_students s ON s.id=r.student_id JOIN academic_assessments a ON a.id=r.assessment_id
            WHERE r.assessment_id=? ORDER BY s.class_course COLLATE NOCASE,s.full_name COLLATE NOCASE""",(int(assessment_id),)).fetchall()

    def student_term_report(self, student_id, term_id):
        self._require_module("Students & Enrolment")
        student=self.conn.execute("SELECT * FROM academic_students WHERE id=?",(int(student_id),)).fetchone()
        term=self.conn.execute("""SELECT t.*,y.name AS academic_year FROM academic_terms t
            JOIN academic_years y ON y.id=t.academic_year_id WHERE t.id=?""",(int(term_id),)).fetchone()
        if not student: raise OperationError("The selected student no longer exists.")
        if not term: raise OperationError("Choose a valid academic term.")
        results=self.conn.execute("""SELECT a.id AS assessment_id,a.name,a.subject,a.assessment_date,a.max_score,
            r.score,r.grade,r.remarks FROM academic_assessments a LEFT JOIN academic_results r
            ON r.assessment_id=a.id AND r.student_id=? WHERE a.term=?
            AND (a.class_course='' OR a.class_course=? COLLATE NOCASE)
            ORDER BY a.assessment_date,a.subject COLLATE NOCASE,a.name COLLATE NOCASE""",
            (int(student_id),term["name"],student["class_course"])).fetchall()
        scored=[]
        for row in results:
            if row["score"] is not None:
                scored.append(Decimal(row["score"])/Decimal(row["max_score"])*Decimal("100"))
        average=(sum(scored,Decimal("0"))/len(scored)) if scored else None
        attendance=self.conn.execute("""SELECT status,COUNT(*) AS total FROM student_attendance
            WHERE student_id=? AND attendance_date BETWEEN ? AND ? GROUP BY status""",
            (int(student_id),term["start_date"],term["end_date"])).fetchall()
        attendance_counts={row["status"]:int(row["total"]) for row in attendance}
        fee=self.conn.execute("""SELECT COALESCE(SUM(CAST(c.amount AS REAL)),0) AS charged,
            COALESCE((SELECT SUM(CAST(a.amount AS REAL)) FROM student_fee_allocations a
                JOIN student_fee_charges paid_charge ON paid_charge.id=a.charge_id
                WHERE paid_charge.student_id=? AND paid_charge.term=?),0) AS paid
            FROM student_fee_charges c WHERE c.student_id=? AND c.term=?""",
            (int(student_id),term["name"],int(student_id),term["name"])).fetchone()
        return {"student":student,"term":term,"results":results,"average_percent":average,
                "attendance":attendance_counts,"fees_charged":Decimal(str(fee["charged"])),
                "fees_paid":Decimal(str(fee["paid"]))}

    def student_transcript(self, student_id):
        self._require_module("Students & Enrolment")
        if not self.conn.execute("SELECT 1 FROM academic_students WHERE id=?",(int(student_id),)).fetchone():
            raise OperationError("The selected student no longer exists.")
        return self.conn.execute("""SELECT a.name,a.subject,a.class_course,a.term,a.assessment_date,a.max_score,
            r.score,r.grade,r.remarks FROM academic_assessments a JOIN academic_results r ON r.assessment_id=a.id
            WHERE r.student_id=? ORDER BY a.assessment_date,a.term COLLATE NOCASE,a.subject COLLATE NOCASE,a.name COLLATE NOCASE""",
            (int(student_id),)).fetchall()

    def create_student_fee_charge(self, student_id, description, amount, term="", due_date=""):
        self._require_module("Students & Enrolment")
        self._require_module("Finance")
        self._require("manage_finance")
        student=self.conn.execute("SELECT id FROM academic_students WHERE id=? AND status<>'Withdrawn'",(int(student_id),)).fetchone()
        if not student: raise OperationError("Choose an existing student who has not withdrawn.")
        description=str(description or "").strip()[:180]
        if not description: raise OperationError("Fee description is required.")
        charge=self._decimal(amount,"Fee charge",minimum=Decimal("0.01"),places=2)
        due_date=self._iso_date(due_date,"Due date",allow_blank=True)
        term=str(term or "").strip()[:100]; timestamp=self._stamp()
        with self._transaction():
            cur=self.conn.execute("INSERT INTO student_fee_charges(student_id,term,description,amount,due_date,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (int(student_id),term,description,self._money(charge),due_date,self.actor_username,timestamp))
            self._enqueue("student_fee_charges",cur.lastrowid,{"id":cur.lastrowid,"student_id":int(student_id),"term":term,"description":description,"amount":self._money(charge),"due_date":due_date},timestamp)
        return cur.lastrowid

    def record_student_fee_payment(self, student_id, amount, method="Cash", reference=""):
        """Record fees and cash in one transaction; allocate first to oldest unpaid charges, retain excess as credit."""
        self._require_module("Students & Enrolment")
        self._require_module("Finance")
        self._require("manage_finance")
        student=self.conn.execute("SELECT id,admission_number,full_name FROM academic_students WHERE id=?",(int(student_id),)).fetchone()
        if not student: raise OperationError("The selected student no longer exists.")
        paid=self._decimal(amount,"Fee payment",minimum=Decimal("0.01"),places=2)
        method=str(method or "Cash").strip()[:60]
        if not method: raise OperationError("Payment method is required.")
        reference=str(reference or "").strip()[:160]; timestamp=self._stamp()
        with self._transaction():
            cur=self.conn.execute("INSERT INTO student_fee_payments(student_id,amount,method,reference,received_by,created_at) VALUES(?,?,?,?,?,?)",
                (int(student_id),self._money(paid),method,reference,self.actor_username,timestamp))
            payment_id=cur.lastrowid
            remaining=paid
            charges=self.conn.execute("""SELECT c.id,c.amount,COALESCE((SELECT SUM(CAST(a.amount AS REAL)) FROM student_fee_allocations a WHERE a.charge_id=c.id),0) AS allocated
                FROM student_fee_charges c WHERE c.student_id=? ORDER BY CASE WHEN c.due_date='' THEN 1 ELSE 0 END,c.due_date,c.id""",(int(student_id),)).fetchall()
            for charge in charges:
                outstanding=max(Decimal("0.00"),Decimal(charge["amount"])-Decimal(str(charge["allocated"])).quantize(Decimal("0.01")))
                if outstanding<=0 or remaining<=0: continue
                allocation=min(outstanding,remaining)
                self.conn.execute("INSERT INTO student_fee_allocations(payment_id,charge_id,amount) VALUES(?,?,?)",
                    (payment_id,int(charge["id"]),self._money(allocation)))
                remaining-=allocation
            self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at) VALUES('in',?,?,?,?,?)",
                (self._money(paid),"student_fee_payment",payment_id,f"Fees · {student['admission_number']} · {student['full_name']} · {method} · {reference}",timestamp))
            self._enqueue("student_fee_payments",payment_id,{"id":payment_id,"student_id":int(student_id),"amount":self._money(paid),"method":method,"reference":reference},timestamp)
        return payment_id

    def student_fee_accounts(self, limit=5000):
        self._require_module("Students & Enrolment")
        self._require_module("Finance")
        return self.conn.execute("""SELECT s.id,s.admission_number,s.full_name,s.class_course,
            COALESCE((SELECT SUM(CAST(c.amount AS REAL)) FROM student_fee_charges c WHERE c.student_id=s.id),0) AS charged,
            COALESCE((SELECT SUM(CAST(p.amount AS REAL)) FROM student_fee_payments p WHERE p.student_id=s.id),0) AS paid
            FROM academic_students s ORDER BY s.class_course COLLATE NOCASE,s.full_name COLLATE NOCASE LIMIT ?""",(max(1,min(int(limit),10000)),)).fetchall()

    def student_fee_statement(self, student_id):
        self._require_module("Students & Enrolment")
        self._require_module("Finance")
        charges=self.conn.execute("""SELECT c.*,COALESCE((SELECT SUM(CAST(a.amount AS REAL)) FROM student_fee_allocations a WHERE a.charge_id=c.id),0) AS allocated
            FROM student_fee_charges c WHERE c.student_id=? ORDER BY c.id""",(int(student_id),)).fetchall()
        payments=self.conn.execute("SELECT * FROM student_fee_payments WHERE student_id=? ORDER BY id",(int(student_id),)).fetchall()
        return {"charges":charges,"payments":payments}

    # ------------------------------------------------------------------ Appointments / service bookings
    @staticmethod
    def _appointment_datetime(value):
        text=str(value or "").strip()
        if not text:
            raise OperationError("Appointment date and start time are required.")
        try:
            parsed=datetime.fromisoformat(text)
        except ValueError as exc:
            raise OperationError("Use an appointment date and time such as 2026-10-27 14:30.") from exc
        if parsed.second or parsed.microsecond:
            parsed=parsed.replace(second=0,microsecond=0)
        return parsed.strftime("%Y-%m-%d %H:%M")

    def create_appointment(self, customer_name, service_name, starts_at, staff_member="", duration_minutes=30, customer_contact="", notes=""):
        self._require_module("Appointments")
        self._require("create")
        customer_name=str(customer_name or "").strip()[:160]
        service_name=str(service_name or "").strip()[:160]
        if not customer_name or not service_name:
            raise OperationError("Customer and service are required.")
        starts_at=self._appointment_datetime(starts_at)
        try: duration=int(duration_minutes)
        except (TypeError,ValueError) as exc: raise OperationError("Appointment duration must be 5–1,440 minutes.") from exc
        if not 5<=duration<=1440: raise OperationError("Appointment duration must be 5–1,440 minutes.")
        staff_member=str(staff_member or "").strip()[:160]
        customer_contact=str(customer_contact or "").strip()[:100]
        notes=str(notes or "").strip()[:1000]
        start_dt=datetime.fromisoformat(starts_at); end_dt=start_dt.timestamp()+duration*60
        timestamp=self._stamp()
        try:
            with self._transaction():
                if staff_member:
                    existing=self.conn.execute("SELECT starts_at,duration_minutes,customer_name FROM appointments WHERE staff_member=? COLLATE NOCASE AND status IN ('Booked','Confirmed','Arrived','In service')",(staff_member,)).fetchall()
                    for row in existing:
                        other_start=datetime.fromisoformat(row["starts_at"]).timestamp()
                        other_end=other_start+int(row["duration_minutes"])*60
                        if start_dt.timestamp()<other_end and end_dt>other_start:
                            raise OperationError(f"{staff_member} already has an overlapping appointment with {row['customer_name']}.")
                cur=self.conn.execute("""INSERT INTO appointments(customer_name,customer_contact,service_name,staff_member,starts_at,duration_minutes,status,notes,created_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'Booked',?,?,?,?)""",(customer_name,customer_contact,service_name,staff_member,starts_at,duration,notes,self.actor_username,timestamp,timestamp))
                self._enqueue("appointments",cur.lastrowid,{"id":cur.lastrowid,"customer_name":customer_name,"customer_contact":customer_contact,"service_name":service_name,"staff_member":staff_member,"starts_at":starts_at,"duration_minutes":duration,"status":"Booked","notes":notes},timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError("The appointment could not be saved because its data conflicts with an existing record.") from exc
        return int(cur.lastrowid)

    def update_appointment_status(self, appointment_id, status, notes=None):
        self._require_module("Appointments")
        self._require("edit")
        allowed={"Booked","Confirmed","Arrived","In service","Completed","No show","Cancelled"}
        if status not in allowed: raise OperationError("Choose a valid appointment status.")
        timestamp=self._stamp()
        with self._transaction():
            row=self.conn.execute("SELECT * FROM appointments WHERE id=?",(int(appointment_id),)).fetchone()
            if not row: raise OperationError("Appointment not found.")
            old=row["status"]
            transitions={"Booked":{"Confirmed","Cancelled","No show"},"Confirmed":{"Arrived","Cancelled","No show"},"Arrived":{"In service","Completed","Cancelled"},"In service":{"Completed","Cancelled"},"Completed":set(),"No show":set(),"Cancelled":set()}
            if status!=old and status not in transitions[old]: raise OperationError(f"Cannot change appointment from {old} to {status}.")
            updated_notes=row["notes"] if notes is None else str(notes or "").strip()[:1000]
            self.conn.execute("UPDATE appointments SET status=?,notes=?,updated_at=? WHERE id=?",(status,updated_notes,timestamp,int(appointment_id)))
            self._enqueue("appointments",int(appointment_id),{"id":int(appointment_id),"status":status,"notes":updated_notes},timestamp)
        return int(appointment_id)

    def appointments(self, date_from="", date_to="", status="", staff_member="", limit=5000):
        self._require_module("Appointments")
        sql="SELECT * FROM appointments WHERE 1=1"; params=[]
        if date_from:
            try: datetime.strptime(str(date_from),"%Y-%m-%d")
            except ValueError as exc: raise OperationError("Start date must use YYYY-MM-DD.") from exc
            sql+=" AND substr(starts_at,1,10)>=?"; params.append(str(date_from))
        if date_to:
            try: datetime.strptime(str(date_to),"%Y-%m-%d")
            except ValueError as exc: raise OperationError("End date must use YYYY-MM-DD.") from exc
            sql+=" AND substr(starts_at,1,10)<=?"; params.append(str(date_to))
        if status:
            sql+=" AND status=?"; params.append(str(status))
        if staff_member:
            sql+=" AND staff_member=? COLLATE NOCASE"; params.append(str(staff_member).strip())
        params.append(max(1,min(int(limit),10000)))
        return self.conn.execute(sql+" ORDER BY starts_at,id LIMIT ?",params).fetchall()

    # ------------------------------------------------------------------ Inventory lot / expiry traceability
    @staticmethod
    def _batch_expiry_date(value, *, allow_blank=True):
        text=str(value or "").strip()
        if not text and allow_blank:
            return ""
        try:
            datetime.strptime(text,"%Y-%m-%d")
        except ValueError as exc:
            raise OperationError("Batch expiry date must use YYYY-MM-DD, or be blank for items without an expiry date.") from exc
        return text

    def _batch_balance(self, product_id):
        row=self.conn.execute("SELECT COALESCE(SUM(CAST(remaining_quantity AS REAL)),0) FROM inventory_batches WHERE product_id=? AND status IN ('In stock','Quarantined')",(int(product_id),)).fetchone()
        return Decimal(str(row[0] or 0)).quantize(Decimal("0.001"))

    def _insert_inventory_batch(self, product_id, lot_number, expiry_date, quantity, supplier_name="", source_type="manual", source_id=None, notes="", timestamp=None, status="In stock"):
        lot_number=str(lot_number or "").strip()[:100]
        if not lot_number:
            raise OperationError("Enter a batch / lot number for batch-tracked stock.")
        expiry_date=self._batch_expiry_date(expiry_date)
        quantity=self._decimal(quantity,"Batch quantity",minimum=Decimal("0.001"),places=3)
        if status not in {"In stock","Quarantined","Disposed","Cancelled"}:
            raise OperationError("Invalid inventory batch status.")
        timestamp=timestamp or self._stamp()
        supplier_name=str(supplier_name or "").strip()[:160]
        notes=str(notes or "").strip()[:500]
        try:
            cur=self.conn.execute("""INSERT INTO inventory_batches(product_id,lot_number,expiry_date,received_quantity,remaining_quantity,supplier_name,status,source_type,source_id,notes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(int(product_id),lot_number,expiry_date,self._quantity(quantity),self._quantity(quantity),supplier_name,status,str(source_type or "manual")[:40],source_id,notes,timestamp,timestamp))
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"Batch / lot {lot_number!r} already exists for this product.") from exc
        self._enqueue("inventory_batches",cur.lastrowid,{"id":cur.lastrowid,"product_id":int(product_id),"lot_number":lot_number,"expiry_date":expiry_date,"received_quantity":self._quantity(quantity),"remaining_quantity":self._quantity(quantity),"supplier_name":supplier_name,"status":status,"source_type":source_type,"source_id":source_id,"notes":notes},timestamp)
        return int(cur.lastrowid)

    def inventory_batches(self, product_id=None, status="", expiring_before="", include_empty=False, limit=10000):
        self._require_module("Batches & Expiry")
        sql="SELECT b.*,p.sku,p.name AS product_name,p.unit,p.track_batches FROM inventory_batches b JOIN products p ON p.id=b.product_id WHERE 1=1"
        params=[]
        if product_id is not None: sql+=" AND b.product_id=?"; params.append(int(product_id))
        if status: sql+=" AND b.status=?"; params.append(str(status))
        if expiring_before:
            expiring_before=self._batch_expiry_date(expiring_before,allow_blank=False)
            sql+=" AND b.expiry_date<>'' AND b.expiry_date<=?"; params.append(expiring_before)
        if not include_empty: sql+=" AND CAST(b.remaining_quantity AS REAL)>0"
        params.append(max(1,min(int(limit),20000)))
        return self.conn.execute(sql+" ORDER BY CASE WHEN b.expiry_date='' THEN 1 ELSE 0 END,b.expiry_date,b.id LIMIT ?",params).fetchall()

    def batch_expiry_alerts(self, days=90):
        self._require_module("Batches & Expiry")
        try: days=max(0,min(int(days),3650))
        except (TypeError,ValueError) as exc: raise OperationError("Expiry alert days must be a whole number from 0 to 3,650.") from exc
        today=datetime.now().date(); horizon=(today.fromordinal(today.toordinal()+days)).isoformat()
        return self.conn.execute("""SELECT b.*,p.sku,p.name AS product_name,p.unit FROM inventory_batches b JOIN products p ON p.id=b.product_id
            WHERE b.status IN ('In stock','Quarantined') AND CAST(b.remaining_quantity AS REAL)>0 AND b.expiry_date<>'' AND b.expiry_date<=?
            ORDER BY b.expiry_date,b.id""",(horizon,)).fetchall()

    def enable_product_batch_tracking(self, product_id, enabled=True, opening_lot_number="", opening_expiry_date=""):
        self._require_module("Batches & Expiry")
        self._require("manage_inventory")
        timestamp=self._stamp(); opening_lot_number=str(opening_lot_number or "").strip()[:100]
        opening_expiry_date=self._batch_expiry_date(opening_expiry_date)
        with self._transaction():
            product=self.conn.execute("SELECT * FROM products WHERE id=?",(int(product_id),)).fetchone()
            if not product: raise OperationError("Product not found.")
            balance=self.stock_on_hand(product_id); batches=self._batch_balance(product_id)
            if enabled:
                if batches>balance: raise OperationError("Tracked batch quantities exceed stock-ledger stock; reconcile them before enabling tracking.")
                missing=balance-batches
                if missing>Decimal("0.000"):
                    if not opening_lot_number: raise OperationError(f"Enter an opening lot number for {self._quantity(missing)} {product['unit']} of existing stock.")
                    self._insert_inventory_batch(product_id,opening_lot_number,opening_expiry_date,missing,"","opening-allocation",product_id,"Existing stock allocated to batch tracking",timestamp)
                self.conn.execute("UPDATE products SET track_batches=1 WHERE id=?",(int(product_id),))
            else:
                if batches>Decimal("0.000"): raise OperationError("Batch tracking cannot be disabled while any batch stock remains. Dispose or reconcile the batches first.")
                self.conn.execute("UPDATE products SET track_batches=0 WHERE id=?",(int(product_id),))
            self._enqueue("products",int(product_id),{"id":int(product_id),"track_batches":bool(enabled)},timestamp)
        return bool(enabled)

    def receive_inventory_batch(self, product_id, lot_number, quantity, expiry_date="", supplier_name="", unit_cost=None, amount_paid=0, notes="", payment_method="Cash", payment_reference=""):
        """Receive a batch-tracked product as a normal purchase, stock movement, cash entry and lot record atomically."""
        self._require_module("Batches & Expiry"); self._require_module("Purchases"); self._require("manage_inventory")
        qty=self._decimal(quantity,"Batch received quantity",minimum=Decimal("0.001"),places=3)
        expiry_date=self._batch_expiry_date(expiry_date)
        timestamp=self._stamp()
        with self._transaction():
            product=self.conn.execute("SELECT * FROM products WHERE id=? AND active=1",(int(product_id),)).fetchone()
            if not product: raise OperationError("Choose an active product.")
            if not bool(product["track_batches"]): raise OperationError("Enable batch tracking for this product before receiving a lot.")
            cost=self._decimal(product["cost_price"] if unit_cost is None else unit_cost,"Unit cost",minimum=Decimal("0"))
            total=(qty*cost).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            paid=self._decimal(amount_paid,"Amount paid",minimum=Decimal("0"))
            payment_method=self._payment_method(payment_method)
            payment_reference=str(payment_reference or "").strip()[:120]
            if paid>total: raise OperationError("Amount paid cannot exceed the batch receipt total.")
            if paid>0 and payment_method=="M-Pesa" and not payment_reference:
                raise OperationError("Enter the confirmed M-Pesa transaction code for this payment.")
            supplier_name=str(supplier_name or "").strip()[:160]
            existing=self.stock_on_hand(product_id); new_qty=existing+qty
            old_cost=Decimal(product["cost_price"])
            average=(old_cost*existing+total)/new_qty if new_qty else old_cost
            cur=self.conn.execute("INSERT INTO purchases(purchase_number,supplier_id,supplier_name,status,payment_status,total,paid_total,created_at) VALUES('',NULL,?,'Received',?,?,?,?)",
                (supplier_name,self._payment_status(total,paid),self._money(total),self._money(paid),timestamp))
            purchase_id=cur.lastrowid; purchase_number=self._next_number("PUR",purchase_id)
            self.conn.execute("UPDATE purchases SET purchase_number=? WHERE id=?",(purchase_number,purchase_id))
            self.conn.execute("INSERT INTO purchase_lines(purchase_id,product_id,sku,description,quantity,unit_cost,line_total,prior_cost) VALUES(?,?,?,?,?,?,?,?)",
                (purchase_id,product_id,product["sku"],product["name"],self._quantity(qty),self._money(cost),self._money(total),self._money(old_cost)))
            self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (int(product_id),self._quantity(qty),"purchase_receipt","purchase",purchase_id,purchase_number,timestamp))
            batch_id=self._insert_inventory_batch(product_id,lot_number,expiry_date,qty,supplier_name,"purchase",purchase_id,notes,timestamp)
            self.conn.execute("UPDATE products SET cost_price=? WHERE id=?",(self._money(average),int(product_id)))
            if paid:
                self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES('out',?,?,?,?,?,?,?,?)",
                    (self._money(paid),"purchase",purchase_id,f"Payment for {purchase_number}",timestamp,payment_method,payment_reference,"Supplier payment"))
            self._enqueue("purchases",purchase_id,{"id":purchase_id,"purchase_number":purchase_number,"supplier_name":supplier_name,"status":"Received","total":self._money(total),"paid_total":self._money(paid),"payment_method":payment_method,"payment_reference":payment_reference,"lines":[{"product_id":int(product_id),"sku":product["sku"],"quantity":self._quantity(qty),"unit_cost":self._money(cost),"batch_id":batch_id,"lot_number":lot_number,"expiry_date":expiry_date}]},timestamp)
        return int(purchase_id)

    def update_inventory_batch_status(self, batch_id, status, reason=""):
        self._require_module("Batches & Expiry"); self._require("manage_inventory")
        if status not in {"In stock","Quarantined","Disposed"}: raise OperationError("Choose In stock, Quarantined, or Disposed.")
        timestamp=self._stamp()
        with self._transaction():
            batch=self.conn.execute("SELECT * FROM inventory_batches WHERE id=?",(int(batch_id),)).fetchone()
            if not batch: raise OperationError("Inventory batch not found.")
            if batch["status"] in {"Disposed","Cancelled"}: raise OperationError("Disposed or cancelled batches cannot be reopened; receive a new traced batch instead.")
            expiry=batch["expiry_date"]
            if status=="In stock" and expiry and expiry<timestamp[:10]: raise OperationError("Expired stock cannot be returned to sellable status.")
            remaining=Decimal(batch["remaining_quantity"])
            if status=="Disposed" and remaining>0:
                if not bool(self.conn.execute("SELECT track_batches FROM products WHERE id=?",(batch["product_id"],)).fetchone()[0]):
                    raise OperationError("Product batch tracking is not enabled; resolve the catalogue configuration first.")
                self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                    (batch["product_id"],self._quantity(-remaining),"batch_disposal","inventory_batch",int(batch_id),str(reason or "Batch disposed")[:240],timestamp))
                self.conn.execute("UPDATE inventory_batches SET remaining_quantity='0.000' WHERE id=?",(int(batch_id),))
                movement_id=self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                self._enqueue("stock_movements",movement_id,{"id":movement_id,"product_id":batch["product_id"],"quantity_delta":self._quantity(-remaining),"reason":str(reason or "Batch disposed")[:240]},timestamp)
            self.conn.execute("UPDATE inventory_batches SET status=?,updated_at=?,notes=CASE WHEN ?='' THEN notes ELSE notes||char(10)||? END WHERE id=?",
                (status,timestamp,str(reason or "").strip()[:500],str(reason or "").strip()[:500],int(batch_id)))
            self._enqueue("inventory_batches",int(batch_id),{"id":int(batch_id),"status":status,"remaining_quantity":"0.000" if status=="Disposed" else self._quantity(remaining),"reason":str(reason or "")},timestamp)
        return int(batch_id)

    def _consume_batches(self, product_id, quantity, timestamp, sale_line_id=None, allow_expired=False, recipe_consumption_id=None):
        qty=self._decimal(quantity,"Batch consumption quantity",minimum=Decimal("0.001"),places=3)
        eligible="status IN ('In stock','Quarantined') AND CAST(remaining_quantity AS REAL)>0"
        if not allow_expired:
            eligible="status='In stock' AND CAST(remaining_quantity AS REAL)>0 AND (expiry_date='' OR expiry_date>=?)"
            today=datetime.now().strftime("%Y-%m-%d")
            rows=self.conn.execute(f"SELECT * FROM inventory_batches WHERE product_id=? AND {eligible} ORDER BY CASE WHEN expiry_date='' THEN 1 ELSE 0 END,expiry_date,id",(int(product_id),today)).fetchall()
        else:
            rows=self.conn.execute(f"SELECT * FROM inventory_batches WHERE product_id=? AND {eligible} ORDER BY CASE WHEN expiry_date='' THEN 1 ELSE 0 END,expiry_date,id",(int(product_id),)).fetchall()
        available=sum((Decimal(row["remaining_quantity"]) for row in rows),Decimal("0.000"))
        if available<qty: raise OperationError(f"Insufficient unexpired, sellable batch stock. Available by lot: {self._quantity(available)}.")
        remaining=qty
        for row in rows:
            if remaining<=0:break
            take=min(remaining,Decimal(row["remaining_quantity"]))
            new_remaining=Decimal(row["remaining_quantity"])-take
            self.conn.execute("UPDATE inventory_batches SET remaining_quantity=?,updated_at=? WHERE id=?",(self._quantity(new_remaining),timestamp,int(row["id"])))
            self._enqueue("inventory_batches",int(row["id"]),{"id":int(row["id"]),"remaining_quantity":self._quantity(new_remaining)},timestamp)
            if sale_line_id is not None:
                self.conn.execute("INSERT INTO sale_line_batch_allocations(sale_line_id,batch_id,quantity,created_at) VALUES(?,?,?,?)",(int(sale_line_id),int(row["id"]),self._quantity(take),timestamp))
            elif recipe_consumption_id is not None:
                self.conn.execute("INSERT INTO recipe_sale_batch_allocations(consumption_id,batch_id,quantity,created_at) VALUES(?,?,?,?)",(int(recipe_consumption_id),int(row["id"]),self._quantity(take),timestamp))
            remaining-=take
        return True

    def _restore_batches_for_sale_line(self, sale_line_id, quantity, timestamp, return_line_id=None):
        qty=self._decimal(quantity,"Batch restoration quantity",minimum=Decimal("0.001"),places=3)
        allocated=self.conn.execute("SELECT * FROM sale_line_batch_allocations WHERE sale_line_id=? ORDER BY id",(int(sale_line_id),)).fetchall()
        if not allocated:
            # Historical sales made before lot tracking was enabled have no original lot trace.
            # Return/cancellation stock is isolated in quarantine rather than made sellable by assumption.
            line=self.conn.execute("SELECT product_id,sku FROM sale_lines WHERE id=?",(int(sale_line_id),)).fetchone()
            source="return" if return_line_id is not None else "sale-cancel"
            lot=f"UNTRACED-{source}-{return_line_id or sale_line_id}"
            self._insert_inventory_batch(line["product_id"],lot,"",qty,"",source,return_line_id or sale_line_id,"Historical stock restored without an original lot; quarantined for review",timestamp,status="Quarantined")
            return
        prior={int(row["batch_id"]):Decimal(row["quantity"]) for row in self.conn.execute("SELECT rba.batch_id,SUM(CAST(rba.quantity AS REAL)) AS quantity FROM sale_return_batch_allocations rba JOIN sale_return_lines rl ON rl.id=rba.return_line_id WHERE rl.sale_line_id=? GROUP BY rba.batch_id",(int(sale_line_id),)).fetchall()}
        remaining=qty
        for allocation in allocated:
            batch_id=int(allocation["batch_id"])
            capacity=max(Decimal("0.000"),Decimal(allocation["quantity"])-prior.get(batch_id,Decimal("0.000")))
            if capacity<=0:continue
            take=min(remaining,capacity)
            if take<=0:continue
            batch=self.conn.execute("SELECT * FROM inventory_batches WHERE id=?",(batch_id,)).fetchone()
            if batch and batch["status"] not in {"Disposed","Cancelled"}:
                new_qty=Decimal(batch["remaining_quantity"])+take
                self.conn.execute("UPDATE inventory_batches SET remaining_quantity=?,updated_at=? WHERE id=?",(self._quantity(new_qty),timestamp,batch_id))
                self._enqueue("inventory_batches",batch_id,{"id":batch_id,"remaining_quantity":self._quantity(new_qty)},timestamp)
                if return_line_id is not None:
                    self.conn.execute("INSERT INTO sale_return_batch_allocations(return_line_id,batch_id,quantity,created_at) VALUES(?,?,?,?)",(int(return_line_id),batch_id,self._quantity(take),timestamp))
            else:
                line=self.conn.execute("SELECT product_id FROM sale_lines WHERE id=?",(int(sale_line_id),)).fetchone()
                lot=f"UNTRACED-RETURN-{return_line_id or sale_line_id}-{batch_id}"
                self._insert_inventory_batch(line["product_id"],lot,"",take,"","return",return_line_id or sale_line_id,"Original lot was disposed; returned item quarantined",timestamp,status="Quarantined")
            remaining-=take
            if remaining<=0:break
        if remaining>Decimal("0.000"):
            line=self.conn.execute("SELECT product_id FROM sale_lines WHERE id=?",(int(sale_line_id),)).fetchone()
            lot=f"UNTRACED-RETURN-{return_line_id or sale_line_id}-EXTRA"
            self._insert_inventory_batch(line["product_id"],lot,"",remaining,"","return",return_line_id or sale_line_id,"Returned quantity lacks original lot allocation; quarantined for review",timestamp,status="Quarantined")

    # ------------------------------------------------------------------ Product identity / SKU catalogue
    @staticmethod
    def _clean_prefix(value, fallback="GEN"):
        value = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()[:10]
        return value or fallback

    def sku_numbering_settings(self):
        self._require_any_module(("Inventory", "System Settings"))
        row = self.conn.execute("SELECT default_prefix,padding,category_prefixes_json FROM sku_settings WHERE id=1").fetchone()
        if not row:
            return {"default_prefix": "GEN", "padding": 6, "category_prefixes": {}}
        try:
            rules = json.loads(row[2] or "{}")
            if not isinstance(rules, dict):
                rules = {}
        except (TypeError, ValueError):
            rules = {}
        return {"default_prefix": row[0], "padding": int(row[1]), "category_prefixes": rules}

    def configure_sku_numbering(self, default_prefix="GEN", padding=6, category_prefixes=None):
        self._require_module("Inventory")
        self._require("configure")
        prefix = self._clean_prefix(default_prefix)
        try:
            padding = int(padding)
        except (TypeError, ValueError) as exc:
            raise OperationError("SKU padding must be a whole number from 2 to 10.") from exc
        if not 2 <= padding <= 10:
            raise OperationError("SKU padding must be a whole number from 2 to 10.")
        rules = {}
        if category_prefixes is not None:
            if not isinstance(category_prefixes, dict):
                raise OperationError("Category SKU rules must be a mapping of category names to prefixes.")
            for category, cat_prefix in category_prefixes.items():
                category = str(category).strip()
                if not category or len(category) > 80:
                    raise OperationError("Each SKU category rule needs a category name of 1–80 characters.")
                rules[category.casefold()] = self._clean_prefix(cat_prefix, prefix)
        with self._transaction():
            self.conn.execute("UPDATE sku_settings SET default_prefix=?,padding=?,category_prefixes_json=? WHERE id=1",
                              (prefix, padding, json.dumps(rules, ensure_ascii=False, sort_keys=True)))
        return {"default_prefix": prefix, "padding": padding, "category_prefixes": rules}

    def _sku_prefix_for_category(self, category, settings=None):
        settings = settings or self.sku_numbering_settings()
        key = str(category or "").strip().casefold()
        rules = settings.get("category_prefixes", {})
        if key and key in rules:
            return self._clean_prefix(rules[key], settings["default_prefix"])
        words = re.findall(r"[A-Za-z0-9]+", str(category or ""))
        if words:
            if len(words) > 1:
                candidate = "".join(word[0] for word in words[:4])
            else:
                candidate = words[0][:4]
            return self._clean_prefix(candidate, settings["default_prefix"])
        return self._clean_prefix(settings.get("default_prefix", "GEN"))

    def _brand_code(self, brand, fallback="GEN"):
        """Map common electronics brands to stable SKU prefixes so Dell groups as DELL-*."""
        known = {
            "dell": "DELL", "hp": "HP", "hewlett packard": "HP", "hewlett-packard": "HP",
            "lenovo": "LENOVO", "apple": "APPLE", "asus": "ASUS", "acer": "ACER",
            "samsung": "SAMSUNG", "microsoft": "MSFT", "logitech": "LOGI",
            "tp-link": "TPLINK", "tplink": "TPLINK", "kingston": "KSTN",
            "seagate": "SEAG", "western digital": "WD", "wd": "WD", "sony": "SONY",
            "canon": "CANON", "epson": "EPSON", "msi": "MSI", "infinix": "INFINIX",
            "tecno": "TECNO", "generic": "GEN", "crucial": "CRUCIAL",
        }
        key = re.sub(r"[^a-z0-9]+", " ", str(brand or "").strip().casefold()).strip()
        if key in known:
            return known[key]
        tight = key.replace(" ", "")
        if tight in known:
            return known[tight]
        return self._clean_prefix(brand, fallback)

    def _sku_prefix_for_product(self, brand="", category="", settings=None):
        """Brand-first SKU prefix: DELL-LAP, HP-PHN, GEN-CAB so same-brand stock stays together."""
        settings = settings or self.sku_numbering_settings()
        brand_code = self._brand_code(brand, "")
        cat_code = self._sku_prefix_for_category(category, settings)
        if brand_code:
            if cat_code and cat_code != brand_code:
                return f"{brand_code}-{cat_code}"
            return brand_code
        return cat_code or self._clean_prefix(settings.get("default_prefix", "GEN"))

    def _sku_candidate(self, prefix, number, padding):
        return f"{prefix}-{int(number):0{int(padding)}d}"

    def suggest_sku(self, category="", brand=""):
        """Return a suggestion without reserving it; create_product allocates under a write lock."""
        self._require_any_module(("Inventory", "System Settings"))
        settings = self.sku_numbering_settings()
        prefix = self._sku_prefix_for_product(brand, category, settings)
        row = self.conn.execute("SELECT next_value FROM sku_sequences WHERE prefix=? COLLATE NOCASE", (prefix,)).fetchone()
        number = int(row[0]) if row else 1
        while True:
            candidate = self._sku_candidate(prefix, number, settings["padding"])
            exists = self.conn.execute("SELECT 1 FROM products WHERE sku=? COLLATE NOCASE LIMIT 1", (candidate,)).fetchone()
            if not exists:
                return candidate
            number += 1

    def _allocate_sku(self, category="", brand=""):
        """Allocate the next unique brand-matched SKU while already inside BEGIN IMMEDIATE."""
        row = self.conn.execute("SELECT default_prefix,padding,category_prefixes_json FROM sku_settings WHERE id=1").fetchone()
        try:
            rules = json.loads(row[2] or "{}") if row else {}
        except (TypeError, ValueError):
            rules = {}
        settings = {"default_prefix": row[0] if row else "GEN", "padding": int(row[1]) if row else 6,
                    "category_prefixes": rules if isinstance(rules, dict) else {}}
        prefix = self._sku_prefix_for_product(brand, category, settings)
        row = self.conn.execute("SELECT next_value FROM sku_sequences WHERE prefix=? COLLATE NOCASE", (prefix,)).fetchone()
        number = int(row[0]) if row else 1
        while True:
            candidate = self._sku_candidate(prefix, number, settings["padding"])
            exists = self.conn.execute("SELECT 1 FROM products WHERE sku=? COLLATE NOCASE LIMIT 1", (candidate,)).fetchone()
            number += 1
            if not exists:
                self.conn.execute("INSERT INTO sku_sequences(prefix,next_value) VALUES(?,?) ON CONFLICT(prefix) DO UPDATE SET next_value=excluded.next_value",
                                  (prefix, number))
                return candidate

    def get_product(self, product_id):
        self._require_any_module(("Inventory", "Sales", "Purchases", "Reports", "Repair Tickets", "Point of Sale"))
        row = self.conn.execute("""SELECT p.*, COALESCE((SELECT SUM(CAST(m.quantity_delta AS REAL))
            FROM stock_movements m WHERE m.product_id=p.id),0) AS stock_on_hand
            FROM products p WHERE p.id=?""", (product_id,)).fetchone()
        return _as_dict(row)

    def product_barcodes(self, product_id):
        self._require_any_module(("Inventory", "Sales", "Purchases", "Point of Sale"))
        return self.conn.execute("SELECT * FROM product_barcodes WHERE product_id=? ORDER BY id", (product_id,)).fetchall()

    def add_product_barcode(self, product_id, barcode, label=""):
        self._require_module("Inventory")
        self._require("manage_inventory")
        barcode = str(barcode or "").strip()
        label = str(label or "").strip()[:80]
        if not barcode or len(barcode) > 128 or any(ord(ch) < 32 for ch in barcode):
            raise OperationError("Enter a barcode of 1–128 characters without control characters.")
        with self._transaction():
            product = self.conn.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
            if not product:
                raise OperationError("The selected product no longer exists.")
            duplicate = self.conn.execute("SELECT product_id FROM product_barcodes WHERE barcode=? COLLATE NOCASE", (barcode,)).fetchone()
            if duplicate and int(duplicate[0]) != int(product_id):
                raise OperationError(f"Barcode {barcode!r} is already assigned to another product.")
            if duplicate:
                return int(self.conn.execute("SELECT id FROM product_barcodes WHERE barcode=? COLLATE NOCASE", (barcode,)).fetchone()[0])
            # Backward-compatible collision guard for databases whose primary barcode table predates aliases.
            duplicate = self.conn.execute("SELECT id FROM products WHERE barcode=? COLLATE NOCASE", (barcode,)).fetchone()
            if duplicate and int(duplicate[0]) != int(product_id):
                raise OperationError(f"Barcode {barcode!r} is already assigned to another product.")
            timestamp = self._stamp()
            cur = self.conn.execute("INSERT INTO product_barcodes(product_id,barcode,label,created_at) VALUES(?,?,?,?)",
                                    (product_id, barcode, label, timestamp))
            self._enqueue("product_barcodes", cur.lastrowid, {"id": cur.lastrowid, "product_id": int(product_id),
                "barcode": barcode, "label": label}, timestamp)
        return cur.lastrowid

    @staticmethod
    def _variant_attributes_json(value):
        if value is None or value == "":
            return "{}"
        if isinstance(value, dict):
            payload = value
        else:
            raw = str(value).strip()
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                # Human-friendly entry accepted by the product editor and spreadsheets:
                # "Size=XL, Colour=Blue". JSON is still accepted for complex imports.
                payload = {}
                if not raw:
                    return "{}"
                for part in re.split(r"[,;]", raw):
                    if "=" not in part:
                        raise OperationError('Enter variants as "Size=XL, Colour=Blue" or provide a valid JSON object.')
                    key, item = part.split("=", 1)
                    key, item = key.strip(), item.strip()
                    if not key or not item:
                        raise OperationError('Each variant option must include a name and value, for example "Size=XL".')
                    payload[key] = item
        if not isinstance(payload, dict):
            raise OperationError("Variant attributes must be a JSON object of attribute names and values.")
        normalized = {}
        for key, item in payload.items():
            key = str(key).strip()
            item = str(item).strip()
            if not key or len(key) > 60 or not item or len(item) > 120:
                raise OperationError("Variant attribute names must be 1–60 characters and values 1–120 characters.")
            normalized[key.casefold()] = item
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _variant_attributes_text(value):
        try:
            payload = json.loads(str(value or "{}"))
        except (TypeError, ValueError):
            return str(value or "")
        if not isinstance(payload, dict) or not payload:
            return ""
        return ", ".join(f"{str(key).replace('_', ' ').title()}={item}" for key, item in payload.items())

    def product_variants(self, variant_group):
        self._require_any_module(("Inventory", "Sales", "Purchases", "Point of Sale"))
        group = str(variant_group or "").strip()
        if not group:
            return []
        rows = self.conn.execute("""SELECT p.*, COALESCE((SELECT SUM(CAST(m.quantity_delta AS REAL))
            FROM stock_movements m WHERE m.product_id=p.id),0) AS stock_on_hand
            FROM products p WHERE p.variant_group=? COLLATE NOCASE ORDER BY p.id""", (group,)).fetchall()
        return rows

    # ------------------------------------------------------------------ Restaurant menu, recipes and kitchen queue
    def create_menu_item(self, name, selling_price, category="", sku="", kitchen_station="Kitchen", prep_minutes=10, recipe_required=True, notes=""):
        self._require_module("Menu & Recipes"); self._require("create")
        name=str(name or "").strip()[:160]; category=str(category or "").strip()[:80]
        kitchen_station=str(kitchen_station or "Kitchen").strip()[:80] or "Kitchen"
        notes=str(notes or "").strip()[:500]
        try: prep_minutes=int(prep_minutes)
        except (ValueError,TypeError): raise OperationError("Preparation time must be a whole number of minutes.")
        if not name: raise OperationError("Menu item name is required.")
        if prep_minutes<0 or prep_minutes>1440: raise OperationError("Preparation time must be between 0 and 1,440 minutes.")
        product_id=self.create_product(sku,name,selling_price,0,0,"serving",0,category,description=notes,track_stock=False)
        timestamp=self._stamp()
        try:
            with self._transaction():
                cur=self.conn.execute("""INSERT INTO restaurant_menu_items(product_id,category,kitchen_station,prep_minutes,available,recipe_required,notes,created_at,updated_at)
                    VALUES(?,?,?,?,1,?,?,?,?)""",(product_id,category,kitchen_station,prep_minutes,int(bool(recipe_required)),notes,timestamp,timestamp))
                menu_id=int(cur.lastrowid)
                self._enqueue("restaurant_menu_items",menu_id,{"id":menu_id,"product_id":product_id,"sku":self.conn.execute("SELECT sku FROM products WHERE id=?",(product_id,)).fetchone()[0],"name":name,"category":category,"kitchen_station":kitchen_station,"prep_minutes":prep_minutes,"available":True,"recipe_required":bool(recipe_required)},timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError("The menu item could not be registered. Check for an existing menu entry.") from exc
        return menu_id

    def restaurant_menu_items(self, include_unavailable=True, limit=1000):
        self._require_module("Menu & Recipes")
        where="" if include_unavailable else "WHERE m.available=1 AND p.active=1"
        return self.conn.execute(f"""SELECT m.*,p.sku,p.name,p.selling_price,p.active,p.track_stock FROM restaurant_menu_items m
            JOIN products p ON p.id=m.product_id {where} ORDER BY m.category COLLATE NOCASE,p.name COLLATE NOCASE,m.id LIMIT ?""",(max(1,min(int(limit),10000)),)).fetchall()

    def restaurant_recipe_lines(self, menu_item_id):
        self._require_module("Menu & Recipes")
        return self.conn.execute("""SELECT r.*,p.sku,p.name,p.unit,p.track_batches,p.track_stock, p.cost_price
            FROM restaurant_recipe_lines r JOIN products p ON p.id=r.ingredient_product_id
            WHERE r.menu_item_id=? ORDER BY p.name COLLATE NOCASE,r.id""",(int(menu_item_id),)).fetchall()

    def set_restaurant_recipe(self, menu_item_id, lines):
        self._require_module("Menu & Recipes"); self._require("manage_inventory")
        if not isinstance(lines,(list,tuple)): raise OperationError("Recipe lines must be a list.")
        menu=self.conn.execute("SELECT m.*,p.name,p.track_stock FROM restaurant_menu_items m JOIN products p ON p.id=m.product_id WHERE m.id=?",(int(menu_item_id),)).fetchone()
        if not menu: raise OperationError("Choose a valid restaurant menu item.")
        normalized=[]; used=set()
        for item in lines:
            try: ingredient_id=int(item["ingredient_product_id"])
            except (KeyError,TypeError,ValueError): raise OperationError("Every recipe line must select an ingredient.")
            if ingredient_id in used: raise OperationError("An ingredient can appear only once in a recipe.")
            used.add(ingredient_id)
            ingredient=self.conn.execute("SELECT * FROM products WHERE id=? AND active=1",(ingredient_id,)).fetchone()
            if not ingredient: raise OperationError("A selected recipe ingredient is not active.")
            if not bool(ingredient["track_stock"]): raise OperationError(f"{ingredient['name']} is a non-stock item and cannot be a recipe ingredient.")
            if int(ingredient_id)==int(menu["product_id"]): raise OperationError("A menu item cannot be an ingredient in its own recipe.")
            quantity=self._decimal(item.get("quantity_per_serving"),f"Quantity for {ingredient['name']}",minimum=Decimal("0.001"),places=3)
            unit=str(item.get("unit") or ingredient["unit"]).strip()[:30]
            if unit.casefold()!=str(ingredient["unit"]).casefold():
                raise OperationError(f"Ingredient {ingredient['name']} uses {ingredient['unit']}; unit conversions are not yet configured, so use that exact stock unit in the recipe.")
            normalized.append((ingredient_id,self._quantity(quantity),unit,str(item.get("notes") or "").strip()[:200]))
        if menu["recipe_required"] and not normalized:
            raise OperationError("This menu item requires a recipe. Add at least one stock-tracked ingredient.")
        timestamp=self._stamp()
        with self._transaction():
            self.conn.execute("DELETE FROM restaurant_recipe_lines WHERE menu_item_id=?",(int(menu_item_id),))
            for ingredient_id,qty,unit,notes in normalized:
                self.conn.execute("INSERT INTO restaurant_recipe_lines(menu_item_id,ingredient_product_id,quantity_per_serving,unit,notes) VALUES(?,?,?,?,?)",(int(menu_item_id),ingredient_id,qty,unit,notes))
            self.conn.execute("UPDATE restaurant_menu_items SET updated_at=? WHERE id=?",(timestamp,int(menu_item_id)))
            self._enqueue("restaurant_menu_items",int(menu_item_id),{"id":int(menu_item_id),"recipe_ingredient_count":len(normalized)},timestamp)
        return len(normalized)

    def set_menu_item_available(self, menu_item_id, available):
        self._require_module("Menu & Recipes"); self._require("edit")
        timestamp=self._stamp()
        with self._transaction():
            row=self.conn.execute("SELECT id,product_id FROM restaurant_menu_items WHERE id=?",(int(menu_item_id),)).fetchone()
            if not row: raise OperationError("Menu item not found.")
            self.conn.execute("UPDATE restaurant_menu_items SET available=?,updated_at=? WHERE id=?",(int(bool(available)),timestamp,int(menu_item_id)))
            self._enqueue("restaurant_menu_items",int(menu_item_id),{"id":int(menu_item_id),"available":bool(available)},timestamp)
        return bool(available)

    def restaurant_kitchen_tickets(self, status="", limit=1000):
        self._require_module("Menu & Recipes")
        sql="SELECT t.*,s.sale_number FROM restaurant_kitchen_tickets t JOIN sales s ON s.id=t.sale_id WHERE 1=1"; params=[]
        if status: sql+=" AND t.status=?"; params.append(str(status))
        params.append(max(1,min(int(limit),10000)))
        return self.conn.execute(sql+" ORDER BY CASE t.status WHEN 'Queued' THEN 0 WHEN 'Preparing' THEN 1 WHEN 'Ready' THEN 2 ELSE 3 END,t.created_at,t.id LIMIT ?",params).fetchall()

    def update_restaurant_kitchen_ticket(self, ticket_id, status, note=""):
        self._require_module("Menu & Recipes"); self._require("edit")
        allowed={"Queued":{"Preparing","Cancelled"},"Preparing":{"Ready","Cancelled"},"Ready":{"Served"},"Served":set(),"Cancelled":set()}
        timestamp=self._stamp(); status=str(status or "").strip(); note=str(note or "").strip()[:300]
        with self._transaction():
            ticket=self.conn.execute("SELECT * FROM restaurant_kitchen_tickets WHERE id=?",(int(ticket_id),)).fetchone()
            if not ticket: raise OperationError("Kitchen ticket not found.")
            if status not in allowed.get(ticket["status"],set()): raise OperationError(f"Cannot move kitchen ticket from {ticket['status']} to {status}.")
            self.conn.execute("UPDATE restaurant_kitchen_tickets SET status=?,notes=CASE WHEN ?='' THEN notes ELSE notes||char(10)||? END,updated_at=? WHERE id=?",(status,note,note,timestamp,int(ticket_id)))
            self.conn.execute("INSERT INTO restaurant_kitchen_events(ticket_id,from_status,to_status,note,changed_by,created_at) VALUES(?,?,?,?,?,?)",(int(ticket_id),ticket["status"],status,note,self.actor_username,timestamp))
            self._enqueue("restaurant_kitchen_tickets",int(ticket_id),{"id":int(ticket_id),"sale_id":ticket["sale_id"],"status":status,"note":note},timestamp)
        return status

    def _recipe_sale_plan(self, normalized):
        """Return menu recipes and aggregate ingredient quantities for a transaction."""
        menu_by_product={}; ingredient_totals={}; lines_by_product={}
        for product,servings,price,amount in normalized:
            menu=self.conn.execute("SELECT * FROM restaurant_menu_items WHERE product_id=?",(int(product["id"]),)).fetchone()
            if not menu: continue
            if not bool(menu["available"]): raise OperationError(f"Menu item {product['name']} is currently unavailable.")
            recipe=self.restaurant_recipe_lines(menu["id"])
            if bool(menu["recipe_required"]) and not recipe: raise OperationError(f"Menu item {product['name']} has no recipe. Configure its ingredients before selling it.")
            if bool(product["track_stock"]): raise OperationError(f"Restaurant menu item {product['name']} must be configured as a non-stock sale item; ingredients are stock-tracked separately.")
            menu_by_product[int(product["id"])]=dict(menu)
            lines_by_product[int(product["id"])]=[dict(row) for row in recipe]
            for row in recipe:
                ingredient_id=int(row["ingredient_product_id"])
                needed=Decimal(str(row["quantity_per_serving"]))*servings
                ingredient_totals[ingredient_id]=ingredient_totals.get(ingredient_id,Decimal("0.000"))+needed
        return menu_by_product,lines_by_product,ingredient_totals

    def _record_recipe_consumption(self, sale_line_id, menu_item, recipe_lines, servings, sale_id, timestamp):
        for recipe in recipe_lines:
            ingredient_id=int(recipe["ingredient_product_id"])
            qty=(Decimal(str(recipe["quantity_per_serving"]))*servings).quantize(Decimal("0.001"),rounding=ROUND_HALF_UP)
            if qty<=0: continue
            consumption_id=self.conn.execute("INSERT INTO sale_recipe_consumptions(sale_line_id,menu_item_id,ingredient_product_id,quantity,created_at) VALUES(?,?,?,?,?)",(int(sale_line_id),int(menu_item["id"]),ingredient_id,self._quantity(qty),timestamp)).lastrowid
            ingredient=self.conn.execute("SELECT * FROM products WHERE id=?",(ingredient_id,)).fetchone()
            if bool(ingredient["track_batches"]): self._consume_batches(ingredient_id,qty,timestamp,recipe_consumption_id=consumption_id)
            movement_id=self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (ingredient_id,self._quantity(-qty),"restaurant_recipe_consumption","sale_recipe",int(sale_id),f"Recipe for {menu_item['id']} · sale line {sale_line_id}",timestamp)).lastrowid
            self.conn.execute("UPDATE sale_recipe_consumptions SET stock_movement_id=? WHERE id=?",(int(movement_id),int(consumption_id)))
            self._enqueue("stock_movements",movement_id,{"id":int(movement_id),"product_id":ingredient_id,"quantity_delta":self._quantity(-qty),"reference_type":"sale_recipe","reference_id":int(sale_id)},timestamp)

    def _restore_recipe_batch_allocations(self, consumption_id, fraction, sale_id, timestamp):
        allocations=self.conn.execute("SELECT a.*,b.expiry_date,b.status,b.remaining_quantity FROM recipe_sale_batch_allocations a JOIN inventory_batches b ON b.id=a.batch_id WHERE a.consumption_id=? ORDER BY a.id",(int(consumption_id),)).fetchall()
        for allocation in allocations:
            restore=(Decimal(allocation["quantity"])*fraction).quantize(Decimal("0.001"),rounding=ROUND_HALF_UP)
            if restore<=0: continue
            batch_id=int(allocation["batch_id"])
            new_status="In stock" if not allocation["expiry_date"] or allocation["expiry_date"]>=timestamp[:10] else "Quarantined"
            self.conn.execute("UPDATE inventory_batches SET remaining_quantity=?,status=?,updated_at=? WHERE id=?",(self._quantity(Decimal(allocation["remaining_quantity"])+restore),new_status,timestamp,batch_id))
            self.conn.execute("INSERT INTO recipe_sale_batch_reversals(consumption_id,batch_id,sale_id,quantity,created_at) VALUES(?,?,?,?,?)",(int(consumption_id),batch_id,int(sale_id),self._quantity(restore),timestamp))

    def _restore_recipe_consumptions(self, sale_line_id, restore_fraction, sale_id, timestamp):
        if restore_fraction<=0: return
        rows=self.conn.execute("SELECT * FROM sale_recipe_consumptions WHERE sale_line_id=? ORDER BY id",(int(sale_line_id),)).fetchall()
        for consumption in rows:
            qty=(Decimal(consumption["quantity"])*restore_fraction).quantize(Decimal("0.001"),rounding=ROUND_HALF_UP)
            if qty<=0: continue
            self._restore_recipe_batch_allocations(consumption["id"],restore_fraction,int(sale_id),timestamp)
            ingredient_id=int(consumption["ingredient_product_id"])
            movement_id=self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (ingredient_id,self._quantity(qty),"restaurant_recipe_reversal","sale_recipe_reversal",int(sale_id),f"Cancelled sale line {sale_line_id}",timestamp)).lastrowid
            self.conn.execute("INSERT INTO sale_recipe_reversals(consumption_id,sale_id,quantity,created_at) VALUES(?,?,?,?)",(int(consumption["id"]),int(sale_id),self._quantity(qty),timestamp))
            self._enqueue("stock_movements",movement_id,{"id":movement_id,"product_id":ingredient_id,"quantity_delta":self._quantity(qty),"reference_type":"sale_recipe_reversal","reference_id":int(sale_id)},timestamp)

    def create_product(self, sku, name, selling_price, cost_price=0, opening_stock=0, unit="each", reorder_level=0, category="", barcode="", description="", brand="", model="", supplier_code="", variant_group="", variant_attributes=None, base_unit="", units_per_stock_unit=1, track_batches=False, opening_batch_number="", opening_expiry_date="", track_stock=True):
        self._require_module("Inventory")
        self._require("manage_inventory")
        sku = str(sku or "").strip()
        name = str(name or "").strip()
        if len(sku) > 80 or not name or len(name) > 160:
            raise OperationError("Product name is required (up to 160 characters); an SKU may be left blank for automatic generation (maximum 80 characters).")
        if any(ord(ch) < 32 for ch in sku):
            raise OperationError("SKU cannot contain control characters.")
        selling = self._decimal(selling_price, "Selling price", minimum=Decimal("0"))
        cost = self._decimal(cost_price, "Cost price", minimum=Decimal("0"))
        opening = self._decimal(opening_stock, "Opening stock", minimum=Decimal("0"), places=3)
        reorder = self._decimal(reorder_level, "Reorder level", minimum=Decimal("0"), places=3)
        unit = str(unit).strip()[:30] or "each"
        base_unit = str(base_unit or unit).strip()[:30] or unit
        unit_size = self._decimal(units_per_stock_unit, "Contents per stock unit", minimum=Decimal("0"), places=3)
        if unit_size <= 0:
            raise OperationError("Contents per stock unit must be greater than zero.")
        category = str(category).strip()[:80]
        barcode = str(barcode).strip()[:128]
        description, brand, model, supplier_code = (str(description or "").strip()[:1000], str(brand or "").strip()[:120],
                                                       str(model or "").strip()[:120], str(supplier_code or "").strip()[:120])
        variant_group = str(variant_group or "").strip()[:160]
        variant_attributes_json = self._variant_attributes_json(variant_attributes)
        if variant_attributes_json != "{}" and not variant_group:
            raise OperationError("Enter a variant group when defining variant attributes.")
        if any(any(ord(ch) < 32 for ch in value) for value in (barcode,)):
            raise OperationError("Barcode cannot contain control characters.")
        track_batches=bool(track_batches)
        track_stock=bool(track_stock)
        if not track_stock and opening>0:
            raise OperationError("A non-stock service or menu item cannot have opening inventory. Track ingredient inventory separately.")
        if not track_stock and track_batches:
            raise OperationError("Batch tracking requires a stock-tracked product.")
        opening_batch_number=str(opening_batch_number or "").strip()[:100]
        opening_expiry_date=self._iso_date(opening_expiry_date,"Batch expiry date",allow_blank=True)
        if track_batches and opening>0 and not opening_batch_number:
            raise OperationError("Enter an opening lot / batch number when batch tracking a product with opening stock.")
        timestamp = self._stamp()
        try:
            with self._transaction():
                if not sku:
                    sku = self._allocate_sku(category, brand=brand)
                duplicate_sku = self.conn.execute("SELECT id FROM products WHERE sku=? COLLATE NOCASE LIMIT 1", (sku,)).fetchone()
                if duplicate_sku:
                    raise OperationError(f"SKU {sku!r} is already assigned to another product.")
                if barcode:
                    duplicate = self.conn.execute("SELECT product_id FROM product_barcodes WHERE barcode=? COLLATE NOCASE", (barcode,)).fetchone()
                    if duplicate:
                        raise OperationError(f"barcode {barcode!r} is already assigned to a product.")
                if variant_group and variant_attributes_json != "{}":
                    duplicate_variant = self.conn.execute("SELECT id FROM products WHERE variant_group=? COLLATE NOCASE AND variant_attributes_json=? LIMIT 1",
                                                          (variant_group, variant_attributes_json)).fetchone()
                    if duplicate_variant:
                        raise OperationError("A product with this variant group and attribute combination already exists.")
                cur = self.conn.execute(
                    """INSERT INTO products(sku,name,unit,category,barcode,description,brand,model,supplier_code,variant_group,variant_attributes_json,
                       selling_price,cost_price,reorder_level,created_at,base_unit,units_per_stock_unit,track_batches,track_stock)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sku, name, unit, category, barcode, description, brand, model, supplier_code, variant_group, variant_attributes_json,
                     self._money(selling), self._money(cost), self._quantity(reorder), timestamp, base_unit, self._quantity(unit_size), int(track_batches),int(track_stock)))
                product_id = cur.lastrowid
                if barcode:
                    self.conn.execute("INSERT INTO product_barcodes(product_id,barcode,label,created_at) VALUES(?,?,?,?)",
                                      (product_id, barcode, "Primary barcode", timestamp))
                if opening:
                    self.conn.execute(
                        "INSERT INTO stock_movements(product_id,quantity_delta,movement_type,note,created_at) VALUES(?,?,?,?,?)",
                        (product_id, self._quantity(opening), "opening", "Opening balance", timestamp))
                    if track_batches:
                        self._insert_inventory_batch(product_id,opening_batch_number,opening_expiry_date,opening,"","opening",product_id,"Opening stock batch",timestamp)
                self._enqueue("products", product_id, {"id": product_id, "sku": sku, "name": name, "unit": unit,
                    "category": category, "barcode": barcode, "description": description, "brand": brand, "model": model,
                    "supplier_code": supplier_code, "variant_group": variant_group, "variant_attributes_json": variant_attributes_json,
                    "base_unit": base_unit, "units_per_stock_unit": self._quantity(unit_size),
                    "selling_price": self._money(selling), "cost_price": self._money(cost),
                    "reorder_level": self._quantity(reorder), "opening_stock": self._quantity(opening), "track_stock":track_stock}, timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"SKU {sku!r} or barcode {barcode!r} is already assigned, or the product could not be saved.") from exc
        return product_id

    def customers(self, limit=10000):
        self._require_any_module(("CRM","Sales"))
        return _as_dicts(self.conn.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE,id LIMIT ?",(limit,)).fetchall())

    def suppliers(self, limit=10000):
        self._require_module("Purchases")
        return _as_dicts(self.conn.execute("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE,id LIMIT ?",(limit,)).fetchall())

    def customer_outstanding(self, customer_id=None, customer_name=None):
        """Return total amount the customer still owes (sum of unpaid sale balances).

        Positive = customer owes the shop (accounts receivable / debit from shop view).
        """
        self._require_any_module(("CRM", "Sales", "Finance"))
        clauses, params = ["status='Completed'"], []
        if customer_id is not None:
            clauses.append("customer_id=?")
            params.append(int(customer_id))
        elif customer_name:
            clauses.append("customer_name=? COLLATE NOCASE")
            params.append(str(customer_name).strip())
        else:
            return Decimal("0.00")
        rows = self.conn.execute(
            f"SELECT total, paid_total FROM sales WHERE {' AND '.join(clauses)}", params
        ).fetchall()
        outstanding = Decimal("0.00")
        for r in rows:
            bal = Decimal(str(r["total"] or "0")) - Decimal(str(r["paid_total"] or "0"))
            if bal > 0:
                outstanding += bal
        return outstanding.quantize(Decimal("0.01"))

    def supplier_outstanding(self, supplier_id=None, supplier_name=None):
        """Return total amount the shop still owes the supplier (unpaid purchase balances).

        Positive = shop owes the supplier (accounts payable).
        """
        self._require_module("Purchases")
        clauses, params = ["status IN ('Received','Partial')"], []
        if supplier_id is not None:
            clauses.append("supplier_id=?")
            params.append(int(supplier_id))
        elif supplier_name:
            clauses.append("supplier_name=? COLLATE NOCASE")
            params.append(str(supplier_name).strip())
        else:
            return Decimal("0.00")
        # purchases table uses the same paid_total pattern
        try:
            rows = self.conn.execute(
                f"SELECT total, paid_total FROM purchases WHERE {' AND '.join(clauses)}", params
            ).fetchall()
        except sqlite3.OperationalError:
            return Decimal("0.00")
        outstanding = Decimal("0.00")
        for r in rows:
            bal = Decimal(str(r["total"] or "0")) - Decimal(str(r["paid_total"] or "0"))
            if bal > 0:
                outstanding += bal
        return outstanding.quantize(Decimal("0.01"))

    def customer_sales_with_balance(self, customer_id=None, customer_name=None, limit=200):
        """List sales for a customer with remaining balance, newest first."""
        self._require_any_module(("CRM", "Sales", "Finance"))
        clauses, params = ["status='Completed'"], []
        if customer_id is not None:
            clauses.append("customer_id=?")
            params.append(int(customer_id))
        elif customer_name:
            clauses.append("customer_name=? COLLATE NOCASE")
            params.append(str(customer_name).strip())
        else:
            return []
        params.append(int(limit))
        rows = self.conn.execute(
            f"""SELECT id, sale_number, customer_name, total, paid_total, payment_status,
                       payment_method, created_at, warranty_period, served_by_name, served_by_code
                FROM sales WHERE {' AND '.join(clauses)}
                ORDER BY id DESC LIMIT ?""", params
        ).fetchall()
        result = []
        for r in rows:
            total = Decimal(str(r["total"] or "0"))
            paid = Decimal(str(r["paid_total"] or "0"))
            bal = (total - paid).quantize(Decimal("0.01"))
            d = dict(r)
            d["balance"] = self._money(bal)
            d["is_credit"] = bal > 0
            result.append(d)
        return result

    def record_customer_payment(self, amount, customer_id=None, customer_name=None,
                                payment_method="Cash", payment_reference="", note=""):
        """Record a payment from a customer and allocate it FIFO to oldest unpaid sales.

        Returns (allocated_total, remaining_unallocated, sale_allocations).
        """
        self._require_any_module(("CRM", "Sales", "Finance"))
        self._require("manage_finance")
        value = self._decimal(amount, "Payment amount", minimum=Decimal("0.01"))
        payment_method = self._payment_method(payment_method)
        payment_reference = str(payment_reference or "").strip()[:120]
        note = str(note or "").strip()[:200]
        timestamp = self._stamp()
        with self._transaction():
            if customer_id is not None:
                cust = self.conn.execute("SELECT id, name FROM customers WHERE id=?", (int(customer_id),)).fetchone()
                if not cust:
                    raise OperationError("Customer not found.")
                customer_id = cust["id"]
                customer_name = cust["name"]
            else:
                customer_name = str(customer_name or "").strip()[:160]
                if not customer_name:
                    raise OperationError("Enter the customer name for this payment.")
            # Oldest unpaid first
            open_sales = self.conn.execute(
                """SELECT id, sale_number, total, paid_total FROM sales
                   WHERE status='Completed' AND customer_name=? COLLATE NOCASE
                   ORDER BY id ASC""",
                (customer_name,)
            ).fetchall()
            remaining = value
            allocations = []
            for sale in open_sales:
                if remaining <= 0:
                    break
                total = Decimal(str(sale["total"] or "0"))
                paid = Decimal(str(sale["paid_total"] or "0"))
                due = total - paid
                if due <= 0:
                    continue
                apply = min(remaining, due)
                new_paid = paid + apply
                self.conn.execute(
                    "UPDATE sales SET paid_total=?, payment_status=? WHERE id=?",
                    (self._money(new_paid), self._payment_status(total, new_paid), int(sale["id"]))
                )
                allocations.append({
                    "sale_id": int(sale["id"]),
                    "sale_number": sale["sale_number"],
                    "amount": self._money(apply),
                })
                remaining -= apply
            allocated = value - remaining
            desc = f"Customer payment · {customer_name}"
            if note:
                desc += f" · {note}"
            if allocations:
                desc += " · applied to " + ", ".join(a["sale_number"] for a in allocations)
            cash_id = self.conn.execute(
                "INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES('in',?,?,?,?,?,?,?,?)",
                (self._money(value), "customer_payment", customer_id or 0, desc, timestamp,
                 payment_method, payment_reference, "Customer receipt")
            ).lastrowid
            self._enqueue("cash_entries", cash_id, {
                "id": cash_id, "direction": "in", "amount": self._money(value),
                "source_type": "customer_payment", "customer_name": customer_name,
                "allocated": self._money(allocated), "unallocated": self._money(remaining),
                "payment_method": payment_method, "reference": payment_reference,
            }, timestamp)
        return self._money(allocated), self._money(remaining), allocations

    def update_product(self, product_id, sku, name, selling_price, cost_price, reorder_level=0, unit="each", active=True, category="", barcode="", description="", brand="", model="", supplier_code="", variant_group="", variant_attributes=None, sku_change_reason="Updated product identifier", base_unit="", units_per_stock_unit=1, track_batches=None, opening_batch_number="", opening_expiry_date="", track_stock=None):
        self._require_module("Inventory")
        self._require("manage_inventory")
        sku = str(sku or "").strip()
        name = str(name or "").strip()
        if not sku or len(sku) > 80 or not name or len(name) > 160:
            raise OperationError("An SKU (up to 80 characters) and product name (up to 160 characters) are required when editing a product.")
        if any(ord(ch) < 32 for ch in sku):
            raise OperationError("SKU cannot contain control characters.")
        selling = self._decimal(selling_price, "Selling price", minimum=Decimal("0"))
        cost = self._decimal(cost_price, "Cost price", minimum=Decimal("0"))
        reorder = self._decimal(reorder_level, "Reorder level", minimum=Decimal("0"), places=3)
        unit = str(unit).strip()[:30] or "each"
        base_unit = str(base_unit or unit).strip()[:30] or unit
        unit_size = self._decimal(units_per_stock_unit, "Contents per stock unit", minimum=Decimal("0"), places=3)
        if unit_size <= 0:
            raise OperationError("Contents per stock unit must be greater than zero.")
        category = str(category).strip()[:80]
        barcode = str(barcode).strip()[:128]
        description, brand, model, supplier_code = (str(description or "").strip()[:1000], str(brand or "").strip()[:120],
                                                       str(model or "").strip()[:120], str(supplier_code or "").strip()[:120])
        variant_group = str(variant_group or "").strip()[:160]
        variant_attributes_json = self._variant_attributes_json(variant_attributes)
        if variant_attributes_json != "{}" and not variant_group:
            raise OperationError("Enter a variant group when defining variant attributes.")
        if any(ord(ch) < 32 for ch in barcode):
            raise OperationError("Barcode cannot contain control characters.")
        opening_batch_number=str(opening_batch_number or "").strip()[:100]
        opening_expiry_date=self._iso_date(opening_expiry_date,"Batch expiry date",allow_blank=True)
        timestamp = self._stamp()
        try:
            with self._transaction():
                old = self.conn.execute("SELECT sku,barcode,track_batches,track_stock FROM products WHERE id=?", (product_id,)).fetchone()
                if not old:
                    raise OperationError("The selected product no longer exists.")
                duplicate_sku = self.conn.execute("SELECT id FROM products WHERE sku=? COLLATE NOCASE AND id<>? LIMIT 1", (sku, product_id)).fetchone()
                if duplicate_sku:
                    raise OperationError(f"SKU {sku!r} is already assigned to another product.")
                if barcode:
                    duplicate = self.conn.execute("SELECT product_id FROM product_barcodes WHERE barcode=? COLLATE NOCASE", (barcode,)).fetchone()
                    if duplicate and int(duplicate[0]) != int(product_id):
                        raise OperationError(f"Barcode {barcode!r} is already assigned to another product.")
                    duplicate = self.conn.execute("SELECT id FROM products WHERE barcode=? COLLATE NOCASE AND id<>?", (barcode, product_id)).fetchone()
                    if duplicate:
                        raise OperationError(f"Barcode {barcode!r} is already assigned to another product.")
                if variant_group and variant_attributes_json != "{}":
                    duplicate_variant = self.conn.execute("SELECT id FROM products WHERE variant_group=? COLLATE NOCASE AND variant_attributes_json=? AND id<>? LIMIT 1",
                                                          (variant_group, variant_attributes_json, product_id)).fetchone()
                    if duplicate_variant:
                        raise OperationError("A product with this variant group and attribute combination already exists.")
                target_track_batches=bool(old["track_batches"]) if track_batches is None else bool(track_batches)
                target_track_stock=bool(old["track_stock"]) if track_stock is None else bool(track_stock)
                current_stock=self.stock_on_hand(product_id)
                if not target_track_stock and target_track_batches:
                    raise OperationError("Batch tracking requires stock tracking; disable batch tracking only after all lot stock has been reconciled.")
                if not target_track_stock and (current_stock>0 or target_track_batches):
                    raise OperationError("Stock tracking cannot be disabled while on-hand or batch-tracked stock exists. Clear/reconcile inventory first.")
                batch_total=self._batch_balance(product_id)
                if target_track_batches:
                    if batch_total>current_stock:
                        raise OperationError("Tracked batch quantities exceed stock-ledger quantity. Reconcile batches before editing this product.")
                    missing=current_stock-batch_total
                    if missing>Decimal("0.000"):
                        if not opening_batch_number:
                            raise OperationError(f"Batch tracking needs a lot number for the existing unallocated stock ({self._quantity(missing)} {unit}). Enter an opening batch number or leave batch tracking off.")
                        self._insert_inventory_batch(product_id,opening_batch_number,opening_expiry_date,missing,"","opening-allocation",product_id,"Allocated existing on-hand stock to batch tracking",timestamp)
                elif batch_total>Decimal("0.000"):
                    raise OperationError("Batch tracking cannot be disabled while tracked stock remains. Dispose or reconcile all batches first.")
                if str(old[0]).casefold() != sku.casefold():
                    self.conn.execute("INSERT INTO product_sku_history(product_id,old_sku,new_sku,changed_by,reason,changed_at) VALUES(?,?,?,?,?,?)",
                                      (product_id, old[0], sku, self.actor_username, str(sku_change_reason or "Updated SKU")[:240], timestamp))
                cur = self.conn.execute("""UPDATE products SET sku=?,name=?,unit=?,category=?,barcode=?,description=?,brand=?,model=?,supplier_code=?,variant_group=?,variant_attributes_json=?,
                    selling_price=?,cost_price=?,reorder_level=?,active=?,base_unit=?,units_per_stock_unit=?,track_batches=?,track_stock=? WHERE id=?""",
                    (sku, name, unit, category, barcode, description, brand, model, supplier_code, variant_group, variant_attributes_json,
                     self._money(selling), self._money(cost), self._quantity(reorder), int(bool(active)), base_unit, self._quantity(unit_size), int(target_track_batches), int(target_track_stock), product_id))
                if not cur.rowcount:
                    raise OperationError("The selected product no longer exists.")
                if barcode:
                    alias = self.conn.execute("SELECT product_id FROM product_barcodes WHERE barcode=? COLLATE NOCASE", (barcode,)).fetchone()
                    if not alias:
                        self.conn.execute("INSERT INTO product_barcodes(product_id,barcode,label,created_at) VALUES(?,?,?,?)",
                                          (product_id, barcode, "Primary barcode", timestamp))
                self._enqueue("products", product_id, {"id": product_id, "sku": sku, "name": name, "unit": unit,
                    "category": category, "barcode": barcode, "description": description, "brand": brand, "model": model,
                    "supplier_code": supplier_code, "variant_group": variant_group, "variant_attributes_json": variant_attributes_json,
                    "base_unit": base_unit, "units_per_stock_unit": self._quantity(unit_size),
                    "selling_price": self._money(selling), "cost_price": self._money(cost),
                    "reorder_level": self._quantity(reorder), "active": bool(active), "track_batches": target_track_batches, "track_stock":target_track_stock}, timestamp)
        except sqlite3.IntegrityError as exc:
            raise OperationError(f"SKU {sku!r} or barcode {barcode!r} is already assigned, or the product could not be saved.") from exc
        return product_id

    def adjust_stock(self, product_id, quantity_delta, reason):
        self._require_module("Inventory")
        self._require("manage_inventory")
        delta=self._decimal(quantity_delta,"Stock adjustment",places=3)
        reason=str(reason).strip()[:240]
        if not delta or not reason:
            raise OperationError("Enter a non-zero stock adjustment and a reason.")
        timestamp=self._stamp()
        with self._transaction():
            product=self.conn.execute("SELECT sku,name,unit,track_batches FROM products WHERE id=? AND active=1",(product_id,)).fetchone()
            if not product:
                raise OperationError("The selected product is unavailable.")
            current=self.stock_on_hand(product_id)
            new_balance=current+delta
            if new_balance < 0:
                raise OperationError(f"Stock cannot fall below zero. Current quantity: {self._quantity(current)}.")
            if bool(product["track_batches"]):
                if self._batch_balance(product_id)!=current.quantize(Decimal("0.001")):
                    raise OperationError("Batch balances do not match stock ledger. Reconcile batches before adjusting this product.")
                if delta>0:
                    raise OperationError("For batch-tracked products, receive new stock through the Batches & Expiry receipt workflow so the lot and supplier are recorded.")
                self._consume_batches(product_id,-delta,timestamp,allow_expired=True)
            cur=self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,note,created_at) VALUES(?,?,?,?,?)",
                                  (product_id,self._quantity(delta),"adjustment",reason,timestamp))
            self._enqueue("stock_movements",cur.lastrowid,{"id":cur.lastrowid,"product_id":product_id,
                "sku":product["sku"],"quantity_delta":self._quantity(delta),"reason":reason},timestamp)
        return cur.lastrowid

    def products(self, active_only=True, limit=None, offset=0, search=None):
        self._require_any_module(("Inventory","Sales","Purchases","Reports","Repair Tickets"))
        # Include stock_on_hand in one query so callers never N+1 sum stock_movements per product.
        stock_sql = "COALESCE((SELECT SUM(CAST(m.quantity_delta AS REAL)) FROM stock_movements m WHERE m.product_id=p.id),0) AS stock_on_hand"
        query = f"SELECT p.*, {stock_sql} FROM products p WHERE 1=1"
        params = []
        if active_only:
            query += " AND p.active=1"
        term = (search or "").strip()
        if term:
            like = f"%{term}%"
            query += (
                " AND (p.sku LIKE ? COLLATE NOCASE OR p.name LIKE ? COLLATE NOCASE"
                " OR p.brand LIKE ? COLLATE NOCASE OR p.category LIKE ? COLLATE NOCASE"
                " OR p.model LIKE ? COLLATE NOCASE OR p.barcode LIKE ? COLLATE NOCASE)"
            )
            params.extend([like, like, like, like, like, like])
        query += " ORDER BY p.brand COLLATE NOCASE, p.sku COLLATE NOCASE, p.name COLLATE NOCASE, p.id"
        if limit is not None:
            try:
                lim = max(1, min(int(limit), 5000))
                off = max(0, int(offset or 0))
            except (TypeError, ValueError):
                lim, off = 100, 0
            query += " LIMIT ? OFFSET ?"
            params.extend([lim, off])
        return _as_dicts(self.conn.execute(query, params).fetchall())

    def product_count(self, active_only=True, search=None):
        """Cheap catalogue size for dashboards and inventory paging."""
        self._require_any_module(("Inventory", "Sales", "Purchases", "Reports", "Repair Tickets"))
        query = "SELECT COUNT(*) FROM products p WHERE 1=1"
        params = []
        if active_only:
            query += " AND p.active=1"
        term = (search or "").strip()
        if term:
            like = f"%{term}%"
            query += (
                " AND (p.sku LIKE ? COLLATE NOCASE OR p.name LIKE ? COLLATE NOCASE"
                " OR p.brand LIKE ? COLLATE NOCASE OR p.category LIKE ? COLLATE NOCASE"
                " OR p.model LIKE ? COLLATE NOCASE OR p.barcode LIKE ? COLLATE NOCASE)"
            )
            params.extend([like, like, like, like, like, like])
        return int(self.conn.execute(query, params).fetchone()[0])

    def search_products(self, query: str, limit: int = 40):
        """Shared product lookup. Exact barcode/alias and SKU hits rank ahead of fuzzy matches."""
        self._require_any_module(("Inventory", "Sales", "Purchases", "Point of Sale", "Repair Tickets"))
        term = (query or "").strip()
        if not term:
            return []
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 40
        stock_sql = "COALESCE((SELECT SUM(CAST(m.quantity_delta AS REAL)) FROM stock_movements m WHERE m.product_id=p.id),0) AS stock_on_hand"
        exact = self.conn.execute(f"""
            SELECT p.*, {stock_sql}
            FROM products p
            WHERE p.active=1 AND (
                p.sku=? COLLATE NOCASE OR p.barcode=? COLLATE NOCASE OR
                EXISTS(SELECT 1 FROM product_barcodes b WHERE b.product_id=p.id AND b.barcode=? COLLATE NOCASE)
            )
            ORDER BY CASE WHEN p.barcode=? COLLATE NOCASE OR EXISTS(
                SELECT 1 FROM product_barcodes b WHERE b.product_id=p.id AND b.barcode=? COLLATE NOCASE
            ) THEN 0 ELSE 1 END, p.id
            LIMIT ?
        """, (term, term, term, term, term, limit)).fetchall()
        if exact:
            return _as_dicts(exact)
        like = f"%{term}%"
        prefix = f"{term}%"
        return _as_dicts(self.conn.execute(f"""
            SELECT p.*, {stock_sql}
            FROM products p
            WHERE p.active=1 AND (
                p.sku LIKE ? COLLATE NOCASE OR p.name LIKE ? COLLATE NOCASE OR
                p.category LIKE ? COLLATE NOCASE OR p.brand LIKE ? COLLATE NOCASE OR
                p.model LIKE ? COLLATE NOCASE OR p.supplier_code LIKE ? COLLATE NOCASE OR
                p.variant_group LIKE ? COLLATE NOCASE OR p.variant_attributes_json LIKE ? COLLATE NOCASE OR
                p.barcode LIKE ? COLLATE NOCASE OR EXISTS(
                    SELECT 1 FROM product_barcodes b WHERE b.product_id=p.id AND b.barcode LIKE ? COLLATE NOCASE
                )
            )
            ORDER BY CASE WHEN p.sku LIKE ? COLLATE NOCASE THEN 0
                          WHEN p.supplier_code LIKE ? COLLATE NOCASE THEN 1
                          WHEN p.barcode LIKE ? COLLATE NOCASE THEN 2 ELSE 3 END,
                     p.name COLLATE NOCASE, p.id
            LIMIT ?
        """, (prefix, like, like, like, like, like, like, like, like, like, prefix, prefix, prefix, limit)).fetchall())

    def generate_missing_skus(self, dry_run=True):
        """Preview or repair legacy products whose SKU is blank; never changes product IDs or stock."""
        self._require_module("Inventory")
        self._require("manage_inventory")
        rows = self.conn.execute("SELECT id,name,category,sku FROM products WHERE sku IS NULL OR TRIM(sku)='' ORDER BY id").fetchall()
        if dry_run:
            settings = self.sku_numbering_settings()
            proposed, reservations = [], set()
            counters = {}
            for row in rows:
                prefix = self._sku_prefix_for_category(row["category"], settings)
                if prefix not in counters:
                    counter_row = self.conn.execute("SELECT next_value FROM sku_sequences WHERE prefix=? COLLATE NOCASE", (prefix,)).fetchone()
                    counters[prefix] = int(counter_row[0]) if counter_row else 1
                number = counters[prefix]
                while True:
                    candidate = self._sku_candidate(prefix, number, settings["padding"])
                    exists = self.conn.execute("SELECT 1 FROM products WHERE sku=? COLLATE NOCASE AND id<>? LIMIT 1", (candidate, row["id"])).fetchone()
                    if not exists and candidate.casefold() not in reservations:
                        break
                    number += 1
                counters[prefix] = number + 1
                reservations.add(candidate.casefold())
                proposed.append({"id": row["id"], "name": row["name"], "old_sku": row["sku"], "suggested_sku": candidate})
            return {"dry_run": True, "count": len(proposed), "rows": proposed}
        updated = []
        with self._transaction():
            rows = self.conn.execute("SELECT id,name,category,sku FROM products WHERE sku IS NULL OR TRIM(sku)='' ORDER BY id").fetchall()
            for row in rows:
                old_sku = str(row["sku"] or "")
                sku = self._allocate_sku(row["category"], brand=row["brand"] if "brand" in row.keys() else "")
                timestamp = self._stamp()
                self.conn.execute("UPDATE products SET sku=? WHERE id=?", (sku, row["id"]))
                self.conn.execute("INSERT INTO product_sku_history(product_id,old_sku,new_sku,changed_by,reason,changed_at) VALUES(?,?,?,?,?,?)",
                                  (row["id"], old_sku, sku, self.actor_username, "Bulk missing-SKU repair", timestamp))
                product = self.conn.execute("SELECT sku,name,unit,category,barcode,description,brand,model,supplier_code,selling_price,cost_price,reorder_level,active FROM products WHERE id=?", (row["id"],)).fetchone()
                self._enqueue("products", row["id"], {"id": row["id"], **dict(product)}, timestamp)
                updated.append({"id": row["id"], "name": row["name"], "old_sku": old_sku, "suggested_sku": sku})
        return {"dry_run": False, "count": len(updated), "rows": updated}

    def export_products_template_csv(self, file_path):
        self._require_module("Inventory")
        self._require("export")
        fields = ["sku", "name", "description", "category", "brand", "model", "barcode", "supplier_code", "unit",
                  "base_unit", "units_per_stock_unit", "variant_group", "variant_attributes", "selling_price", "cost_price", "reorder_level", "active"]
        try:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
        except OSError as exc:
            raise OperationError(f"Could not create the product import template: {exc}") from exc
        return fields

    def export_products_csv(self, file_path):
        """Export catalogue data; current stock is informational and is not imported as a product edit."""
        self._require_module("Inventory")
        self._require("export")
        rows = self.conn.execute("""SELECT p.*, COALESCE((SELECT SUM(CAST(m.quantity_delta AS REAL))
            FROM stock_movements m WHERE m.product_id=p.id),0) AS stock_on_hand
            FROM products p ORDER BY p.sku COLLATE NOCASE,p.id""").fetchall()
        fields = ["sku", "name", "description", "category", "brand", "model", "barcode", "supplier_code", "unit",
                  "base_unit", "units_per_stock_unit", "variant_group", "variant_attributes", "selling_price", "cost_price", "reorder_level", "active", "stock_on_hand"]
        try:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    payload = {field: row[field] if field in row.keys() else "" for field in fields}
                    payload["variant_attributes"] = self._variant_attributes_text(row["variant_attributes_json"])
                    writer.writerow(payload)
        except OSError as exc:
            raise OperationError(f"Could not export the product catalogue: {exc}") from exc
        return len(rows)

    def import_products_csv(self, file_path, dry_run=True, update_existing=False):
        """Validate or import a catalogue CSV in recoverable row batches; stock quantities are never changed."""
        self._require_module("Inventory")
        self._require("manage_inventory")
        try:
            with open(file_path, "r", newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or not {"name", "selling_price", "cost_price"}.issubset({x.strip() for x in reader.fieldnames if x}):
                    raise OperationError("CSV must include at least name, selling_price, and cost_price columns. Use the exported catalogue as a template.")
                source_rows = [(reader.line_num, {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}) for row in reader]
        except OSError as exc:
            raise OperationError(f"Could not read the catalogue file: {exc}") from exc
        except UnicodeError as exc:
            raise OperationError("The CSV encoding could not be read. Save it as UTF-8 CSV and retry.") from exc
        existing = {str(r["sku"]).casefold(): dict(r) for r in self.conn.execute("SELECT * FROM products").fetchall() if str(r["sku"]).strip()}
        known_barcodes = {str(r[0]).casefold(): int(r[1]) for r in self.conn.execute("SELECT barcode,product_id FROM product_barcodes").fetchall()}
        seen_skus, seen_barcodes, seen_variants = set(), set(), set()
        outcomes = []
        for line_no, row in source_rows:
            sku = row.get("sku", "").strip()
            name = row.get("name", "").strip()
            barcode = row.get("barcode", "").strip()
            status, message = "ready", "Ready to import"
            try:
                if not name or len(name) > 160:
                    raise OperationError("Product name is required (up to 160 characters).")
                if sku and (len(sku) > 80 or any(ord(ch) < 32 for ch in sku)):
                    raise OperationError("SKU must be 80 characters or fewer and contain no control characters.")
                if not sku:
                    message = "SKU will be generated automatically on import"
                elif sku.casefold() in seen_skus:
                    raise OperationError(f"Duplicate SKU {sku!r} appears more than once in this file.")
                else:
                    seen_skus.add(sku.casefold())
                if barcode:
                    if barcode.casefold() in seen_barcodes:
                        raise OperationError(f"Duplicate barcode {barcode!r} appears more than once in this file.")
                    seen_barcodes.add(barcode.casefold())
                    owner = known_barcodes.get(barcode.casefold())
                    existing_id = int(existing.get(sku.casefold(), {}).get("id", -1)) if sku else -1
                    if owner is not None and owner != existing_id:
                        raise OperationError(f"Barcode {barcode!r} is already assigned to another product.")
                self._decimal(row.get("selling_price", "0"), "Selling price", minimum=Decimal("0"))
                self._decimal(row.get("cost_price", "0"), "Cost price", minimum=Decimal("0"))
                self._decimal(row.get("reorder_level", "0") or "0", "Reorder level", minimum=Decimal("0"), places=3)
                unit_size = self._decimal(row.get("units_per_stock_unit", "1") or "1", "Contents per stock unit", minimum=Decimal("0"), places=3)
                if unit_size <= 0:
                    raise OperationError("Contents per stock unit must be greater than zero.")
                unit_size = self._decimal(row.get("units_per_stock_unit", "1") or "1", "Contents per stock unit", minimum=Decimal("0"), places=3)
                if unit_size <= 0:
                    raise OperationError("Contents per stock unit must be greater than zero.")
                variant_group = str(row.get("variant_group", "") or "").strip()[:160]
                variant_attrs = self._variant_attributes_json(row.get("variant_attributes", row.get("variant_attributes_json", "")))
                if variant_attrs != "{}" and not variant_group:
                    raise OperationError("Enter a variant group when defining variant attributes.")
                if variant_group and variant_attrs != "{}":
                    variant_key = (variant_group.casefold(), variant_attrs)
                    if variant_key in seen_variants:
                        raise OperationError("Duplicate variant group and attribute combination appears more than once in this file.")
                    seen_variants.add(variant_key)
                    for existing_item in existing.values():
                        if (str(existing_item.get("variant_group", "")).casefold() == variant_group.casefold()
                                and str(existing_item.get("variant_attributes_json", "{}")) == variant_attrs
                                and (not sku or str(existing_item.get("sku", "")).casefold() != sku.casefold())):
                            raise OperationError("This variant group and attribute combination already exists in the catalogue.")
                if "active" in row and row["active"] and row["active"].casefold() not in {"1", "0", "true", "false", "yes", "no", "active", "inactive"}:
                    raise OperationError("Active must be yes/no, true/false, or 1/0.")
                if sku and sku.casefold() in existing:
                    status, message = ("update" if update_existing else "skip"), ("Will update existing product details; stock will not change" if update_existing else "Existing SKU will be skipped")
                outcomes.append({"line": line_no, "sku": sku, "name": name, "status": status, "message": message, "data": row})
            except (OperationError, ValueError) as exc:
                outcomes.append({"line": line_no, "sku": sku, "name": name, "status": "error", "message": str(exc), "data": row})
        counts = {key: sum(1 for item in outcomes if item["status"] == key) for key in ("ready", "update", "skip", "error")}
        if dry_run:
            return {"dry_run": True, **counts, "rows": outcomes, "created": 0, "updated": 0, "failed": counts["error"]}
        created = updated = skipped = failed = 0
        for item in outcomes:
            if item["status"] == "error":
                failed += 1
                continue
            row = item["data"]
            sku = row.get("sku", "").strip()
            if item["status"] == "skip":
                skipped += 1
                continue
            try:
                active_raw = row.get("active", "yes").casefold()
                active = active_raw not in {"0", "false", "no", "inactive"}
                fields = dict(sku=sku, name=row["name"], selling_price=row.get("selling_price", "0") or "0",
                              cost_price=row.get("cost_price", "0") or "0", unit=row.get("unit", "each") or "each",
                              base_unit=row.get("base_unit", row.get("unit", "each")) or row.get("unit", "each"),
                              units_per_stock_unit=row.get("units_per_stock_unit", "1") or "1",
                              reorder_level=row.get("reorder_level", "0") or "0", category=row.get("category", ""),
                              barcode=row.get("barcode", ""), description=row.get("description", ""),
                              brand=row.get("brand", ""), model=row.get("model", ""), supplier_code=row.get("supplier_code", ""),
                              variant_group=row.get("variant_group", ""), variant_attributes=row.get("variant_attributes", row.get("variant_attributes_json", "")))
                if item["status"] == "update":
                    existing_row = existing.get(sku.casefold())
                    self.update_product(existing_row["id"], sku, fields["name"], fields["selling_price"], fields["cost_price"],
                                        fields["reorder_level"], fields["unit"], active, fields["category"], fields["barcode"],
                                        fields["description"], fields["brand"], fields["model"], fields["supplier_code"],
                                        sku_change_reason="Catalogue CSV import", variant_group=fields["variant_group"],
                                        variant_attributes=fields["variant_attributes"], base_unit=fields["base_unit"],
                                        units_per_stock_unit=fields["units_per_stock_unit"])
                    updated += 1
                else:
                    self.create_product(**fields)
                    created += 1
            except (OperationError, ValueError, sqlite3.IntegrityError) as exc:
                failed += 1
                item["status"], item["message"] = "error", str(exc)
        return {"dry_run": False, "created": created, "updated": updated, "skipped": skipped,
                "failed": failed, "rows": outcomes}

    def stock_on_hand(self, product_id):
        self._require_any_module(("Inventory","Sales","Purchases","Reports","Repair Tickets"))
        # Single aggregate query — never fetch every movement row into Python.
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CAST(quantity_delta AS REAL)),0) FROM stock_movements WHERE product_id=?",
            (int(product_id),),
        ).fetchone()
        return Decimal(str(row[0] if row else 0))

    def stock_in_base_units(self, product_id):
        """Return both stock quantity and content-unit quantity (e.g. 12 bags × 2 kg = 24 kg)."""
        self._require_any_module(("Inventory", "Sales", "Purchases", "Reports", "Repair Tickets"))
        row = self.conn.execute("SELECT base_unit,units_per_stock_unit FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise OperationError("The selected product no longer exists.")
        quantity = self.stock_on_hand(product_id)
        factor = self._decimal(row["units_per_stock_unit"], "Contents per stock unit", minimum=Decimal("0"), places=3)
        return (quantity * factor).quantize(Decimal("0.001")), str(row["base_unit"] or "")

    @staticmethod
    def product_unit_description(product):
        """Describe a stock unit and its contents, without changing transaction quantity semantics."""
        try:
            size = Decimal(str(product["units_per_stock_unit"] or "1"))
        except Exception:
            size = Decimal("1")
        stock_unit = str(product["unit"] or "each")
        base_unit = str(product["base_unit"] or stock_unit)
        if size == Decimal("1") and base_unit.casefold() == stock_unit.casefold():
            return stock_unit
        return f"{stock_unit} ({format(size.normalize(), 'f')} {base_unit} each)"

    def sku_label_records(self, copies_by_product, capability="print", max_labels=5000):
        """Validate a batch of product IDs and per-product label-copy counts."""
        self._require_module("Inventory")
        self._require(capability)
        if not isinstance(copies_by_product, dict) or not copies_by_product:
            raise OperationError("Select at least one product for the SKU label batch.")
        normalized = {}
        total = 0
        for key, value in copies_by_product.items():
            try:
                product_id = int(key)
                copies = int(value)
            except (TypeError, ValueError):
                raise OperationError("Product IDs and label copy counts must be whole numbers.")
            if product_id <= 0 or copies < 1 or copies > 500:
                raise OperationError("Each selected product must have between 1 and 500 label copies.")
            normalized[product_id] = copies
            total += copies
        if total > max_labels:
            raise OperationError(f"A single batch is limited to {max_labels:,} labels. Split this into smaller batches.")
        marks = ",".join("?" for _ in normalized)
        rows = self.conn.execute(f"""SELECT p.*,
            COALESCE((SELECT SUM(CAST(m.quantity_delta AS REAL)) FROM stock_movements m WHERE m.product_id=p.id),0) AS stock_on_hand
            FROM products p WHERE p.id IN ({marks}) ORDER BY p.brand COLLATE NOCASE,p.sku COLLATE NOCASE,p.name COLLATE NOCASE""", tuple(normalized)).fetchall()
        found = {int(row["id"]) for row in rows}
        missing = set(normalized) - found
        if missing:
            raise OperationError("One or more selected products no longer exist. Refresh the catalogue and try again.")
        return [{**dict(row), "copies": normalized[int(row["id"])],
                 "unit_description": self.product_unit_description(row)} for row in rows]

    def export_sku_labels_csv(self, file_path, copies_by_product):
        """Export one row per physical label, suitable for downstream label workflows."""
        self._require_module("Inventory")
        self._require("export")
        products = self.sku_label_records(copies_by_product, capability="export")
        fields = ["product_id", "sku", "barcode", "name", "variant_attributes", "stock_unit", "units_per_stock_unit", "base_unit", "selling_price", "copy_number"]
        try:
            with open(file_path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for product in products:
                    for copy_number in range(1, product["copies"] + 1):
                        writer.writerow({"product_id": product["id"], "sku": product["sku"], "barcode": product["barcode"],
                            "name": product["name"], "variant_attributes": self._variant_attributes_text(product["variant_attributes_json"]),
                            "stock_unit": product["unit"], "units_per_stock_unit": product["units_per_stock_unit"],
                            "base_unit": product["base_unit"], "selling_price": product["selling_price"], "copy_number": copy_number})
        except OSError as exc:
            raise OperationError(f"Could not export SKU labels: {exc}") from exc
        return sum(item["copies"] for item in products)

    def export_sku_labels_pdf(self, file_path, copies_by_product, label_width_mm=63, label_height_mm=32, columns=3, capability="export"):
        """Create an A4 sheet of printable product labels with Code 128-encoded SKUs.

        The barcode encodes the internal SKU, not a GS1/EAN retail identifier.
        The POS/product lookup can resolve that SKU when scanned.
        """
        self._require_module("Inventory")
        self._require(capability)
        products = self.sku_label_records(copies_by_product, capability=capability)
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
            from reportlab.pdfgen import canvas
            from reportlab.graphics.barcode import createBarcodeDrawing
            from reportlab.graphics import renderPDF
        except ImportError as exc:
            raise OperationError("PDF label export requires reportlab. Install the listed application dependencies and retry.") from exc
        if int(columns) < 1 or int(columns) > 4 or label_width_mm < 35 or label_width_mm > 90 or label_height_mm < 22 or label_height_mm > 60:
            raise OperationError("Choose a label size between 35–90 mm wide and 22–60 mm high, with 1–4 columns.")
        page_w, page_h = A4
        label_w, label_h = float(label_width_mm) * mm, float(label_height_mm) * mm
        margin_x, margin_y, gap_x, gap_y = 7 * mm, 8 * mm, 2 * mm, 2 * mm
        usable_w = page_w - 2 * margin_x
        if columns * label_w + (columns - 1) * gap_x > usable_w:
            raise OperationError("The selected label width and columns do not fit on A4 paper.")
        rows_per_page = int((page_h - 2 * margin_y + gap_y) // (label_h + gap_y))
        if rows_per_page < 1:
            raise OperationError("The selected label height does not fit on A4 paper.")
        total_labels = sum(product["copies"] for product in products)
        if total_labels > 5000:
            raise OperationError("A single batch is limited to 5,000 labels.")
        labels = []
        for product in products:
            for copy_number in range(1, product["copies"] + 1):
                labels.append((product, copy_number))
        business_name = ""
        # Use no business settings dependency here; label content remains printable
        # even if this service is exercised outside the desktop Store wrapper.
        try:
            business_name = str(self.conn.execute("SELECT value FROM settings WHERE key='business_name'").fetchone()[0] or "")
        except (sqlite3.Error, TypeError):
            business_name = ""
        try:
            pdf = canvas.Canvas(str(file_path), pagesize=A4, pageCompression=1)
            pdf.setTitle("NextGen ERP SKU Product Labels")
            for index, (product, copy_number) in enumerate(labels):
                if index and index % (columns * rows_per_page) == 0:
                    pdf.showPage()
                pos = index % (columns * rows_per_page)
                col = pos % columns
                row = pos // columns
                x = margin_x + col * (label_w + gap_x)
                y = page_h - margin_y - (row + 1) * label_h - row * gap_y
                pdf.setStrokeColor(colors.HexColor("#B8C0CC"))
                pdf.setLineWidth(0.4)
                pdf.roundRect(x, y, label_w, label_h, 1.5 * mm, stroke=1, fill=0)
                left = x + 2.5 * mm
                right = x + label_w - 2.5 * mm
                current_y = y + label_h - 4.2 * mm
                if business_name:
                    pdf.setFont("Helvetica-Bold", 6.5)
                    pdf.drawString(left, current_y, business_name[:42])
                    current_y -= 3.3 * mm
                pdf.setFont("Helvetica-Bold", 8)
                name = str(product["name"] or "")
                pdf.drawString(left, current_y, name[:45])
                current_y -= 4 * mm
                attr = self._variant_attributes_text(product.get("variant_attributes_json", ""))
                unit_detail = product.get("unit_description", product.get("unit", "each"))
                details = (attr + " · " if attr else "") + str(unit_detail)
                pdf.setFont("Helvetica", 6.2)
                pdf.drawString(left, current_y, details[:65])
                current_y -= 2.5 * mm
                sku = str(product["sku"])
                # Dynamically reduce module width for long SKUs so the code fits the label.
                max_bar_width = label_w - 6 * mm
                bar_width = min(0.30 * mm, max_bar_width / max(1, (11 * (len(sku) + 4))))
                drawing = createBarcodeDrawing("Code128", value=sku, barWidth=max(0.13 * mm, bar_width), barHeight=7.5 * mm,
                                               humanReadable=False, quiet=True)
                drawing_width = min(float(drawing.width), max_bar_width)
                renderPDF.draw(drawing, pdf, left, y + 7.1 * mm)
                pdf.setFont("Helvetica-Bold", 7)
                pdf.drawCentredString(x + label_w / 2, y + 4.0 * mm, sku[:70])
                pdf.setFont("Helvetica", 6)
                price = f"{product['selling_price']}"
                pdf.drawRightString(right, y + 4.0 * mm, price[:18])
                if product.get("barcode"):
                    pdf.setFont("Helvetica", 5.5)
                    pdf.drawString(left, y + 1.4 * mm, "Barcode: " + str(product["barcode"])[:40])
                # Retain selected copy number in the export; it is not printed on-label.
            pdf.save()
        except OSError as exc:
            raise OperationError(f"Could not write the SKU label PDF: {exc}") from exc
        return {"labels": total_labels, "products": len(products), "pages": (total_labels + columns * rows_per_page - 1) // (columns * rows_per_page)}

    def low_stock_products(self, limit=10000):
        self._require_module("Inventory")
        try:
            limit=max(1,min(int(limit),10000))
        except (TypeError,ValueError):
            raise OperationError("The low-stock result limit must be a whole number.")
        return _as_dicts(self.conn.execute("""
            SELECT p.*, COALESCE(SUM(CAST(m.quantity_delta AS REAL)),0) AS stock_on_hand
            FROM products p LEFT JOIN stock_movements m ON m.product_id=p.id
            WHERE p.active=1 AND p.track_stock=1
            GROUP BY p.id
            HAVING COALESCE(SUM(CAST(m.quantity_delta AS REAL)),0) <= CAST(p.reorder_level AS REAL)
            ORDER BY stock_on_hand ASC,p.category COLLATE NOCASE,p.name COLLATE NOCASE,p.id
            LIMIT ?
        """,(limit,)).fetchall())

    def low_stock_count(self):
        self._require_module("Inventory")
        return self.conn.execute("""
            SELECT COUNT(*) FROM products p
            WHERE p.active=1 AND p.track_stock=1 AND COALESCE(
                (SELECT SUM(CAST(m.quantity_delta AS REAL)) FROM stock_movements m WHERE m.product_id=p.id),0
            ) <= CAST(p.reorder_level AS REAL)
        """).fetchone()[0]

    def _normalize_lines(self, lines, price_field):
        inclusive=price_field=="unit_price" and self._tax_inclusive()
        tax_rate=self._tax_config()[1] if inclusive else Decimal("0")
        if not lines:
            raise OperationError("Add at least one product line.")
        normalized=[]
        for line in lines:
            try:
                product_id=int(line["product_id"])
            except (KeyError,TypeError,ValueError):
                raise OperationError("Every line must select a valid product.")
            qty=self._decimal(line.get("quantity"),"Quantity",minimum=Decimal("0.001"),places=3)
            product=self.conn.execute("SELECT * FROM products WHERE id=? AND active=1",(product_id,)).fetchone()
            if not product:
                raise OperationError(f"Product {product_id} is unavailable.")
            default_price=product["selling_price"] if price_field=="unit_price" else product["cost_price"]
            price=self._decimal(line.get(price_field,default_price),"Unit price" if price_field=="unit_price" else "Unit cost",minimum=Decimal("0"))
            amount=(qty*price).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            if price_field=="unit_price" and inclusive and tax_rate>0:
                net,tax=self._inclusive_split(amount,tax_rate)
                normalized.append(TaxedLine(product,qty,price,net,tax))
            else:
                normalized.append((product,qty,price,amount))
        return normalized

    @staticmethod
    def _payment_status(total, paid):
        if paid <= 0:
            return "Unpaid"
        if paid >= total:
            return "Paid"
        return "Partial"

    PAYMENT_METHODS = ("Cash", "M-Pesa", "Bank transfer", "Card", "Credit / unpaid", "Other")
    WARRANTY_PERIODS = ("1 month", "3 months", "6 months", "1 year", "N/A")

    @classmethod
    def _payment_method(cls, value, *, allow_unspecified=False):
        method=str(value or "Cash").strip()
        # POS and older UIs may use friendlier labels
        aliases = {
            "Customer account (credit)": "Credit / unpaid",
            "Credit": "Credit / unpaid",
            "Account": "Credit / unpaid",
            "Unpaid": "Credit / unpaid",
        }
        method = aliases.get(method, method)
        allowed=set(cls.PAYMENT_METHODS)
        if allow_unspecified:
            allowed.add("Unspecified")
        if method not in allowed:
            raise OperationError("Choose a supported payment method: " + ", ".join(cls.PAYMENT_METHODS) + ".")
        return method

    @classmethod
    def _warranty_period(cls, value):
        text = str(value or "").strip()
        if not text:
            raise OperationError("Warranty period is required. Choose 1 month, 3 months, 6 months, 1 year, N/A, or enter a custom period.")
        # Allow the standard set or any reasonable custom string (e.g. "18 months", "2 years")
        if len(text) > 40:
            raise OperationError("Warranty period is too long (max 40 characters).")
        return text

    def record_sale(self, customer_name, lines, amount_paid=0, customer_id=None, payment_method="Cash",
                    payment_reference="", warranty_period="N/A", served_by_name="", served_by_code=""):
        self._require_module("Sales")
        self._require("create")
        payment_method=self._payment_method(payment_method)
        payment_reference=str(payment_reference or "").strip()[:120]
        warranty_period=self._warranty_period(warranty_period)
        normalized=self._normalize_lines(lines,"unit_price")
        tax_label,tax_rate=self._tax_config()
        subtotal=sum((line[3] for line in normalized),Decimal("0")).quantize(Decimal("0.01"))
        tax_total=sum((self._line_tax(line,tax_rate) for line in normalized),Decimal("0.00"))
        total=subtotal+tax_total
        paid=self._decimal(amount_paid,"Amount paid",minimum=Decimal("0"))
        if payment_method == "Credit / unpaid":
            # Explicit credit sale: any amount paid is treated as a deposit; zero is pure credit
            if paid > total:
                raise OperationError("Amount paid cannot exceed the sale total including tax.")
        else:
            if paid > total:
                raise OperationError("Amount paid cannot exceed the sale total including tax.")
            if paid == 0 and total > 0:
                # Force the operator to choose Credit / unpaid rather than silently creating a zero-paid cash sale
                raise OperationError("Amount paid is zero. Choose payment method 'Credit / unpaid' for credit sales, or enter the amount received.")
        if paid > 0 and payment_method == "Credit / unpaid" and paid >= total:
            # Fully paid under the credit label is just a normal sale
            payment_method = "Other"
        if payment_method == "M-Pesa" and paid > 0 and not payment_reference:
            raise OperationError("Enter the confirmed M-Pesa transaction code for this payment.")
        if payment_method == "Credit / unpaid" and not str(customer_name or "").strip():
            raise OperationError("Credit sales require a named customer.")
        return self._complete_sale(
            normalized, customer_name, paid, customer_id, self._stamp(),
            tax_label=tax_label, tax_rate=tax_rate,
            payment_method=payment_method, payment_reference=payment_reference,
            warranty_period=warranty_period,
            served_by_name=served_by_name, served_by_code=served_by_code,
        )

    def _complete_sale(self, normalized, customer_name, paid, customer_id, timestamp, quote_id=None, tax_label=None, tax_rate=None,
                       payment_method="Cash", payment_reference="", warranty_period="N/A",
                       served_by_name="", served_by_code=""):
        payment_method=self._payment_method(payment_method)
        payment_reference=str(payment_reference or "").strip()[:120]
        warranty_period=self._warranty_period(warranty_period)
        served_by_name=str(served_by_name or "").strip()[:100]
        served_by_code=str(served_by_code or "").strip()[:40]
        if not served_by_name:
            served_by_name = str(self.actor_username or "Staff")[:100]
        with self._transaction():
            quote=None
            if quote_id is not None:
                quote=self.conn.execute("SELECT * FROM quotes WHERE id=?",(int(quote_id),)).fetchone()
                if not quote or quote["status"]!="Accepted" or quote["converted_sale_id"] is not None:
                    raise OperationError("Only an accepted, unconverted quote can be invoiced.")
                customer_id=quote["customer_id"]
                customer_name=quote["customer_name"]
                tax_label=quote["tax_label"]
                tax_rate=Decimal(quote["tax_rate"])
            if tax_rate is None:
                tax_label,tax_rate=self._tax_config()
            else:
                tax_rate=self._decimal(tax_rate,"Tax rate",minimum=Decimal("0"))
                tax_label=str(tax_label or "Tax").strip()[:60] or "Tax"
            if tax_rate>Decimal("100"):
                raise OperationError("Tax rate cannot exceed 100%.")
            line_taxes=[self._line_tax(line,tax_rate) for line in normalized]
            subtotal=sum((line[3] for line in normalized),Decimal("0")).quantize(Decimal("0.01"))
            tax_total=sum(line_taxes,Decimal("0.00"))
            total=subtotal+tax_total
            if paid>total:
                raise OperationError("Amount paid cannot exceed the sale total including tax.")
            if customer_id is not None:
                customer=self.conn.execute("SELECT name FROM customers WHERE id=?",(int(customer_id),)).fetchone()
                if not customer:
                    raise OperationError("The selected customer no longer exists.")
                customer_name=customer["name"]
            customer_name=str(customer_name or "").strip()[:160]
            menu_by_product,recipe_lines_by_product,recipe_requirements=self._recipe_sale_plan(normalized)
            required={}
            products_by_id={}
            for product,qty,price,amount in normalized:
                products_by_id[product["id"]]=product
                if bool(product["track_stock"]):
                    required[product["id"]]=required.get(product["id"],Decimal("0"))+qty
            for ingredient_id,needed in recipe_requirements.items():
                required[ingredient_id]=required.get(ingredient_id,Decimal("0"))+needed
                if ingredient_id not in products_by_id:
                    products_by_id[ingredient_id]=self.conn.execute("SELECT * FROM products WHERE id=?",(int(ingredient_id),)).fetchone()
            for product_id,needed in required.items():
                product=products_by_id[product_id]
                available=self.stock_on_hand(product_id)
                if available<needed:
                    raise OperationError(f"Not enough stock for {product['name']} ({product['sku']}). Required: {self._quantity(needed)} {product['unit']}; available: {self._quantity(available)}.")
                if bool(product["track_batches"]):
                    tracked=self._batch_balance(product_id)
                    if tracked!=available.quantize(Decimal("0.001")):
                        raise OperationError(f"Batch quantities for {product['name']} do not match the stock ledger. Reconcile the lots before selling.")
                    today=datetime.now().strftime("%Y-%m-%d")
                    lot_rows=self.conn.execute("SELECT remaining_quantity FROM inventory_batches WHERE product_id=? AND status='In stock' AND CAST(remaining_quantity AS REAL)>0 AND (expiry_date='' OR expiry_date>=?)",(product_id,today)).fetchall()
                    sellable=sum((Decimal(r["remaining_quantity"]) for r in lot_rows),Decimal("0.000"))
                    if sellable<needed:
                        raise OperationError(f"Not enough unexpired, sellable lots for {product['name']} ({product['sku']}). Available by lot: {self._quantity(sellable)} {product['unit']}.")
            cur=self.conn.execute(
                """INSERT INTO sales(
                    sale_number,customer_id,customer_name,status,payment_status,total,paid_total,created_at,
                    quote_id,subtotal,tax_rate,tax_total,tax_label,
                    payment_method,payment_reference,created_by,served_by_name,served_by_code,warranty_period
                ) VALUES('',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (customer_id, customer_name, "Completed", self._payment_status(total, paid),
                 self._money(total), self._money(paid), timestamp, quote_id,
                 self._money(subtotal), self._money(tax_rate), self._money(tax_total), tax_label,
                 payment_method, payment_reference, str(self.actor_username or "")[:80],
                 served_by_name, served_by_code, warranty_period))
            sale_id=cur.lastrowid
            sale_number=self._next_number("SALE",sale_id)
            self.conn.execute("UPDATE sales SET sale_number=? WHERE id=?",(sale_number,sale_id))
            for (product,qty,price,amount),tax_amount in zip(normalized,line_taxes):
                line_id=self.conn.execute("INSERT INTO sale_lines(sale_id,product_id,sku,description,quantity,unit_price,line_total,tax_amount) VALUES(?,?,?,?,?,?,?,?)",
                    (sale_id,product["id"],product["sku"],product["name"],self._quantity(qty),self._money(price),self._money(amount),self._money(tax_amount))).lastrowid
                product_id=int(product["id"])
                if bool(product["track_stock"]):
                    if bool(product["track_batches"]):
                        self._consume_batches(product_id,qty,timestamp,sale_line_id=line_id)
                    self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                        (product_id,self._quantity(-qty),"sale","sale",sale_id,sale_number,timestamp))
                menu=menu_by_product.get(product_id)
                if menu:
                    recipe=self.conn.execute("SELECT * FROM restaurant_recipe_lines WHERE menu_item_id=? ORDER BY id",(int(menu["id"]),)).fetchall()
                    self._record_recipe_consumption(line_id,menu,recipe,qty,sale_id,timestamp)
                    ticket_id=self.conn.execute("""INSERT INTO restaurant_kitchen_tickets(sale_id,sale_line_id,menu_item_id,item_name,quantity,kitchen_station,status,notes,created_at,updated_at)
                        VALUES(?,?,?,?,?,?, 'Queued','',?,?)""",(sale_id,line_id,int(menu["id"]),str(product["name"]),self._quantity(qty),menu["kitchen_station"],timestamp,timestamp)).lastrowid
                    self.conn.execute("INSERT INTO restaurant_kitchen_events(ticket_id,from_status,to_status,note,changed_by,created_at) VALUES(?,?,'Queued','Sale received',?,?)",(int(ticket_id),"",self.actor_username,timestamp))
                    self._enqueue("restaurant_kitchen_tickets",int(ticket_id),{"id":int(ticket_id),"sale_id":int(sale_id),"sale_line_id":int(line_id),"menu_item_id":int(menu["id"]),"item_name":str(product["name"]),"quantity":self._quantity(qty),"status":"Queued"},timestamp)
            if paid:
                self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES('in',?,?,?,?,?,?,?,?)",
                    (self._money(paid),"sale",sale_id,f"Payment received for {sale_number}",timestamp,payment_method,payment_reference,"Sales receipt"))
            self._enqueue("sales",sale_id,{"id":sale_id,"sale_number":sale_number,"customer_name":customer_name,
                "status":"Completed","subtotal":self._money(subtotal),"tax_label":tax_label,"tax_rate":self._money(tax_rate),
                "tax_total":self._money(tax_total),"total":self._money(total),"paid_total":self._money(paid),
                "payment_method":payment_method,"payment_reference":payment_reference,"quote_id":quote_id,
                "created_by":str(self.actor_username or ""),"served_by_name":served_by_name,
                "served_by_code":served_by_code,"warranty_period":warranty_period,
                "lines":[{"product_id":p["id"],"sku":p["sku"],"quantity":self._quantity(q),"unit_price":self._money(price),"line_total":self._money(amount),"tax_amount":self._money(tax)} for (p,q,price,amount),tax in zip(normalized,line_taxes)]},timestamp)
            if quote is not None:
                self.conn.execute("UPDATE quotes SET status='Converted',converted_sale_id=?,updated_at=? WHERE id=?",(sale_id,timestamp,int(quote_id)))
                self._enqueue("quotes",int(quote_id),{"id":int(quote_id),"quote_number":quote["quote_number"],"status":"Converted","sale_id":sale_id},timestamp)
        return sale_id

    def create_quote(self, customer_name, lines, customer_id=None, expires_on=""):
        self._require_module("Sales")
        self._require("create")
        normalized=self._normalize_lines(lines,"unit_price")
        tax_label,tax_rate=self._tax_config()
        subtotal=sum((line[3] for line in normalized),Decimal("0")).quantize(Decimal("0.01"))
        line_taxes=[self._line_tax(line,tax_rate) for line in normalized]
        tax_total=sum(line_taxes,Decimal("0.00"))
        total=subtotal+tax_total
        timestamp=self._stamp()
        with self._transaction():
            if customer_id is not None:
                customer=self.conn.execute("SELECT name FROM customers WHERE id=?",(int(customer_id),)).fetchone()
                if not customer: raise OperationError("The selected customer no longer exists.")
                customer_name=customer["name"]
            customer_name=str(customer_name or "").strip()[:160]
            if not customer_name: raise OperationError("Enter a customer name before saving the quotation.")
            expiry=str(expires_on or "").strip()
            if expiry:
                try: datetime.strptime(expiry,"%Y-%m-%d")
                except ValueError: raise OperationError("Quote expiry must use YYYY-MM-DD format.")
            cur=self.conn.execute("INSERT INTO quotes(quote_number,customer_id,customer_name,status,total,expires_on,created_at,updated_at,created_by,subtotal,tax_rate,tax_total,tax_label) VALUES('',?,?, 'Draft',?,?,?,?,?,?,?,?,?)",
                (customer_id,customer_name,self._money(total),expiry,timestamp,timestamp,self.actor_username,self._money(subtotal),self._money(tax_rate),self._money(tax_total),tax_label))
            quote_id=cur.lastrowid
            quote_number=self._next_number("QUO",quote_id)
            self.conn.execute("UPDATE quotes SET quote_number=? WHERE id=?",(quote_number,quote_id))
            for (product,qty,price,amount),tax_amount in zip(normalized,line_taxes):
                self.conn.execute("INSERT INTO quote_lines(quote_id,product_id,sku,description,quantity,unit_price,line_total,tax_amount) VALUES(?,?,?,?,?,?,?,?)",
                    (quote_id,product["id"],product["sku"],product["name"],self._quantity(qty),self._money(price),self._money(amount),self._money(tax_amount)))
            self._enqueue("quotes",quote_id,{"id":quote_id,"quote_number":quote_number,"customer_name":customer_name,
                "status":"Draft","subtotal":self._money(subtotal),"tax_label":tax_label,"tax_rate":self._money(tax_rate),"tax_total":self._money(tax_total),"total":self._money(total),"expires_on":expiry},timestamp)
        return quote_id

    def quotes(self, limit=500):
        self._require_module("Sales")
        return self.conn.execute("SELECT * FROM quotes ORDER BY id DESC LIMIT ?",(limit,)).fetchall()

    def quote_lines(self, quote_id):
        self._require_module("Sales")
        return self.conn.execute("SELECT * FROM quote_lines WHERE quote_id=? ORDER BY id",(int(quote_id),)).fetchall()

    def set_quote_status(self, quote_id, status):
        self._require_module("Sales")
        if status not in {"Sent","Accepted","Expired","Cancelled"}:
            raise OperationError("Unsupported quote status.")
        self._require("delete" if status=="Cancelled" else "edit")
        timestamp=self._stamp()
        with self._transaction():
            quote=self.conn.execute("SELECT * FROM quotes WHERE id=?",(int(quote_id),)).fetchone()
            if not quote or quote["status"] not in {"Draft","Sent","Accepted"}:
                raise OperationError("This quotation is no longer available for status changes.")
            if quote["status"]=="Draft" and status=="Sent":
                pass
            elif quote["status"] in {"Draft","Sent"} and status in {"Accepted","Expired","Cancelled"}:
                pass
            elif quote["status"]=="Accepted" and status=="Cancelled":
                pass
            else:
                raise OperationError(f"A quotation cannot move from {quote['status']} to {status}.")
            if status=="Accepted" and quote["expires_on"] and quote["expires_on"]<timestamp[:10]:
                raise OperationError("This quotation has expired and cannot be accepted.")
            self.conn.execute("UPDATE quotes SET status=?,updated_at=? WHERE id=?",(status,timestamp,int(quote_id)))
            self._enqueue("quotes",int(quote_id),{"id":int(quote_id),"quote_number":quote["quote_number"],"status":status},timestamp)
        return True

    def convert_quote(self, quote_id, amount_paid=0):
        self._require_module("Sales")
        self._require("create")
        quote=self.conn.execute("SELECT * FROM quotes WHERE id=?",(int(quote_id),)).fetchone()
        if not quote or quote["status"]!="Accepted" or quote["converted_sale_id"] is not None:
            raise OperationError("Only an accepted, unconverted quotation can be invoiced.")
        normalized=[]
        for line in self.quote_lines(quote_id):
            product=self.conn.execute("SELECT * FROM products WHERE id=? AND active=1",(line["product_id"],)).fetchone()
            if not product:
                raise OperationError(f"Quoted product {line['sku']} is no longer active.")
            snapshot=dict(product); snapshot["sku"]=line["sku"]; snapshot["name"]=line["description"]
            qty=Decimal(line["quantity"]); price=Decimal(line["unit_price"])
            normalized.append(TaxedLine(snapshot,qty,price,Decimal(line["line_total"]),Decimal(line["tax_amount"] or "0.00")))
        total=Decimal(quote["total"])
        paid=self._decimal(amount_paid,"Amount paid",minimum=Decimal("0"))
        if paid>total: raise OperationError("Amount paid cannot exceed the quotation total.")
        return self._complete_sale(normalized,quote["customer_name"],paid,quote["customer_id"],self._stamp(),int(quote_id),quote["tax_label"],Decimal(quote["tax_rate"]))

    def create_purchase_order(self, supplier_name, lines, supplier_id=None):
        self._require_module("Purchases")
        self._require("manage_inventory")
        normalized=self._normalize_lines(lines,"unit_cost")
        timestamp=self._stamp()
        with self._transaction():
            if supplier_id is not None:
                supplier=self.conn.execute("SELECT name FROM suppliers WHERE id=?",(int(supplier_id),)).fetchone()
                if not supplier:
                    raise OperationError("The selected supplier no longer exists.")
                supplier_name=supplier["name"]
            supplier_name=str(supplier_name or "").strip()[:160]
            if not supplier_name:
                raise OperationError("Enter a supplier name before saving the purchase order.")
            cur=self.conn.execute("INSERT INTO purchase_orders(order_number,supplier_id,supplier_name,status,created_at,updated_at) VALUES('',?,?, 'Ordered',?,?)",
                (supplier_id,supplier_name,timestamp,timestamp))
            order_id=cur.lastrowid
            order_number=self._next_number("PO",order_id)
            self.conn.execute("UPDATE purchase_orders SET order_number=? WHERE id=?",(order_number,order_id))
            order_lines=[]
            for product,qty,cost,_amount in normalized:
                line_id=self.conn.execute("INSERT INTO purchase_order_lines(purchase_order_id,product_id,sku,description,quantity,unit_cost,received_quantity) VALUES(?,?,?,?,?,?,'0')",
                    (order_id,product["id"],product["sku"],product["name"],self._quantity(qty),self._money(cost))).lastrowid
                order_lines.append({"id":line_id,"product_id":product["id"],"sku":product["sku"],"quantity":self._quantity(qty),"unit_cost":self._money(cost)})
            self._enqueue("purchase_orders",order_id,{"id":order_id,"order_number":order_number,"supplier_name":supplier_name,"status":"Ordered","lines":order_lines},timestamp)
        return order_id

    def purchase_orders(self, limit=500):
        self._require_module("Purchases")
        return self.conn.execute("SELECT * FROM purchase_orders ORDER BY id DESC LIMIT ?",(limit,)).fetchall()

    def purchase_order_lines(self, order_id):
        self._require_module("Purchases")
        return self.conn.execute("""SELECT pol.*,p.track_batches,p.unit,p.base_unit,p.units_per_stock_unit
            FROM purchase_order_lines pol JOIN products p ON p.id=pol.product_id
            WHERE pol.purchase_order_id=? ORDER BY pol.id""",(int(order_id),)).fetchall()

    def receive_purchase_order(self, order_id, lines, amount_paid=0, payment_method="Cash", payment_reference=""):
        self._require_module("Purchases")
        self._require("manage_inventory")
        if not isinstance(lines,(list,tuple)) or not lines:
            raise OperationError("Select at least one outstanding order line to receive.")
        timestamp=self._stamp()
        with self._transaction():
            order=self.conn.execute("SELECT * FROM purchase_orders WHERE id=?",(int(order_id),)).fetchone()
            if not order or order["status"] not in {"Ordered","Partially received"}:
                raise OperationError("Only open purchase orders can receive goods.")
            source={row["id"]:row for row in self.purchase_order_lines(order_id)}
            requested={}
            batch_meta={}
            for item in lines:
                try:
                    line_id=int(item["purchase_order_line_id"])
                except (KeyError,TypeError,ValueError):
                    raise OperationError("Each received quantity must refer to an order line.")
                if line_id not in source:
                    raise OperationError("A selected line does not belong to this purchase order.")
                qty=self._decimal(item.get("quantity"),"Received quantity",minimum=Decimal("0.001"),places=3)
                if line_id in requested:
                    raise OperationError("An order line can only appear once in a receipt.")
                batch_meta[line_id]={"batch_number":str(item.get("batch_number") or "").strip(),"expiry_date":str(item.get("expiry_date") or "").strip(),"batch_notes":str(item.get("batch_notes") or "").strip()}
                remaining=Decimal(source[line_id]["quantity"])-Decimal(source[line_id]["received_quantity"])
                if qty>remaining:
                    raise OperationError(f"Received quantity exceeds the outstanding order quantity for {source[line_id]['sku']}.")
                requested[line_id]=qty
            normalized=[]
            total=Decimal("0.00")
            for line_id,qty in requested.items():
                order_line=source[line_id]
                product=self.conn.execute("SELECT * FROM products WHERE id=? AND active=1",(order_line["product_id"],)).fetchone()
                if not product:
                    raise OperationError(f"Product {order_line['sku']} is no longer active.")
                if bool(product["track_batches"]):
                    if not batch_meta.get(line_id,{}).get("batch_number"):
                        raise OperationError(f"Product {product['name']} is batch-tracked. Enter its batch number in the receipt dialog before receiving.")
                    self._batch_expiry_date(batch_meta[line_id].get("expiry_date",""))
                cost=Decimal(order_line["unit_cost"])
                amount=(qty*cost).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
                total+=amount
                normalized.append((product,qty,cost,amount,line_id))
            paid=self._decimal(amount_paid,"Amount paid",minimum=Decimal("0"))
            payment_method=self._payment_method(payment_method)
            payment_reference=str(payment_reference or "").strip()[:120]
            if paid>total:
                raise OperationError("Amount paid cannot exceed the received goods total.")
            if paid>0 and payment_method=="M-Pesa" and not payment_reference:
                raise OperationError("Enter the confirmed M-Pesa transaction code for this payment.")
            buckets={}
            for product,qty,cost,amount,line_id in normalized:
                item=buckets.setdefault(int(product["id"]),[Decimal("0"),Decimal("0"),product])
                item[0]+=qty
                item[1]+=amount
            weighted_costs={}
            prior_costs={}
            for product_id,(qty,cost_total,product) in buckets.items():
                existing=self.stock_on_hand(product_id)
                new_qty=existing+qty
                old_cost=Decimal(product["cost_price"])
                prior_costs[product_id]=old_cost
                weighted_costs[product_id]=(old_cost*existing+cost_total)/new_qty if new_qty else old_cost
            cur=self.conn.execute("INSERT INTO purchases(purchase_number,order_id,supplier_id,supplier_name,status,payment_status,total,paid_total,created_at) VALUES('',?,?,?,?,?,?,?,?)",
                (int(order_id),order["supplier_id"],order["supplier_name"],"Received",self._payment_status(total,paid),self._money(total),self._money(paid),timestamp))
            purchase_id=cur.lastrowid
            purchase_number=self._next_number("PUR",purchase_id)
            self.conn.execute("UPDATE purchases SET purchase_number=? WHERE id=?",(purchase_number,purchase_id))
            sync_lines=[]
            for product,qty,cost,amount,line_id in normalized:
                self.conn.execute("INSERT INTO purchase_lines(purchase_id,product_id,sku,description,quantity,unit_cost,line_total,purchase_order_line_id,prior_cost) VALUES(?,?,?,?,?,?,?,?,?)",
                    (purchase_id,product["id"],product["sku"],product["name"],self._quantity(qty),self._money(cost),self._money(amount),line_id,self._money(prior_costs[int(product["id"])])))
                self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                    (product["id"],self._quantity(qty),"purchase_receipt","purchase",purchase_id,purchase_number,timestamp))
                if bool(product["track_batches"]):
                    meta=batch_meta[line_id]
                    self._insert_inventory_batch(product["id"],meta["batch_number"],meta["expiry_date"],qty,order["supplier_name"],"purchase",purchase_id,meta["batch_notes"],timestamp)
                self.conn.execute("UPDATE purchase_order_lines SET received_quantity=? WHERE id=?",
                    (self._quantity(Decimal(source[line_id]["received_quantity"])+qty),line_id))
                sync_lines.append({"purchase_order_line_id":line_id,"product_id":product["id"],"quantity":self._quantity(qty),"unit_cost":self._money(cost)})
            for product_id,average_cost in weighted_costs.items():
                self.conn.execute("UPDATE products SET cost_price=? WHERE id=?",(self._money(average_cost),product_id))
            if paid:
                self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES('out',?,?,?,?,?,?,?,?)",
                    (self._money(paid),"purchase",purchase_id,f"Payment for {purchase_number}",timestamp,payment_method,payment_reference,"Supplier payment"))
            current=self.purchase_order_lines(order_id)
            complete=all(Decimal(line["received_quantity"])==Decimal(line["quantity"]) for line in current)
            new_status="Received" if complete else "Partially received"
            self.conn.execute("UPDATE purchase_orders SET status=?,updated_at=? WHERE id=?",(new_status,timestamp,int(order_id)))
            self._enqueue("purchases",purchase_id,{"id":purchase_id,"purchase_number":purchase_number,"order_id":int(order_id),"supplier_name":order["supplier_name"],"status":"Received","total":self._money(total),"paid_total":self._money(paid),"payment_method":payment_method,"payment_reference":payment_reference,"lines":sync_lines},timestamp)
            self._enqueue("purchase_orders",int(order_id),{"id":int(order_id),"order_number":order["order_number"],"status":new_status,"received_lines":sync_lines},timestamp)
        return purchase_id

    def cancel_purchase_order(self, order_id, reason=""):
        self._require_module("Purchases")
        self._require("delete")
        timestamp=self._stamp()
        with self._transaction():
            order=self.conn.execute("SELECT * FROM purchase_orders WHERE id=?",(int(order_id),)).fetchone()
            if not order or order["status"] not in {"Ordered","Partially received"}:
                raise OperationError("Only an open purchase order can be cancelled.")
            self.conn.execute("UPDATE purchase_orders SET status='Cancelled',cancelled_at=?,cancellation_reason=?,updated_at=? WHERE id=?",
                (timestamp,str(reason).strip()[:300],timestamp,int(order_id)))
            self._enqueue("purchase_orders",int(order_id),{"id":int(order_id),"order_number":order["order_number"],"status":"Cancelled","reason":str(reason).strip()[:300]},timestamp)
        return True

    def receive_purchase(self, supplier_name, lines, amount_paid=0, supplier_id=None, payment_method="Cash", payment_reference=""):
        self._require_module("Purchases")
        self._require("manage_inventory")
        source_lines=list(lines or [])
        normalized=self._normalize_lines(source_lines,"unit_cost")
        if len(source_lines)!=len(normalized): raise OperationError("Purchase lines could not be validated consistently.")
        for (product,qty,cost,amount),source_line in zip(normalized,source_lines):
            if bool(product["track_batches"]) and not str(source_line.get("batch_number") or "").strip():
                raise OperationError(f"Product {product['name']} is batch-tracked. Enter batch_number and optional expiry_date on its purchase line, or receive it through Batches & Expiry.")
            if bool(product["track_batches"]): self._batch_expiry_date(source_line.get("expiry_date",""))
        total=sum((line[3] for line in normalized),Decimal("0")).quantize(Decimal("0.01"))
        paid=self._decimal(amount_paid,"Amount paid",minimum=Decimal("0"))
        payment_method=self._payment_method(payment_method)
        payment_reference=str(payment_reference or "").strip()[:120]
        if paid > total:
            raise OperationError("Amount paid cannot exceed the purchase total.")
        if paid>0 and payment_method=="M-Pesa" and not payment_reference:
            raise OperationError("Enter the confirmed M-Pesa transaction code for this payment.")
        timestamp=self._stamp()
        with self._transaction():
            if supplier_id is not None:
                supplier=self.conn.execute("SELECT name FROM suppliers WHERE id=?",(int(supplier_id),)).fetchone()
                if not supplier:
                    raise OperationError("The selected supplier no longer exists.")
                supplier_name=supplier["name"]
            supplier_name=str(supplier_name or "").strip()[:160]
            incoming={}
            products_by_id={}
            for product,qty,price,amount in normalized:
                bucket=incoming.setdefault(product["id"],[Decimal("0"),Decimal("0")])
                bucket[0]+=qty
                bucket[1]+=amount
                products_by_id[product["id"]]=product
            weighted_costs={}
            prior_costs={}
            for product_id,(qty,cost_total) in incoming.items():
                product=products_by_id[product_id]
                existing_qty=self.stock_on_hand(product_id)
                total_qty=existing_qty+qty
                existing_cost=Decimal(product["cost_price"])
                prior_costs[product_id]=existing_cost
                weighted_costs[product_id]=(existing_cost*existing_qty+cost_total)/total_qty if total_qty else existing_cost
            cur=self.conn.execute(
                "INSERT INTO purchases(purchase_number,supplier_id,supplier_name,status,payment_status,total,paid_total,created_at) VALUES('',?,?,?,?,?,?,?)",
                (supplier_id,supplier_name,"Received",self._payment_status(total,paid),self._money(total),self._money(paid),timestamp))
            purchase_id=cur.lastrowid
            purchase_number=self._next_number("PUR",purchase_id)
            self.conn.execute("UPDATE purchases SET purchase_number=? WHERE id=?",(purchase_number,purchase_id))
            for (product,qty,price,amount),source_line in zip(normalized,source_lines):
                self.conn.execute("INSERT INTO purchase_lines(purchase_id,product_id,sku,description,quantity,unit_cost,line_total,prior_cost) VALUES(?,?,?,?,?,?,?,?)",
                                  (purchase_id,product["id"],product["sku"],product["name"],self._quantity(qty),self._money(price),self._money(amount),self._money(prior_costs[int(product["id"])])))
                self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                                  (product["id"],self._quantity(qty),"purchase_receipt","purchase",purchase_id,purchase_number,timestamp))
                if bool(product["track_batches"]):
                    self._insert_inventory_batch(product["id"],source_line.get("batch_number"),source_line.get("expiry_date",""),qty,supplier_name,"purchase",purchase_id,source_line.get("batch_notes",""),timestamp)
            for product_id,average_cost in weighted_costs.items():
                self.conn.execute("UPDATE products SET cost_price=? WHERE id=?",(self._money(average_cost),product_id))
            if paid:
                self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES('out',?,?,?,?,?,?,?,?)",
                                  (self._money(paid),"purchase",purchase_id,f"Payment made for {purchase_number}",timestamp,payment_method,payment_reference,"Supplier payment"))
            self._enqueue("purchases",purchase_id,{"id":purchase_id,"purchase_number":purchase_number,"supplier_name":supplier_name,
                "status":"Received","total":self._money(total),"paid_total":self._money(paid),"payment_method":payment_method,"payment_reference":payment_reference,
                "lines":[{"product_id":p["id"],"sku":p["sku"],"quantity":self._quantity(q),"unit_cost":self._money(price),"line_total":self._money(amount)} for p,q,price,amount in normalized]},timestamp)
        return purchase_id

    def record_sale_return(self, sale_id, lines, reason="", payment_method="Unspecified", payment_reference=""):
        self._require_module("Sales")
        self._require("process_returns")
        if not isinstance(lines, (list, tuple)) or not lines:
            raise OperationError("Select at least one sold item to return.")
        payment_method=self._payment_method(payment_method,allow_unspecified=True)
        payment_reference=str(payment_reference or "").strip()[:120]
        timestamp=self._stamp()
        with self._transaction():
            sale=self.conn.execute("SELECT * FROM sales WHERE id=?",(int(sale_id),)).fetchone()
            if not sale or sale["status"] not in {"Completed", "Partially returned"}:
                raise OperationError("Only a completed sale with unreturned items can be returned.")
            source_lines={row["id"]:row for row in self.conn.execute("SELECT * FROM sale_lines WHERE sale_id=?",(int(sale_id),)).fetchall()}
            already={}
            already_tax={}
            already_base={}
            for returned_line in self.conn.execute(
                "SELECT rl.sale_line_id,rl.quantity,rl.tax_amount,rl.line_total FROM sale_return_lines rl JOIN sales_returns sr ON sr.id=rl.return_id WHERE sr.sale_id=?",(int(sale_id),)).fetchall():
                line_id=int(returned_line["sale_line_id"])
                already[line_id]=already.get(line_id,Decimal("0"))+Decimal(returned_line["quantity"])
                already_tax[line_id]=already_tax.get(line_id,Decimal("0.00"))+Decimal(returned_line["tax_amount"] or "0.00")
                already_base[line_id]=already_base.get(line_id,Decimal("0.00"))+Decimal(returned_line["line_total"])
            requested={}
            for item in lines:
                try:
                    line_id=int(item["sale_line_id"])
                except (KeyError,TypeError,ValueError):
                    raise OperationError("Each returned item must refer to a sale line.")
                if line_id not in source_lines:
                    raise OperationError("A selected item does not belong to this sale.")
                qty=self._decimal(item.get("quantity"),"Return quantity",minimum=Decimal("0.001"),places=3)
                if line_id in requested:
                    raise OperationError("A sale line can only appear once in a return.")
                requested[line_id]=(qty,bool(item.get("restock",True)))
            refund_total=Decimal("0.00")
            base_refund_total=Decimal("0.00")
            tax_refund_total=Decimal("0.00")
            return_amounts={}
            new_returned={}
            for line_id,(qty,restock) in requested.items():
                source=source_lines[line_id]
                prior=already.get(line_id,Decimal("0"))
                sold=Decimal(source["quantity"])
                if prior+qty>sold:
                    raise OperationError(f"Return quantity exceeds the remaining sold quantity for {source['sku']}.")
                if prior+qty == sold:
                    base_amount=Decimal(source["line_total"])-already_base.get(line_id,Decimal("0.00"))
                    tax_amount=Decimal(source["tax_amount"] or "0.00")-already_tax.get(line_id,Decimal("0.00"))
                else:
                    # Allocate the stored line total, not unit_price * qty: the line
                    # total is authoritative when discounts/adjustments exist. The final
                    # return consumes the exact remaining cents above.
                    base_amount=(Decimal(source["line_total"])*qty/sold).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
                    tax_amount=(Decimal(source["tax_amount"] or "0.00")*qty/sold).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
                amount=base_amount+tax_amount
                return_amounts[line_id]=(base_amount,tax_amount,amount)
                base_refund_total+=base_amount
                tax_refund_total+=tax_amount
                refund_total+=amount
                new_returned[line_id]=prior+qty
            if refund_total > Decimal(sale["paid_total"]):
                raise OperationError("The returned items exceed the refundable amount already paid. Customer credit returns are not supported yet.")
            return_id=self.conn.execute(
                "INSERT INTO sales_returns(return_number,sale_id,refund_amount,reason,created_at,created_by) VALUES('',?,?,?,?,?)",
                (int(sale_id),self._money(refund_total),str(reason).strip()[:300],timestamp,self.actor_username)).lastrowid
            return_number=self._next_number("RET",return_id)
            self.conn.execute("UPDATE sales_returns SET return_number=? WHERE id=?",(return_number,return_id))
            for line_id,(qty,restock) in requested.items():
                source=source_lines[line_id]
                base_amount,tax_amount,amount=return_amounts[line_id]
                return_line_id=self.conn.execute("INSERT INTO sale_return_lines(return_id,sale_line_id,product_id,sku,description,quantity,restocked,unit_price,line_total,tax_amount) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (return_id,line_id,source["product_id"],source["sku"],source["description"],self._quantity(qty),int(restock),source["unit_price"],self._money(base_amount),self._money(tax_amount))).lastrowid
                if restock:
                    tracked=self.conn.execute("SELECT track_batches,track_stock FROM products WHERE id=?",(source["product_id"],)).fetchone()
                    if tracked and not bool(tracked["track_stock"]):
                        raise OperationError(
                            f"Cannot restock {source['sku']}: stock tracking is disabled for this product. "
                            "Enable stock tracking first or record the return without restocking."
                        )
                    if tracked:
                        if bool(tracked["track_batches"]):
                            self._restore_batches_for_sale_line(line_id,qty,timestamp,return_line_id=return_line_id)
                        self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                            (source["product_id"],self._quantity(qty),"sale_return","sales_return",return_id,f"{return_number} · {source['sku']}",timestamp))
            if refund_total:
                if payment_method=="Credit / unpaid":
                    raise OperationError("A refund must use an actual payment channel.")
                if payment_method=="M-Pesa" and not payment_reference:
                    raise OperationError("Enter the confirmed M-Pesa transaction code for this refund.")
                self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES('out',?,?,?,?,?,?,?,?)",
                    (self._money(refund_total),"sale_return",return_id,f"Refund for {return_number}",timestamp,payment_method,payment_reference,"Customer refund"))
            is_fully_returned=all(new_returned.get(line_id,already.get(line_id,Decimal("0")))==Decimal(row["quantity"]) for line_id,row in source_lines.items())
            new_status="Returned" if is_fully_returned else "Partially returned"
            net_total=Decimal(sale["total"])-refund_total
            net_subtotal=Decimal(sale["subtotal"])-base_refund_total
            net_tax=Decimal(sale["tax_total"])-tax_refund_total
            net_paid=Decimal(sale["paid_total"])-refund_total
            self.conn.execute("UPDATE sales SET status=?,subtotal=?,tax_total=?,total=?,paid_total=?,payment_status=? WHERE id=?",
                (new_status,self._money(net_subtotal),self._money(net_tax),self._money(net_total),self._money(net_paid),self._payment_status(net_total,net_paid),int(sale_id)))
            self._enqueue("sales_returns",return_id,{"id":return_id,"return_number":return_number,"sale_id":int(sale_id),"refund_amount":self._money(refund_total),"reason":str(reason).strip()[:300],"payment_method":payment_method,"payment_reference":payment_reference,"lines":[{"sale_line_id":line_id,"quantity":self._quantity(qty),"restocked":restock} for line_id,(qty,restock) in requested.items()]},timestamp)
            self._enqueue("sales",int(sale_id),{"id":int(sale_id),"sale_number":sale["sale_number"],"status":new_status},timestamp)
        return return_id

    def returned_quantity(self, sale_line_id):
        rows=self.conn.execute("SELECT quantity FROM sale_return_lines WHERE sale_line_id=?",(int(sale_line_id),)).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows),Decimal("0"))

    def sales_returns(self, sale_id=None, limit=1000):
        self._require_module("Sales")
        if sale_id is None:
            return self.conn.execute("SELECT sr.*,s.sale_number,s.customer_name FROM sales_returns sr JOIN sales s ON s.id=sr.sale_id ORDER BY sr.id DESC LIMIT ?",(limit,)).fetchall()
        return self.conn.execute("SELECT * FROM sales_returns WHERE sale_id=? ORDER BY id DESC LIMIT ?",(int(sale_id),limit)).fetchall()

    def cancel_sale(self, sale_id, reason="", refund_amount=0):
        self._require_module("Sales")
        self._require("delete")
        timestamp=self._stamp()
        with self._transaction():
            sale=self.conn.execute("SELECT * FROM sales WHERE id=?",(sale_id,)).fetchone()
            if not sale or sale["status"] not in {"Completed", "Partially returned"}:
                raise OperationError("Only an active sale can be cancelled.")
            refunded=self._decimal(refund_amount,"Refund amount",minimum=Decimal("0"))
            refundable=Decimal(sale["paid_total"])
            if refunded > refundable:
                raise OperationError("Refund amount cannot exceed the remaining amount paid.")
            lines=self.conn.execute("SELECT * FROM sale_lines WHERE sale_id=?",(sale_id,)).fetchall()
            returned={}
            for row in self.conn.execute("SELECT rl.sale_line_id,rl.quantity FROM sale_return_lines rl JOIN sales_returns sr ON sr.id=rl.return_id WHERE sr.sale_id=?",(sale_id,)).fetchall():
                line_id=int(row["sale_line_id"])
                returned[line_id]=returned.get(line_id,Decimal("0"))+Decimal(row["quantity"])
            for line in lines:
                remaining=Decimal(line["quantity"])-returned.get(int(line["id"]),Decimal("0"))
                if remaining>0:
                    tracked=self.conn.execute("SELECT track_batches,track_stock FROM products WHERE id=?",(line["product_id"],)).fetchone()
                    if tracked and bool(tracked["track_stock"]):
                        if bool(tracked["track_batches"]): self._restore_batches_for_sale_line(int(line["id"]),remaining,timestamp)
                        self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                            (line["product_id"],self._quantity(remaining),"sale_reversal","sale",sale_id,f"Cancellation {sale['sale_number']}",timestamp))
                    else:
                        fraction=remaining/Decimal(line["quantity"])
                        self._restore_recipe_consumptions(int(line["id"]),fraction,int(sale_id),timestamp)
            self.conn.execute("UPDATE sales SET status='Cancelled',cancelled_at=?,cancellation_reason=? WHERE id=?",
                              (timestamp,str(reason).strip()[:300],sale_id))
            if refunded:
                self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at) VALUES('out',?,?,?,?,?)",
                                  (self._money(refunded),"sale_refund",sale_id,f"Refund for {sale['sale_number']}",timestamp))
            self._enqueue("sales",sale_id,{"id":sale_id,"sale_number":sale["sale_number"],"status":"Cancelled",
                "reason":str(reason).strip()[:300],"refund_amount":self._money(refunded)},timestamp)
        return True

    def cancel_purchase(self, purchase_id, reason="", refund_amount=0):
        self._require_module("Purchases")
        self._require("delete")
        timestamp=self._stamp()
        with self._transaction():
            purchase=self.conn.execute("SELECT * FROM purchases WHERE id=?",(purchase_id,)).fetchone()
            if not purchase or purchase["status"] != "Received":
                raise OperationError("Only a received purchase can be cancelled.")
            refunded=self._decimal(refund_amount,"Refund amount",minimum=Decimal("0"))
            if refunded > Decimal(purchase["paid_total"]):
                raise OperationError("Refund amount cannot exceed the amount paid.")
            lines=self.conn.execute("SELECT * FROM purchase_lines WHERE purchase_id=?",(purchase_id,)).fetchall()
            required={}
            for line in lines:
                required[line["product_id"]]=required.get(line["product_id"],Decimal("0"))+Decimal(line["quantity"])
            for product_id,received in required.items():
                available=self.stock_on_hand(product_id)
                if available < received:
                    raise OperationError(f"Cannot cancel {purchase['purchase_number']}: later operations have consumed some of its stock.")
            for product_id in required:
                purchase_move=self.conn.execute("SELECT MAX(id) FROM stock_movements WHERE product_id=? AND reference_type='purchase' AND reference_id=?",(product_id,purchase_id)).fetchone()[0]
                later=self.conn.execute("SELECT 1 FROM stock_movements WHERE product_id=? AND id>? LIMIT 1",(product_id,purchase_move or 0)).fetchone()
                if later:
                    raise OperationError(f"Cannot cancel {purchase['purchase_number']} after later stock activity because weighted-average cost can no longer be reversed safely.")
            prior_cost_by_product={}
            for line in lines:
                if line["prior_cost"] is not None:
                    prior_cost_by_product[int(line["product_id"])]=Decimal(line["prior_cost"])
                product_tracking=self.conn.execute("SELECT track_batches FROM products WHERE id=?",(line["product_id"],)).fetchone()
                if product_tracking and bool(product_tracking[0]):
                    self.conn.execute("UPDATE inventory_batches SET status='Cancelled',remaining_quantity='0.000',updated_at=? WHERE product_id=? AND source_type='purchase' AND source_id=?",(timestamp,line["product_id"],purchase_id))
                qty=Decimal(line["quantity"])
                self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                                  (line["product_id"],self._quantity(-qty),"purchase_reversal","purchase",purchase_id,f"Cancellation {purchase['purchase_number']}",timestamp))
            for product_id,prior_cost in prior_cost_by_product.items():
                self.conn.execute("UPDATE products SET cost_price=? WHERE id=?",(self._money(prior_cost),product_id))
            if purchase["order_id"] is not None:
                for line in lines:
                    line_id=line["purchase_order_line_id"]
                    if line_id is None:
                        continue
                    order_line=self.conn.execute("SELECT received_quantity FROM purchase_order_lines WHERE id=?",(line_id,)).fetchone()
                    if order_line:
                        remaining=Decimal(order_line["received_quantity"])-Decimal(line["quantity"])
                        if remaining<0:
                            raise OperationError("Purchase-order received quantity would become negative.")
                        self.conn.execute("UPDATE purchase_order_lines SET received_quantity=? WHERE id=?",(self._quantity(remaining),line_id))
                order_lines=self.purchase_order_lines(purchase["order_id"])
                complete=all(Decimal(row["received_quantity"])==Decimal(row["quantity"]) for row in order_lines)
                any_received=any(Decimal(row["received_quantity"])>0 for row in order_lines)
                order=self.conn.execute("SELECT order_number,status FROM purchase_orders WHERE id=?",(purchase["order_id"],)).fetchone()
                order_status=order["status"] if order["status"]=="Cancelled" else ("Received" if complete else ("Partially received" if any_received else "Ordered"))
                self.conn.execute("UPDATE purchase_orders SET status=?,updated_at=? WHERE id=?",(order_status,timestamp,purchase["order_id"]))
                self._enqueue("purchase_orders",int(purchase["order_id"]),{"id":int(purchase["order_id"]),"order_number":order["order_number"],"status":order_status},timestamp)
            self.conn.execute("UPDATE purchases SET status='Cancelled',cancelled_at=?,cancellation_reason=? WHERE id=?",
                              (timestamp,str(reason).strip()[:300],purchase_id))
            if refunded:
                self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at) VALUES('in',?,?,?,?,?)",
                                  (self._money(refunded),"purchase_refund",purchase_id,f"Refund for {purchase['purchase_number']}",timestamp))
            self._enqueue("purchases",purchase_id,{"id":purchase_id,"purchase_number":purchase["purchase_number"],"status":"Cancelled",
                "reason":str(reason).strip()[:300],"refund_amount":self._money(refunded)},timestamp)
        return True

    def _repair_event(self, job_id, event, details, timestamp):
        self.conn.execute("INSERT INTO repair_job_events(job_id,event,details,actor,created_at) VALUES(?,?,?,?,?)",
                          (int(job_id),str(event)[:80],str(details)[:500],self.actor_username,timestamp))

    def create_repair_job(self, customer_name, device, reported_fault, serial_number="", customer_id=None, warranty_notes="", work_kind="Repair", service_type="", received_date="", expected_collection_date="", product_id=None, service_notes=""):

        self._require_module("Repair Tickets")
        self._require("create")
        device=str(device or "").strip()[:160]
        fault=str(reported_fault or "").strip()[:4000]
        if not device or not fault:
            raise OperationError("Enter the device and the reported fault.")
        if customer_id is not None:
            customer=self.conn.execute("SELECT name FROM customers WHERE id=?",(int(customer_id),)).fetchone()
            if not customer:
                raise OperationError("The selected customer no longer exists.")
            customer_name=customer["name"]
        customer_name=str(customer_name or "").strip()[:160]
        if not customer_name:
            raise OperationError("Enter a customer name before creating the repair ticket.")
        work_kind=str(work_kind or "Repair").strip()
        if work_kind not in {"Repair", "Servicing"}:
            raise OperationError("Choose Repair or Servicing.")
        # Keep this field open-ended so new service types (e.g. OS installation,
        # router configuration, data recovery) can be recorded without a schema
        # change. The UI offers a standard searchable catalogue plus custom entry.
        service_type=str(service_type or "").strip()[:160]
        if work_kind == "Servicing" and not service_type:
            raise OperationError("Choose or enter the service requested.")
        received_date=str(received_date or datetime.now().strftime("%Y-%m-%d"))[:10]
        expected_collection_date=str(expected_collection_date or received_date)[:10]
        try:
            datetime.strptime(received_date, "%Y-%m-%d"); datetime.strptime(expected_collection_date, "%Y-%m-%d")
        except ValueError:
            raise OperationError("Dates must be valid calendar dates.")
        if expected_collection_date < received_date:
            raise OperationError("Collection date cannot be earlier than the received date.")
        if product_id is not None and not self.conn.execute("SELECT 1 FROM products WHERE id=? AND active=1", (int(product_id),)).fetchone():
            raise OperationError("The selected product is no longer available.")
        timestamp=self._stamp()
        with self._transaction():
            cur=self.conn.execute("INSERT INTO repair_jobs(ticket_number,customer_id,customer_name,device,serial_number,reported_fault,warranty_notes,status,created_by,created_at,updated_at,work_kind,service_type,received_date,expected_collection_date,product_id,service_notes) VALUES('',?,?,?,?,?,?,'Received',?,?,?,?,?,?,?,?,?)",
                (customer_id,customer_name,device,str(serial_number or "").strip()[:160],fault,str(warranty_notes or "").strip()[:1000],self.actor_username,timestamp,timestamp,work_kind,service_type,received_date,expected_collection_date,product_id,str(service_notes or "").strip()[:2000]))
            job_id=cur.lastrowid
            ticket=self._next_number("JOB",job_id)
            self.conn.execute("UPDATE repair_jobs SET ticket_number=? WHERE id=?",(ticket,job_id))
            self._repair_event(job_id,"Ticket received",f"{device} · {customer_name}",timestamp)
            self._enqueue("repair_jobs",job_id,{
                "id":job_id,"ticket_number":ticket,"customer_id":customer_id,"customer_name":customer_name,
                "device":device,"serial_number":str(serial_number or "").strip()[:160],"reported_fault":fault,
                "warranty_notes":str(warranty_notes or "").strip()[:1000],"status":"Received",
                "work_kind":work_kind,"service_type":service_type,"received_date":received_date,
                "expected_collection_date":expected_collection_date,"product_id":product_id,
                "service_notes":str(service_notes or "").strip()[:2000]
            },timestamp)
        return job_id

    def update_repair_diagnosis(self, job_id, diagnosis, technician, labor_amount, estimate_amount):
        self._require_module("Repair Tickets")
        self._require("edit")
        diagnosis=str(diagnosis or "").strip()[:4000]
        technician=str(technician or "").strip()[:160]
        if not diagnosis:
            raise OperationError("Enter a diagnosis before requesting customer approval.")
        labor=self._decimal(labor_amount,"Labor charge",minimum=Decimal("0"))
        estimate=self._decimal(estimate_amount,"Pre-tax estimate",minimum=Decimal("0"))
        timestamp=self._stamp()
        with self._transaction():
            job=self.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
            if not job or job["status"] not in {"Received","Diagnosing","Declined","In progress"}:
                raise OperationError("This repair ticket cannot be diagnosed in its current state.")
            parts=sum((Decimal(row[0]) for row in self.conn.execute("SELECT line_total FROM repair_job_parts WHERE job_id=?",(int(job_id),)).fetchall()),Decimal("0.00"))
            if labor+parts>estimate:
                raise OperationError("The estimate must cover labor and parts already issued.")
            self.conn.execute("UPDATE repair_jobs SET diagnosis=?,technician=?,labor_amount=?,estimate_amount=?,approval_status='Pending',status='Awaiting approval',updated_at=? WHERE id=?",
                (diagnosis,technician,self._money(labor),self._money(estimate),timestamp,int(job_id)))
            self._repair_event(job_id,"Estimate requested",f"Pre-tax estimate {self._money(estimate)} · technician {technician or 'Unassigned'}",timestamp)
            self._enqueue("repair_jobs",int(job_id),{"id":int(job_id),"diagnosis":diagnosis,"technician":technician,"labor_amount":self._money(labor),"estimate_amount":self._money(estimate),"approval_status":"Pending","status":"Awaiting approval"},timestamp)
        return True

    def approve_repair_estimate(self, job_id, approved, note=""):
        self._require_module("Repair Tickets")
        self._require("edit")
        timestamp=self._stamp()
        with self._transaction():
            job=self.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
            if not job or job["status"]!="Awaiting approval":
                raise OperationError("Only a repair awaiting customer approval can be accepted or declined.")
            approval="Approved" if bool(approved) else "Declined"
            status="In progress" if approved else "Declined"
            self.conn.execute("UPDATE repair_jobs SET approval_status=?,status=?,updated_at=? WHERE id=?",(approval,status,timestamp,int(job_id)))
            details=str(note or "").strip()[:400] or ("Customer approved the estimate." if approved else "Customer declined the estimate.")
            self._repair_event(job_id,"Estimate "+approval,details,timestamp)
            self._enqueue("repair_jobs",int(job_id),{"id":int(job_id),"approval_status":approval,"status":status},timestamp)
        return True

    def add_repair_part(self, job_id, product_id, quantity):
        self._require_module("Repair Tickets")
        self._require("manage_inventory")
        qty=self._decimal(quantity,"Part quantity",minimum=Decimal("0.001"),places=3)
        timestamp=self._stamp()
        with self._transaction():
            job=self.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
            if not job or job["status"]!="In progress" or job["approval_status"]!="Approved":
                raise OperationError("Parts can only be issued to an approved repair in progress.")
            product=self.conn.execute("SELECT * FROM products WHERE id=? AND active=1",(int(product_id),)).fetchone()
            if not product:
                raise OperationError("The selected part is no longer available.")
            stock=self.stock_on_hand(product_id)
            if stock<qty:
                raise OperationError(f"Not enough stock for {product['name']}. Available: {self._quantity(stock)} {product['unit']}.")
            price=Decimal(product["selling_price"])
            line_total=(qty*price).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
            parts_before=sum((Decimal(row[0]) for row in self.conn.execute("SELECT line_total FROM repair_job_parts WHERE job_id=?",(int(job_id),)).fetchall()),Decimal("0.00"))
            if Decimal(job["labor_amount"])+parts_before+line_total>Decimal(job["estimate_amount"]):
                raise OperationError("This part would exceed the customer-approved pre-tax estimate. Revise the estimate and obtain approval first.")
            part_id=self.conn.execute("INSERT INTO repair_job_parts(job_id,product_id,sku,description,quantity,unit_price,line_total,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (int(job_id),int(product_id),product["sku"],product["name"],self._quantity(qty),self._money(price),self._money(line_total),self.actor_username,timestamp)).lastrowid
            movement_id=self.conn.execute("INSERT INTO stock_movements(product_id,quantity_delta,movement_type,reference_type,reference_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (int(product_id),self._quantity(-qty),"repair_part","repair_job",int(job_id),job["ticket_number"],timestamp)).lastrowid
            self._repair_event(job_id,"Part issued",f"{product['sku']} · {self._quantity(qty)} · {self._money(line_total)}",timestamp)
            self._enqueue("repair_job_parts",part_id,{"id":part_id,"job_id":int(job_id),"product_id":int(product_id),"sku":product["sku"],"quantity":self._quantity(qty),"unit_price":self._money(price),"line_total":self._money(line_total)},timestamp)
            self._enqueue("stock_movements",movement_id,{"id":movement_id,"product_id":int(product_id),"sku":product["sku"],"quantity_delta":self._quantity(-qty),"reference_type":"repair_job","reference_id":int(job_id)},timestamp)
        return part_id

    def mark_repair_ready(self, job_id):
        self._require_module("Repair Tickets")
        self._require("edit")
        timestamp=self._stamp()
        with self._transaction():
            job=self.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
            if not job or job["status"]!="In progress" or job["approval_status"]!="Approved":
                raise OperationError("Only an approved repair in progress can be marked ready.")
            parts=sum((Decimal(row[0]) for row in self.conn.execute("SELECT line_total FROM repair_job_parts WHERE job_id=?",(int(job_id),)).fetchall()),Decimal("0.00"))
            subtotal=Decimal(job["labor_amount"])+parts
            tax_label,tax_rate=self._tax_config()
            tax_total=self._tax_amount(subtotal,tax_rate)
            total=subtotal+tax_total
            self.conn.execute("UPDATE repair_jobs SET subtotal=?,tax_label=?,tax_rate=?,tax_total=?,total=?,status='Ready',updated_at=? WHERE id=?",
                (self._money(subtotal),tax_label,self._money(tax_rate),self._money(tax_total),self._money(total),timestamp,int(job_id)))
            self._repair_event(job_id,"Repair ready",f"Total {self._money(total)} including configured tax",timestamp)
            self._enqueue("repair_jobs",int(job_id),{"id":int(job_id),"subtotal":self._money(subtotal),"tax_label":tax_label,"tax_rate":self._money(tax_rate),"tax_total":self._money(tax_total),"total":self._money(total),"status":"Ready"},timestamp)
        return True

    def record_repair_payment(self, job_id, amount, reference="", payment_method="Cash"):
        self._require_module("Repair Tickets")
        self._require("manage_finance")
        value=self._decimal(amount,"Payment",minimum=Decimal("0.01"))
        reference=str(reference or "").strip()[:120]
        payment_method=self._payment_method(payment_method)
        if payment_method=="M-Pesa" and not reference:
            raise OperationError("Enter the confirmed M-Pesa transaction code for this payment.")
        timestamp=self._stamp()
        with self._transaction():
            job=self.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
            if not job or job["status"]!="Ready":
                raise OperationError("Payments can be recorded only when the repair is ready for collection.")
            due=Decimal(job["total"])-Decimal(job["paid_total"])
            if value>due:
                raise OperationError("Payment cannot exceed the outstanding repair balance.")
            payment_id=self.conn.execute("INSERT INTO repair_job_payments(job_id,amount,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                (int(job_id),self._money(value),reference,self.actor_username,timestamp)).lastrowid
            paid=Decimal(job["paid_total"])+value
            self.conn.execute("UPDATE repair_jobs SET paid_total=?,updated_at=? WHERE id=?",(self._money(paid),timestamp,int(job_id)))
            cash_id=self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES('in',?,'repair_job',?,?,?,?,?,?)",
                (self._money(value),int(job_id),f"Repair payment {job['ticket_number']}",timestamp,payment_method,reference,"Repair income")).lastrowid
            self._repair_event(job_id,"Payment received",f"{self._money(value)} · {reference or 'No reference'}",timestamp)
            self._enqueue("repair_job_payments",payment_id,{"id":payment_id,"job_id":int(job_id),"amount":self._money(value),"reference":reference},timestamp)
            self._enqueue("cash_entries",cash_id,{"id":cash_id,"direction":"in","amount":self._money(value),"source_type":"repair_job","source_id":int(job_id),"payment_method":payment_method,"reference":reference,"category":"Repair income"},timestamp)
            self._enqueue("repair_jobs",int(job_id),{"id":int(job_id),"paid_total":self._money(paid)},timestamp)
        return payment_id

    def collect_repair_job(self, job_id):
        self._require_module("Repair Tickets")
        self._require("edit")
        timestamp=self._stamp()
        with self._transaction():
            job=self.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
            if not job or job["status"]!="Ready":
                raise OperationError("Only a ready repair can be collected.")
            if Decimal(job["paid_total"])<Decimal(job["total"]):
                raise OperationError("The repair must be paid in full before collection.")
            self.conn.execute("UPDATE repair_jobs SET status='Collected',updated_at=? WHERE id=?",(timestamp,int(job_id)))
            self._repair_event(job_id,"Device collected","Customer collected the repaired device.",timestamp)
            self._enqueue("repair_jobs",int(job_id),{"id":int(job_id),"status":"Collected"},timestamp)
        return True

    def cancel_repair_job(self, job_id, reason=""):
        self._require_module("Repair Tickets")
        self._require("delete")
        timestamp=self._stamp()
        with self._transaction():
            job=self.conn.execute("SELECT * FROM repair_jobs WHERE id=?",(int(job_id),)).fetchone()
            if not job or job["status"] not in {"Received","Diagnosing","Awaiting approval","Declined"}:
                raise OperationError("This repair can no longer be cancelled after work or parts issuance has started.")
            reason=str(reason or "").strip()[:300]
            self.conn.execute("UPDATE repair_jobs SET status='Cancelled',updated_at=? WHERE id=?",(timestamp,int(job_id)))
            self._repair_event(job_id,"Ticket cancelled",reason or "No reason supplied",timestamp)
            self._enqueue("repair_jobs",int(job_id),{"id":int(job_id),"status":"Cancelled","reason":reason},timestamp)
        return True

    def repair_jobs(self, limit=500, customer_id=None):
        self._require_module("Repair Tickets")
        if customer_id is not None:
            return self.conn.execute("SELECT * FROM repair_jobs WHERE customer_id=? ORDER BY id DESC LIMIT ?",(int(customer_id),limit)).fetchall()
        return self.conn.execute("SELECT * FROM repair_jobs ORDER BY id DESC LIMIT ?",(limit,)).fetchall()

    def repair_job_parts(self, job_id):
        self._require_module("Repair Tickets")
        return self.conn.execute("SELECT * FROM repair_job_parts WHERE job_id=? ORDER BY id",(int(job_id),)).fetchall()

    def repair_job_payments(self, job_id):
        self._require_module("Repair Tickets")
        return self.conn.execute("SELECT * FROM repair_job_payments WHERE job_id=? ORDER BY id",(int(job_id),)).fetchall()

    def repair_job_events(self, job_id):
        self._require_module("Repair Tickets")
        return self.conn.execute("SELECT * FROM repair_job_events WHERE job_id=? ORDER BY id",(int(job_id),)).fetchall()

    def record_cash_entry(self, direction, amount, description, payment_method="Cash", reference="", category="General"):
        """Record a cashbook event. This is a cash-movement ledger, not double-entry accounting."""
        self._require_module("Finance")
        self._require("manage_finance")
        if direction not in {"in","out"}:
            raise OperationError("Cash direction must be 'in' or 'out'.")
        value=self._decimal(amount,"Amount",minimum=Decimal("0.01"))
        description=str(description or "").strip()[:240]
        if not description:
            raise OperationError("Enter a description for the cash movement.")
        payment_method=self._payment_method(payment_method)
        reference=str(reference or "").strip()[:120]
        category=str(category or "General").strip()[:80] or "General"
        if payment_method == "M-Pesa" and not reference:
            raise OperationError("Enter the M-Pesa transaction code/reference so it can be reconciled against the statement.")
        timestamp=self._stamp()
        with self._transaction():
            cur=self.conn.execute("INSERT INTO cash_entries(direction,amount,source_type,source_id,description,created_at,payment_method,reference,category) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (direction,self._money(value),"manual",0,description,timestamp,payment_method,reference,category))
            payload={"id":cur.lastrowid,"direction":direction,"amount":self._money(value),"description":description,
                     "payment_method":payment_method,"reference":reference,"category":category}
            self._enqueue("cash_entries",cur.lastrowid,payload,timestamp)
        return cur.lastrowid

    def cash_balance(self):
        self._require_any_module(("Finance","Reports"))
        entries=self.cash_entries(limit=1000000)
        balance=Decimal("0")
        for entry in entries:
            amount=Decimal(entry["amount"])
            balance += amount if entry["direction"] == "in" else -amount
        return balance

    def inventory_value(self):
        self._require_any_module(("Inventory","Reports"))
        total=Decimal("0")
        for product in self.products(active_only=False):
            total += self.stock_on_hand(product["id"])*Decimal(product["cost_price"])
        return total.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)

    def set_invoice_date(self, sale_id, invoice_date):
        """Set the date shown on printed invoices (does not change system created_at).

        ``invoice_date`` may be YYYY-MM-DD or a full timestamp. Empty clears the override
        so the invoice falls back to the sale's recorded created_at.
        """
        self._require("edit")
        self._require_module("Sales")
        sale = self.conn.execute("SELECT id, status FROM sales WHERE id=?", (int(sale_id),)).fetchone()
        if not sale:
            raise OperationError("That sale no longer exists.")
        if sale["status"] == "Cancelled":
            raise OperationError("Cannot set an invoice date on a cancelled sale.")
        text = str(invoice_date or "").strip().replace("T", " ")
        if text:
            from datetime import datetime as _dt
            # Accept YYYY-MM-DD, YYYY-MM-DD HH:MM, or YYYY-MM-DD HH:MM:SS
            parsed = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    parsed = _dt.strptime(text[:19] if fmt.endswith("S") else text[:16] if "H" in fmt else text[:10], fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                raise OperationError("Invoice date must be a valid date and optional time (YYYY-MM-DD HH:MM).")
            # Store consistently as YYYY-MM-DD HH:MM (no seconds needed on the paper)
            text = parsed.strftime("%Y-%m-%d %H:%M")
        self.conn.execute("UPDATE sales SET invoice_date=? WHERE id=?", (text, int(sale_id)))
        self.conn.commit()
        return text

    def sales(self, limit=500):
        self._require_any_module(("Sales","Reports"))
        return self.conn.execute("SELECT * FROM sales ORDER BY id DESC LIMIT ?",(limit,)).fetchall()

    def purchases(self, limit=500):
        self._require_any_module(("Purchases","Reports"))
        return self.conn.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ?",(limit,)).fetchall()

    def cash_entries(self, limit=500, start_date=None, end_date=None, payment_method=None):
        self._require_any_module(("Finance","Reports"))
        where=[]; params=[]
        if start_date:
            where.append("substr(created_at,1,10)>=?"); params.append(self._iso_date(start_date,"Start date"))
        if end_date:
            where.append("substr(created_at,1,10)<=?"); params.append(self._iso_date(end_date,"End date"))
        if payment_method and payment_method != "All methods":
            method=self._payment_method(payment_method,allow_unspecified=True)
            where.append("payment_method=?"); params.append(method)
        query="SELECT * FROM cash_entries"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY id DESC LIMIT ?"
        params.append(max(1,min(int(limit),1000000)))
        return self.conn.execute(query,tuple(params)).fetchall()

    def cashflow_summary(self, start_date=None, end_date=None):
        """Summarize recorded receipts/payments by channel and category for a date range."""
        self._require_any_module(("Finance","Reports"))
        if start_date and end_date and self._iso_date(start_date,"Start date") > self._iso_date(end_date,"End date"):
            raise OperationError("Start date cannot be after end date.")
        rows=self.cash_entries(limit=1000000,start_date=start_date,end_date=end_date)
        total_in=Decimal("0.00"); total_out=Decimal("0.00")
        by_method={}; by_category={}
        for row in rows:
            amount=Decimal(row["amount"])
            direction=row["direction"]
            if direction=="in": total_in+=amount
            else: total_out+=amount
            method=row["payment_method"] or "Unspecified"
            category=(row["category"] or "").strip() or row["source_type"] or "Other"
            for key,bucket in ((method,by_method),(category,by_category)):
                item=bucket.setdefault(key,{"in":Decimal("0.00"),"out":Decimal("0.00"),"net":Decimal("0.00"),"count":0})
                item[direction]+=amount; item["net"]+=(amount if direction=="in" else -amount); item["count"]+=1
        return {"start_date":start_date or "","end_date":end_date or "","entries":rows,
                "money_in":total_in.quantize(Decimal("0.01")),"money_out":total_out.quantize(Decimal("0.01")),
                "net_movement":(total_in-total_out).quantize(Decimal("0.01")),
                "by_method":by_method,"by_category":by_category}

    def stock_movements(self, limit=500):
        self._require_any_module(("Inventory","Reports"))
        return _as_dicts(self.conn.execute("""SELECT m.*,p.sku,p.name,p.unit FROM stock_movements m
                                  JOIN products p ON p.id=m.product_id
                                  ORDER BY m.id DESC LIMIT ?""",(limit,)).fetchall())



    # ------------------------------------------------------------------ Advanced hybrid pillars
    def register_serial(self, product_id, serial_number, location_id="main", notes=""):
        from advanced_features import normalize_serial
        serial = normalize_serial(serial_number)
        if len(serial) < 3:
            raise OperationError("Serial / IMEI must be at least 3 characters.")
        timestamp = self._stamp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO serial_units(product_id,serial_number,status,location_id,notes,created_at,updated_at) VALUES(?,?, 'in_stock',?,?,?,?)",
                (int(product_id), serial, str(location_id or "main")[:40], str(notes)[:200], timestamp, timestamp),
            )
        return serial

    def list_serials(self, product_id=None, status=None, limit=200):
        sql = "SELECT s.*, p.sku, p.name FROM serial_units s JOIN products p ON p.id=s.product_id WHERE 1=1"
        params = []
        if product_id:
            sql += " AND s.product_id=?"; params.append(int(product_id))
        if status:
            sql += " AND s.status=?"; params.append(str(status))
        sql += " ORDER BY s.id DESC LIMIT ?"; params.append(int(limit))
        return self.conn.execute(sql, params).fetchall()

    def mark_serial_sold(self, product_id, serial_number, sale_id):
        from advanced_features import normalize_serial
        serial = normalize_serial(serial_number)
        timestamp = self._stamp()
        with self.conn:
            row = self.conn.execute(
                "SELECT id,status FROM serial_units WHERE product_id=? AND serial_number=? COLLATE NOCASE",
                (int(product_id), serial)).fetchone()
            if not row:
                # Allow ad-hoc serial capture at till for products that track serials
                self.conn.execute(
                    "INSERT INTO serial_units(product_id,serial_number,status,location_id,sale_id,notes,created_at,updated_at) VALUES(?,?, 'sold','main',?,?,?,?)",
                    (int(product_id), serial, int(sale_id), "Captured at POS", timestamp, timestamp))
                return
            if row["status"] != "in_stock":
                raise OperationError(f"Serial {serial} is not available (status: {row['status']}).")
            self.conn.execute(
                "UPDATE serial_units SET status='sold', sale_id=?, updated_at=? WHERE id=?",
                (int(sale_id), timestamp, row["id"]))

    def loyalty_balance(self, customer_name):
        name = str(customer_name or "").strip()
        if not name:
            return 0
        row = self.conn.execute(
            "SELECT points FROM loyalty_accounts WHERE customer_name=? COLLATE NOCASE", (name,)).fetchone()
        return int(row["points"]) if row else 0

    def loyalty_earn(self, customer_name, points, reason="Sale", source_type="sale", source_id=None):
        points = int(points)
        if points <= 0:
            return 0
        name = str(customer_name or "").strip() or "Walk-in Customer"
        if name.lower() == "walk-in customer":
            return 0
        timestamp = self._stamp()
        with self.conn:
            row = self.conn.execute(
                "SELECT id, points, lifetime_points FROM loyalty_accounts WHERE customer_name=? COLLATE NOCASE",
                (name,)).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE loyalty_accounts SET points=?, lifetime_points=?, updated_at=? WHERE id=?",
                    (int(row["points"]) + points, int(row["lifetime_points"]) + points, timestamp, row["id"]))
                account_id = row["id"]
            else:
                account_id = self.conn.execute(
                    "INSERT INTO loyalty_accounts(customer_id,customer_name,points,lifetime_points,updated_at) VALUES(NULL,?,?,?,?)",
                    (name, points, points, timestamp)).lastrowid
            self.conn.execute(
                "INSERT INTO loyalty_ledger(account_id,delta,reason,source_type,source_id,created_at) VALUES(?,?,?,?,?,?)",
                (account_id, points, str(reason)[:120], str(source_type)[:40], source_id, timestamp))
        return points
