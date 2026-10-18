"""A completed move is followed by a fresh census.

The census step is reachable only when no ledger exists, so reporting a done
ledger complete would let the command carry exactly one move forever. A done
ledger therefore always makes the next invocation begin a new move at census,
whatever job the fleet record names; census and layout only read state, so this
starts nothing costly. The earlier ledger is left untouched.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from reckon.crew import fleet_migrate, fleet_node

FIXTURE = Path(__file__).parent / "fixtures" / "fleet_migrate"

_MOVE = "move-20260101T000000Z"


def _source(tmp_path: Path) -> dict:
    rows = list(csv.DictReader((FIXTURE / "resume.tsv").open(), delimiter="\t"))
    transcript_root = tmp_path / "transcripts"
    records = {}
    for row in rows:
        conversation = row["conversation"]
        cwd = row["cwd"]
        records[conversation] = {"conversation": conversation, "cwd": cwd}
        path = transcript_root / cwd.replace("/", "-") / f"{conversation}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    return {
        "layouts": {
            path.stem: path.read_text() for path in (FIXTURE / "layouts").glob("*.kdl")
        },
        "claude": records,
        "starts": [row["conversation"] for row in rows],
        "transcript_root": str(transcript_root),
    }


def _ledger(state: Path, *, next_step: str, promoted_job: str) -> Path:
    path = state / "migration" / _MOVE / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = ["census", "layout", "stand-up", "promote"]
    if next_step != "cutover":
        completed.append("cutover")
    if next_step == "done":
        completed.append("retire")
    path.write_text(
        json.dumps(
            {
                "completed": completed,
                "next_step": next_step,
                "census": {"sessions": [{"name": "one-fleet"}]},
                "stand_up": {"job_id": promoted_job, "node": "new-node"},
            }
        )
    )
    return path


def _record(state: Path, job_id: str) -> None:
    (state / "record.json").write_text(
        json.dumps({"job_id": job_id, "node": "other-node"})
    )


def test_completed_move_starts_a_fresh_census_when_the_fleet_is_its_own(
    tmp_path: Path,
) -> None:
    """The fleet record still names the job the move promoted, so the record
    cannot signal that another move is due; a done ledger always censuses."""
    state = tmp_path / "state"
    path = _ledger(state, next_step="done", promoted_job="new-job")
    _record(state, "new-job")
    source = _source(tmp_path)
    before = path.read_bytes()

    dry = fleet_migrate.migrate(
        dry_run=True,
        state=state,
        layouts_dir=tmp_path,
        observation=lambda: source,
        read_runs=list,
    )
    assert dry.startswith("census:")
    assert "migration complete" not in dry
    assert path.read_bytes() == before

    answer = fleet_migrate.migrate(
        state=state,
        layouts_dir=tmp_path,
        observation=lambda: source,
        read_runs=list,
    )
    assert answer.startswith("Recorded")
    assert "next step: layout" in answer
    assert path.read_bytes() == before
    fresh = [
        candidate
        for candidate in (state / "migration").glob("move-*/ledger.json")
        if candidate != path
    ]
    assert len(fresh) == 1
    assert json.loads(fresh[0].read_text())["completed"] == ["census"]


def test_completed_move_starts_a_new_census_when_the_fleet_moved_on(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    path = _ledger(state, next_step="done", promoted_job="first-job")
    _record(state, "second-job")
    source = _source(tmp_path)
    before = path.read_bytes()

    dry = fleet_migrate.migrate(
        dry_run=True,
        state=state,
        layouts_dir=tmp_path,
        observation=lambda: source,
        read_runs=list,
    )
    assert dry.startswith("census:")
    assert "migration complete" not in dry
    assert path.read_bytes() == before

    answer = fleet_migrate.migrate(
        state=state,
        layouts_dir=tmp_path,
        observation=lambda: source,  # type: ignore[arg-type]
        read_runs=list,
    )
    assert "next step: layout" in answer
    assert "migration complete" not in answer
    assert path.read_bytes() == before
    fresh = [
        candidate
        for candidate in (state / "migration").glob("move-*/ledger.json")
        if candidate != path
    ]
    assert len(fresh) == 1
    written = json.loads(fresh[0].read_text())
    assert written["next_step"] == "layout"
    assert written["completed"] == ["census"]


def test_a_move_at_cutover_resumes_rather_than_starting_a_census(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    path = _ledger(state, next_step="cutover", promoted_job="first-job")
    _record(state, "second-job")
    before = path.read_bytes()

    def observe() -> dict:
        raise AssertionError("a move at cutover must not start a census")

    assert (
        fleet_migrate.migrate(dry_run=True, state=state, layouts_dir=tmp_path)
        == "cutover: sessions still to move: one-fleet"
    )
    assert fleet_migrate.migrate(
        state=state, layouts_dir=tmp_path, observation=observe
    ).startswith("cutover:")
    assert path.read_bytes() == before
    assert list((state / "migration").glob("move-*/ledger.json")) == [path]


def test_status_drain_warning_states_the_allocation_continues() -> None:
    lines = fleet_node.describe_allocation(
        {
            "jobid": "1275000",
            "state": "RUNNING",
            "node": "rigel-03",
            "time": "2d03h",
            "timeleft": fleet_node.UNBOUNDED_TIME,
        },
        state_of_node="MIXED+DRAIN+REBOOT_REQUESTED",
    )
    warning = next(line for line in lines if line.startswith("WARNING"))
    assert "the allocation ends when the drain completes" not in warning
    assert "places no new job on the node" in warning
    assert "the allocation continues until it ends" in warning
    assert "returns only when an administrator resumes it" in warning
