"""Replay the control admission against four runs that needed a waiver.

Each entry names a run whose promotion the baseline comparison refused and which
was promoted on ``--waive-negative-control``. The driver reads that run's own
manifest, control log and committed row from disk — nothing here writes to a run
directory — rebuilds the node record the gate judges, and prints the verdict it
now reaches.

The last entry replays the first run with its head arm removed: it exists so the
replay shows the refusal as well as the admissions. A replay that could only
report the answer it wanted would be evidence of nothing.

Usage: PYTHONPATH=<worktree> python replay_head_arm_admission.py <repo-root>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reckon.crew.node import CrewError
from reckon.crew.promotion import _require_declared_negative_control
from reckon.crew.reports import parse_manifest

RUNS_ROOT = Path("~/.config/reckon/crew/runs").expanduser()

# (run directory, the node's own test path that triggers the gate, drop head arm)
ROWS = [
    (
        "r-20260928T142644163914-lane-reading-carries-generating-and-waiting",
        "tests/test_lane_reading_carries_counts.py",
        False,
    ),
    (
        "r-20260928T193532461213-receipt-rollback-classification-red",
        "tests/test_receipt_names_the_rolled_back_row.py",
        False,
    ),
    (
        "r-20260928T204215455574-directory-row-reads-the-derived-phase",
        "tests/test_directory_row_reads_the_derived_phase.py",
        False,
    ),
    (
        "r-20260929T001417713998-orientation-stub-is-not-unwritten",
        "tests/test_orientation_stub_is_not_unwritten.py",
        False,
    ),
    (
        "r-20260928T142644163914-lane-reading-carries-generating-and-waiting",
        "tests/test_lane_reading_carries_counts.py",
        True,
    ),
]


def _declaration(repo_root: Path, run: str) -> str:
    """The mutation the run recorded on its node, read from the committed row."""
    row = (repo_root / "docs" / "state" / "reckon" / "runs" / f"{run}.json").read_text(
        encoding="utf-8"
    )
    return str((json.loads(row).get("negative_control") or {}).get("declaration") or "")


def main(argv: list[str]) -> int:
    repo_root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    for run, test_path, drop_head_arm in ROWS:
        run_dir = RUNS_ROOT / run
        manifest_path = run_dir / "manifest.md"
        manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
        if drop_head_arm:
            manifest.pop("after_suite", None)
        record = {
            "run_id": run,
            "node": {
                "id": run,
                "write_paths": [test_path],
                "negative_control": _declaration(repo_root, run),
            },
        }
        print(f"=== {run}{' (head arm removed)' if drop_head_arm else ''}")
        print(f"    manifest: {manifest_path}")
        print(f"    control log: {manifest.get('negative_control_log')}")
        try:
            check = _require_declared_negative_control(
                run,
                record,
                gate="passed",
                manifest=manifest,
                manifest_path=str(manifest_path),
            )
        except CrewError as refusal:
            print(f"    verdict: REFUSED\n    {refusal}")
            continue
        print(f"    verdict: {check['verdict']}")
        print(f"    comparison arm: {check['comparison_arm']}")
        print(f"    control failing ids: {check['control_failure_ids']}")
        print(f"    head arm failing ids: {check.get('head_failure_ids')}")
        print(f"    ids added to the deciding arm: {check['added_failure_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
