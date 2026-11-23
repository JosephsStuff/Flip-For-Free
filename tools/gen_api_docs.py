"""Regenerate the API docs from the running code: python tools/gen_api_docs.py

Seeds a throwaway demo database, creates an API key, calls every endpoint with a real request, and writes
  docs/openapi.json   (OpenAPI 3.1, examples included)
  docs/api-examples.json (the request/response pairs used on docs/api.html)
so the published docs always match what the API actually returns. Long lists are cut to two entries.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["EPT_CONFIG_DIR"] = tempfile.mkdtemp(prefix="flip-apidocs-")

import db  # noqa: E402
import demo  # noqa: E402

DB = os.path.join(os.environ["EPT_CONFIG_DIR"], "demo.db")
demo.seed(DB)
db.set_db_path(DB)

import api  # noqa: E402
import app as flip  # noqa: E402

flip.app.config.update(DEMO=True, SECRET_KEY="docs", TESTING=True)
KEY = api.create_key("docs")
client = flip.app.test_client()
H = {"Authorization": f"Bearer {KEY}"}


def trim(v, depth=0):
    if isinstance(v, list):
        out = [trim(x, depth + 1) for x in v[:2]]
        return out + ([f"… {len(v) - 2} more"] if len(v) > 2 else [])
    if isinstance(v, dict):
        return {k: trim(x, depth + 1) for k, x in v.items()}
    return v


def call(method, path, body=None):
    r = client.open("/api/v1" + path, method=method, headers=H, json=body)
    try:
        data = r.get_json()
    except Exception:  # noqa: BLE001
        data = None
    return r.status_code, data


conn = db.connect()
sale_id = conn.execute("SELECT line_item_id FROM sales WHERE source='ebay' ORDER BY sold_at DESC").fetchone()[0]
item_id = conn.execute("SELECT id FROM items WHERE archived=0 ORDER BY id").fetchone()[0]
bulk_id = conn.execute("SELECT id FROM bulk_buys ORDER BY id").fetchone()[0]
listing = conn.execute("SELECT legacy_item_id FROM listings WHERE status='active'").fetchone()[0]
conn.close()

EXAMPLES = [
    ("GET", "", None, "What the API offers, as JSON."),
    ("GET", "/summary?period=30d", None, "Profit for the last 30 days."),
    ("GET", "/time-to-sell?period=90d", None, None),
    ("GET", "/sales?period=30d&limit=2", None, None),
    ("GET", f"/sales/{sale_id}", None, None),
    ("PATCH", f"/sales/{sale_id}", {"cost": 4.5, "packaging_cost": 0.3}, None),
    ("POST", "/sales", {"title": "Zara wool coat", "item_price": 28, "channel": "Vinted", "sold_at": "2026-11-22",
                        "postage_cost": 0, "cost": 6}, None),
    ("GET", "/items?status=instock", None, None),
    ("GET", f"/items/{item_id}", None, None),
    ("POST", "/items", {"titles": ["Pot of Greed LOB-119", "Mirror Force MRD-138"], "cost": 1.5, "source": "Car boot",
                        "location": "Box 3"}, "Several items in one call."),
    ("PATCH", f"/items/{item_id}", {"location": "Shelf B", "cost": 2.0}, None),
    ("GET", "/bulk-buys", None, None),
    ("POST", "/bulk-buys", {"name": "Binder of 40 Yu-Gi-Oh cards", "total_cost": 60, "source": "Car boot",
                            "purchased_at": "2026-11-21", "items": ["Dark Magician SDY-006", "Exodia LOB-124"]}, None),
    ("PATCH", f"/bulk-buys/{bulk_id}", {"notes": "Bought at Barras market"}, None),
    ("POST", f"/bulk-buys/{bulk_id}/link-listings", {"listing_ids": [listing]}, None),
    ("GET", "/listings?status=active", None, None),
    ("GET", "/expenses?year=2026", None, None),
    ("POST", "/expenses", {"date": "2026-11-20", "category": "office", "description": "Bubble mailers x50",
                           "amount": 7.99}, None),
    ("POST", "/mileage", {"date": "2026-11-20", "miles": 3, "purpose": "Post office", "return_trip": True}, None),
    ("GET", "/tax?year=2026", None, None),
    ("GET", "/notifications?unread=1", None, None),
    ("POST", f"/items/{item_id}/write-off", {"reason": "Water damaged", "date": "2026-11-23"}, None),
    ("POST", "/sync", None, "Starts a sync in the background (in demo mode it just says sync is off)."),
]

out = []
for method, path, body, note in EXAMPLES:
    status, data = call(method, path, body)
    out.append({"method": method, "path": "/api/v1" + path, "body": body, "status": status,
                "response": trim(data), "note": note})
    print(f"{status} {method} /api/v1{path}")

# OpenAPI from the endpoint catalogue + the real examples
paths = {}
for m, p, does in api.ENDPOINTS:
    oa_path = "/api/v1" + p.replace("<line_item_id>", "{line_item_id}").replace("<id>", "{id}")
    op = {"summary": does.split(". ")[0][:120], "description": does,
          "responses": {"200": {"description": "OK"}, "400": {"description": "Bad input: {\"error\": \"…\"}"},
                        "401": {"description": "Missing or wrong API key"},
                        "409": {"description": "Would change a tax year marked as filed (send confirm_closed: true)"}}}
    params = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
              for n in ("line_item_id", "id") if "{" + n + "}" in oa_path]
    if params:
        op["parameters"] = params
    for ex in out:
        base = ex["path"].split("?")[0]
        if ex["method"] == m and (base == oa_path or (params and base.rsplit("/", 1)[0] == oa_path.rsplit("/", 1)[0]
                                                      and oa_path.count("/") == base.count("/"))):
            if ex["body"] is not None:
                op["requestBody"] = {"content": {"application/json": {"example": ex["body"]}}}
            op["responses"][str(ex["status"])] = {"description": "Example", "content": {
                "application/json": {"example": ex["response"]}}}
            break
    paths.setdefault(oa_path, {})[m.lower()] = op

spec = {"openapi": "3.1.0",
        "info": {"title": "Flip for Free API", "version": (ROOT / "VERSION").read_text().strip(),
                 "description": api.__doc__.strip()},
        "servers": [{"url": "https://localhost:8765", "description": "Your own copy (local)"},
                    {"url": "https://flip.your-tailnet.ts.net", "description": "Your own server"}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                                      "description": "A key from Settings → API keys (flip_…)"}}},
        "security": [{"bearer": []}], "paths": paths}
(ROOT / "docs" / "openapi.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False))
(ROOT / "docs" / "api-examples.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print("wrote docs/openapi.json and docs/api-examples.json")
