"""Count, per project, the rows retention finds each figure recorded on.

A ledger write that is never committed is invisible to anyone reading the
repository and is the first thing a reclaim loses. The count here is taken with
the same rule the reaper applies, so the two cannot disagree about whether a
figure exists: a row counts for a declared stream-derived figure only when
routing's retention finds that figure recorded on it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from reckon.crew.routing import STREAM_DERIVED_FIELDS, _figure_retention


def ledger_path(project: str) -> str:
    return f"docs/state/{project}/crew.json"


def rows(document: dict) -> list[dict]:
    runs = (document.get("data") or {}).get("runs") or []
    if isinstance(runs, dict):
        runs = list(runs.values())
    return [row for row in runs if isinstance(row, dict)]


def recorded_rows(document: dict, field: str) -> int:
    return sum(
        1 for row in rows(document) if _figure_retention(row, field) == "recorded"
    )


def head_document(repo: Path, relative: str) -> dict:
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{relative}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0:
        raise SystemExit(f"{repo}: cannot read HEAD:{relative}")
    return json.loads(shown.stdout)


def working_document(repo: Path, relative: str) -> dict:
    return json.loads((repo / relative).read_text())


def report(repo: Path) -> dict:
    relative = ledger_path(repo.name)
    at_head = head_document(repo, relative)
    in_tree = working_document(repo, relative)
    fields = {}
    for field in STREAM_DERIVED_FIELDS:
        head_count = recorded_rows(at_head, field)
        tree_count = recorded_rows(in_tree, field)
        fields[field] = {
            "at_head": head_count,
            "in_working_tree": tree_count,
            "only_in_working_tree": max(tree_count - head_count, 0),
            "only_at_head": max(head_count - tree_count, 0),
        }
    return {"project": repo.name, "fields": fields}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repositories", nargs="+", type=Path)
    parser.add_argument(
        "--require-durable",
        action="store_true",
        help="exit 1 when any figure is recorded on more rows in the working tree than at HEAD",
    )
    arguments = parser.parse_args(argv)
    reports = [report(repo) for repo in arguments.repositories]
    pending = []
    for row in reports:
        print("DURABILITY " + json.dumps(row, sort_keys=True))
        for field, counts in row["fields"].items():
            print(
                f"{row['project']} {field}: at HEAD {counts['at_head']}, "
                f"in the working tree {counts['in_working_tree']}"
            )
            if counts["only_in_working_tree"]:
                print(
                    f"UNCOMMITTED {row['project']} {field}: "
                    f"{counts['only_in_working_tree']} row(s) are recorded only in the working tree"
                )
                pending.append(row)
            if counts["only_at_head"]:
                print(
                    f"AHEAD_OF_WORKING_TREE {row['project']} {field}: "
                    f"{counts['only_at_head']} row(s) are recorded at HEAD and not in the working tree"
                )
    if arguments.require_durable and pending:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
