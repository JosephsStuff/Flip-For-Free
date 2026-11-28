"""Your time zone. Decides what "today" is, which tax year a sale falls in and when automations run.

On your own computer Flip simply uses the computer's time zone. In Docker or on a server the machine's clock is
often UTC, so Flip asks your browser which time zone you're in the first time you use it, saves that, and uses it
from then on. You can change it in Settings → Preferences.
"""
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import db

SETTING = "timezone"


def valid(name: str | None) -> bool:
    if not name or len(name) > 64 or name.startswith(("/", ".")) or ".." in name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def saved() -> str | None:
    try:
        name = db.Settings().get(SETTING) if db.get_db_path() else None
    except Exception:  # noqa: BLE001 - before setup there's no database
        return None
    return name if valid(name) else None


def _tz_value(name: str) -> str:
    """What to put in TZ. Uses the system's zone files, or the tzdata package's copy if the system has none
    (slim Docker images)."""
    if Path("/usr/share/zoneinfo", name).is_file():
        return name
    try:
        import tzdata
        f = Path(tzdata.__file__).parent / "zoneinfo" / name
        if f.is_file():
            return ":" + str(f)
    except ImportError:
        pass
    return name


def apply(name: str | None = None) -> str | None:
    """Make this process use the time zone. Windows can't switch at run time, but there Flip runs on your own PC
    and already uses its time zone."""
    name = name or saved()
    if not name or not valid(name) or not hasattr(time, "tzset"):
        return None
    os.environ["TZ"] = _tz_value(name)
    time.tzset()
    return name


def current() -> str:
    """The time zone in use, by name if we know it."""
    return saved() or os.environ.get("TZ", "").lstrip(":").split("zoneinfo/")[-1] or time.tzname[0]


def needs_detecting() -> bool:
    """No time zone chosen yet: ask the browser."""
    return saved() is None


def choices() -> list[str]:
    return sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/")))


def save(name: str) -> bool:
    if not valid(name):
        return False
    db.Settings().set(SETTING, name)
    apply(name)
    return True
