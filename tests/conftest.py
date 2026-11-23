"""Shared fixtures: every test gets a fresh demo database (a year of fake sales, stock and bulk buys)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EPT_CONFIG_DIR", tempfile.mkdtemp(prefix="flip-tests-"))

import db  # noqa: E402
import demo  # noqa: E402


@pytest.fixture()
def demo_db(tmp_path):
    path = str(tmp_path / "flip.db")
    demo.seed(path)
    db.set_db_path(path)
    return path


@pytest.fixture()
def flask_app(demo_db):
    import app as flip
    flip.app.config.update(DEMO=True, SECRET_KEY="tests", TESTING=True, PUBLIC_URL="", LAN=False)
    yield flip.app
    flip.app.config.update(DEMO=True, PUBLIC_URL="", LAN=False)


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture()
def conn(demo_db):
    c = db.connect()
    yield c
    c.close()
