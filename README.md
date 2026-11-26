# NextGen ERP Desktop — Finalized Build

Windows desktop ERP for New Age Systems. Python + PySide6 + SQLite + ReportLab.
Offline-first. Not a website.

## Start
1. Extract this ZIP to a writable folder.
2. Run `START_ERP.bat`.
3. First launch may install packages from `requirements.txt` (PySide6, reportlab).

## What this build tightens
- **Brand-matched SKUs** — new codes use `BRAND-CATEGORY-####` (e.g. `DELL-LAP-0001`). All Dell stock starts with DELL and sorts together. Batch SKU labels print in brand then SKU order.
- **Tables size to content** — column widths follow content instead of stretching everything into thin squeezed cells; horizontal scroll appears when needed.
- **Inventory catalogue** — primary columns are SKU, Product, Brand, Category, On hand, Sell, Cost, Reorder, Active. Model sits under the product name. Variant / unit-conversion fields stay in the data model but stay off the main electronics product form.
- **Batch SKU labels** — Inventory → Batch SKU labels · PDF / CSV. Select products, set copies, print or export. Code 128 encodes the internal SKU for POS scan lookup.

## Still available
- POS, sales, purchases, repairs, CRM, finance, roles, database export/import.
- Read-only website catalogue mirror (Administration → Integrations) if you want it; it never writes the live site.

## Data safety
Use **Database & Backup** for full package export/import. See `SECURITY_AND_DATA_PORTABILITY.md`.


## Retail Super Upgrade (development branch)

- Customer credit facilities: Admin can enable credit per registered customer, set an individual KES limit and repayment terms. POS credit sales are blocked for walk-ins, disabled facilities and limit overruns.
- Customer negotiated pricing: Admin/Manager can double-click a cart item's unit-price cell to override its price. A reason is required and recorded in the audit events. Catalogue price is unchanged.
- Audit events: customer credit configuration, cart overrides, sales, repayments and returns are recorded with actor, role, timestamp and detail. Admin can view the audit log from Customers/CRM.
- Returns: audit event captures return/refund amount and reason. Existing return processing can restore stock and refund through a chosen payment method.
- Credit due dates are stored for sales based on customer terms where enabled.
- Theme switch preserves loaded page widgets to reduce rebuilding and visual lag.
- Project SVG logo is in `assets/nextgen-erp-mark.svg` and is set as the desktop application icon where Qt supports SVG icons.
- Windows launcher creates a local `.venv`, so unrelated global Python packages and their dependency warnings do not affect the ERP.

**Development branch:** `feature/retail-super-upgrades`. This is not yet merged to `main`; test on a copied database before adopting it.


## Product Lending Centre
- Temporary product issues are tracked separately from POS credit sales.
- Each loan records the registered customer, product, quantity, issue/due dates, condition, serial/IMEI where applicable, issuing employee and approval identity.
- Staff lending authority is configured per employee in **User Management** and applies per transaction. A transaction above that authority requires a different active Manager/Admin account and password verification.
- The customer's existing credit facility is the shared exposure ceiling across unpaid sales and active product loans. A manager override does not bypass the customer's credit limit.
- Partial repayments are recorded to the cashbook. Full repayment marks the lending obligation as settled while the product remains with the customer; receiving the product back closes the return side of the loan.
- A good-condition return that clears an unpaid balance requires Manager/Admin approval. Damaged or incomplete returns are recorded without returning the item to sellable stock; any remaining balance stays on the customer's account.
- Loan issue/return stock movements, payments, approvals and audit events are stored locally and queued for sync when the system's sync service is configured.


## Guided first-run experience
- After the first successful sign-in, the desktop app opens a step-by-step walkthrough of enabled business workspaces, with role-aware guidance.
- The tour can be skipped immediately and replayed at any time from **Guided tour** in the top bar.
- Tour completion is stored locally; closing the tour without choosing Skip leaves it available on the next launch.

## Product Lending Centre experience
- Searchable lending register with status filters, exposure KPIs, selected-agreement details and an activity timeline.
- Live issue preview displays the customer's shared available credit and the current employee's per-transaction authority before confirmation.
- Repayment and return dialogs explain financial and stock consequences before saving.


## Offline Update Centre (development branch)

Open **System Settings → Update Centre** to import a full-update ZIP. The centre validates the manifest and SHA-256 hashes, shows the file list and release notes, stages the update, backs up replaced files under `%APPDATA%\\NextGenERP\\update_backups`, then closes the app and applies the package using a Windows helper script.

Package layout:

```
update.zip
├── update_manifest.json
├── release_notes.md (optional)
└── payload/
    ├── main.py
    ├── update_centre.py
    └── assets/...
```

Manifest example:

```json
{
  "package_type": "full_update",
  "version": "2.0.1",
  "files": {
    "main.py": "<sha256 of payload/main.py>",
    "update_centre.py": "<sha256 of payload/update_centre.py>"
  }
}
```

Only application files are permitted; database and user data paths are explicitly excluded. This first version supports **offline full application packages only** (not online update checks or independent module updates). Hashes detect package corruption, but do not authenticate who produced a package—only install packages from a source you trust. The updater currently performs rollback if file-copy installation fails; post-launch health-check rollback and signed packages are planned hardening work.


## Experience and performance

The ongoing Experience Upgrade prioritizes a premium interface without sacrificing speed. Decorative motion and continuous effects are off by default; see [PERFORMANCE_AND_UX.md](PERFORMANCE_AND_UX.md) for performance budgets and UX principles.
