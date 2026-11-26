"""Update channels: main release and beta."""
import io
import json
import zipfile

import pytest

import db
import updater


@pytest.mark.parametrize("newer,older", [("1.8.0", "1.8.0-beta.2"), ("1.8.0-beta.2", "1.8.0-beta.1"),
                                         ("1.8.0-beta.1", "1.7.6"), ("1.7.10", "1.7.9"), ("v2.0.0", "1.99.99")])
def test_version_order(newer, older):
    assert updater.is_newer(newer, older) and not updater.is_newer(older, newer)


@pytest.fixture()
def chan(demo_db, tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "CHANNEL_FILE", tmp_path / ".flip-channel")
    monkeypatch.setattr(updater, "current_version", lambda: "1.8.0-beta.1")
    return tmp_path


class Resp:
    def __init__(self, status, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content


def test_switching_back_to_main_offers_the_lower_version(chan, monkeypatch):
    (chan / ".flip-channel").write_text("beta")
    db.Settings().set("update_channel", "main")
    asked = []
    monkeypatch.setattr(updater.requests, "get", lambda url, timeout, params=None, headers=None: asked.append((url, params)) or
                        Resp(200, "1.7.9" if url.endswith("VERSION") else "## 1.7.9\n- fixes\n"))
    updater.check()
    assert asked[0] == (f"https://api.github.com/repos/{updater.repo()}/contents/VERSION", {"ref": "main"})
    assert updater.switching() and updater.update_available()          # 1.7.9 < 1.8.0-beta.1, but it's a switch


def test_beta_channel_checks_and_installs_the_beta_branch(chan, monkeypatch):
    db.Settings().set("update_channel", "beta")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Flip-For-Free-beta/app.py", "")
    urls = []

    def get(url, timeout, params=None, headers=None):
        urls.append((url, params))
        return Resp(200, "1.8.0-beta.2") if url.endswith("VERSION") else Resp(200, content=buf.getvalue())

    monkeypatch.setattr(updater.requests, "get", get)
    monkeypatch.setattr(updater, "_snapshot_app", lambda: None)
    monkeypatch.setattr(updater, "APP_DIR", chan / "app")
    (chan / "app").mkdir()
    updater.check()
    assert urls[0][1] == {"ref": "beta"} and updater.update_available()
    updater.apply_update()
    assert urls[-1][0].endswith("/zip/refs/heads/beta") and updater.installed_channel() == "beta"
    assert not updater.switching()


def test_channel_setting_page(client):
    r = client.get("/settings")
    assert b"Release channel" in r.data and b'value="beta"' in r.data


ALERTS = {"alerts": [
    {"id": "sec-2026-01", "level": "critical", "title": "Security fix: please update today",
     "body": "<script>alert(1)</script> A bug could expose your eBay token.", "fixed_in": {"main": "1.7.8", "beta": "1.8.0-beta.7"}},
    {"id": "old-one", "title": "Long fixed", "fixed_in": "1.0.0"},
    {"id": "expired", "title": "Gone", "fixed_in": "9.9.9", "expires": "2001-01-01"},
    {"id": "too-new", "title": "Only affects 1.9+", "fixed_in": "1.9.5", "since": "1.9.0"},
    {"id": "bad id!", "title": "ignored", "fixed_in": "9.9.9"},
]}


@pytest.fixture()
def alerts(chan, monkeypatch):
    monkeypatch.setattr(updater.requests, "get", lambda url, timeout, params=None, headers=None: Resp(
        200, json.dumps(ALERTS)) if url.endswith("ALERTS.json") and params == {"ref": "main"} else Resp(404))
    Resp.json = lambda self: json.loads(self.text)
    updater.fetch_alerts()
    return chan


def test_alerts_show_only_for_affected_versions(alerts, monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "1.7.7")
    assert [a["id"] for a in updater.active_alerts()] == ["sec-2026-01"]
    assert updater.active_alerts()[0]["fix"] == "1.7.8"
    assert updater.active_alerts("1.7.8") == []                       # fixed: banner goes away
    (alerts / ".flip-channel").write_text("beta")
    assert updater.active_alerts("1.8.0-beta.6")[0]["fix"] == "1.8.0-beta.7"
    assert updater.active_alerts("1.8.0-beta.7") == []


def test_alert_banner_is_plain_text_on_every_page(alerts, monkeypatch, client):
    monkeypatch.setattr(updater, "current_version", lambda: "1.7.7")
    import app as flip
    flip.app.config["DEMO"] = False
    with client.session_transaction() as sess:
        sess.update(user_id="demo", db_path=db.get_db_path())
    try:
        for url in ("/", "/sales"):
            page = client.get(url).get_data(as_text=True)
            assert "Security fix: please update today" in page and "<script>alert(1)</script>" not in page
            assert "&lt;script&gt;" in page and 'class="banner urgent critical"' in page
    finally:
        flip.app.config["DEMO"] = True


def test_no_alerts_file_means_no_banner(chan, monkeypatch):
    monkeypatch.setattr(updater.requests, "get", lambda url, timeout, params=None, headers=None: Resp(404))
    assert updater.fetch_alerts() == [] and updater.active_alerts() == []


def test_falls_back_to_raw_github_when_the_api_is_busy(chan, monkeypatch):
    def get(url, timeout, params=None, headers=None):
        if "api.github.com" in url:
            return Resp(403)                                   # hourly limit used up
        return Resp(200, "1.9.0" if url.endswith("main/VERSION") else "")
    monkeypatch.setattr(updater.requests, "get", get)
    assert updater.check()["latest"] == "1.9.0"


@pytest.mark.parametrize("newer,older", [("26.11.2026", "1.8.0-beta.10"), ("1.12.2026", "30.11.2026.3"),
                                         ("26.11.2026.2", "26.11.2026.1"), ("26.11.2026.1", "26.11.2026"),
                                         ("1.1.2027", "31.12.2026.9"), ("5.1.2027", "1.7.7")])
def test_dated_versions_sort_by_date(newer, older):
    assert updater.is_newer(newer, older) and not updater.is_newer(older, newer)


def test_changelog_finds_the_exact_version():
    log = "# Changelog\n\n## 26.11.2026.10\n- ten\n\n## 26.11.2026.1\n- one\n"
    assert updater._section(log, "26.11.2026.1") == "- one"
    assert updater._section(log, "26.11.2026.10") == "- ten"


def test_bump_version():
    import importlib.util
    from datetime import date
    spec = importlib.util.spec_from_file_location("bump", "tools/bump_version.py")
    bump = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bump)
    d = date(2026, 11, 26)
    assert bump.next_version("beta", "1.8.0-beta.10", d) == "26.11.2026.1"
    assert bump.next_version("beta", "26.11.2026.1", d) == "26.11.2026.2"
    assert bump.next_version("beta", "25.11.2026.4", d) == "26.11.2026.1"
    assert bump.next_version("main", "1.7.7", d) == "26.11.2026"
    assert bump.next_version("main", "26.11.2026", d) == "26.11.2026.1"
