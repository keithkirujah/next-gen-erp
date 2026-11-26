"""Business Pulse: the numbers an owner wants to see every morning, computed from real sales data.

Pure Python (no Qt) so it is fully unit-tested. Everything is derived from the business workspace
database; nothing is invented. Achievements and the daily goal are stored in workspace settings.

Design rule: reward real business progress (a good day, a streak, a milestone) and never nag.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

SALES_COUNT_MILESTONES = (1, 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000)
REVENUE_MILESTONES = (10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 5_000_000, 10_000_000)
STREAK_MILESTONES = (3, 7, 14, 30, 60, 100, 365)

TIPS = (
    "Tip: press Ctrl+K anywhere to jump to a page or action instantly.",
    "Tip: scan a barcode in the POS search box — Enter adds the item.",
    "Tip: hold a sale with F6 when a customer steps away, recall it with F7.",
    "Tip: close your shift at the end of the day to see cash variance in the Z-report.",
    "Tip: Real-time Backups save after every change — point them at a USB drive or cloud folder.",
    "Tip: set a daily goal on the Pulse card and watch the ring fill as you sell.",
    "Tip: Printer setup lets you print receipts automatically after every POS sale.",
    "Tip: the Reports page can export any report to CSV for your accountant.",
)


def _d(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def money(value, currency="KES") -> str:
    return f"{currency} {_d(value):,.0f}" if abs(_d(value)) >= 1000 else f"{currency} {_d(value):,.2f}"


@dataclass
class Achievement:
    key: str
    icon: str
    title: str
    detail: str


@dataclass
class Pulse:
    today: Decimal = Decimal("0")
    today_count: int = 0
    yesterday: Decimal = Decimal("0")
    same_day_last_week: Decimal = Decimal("0")
    week_total: Decimal = Decimal("0")
    prev_week_total: Decimal = Decimal("0")
    month_total: Decimal = Decimal("0")
    best_day_total: Decimal = Decimal("0")
    best_day: str = ""
    streak_days: int = 0
    goal: Decimal = Decimal("0")
    suggested_goal: Decimal = Decimal("0")
    series: list = field(default_factory=list)          # [(date_iso, Decimal)] oldest -> today
    lifetime_sales: int = 0
    lifetime_revenue: Decimal = Decimal("0")
    top_product_week: tuple | None = None               # (name, qty)
    busiest_hour: int | None = None
    low_stock: int = 0
    unpaid_total: Decimal = Decimal("0")
    unpaid_count: int = 0
    insights: list = field(default_factory=list)
    earned: list = field(default_factory=list)          # [Achievement] — all
    new_achievements: list = field(default_factory=list)  # [Achievement] — not yet celebrated

    @property
    def goal_progress(self) -> float:
        return float(self.today / self.goal) if self.goal > 0 else 0.0

    @property
    def delta_vs_yesterday(self):
        if self.yesterday > 0:
            return float((self.today - self.yesterday) / self.yesterday)
        return None

    @property
    def is_best_day_ever(self) -> bool:
        return self.today > 0 and self.today >= self.best_day_total and self.lifetime_sales > 1


class PulseService:
    def __init__(self, conn, get_setting, set_setting, currency="KES"):
        self.conn = conn
        self.get = get_setting
        self.set = set_setting
        self.currency = currency

    # ------------------------------------------------------------------ data
    def _daily_totals(self, since: date) -> dict:
        rows = self.conn.execute(
            "SELECT substr(created_at,1,10) AS d, total FROM sales WHERE status='Completed' AND substr(created_at,1,10)>=?",
            (since.isoformat(),)).fetchall()
        totals = defaultdict(Decimal)
        for row in rows:
            totals[row["d"]] += _d(row["total"])
        return totals

    def compute(self, today: date | None = None) -> Pulse:
        today = today or date.today()
        p = Pulse()
        since = today - timedelta(days=400)
        totals = self._daily_totals(since)
        counts = self.conn.execute("SELECT COUNT(*) c, substr(created_at,1,10) d FROM sales WHERE status='Completed' GROUP BY d").fetchall()
        count_by_day = {r["d"]: r["c"] for r in counts}
        iso = lambda dt: dt.isoformat()
        p.today = totals.get(iso(today), Decimal("0"))
        p.today_count = count_by_day.get(iso(today), 0)
        p.yesterday = totals.get(iso(today - timedelta(days=1)), Decimal("0"))
        p.same_day_last_week = totals.get(iso(today - timedelta(days=7)), Decimal("0"))
        p.series = [(iso(today - timedelta(days=i)), totals.get(iso(today - timedelta(days=i)), Decimal("0"))) for i in range(13, -1, -1)]
        p.week_total = sum((totals.get(iso(today - timedelta(days=i)), Decimal("0")) for i in range(0, 7)), Decimal("0"))
        p.prev_week_total = sum((totals.get(iso(today - timedelta(days=i)), Decimal("0")) for i in range(7, 14)), Decimal("0"))
        month_start = today.replace(day=1)
        p.month_total = sum((v for k, v in totals.items() if k >= iso(month_start) and k <= iso(today)), Decimal("0"))
        life = self.conn.execute("SELECT COUNT(*) c FROM sales WHERE status='Completed'").fetchone()["c"]
        p.lifetime_sales = int(life)
        p.lifetime_revenue = sum((_d(r["total"]) for r in self.conn.execute("SELECT total FROM sales WHERE status='Completed'")), Decimal("0"))
        all_totals = totals
        if all_totals:
            best_day, best_total = max(all_totals.items(), key=lambda kv: kv[1])
            p.best_day, p.best_day_total = best_day, best_total
        # streak: consecutive selling days ending today (or yesterday if nothing sold yet today)
        cursor = today if p.today_count else today - timedelta(days=1)
        streak = 0
        while count_by_day.get(iso(cursor), 0) > 0:
            streak += 1
            cursor -= timedelta(days=1)
        p.streak_days = streak
        # goal
        try:
            p.goal = _d(self.get("pulse_goal_daily", "0") or "0")
        except Exception:
            p.goal = Decimal("0")
        recent = [totals.get(iso(today - timedelta(days=i)), Decimal("0")) for i in range(1, 15)]
        active = [v for v in recent if v > 0]
        if active:
            avg = sum(active, Decimal("0")) / len(active)
            step = Decimal("1000") if avg >= 5000 else Decimal("100")
            p.suggested_goal = ((avg * Decimal("1.1")) / step).to_integral_value(rounding="ROUND_CEILING") * step
        # top product this week, busiest hour, stock and debtors
        top = self.conn.execute(
            """SELECT l.description AS n, SUM(CAST(l.quantity AS REAL)) AS q FROM sale_lines l JOIN sales s ON s.id=l.sale_id
               WHERE s.status='Completed' AND substr(s.created_at,1,10)>=? GROUP BY l.description ORDER BY q DESC LIMIT 1""",
            (iso(today - timedelta(days=6)),)).fetchone()
        if top:
            p.top_product_week = (top["n"], top["q"])
        hours = defaultdict(int)
        for r in self.conn.execute("SELECT created_at FROM sales WHERE status='Completed' AND substr(created_at,1,10)>=?", (iso(today - timedelta(days=30)),)):
            try:
                hours[int(r["created_at"][11:13])] += 1
            except (ValueError, IndexError):
                pass
        if hours:
            p.busiest_hour = max(hours.items(), key=lambda kv: kv[1])[0]
        try:
            p.low_stock = self.conn.execute(
                """SELECT COUNT(*) c FROM products pr WHERE pr.active=1 AND pr.track_stock=1 AND pr.reorder_level IS NOT NULL
                   AND CAST(pr.reorder_level AS REAL)>0 AND COALESCE((SELECT SUM(CAST(quantity_delta AS REAL)) FROM stock_movements m WHERE m.product_id=pr.id),0)<=CAST(pr.reorder_level AS REAL)""").fetchone()["c"]
        except Exception:
            p.low_stock = 0
        unpaid = self.conn.execute("SELECT total,paid_total FROM sales WHERE status='Completed' AND payment_status IN ('Unpaid','Partial')").fetchall()
        p.unpaid_count = len(unpaid)
        p.unpaid_total = sum((_d(r["total"]) - _d(r["paid_total"]) for r in unpaid), Decimal("0"))
        p.earned = self._achievements(p)
        seen = self._seen()
        p.new_achievements = [a for a in p.earned if a.key not in seen]
        p.insights = self._insights(p, today)
        return p

    # ------------------------------------------------------------------ achievements
    def _seen(self) -> dict:
        try:
            data = json.loads(self.get("pulse_achievements", "{}") or "{}")
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def mark_seen(self, achievements) -> None:
        seen = self._seen()
        for a in achievements:
            seen.setdefault(a.key, datetime.now().strftime("%Y-%m-%d"))
        self.set("pulse_achievements", json.dumps(seen))

    def _achievements(self, p: Pulse) -> list:
        out = []
        for n in SALES_COUNT_MILESTONES:
            if p.lifetime_sales >= n:
                out.append(Achievement(f"sales-{n}", "🛒", "First sale!" if n == 1 else f"{n:,} sales", "Your very first sale is in the books." if n == 1 else f"You have completed {n:,} sales."))
        for n in REVENUE_MILESTONES:
            if p.lifetime_revenue >= n:
                out.append(Achievement(f"revenue-{n}", "💰", f"{money(n, self.currency)} sold", f"Lifetime sales passed {money(n, self.currency)}."))
        for n in STREAK_MILESTONES:
            if p.streak_days >= n:
                out.append(Achievement(f"streak-{n}", "🔥", f"{n}-day selling streak", f"You have sold something {n} days in a row."))
        if p.goal > 0 and p.today >= p.goal:
            out.append(Achievement(f"goal-{date.today().isoformat()}", "🎯", "Daily goal reached", f"You hit {money(p.goal, self.currency)} today."))
        if p.is_best_day_ever:
            out.append(Achievement(f"best-{p.best_day}", "🏆", "Best day ever", f"{money(p.today, self.currency)} — your biggest day so far."))
        return out

    # ------------------------------------------------------------------ insights
    def _insights(self, p: Pulse, today: date) -> list:
        c = self.currency
        lines = []
        if p.yesterday > 0 and p.today_count == 0:
            lines.append(f"Yesterday you sold {money(p.yesterday, c)}. Let's beat it today.")
        if p.prev_week_total > 0 and p.week_total > 0:
            change = (p.week_total - p.prev_week_total) / p.prev_week_total * 100
            lines.append(f"The last 7 days are {'up' if change >= 0 else 'down'} {abs(change):.0f}% on the week before.")
        if p.top_product_week:
            q = p.top_product_week[1]
            lines.append(f"Top seller this week: {p.top_product_week[0]} ({q:g} sold).")
        if p.busiest_hour is not None:
            lines.append(f"Your busiest hour lately is around {p.busiest_hour % 12 or 12}{'am' if p.busiest_hour < 12 else 'pm'}.")
        if p.low_stock:
            lines.append(f"{p.low_stock} product{'s' if p.low_stock != 1 else ''} at or below reorder level — time to restock.")
        if p.unpaid_count:
            lines.append(f"{p.unpaid_count} unpaid invoice{'s' if p.unpaid_count != 1 else ''} worth {money(p.unpaid_total, c)} to collect.")
        if p.best_day and p.best_day_total > 0:
            lines.append(f"Your best day so far was {p.best_day} at {money(p.best_day_total, c)}.")
        if p.month_total > 0:
            lines.append(f"{today.strftime('%B')} so far: {money(p.month_total, c)}.")
        lines.append(TIPS[today.toordinal() % len(TIPS)])
        return lines

    def greeting(self, name: str, now: datetime | None = None) -> str:
        now = now or datetime.now()
        word = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
        return f"{word}, {name}"

    # ------------------------------------------------------------------ morning recap (shown once per day)
    def recap_due(self, today: date | None = None) -> bool:
        today = (today or date.today()).isoformat()
        return (self.get("pulse_last_recap", "") or "") != today

    def mark_recap_shown(self, today: date | None = None) -> None:
        self.set("pulse_last_recap", (today or date.today()).isoformat())
