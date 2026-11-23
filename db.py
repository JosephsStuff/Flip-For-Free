"""Storage layer.

Two kinds of storage:
  * A tiny per-device config file (in the OS app-data folder) that only remembers
    WHERE the database lives plus a per-device secret. Nothing else.
  * The SQLite database itself, which you can put in any folder - e.g. a Google
    Drive / OneDrive / Dropbox synced folder. Everything (sales, costs, bulk buys,
    eBay app keys, the eBay refresh token) lives in the database, so pointing a
    second device at the same file gives you the same data.
"""
import json
import os
import secrets
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "eBayProfitTracker"
DB_FILENAME = "ebay_profit_tracker.db"
SCHEMA_VERSION = 2

_write_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# Per-device config
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    override = os.environ.get("EPT_CONFIG_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    p = _config_path()
    cfg = {}
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    changed = False
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        changed = True
    cfg.setdefault("recent_db_paths", [])
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    p = _config_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(p)


def get_db_path() -> str | None:
    path = load_config().get("db_path")
    return path if path else None


def set_db_path(path: str) -> None:
    cfg = load_config()
    cfg["db_path"] = path
    recent = [p for p in cfg.get("recent_db_paths", []) if p != path]
    cfg["recent_db_paths"] = [path] + recent[:4]
    save_config(cfg)


def device_name() -> str:
    return socket.gethostname() or "this device"


def suggested_cloud_folders() -> list[dict]:
    """Best-effort list of synced cloud folders on this machine."""
    home = Path.home()
    found = []
    candidates = []
    if sys.platform == "win32":
        for letter in "GHIJKLMNOPQRSTUVWXYZ":
            candidates.append(("Google Drive", Path(f"{letter}:/My Drive")))
        candidates.append(("Google Drive", home / "Google Drive"))
        candidates.append(("OneDrive", Path(os.environ.get("OneDrive", home / "OneDrive"))))
        candidates.append(("Dropbox", home / "Dropbox"))
    else:
        cloud = home / "Library" / "CloudStorage"
        if cloud.exists():
            for child in sorted(cloud.iterdir()):
                if child.name.startswith("GoogleDrive"):
                    my = child / "My Drive"
                    candidates.append(("Google Drive", my if my.exists() else child))
                elif child.name.startswith("OneDrive"):
                    candidates.append(("OneDrive", child))
                elif child.name.startswith("Dropbox"):
                    candidates.append(("Dropbox", child))
        candidates.append(("Google Drive", home / "Google Drive"))
        candidates.append(("Google Drive", home / "GoogleDrive"))
        candidates.append(("Dropbox", home / "Dropbox"))
    seen = set()
    for label, p in candidates:
        try:
            if p.exists() and p.is_dir() and str(p) not in seen:
                seen.add(str(p))
                found.append({"label": label, "path": str(p)})
        except OSError:
            pass
    found.append({"label": "Home folder", "path": str(home)})
    return found


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    created_at      TEXT,
    buyer           TEXT,
    payment_status  TEXT,
    cancel_state    TEXT,
    currency        TEXT,
    subtotal        REAL,
    delivery        REAL,
    total           REAL,
    marketplace_fee REAL,
    raw             TEXT,
    updated_at      TEXT
);

-- One row per eBay order line item (= one sale). User-entered fields are
-- never overwritten by a sync.
CREATE TABLE IF NOT EXISTS sales (
    line_item_id     TEXT PRIMARY KEY,
    order_id         TEXT,
    legacy_item_id   TEXT,
    sku              TEXT,
    title            TEXT,
    quantity         INTEGER DEFAULT 1,
    item_price       REAL DEFAULT 0,
    delivery_charged REAL DEFAULT 0,
    currency         TEXT,
    sold_at          TEXT,
    source           TEXT DEFAULT 'ebay',
    -- user-entered
    cost             REAL,
    postage_cost     REAL,
    packaging_cost   REAL,
    other_cost       REAL,
    bulk_buy_id      INTEGER REFERENCES bulk_buys(id) ON DELETE SET NULL,
    notes            TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_order ON sales(order_id);
CREATE INDEX IF NOT EXISTS idx_sales_sold ON sales(sold_at);
CREATE INDEX IF NOT EXISTS idx_sales_bulk ON sales(bulk_buy_id);
CREATE INDEX IF NOT EXISTS idx_sales_item ON sales(legacy_item_id);

-- Money movements from the eBay Finances API (fees, refunds, labels, ads...).
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT,
    type           TEXT,
    order_id       TEXT,
    booking        TEXT,       -- CREDIT / DEBIT
    amount         REAL,       -- always positive; sign comes from booking
    currency       TEXT,
    fee_type       TEXT,
    total_fee      REAL,
    fee_basis      REAL,
    memo           TEXT,
    date           TEXT,
    line_fees      TEXT,       -- JSON {lineItemId: [{feeType, amount}]}
    refs           TEXT,       -- JSON list of references
    raw            TEXT,
    PRIMARY KEY (transaction_id, type)
);
CREATE INDEX IF NOT EXISTS idx_tx_order ON transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);

