#!/usr/bin/env python3
"""Fail if the record's site table drifts from the revision it names.

Run from anywhere:

    python docs/research/where-behaviour-follows-a-backends-name.check.py

The record lists every place under ``reckon/`` that decides behaviour from a
backend's name or a harness dialect, each with a path and a line at one named
revision. A row is a claim about that revision, so a row that no longer
resolves — because the file moved, the line shifted past the end, or the line
went blank — is a stale record, and this check fails rather than letting the
table rot quietly.

Exit status is 0 when every row resolves and the stated counts agree with the
table; 1 when any row does not, naming each; and 2 when the record or the
revision cannot be read at all.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORD = HERE / "where-behaviour-follows-a-backends-name.html"
REPO = HERE.parents[1]

CLASSES = ("launch argv", "harness home", "stream format", "model identifier", "none")
SITE = re.compile(r'<td><code>(?P<path>[^<]+?):(?P<line>[0-9]+)</code></td>')
CLASS_CELL = re.compile(r"<td>(launch argv|harness home|stream format|model identifier|none)</td>")


def _meta(text: str, name: str) -> str:
    match = re.search(rf'<meta name="{re.escape(name)}" content="([^"]*)"', text)
    return match.group(1).strip() if match else ""


def _source_line(revision: str, path: str, lineno: int) -> tuple[bool, str]:
    try:
        shown = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{revision}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return False, f"{path}:{lineno} does not resolve at {revision[:12]}"
    lines = shown.splitlines()
    if lineno < 1 or lineno > len(lines):
        return False, f"{path}:{lineno} is past the end ({len(lines)} lines)"
    if not lines[lineno - 1].strip():
        return False, f"{path}:{lineno} is blank"
    return True, ""


def main() -> int:
    try:
        text = RECORD.read_text(encoding="utf-8")
    except OSError as error:
        print(f"unreadable record: {error}")
        return 2

    revision = _meta(text, "plan-measured-at")
    if not revision:
        print("the record names no revision in a plan-measured-at meta")
        return 2

    rows = [(m.group("path"), int(m.group("line"))) for m in SITE.finditer(text)]
    if not rows:
        print("the record lists no site rows")
        return 2

    classes = CLASS_CELL.findall(text)
    stated = _meta(text, "plan-row-count")
    if stated != str(len(rows)):
        print(
            f"the record states {stated or 'no'} rows and its table holds {len(rows)}"
        )
        return 1
    if len(classes) != len(rows):
        print(f"{len(rows)} rows carry {len(classes)} class cells")
        return 1

    failures = []
    for path, lineno in rows:
        ok, reason = _source_line(revision, path, lineno)
        if not ok:
            failures.append(reason)
    for failure in failures:
        print(failure)
    if failures:
        print(f"{len(failures)} of {len(rows)} rows do not resolve at {revision}")
        return 1

    counts = {name: classes.count(name) for name in CLASSES}
    print(
        f"{len(rows)} rows resolve at {revision}: "
        + ", ".join(f"{counts[name]} {name}" for name in CLASSES)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())