"""Set VERSION to today's dated version and add a CHANGELOG heading for it.

    python tools/bump_version.py beta     # 26.11.2026.1, then .2, .3 … for each beta that day
    python tools/bump_version.py main     # 26.11.2026 (a second main release that day gets .1)
"""
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def next_version(channel: str, current: str, today: date | None = None) -> str:
    today = today or date.today()
    stem = f"{today.day}.{today.month}.{today.year}"
    m = re.fullmatch(re.escape(stem) + r"(?:\.(\d+))?", current.strip())
    if channel == "beta":
        return f"{stem}.{int(m.group(1) or 0) + 1 if m else 1}"
    if not m:
        return stem
    return f"{stem}.{int(m.group(1) or 0) + 1}"


def main():
    channel = (sys.argv[1:] or ["beta"])[0]
    if channel not in ("beta", "main"):
        raise SystemExit("usage: bump_version.py beta|main")
    vfile, log = ROOT / "VERSION", ROOT / "CHANGELOG.md"
    v = next_version(channel, vfile.read_text())
    vfile.write_text(v + "\n")
    text = log.read_text()
    if f"## {v}\n" not in text:
        log.write_text(text.replace("# Changelog\n\n", f"# Changelog\n\n## {v}\n- \n\n", 1))
    print(v)


if __name__ == "__main__":
    main()
