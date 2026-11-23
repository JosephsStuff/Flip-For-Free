"""Things that must never go wrong: secrets in error reports, and a long sync locking everyone out."""
import json
import threading
import time

import db
import errors


def test_scrub_removes_personal_details_and_secrets(demo_db):
    db.Settings().set_many({"ct_token": "s3cr3t-cardtrader-token-value", "shopify_client_secret": "plain-secret-123"})
    raw = ("joe@example.com Bearer abc.def flip_abcdefghijklmnop shpat_0123456789abcdef tskey-auth-kABC123 "
           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl G72 8YZ s3cr3t-cardtrader-token-value plain-secret-123")
    out = errors.scrub(raw)
    for leak in ["joe@example.com", "abc.def", "abcdefghijklmnop", "0123456789abcdef", "kABC123", "eyJhbGci",
                 "G72 8YZ", "s3cr3t-cardtrader-token-value", "plain-secret-123"]:
        assert leak not in out, (leak, out)


def test_slow_sync_doesnt_lock_the_database(demo_db, monkeypatch):
    """Other pages must still be able to write while a sync waits on eBay (it used to say 'database is locked')."""
    import automation
    import sync
    conn = db.connect()
    orders = [json.loads(r[0]) for r in conn.execute("SELECT raw FROM orders LIMIT 4")]
    conn.close()

    class SlowEbay:
        def __init__(self, *a, **k): pass

        def iter_orders(self, since, by_modified):
            for o in orders:
                time.sleep(0.6)
                yield o

        def iter_transactions(self, *a): return iter([])

        def shipped_date(self, *a): return None

        def iter_active_listings(self): return iter([])

    monkeypatch.setattr(sync, "EbayClient", SlowEbay)
    t = threading.Thread(target=sync.run_sync)
    t.start()
    time.sleep(0.3)
    writes = 0
    while t.is_alive():
        c = db.connect()
        c.execute("PRAGMA busy_timeout=1500")
        c.execute("DELETE FROM automation_rules")
        automation.ensure_rules(c)
        c.close()
        writes += 1
        time.sleep(0.3)
    t.join()
    assert writes >= 3