CREATE TABLE IF NOT EXISTS bulk_buys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    purchased_at TEXT,
    total_cost   REAL DEFAULT 0,
    extra_costs  REAL DEFAULT 0,
    item_count   INTEGER,
    notes        TEXT,
    created_at   TEXT
);

-- "Every sale of this eBay listing belongs to bulk buy X / costs Y each".
CREATE TABLE IF NOT EXISTS listing_rules (
    legacy_item_id TEXT PRIMARY KEY,
    bulk_buy_id    INTEGER REFERENCES bulk_buys(id) ON DELETE CASCADE,
    unit_cost      REAL
);

-- Inventory: one row per thing you bought (can be quantity > 1).
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    sku          TEXT,
    quantity     INTEGER DEFAULT 1,
    cost         REAL,            -- unit cost; for bulk items only used when cost_locked=1
    cost_locked  INTEGER DEFAULT 0,
    source       TEXT,            -- where you bought it
    purchased_at TEXT,
    location     TEXT,            -- where it's stored
    bulk_buy_id  INTEGER REFERENCES bulk_buys(id) ON DELETE SET NULL,
    notes        TEXT,
    archived     INTEGER DEFAULT 0,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_bulk ON items(bulk_buy_id);

-- Your eBay listings (active and ended), from the Trading API.
CREATE TABLE IF NOT EXISTS listings (
    legacy_item_id     TEXT PRIMARY KEY,
    item_id            INTEGER REFERENCES items(id) ON DELETE SET NULL,
    title              TEXT,
    sku                TEXT,
    price              REAL,
    original_price     REAL,
    currency           TEXT,
    quantity           INTEGER,
    quantity_available INTEGER,
    quantity_sold      INTEGER DEFAULT 0,
    listing_type       TEXT,
    duration           TEXT,
    start_time         TEXT,
    end_time           TEXT,
    status             TEXT,      -- active / ended / sold
    watch_count        INTEGER,
    image_url          TEXT,
    url                TEXT,
    promoted           INTEGER DEFAULT 0,
    relisted_from      TEXT,
    relist_count       INTEGER DEFAULT 0,
    last_seen          TEXT,
    updated_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_item ON listings(item_id);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);

-- Product timeline
-- eBay listing details fetched on demand for copying a listing to another marketplace
CREATE TABLE IF NOT EXISTS listing_details (
    legacy_item_id TEXT PRIMARY KEY,
    data           TEXT NOT NULL,       -- JSON from EbayClient.get_item_details
    fetched_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        INTEGER,
    legacy_item_id TEXT,
    at             TEXT,
    kind           TEXT,
    detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_item ON item_events(item_id);

CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT,
    category    TEXT,
    description TEXT,
    amount      REAL,
    item_id     INTEGER REFERENCES items(id) ON DELETE SET NULL,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS mileage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT,
    miles      REAL,
    vehicle    TEXT DEFAULT 'car',
    purpose    TEXT,
    route      TEXT,
    created_at TEXT
);

-- Places you drop parcels off (post office, InPost locker, Collect+ shop...)
CREATE TABLE IF NOT EXISTS drop_off_locations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    postcode      TEXT,
    miles_one_way REAL,
    distance_note TEXT,
    is_default    INTEGER DEFAULT 0,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key       TEXT UNIQUE,
    at        TEXT,
    kind      TEXT,     -- sale / issue / automation / info
    title     TEXT,
    body      TEXT,
    link      TEXT,
    read      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS automation_rules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT,     -- relist / promote
    name     TEXT,
    enabled  INTEGER DEFAULT 0,
    config   TEXT,
    last_run TEXT
);

