"""Flip for Free connector for Claude (MCP server).

Lets Claude use your Flip for Free data as tools: add stock and bulk buys, fill in missing costs,
record sales from other marketplaces, log expenses and mileage, and check profit and tax.

It talks to the running Flip for Free app through its API, so the app must be running.

Settings (environment variables, set in Claude's connector config):
    FLIP_URL      address of the app, e.g. https://localhost:8765   (default)
    FLIP_API_KEY  a key from Flip for Free -> Settings -> API keys     (required)

Run it yourself to check it works:   python mcp_server.py --check
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

try:                                    # MCP Python SDK v2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                     # MCP Python SDK v1
    from mcp.server.fastmcp import FastMCP as _Server

URL = os.environ.get("FLIP_URL", "https://localhost:8765").rstrip("/")
KEY = os.environ.get("FLIP_API_KEY", "")

INSTRUCTIONS = """Flip for Free is the user's eBay/reselling profit tracker (UK, money in GBP).
eBay sales, fees, refunds and listings sync automatically; the user only adds what eBay doesn't know:
what they paid (cost), where they bought it (source), where it's stored (location), bulk buys, sales on
other marketplaces (Vinted, Facebook, car boot...), expenses and mileage.
Before changing data, look it up (list_items / list_sales / list_bulk_buys) so you use the right ids.
Confirm with the user before bulk changes or anything you're unsure about. Never set confirm_closed=true
unless the user has explicitly said to change a tax year they've already filed."""

mcp = _Server("Flip for Free", instructions=INSTRUCTIONS)


def _verify():
    """The app uses its own certificate on localhost; trust exactly that certificate."""
    host = urlparse(URL).hostname or ""
    if URL.startswith("https://") and host in ("localhost", "127.0.0.1"):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import db
            cert = db.config_dir() / "localhost-cert.pem"
            if cert.exists():
                return str(cert)
        except Exception:
            pass
        return False
    return True


def _call(method, path, params=None, body=None):
    if not KEY:
        return {"error": "FLIP_API_KEY isn't set. Create a key in Flip for Free → Settings → API keys and add it to "
                         "this connector's settings."}
    try:
        r = requests.request(method, f"{URL}/api/v1{path}", params={k: v for k, v in (params or {}).items()
                                                                     if v not in (None, "")},
                             json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=60, verify=_verify())
    except requests.ConnectionError:
        return {"error": f"Can't reach Flip for Free at {URL}. Is the app running?"}
    try:
        data = r.json()
    except ValueError:
        return {"error": f"Unexpected reply ({r.status_code}) from Flip for Free."}
    if r.status_code >= 400 and "error" not in data:
        data = {"error": f"HTTP {r.status_code}", **data}
    return data


def _clean(d):
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------- #
# Overview
# ---------------------------------------------------------------------------- #
@mcp.tool()
def get_summary(period: str = "30d", start: str | None = None, end: str | None = None) -> dict:
    """Profit summary: sales, money made after eBay fees and your packing costs, cost of goods, profit,
    and how many sales are missing a cost. period: today, 7d, 30d, month, last_month, 90d, ytd, fy, all,
    or custom (with start/end as YYYY-MM-DD)."""
    return _call("GET", "/summary", {"period": period, "start": start, "end": end})


@mcp.tool()
def get_tax_summary(year: int | None = None, basis: str | None = None) -> dict:
    """UK Self Assessment (SA103S) figures for a tax year (year = the year it starts, e.g. 2026 for 2026/27),
    plus data checks to fix before filing. basis: cash or accrual."""
    return _call("GET", "/tax", {"year": year, "basis": basis})


@mcp.tool()
def list_notifications(unread_only: bool = True, kind: str | None = None) -> dict:
    """Recent alerts: new sales (with storage location), problems eBay charged for, automation results.
    kind: sale, issue or automation."""
    return _call("GET", "/notifications", {"unread": "1" if unread_only else None, "kind": kind})


@mcp.tool()
def start_sync() -> dict:
    """Pull the latest orders, fees and listings from eBay now."""
    return _call("POST", "/sync", body={})


# ---------------------------------------------------------------------------- #
# Sales
# ---------------------------------------------------------------------------- #
@mcp.tool()
def list_sales(period: str = "30d", missing_cost_only: bool = False, search: str | None = None,
               channel: str | None = None, limit: int = 100) -> dict:
    """Sales with the full breakdown (what the buyer paid, eBay deductions, your costs, profit).
    Use missing_cost_only=True to find sales that still need what you paid."""
    return _call("GET", "/sales", {"period": period, "missing_cost": "1" if missing_cost_only else None,
                                   "q": search, "channel": channel, "limit": limit})


