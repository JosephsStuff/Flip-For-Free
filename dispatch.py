"""Post office mileage from dispatch dates.

Every day you sent at least one parcel counts as one return trip from home to a drop-off
point (post office, parcel locker, Collect+ shop...). Distances are worked out once per
drop-off point from postcodes: postcodes.io finds the location and OSRM (OpenStreetMap
routing) gives the driving distance. If routing isn't available, the straight-line distance
x 1.25 is used, and you can always type the miles yourself.
"""
import math
from datetime import date, timedelta
from collections import defaultdict

import requests

from db import Settings, now_iso, write_lock

TIMEOUT = 15
ROAD_FACTOR = 1.25
NO_POSTAGE_CHANNELS = {"Car boot", "In person"}


class DistanceError(Exception):
    pass


def normalise_postcode(pc: str) -> str:
    pc = "".join((pc or "").upper().split())
    return f"{pc[:-3]} {pc[-3:]}" if len(pc) > 4 else pc


def lookup(pc: str):
    pc = normalise_postcode(pc)
    if not pc:
        raise DistanceError("No postcode given.")
    url = (f"https://api.postcodes.io/postcodes/{requests.utils.quote(pc)}" if " " in pc
           else f"https://api.postcodes.io/outcodes/{requests.utils.quote(pc)}")
    try:
        r = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise DistanceError(f"Couldn't reach the postcode lookup ({e.__class__.__name__}).")
    if r.status_code == 404:
        raise DistanceError(f"Postcode {pc} wasn't found.")
    if r.status_code != 200:
        raise DistanceError(f"Postcode lookup failed ({r.status_code}).")
    res = r.json().get("result") or {}
    if res.get("latitude") is None:
        raise DistanceError(f"Postcode {pc} has no location.")
    return float(res["latitude"]), float(res["longitude"])


def straight_miles(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def driving_miles(home_pc: str, dest_pc: str):
    """(one-way miles, note)."""
    a, b = lookup(home_pc), lookup(dest_pc)
    try:
        r = requests.get(f"https://router.project-osrm.org/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}",
                         params={"overview": "false"}, timeout=TIMEOUT)
        if r.status_code == 200 and r.json().get("routes"):
            meters = r.json()["routes"][0]["distance"]
            return round(meters / 1609.344, 1), "driving distance (OpenStreetMap)"
    except (requests.RequestException, ValueError, KeyError):
        pass
    return round(straight_miles(a, b) * ROAD_FACTOR, 1), "estimated (straight line × 1.25)"


def locations(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM drop_off_locations ORDER BY is_default DESC, name")]


def default_location(conn):
    r = conn.execute("SELECT * FROM drop_off_locations ORDER BY is_default DESC, id LIMIT 1").fetchone()
    return dict(r) if r else None


def dispatch_days(conn, start: str, end: str):
    """[{date, parcels, titles, logged (mileage row or None)}] newest first."""
    days = defaultdict(lambda: {"parcels": 0, "orders": set(), "titles": []})
    for s in conn.execute("""SELECT order_id, title, shipped_at, sold_at, channel, source, postage_cost,
                                    delivery_charged, status FROM sales
                             WHERE COALESCE(status, '') <> 'cancelled'
                               AND COALESCE(shipped_at, CASE WHEN source='manual' THEN sold_at END) >= ?
                               AND COALESCE(shipped_at, CASE WHEN source='manual' THEN sold_at END) < ?""",
                          (start, end)):
        if s["source"] == "manual":
            if s["channel"] in NO_POSTAGE_CHANNELS or not ((s["postage_cost"] or 0) or (s["delivery_charged"] or 0)):
                continue
            d = (s["shipped_at"] or s["sold_at"])[:10]
        else:
            if not s["shipped_at"]:
                continue
            d = s["shipped_at"][:10]
        day = days[d]
        if s["order_id"] not in day["orders"]:
            day["orders"].add(s["order_id"])
            day["parcels"] += 1
        if len(day["titles"]) < 4:
            day["titles"].append(s["title"])
    logged = {r["dispatch_date"]: dict(r) for r in conn.execute(
        "SELECT * FROM mileage WHERE dispatch_date IS NOT NULL AND dispatch_date >= ? AND dispatch_date < ?",
        (start[:10], end[:10]))}
    # a manually logged post office trip on the same day also counts
    manual = {r["date"]: dict(r) for r in conn.execute(
        """SELECT * FROM mileage WHERE dispatch_date IS NULL AND date >= ? AND date < ?
           AND (LOWER(purpose) LIKE '%post%' OR LOWER(purpose) LIKE '%drop%' OR LOWER(purpose) LIKE '%parcel%')""",
        (start[:10], end[:10]))}
    out = []
    for d in sorted(days, reverse=True):
        out.append({"date": d, "parcels": days[d]["parcels"], "titles": days[d]["titles"],
                    "logged": logged.get(d) or manual.get(d)})
    return out


def log_trips(conn, dates, location, vehicle="car"):
    """Add one return trip per date to `location`. Skips days already logged."""
    if not location or not location.get("miles_one_way"):
        raise DistanceError("That drop-off point has no distance yet.")
    added = 0
    with write_lock():
        for d in dates:
            if conn.execute("SELECT 1 FROM mileage WHERE dispatch_date=?", (d,)).fetchone():
                continue
            nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
            day = dispatch_days(conn, d, nxt)
            parcels = day[0]["parcels"] if day else 1
            conn.execute("""INSERT INTO mileage(date, miles, vehicle, purpose, route, created_at, dispatch_date,
                                                drop_off_id) VALUES (?,?,?,?,?,?,?,?)""",
                         (d, round(location["miles_one_way"] * 2, 1), vehicle,
                          f"Dropping off {parcels} parcel{'s' if parcels != 1 else ''}",
                          f"Home → {location['name']} → Home", now_iso(), d, location["id"]))
            added += 1
        conn.commit()
    return added


def auto_log(conn) -> int:
    """If switched on, log a trip for every new dispatch day since the setting was turned on."""
    st = Settings().get_many(["auto_dropoff_trips", "auto_dropoff_since", "mileage_vehicle"])
    if st.get("auto_dropoff_trips") != "1":
        return 0
    loc = default_location(conn)
    if not loc or not loc.get("miles_one_way"):
        return 0
    since = st.get("auto_dropoff_since") or now_iso()[:10]
    days = [d["date"] for d in dispatch_days(conn, since, "9999-12-31") if not d["logged"]]
    return log_trips(conn, days, loc, st.get("mileage_vehicle") or "car")
