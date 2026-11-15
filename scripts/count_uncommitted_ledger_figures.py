"""Count ledger rows carrying a derived figure at HEAD and in the working tree.

A ledger write that is never committed is invisible to anyone reading the
repository, and it is the first thing a reclaim loses. Counting the rows that
carry a figure in each state makes that gap measurable, and the exit status
reddens when a project's figures exist only in its working tree.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

FIGURE_FIELDS = ("tool_steps", "orientation_input_tokens")


def ledger_path(project: str) -> str:
    return f"docs/state/{project}/crew.json"


def filled_rows(document: dict) -> int:
    runs = (document.get("data") or {}).get("runs") or []
    if isinstance(runs, dict):
        runs = list(runs.values())
    return sum(
        1
        for row in runs
        if isinstance(row, dict) and any(row.get(f) is not None for f in FIGURE_FIELDS)
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
    project = repo.name
    relative = ledger_path(project)
    at_head = filled_rows(head_document(repo, relative))
    in_tree = filled_rows(working_document(repo, relative))
    return {
        "project": project,
        "at_head": at_head,
        "in_working_tree": in_tree,
        "uncommitted": in_tree - at_head,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repositories", nargs="+", type=Path)
    parser.add_argument(
        "--require-durable",
        action="store_true",
        help="exit 1 when any project's figures are not all in HEAD",
    )
    arguments = parser.parse_args(argv)
    rows = [report(repo) for repo in arguments.repositories]
    for row in rows:
        print(
            "DURABILITY "
            + json.dumps(row, sort_keys=True)
            + f" head_vs_working_tree={row['at_head']} vs {row['in_working_tree']}"
        )
    uncommitted = [row for row in rows if row["uncommitted"] != 0]
    if arguments.require_durable and uncommitted:
        for row in uncommitted:
            print(
                f"UNCOMMITTED {row['project']}: {row['uncommitted']} row(s) "
                "carry a figure only in its working tree"
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