@mcp.tool()
def update_sale_costs(line_item_id: str, cost: float | None = None, postage_cost: float | None = None,
                      packaging_cost: float | None = None, handling_cost: float | None = None,
                      other_cost: float | None = None, bulk_buy_id: int | None = None, notes: str | None = None,
                      confirm_closed: bool = False) -> dict:
    """Set your costs on one sale. cost = what you paid for the item(s) in this sale. postage_cost only for
    postage bought outside eBay. Only the fields you pass are changed."""
    return _call("PATCH", f"/sales/{line_item_id}", body=_clean({
        "cost": cost, "postage_cost": postage_cost, "packaging_cost": packaging_cost,
        "handling_cost": handling_cost, "other_cost": other_cost, "bulk_buy_id": bulk_buy_id, "notes": notes,
        "confirm_closed": confirm_closed or None}))


@mcp.tool()
def record_sale(title: str, item_price: float, channel: str = "Vinted", sold_at: str | None = None,
                quantity: int = 1, delivery_charged: float | None = None, fees: float | None = None,
                postage_cost: float | None = None, packaging_cost: float | None = None,
                handling_cost: float | None = None, cost: float | None = None, item_id: int | None = None,
                notes: str | None = None) -> dict:
    """Record a sale made outside eBay (Vinted, Facebook Marketplace, Depop, Etsy, car boot, in person...).
    Link it to an inventory item with item_id so its cost and bulk buy are used. Don't use for eBay sales:
    those sync automatically."""
    return _call("POST", "/sales", body=_clean({
        "title": title, "item_price": item_price, "channel": channel, "sold_at": sold_at, "quantity": quantity,
        "delivery_charged": delivery_charged, "fees": fees, "postage_cost": postage_cost,
        "packaging_cost": packaging_cost, "handling_cost": handling_cost, "cost": cost, "item_id": item_id,
        "notes": notes}))


# ---------------------------------------------------------------------------- #
# Inventory
# ---------------------------------------------------------------------------- #
@mcp.tool()
def list_items(status: str = "instock", search: str | None = None, location: str | None = None,
               source: str | None = None, missing_cost_only: bool = False) -> dict:
    """Inventory items. status: instock (listed + not listed), listed, unlisted, sold or all."""
    return _call("GET", "/items", {"status": status, "q": search, "location": location, "source": source,
                                   "missing_cost": "1" if missing_cost_only else None})


@mcp.tool()
def get_item(item_id: int) -> dict:
    """One inventory item with its eBay listings, sales and timeline."""
    return _call("GET", f"/items/{item_id}")


@mcp.tool()
def add_items(titles: list[str], cost_each: float | None = None, quantity: int = 1, source: str | None = None,
              purchased_at: str | None = None, location: str | None = None, bulk_buy_id: int | None = None,
              fixed_cost_in_bulk: bool = False, sku: str | None = None, packaging_cost: float | None = None,
              handling_cost: float | None = None, restock_url: str | None = None, notes: str | None = None) -> dict:
    """Add stock you've bought. One item is created per title, all sharing the other details.
    For things from a bulk buy, pass bulk_buy_id and leave cost_each empty (they share the lot's cost),
    unless fixed_cost_in_bulk=True for a valuable item with its own cost."""
    return _call("POST", "/items", body=_clean({
        "titles": titles, "cost": cost_each, "quantity": quantity, "source": source, "purchased_at": purchased_at,
        "location": location, "bulk_buy_id": bulk_buy_id, "cost_locked": fixed_cost_in_bulk or None, "sku": sku,
        "packaging_cost": packaging_cost, "handling_cost": handling_cost, "restock_url": restock_url,
        "notes": notes}))


@mcp.tool()
def update_item(item_id: int, title: str | None = None, cost_each: float | None = None,
                quantity: int | None = None, source: str | None = None, purchased_at: str | None = None,
                location: str | None = None, bulk_buy_id: int | None = None, sku: str | None = None,
                packaging_cost: float | None = None, handling_cost: float | None = None,
                restock_url: str | None = None, notes: str | None = None, link_listing: str | None = None,
                confirm_closed: bool = False) -> dict:
    """Change an inventory item. Only fields you pass change. link_listing = an eBay item ID to attach."""
    return _call("PATCH", f"/items/{item_id}", body=_clean({
        "title": title, "cost": cost_each, "quantity": quantity, "source": source, "purchased_at": purchased_at,
        "location": location, "bulk_buy_id": bulk_buy_id, "sku": sku, "packaging_cost": packaging_cost,
        "handling_cost": handling_cost, "restock_url": restock_url, "notes": notes, "link_listing": link_listing,
        "confirm_closed": confirm_closed or None}))


