"""Report every admission hold in a window beside the claim path it names.

§9's census (``scripts/lane_window_census.py``) counts the admit passes in a
window and how many of them held a request on a claim under a shared root. It
does not print the paths, so a count of one cannot say whether that hold was a
crew-home directory no run owns (§69's case) or a crew-home file one live run
does own (permitted). This reads the same admission log and prints the total
passes and, for every hold, the verdict, the claimed path, and whether that
path lies under the crew home.

    python lane_window_claim_paths.py --window 2026-11-17T00:00Z/2026-11-17T04:00Z

It is a reader: it writes nothing, and it reports a window the log does not
cover as unmeasured rather than as empty.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

CREW_HOME = Path(
    os.environ.get("RECKON_CREW_HOME", "~/.config/reckon/crew")
).expanduser()
ADMISSION_LOG = CREW_HOME / "admission-verdicts.jsonl"

_REPR_QUOTED = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
_PATH_PATTERNS = (
    re.compile(rf"\bholds\s+({_REPR_QUOTED})"),
    re.compile(rf"\blive claim\s+({_REPR_QUOTED})"),
    re.compile(rf"\bclaiming\s+({_REPR_QUOTED})"),
)


def parse_instant(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def parse_window(text: str) -> tuple[dt.datetime, dt.datetime]:
    start, _, end = text.partition("/")
    return parse_instant(start), parse_instant(end)


def claimed_path(detail: str) -> str:
    for pattern in _PATH_PATTERNS:
        match = pattern.search(detail or "")
        if match:
            try:
                return str(ast.literal_eval(match.group(1)))
            except (ValueError, SyntaxError):
                return ""
    return "hold-surface" if detail else ""


def under_crew_home(path_text: str) -> bool:
    if not path_text:
        return False
    return str(Path(path_text).expanduser()).startswith(str(CREW_HOME) + os.sep)


def rows(start: dt.datetime, end: dt.datetime) -> list[dict]:
    if not ADMISSION_LOG.exists():
        raise SystemExit(f"the admission log is not at {ADMISSION_LOG}")
    out = []
    for line in ADMISSION_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            when = parse_instant(row["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if start <= when < end:
            out.append(row)
    return out


def still_stored(start: dt.datetime, end: dt.datetime) -> tuple[bool, list[str]]:
    stamps = []
    for line in ADMISSION_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            stamps.append(parse_instant(json.loads(line)["at"]))
        except (ValueError, KeyError, TypeError):
            continue
    if not stamps:
        return False, []
    span = [min(stamps).isoformat(), max(stamps).isoformat()]
    return (not (end <= min(stamps) or start >= max(stamps))), span


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", required=True, help="START/END, ISO instants")
    args = parser.parse_args(argv)
    start, end = parse_window(args.window)
    covered, span = still_stored(start, end)
    if not covered:
        print(
            json.dumps(
                {
                    "window": [start.isoformat(), end.isoformat()],
                    "log_span": span,
                    "passes": None,
                    "note": "the window lies outside the passes the admission log still holds",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    held = []
    passes = rows(start, end)
    holding_passes = 0
    verdicts: dict[str, int] = {}
    for row in passes:
        pointers = [
            pointer
            for pointer in row.get("pointers") or []
            if pointer.get("verdict") == "held-on-claim"
        ]
        for pointer in row.get("pointers") or []:
            key = str(pointer.get("verdict") or "?")
            verdicts[key] = verdicts.get(key, 0) + 1
        if pointers:
            holding_passes += 1
        for pointer in pointers:
            detail = pointer.get("detail") or ""
            path = claimed_path(str(detail))
            held.append(
                {
                    "at": row.get("at"),
                    "project": row.get("project"),
                    "run": pointer.get("run") or row.get("run"),
                    "verdict": pointer.get("verdict"),
                    "claim_path": path,
                    "under_crew_home": under_crew_home(path),
                    "detail": " ".join(str(detail).split())[:400],
                }
            )
    print(
        json.dumps(
            {
                "window": [start.isoformat(), end.isoformat()],
                "log_span": span,
                "passes": len(passes),
                "passes_holding_a_request": holding_passes,
                "verdict_counts": dict(sorted(verdicts.items())),
                "holds": held,
                "holds_under_crew_home": sum(1 for h in held if h["under_crew_home"]),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
