"""Update channels: main release and beta."""
import io
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
    monkeypatch.setattr(updater.requests, "get", lambda url, timeout: asked.append(url) or
                        Resp(200, "1.7.9" if url.endswith("VERSION") else "## 1.7.9\n- fixes\n"))
    updater.check()
    assert "/main/VERSION" in asked[0]
    assert updater.switching() and updater.update_available()          # 1.7.9 < 1.8.0-beta.1, but it's a switch


def test_beta_channel_checks_and_installs_the_beta_branch(chan, monkeypatch):
    db.Settings().set("update_channel", "beta")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Flip-For-Free-beta/app.py", "")
    urls = []

    def get(url, timeout):
        urls.append(url)
        return Resp(200, "1.8.0-beta.2") if url.endswith("VERSION") else Resp(200, content=buf.getvalue())

    monkeypatch.setattr(updater.requests, "get", get)
    monkeypatch.setattr(updater, "_snapshot_app", lambda: None)
    monkeypatch.setattr(updater, "APP_DIR", chan / "app")
    (chan / "app").mkdir()
    updater.check()
    assert "/beta/VERSION" in urls[0] and updater.update_available()
    updater.apply_update()
    assert urls[-1].endswith("/zip/refs/heads/beta") and updater.installed_channel() == "beta"
    assert not updater.switching()


def test_channel_setting_page(client):
    r = client.get("/settings")
    assert b"Release channel" in r.data and b'value="beta"' in r.data
