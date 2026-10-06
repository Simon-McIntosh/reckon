#!/usr/bin/env python3
"""Replay the cumulative coverage rule over the 2026-10-06 review snapshot.

The plan-review cycle research (docs/research/plan-review-cycles.html) measured,
over the consecutive pairs of stored reviews, that "a section was added, or a
unit changed by 30 % or more of its own words" fires on 15 of the re-reviews of
2026-10-06 and keeps all four added sections. That rule compares consecutive
snapshots. The coverage predicate landed for the unit-of-review work compares
each review against the snapshot that last *covered* the plan, so a run of small
edits accumulates until together they cross the threshold. This replay reports
the same two figures under the cumulative rule, beside the consecutive 15.

It reuses the measurement module's loaders and unit readers so the blob
recovery, the section prose reader and the word tokeniser are the same ones the
research used. Run from the worktree root:

    PYTHONPATH=$PWD <repo>/.venv/bin/python \
        docs/figures/a-plan-is-reviewed-before-it-is-built/\
review-is-earned-by-significant-change/replay_cumulative.py
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from reckon.crew import plan_review

_HERE = Path(__file__).resolve()
_MEASURE = (
    _HERE.parents[2] / "plan-review-cycles" / "measure_review_cycles.py"
)

THRESHOLD = 0.30
DATE = "2026-10-06"
# The research snapshot was taken at this instant; the store is live and has
# grown since, so the replay is bounded to reviews that had run by then.
SNAPSHOT_ISO = "2026-10-06T04:48:00+00:00"


def _load_measure():
    spec = importlib.util.spec_from_file_location("measure_review_cycles", _MEASURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _share(reviewed: str, present: str) -> float:
    return plan_review._edit_share(reviewed, present)


def _on_date(record) -> bool:
    ts = record.get("_ts")
    if ts is None:
        return False
    return datetime.fromtimestamp(ts.timestamp(), tz=timezone.utc).strftime(
        "%Y-%m-%d"
    ) == DATE


def _before_snapshot(record) -> bool:
    ts = record.get("_ts")
    if ts is None:
        return False
    return ts.timestamp() <= datetime.fromisoformat(SNAPSHOT_ISO).timestamp()


def main() -> int:
    m = _load_measure()
    repos = m.load_mounts()
    records = m.load_reviews()
    store = m.BlobStore()

    import collections
    from datetime import timedelta

    by_plan: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for record in records:
        run_ts = m.review_run_time(record)
        store_ts = m.parse_ts(record.get("timestamp"))
        ts = run_ts or store_ts
        if ts is None:
            continue
        record["_ts"] = ts
        repo = repos.get(record["project"])
        text = None
        if repo is not None:
            sha = str(record.get("reviewed_blob_sha") or "").strip()
            if sha:
                text = store.blob_text(repo, sha)
            if text is None:
                relpath = m.plan_relative_path(record)
                before = [i for i in store.history(repo, relpath) if i[0] <= ts]
                if before:
                    text = store.blob_text(repo, before[-1][1])
        record["_text"] = text
        by_plan[(record["project"], record["plan_slug"])].append(record)

    for reviews in by_plan.values():
        reviews.sort(key=lambda r: (r["_ts"], int(r.get("plan_version") or 0)))
    _ = timedelta

    considered = 0
    consecutive_fires = 0
    cumulative_fires = 0
    added_kept = 0
    for reviews in by_plan.values():
        snapshot_text = None
        prev_text = None
        for record in reviews:
            text = record.get("_text")
            if text is None:
                continue
            if snapshot_text is None:
                # The first review of a chain establishes the snapshot.
                snapshot_text = text
                prev_text = text
                continue
            if not (_on_date(record) and _before_snapshot(record)):
                snapshot_text = text
                prev_text = text
                continue
            considered += 1
            present_sections = m.section_prose_map(text)
            snap_sections = m.section_prose_map(snapshot_text)
            prev_sections = m.section_prose_map(prev_text or text)

            def uncovered_against(reference_sections, reference_text):
                uncovered = set()
                for unit, prose in present_sections.items():
                    if unit not in reference_sections:
                        uncovered.add(unit)
                    elif _share(reference_sections[unit], prose) >= THRESHOLD:
                        uncovered.add(unit)
                if m.trimmed_section_digests(reference_text).get(
                    plan_review._DOCUMENT_UNIT
                ) != m.trimmed_section_digests(text).get(plan_review._DOCUMENT_UNIT):
                    uncovered.add(plan_review._DOCUMENT_UNIT)
                return uncovered

            cum = uncovered_against(snap_sections, snapshot_text)
            con = uncovered_against(prev_sections, prev_text or text)
            if cum:
                cumulative_fires += 1
                added_kept += len(
                    [u for u in cum if u not in snap_sections]
                )
                snapshot_text = text
            if con:
                consecutive_fires += 1
            prev_text = text

    print(f"considered re-reviews dated {DATE}: {considered}")
    print(f"consecutive rule fires: {consecutive_fires}")
    print(f"cumulative rule fires: {cumulative_fires}")
    print(f"added sections the cumulative rule keeps: {added_kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())