"""Which eBay market you sell on, and what that means for money, distance and tax.

The five biggest eBay markets (US, UK, Germany, Australia, Canada) get a tax summary laid out like that country's
self-employment form. Every other market gets the same profit tracking with no tax page, and you can pick the
currency in Settings. Nothing here is tax advice; it's a record-keeping aid.
"""
import time
from datetime import date

import db

CURRENCIES = {   # code -> (symbol, name)
    "GBP": ("£", "Pound sterling"), "USD": ("$", "US dollar"), "EUR": ("€", "Euro"),
    "AUD": ("A$", "Australian dollar"), "CAD": ("C$", "Canadian dollar"), "CHF": ("CHF ", "Swiss franc"),
    "PLN": ("zł ", "Polish złoty"), "SEK": ("kr ", "Swedish krona"), "DKK": ("kr ", "Danish krone"),
    "NOK": ("kr ", "Norwegian krone"), "CZK": ("Kč ", "Czech koruna"), "HUF": ("Ft ", "Hungarian forint"),
    "NZD": ("NZ$", "New Zealand dollar"), "HKD": ("HK$", "Hong Kong dollar"), "SGD": ("S$", "Singapore dollar"),
    "JPY": ("¥", "Japanese yen"),
}

# Tax profiles for the top five markets. `lines` maps the app's expense groups (numbered like the UK SA103S boxes
# 11-19, which is how expenses are categorised internally) to that country's form lines.
TAX = {
    "UK": {"name": "Self Assessment", "form": "SA103S (self-employment, short)", "year_start": (4, 6)},
    "US": {
        "name": "Schedule C", "form": "Schedule C (Form 1040)", "year_start": (1, 1),
        "income": "Line 1: Gross receipts or sales",
        "lines": {11: "Part III / line 4: Cost of goods sold", 12: "Line 24a: Travel",
                  13: "Line 26: Wages", 14: "Lines 15, 20b, 25: Insurance, rent, utilities",
                  15: "Line 21: Repairs and maintenance", 16: "Line 17: Legal and professional services",
                  17: "Line 27a: Other expenses (bank and card charges)", 18: "Line 18: Office expense (incl. postage, packaging)",
                  19: "Line 10 / 27a: Commissions, fees and other expenses (incl. eBay fees)"},
        "mileage_line": "Line 9: Car and truck expenses (standard mileage rate)",
        "mileage": {2024: 0.67, 2025: 0.70, 2026: 0.725}, "mileage_note": "IRS standard mileage rate",
        "threshold": (20000, "Form 1099-K reporting (with 200+ transactions)"),
        "notes": [
            "eBay sends a Form 1099-K when your sales pass $20,000 and 200 transactions in a calendar year. You must "
            "report all business income whether or not you get one.",
            "Self-employment tax (Schedule SE) applies once net earnings reach $400.",
            "If you'll owe $1,000 or more, the IRS expects quarterly estimated payments (Form 1040-ES).",
            "State income and sales tax rules vary; eBay collects and pays sales tax on most US orders.",
        ],
        "source": "https://www.irs.gov/forms-pubs/about-schedule-c-form-1040",
    },
    "DE": {
        "name": "EÜR", "form": "Anlage EÜR (Einnahmenüberschussrechnung)", "year_start": (1, 1),
        "income": "Betriebseinnahmen (business income)",
        "lines": {11: "Wareneinkauf (goods bought for resale)", 12: "Reisekosten (travel)",
                  13: "Löhne und Gehälter (wages)", 14: "Raumkosten, Versicherungen (premises, insurance)",
                  15: "Instandhaltung (repairs)", 16: "Rechts- und Steuerberatung (legal, tax adviser)",
                  17: "Kontoführung, Zinsen (bank charges, interest)",
                  18: "Porto, Verpackung, Bürobedarf (postage, packaging, office)",
                  19: "Übrige Betriebsausgaben (other, incl. eBay-Gebühren)"},
        "mileage_line": "Fahrtkosten (business trips in your own car)",
        "mileage": {0: 0.30}, "mileage_note": "Kilometerpauschale for business trips",
        "threshold": (25000, "Kleinunternehmer limit (checked against last year's turnover)"),
        "notes": [
            "Kleinunternehmerregelung: no VAT to charge if turnover was at most €25,000 last year and stays under "
            "€100,000 this year. Above that, you charge and file VAT (Umsatzsteuer).",
            "Platforms report sellers with 30+ sales or €2,000+ a year to the Bundeszentralamt für Steuern (PStTG).",
            "Selling your own used belongings occasionally isn't a business; regular buying to resell is.",
        ],
        "source": "https://www.elster.de",
    },
    "AU": {
        "name": "Business schedule", "form": "Business and professional items schedule (individual return)",
        "year_start": (7, 1), "income": "Other business income (sales)",
        "lines": {11: "Purchases and other costs (cost of goods)", 12: "Motor vehicle / travel expenses",
                  13: "Salary and wage expenses", 14: "Rent expenses (and insurance, power)",
                  15: "Repairs and maintenance", 16: "All other expenses (accounting, professional fees)",
                  17: "Interest expenses / bank fees", 18: "All other expenses (postage, packaging, office)",
                  19: "All other expenses (incl. eBay fees)"},
        "mileage_line": "Motor vehicle expenses (cents per kilometre method)",
        "mileage": {0: 0.88}, "mileage_cap": 5000, "mileage_note": "ATO cents per kilometre, up to 5,000 km a year",
        "threshold": (75000, "GST registration threshold"),
        "notes": [
            "Register for GST once your turnover reaches $75,000 a year.",
            "The cents per kilometre method covers up to 5,000 business km a year; above that you need a logbook.",
            "The tax year runs 1 July to 30 June.",
        ],
        "source": "https://www.ato.gov.au/businesses-and-organisations",
    },
    "CA": {
        "name": "T2125", "form": "T2125 Statement of Business or Professional Activities", "year_start": (1, 1),
        "income": "Line 8000: Gross sales",
        "lines": {11: "Line 8518: Cost of goods sold", 12: "Line 9200: Travel expenses",
                  13: "Line 9060: Salaries, wages and benefits", 14: "Lines 8690, 8910, 9220: Insurance, rent, utilities",
                  15: "Line 8960: Maintenance and repairs", 16: "Line 8860: Legal, accounting and other professional fees",
                  17: "Line 8710: Interest and bank charges", 18: "Lines 8810, 9275: Office, delivery and postage",
                  19: "Line 9270: Other expenses (incl. eBay fees)"},
        "mileage_line": None, "mileage": {}, "threshold": (30000, "GST/HST small supplier limit"),
        "mileage_note": "As a self-employed seller you claim your actual vehicle costs × your business-use share "
                        "(line 9281), not a per-km rate. Your log is the proof of that share.",
        "notes": [
            "Register for GST/HST once sales pass $30,000 over four calendar quarters in a row.",
            "Keep a vehicle log of business km and total km to work out your business-use share.",
            "Platforms report sellers to the CRA under the digital platform reporting rules.",
        ],
        "source": "https://www.canada.ca/en/revenue-agency/services/forms-publications/forms/t2125.html",
    },
}

