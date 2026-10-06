#!/usr/bin/env python3
"""Replay the cumulative coverage rule over the 2026-10-06 review snapshots.

The plan-review cycle research measured, over the consecutive pairs of stored
reviews, that "a section was added, or a unit changed by 30% or more of its own
words" fires on 15 of the 96 re-reviews of 2026-10-06 and keeps all four added
sections. That rule compares consecutive snapshots. The coverage predicate
landed for the unit-of-review work compares each review against the snapshot
that last *covered* the plan, so a run of small edits accumulates until together
they cross the threshold. This replay computes both rules over the same 96
re-reviews, so the cumulative figure sits beside the research's consecutive 15
with a matching denominator.

The population is the per-run review snapshots under
``~/.config/reckon/crew/reports/<project>/plan-review/<run>/plan.html`` — the
same store the research's ``edit_size.py`` and ``section_change.py`` read — kept
read-only. Sections are read through the review module's own prose reader so the
word tokeniser and the bookkeeping exclusion are the measure's, not a copy. Run
from the worktree root:

    PYTHONPATH=$PWD <repo>/.venv/bin/python \
        docs/figures/a-plan-is-reviewed-before-it-is-built/\
review-is-earned-by-significant-change/replay_cumulative.py
"""

from __future__ import annotations

import glob
import os
import sys
from collections import defaultdict
from pathlib import Path

from reckon.crew import plan_review

THRESHOLD = 0.30
DAY = "20261006"
REPORTS_ROOT = Path.home() / ".config/reckon" / "crew" / "reports"


def _sections(text: str) -> dict[str, str]:
    """Return each heading's prose, bookkeeping already excluded."""
    return {
        identity: prose
        for identity, prose in plan_review._prose_texts(text).items()
        if identity != plan_review._DOCUMENT_UNIT
    }


def _fires(present: dict[str, str], reference: dict[str, str]) -> tuple[bool, int]:
    """Whether the section rule fires against ``reference``, and added count."""
    added = 0
    fired = False
    for identity, prose in present.items():
        if identity not in reference:
            added += 1
            fired = True
        elif plan_review._edit_share(reference[identity], prose) >= THRESHOLD:
            fired = True
    return fired, added


def main() -> int:
    by_plan: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for run_dir in sorted(glob.glob(f"{REPORTS_ROOT}/*/plan-review/*/r-*")):
        plan = os.path.join(run_dir, "plan.html")
        if not os.path.isfile(plan):
            continue
        parts = run_dir.split("/")
        by_plan[(parts[-4], parts[-2])].append((os.path.basename(run_dir), plan))

    considered = 0
    consecutive_fires = 0
    cumulative_fires = 0
    added_kept = 0
    for snapshots in by_plan.values():
        snapshots.sort()
        consecutive_ref = None
        cumulative_ref = None
        for run, path in snapshots:
            present = _sections(Path(path).read_text(encoding="utf-8", errors="replace"))
            if consecutive_ref is None:
                consecutive_ref = present
                cumulative_ref = present
                continue
            if DAY in run:
                considered += 1
                con, _ = _fires(present, consecutive_ref)
                if con:
                    consecutive_fires += 1
                cum, added = _fires(present, cumulative_ref)
                if cum:
                    cumulative_fires += 1
                    added_kept += added
                    cumulative_ref = present
            consecutive_ref = present

    print(f"re-reviews dated {DAY} with a prior snapshot: {considered}")
    print(f"consecutive rule (new section or any section >= 30% of itself): {consecutive_fires}")
    print(f"cumulative rule (against the last snapshot that fired): {cumulative_fires}")
    print(f"re-reviews the cumulative rule keeps that added a section: {added_kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())