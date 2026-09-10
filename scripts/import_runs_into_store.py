#!/usr/bin/env python3
"""Import a project's committed crew ledger into the shadow run store.

    uv run python scripts/import_runs_into_store.py --project <project> [--root <path>]

The ledger is resolved by project name through the ordinary ledger loader and
never opened directly by this script, so whichever checkout the config home
routes that project to is the file read. Re-runnable: a run already present
with an identical durable record is left untouched, an already-present run
whose durable record disagrees is corrected, and an absent run is inserted, so
a second immediate pass reports zero rows imported and zero rows already
present. The historical ``store_write`` field is never written to the store —
promotion records stay on the committed file rows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reckon import run_store  # noqa: E402  (import after sys.path is set)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a project's committed crew ledger into the shadow run store"
    )
    parser.add_argument(
        "--project", required=True, help="project whose ledger to import"
    )
    parser.add_argument(
        "--root",
        default=None,
        help="checkout root holding the ledger when config-home routing is not wanted",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(list(argv) if argv is not None else sys.argv[1:])
    result = run_store.import_ledger(args.project, root=args.root)
    print(f"project: {result['project']}")
    print(f"rows imported: {result['rows_imported']}")
    print(f"rows already present: {result['rows_already_present']}")
    if result["rows_unchanged"]:
        print(f"rows already present and unchanged: {result['rows_unchanged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