# eBay sites. Top five carry a tax profile; the rest get profit tracking only.
SITES = {
    "EBAY_US": {"name": "eBay US", "country": "United States", "currency": "USD", "unit": "mi", "tax": "US",
                "fee": (13.6, 0.40), "domain": "ebay.com"},
    "EBAY_GB": {"name": "eBay UK", "country": "United Kingdom", "currency": "GBP", "unit": "mi", "tax": "UK",
                "fee": (12.8, 0.40), "domain": "ebay.co.uk"},
    "EBAY_DE": {"name": "eBay Germany", "country": "Germany", "currency": "EUR", "unit": "km", "tax": "DE",
                "fee": (11.0, 0.35), "domain": "ebay.de"},
    "EBAY_AU": {"name": "eBay Australia", "country": "Australia", "currency": "AUD", "unit": "km", "tax": "AU",
                "fee": (13.4, 0.30), "domain": "ebay.com.au"},
    "EBAY_CA": {"name": "eBay Canada", "country": "Canada", "currency": "CAD", "unit": "km", "tax": "CA",
                "fee": (13.6, 0.40), "domain": "ebay.ca"},
    "EBAY_IE": {"name": "eBay Ireland", "country": "Ireland", "currency": "EUR", "unit": "km", "domain": "ebay.ie"},
    "EBAY_FR": {"name": "eBay France", "country": "France", "currency": "EUR", "unit": "km", "domain": "ebay.fr"},
    "EBAY_IT": {"name": "eBay Italy", "country": "Italy", "currency": "EUR", "unit": "km", "domain": "ebay.it"},
    "EBAY_ES": {"name": "eBay Spain", "country": "Spain", "currency": "EUR", "unit": "km", "domain": "ebay.es"},
    "EBAY_AT": {"name": "eBay Austria", "country": "Austria", "currency": "EUR", "unit": "km", "domain": "ebay.at"},
    "EBAY_NL": {"name": "eBay Netherlands", "country": "Netherlands", "currency": "EUR", "unit": "km", "domain": "ebay.nl"},
    "EBAY_BE": {"name": "eBay Belgium", "country": "Belgium", "currency": "EUR", "unit": "km", "domain": "befr.ebay.be"},
    "EBAY_CH": {"name": "eBay Switzerland", "country": "Switzerland", "currency": "CHF", "unit": "km", "domain": "ebay.ch"},
    "EBAY_PL": {"name": "eBay Poland", "country": "Poland", "currency": "PLN", "unit": "km", "domain": "ebay.pl"},
    "EBAY_HK": {"name": "eBay Hong Kong", "country": "Hong Kong", "currency": "HKD", "unit": "km", "domain": "ebay.com.hk"},
    "EBAY_SG": {"name": "eBay Singapore", "country": "Singapore", "currency": "SGD", "unit": "km", "domain": "ebay.com.sg"},
}
DEFAULT_SITE = "EBAY_GB"
TOP_FIVE = [k for k, v in SITES.items() if v.get("tax")]


