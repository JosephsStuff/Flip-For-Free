"""True profit must always reconcile: buyer paid − eBay deductions = net, and so on down to profit."""
import pytest

import profit
import timetosell


def test_every_sale_reconciles(conn):
    sales = profit.compute_sales(conn)
    assert sales, "demo data should have sales"
    for s in sales:
        assert s["gross"] - s["ebay_deductions_total"] == pytest.approx(s["net_from_ebay"], abs=0.011), s["line_item_id"]
        assert s["net_from_ebay"] - s["own_costs_total"] == pytest.approx(s["money_made"], abs=0.011)
        if s["item_cost"] is not None:
            assert s["money_made"] - s["item_cost"] == pytest.approx(s["profit"], abs=0.011)


def test_deduction_breakdown_adds_up(conn):
    for s in profit.compute_sales(conn):
        assert sum(s["ebay_deductions"].values()) == pytest.approx(s["ebay_deductions_total"], abs=0.02)


def test_summary_matches_sales(conn):
    sales = profit.compute_sales(conn)
    tot = profit.summarise(sales)
    assert tot["count"] == len(sales)
    assert tot["money_made"] == pytest.approx(sum(s["money_made"] for s in sales), abs=0.05)


def test_time_to_sell(conn):
    t = timetosell.compute(conn)
    assert t["single"]["count"] > 0 and t["single"]["avg"] > 0
    assert t["single"]["fastest"] <= t["single"]["median"] <= t["single"]["slowest"]
    for row in t["multi"]["listings"]:
        if row["every_days"]:
            assert row["every_days"] == pytest.approx(row["listed_days"] / row["units"])