CREATE TABLE IF NOT EXISTS automation_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT,
    rule_id        INTEGER,
    legacy_item_id TEXT,
    action         TEXT,
    result         TEXT,    -- ok / error / skipped
    detail         TEXT
);

-- Relists waiting for your approval (when "review before relisting" is on)
CREATE TABLE IF NOT EXISTS relist_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_item_id TEXT,
    title          TEXT,
    price          REAL,
    new_price      REAL,
    created_at     TEXT,
    status         TEXT DEFAULT 'pending'   -- pending / approved / rejected / done / failed
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    message     TEXT,
    device      TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or get_db_path()
    if not path:
        raise RuntimeError("No database location has been chosen yet.")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # DELETE journal (not WAL) so the database is always a single file -
    # cloud sync tools handle that far better than WAL's side files.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after version 1 (applied to existing databases on open).
MIGRATIONS = {
    "sales": [("item_id", "INTEGER"), ("channel", "TEXT DEFAULT 'eBay'"), ("status", "TEXT"),
              ("ship_country", "TEXT"), ("ship_outcode", "TEXT"), ("ship_city", "TEXT"),
              ("manual_fees", "REAL"), ("shipped_at", "TEXT"), ("handling_cost", "REAL")],
    "mileage": [("dispatch_date", "TEXT"), ("drop_off_id", "INTEGER")],
    "bulk_buys": [("source", "TEXT"), ("location", "TEXT"), ("split_method", "TEXT DEFAULT 'even'")],
    "items": [("restock_url", "TEXT"), ("packaging_cost", "REAL"), ("handling_cost", "REAL"), ("written_off_at", "TEXT"), ("write_off_reason", "TEXT"), ("relist_exclude", "INTEGER DEFAULT 0"),
              ("relist_floor", "REAL"), ("vinted_listed_at", "TEXT"), ("vinted_url", "TEXT")],
    "expenses": [("receipt_path", "TEXT")],
}


def _migrate(conn) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_itemid ON sales(item_id)")


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        conn = connect(path)
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('created_at', ?)",
                (now_iso(),),
            )
            conn.commit()
        finally:
            conn.close()


def is_tracker_db(path: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


class Settings:
    """Key/value settings stored inside the database (so they travel with it)."""

    def __init__(self, path: str | None = None):
        self.path = path

    def get(self, key, default=None):
        conn = connect(self.path)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row and row["value"] is not None else default
        finally:
            conn.close()

    def get_many(self, keys) -> dict:
        conn = connect(self.path)
        try:
            q = "SELECT key, value FROM settings WHERE key IN (%s)" % ",".join("?" * len(keys))
            return {r["key"]: r["value"] for r in conn.execute(q, list(keys))}
        finally:
            conn.close()

    def set(self, key, value) -> None:
        self.set_many({key: value})

    def set_many(self, values: dict) -> None:
        with _write_lock:
            conn = connect(self.path)
            try:
                for k, v in values.items():
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, None if v is None else str(v)),
                    )
                conn.commit()
            finally:
                conn.close()


def write_lock():
    return _write_lock


_initialised = set()


def ensure_schema(path: str | None = None) -> None:
    """Upgrade an existing database once per run (cheap no-op afterwards)."""
    path = path or get_db_path()
    if path and path not in _initialised and Path(path).exists():
        init_db(path)
        _initialised.add(path)


def add_event(conn, item_id, kind, detail, legacy_item_id=None, at=None):
    conn.execute("INSERT INTO item_events(item_id, legacy_item_id, at, kind, detail) VALUES (?,?,?,?,?)",
                 (item_id, legacy_item_id, at or now_iso(), kind, detail))


def notify(conn, kind, title, body="", link="", key=None, at=None):
    """Add a notification; `key` de-duplicates (same key is only stored once)."""
    conn.execute("INSERT OR IGNORE INTO notifications(key, at, kind, title, body, link) VALUES (?,?,?,?,?,?)",
                 (key, at or now_iso(), kind, title, body, link))
