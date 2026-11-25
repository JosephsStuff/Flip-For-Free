"""Showcase mode: one browser sees fake data; the real database is hidden and never written to."""
import pytest

import db


@pytest.fixture()
def real(flask_app, conn):
    flask_app.config["DEMO"] = False
    conn.execute("UPDATE sales SET title='MY-REAL-ITEM' WHERE line_item_id=(SELECT line_item_id FROM sales LIMIT 1)")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('marketplace_id', 'EBAY_GB')")
    conn.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(user_id="demo", db_path=db.get_db_path())
    yield c, db.get_db_path()
    flask_app.config["DEMO"] = True


def page(c, url):
    return c.get(url).get_data(as_text=True)


def test_showcase_hides_real_data_and_never_writes_to_it(real):
    c, real_path = real
    assert "MY-REAL-ITEM" in page(c, "/sales?period=all")
    assert c.post("/showcase/on").status_code == 302
    shown = page(c, "/sales?period=all")
    assert "MY-REAL-ITEM" not in shown and 'class="tag showcase"' in shown
    c.post("/settings/market", data={"marketplace_id": "EBAY_US", "currency": ""})   # a change while showcasing
    assert "$" in page(c, "/pnl")
    assert db.get_db_path() == real_path                                              # outside the request: yours
    assert db.Settings(real_path).get("marketplace_id") == "EBAY_GB"                  # untouched
    assert c.get("/setup").status_code == 302                                          # setup is off-limits
    c.post("/showcase/off")
    back = page(c, "/sales?period=all")
    assert "MY-REAL-ITEM" in back and 'class="tag showcase"' not in back


def test_other_browsers_keep_seeing_real_data(real, flask_app):
    c, _ = real
    c.post("/showcase/on")
    other = flask_app.test_client()
    with other.session_transaction() as s:
        s.update(user_id="demo", db_path=db.get_db_path())
    assert "MY-REAL-ITEM" in page(other, "/sales?period=all")


def test_signing_out_ends_showcase(real):
    c, _ = real
    c.post("/showcase/on")
    with c.session_transaction() as s:
        s.pop("user_id")
    c.get("/")
    with c.session_transaction() as s:
        assert "showcase" not in s
