#!/usr/bin/env python3
"""Backfill per-run figures onto rows recorded before promotion computed them.

    uv run python scripts/derive_run_figures.py --project <project> [--root <path>]

Idempotent and re-runnable: fills ``tool_steps`` and ``orientation_input_tokens``
only for rows that lack a measured figure, records both as absent when the run's
stream is no longer on disk (never as zero), and reports rows filled and rows
skipped for a missing stream as separate numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reckon import capabilities, ledger  # noqa: E402  (import after sys.path is set)


def backfill_run_figures(
    project: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Fill the two per-run figures on rows that still lack them.

    Returns separate counts: ``rows_filled`` for rows whose stream was still on
    disk and yielded at least one derived number, ``rows_skipped`` for rows whose
    stream is gone (both figures recorded as absent, never as zero), and
    ``rows_unmeasurable`` for rows whose stream is present but evidences neither
    figure. A row is processed only when a figure key is absent, so a row that
    already records a figure - or records an explicit absent - is left alone and
    a second run is a no-op.
    """

    data, version = ledger.load(project, root=root)
    filled = 0
    skipped = 0
    unmeasurable = 0
    for row in data["runs"]:
        missing_orientation = "orientation_input_tokens" not in row
        missing_tool_steps = "tool_steps" not in row
        if not missing_orientation and not missing_tool_steps:
            continue
        if capabilities.run_stream_path(row) is None:
            row["tool_steps"] = None
            row["orientation_input_tokens"] = None
            skipped += 1
            continue
        figures = capabilities.derive_run_figures(row)
        row["tool_steps"] = figures["tool_steps"]
        row["orientation_input_tokens"] = figures["orientation_input_tokens"]
        if figures["tool_steps"] is None and figures["orientation_input_tokens"] is None:
            unmeasurable += 1
        else:
            filled += 1
    if filled or skipped or unmeasurable:
        ledger.write(project, data, version, root=root)
    return {
        "project": project,
        "rows_filled": filled,
        "rows_skipped": skipped,
        "rows_unmeasurable": unmeasurable,
        "ledger_version": version,
    }


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="derive_run_figures",
        description=(
            "Fill tool_steps and orientation_input_tokens on ledger rows that "
            "lack them, recording absent (never zero) when a stream is gone."
        ),
    )
    parser.add_argument("--project", required=True, help="project whose ledger to fill")
    parser.add_argument("--root", default=None, help="checkout root holding the ledger")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(list(argv) if argv is not None else sys.argv[1:])
    result = backfill_run_figures(args.project, root=args.root)
    print(
        f"{result['project']}: filled {result['rows_filled']}, "
        f"skipped {result['rows_skipped']} (stream missing), "
        f"unmeasurable {result['rows_unmeasurable']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
