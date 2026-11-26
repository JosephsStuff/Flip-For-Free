"""Every page renders, every API endpoint answers, and access rules hold."""
import pytest

import api

PAGES = ["/", "/sales", "/inventory", "/bulk", "/insights", "/performance", "/automations", "/pnl", "/tax",
         "/expenses", "/mileage", "/notifications", "/settings", "/tools", "/prices", "/import", "/sales/new",
         "/phone", "/settings/shops", "/inventory/1", "/inventory/1/vinted", "/inventory/1/cardtrader",
         "/inventory/1/shopify"]


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)


@pytest.fixture()
def auth(flask_app):
    return {"Authorization": f"Bearer {api.create_key('tests')}"}


def test_api_needs_a_key(client):
    assert client.get("/api/v1/summary").status_code == 401
    assert client.get("/api/v1/summary", headers={"Authorization": "Bearer flip_wrong"}).status_code == 401


@pytest.mark.parametrize("path", ["", "/summary?period=all", "/time-to-sell", "/sales?limit=5", "/items",
                                  "/bulk-buys", "/listings", "/expenses", "/tax", "/notifications"])
def test_api_reads(client, auth, path):
    r = client.get("/api/v1" + path, headers=auth)
    assert r.status_code == 200, r.get_json()


def test_api_records_a_sale(client, auth):
    r = client.post("/api/v1/sales", headers=auth, json={"title": "Coat", "item_price": 28, "channel": "Vinted",
                                                          "cost": 6, "sold_at": "2026-11-20"})
    assert r.status_code == 201
    s = r.get_json()
    assert s["channel"] == "Vinted" and s["money_made"] == pytest.approx(28) and s["profit"] == pytest.approx(22)


def test_api_rejects_bad_input(client, auth):
    assert client.post("/api/v1/sales", headers=auth, json={"title": "x"}).status_code == 400
    assert client.post("/api/v1/expenses", headers=auth,
                       json={"category": "nonsense", "description": "x", "amount": 1}).status_code == 400


def test_lan_mode_blocks_unpaired_devices(flask_app, client):
    flask_app.config.update(DEMO=False, LAN=True)
    r = client.get("/", environ_base={"REMOTE_ADDR": "192.168.1.50"})
    assert r.status_code == 403


def test_server_mode_trusts_the_proxy(flask_app, client):
    """Behind Tailscale Serve every visitor arrives via the proxy with their own address: never 'Forbidden'."""
    flask_app.config.update(DEMO=False, PUBLIC_URL="https://flip.example.ts.net")
    r = client.get("/", environ_base={"REMOTE_ADDR": "100.70.0.50"})
    assert r.status_code != 403


def test_dismissed_notifications_stay_gone(client, conn):
    n = conn.execute("SELECT id, title FROM notifications WHERE COALESCE(dismissed, 0)=0 ORDER BY id DESC LIMIT 1").fetchone()
    assert n["title"] in client.get("/notifications").get_data(as_text=True)
    r = client.post(f"/notifications/{n['id']}/dismiss", headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"]
    row = conn.execute("SELECT dismissed, read FROM notifications WHERE id=?", (n["id"],)).fetchone()
    assert (row["dismissed"], row["read"]) == (1, 1)
    assert f'data-id="{n["id"]}"' not in client.get("/notifications").get_data(as_text=True)
