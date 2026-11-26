"""Built-in industry blueprints for NextGen ERP.

Each business / institution profile defines:
- modules that should be enabled
- specialist workspaces (field packs)
- industry roles with default module access

Selecting a profile in setup or System Settings applies modules and seeds roles
so each user type only sees what they need.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProfileRole:
    name: str
    template: str  # Manager | Staff — capability baseline
    modules: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class BusinessProfile:
    name: str
    modules: tuple[str, ...]
    workspaces: dict
    roles: tuple[ProfileRole, ...] = ()
    pos_recommended: bool = False
    summary: str = ""


def _field(name, kind="text", required=False, options=()):
    return {"name": name, "type": kind, "required": required, "options": list(options)}


# ---------------------------------------------------------------------------
# Specialist workspace field packs
# ---------------------------------------------------------------------------
REPAIR_TICKETS = (
    _field("Customer", required=True), _field("Device", required=True),
    _field("Serial number"), _field("Reported fault", "notes", True),
    _field("Technician"), _field("Estimate (KES)", "number"),
    _field("Status", "choice", True, ("Received", "Diagnosing", "Awaiting approval", "In progress", "Ready", "Collected", "Cancelled")),
    _field("Warranty notes", "notes"),
)
APPOINTMENTS = (
    _field("Customer", required=True), _field("Service", required=True),
    _field("Staff member"), _field("Appointment date", "date", True),
    _field("Start time"),
    _field("Status", "choice", True, ("Booked", "Confirmed", "Arrived", "Completed", "No show", "Cancelled")),
    _field("Notes", "notes"),
)
MENU_ITEMS = (
    _field("Item name", required=True), _field("Category"),
    _field("Selling price (KES)", "number", True), _field("Recipe / ingredients", "notes"),
    _field("Status", "choice", True, ("Available", "Unavailable", "Seasonal")),
)
BATCHES = (
    _field("Product / medicine", required=True), _field("Batch number", required=True),
    _field("Quantity", "number", True), _field("Expiry date", "date", True),
    _field("Supplier"),
    _field("Status", "choice", True, ("In stock", "Quarantined", "Expired", "Disposed")),
)
STUDENTS = (
    _field("Student name", required=True), _field("Admission number", required=True),
    _field("Class / course"), _field("Guardian"), _field("Guardian contact"),
    _field("Enrolment date", "date"),
    _field("Status", "choice", True, ("Active", "On leave", "Completed", "Withdrawn")),
)
FEES = (
    _field("Student / admission no.", required=True), _field("Fee type", required=True),
    _field("Term / period"), _field("Amount (KES)", "number", True),
    _field("Due date", "date"),
    _field("Status", "choice", True, ("Pending", "Partial", "Paid", "Waived", "Overdue")),
    _field("Notes", "notes"),
)
CLASSES = (
    _field("Class / stream name", required=True), _field("Level / form"),
    _field("Class teacher"), _field("Room"),
    _field("Capacity", "number"),
    _field("Status", "choice", True, ("Active", "Archived")),
)
LIBRARY = (
    _field("Title", required=True), _field("Author"), _field("ISBN"),
    _field("Category"), _field("Copies", "number"),
    _field("Status", "choice", True, ("Available", "On loan", "Lost", "Archived")),
)
JOB_CARDS = (
    _field("Client", required=True), _field("Job title", required=True),
    _field("Site / location"), _field("Supervisor"),
    _field("Start date", "date"), _field("Due date", "date"),
    _field("Status", "choice", True, ("Quoted", "Scheduled", "In progress", "Completed", "Invoiced", "Cancelled")),
    _field("Notes", "notes"),
)
FLEET = (
    _field("Vehicle / asset", required=True), _field("Reg. number"),
    _field("Driver"), _field("Service type"),
    _field("Service date", "date"), _field("Next service", "date"),
    _field("Cost (KES)", "number"),
    _field("Status", "choice", True, ("Due", "Booked", "Done", "Overdue")),
)

# ---------------------------------------------------------------------------
# Core module catalogue
# ---------------------------------------------------------------------------
CORE_MODULES = (
    "Sales", "Point of Sale", "Returns Desk", "Purchases", "Inventory",
    "Finance", "HR & Payroll", "CRM", "Projects", "Reports",
)

MODULE_DEPENDENCIES = {
    "Sales": ("Inventory", "Finance"),
    "Purchases": ("Inventory", "Finance"),
    "Batches & Expiry": ("Inventory",),
    "Menu & Recipes": ("Inventory", "Sales", "Purchases"),
    "Repair Tickets": ("Inventory", "Sales", "CRM"),
    "Appointments": ("CRM",),
    "Students & Enrolment": ("CRM",),
    "Fees & Billing": ("Finance", "Students & Enrolment"),
    "Classes & Timetable": ("Students & Enrolment",),
    "Library": ("Students & Enrolment",),
    "Point of Sale": ("Sales",),
    "Returns Desk": ("Sales",),
    "Job Cards": ("Projects", "CRM"),
    "Fleet & Assets": ("Projects",),
}


def missing_dependencies(enabled_modules):
    enabled = set(enabled_modules)
    missing = set()
    pending = list(enabled)
    checked = set()
    while pending:
        module = pending.pop()
        if module in checked:
            continue
        checked.add(module)
        for dependency in MODULE_DEPENDENCIES.get(module, ()):
            if dependency not in enabled:
                missing.add((module, dependency))
            else:
                pending.append(dependency)
    return sorted(missing)


# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------
def _role(name, template, modules, description=""):
    return ProfileRole(name=name, template=template, modules=tuple(modules), description=description)


# Shared retail-ish module sets
_RETAIL = ("Sales", "Point of Sale", "Returns Desk", "Purchases", "Inventory", "Finance", "CRM", "Reports")
_RETAIL_HR = _RETAIL + ("HR & Payroll",)
_REPAIR = ("Sales", "Point of Sale", "Returns Desk", "Purchases", "Inventory", "Finance", "CRM", "Reports", "Repair Tickets")
_SCHOOL = (
    "Finance", "HR & Payroll", "CRM", "Projects", "Reports",
    "Students & Enrolment", "Fees & Billing", "Classes & Timetable", "Library",
)

ALL_MODULES = CORE_MODULES

# ---------------------------------------------------------------------------
# Industry profiles
# ---------------------------------------------------------------------------
BUSINESS_PROFILES = {
    "General business": BusinessProfile(
        "General business",
        ALL_MODULES,
        {},
        roles=(
            _role("Manager", "Manager", ALL_MODULES, "Runs day-to-day operations"),
            _role("Staff", "Staff", ("Sales", "Point of Sale", "Returns Desk", "Inventory", "CRM"), "Floor, till and data entry"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Sales", "Purchases"), "Books and compliance"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales", "Returns Desk"), "Till only"),
        ),
        pos_recommended=True,
        summary="Balanced pack for mixed trading businesses.",
    ),
    "Supermarket / minimart": BusinessProfile(
        "Supermarket / minimart",
        _RETAIL + ("Batches & Expiry", "HR & Payroll"),
        {"Batches & Expiry": BATCHES},
        roles=(
            _role("Store Manager", "Manager", _RETAIL + ("Batches & Expiry", "HR & Payroll"), "Full store control"),
            _role("Supervisor", "Manager", ("Sales", "Point of Sale", "Returns Desk", "Inventory", "Reports"), "Floor supervision"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales", "Returns Desk"), "Checkout"),
            _role("Stock Clerk", "Staff", ("Inventory", "Purchases", "Batches & Expiry"), "Receiving and shelves"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Purchases", "Sales"), "Cash-up and books"),
        ),
        pos_recommended=True,
        summary="High-volume retail with batches, cashiers and stock control.",
    ),
    "Computers and Electronics Sales and Repairs": BusinessProfile(
        "Computers and Electronics Sales and Repairs",
        _REPAIR + ("HR & Payroll", "Projects"),
        {"Repair Tickets": REPAIR_TICKETS},
        roles=(
            _role("Service Manager", "Manager", _REPAIR + ("HR & Payroll", "Projects"), "Workshop and counter"),
            _role("Technician", "Staff", ("Repair Tickets", "Inventory", "CRM"), "Bench work"),
            _role("Front Desk", "Staff", ("Sales", "Point of Sale", "CRM", "Repair Tickets"), "Customer intake"),
            _role("Parts Clerk", "Staff", ("Inventory", "Purchases"), "Spares"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales", "Returns Desk"), "Payments"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Sales", "Purchases"), "Accounts"),
        ),
        pos_recommended=True,
        summary="Retail + workshop with serials, repairs and parts.",
    ),
    "Electronics retail": BusinessProfile(
        "Electronics retail",
        _RETAIL + ("HR & Payroll",),
        {},
        roles=(
            _role("Store Manager", "Manager", _RETAIL_HR, "Store lead"),
            _role("Sales Associate", "Staff", ("Sales", "Point of Sale", "Inventory", "CRM"), "Selling floor"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales", "Returns Desk"), "Till"),
            _role("Stock Clerk", "Staff", ("Inventory", "Purchases"), "Warehouse"),
            _role("Accountant", "Manager", ("Finance", "Reports"), "Finance"),
        ),
        pos_recommended=True,
        summary="Electronics counter sales and inventory.",
    ),
    "General retail shop": BusinessProfile(
        "General retail shop",
        _RETAIL,
        {},
        roles=(
            _role("Manager", "Manager", _RETAIL, "Owner/manager"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales", "Returns Desk"), "Till"),
            _role("Shop Assistant", "Staff", ("Sales", "Inventory", "CRM"), "Floor"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Purchases"), "Books"),
        ),
        pos_recommended=True,
        summary="Classic retail shop pack.",
    ),
    "Wholesale / distribution": BusinessProfile(
        "Wholesale / distribution",
        ("Sales", "Purchases", "Inventory", "Finance", "CRM", "Reports", "HR & Payroll", "Projects"),
        {},
        roles=(
            _role("Operations Manager", "Manager", ("Sales", "Purchases", "Inventory", "Finance", "CRM", "Reports", "Projects"), "Ops"),
            _role("Sales Rep", "Staff", ("Sales", "CRM", "Inventory"), "B2B selling"),
            _role("Warehouse Clerk", "Staff", ("Inventory", "Purchases"), "Dispatch"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Sales", "Purchases"), "Credit control"),
        ),
        pos_recommended=False,
        summary="Bulk trade, credit customers and warehouse.",
    ),
    "Restaurant / café": BusinessProfile(
        "Restaurant / café",
        ("Sales", "Point of Sale", "Purchases", "Inventory", "Finance", "Reports", "Menu & Recipes", "HR & Payroll"),
        {"Menu & Recipes": MENU_ITEMS},
        roles=(
            _role("Restaurant Manager", "Manager", ("Sales", "Point of Sale", "Purchases", "Inventory", "Finance", "Reports", "Menu & Recipes", "HR & Payroll"), "Full house"),
            _role("Head Chef", "Manager", ("Menu & Recipes", "Inventory", "Purchases"), "Kitchen"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales"), "Counter"),
            _role("Waiter", "Staff", ("Point of Sale", "Sales", "CRM"), "Service"),
            _role("Accountant", "Manager", ("Finance", "Reports"), "Books"),
        ),
        pos_recommended=True,
        summary="F&B with recipes, till and kitchen stock.",
    ),
    "Pharmacy": BusinessProfile(
        "Pharmacy",
        _RETAIL + ("Batches & Expiry", "HR & Payroll"),
        {"Batches & Expiry": BATCHES},
        roles=(
            _role("Pharmacist in Charge", "Manager", _RETAIL + ("Batches & Expiry", "HR & Payroll"), "Responsible pharmacist"),
            _role("Pharmacy Assistant", "Staff", ("Point of Sale", "Sales", "Inventory", "Batches & Expiry"), "Dispensing support"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales", "Returns Desk"), "Till"),
            _role("Stock Clerk", "Staff", ("Inventory", "Purchases", "Batches & Expiry"), "Receiving"),
            _role("Accountant", "Manager", ("Finance", "Reports"), "Finance"),
        ),
        pos_recommended=True,
        summary="Regulated retail with batch and expiry control.",
    ),
    "Hardware / building supplies": BusinessProfile(
        "Hardware / building supplies",
        _RETAIL + ("HR & Payroll", "Projects"),
        {},
        roles=(
            _role("Yard Manager", "Manager", _RETAIL_HR + ("Projects",), "Yard and counter"),
            _role("Counter Sales", "Staff", ("Sales", "Point of Sale", "Inventory", "CRM"), "Trade counter"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales", "Returns Desk"), "Till"),
            _role("Storeman", "Staff", ("Inventory", "Purchases"), "Stores"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Sales"), "Accounts"),
        ),
        pos_recommended=True,
        summary="Building materials and trade sales.",
    ),
    "Salon / barbershop": BusinessProfile(
        "Salon / barbershop",
        ("Sales", "Point of Sale", "Inventory", "Finance", "CRM", "HR & Payroll", "Reports", "Appointments"),
        {"Appointments": APPOINTMENTS},
        roles=(
            _role("Salon Manager", "Manager", ("Sales", "Point of Sale", "Inventory", "Finance", "CRM", "HR & Payroll", "Reports", "Appointments"), "Full salon"),
            _role("Stylist", "Staff", ("Appointments", "CRM", "Sales"), "Services"),
            _role("Receptionist", "Staff", ("Appointments", "Point of Sale", "CRM", "Sales"), "Front desk"),
            _role("Cashier", "Staff", ("Point of Sale", "Sales"), "Payments"),
        ),
        pos_recommended=True,
        summary="Appointments, services and retail products.",
    ),
    "Professional services": BusinessProfile(
        "Professional services",
        ("Sales", "Finance", "CRM", "Projects", "Reports", "HR & Payroll", "Job Cards"),
        {"Job Cards": JOB_CARDS},
        roles=(
            _role("Partner / Director", "Manager", ("Sales", "Finance", "CRM", "Projects", "Reports", "HR & Payroll", "Job Cards"), "Leadership"),
            _role("Project Lead", "Manager", ("Projects", "Job Cards", "CRM", "Sales"), "Delivery"),
            _role("Consultant", "Staff", ("Projects", "Job Cards", "CRM"), "Billable work"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Sales"), "Billing"),
            _role("Admin", "Staff", ("CRM", "Projects"), "Coordination"),
        ),
        pos_recommended=False,
        summary="Firms, agencies and consultancies.",
    ),
    "Repair / service centre": BusinessProfile(
        "Repair / service centre",
        _REPAIR + ("Projects", "HR & Payroll", "Job Cards"),
        {"Repair Tickets": REPAIR_TICKETS, "Job Cards": JOB_CARDS},
        roles=(
            _role("Centre Manager", "Manager", _REPAIR + ("Projects", "HR & Payroll", "Job Cards"), "Full centre"),
            _role("Lead Technician", "Manager", ("Repair Tickets", "Inventory", "Job Cards"), "Workshop lead"),
            _role("Technician", "Staff", ("Repair Tickets", "Inventory"), "Repairs"),
            _role("Front Desk", "Staff", ("CRM", "Repair Tickets", "Sales", "Point of Sale"), "Intake"),
            _role("Parts Clerk", "Staff", ("Inventory", "Purchases"), "Spares"),
            _role("Accountant", "Manager", ("Finance", "Reports"), "Accounts"),
        ),
        pos_recommended=True,
        summary="Dedicated service workshop.",
    ),
    "School / training centre": BusinessProfile(
        "School / training centre",
        _SCHOOL,
        {
            "Students & Enrolment": STUDENTS,
            "Fees & Billing": FEES,
            "Classes & Timetable": CLASSES,
            "Library": LIBRARY,
        },
        roles=(
            _role("Principal", "Manager", _SCHOOL, "Institution head — full academic and finance oversight"),
            _role("Deputy Principal", "Manager", ("Students & Enrolment", "Classes & Timetable", "HR & Payroll", "Reports", "CRM", "Projects"), "Academic and discipline"),
            _role("Head of Department", "Manager", ("Classes & Timetable", "Students & Enrolment", "Reports", "Projects"), "Departmental academic lead"),
            _role("Finance Officer", "Manager", ("Finance", "Fees & Billing", "Reports", "Purchases"), "Fees, budgets and suppliers"),
            _role("Registrar", "Staff", ("Students & Enrolment", "Classes & Timetable", "CRM"), "Admissions and records"),
            _role("Teacher", "Staff", ("Classes & Timetable", "Students & Enrolment"), "Class teacher access"),
            _role("Librarian", "Staff", ("Library", "Students & Enrolment"), "Library desk"),
            _role("Accountant", "Manager", ("Finance", "Fees & Billing", "Reports"), "Accounts"),
            _role("HR Officer", "Manager", ("HR & Payroll", "Reports"), "Staff records and payroll"),
        ),
        pos_recommended=False,
        summary="Schools and training centres with learners, fees, classes and library.",
    ),
    "Clinic / medical practice": BusinessProfile(
        "Clinic / medical practice",
        ("Sales", "Point of Sale", "Inventory", "Finance", "CRM", "HR & Payroll", "Reports", "Appointments", "Batches & Expiry"),
        {"Appointments": APPOINTMENTS, "Batches & Expiry": BATCHES},
        roles=(
            _role("Clinic Manager", "Manager", ("Sales", "Point of Sale", "Inventory", "Finance", "CRM", "HR & Payroll", "Reports", "Appointments", "Batches & Expiry"), "Full clinic"),
            _role("Doctor", "Staff", ("Appointments", "CRM"), "Consultations"),
            _role("Nurse", "Staff", ("Appointments", "CRM", "Inventory"), "Care support"),
            _role("Receptionist", "Staff", ("Appointments", "CRM", "Point of Sale", "Sales"), "Front desk"),
            _role("Pharmacist", "Staff", ("Point of Sale", "Inventory", "Batches & Expiry"), "Dispensary"),
            _role("Accountant", "Manager", ("Finance", "Reports"), "Billing"),
        ),
        pos_recommended=True,
        summary="Outpatient clinic with appointments and dispensary.",
    ),
    "Logistics / transport": BusinessProfile(
        "Logistics / transport",
        ("Sales", "Finance", "CRM", "Projects", "Reports", "HR & Payroll", "Fleet & Assets", "Job Cards"),
        {"Fleet & Assets": FLEET, "Job Cards": JOB_CARDS},
        roles=(
            _role("Operations Manager", "Manager", ("Sales", "Finance", "CRM", "Projects", "Reports", "HR & Payroll", "Fleet & Assets", "Job Cards"), "Ops"),
            _role("Dispatcher", "Staff", ("Fleet & Assets", "Job Cards", "CRM"), "Trips"),
            _role("Driver", "Staff", ("Fleet & Assets", "Job Cards"), "Assigned trips"),
            _role("Accountant", "Manager", ("Finance", "Reports", "Sales"), "Billing"),
        ),
        pos_recommended=False,
        summary="Fleet, jobs and billing for transport operators.",
    ),
    "Custom business": BusinessProfile(
        "Custom business",
        ALL_MODULES,
        {},
        roles=(
            _role("Manager", "Manager", ALL_MODULES, "Full access"),
            _role("Staff", "Staff", ("Sales", "Point of Sale", "Returns Desk", "Inventory", "CRM"), "Floor and till access"),
        ),
        pos_recommended=False,
        summary="Start from core modules and customise.",
    ),
}


def get_profile(name: str) -> BusinessProfile:
    return BUSINESS_PROFILES.get(name, BUSINESS_PROFILES["General business"])


def profile_role_names(profile_name: str) -> list[str]:
    profile = get_profile(profile_name)
    return [role.name for role in profile.roles]


def profile_role_module_map(profile_name: str) -> dict[str, list[str]]:
    profile = get_profile(profile_name)
    return {role.name: list(role.modules) for role in profile.roles}


WORKSPACE_NAV_ORDER = (
    "Point of Sale",
    "Returns Desk",
    "Sales", "Purchases", "Inventory", "Finance", "CRM",
    "Repair Tickets", "Reports", "HR & Payroll", "Projects",
    "Students & Enrolment", "Fees & Billing", "Classes & Timetable", "Library",
    "Appointments", "Batches & Expiry", "Menu & Recipes",
    "Job Cards", "Fleet & Assets",
)


def workspace_navigation_names(enabled_modules, pos_enabled=True, custom_modules=()):
    """Return visible operational navigation without exposing disabled modules."""
    enabled = set(enabled_modules)
    names = ["Dashboard"]
    for name in WORKSPACE_NAV_ORDER:
        if name not in enabled:
            continue
        if name == "Point of Sale" and not pos_enabled:
            continue
        names.append(name)
    names.extend(name for name in custom_modules if name not in names and name not in WORKSPACE_NAV_ORDER)
    return names
