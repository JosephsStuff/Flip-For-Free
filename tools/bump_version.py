"""Set VERSION to the next year.month.number and add a CHANGELOG heading for it.

    python tools/bump_version.py           # next number this month, e.g. 2026.11.4

One counter per month, shared by beta and main (it looks at both branches on GitHub), so every release has its own
number. When a beta becomes the main release, merge it and keep its number: it's the same code.
"""
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def next_version(seen: list[str], today: date | None = None) -> str:
    """The next number this month after every version already used (on any branch)."""
    today = today or date.today()
    used = [int(m.group(1)) for v in seen
            if (m := re.fullmatch(rf"{today.year}\.{today.month}\.(\d+)", v.strip()))]
    return f"{today.year}.{today.month}.{max(used, default=0) + 1}"


def _branch_version(branch: str) -> str:
    try:
        subprocess.run(["git", "-C", str(ROOT), "fetch", "-q", "origin", branch], check=False, timeout=60)
        return subprocess.run(["git", "-C", str(ROOT), "show", f"origin/{branch}:VERSION"], capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main():
    vfile, log = ROOT / "VERSION", ROOT / "CHANGELOG.md"
    seen = [vfile.read_text(), _branch_version("main"), _branch_version("beta")]
    v = next_version(seen)
    vfile.write_text(v + "\n")
    text = log.read_text()
    if f"## {v}\n" not in text:
        log.write_text(text.replace("# Changelog\n\n", f"# Changelog\n\n## {v}\n- \n\n", 1))
    print(v)


if __name__ == "__main__" and len(sys.argv) <= 2:
    main()
