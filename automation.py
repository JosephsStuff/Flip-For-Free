"""Listing automations that run on their own:

  * End & relist - ends fixed-price "Good 'Til Cancelled" listings shortly before
    eBay's 30-day auto-renew and relists them (new item ID, fresh start date),
    optionally cutting the price each time down to a floor you set.
  * Delayed promotion - adds Promoted Listings (cost-per-sale) ads only once an
    item has gone unsold for N days, so you don't pay ad fees on things that sell anyway.

Every run refreshes your active listings from eBay first, then applies the rules.
Everything that happens is written to the automation log and the item's timeline.
"""
import json
import math
import threading
from datetime import datetime, timedelta, timezone

from db import Settings, add_event, connect, device_name, notify, now_iso, write_lock
from ebay_api import EbayClient, EbayError

RENEW_DAYS = 30

DEFAULTS = {
    "relist": {
        "mode": "renewal",             # "renewal": just before eBay's 30-day auto-renew; "age": once N days old
        "hours_before_renew": 24,      # renewal mode: act this long before the auto-renew
        "relist_after_days": 30,       # age mode: relist once a listing is this many days old
        "min_age_days": 0,             # only relist listings at least this old
        "relist_hour": -1,             # preferred hour of day (0-23) to relist; -1 = any time
        "price_drop_pct": 5.0,         # cut price by this % on each relist (0 = keep price)
        "max_cycles": 20,              # stop cutting the price after this many relists
        "max_total_drop_pct": 25.0,    # never go below original price minus this %
        "min_price": 0.0,              # never go below this price (£, 0 = no fixed floor)
        "never_below_cost": True,      # never go below what you paid
        "skip_with_sales": True,       # skip multi-quantity listings that already have sales
        "skip_watchers_at": 0,         # skip if this many people watch it (0 = don't skip)
        "max_per_day": 50,             # daily cap (max 200)
        "require_approval": False,     # queue relists for you to approve first
    },
    "promote": {
        "after_days": 21,              # promote once unsold for this many days
        "ad_rate": 4.0,                # % ad rate (cost per sale)
        "campaign_name": "Flip for Free auto-promote",
        "max_per_run": 200,
    },
}
LABELS = {"relist": "End & relist (with price drops)", "promote": "Delayed promotion"}

STATE = {"running": False, "last": None, "message": ""}
_lock = threading.Lock()


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def ensure_rules(conn):
    if conn.execute("SELECT COUNT(DISTINCT kind) FROM automation_rules WHERE kind IN ('relist','promote')").fetchone()[0] == 2:
        return                         # already there: viewing the page never needs to write
    with write_lock():
        for kind in ("relist", "promote"):
            if not conn.execute("SELECT 1 FROM automation_rules WHERE kind=?", (kind,)).fetchone():
                conn.execute("INSERT INTO automation_rules(kind, name, enabled, config) VALUES (?,?,0,?)",
                             (kind, LABELS[kind], json.dumps(DEFAULTS[kind])))
        conn.commit()


def rule_config(rule) -> dict:
    cfg = dict(DEFAULTS[rule["kind"]])
    try:
        cfg.update(json.loads(rule["config"] or "{}"))
    except ValueError:
        pass
    return cfg


def _log(conn, rule_id, lid, action, result, detail):
    conn.execute("INSERT INTO automation_log(at, rule_id, legacy_item_id, action, result, detail) "
                 "VALUES (?,?,?,?,?,?)", (now_iso(), rule_id, lid, action, result, detail))


# --------------------------------------------------------------------------- #
# Candidates (also used for the preview on the Automations page)
# --------------------------------------------------------------------------- #
def next_renewal(start: datetime, now: datetime) -> datetime:
    age_days = (now - start).total_seconds() / 86400
    cycles = max(math.ceil(age_days / RENEW_DAYS), 1)
    nxt = start + timedelta(days=RENEW_DAYS * cycles)
    if nxt <= now:
        nxt += timedelta(days=RENEW_DAYS)
    return nxt


