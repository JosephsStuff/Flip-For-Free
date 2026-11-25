"""Other eBay markets: currency, tax years, mileage and tax summaries. The UK must stay exactly as it was."""
import pytest

import common
import db
import fx
import markets
import taxcalc


@pytest.fixture()
def market(demo_db):
    def use(site, currency=None):
        db.Settings().set_many({"marketplace_id": site, "currency": currency})
    yield use
    db.Settings().set_many({"marketplace_id": "EBAY_GB", "currency": None})


@pytest.mark.parametrize("site,cur,expect", [("EBAY_GB", None, "£1,234.50"), ("EBAY_US", None, "$1,234.50"),
                                             ("EBAY_DE", None, "€1,234.50"), ("EBAY_AU", None, "A$1,234.50"),
                                             ("EBAY_FR", "CHF", "CHF 1,234.50")])
def test_money_follows_market_and_currency_choice(market, site, cur, expect):
    market(site, cur)
    assert markets.money(1234.5) == expect and markets.money(-2) == "-" + expect[: -8] + "2.00"


@pytest.mark.parametrize("text,value", [("£1,234.56", 1234.56), ("$12", 12.0), ("€12,50", 12.5),
                                        ("1.234,50", 1234.5), ("CHF 7", 7.0), ("12,345", 12345.0)])
def test_money_input_in_any_format(text, value):
    assert common.parse_money(text) == value


@pytest.mark.parametrize("site,day,year,label,start", [("EBAY_GB", "2026-04-05", 2025, "2025/26", "2025-04-06"),
                                                       ("EBAY_US", "2026-04-05", 2026, "2026", "2026-01-01"),
                                                       ("EBAY_AU", "2026-06-30", 2025, "2025/26", "2025-07-01"),
                                                       ("EBAY_CA", "2026-12-31", 2026, "2026", "2026-01-01")])
def test_tax_years(market, site, day, year, label, start):
    market(site)
    y = common.tax_year_of(day)
    assert (y, common.tax_year_label(y), common.tax_year_range(y)[0]) == (year, label, start)


def test_mileage_rates_by_market(market):
    rows = [{"date": "2026-03-01", "miles": 6000, "vehicle": "car"}]
    market("EBAY_US")
    assert taxcalc.mileage_allowance(rows, 2026)["total"] == pytest.approx(6000 * 0.725)
    market("EBAY_AU")
    m = taxcalc.mileage_allowance(rows, 2025)
    assert m["total"] == pytest.approx(5000 * 0.88) and m["capped"]          # 5,000 km cap
    market("EBAY_CA")
    assert taxcalc.mileage_allowance(rows, 2026)["total"] == 0                # actual costs, not a rate
    market("EBAY_FR")
    assert taxcalc.mileage_allowance(rows, 2026)["total"] == 0                # no tax profile


@pytest.mark.parametrize("site", ["EBAY_US", "EBAY_DE", "EBAY_AU", "EBAY_CA"])
def test_summary_reconciles_with_the_full_figures(market, conn, site):
    market(site)
    y = common.tax_year_of("2026-06-01")
    full = taxcalc.compute(conn, y)
    s = taxcalc.summary(conn, y)
    assert s["profit"] == pytest.approx(full["net"], abs=0.02)
    assert all(row["line"] for row in s["rows"])


def test_small_markets_get_no_tax_page(market, client):
    market("EBAY_FR")
    r = client.get("/tax")
    assert r.status_code == 302 and "/pnl" in r.headers["Location"]
    page = client.get("/pnl").get_data(as_text=True)
    assert "Self Assessment" not in page and "€" in page and "£" not in page


def test_uk_pages_unchanged(client):
    page = client.get("/tax").get_data(as_text=True)
    assert "Self Assessment" in page and "SA103S" in page and "£" in page


def test_us_pages(market, client):
    market("EBAY_US")
    page = client.get("/tax").get_data(as_text=True)
    assert "Schedule C" in page and "Line 9: Car and truck expenses" in page and "£" not in page


def test_change_market_in_settings(market, client):
    r = client.post("/settings/market", data={"marketplace_id": "EBAY_CA", "currency": ""})
    assert r.status_code == 302 and markets.currency() == "CAD" and markets.unit() == "km"
    assert client.post("/settings/market", data={"marketplace_id": "EBAY_XX"}).status_code == 400


def test_currency_conversion_between_any_pair(monkeypatch):
    monkeypatch.setattr(fx, "rates", lambda: {"EUR": 1.0, "GBP": 0.85, "USD": 1.10})
    assert fx.convert(10, "EUR", "USD") == 11.0
    assert fx.convert(11, "USD", "GBP") == pytest.approx(8.5)
    assert fx.convert(5, "GBP", "GBP") == 5
