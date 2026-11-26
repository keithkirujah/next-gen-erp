"""Till shifts and the end-of-day Z-report.

A shift starts with an opening float, runs while sales are made, and ends with a cash count.
The system compares what the till SHOULD hold (float + cash in − cash out) with what was
counted, and keeps the variance on record. Tables live in the business workspace database, so
they travel with backups. One shift can be open at a time per business workspace.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_shifts(
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('Open','Closed')),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    opening_float TEXT NOT NULL DEFAULT '0.00',
    closed_by TEXT NOT NULL DEFAULT '',
    closed_at TEXT NOT NULL DEFAULT '',
    counted_cash TEXT NOT NULL DEFAULT '',
    expected_cash TEXT NOT NULL DEFAULT '',
    variance TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_shift ON cash_shifts(status) WHERE status='Open';
"""


class ShiftError(Exception):
    pass


def _d(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ShiftService:
    def __init__(self, conn, actor=lambda: "system", log=lambda *a: None):
        self.conn = conn
        self.actor = actor
        self.log = log
        with self.conn:
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ state
    def current(self):
        return self.conn.execute("SELECT * FROM cash_shifts WHERE status='Open' ORDER BY id DESC LIMIT 1").fetchone()

    def get(self, shift_id):
        return self.conn.execute("SELECT * FROM cash_shifts WHERE id=?", (int(shift_id),)).fetchone()

    def history(self, limit=60):
        return self.conn.execute("SELECT * FROM cash_shifts ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()

    # ------------------------------------------------------------------ actions
    def open_shift(self, opening_float="0"):
        amount = _d(opening_float)
        if amount < 0:
            raise ShiftError("The opening float cannot be negative.")
        if self.current():
            raise ShiftError("A shift is already open. Close it before opening a new one.")
        with self.conn:
            cur = self.conn.execute("INSERT INTO cash_shifts(status,opened_by,opened_at,opening_float) VALUES('Open',?,?,?)",
                                    (self.actor(), _stamp(), str(_q(amount))))
        self.log("Opened shift", f"float {_q(amount)}")
        return cur.lastrowid

    def close_shift(self, counted_cash, notes=""):
        shift = self.current()
        if not shift:
            raise ShiftError("There is no open shift to close.")
        counted = _d(counted_cash)
        if counted < 0:
            raise ShiftError("Counted cash cannot be negative.")
        closed_at = _stamp()
        summary = self.summary(shift["id"], until=closed_at)
        expected = summary["expected_cash"]
        variance = _q(counted - expected)
        with self.conn:
            self.conn.execute(
                "UPDATE cash_shifts SET status='Closed',closed_by=?,closed_at=?,counted_cash=?,expected_cash=?,variance=?,notes=? WHERE id=?",
                (self.actor(), closed_at, str(_q(counted)), str(expected), str(variance), str(notes or "")[:400], shift["id"]))
        self.log("Closed shift", f"expected {expected} counted {_q(counted)} variance {variance}")
        return shift["id"]

    # ------------------------------------------------------------------ numbers
    def summary(self, shift_id, until=None) -> dict:
        shift = self.get(shift_id)
        if not shift:
            raise ShiftError("That shift does not exist.")
        start = shift["opened_at"]
        end = until or (shift["closed_at"] or _stamp())
        sales = self.conn.execute(
            "SELECT * FROM sales WHERE status='Completed' AND created_at>=? AND created_at<=? ORDER BY id", (start, end)).fetchall()
        gross = sum((_d(s["total"]) for s in sales), Decimal("0"))
        tax = sum((_d(s["tax_total"]) for s in sales), Decimal("0"))
        credit = sum((_d(s["total"]) - _d(s["paid_total"]) for s in sales), Decimal("0"))
        cash_in, cash_out = defaultdict(Decimal), defaultdict(Decimal)
        for row in self.conn.execute(
                "SELECT direction,amount,payment_method,source_type FROM cash_entries WHERE created_at>=? AND created_at<=?", (start, end)):
            bucket = cash_in if row["direction"] == "in" else cash_out
            bucket[row["payment_method"]] += _d(row["amount"])
        refunds = sum((_d(r["amount"]) for r in self.conn.execute(
            "SELECT amount FROM cash_entries WHERE direction='out' AND source_type LIKE '%return%' AND created_at>=? AND created_at<=?", (start, end))), Decimal("0"))
        opening = _d(shift["opening_float"])
        expected = _q(opening + cash_in.get("Cash", Decimal("0")) - cash_out.get("Cash", Decimal("0")))
        return {
            "shift": shift, "sales_count": len(sales), "gross_sales": _q(gross), "tax_total": _q(tax),
            "net_sales": _q(gross - tax), "credit_sales": _q(credit), "refunds": _q(refunds),
            "money_in": {k: _q(v) for k, v in sorted(cash_in.items())},
            "money_out": {k: _q(v) for k, v in sorted(cash_out.items())},
            "opening_float": _q(opening), "expected_cash": expected,
            "counted_cash": _q(_d(shift["counted_cash"])) if shift["counted_cash"] != "" else None,
            "variance": _q(_d(shift["variance"])) if shift["variance"] != "" else None,
        }