def _item_cost(conn, item_id):
    if not item_id:
        return None
    from profit import item_unit_costs
    return item_unit_costs(conn).get(item_id)


def relist_candidates(conn, cfg, now=None, horizon_hours=None):
    """Listings the relist rule will act on. With horizon_hours, also those due within that window."""
    now = now or datetime.now(timezone.utc)
    window = horizon_hours if horizon_hours is not None else cfg["hours_before_renew"]
    out = []
    rows = conn.execute("""SELECT l.*, i.relist_exclude, i.relist_floor FROM listings l
                           LEFT JOIN items i ON i.id = l.item_id
                           WHERE l.status IN ('active', 'relist_failed')
                           AND (l.listing_type IS NULL OR l.listing_type IN ('FixedPriceItem', 'StoresFixedPrice'))
                           AND (l.duration IS NULL OR l.duration = 'GTC')""").fetchall()
    for L in rows:
        L = dict(L)
        start = _parse(L["start_time"])
        if not start:
            continue
        age_days = (now - start).total_seconds() / 86400
        if cfg.get("mode") == "age":
            renew = start + timedelta(days=cfg["relist_after_days"])
        else:
            renew = next_renewal(start, now)
        hours_left = (renew - now).total_seconds() / 3600
        skip = None
        if L["status"] == "relist_failed":
            hours_left = 0
        elif age_days < cfg["min_age_days"]:
            skip = f"younger than {cfg['min_age_days']} days"
        if cfg["skip_with_sales"] and (L["quantity_sold"] or 0) > 0:
            skip = "already has sales on this listing"
        if cfg["skip_watchers_at"] and (L["watch_count"] or 0) >= cfg["skip_watchers_at"]:
            skip = f"{L['watch_count']} watchers"
        if L.get("relist_exclude"):
            skip = "excluded on the item"
        if hours_left > window:
            continue
        new_price, floor = plan_price(conn, L, cfg)
        out.append({**L, "renew_at": renew.strftime("%Y-%m-%dT%H:%M:%SZ"), "hours_left": hours_left,
                    "new_price": new_price, "floor": floor, "skip": skip,
                    "due": hours_left <= cfg["hours_before_renew"]})
    out.sort(key=lambda c: c["hours_left"])
    return out


def plan_price(conn, L, cfg):
    cur = L["price"] or 0
    orig = L["original_price"] or cur
    floor = orig * (1 - (cfg["max_total_drop_pct"] or 0) / 100)
    if cfg["never_below_cost"]:
        cost = _item_cost(conn, L["item_id"])
        if cost:
            floor = max(floor, cost)
    floor = max(floor, cfg.get("min_price") or 0, L.get("relist_floor") or 0)
    floor = round(floor, 2)
    drop = cfg["price_drop_pct"] or 0
    if (L.get("relist_count") or 0) >= cfg.get("max_cycles", 20):
        drop = 0
    if not drop or cur <= floor:
        return None, floor
    new = max(round(cur * (1 - drop / 100), 2), floor)
    return (new if new < cur - 0.004 else None), floor


