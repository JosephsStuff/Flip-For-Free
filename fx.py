"""Currency conversion to and from GBP using the European Central Bank's daily reference rates (free, no key).

Rates are cached in the database for a day. If the ECB can't be reached, the last cached rates are used.
"""
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from db import Settings

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


class FxError(Exception):
    pass


def _fetch() -> dict:
    r = requests.get(ECB_URL, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    rates = {"EUR": 1.0}
    for el in root.iter():
        if el.get("currency") and el.get("rate"):
            rates[el.get("currency")] = float(el.get("rate"))
    if "GBP" not in rates:
        raise FxError("The ECB rates had no GBP rate.")
    return rates


def rates() -> dict:
    """Units of each currency per 1 EUR."""
    s = Settings()
    cached = s.get("fx_rates")
    data = None
    if cached:
        try:
            data = json.loads(cached)
            fetched = datetime.fromisoformat(data["fetched_at"])
            if datetime.now(timezone.utc) - fetched < timedelta(hours=20):
                return data["rates"]
        except (ValueError, KeyError):
            data = None
    try:
        fresh = _fetch()
    except (requests.RequestException, ET.ParseError, FxError) as e:
        if data:
            return data["rates"]
        raise FxError(f"Couldn't get exchange rates from the European Central Bank ({e}).")
    s.set("fx_rates", json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "rates": fresh}))
    return fresh


def to_gbp(amount: float, currency: str | None) -> float:
    if not amount or not currency or currency.upper() == "GBP":
        return float(amount or 0)
    r = rates()
    cur = currency.upper()
    if cur not in r:
        raise FxError(f"No exchange rate for {cur}.")
    return round(float(amount) / r[cur] * r["GBP"], 2)


def from_gbp(amount: float, currency: str | None) -> float:
    if not amount or not currency or currency.upper() == "GBP":
        return float(amount or 0)
    r = rates()
    cur = currency.upper()
    if cur not in r:
        raise FxError(f"No exchange rate for {cur}.")
    return round(float(amount) / r["GBP"] * r[cur], 2)


def convert(amount: float, from_cur: str | None, to_cur: str) -> float:
    """Convert between any two ECB currencies."""
    if not amount or not from_cur or from_cur.upper() == to_cur.upper():
        return float(amount or 0)
    r = rates()
    a, b = from_cur.upper(), to_cur.upper()
    for c in (a, b):
        if c != "EUR" and c not in r:
            raise FxError(f"No exchange rate for {c}.")
    eur = float(amount) / (1.0 if a == "EUR" else r[a])
    return round(eur * (1.0 if b == "EUR" else r[b]), 2)


def to_home(amount: float, currency: str | None) -> float:
    """Into your currency (Settings → Market and currency)."""
    import markets
    return convert(amount, currency, markets.currency())
