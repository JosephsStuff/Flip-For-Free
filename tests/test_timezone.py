"""Time zone: picked up from your browser once, changeable in Settings, never overwritten automatically."""
import os
import time
from datetime import datetime

import pytest

import db
import tz


@pytest.fixture()
def keep_tz(demo_db):
    before = os.environ.get("TZ")
    db.Settings().set("timezone", None)
    yield
    db.Settings().set("timezone", None)
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def test_validation():
    assert tz.valid("Europe/London") and tz.valid("America/New_York") and tz.valid("Australia/Sydney")
    for bad in (None, "", "Mars/Olympus", "../../etc/passwd", "/etc/localtime", "x" * 80):
        assert not tz.valid(bad)


def test_saving_switches_the_clock(keep_tz):
    tz.save("Pacific/Auckland")
    nz = datetime.now().utcoffset() if datetime.now().tzinfo else time.localtime().tm_gmtoff
    tz.save("America/Los_Angeles")
    la = time.localtime().tm_gmtoff
    assert nz != la and la < 0 and tz.current() == "America/Los_Angeles"


def test_browser_sets_it_once_and_settings_can_change_it(keep_tz, client):
    import app as flip
    flip.app.config["DEMO"] = False
    with client.session_transaction() as s:
        s.update(user_id="demo", db_path=db.get_db_path())
    try:
        assert "Intl.DateTimeFormat" in client.get("/").get_data(as_text=True)          # asks the browser
        r = client.post("/settings/timezone", json={"timezone": "America/Chicago", "auto": True})
        assert r.get_json()["timezone"] == "America/Chicago"
        assert "Intl.DateTimeFormat" not in client.get("/").get_data(as_text=True)      # only once
        client.post("/settings/timezone", json={"timezone": "Europe/Paris", "auto": True})
        assert tz.saved() == "America/Chicago"                                          # auto never overwrites
        client.post("/settings/timezone", data={"timezone": "Europe/London"})
        assert tz.saved() == "Europe/London"                                            # your choice does
        assert client.post("/settings/timezone", json={"timezone": "Nowhere/Land"}).status_code == 400
    finally:
        flip.app.config["DEMO"] = True