def promote_candidates(conn, cfg, now=None, horizon_days=0):
    now = now or datetime.now(timezone.utc)
    out = []
    rows = conn.execute("""SELECT l.*, (SELECT MIN(l2.start_time) FROM listings l2
                                        WHERE l2.item_id = l.item_id AND l.item_id IS NOT NULL) AS first_listed
                           FROM listings l WHERE l.status='active' AND COALESCE(l.promoted, 0) = 0""").fetchall()
    for L in rows:
        L = dict(L)
        first = _parse(L["first_listed"] or L["start_time"])
        if not first:
            continue
        days = (now - first).total_seconds() / 86400
        if days + horizon_days < cfg["after_days"]:
            continue
        out.append({**L, "unsold_days": int(days), "due": days >= cfg["after_days"]})
    out.sort(key=lambda c: -c["unsold_days"])
    return out


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def run_all(only_rule: int | None = None, simulate: bool = False, triggered_by="schedule") -> str:
    if not _lock.acquire(blocking=False):
        return "Automations are already running."
    STATE.update(running=True, message="Running automations...")
    conn = connect()
    msgs = []
    try:
        st = Settings()
        if not simulate and triggered_by == "schedule":
            if st.get("automations_paused") == "1":
                return "Automations are paused."
            if not _claim_runner(st):
                return "Automations are running on another device."
        ensure_rules(conn)
        client = None if simulate else EbayClient(st)
        if client is not None:
            # never act on stale data: refresh listings first
            from sync import link_sales_to_items, sync_listings
            sync_listings(conn, client.iter_active_listings())
            link_sales_to_items(conn)
            conn.commit()
        q = "SELECT * FROM automation_rules WHERE enabled=1" if only_rule is None else \
            "SELECT * FROM automation_rules WHERE id=?"
        for rule in conn.execute(q, () if only_rule is None else (only_rule,)).fetchall():
            cfg = rule_config(rule)
            if rule["kind"] == "relist":
                msgs.append(_run_relist(conn, client, rule, cfg, manual=triggered_by == "manual"))
                daily_digest(conn)
            elif rule["kind"] == "promote":
                msgs.append(_run_promote(conn, client, rule, cfg))
            with write_lock():
                conn.execute("UPDATE automation_rules SET last_run=? WHERE id=?", (now_iso(), rule["id"]))
                conn.commit()
        return " ".join(msgs) or "No automations are switched on."
    except EbayError as e:
        msgs.append(f"Stopped: {e}")
        with write_lock():
            notify(conn, "issue", "Automations stopped", str(e)[:400], "/automations",
                   key=f"autostop-{now_iso()[:13]}")
            conn.commit()
        return " ".join(msgs)
    except Exception as e:  # noqa: BLE001
        import errors
        errors.record("Automations", e)
        return f"Automations failed: {e}"
    finally:
        conn.close()
        STATE.update(running=False, last=now_iso(), message=" ".join(msgs))
        _lock.release()


def _claim_runner(st) -> bool:
    """Only one device runs automations against a shared database."""
    me = device_name()
    s = st.get_many(["automation_runner", "automation_runner_seen"])
    other, seen = s.get("automation_runner"), _parse(s.get("automation_runner_seen"))
    if other and other != me and seen and datetime.now(timezone.utc) - seen < timedelta(hours=2):
        return False
    st.set_many({"automation_runner": me, "automation_runner_seen": now_iso()})
    return True


def _local_midnight_utc():
    now = datetime.now().astimezone()
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return mid.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def relisted_today(conn) -> int:
    return conn.execute("""SELECT COUNT(*) FROM automation_log WHERE action='relist' AND result='ok' AND at >= ?""",
                        (_local_midnight_utc(),)).fetchone()[0]


