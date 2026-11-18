#!/usr/bin/env python3
"""Import and verify a project's ledger before retiring its files.

    uv run python scripts/import_runs_into_store.py --project <project> [--root <path>]

The existing importer owns run synchronisation and revision backfill. Roster
and hold state is seeded only when the store has never held it, preserving
mutable state that has advanced beyond the aggregate. A successful receipt
requires run parity with the source documents. Run this before removing them;
an absent source is refused rather than mistaken for an empty import.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reckon import ledger, run_store  # noqa: E402  (import after sys.path is set)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import and verify ledger history before removing its files"
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
    source = ledger.ledger_path(args.project, args.root)
    if not source.is_file() and not any((source.parent / "runs").glob("*.json")):
        print(
            f"no ledger documents at {source.parent}; import before removing "
            "the files, or restore a source export to a separate checkout",
            file=sys.stderr,
        )
        return 1
    result = run_store.import_ledger(args.project, root=args.root)
    auxiliary, version = ledger.load_auxiliary(args.project, args.root)
    comparison = run_store.compare(args.project, root=args.root)
    if any(
        comparison[key]
        for key in (
            "rows_in_file_absent_from_store",
            "rows_in_store_absent_from_file",
            "rows_present_in_both_disagreeing",
        )
    ):
        print(f"ledger verification failed: {comparison}", file=sys.stderr)
        return 1
    print(f"project: {result['project']}")
    print(f"rows imported: {result['rows_imported']}")
    print(f"rows already present: {result['rows_already_present']}")
    if result["rows_unchanged"]:
        print(f"rows already present and unchanged: {result['rows_unchanged']}")
    print(f"members preserved: {len(auxiliary['members'])}")
    print(f"holds preserved: {len(auxiliary['holds'])}")
    print(f"auxiliary version: {version}")
    print("run verification: 0 missing, 0 extra, 0 differing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