_KEYS = ("marketplace_id", "currency", "fy_start")
_cache = {}          # database (override path, or "" for yours) -> {"at", "values"}


def _settings(*keys):
    """The market settings, cached for a few seconds (money() runs hundreds of times a page)."""
    key = db.database_override() or ""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit["at"] < 5:
        return hit["values"]
    values = {}
    try:
        path = db.get_db_path()
        if path:
            values = db.Settings().get_many(list(_KEYS))
    except Exception:  # noqa: BLE001 - before setup there's no database
        values = {}
    _cache[key] = {"at": time.monotonic(), "values": values}
    return values


def invalidate():
    """Call after changing the eBay site, currency or financial year."""
    _cache.clear()


def site_id() -> str:
    s = _settings("marketplace_id").get("marketplace_id")
    return s if s in SITES else DEFAULT_SITE


def site() -> dict:
    return {"id": site_id(), **SITES[site_id()]}


def currency() -> str:
    """Your currency: the one you picked in Settings, else your eBay site's."""
    c = _settings("currency").get("currency")
    return c if c in CURRENCIES else SITES[site_id()]["currency"]


def symbol(code: str | None = None) -> str:
    code = code or currency()
    return CURRENCIES.get(code, (code + " ", ""))[0]


def money(v, blank="—", code: str | None = None) -> str:
    """£1,234.56 / -$12.00 / €3.50 (no decimals for yen)."""
    if v is None:
        return blank
    code = code or currency()
    s = symbol(code)
    n = f"{abs(v):,.0f}" if code in ("JPY", "HUF") else f"{abs(v):,.2f}"
    return ("-" if v < 0 else "") + s + n


def unit() -> str:
    """'mi' or 'km' for the mileage log."""
    return SITES[site_id()]["unit"]


def unit_word(plural=True) -> str:
    return ("miles" if plural else "mile") if unit() == "mi" else ("km")


def tax_code() -> str | None:
    """'UK', 'US', 'DE', 'AU', 'CA' for the top five markets, else None (no tax page)."""
    return SITES[site_id()].get("tax")


def tax() -> dict | None:
    c = tax_code()
    return {"code": c, **TAX[c]} if c else None


def year_start() -> tuple[int, int]:
    """Start of the tax year (top five), or of the financial year you set in Settings (everywhere else)."""
    t = tax()
    if t:
        return t["year_start"]
    raw = _settings("fy_start").get("fy_start") or "01-01"
    try:
        m, d = (int(x) for x in raw.split("-"))
        date(2001, m, d)
        return m, d
    except (ValueError, TypeError):
        return 1, 1


def fee_defaults() -> tuple[float, float]:
    """Typical (final value fee %, per-order fee) for the fee calculator. Check eBay's current fee page."""
    return SITES[site_id()].get("fee", (13.0, 0.35))


def mileage_rate(year: int) -> float:
    """Per-mile/km rate for a car in the top-five non-UK markets (0 if a rate method doesn't apply)."""
    t = tax()
    if not t or t["code"] == "UK":
        return 0.0
    rates = t.get("mileage") or {}
    if not rates:
        return 0.0
    if 0 in rates:
        return rates[0]
    known = [y for y in rates if y <= year] or [min(rates)]
    return rates[max(known)]


def listing_url(item_id) -> str:
    d = SITES[site_id()]["domain"]
    return f"https://{d if d.startswith('befr.') else 'www.' + d}/itm/{item_id}"