def _run_relist(conn, client, rule, cfg, manual=False) -> str:
    hour = int(cfg.get("relist_hour", -1))
    if hour >= 0 and not manual and datetime.now().hour < hour:
        return f"Relist: waiting until {hour:02d}:00."
    cap = min(int(cfg.get("max_per_day") or cfg.get("max_per_run") or 50), 200)
    left = cap - relisted_today(conn)
    if left <= 0:
        return f"Relist: today's limit of {cap} reached."
    cands = [c for c in relist_candidates(conn, cfg) if c["due"]]
    if cfg.get("require_approval"):
        queued = 0
        approved = {r["legacy_item_id"] for r in conn.execute(
            "SELECT legacy_item_id FROM relist_queue WHERE status='approved'")}
        known = {r["legacy_item_id"] for r in conn.execute(
            "SELECT legacy_item_id FROM relist_queue WHERE status IN ('pending', 'rejected', 'approved')")}
        with write_lock():
            for c in cands:
                if c["skip"] or c["legacy_item_id"] in known:
                    continue
                conn.execute("""INSERT INTO relist_queue(legacy_item_id, title, price, new_price, created_at)
                                VALUES (?,?,?,?,?)""", (c["legacy_item_id"], c["title"], c["price"], c["new_price"],
                                                         now_iso()))
                queued += 1
            if queued:
                notify(conn, "automation", f"{queued} relist{'s' if queued != 1 else ''} waiting for your approval",
                       "Review them on the Automations page.", "/automations", key=f"queue-{now_iso()[:13]}")
            conn.commit()
        cands = [c for c in cands if c["legacy_item_id"] in approved]
        if not cands:
            return f"Relist: {queued} queued for approval."
    done = failed = skipped = 0
    for c in cands[:left]:
        lid = c["legacy_item_id"]
        if c["skip"]:
            skipped += 1
            with write_lock():
                if not conn.execute("""SELECT 1 FROM automation_log WHERE legacy_item_id=? AND result='skipped'
                                       AND at >= ?""", (lid, (datetime.now(timezone.utc) - timedelta(days=1))
                                                        .strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone():
                    _log(conn, rule["id"], lid, "relist", "skipped", c["skip"])
                conn.commit()
            continue
        try:
            if c["status"] != "relist_failed":
                if client:
                    client.end_listing(lid)
            if client:
                new_id = client.relist(lid, c["new_price"])
            else:
                new_id = f"{int(lid) + 7000000000}" if lid.isdigit() else lid + "-R"
        except EbayError as e:
            failed += 1
            with write_lock():
                ended = c["status"] == "relist_failed" or "EndFixedPriceItem" not in str(e)
                if ended:
                    conn.execute("UPDATE listings SET status='relist_failed' WHERE legacy_item_id=?", (lid,))
                    notify(conn, "issue", f"Relist failed: {c['title']}",
                           f"The listing was ended but eBay refused the relist: {str(e)[:250]}. "
                           "It will be retried on the next run.", f"/inventory/{c['item_id']}" if c['item_id'] else "",
                           key=f"relistfail-{lid}")
                _log(conn, rule["id"], lid, "relist", "error", str(e)[:500])
                conn.execute("UPDATE relist_queue SET status='failed' WHERE legacy_item_id=? AND status='approved'",
                             (lid,))
                add_event(conn, c["item_id"], "error", f"Relist failed: {str(e)[:200]}", lid)
                conn.commit()
            continue
        price = c["new_price"] or c["price"]
        with write_lock():
            conn.execute("UPDATE listings SET status='relisted', updated_at=? WHERE legacy_item_id=?",
                         (now_iso(), lid))
            conn.execute(
                """INSERT OR REPLACE INTO listings(legacy_item_id, item_id, title, sku, price, original_price,
                       currency, quantity, quantity_available, quantity_sold, listing_type, duration, start_time,
                       status, watch_count, image_url, url, promoted, relisted_from, relist_count, last_seen,
                       updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?, 'active', 0, ?, ?, 0, ?, ?, ?, ?)""",
                (new_id, c["item_id"], c["title"], c["sku"], price, c["original_price"] or c["price"],
                 c["currency"], c["quantity_available"] or c["quantity"], c["quantity_available"] or c["quantity"],
                 c["listing_type"], c["duration"], now_iso(), c["image_url"],
                 f"https://www.ebay.co.uk/itm/{new_id}", lid, (c["relist_count"] or 0) + 1, now_iso(), now_iso()))
            rule_row = conn.execute("SELECT * FROM listing_rules WHERE legacy_item_id=?", (lid,)).fetchone()
            if rule_row:
                conn.execute("INSERT OR REPLACE INTO listing_rules(legacy_item_id, bulk_buy_id, unit_cost) "
                             "VALUES (?,?,?)", (new_id, rule_row["bulk_buy_id"], rule_row["unit_cost"]))
            change = (f" at £{price:,.2f} (was £{c['price']:,.2f})" if c["new_price"]
                      else f" at £{price:,.2f}")
            conn.execute("UPDATE relist_queue SET status='done' WHERE legacy_item_id=? AND status='approved'", (lid,))
            add_event(conn, c["item_id"], "relisted", f"Relisted as #{new_id}{change}", new_id)
            _log(conn, rule["id"], lid, "relist", "ok", f"→ #{new_id}{change}")
            conn.commit()
        done += 1
    return f"Relist: {done} relisted, {failed} failed, {skipped} skipped."


def daily_digest(conn) -> None:
    """One notification per day summing up relists, kept up to date through the day."""
    since = _local_midnight_utc()
    rows = conn.execute("""SELECT a.result, a.detail, l.title FROM automation_log a
                           LEFT JOIN listings l ON l.legacy_item_id = a.legacy_item_id
                           WHERE a.action='relist' AND a.at >= ? AND a.result IN ('ok', 'error')""",
                        (since,)).fetchall()
    if not rows:
        return
    ok = [r for r in rows if r["result"] == "ok"]
    bad = [r for r in rows if r["result"] == "error"]
    body = f"{len(ok)} relisted" + (f", {len(bad)} failed" if bad else "") + "."
    if bad:
        body += " Failures: " + "; ".join(f"{(r['title'] or '')[:40]} — {(r['detail'] or '')[:80]}" for r in bad[:5])
    key = "digest-" + datetime.now().strftime("%Y-%m-%d")
    with write_lock():
        conn.execute("""INSERT INTO notifications(key, at, kind, title, body, link, read) VALUES (?,?,?,?,?,?,0)
                        ON CONFLICT(key) DO UPDATE SET body=excluded.body, at=excluded.at,
                        read = CASE WHEN notifications.body = excluded.body THEN notifications.read ELSE 0 END""",
                     (key, now_iso(), "automation", f"Relist digest for {datetime.now():%d %b}", body, "/automations"))
        conn.commit()


def _run_promote(conn, client, rule, cfg) -> str:
    if client and not client.can_promote():
        with write_lock():
            notify(conn, "issue", "Sign in again to allow promotion",
                   "Delayed promotion needs permission to manage Promoted Listings. "
                   "Sign out and sign in with eBay again.", "/automations", key="needs-marketing-scope")
            conn.commit()
        return "Promotion: needs you to sign in again."
    cands = [c for c in promote_candidates(conn, cfg) if c["due"]][: cfg["max_per_run"]]
    if not cands:
        return "Promotion: nothing due."
    rate = float(cfg["ad_rate"])
    if client:
        cid = client.ensure_campaign(cfg["campaign_name"], rate)
        results = client.add_ads(cid, [c["legacy_item_id"] for c in cands], rate)
    else:
        results = {c["legacy_item_id"]: None for c in cands}
    ok = bad = 0
    with write_lock():
        for c in cands:
            err = results.get(c["legacy_item_id"])
            if err:
                bad += 1
                _log(conn, rule["id"], c["legacy_item_id"], "promote", "error", err[:500])
            else:
                ok += 1
                conn.execute("UPDATE listings SET promoted=1 WHERE legacy_item_id=?", (c["legacy_item_id"],))
                add_event(conn, c["item_id"], "promoted",
                          f"Promoted at {rate:.1f}% after {c['unsold_days']} days unsold", c["legacy_item_id"])
                _log(conn, rule["id"], c["legacy_item_id"], "promote", "ok", f"{rate:.1f}% ad rate")
        if ok or bad:
            notify(conn, "automation", f"Promoted {ok} listing{'s' if ok != 1 else ''}"
                   + (f", {bad} failed" if bad else ""), "", "/automations", key=f"promrun-{now_iso()}")
        conn.commit()
    return f"Promotion: {ok} promoted, {bad} failed."
