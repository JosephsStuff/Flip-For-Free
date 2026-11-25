"""Helpers shared by every page."""
import re
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import current_app, redirect, render_template, request, session, url_for

import db
import markets


def parse_money(v):
    if v is None:
        return None
    v = re.sub(r"[^\d.,\-]", "", str(v).strip())
    if re.search(r",\d{1,2}$", v) and (v.count(",") == 1):      # 12,50 or 1.234,50 (decimal comma)
        v = v.replace(".", "").replace(",", ".")
    else:
        v = v.replace(",", "")
    if v == "":
        return None
    try:
        return round(float(v), 2)
    except ValueError:
        return None


def parse_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_float(v):
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def db_ready() -> bool:
    p = db.get_db_path()
    return bool(p and Path(p).exists())


PERIODS = [("today", "Today"), ("week", "This week"), ("month", "This month"), ("last_month", "Last month"),
           ("30d", "31 days"), ("90d", "90 days"), ("12m", "12 months"), ("fy", "Financial year"), ("all", "All time"),
           ("custom", "Custom")]


# --------------------------------------------------------------------------- #
# Financial year (you choose the start; default is your market's tax year, e.g. 6 April in the UK)
# --------------------------------------------------------------------------- #
def fy_start_md() -> tuple[int, int]:
    raw = (db.Settings().get("fy_start") or "") if db_ready() else ""
    try:
        m, d = (int(x) for x in raw.split("-"))
        date(2001, m, d)
        return m, d
    except (ValueError, TypeError):
        return markets.year_start()


def fy_of(d=None, md=None) -> int:
    d = d or date.today()
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    m, dd = md or fy_start_md()
    return d.year if (d.month, d.day) >= (m, dd) else d.year - 1


def fy_range(y: int, md=None):
    m, d = md or fy_start_md()
    return date(y, m, d).isoformat(), date(y + 1, m, d).isoformat()


def fy_label(y: int, md=None) -> str:
    m, d = md or fy_start_md()
    return str(y) if (m, d) == (1, 1) else f"{y}/{str(y + 1)[-2:]}"


def saved_ranges() -> list[dict]:
    """Named date ranges you've saved: [{"id", "name", "start", "end"}] (end inclusive)."""
    import json
    if not db_ready():
        return []
    try:
        return json.loads(db.Settings().get("saved_ranges") or "[]")
    except ValueError:
        return []


def find_range(key: str):
    if key and key.startswith("r:"):
        return next((r for r in saved_ranges() if r["id"] == key[2:]), None)
    return None


def period_label(key: str, start: str | None = None, end: str | None = None) -> str:
    r = find_range(key)
    if r:
        return r["name"]
    if key == "custom":
        try:
            a = date.fromisoformat(start).strftime("%d %b %Y") if start else "start"
            b = date.fromisoformat(end).strftime("%d %b %Y") if end else "today"
            return f"{a} – {b}"
        except ValueError:
            return "Custom range"
    return dict(PERIODS).get(key, "All time")


def period_range(key: str, start: str | None = None, end: str | None = None):
    today = date.today()
    r = find_range(key)
    if r:
        key, start, end = "custom", r.get("start"), r.get("end")
    if key == "today":
        s, e = today, None
    elif key == "week":
        s, e = today - timedelta(days=today.weekday()), None
    elif key == "month":
        s, e = today.replace(day=1), None
    elif key == "last_month":
        first = today.replace(day=1)
        s, e = (first - timedelta(days=1)).replace(day=1), first
    elif key == "30d":
        s, e = today - timedelta(days=31), None
    elif key == "90d":
        s, e = today - timedelta(days=90), None
    elif key == "12m":
        s, e = today - timedelta(days=365), None
    elif key in ("fy", "ytd"):
        a, b = fy_range(fy_of(today))
        s, e = date.fromisoformat(a), None
    elif key == "custom":
        try:
            s = date.fromisoformat(start) if start else None
            e = (date.fromisoformat(end) + timedelta(days=1)) if end else None
        except ValueError:
            s = e = None
    else:
        s, e = None, None
    return (s.isoformat() if s else None, e.isoformat() if e else None)


def sales_where(start, end, extra=""):
    where, params = ["1=1"], []
    if start:
        where.append("s.sold_at >= ?")
        params.append(start)
    if end:
        where.append("s.sold_at < ?")
        params.append(end)
    if extra:
        where.append(extra)
    return " AND ".join(where), params


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        from ebay_api import EbayClient
        if not db_ready():
            return redirect(url_for("setup"))
        db.ensure_schema()
        if not current_app.config["DEMO"]:
            if not EbayClient().configured:
                return redirect(url_for("setup"))
            if not session.get("user_id"):
                return redirect(url_for("login", next=request.path))
            if session.get("db_path") != db.get_db_path():
                session.clear()
                return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


# --------------------------------------------------------------------------- #
# Tax years (your market's) & closed-year protection
# --------------------------------------------------------------------------- #
def tax_year_of(d) -> int | None:
    """Tax year identified by its starting calendar year: UK 6 Apr 2025 – 5 Apr 2026 -> 2025; US 2025 -> 2025."""
    if not d:
        return None
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return None
    if isinstance(d, datetime):
        d = d.date()
    return d.year if (d.month, d.day) >= markets.year_start() else d.year - 1


def tax_year_range(y: int):
    m, d = markets.year_start()
    return date(y, m, d).isoformat(), date(y + 1, m, d).isoformat()


def tax_year_label(y: int) -> str:
    return str(y) if markets.year_start() == (1, 1) else f"{y}/{str(y + 1)[-2:]}"


def closed_years() -> set[int]:
    raw = db.Settings().get("closed_tax_years") or ""
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def closed_guard(dates, what="This change"):
    """If any date falls in a closed tax year and the user hasn't confirmed, return a confirmation
    page response; otherwise None. Forms re-post with confirm_closed=1."""
    if request.form.get("confirm_closed") == "1":
        return None
    closed = closed_years()
    hit = sorted({y for y in (tax_year_of(d) for d in dates) if y is not None and y in closed})
    if not hit:
        return None
    fields = [(k, v) for k in request.form for v in request.form.getlist(k)]
    return render_template("confirm_closed.html", years=[tax_year_label(y) for y in hit], what=what,
                           fields=fields, action=request.path, back=request.referrer or "/")


def now_utc():
    return datetime.now(timezone.utc)
