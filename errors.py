"""Errors: a log file, a friendly error page, and a one-click GitHub issue that the user reviews before sending.

Nothing is ever sent automatically. "Report on GitHub" opens a pre-filled issue in the user's browser; they read
it, edit it and press Submit themselves (a free GitHub account is needed). Personal details are scrubbed first:
home-folder paths, email addresses, long tokens/keys, eBay user names and postcodes.
"""
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import traceback
import uuid
from collections import deque
from datetime import datetime
from urllib.parse import urlencode

import db

ISSUES_REPO = "JosephsStuff/Flip-For-Free"
log = logging.getLogger("flip")
RECENT = deque(maxlen=20)          # the last errors, newest last, for Settings → Report a problem
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Log file
# --------------------------------------------------------------------------- #
def log_path():
    d = db.config_dir() / "logs"
    d.mkdir(exist_ok=True)
    return d / "flip.log"


def setup_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    s = logging.StreamHandler(sys.stderr)
    s.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(s)


# --------------------------------------------------------------------------- #
# Scrubbing personal details
# --------------------------------------------------------------------------- #
_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(v\^1\.1#|Bearer\s+)[^\s'\"]+"), r"\1<token>"),
    (re.compile(r"\bflip_[A-Za-z0-9_-]{10,}"), "flip_<key>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "<long-id>"),
    (re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"), "<postcode>"),
    (re.compile(r"(?i)\b(user(?:name)?|seller|buyer)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), r"\1\2<name>"),
]


def scrub(text: str) -> str:
    if not text:
        return ""
    home = os.path.expanduser("~")
    if home and home not in ("/", "~"):
        text = text.replace(home, "~")
    text = re.sub(r"(?i)([A-Z]:\\Users\\)[^\\\s]+", r"\1<you>", text)
    text = re.sub(r"(/(?:home|Users)/)[^/\s]+", r"\1<you>", text)
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return text


# --------------------------------------------------------------------------- #
# Recording an error
# --------------------------------------------------------------------------- #
def mode():
    if os.environ.get("FLIP_DOCKER") == "1":
        return "Docker"
    if os.environ.get("INVOCATION_ID"):
        return "server (systemd)"
    return "this computer"


def environment():
    try:
        import updater
        version = updater.current_version()
    except Exception:  # noqa: BLE001
        version = "?"
    return {"version": version, "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(), "mode": mode()}


def record(where: str, exc: BaseException | None = None, detail: str = "") -> dict:
    """Log an error and keep it for the report page. Returns the error record (with its id)."""
    exc = exc or sys.exc_info()[1]
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else detail
    err = {"id": uuid.uuid4().hex[:8], "at": datetime.now().isoformat(timespec="seconds"), "where": where,
           "type": type(exc).__name__ if exc else "Error", "message": scrub(str(exc) if exc else detail)[:300],
           "trace": scrub(tb)}
    with _lock:
        RECENT.append(err)
    log.error("[%s] %s: %s\n%s", err["id"], where, err["message"], err["trace"])
    return err


def get(err_id):
    with _lock:
        return next((e for e in RECENT if e["id"] == err_id), None)


def notify_failure(conn, where, err, link="/settings#problems"):
    """A notification for an error in a background job, with a way to report it."""
    db.notify(conn, "issue", f"{where} hit an error",
              f"{err['type']}: {err['message'][:200]} (error {err['id']}). Report it from Settings → Report a problem.",
              link, key=f"err-{where}-{err['type']}-{err['at'][:13]}")


# --------------------------------------------------------------------------- #
# GitHub issue link
# --------------------------------------------------------------------------- #
def issue_url(err: dict | None = None, kind="bug") -> str:
    env = environment()
    q = {"template": "bug_report.yml" if kind == "bug" else "feature_request.yml",
         "version": env["version"], "setup": env["mode"], "os": f"{env['os']}, Python {env['python']}"}
    if err:
        trace = err["trace"]
        lines = trace.splitlines()
        if len(lines) > 40:
            trace = "\n".join(["…"] + lines[-40:])
        q["title"] = f"Error in {err['where']}: {err['type']}"
        q["what"] = f"(Say what you were doing when this happened.)\n\nFlip for Free showed error {err['id']} in {err['where']}."
        q["logs"] = f"{err['type']}: {err['message']}\n\n{trace}"
    url = f"https://github.com/{ISSUES_REPO}/issues/new?" + urlencode(q)
    while len(url) > 7500 and q.get("logs"):          # GitHub rejects very long links
        q["logs"] = q["logs"][: len(q["logs"]) // 2] + "\n…"
        url = f"https://github.com/{ISSUES_REPO}/issues/new?" + urlencode(q)
    return url


def scrubbed_log() -> str:
    """The log file (and its rotated copies), oldest first, with personal details removed."""
    p = log_path()
    parts = []
    for i in (3, 2, 1):
        q = p.with_name(f"{p.name}.{i}")
        if q.exists():
            parts.append(q.read_text(encoding="utf-8", errors="replace"))
    if p.exists():
        parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return scrub("".join(parts)) or "No errors logged.\n"


# --------------------------------------------------------------------------- #
# Flask
# --------------------------------------------------------------------------- #
def register(app):
    from flask import jsonify, render_template, request, Response
    from werkzeug.exceptions import HTTPException

    setup_logging()

    @app.errorhandler(Exception)
    def _unhandled(e):
        if isinstance(e, HTTPException):
            return e
        err = record(f"{request.method} {request.path}", e)
        if request.path.startswith("/api/"):
            return jsonify(error="internal_error", error_id=err["id"],
                           message="Flip for Free hit an error. It has been logged; report it from Settings.",
                           report_url=issue_url(err)), 500
        try:
            return render_template("error.html", err=err, report=issue_url(err), env=environment()), 500
        except Exception:  # noqa: BLE001  the error page itself must never fail
            return Response(f"Flip for Free hit an error (error {err['id']}). Report it at "
                            f"https://github.com/{ISSUES_REPO}/issues", 500, mimetype="text/plain")

    @app.route("/settings/problems/log")
    def problems_log():
        return Response(scrubbed_log(), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=flip-for-free-log.txt"})

    @app.context_processor
    def _ctx():
        return {"report_problem_url": issue_url, "recent_errors": lambda: list(reversed(RECENT))}
