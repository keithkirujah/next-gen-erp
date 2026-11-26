"""Report centre data: every report is (title, headers, rows) so it can be shown, exported to CSV and printed.

All queries run on the business workspace connection and honour the chosen date range
(``start``/``end`` are ISO dates, inclusive). Money is summed with Decimal.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


def _d(v) -> Decimal:
    try:
        return Decimal(str(v or "0"))
    except Exception:
        return Decimal("0")


def _n(v) -> str:
    """Quantity without trailing zeros: Decimal('1.000') -> '1', Decimal('2.50') -> '2.5'."""
    v = _d(v)
    text = format(v.normalize(), "f")
    return text

def fmt(v) -> str:
    return f"{_d(v):,.2f}"


@dataclass
class Report:
    title: str
    headers: list
    rows: list
    footer: list | None = None     # optional totals row


REPORT_NAMES = [
    ("sales_by_day", "Sales by day"),
    ("sales_by_product", "Sales by product (best sellers)"),
    ("payment_methods", "Takings by payment method"),
    ("tax_summary", "Tax collected"),
    ("profit_by_product", "Gross profit by product"),
    ("aged_debtors", "Unpaid invoices (aged debtors)"),
    ("stock_valuation", "Stock valuation"),
    ("low_stock", "Low stock / reorder list"),
    ("money_out", "Money out by category (expenses & refunds)"),
    ("shift_history", "Shifts & cash variance"),
]


def build(conn, key: str, start: str, end: str) -> Report:
    fn = globals().get("_r_" + key)
    if fn is None:
        raise ValueError(f"Unknown report: {key}")
    return fn(conn, start, end)


def _sales(conn, start, end):
    return conn.execute("SELECT * FROM sales WHERE status='Completed' AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=? ORDER BY id",
                        (start, end)).fetchall()


def _r_sales_by_day(conn, start, end):
    days = defaultdict(lambda: [0, Decimal("0"), Decimal("0")])
    for s in _sales(conn, start, end):
        row = days[s["created_at"][:10]]
        row[0] += 1; row[1] += _d(s["total"]); row[2] += _d(s["tax_total"])
    rows = [[d, c, fmt(t), fmt(tax)] for d, (c, t, tax) in sorted(days.items())]
    footer = ["TOTAL", sum(v[0] for v in days.values()), fmt(sum((v[1] for v in days.values()), Decimal("0"))), fmt(sum((v[2] for v in days.values()), Decimal("0")))]
    return Report("Sales by day", ["Date", "Sales", "Total", "Tax included/added"], rows, footer)


def _lines(conn, start, end):
    return conn.execute(
        """SELECT l.sku,l.description,l.quantity,l.line_total,l.tax_amount,l.product_id FROM sale_lines l JOIN sales s ON s.id=l.sale_id
           WHERE s.status='Completed' AND substr(s.created_at,1,10)>=? AND substr(s.created_at,1,10)<=?""", (start, end)).fetchall()


def _r_sales_by_product(conn, start, end):
    agg = defaultdict(lambda: [Decimal("0"), Decimal("0"), ""])
    for l in _lines(conn, start, end):
        a = agg[l["sku"]]
        a[0] += _d(l["quantity"]); a[1] += _d(l["line_total"]) + _d(l["tax_amount"]); a[2] = l["description"]
    ranked = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)
    rows = [[sku, v[2], _n(v[0]), fmt(v[1])] for sku, v in ranked]
    return Report("Sales by product", ["SKU", "Product", "Qty sold", "Revenue"], rows,
                  ["TOTAL", "", _n(sum((v[0] for v in agg.values()), Decimal('0'))), fmt(sum((v[1] for v in agg.values()), Decimal('0')))])


def _r_payment_methods(conn, start, end):
    agg = defaultdict(lambda: [0, Decimal("0")])
    for r in conn.execute("SELECT payment_method,amount FROM cash_entries WHERE direction='in' AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?", (start, end)):
        agg[r["payment_method"]][0] += 1; agg[r["payment_method"]][1] += _d(r["amount"])
    rows = [[k, v[0], fmt(v[1])] for k, v in sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)]
    return Report("Takings by payment method", ["Method", "Payments", "Amount"], rows,
                  ["TOTAL", sum(v[0] for v in agg.values()), fmt(sum((v[1] for v in agg.values()), Decimal("0")))])


def _r_tax_summary(conn, start, end):
    agg = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    for s in _sales(conn, start, end):
        label = f"{s['tax_label'] or 'Tax'} {s['tax_rate']}%" if _d(s["tax_rate"]) > 0 else "No tax"
        agg[label][0] += _d(s["subtotal"]); agg[label][1] += _d(s["tax_total"])
    rows = [[k, fmt(v[0]), fmt(v[1]), fmt(v[0] + v[1])] for k, v in sorted(agg.items())]
    return Report("Tax collected", ["Tax", "Net sales", "Tax amount", "Gross"], rows,
                  ["TOTAL", fmt(sum((v[0] for v in agg.values()), Decimal("0"))), fmt(sum((v[1] for v in agg.values()), Decimal("0"))),
                   fmt(sum((v[0] + v[1] for v in agg.values()), Decimal("0")))])


def _r_profit_by_product(conn, start, end):
    costs = {r["id"]: _d(r["cost_price"]) for r in conn.execute("SELECT id,cost_price FROM products")}
    agg = defaultdict(lambda: [Decimal("0"), Decimal("0"), Decimal("0"), ""])
    for l in _lines(conn, start, end):
        a = agg[l["sku"]]
        qty = _d(l["quantity"]); a[0] += qty; a[1] += _d(l["line_total"]); a[2] += qty * costs.get(l["product_id"], Decimal("0")); a[3] = l["description"]
    rows = []
    for sku, (qty, revenue, cost, name) in sorted(agg.items(), key=lambda kv: kv[1][1] - kv[1][2], reverse=True):
        profit = revenue - cost
        rows.append([sku, name, _n(qty), fmt(revenue), fmt(cost), fmt(profit), f"{(profit / revenue * 100) if revenue else 0:.0f}%"])
    tr = sum((v[1] for v in agg.values()), Decimal("0")); tc = sum((v[2] for v in agg.values()), Decimal("0"))
    return Report("Gross profit by product (net of tax)", ["SKU", "Product", "Qty", "Net revenue", "Cost", "Profit", "Margin"], rows,
                  ["TOTAL", "", "", fmt(tr), fmt(tc), fmt(tr - tc), f"{((tr - tc) / tr * 100) if tr else 0:.0f}%"])


def _r_aged_debtors(conn, start, end):
    today = date.today()
    rows, total = [], Decimal("0")
    for s in conn.execute("SELECT * FROM sales WHERE status='Completed' AND payment_status IN ('Unpaid','Partial') ORDER BY created_at"):
        owing = _d(s["total"]) - _d(s["paid_total"])
        try:
            age = (today - datetime.strptime(s["created_at"][:10], "%Y-%m-%d").date()).days
        except ValueError:
            age = 0
        bucket = "0-30 days" if age <= 30 else "31-60 days" if age <= 60 else "61-90 days" if age <= 90 else "90+ days"
        rows.append([s["sale_number"], s["customer_name"], s["created_at"][:10], fmt(s["total"]), fmt(owing), age, bucket])
        total += owing
    return Report("Unpaid invoices (all dates)", ["Invoice", "Customer", "Date", "Total", "Owing", "Age (days)", "Bucket"], rows, ["TOTAL OWING", "", "", "", fmt(total), "", ""])


def _stock(conn):
    out = []
    for p in conn.execute("SELECT * FROM products WHERE active=1 ORDER BY name"):
        qty = _d(conn.execute("SELECT COALESCE(SUM(CAST(quantity_delta AS REAL)),0) q FROM stock_movements WHERE product_id=?", (p["id"],)).fetchone()["q"])
        out.append((p, qty))
    return out


def _r_stock_valuation(conn, start, end):
    rows, total_cost, total_retail = [], Decimal("0"), Decimal("0")
    for p, qty in _stock(conn):
        if not p["track_stock"]:
            continue
        cost, retail = qty * _d(p["cost_price"]), qty * _d(p["selling_price"])
        rows.append([p["sku"], p["name"], _n(qty), fmt(p["cost_price"]), fmt(cost), fmt(retail)])
        total_cost += cost; total_retail += retail
    return Report("Stock valuation (today)", ["SKU", "Product", "On hand", "Unit cost", "Value at cost", "Value at selling price"], rows, ["TOTAL", "", "", "", fmt(total_cost), fmt(total_retail)])


def _r_low_stock(conn, start, end):
    rows = []
    for p, qty in _stock(conn):
        reorder = _d(p["reorder_level"])
        if p["track_stock"] and reorder > 0 and qty <= reorder:
            rows.append([p["sku"], p["name"], _n(qty), _n(reorder), _n(max(reorder * 2 - qty, 0))])
    return Report("Low stock / reorder list", ["SKU", "Product", "On hand", "Reorder level", "Suggested order qty"], rows)


def _r_money_out(conn, start, end):
    agg = defaultdict(lambda: [0, Decimal("0")])
    for r in conn.execute("SELECT category,source_type,amount FROM cash_entries WHERE direction='out' AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?", (start, end)):
        key = r["category"] or r["source_type"]
        agg[key][0] += 1; agg[key][1] += _d(r["amount"])
    rows = [[k, v[0], fmt(v[1])] for k, v in sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)]
    return Report("Money out by category", ["Category", "Entries", "Amount"], rows, ["TOTAL", sum(v[0] for v in agg.values()), fmt(sum((v[1] for v in agg.values()), Decimal("0")))])


def _r_shift_history(conn, start, end):
    try:
        rows = [[s["id"], s["opened_at"][:16].replace("T", " "), s["opened_by"], (s["closed_at"][:16].replace("T", " ") if s["closed_at"] else "open"),
                 fmt(s["opening_float"]), fmt(s["expected_cash"]) if s["expected_cash"] else "", fmt(s["counted_cash"]) if s["counted_cash"] else "",
                 fmt(s["variance"]) if s["variance"] else ""]
                for s in conn.execute("SELECT * FROM cash_shifts WHERE substr(opened_at,1,10)>=? AND substr(opened_at,1,10)<=? ORDER BY id DESC", (start, end))]
    except Exception:
        rows = []
    return Report("Shifts & cash variance", ["#", "Opened", "By", "Closed", "Float", "Expected", "Counted", "Variance"], rows)


def to_csv(report: Report, path: str) -> int:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([report.title]); w.writerow(report.headers)
        w.writerows(report.rows)
        if report.footer:
            w.writerow(report.footer)
    return len(report.rows)


def to_html(report: Report, business: dict, period: str, layout="document") -> str:
    from html import escape
    head = "".join(f"<th>{escape(str(h))}</th>" for h in report.headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in report.rows)
    foot = ("<tr>" + "".join(f"<th>{escape(str(c))}</th>" for c in report.footer) + "</tr>") if report.footer else ""
    size = "7.5pt" if layout == "receipt" else "9.5pt"
    return (f"<html><head><meta charset='utf-8'><style>body{{font-family:Arial;font-size:{size}}}td,th{{padding:4px;border-bottom:1px solid #ccc;text-align:left}}th{{background:#eee}}</style></head><body>"
            f"<h2>{escape(business.get('name',''))}</h2><h3>{escape(report.title)}</h3><p>{escape(period)}</p>"
            f"<table width='100%' cellspacing='0'><thead><tr>{head}</tr></thead><tbody>{body}</tbody><tfoot>{foot}</tfoot></table>"
            f"<p style='color:#666'>Printed {datetime.now():%Y-%m-%d %H:%M}</p></body></html>")
