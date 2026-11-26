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
