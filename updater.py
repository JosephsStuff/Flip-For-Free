"""Update Flip for Free from GitHub without touching your data.

Your data never lives in the app folder: the database (and receipts) are in the folder you chose on
the setup page, and settings are in your user config folder. Updating only replaces the app's code.

How it updates:
  * if the app folder is a git clone  -> `git pull --ff-only`
  * otherwise                         -> download the latest zip from GitHub and copy it over the app
Before every update it saves a backup of your database and a copy of the current app version.
Then it restarts itself (works under run.bat / run.command / run.sh, systemd on a server, or plain python).
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests

import db

APP_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = "JosephsStuff/flip-for-free"
BRANCH = "main"
KEEP = {".git", ".venv", "venv", "__pycache__"}          # never overwritten or removed
STATE = {"checking": False, "latest": None, "notes": None, "checked_at": None, "error": None, "updating": False}


def current_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def repo() -> str:
    try:
        return db.Settings().get("update_repo") or DEFAULT_REPO
    except Exception:
        return DEFAULT_REPO


def _vtuple(v):
    out = []
    for p in str(v).strip().lstrip("v").split("."):
        try:
            out.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
        except ValueError:
            out.append(0)
    return tuple(out + [0] * (3 - len(out)))


def is_newer(latest, current=None) -> bool:
    return bool(latest) and _vtuple(latest) > _vtuple(current or current_version())


def check() -> dict:
    """Ask GitHub for the latest version number and its notes."""
    STATE.update(checking=True, error=None)
    try:
        base = f"https://raw.githubusercontent.com/{repo()}/{BRANCH}"
        r = requests.get(f"{base}/VERSION", timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"GitHub replied {r.status_code} for {repo()}")
        latest = r.text.strip()
        notes = None
        c = requests.get(f"{base}/CHANGELOG.md", timeout=20)
        if c.status_code == 200:
            notes = _section(c.text, latest)
        STATE.update(latest=latest, notes=notes, checked_at=datetime.now().isoformat(timespec="minutes"))
    except Exception as e:  # noqa: BLE001
        STATE["error"] = f"Couldn't check for updates: {e}"
    finally:
        STATE["checking"] = False
    return STATE


def _section(changelog: str, version: str) -> str | None:
    """The CHANGELOG entry for `version` (lines between its '## ' heading and the next)."""
    out, on = [], False
    for line in changelog.splitlines():
        if line.startswith("## "):
            if on:
                break
            on = version in line
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip() or None


# --------------------------------------------------------------------------- #
# Updating
# --------------------------------------------------------------------------- #
def backup_database(reason="before-update") -> Path | None:
    """Consistent copy of the database into <database folder>/backups."""
    import sqlite3
    p = db.get_db_path()
    if not p or not Path(p).exists():
        return None
    out_dir = Path(p).parent / "backups"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"flip-{reason}-{datetime.now():%Y-%m-%d_%H%M%S}.db"
    src, dst = sqlite3.connect(p), sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return out


def _snapshot_app() -> Path:
    d = db.config_dir() / "updates"
    d.mkdir(exist_ok=True)
    out = d / f"flip-for-free-{current_version()}-{datetime.now():%Y%m%d%H%M%S}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in APP_DIR.rglob("*"):
            rel = f.relative_to(APP_DIR)
            if f.is_file() and not (set(rel.parts) & KEEP):
                z.write(f, rel)
    # keep the 3 newest
    for old in sorted(d.glob("flip-for-free-*.zip"))[:-3]:
        old.unlink(missing_ok=True)
    return out


def apply_update() -> str:
    """Download and install the latest version. Returns a message. Restart separately."""
    if STATE["updating"]:
        return "An update is already running."
    STATE["updating"] = True
    try:
        before_req = (APP_DIR / "requirements.txt").read_text() if (APP_DIR / "requirements.txt").exists() else ""
        backup = backup_database()
        _snapshot_app()
        if (APP_DIR / ".git").exists() and shutil.which("git"):
            r = subprocess.run(["git", "-C", str(APP_DIR), "pull", "--ff-only"], capture_output=True, text=True,
                               timeout=180)
            if r.returncode != 0:
                raise RuntimeError("git pull failed: " + (r.stderr or r.stdout)[-400:])
        else:
            url = f"https://codeload.github.com/{repo()}/zip/refs/heads/{BRANCH}"
            r = requests.get(url, timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"Download failed ({r.status_code}) from {url}")
            with zipfile.ZipFile(io.BytesIO(r.content)) as z, tempfile.TemporaryDirectory() as tmp:
                z.extractall(tmp)
                roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
                if len(roots) != 1 or not (roots[0] / "app.py").exists():
                    raise RuntimeError("The download doesn't look like Flip for Free.")
                src = roots[0]
                for f in src.rglob("*"):
                    rel = f.relative_to(src)
                    if set(rel.parts) & KEEP:
                        continue
                    dest = APP_DIR / rel
                    if f.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest)
        after_req = (APP_DIR / "requirements.txt").read_text() if (APP_DIR / "requirements.txt").exists() else ""
        if after_req != before_req:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(APP_DIR / "requirements.txt")],
                           capture_output=True, timeout=600)
        return (f"Updated to version {current_version()}. Your data was backed up first"
                + (f" ({backup.name})" if backup else "") + ". Restarting…")
    finally:
        STATE["updating"] = False


def restart(delay=1.5):
    """Restart the app after the current response has been sent."""
    def _go():
        time.sleep(delay)
        if os.environ.get("INVOCATION_ID") or os.environ.get("FLIP_LAUNCHER"):
            # systemd (server) or our run.bat / run.command / run.sh loop restarts us
            os._exit(3)
        env = dict(os.environ, FLIP_START_DELAY="2")
        subprocess.Popen([sys.executable] + sys.argv, cwd=str(APP_DIR), env=env)
        os._exit(0)
    threading.Thread(target=_go, daemon=True).start()