@mcp.tool()
def write_off_item(item_id: int, reason: str, date: str | None = None) -> dict:
    """Write an item off as lost, damaged or donated (its cost then shows on the P&L)."""
    return _call("POST", f"/items/{item_id}/write-off", body=_clean({"reason": reason, "date": date}))


@mcp.tool()
def list_listings(status: str = "active", search: str | None = None) -> dict:
    """The user's eBay listings (synced). status: active, ended, sold or all."""
    return _call("GET", "/listings", {"status": status, "q": search})


# ---------------------------------------------------------------------------- #
# Bulk buys
# ---------------------------------------------------------------------------- #
@mcp.tool()
def list_bulk_buys() -> dict:
    """Bulk buys (job lots, binders, car boot hauls) with cost, money made back, profit and ROI."""
    return _call("GET", "/bulk-buys")


@mcp.tool()
def create_bulk_buy(name: str, total_cost: float, purchased_at: str | None = None,
                    extra_costs: float | None = None, item_count: int | None = None, source: str | None = None,
                    location: str | None = None, split_method: str = "even", item_titles: list[str] | None = None,
                    notes: str | None = None) -> dict:
    """Create a bulk buy. item_count = how many items were in the lot (used to split the cost).
    split_method: even, or price (weighted by each item's price). item_titles logs its items now."""
    return _call("POST", "/bulk-buys", body=_clean({
        "name": name, "total_cost": total_cost, "purchased_at": purchased_at, "extra_costs": extra_costs,
        "item_count": item_count, "source": source, "location": location, "split_method": split_method,
        "items": item_titles, "notes": notes}))


@mcp.tool()
def update_bulk_buy(bulk_buy_id: int, name: str | None = None, total_cost: float | None = None,
                    purchased_at: str | None = None, extra_costs: float | None = None,
                    item_count: int | None = None, source: str | None = None, location: str | None = None,
                    split_method: str | None = None, notes: str | None = None, confirm_closed: bool = False) -> dict:
    """Change a bulk buy. Only fields you pass change."""
    return _call("PATCH", f"/bulk-buys/{bulk_buy_id}", body=_clean({
        "name": name, "total_cost": total_cost, "purchased_at": purchased_at, "extra_costs": extra_costs,
        "item_count": item_count, "source": source, "location": location, "split_method": split_method,
        "notes": notes, "confirm_closed": confirm_closed or None}))


@mcp.tool()
def link_listings_to_bulk_buy(bulk_buy_id: int, listing_ids: list[str]) -> dict:
    """Put existing eBay listings (by eBay item ID) into a bulk buy; their past and future sales count towards it."""
    return _call("POST", f"/bulk-buys/{bulk_buy_id}/link-listings", body={"listing_ids": listing_ids})


# ---------------------------------------------------------------------------- #
# Expenses & mileage
# ---------------------------------------------------------------------------- #
@mcp.tool()
def list_expenses(tax_year: int | None = None) -> dict:
    """Business expenses for a UK tax year (year it starts, e.g. 2026 = 6 Apr 2026 – 5 Apr 2027)."""
    return _call("GET", "/expenses", {"year": tax_year})


@mcp.tool()
def add_expense(description: str, amount: float, category: str, date: str | None = None,
                item_id: int | None = None) -> dict:
    """Log a business expense. category: stock, travel, wages, premises, repairs, professional, finance,
    office (postage, packaging, phone, stationery), software, advertising, platform, equipment, other.
    Don't log eBay fees or eBay postage labels: those sync automatically."""
    return _call("POST", "/expenses", body=_clean({"description": description, "amount": amount,
                                                   "category": category, "date": date, "item_id": item_id}))


@mcp.tool()
def add_mileage(miles: float, purpose: str, date: str | None = None, return_trip: bool = False,
                vehicle: str = "car", route: str | None = None) -> dict:
    """Log a business journey (sourcing trip, collecting a lot, post office run). return_trip doubles the miles."""
    return _call("POST", "/mileage", body=_clean({"miles": miles, "purpose": purpose, "date": date,
                                                  "return_trip": return_trip, "vehicle": vehicle, "route": route}))


if __name__ == "__main__":
    if "--check" in sys.argv:
        res = _call("GET", "/summary", {"period": "30d"})
        print(res if "error" in res else f"OK: connected to Flip for Free at {URL}. "
                                          f"{res['sales']} sales in the last 30 days, money made £{res['money_made']:,.2f}.")
        sys.exit(1 if "error" in res else 0)
    mcp.run()
