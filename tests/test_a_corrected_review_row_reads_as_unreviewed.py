"""A row whose review is superseded is corrected in place of nothing.

A committed ledger row records what a promotion believed when it landed: its
``review`` block names the head the review read and the score it gave. When
that block describes another round, the row is not rewritten — the belief is
itself evidence — so a correction is appended beside it and every reader of a
row's review reads the correction. These cases build one misattributed row in a
throwaway repository, run ``crew supersede-review`` over it, and assert both
halves: the correction is appended without touching the recorded fields and
lands as one commit, and each named reader then reads the row as a run with no
review.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew.node import TaskNode
from reckon.crew.picker import outcomes as outcomes_module
from reckon.crew.picker import snapshot as snapshot_module
from reckon.crew.picker.types import PickRequest
from reckon.crew.repair import round_node_id
from reckon.crew.review import REVIEW_DIMENSIONS

PROJECT = "temp-project"
DISPATCHED_HEAD = "0a451acfdb02"
RECORDED_HEAD = "fbc3788e9d1c4ab5e6f7081920a1b2c3d4e5f607"
REVIEW_NODE = round_node_id("review-of-", "tcv-liuqe-known-answer", DISPATCHED_HEAD)
REVIEW_RUN_ID = "r-20261018T192200408612-review-of-tcv-liuqe-known-answer"


def _utc(days_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _row(**overrides) -> dict:
    """One promoted review row carrying another round's head.

    The row is attributed to ``DISPATCHED_HEAD`` by the head suffix its node id
    mints, and its recorded review names ``RECORDED_HEAD — so the recorded head
    does not begin with the dispatched one.
    """
    row = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "node": REVIEW_NODE,
        "role": "review",
        "spec_level": "standard",
        "backend": "local",
        "lane": "local",
        "model_key": "local-worker",
        "gate": "passed",
        "completed_at": _utc(1),
        "dispatched_at": _utc(2),
        "wall_seconds": 120.0,
        "route_mode": "picker",
        "picker_selection": {"action": "route", "backend": "local"},
        "agent": {"model": "local"},
        "node_definition": {
            "write_paths": [
                f"docs/state/temp-project/reviews/{REVIEW_RUN_ID}.at-{DISPATCHED_HEAD}.json"
            ],
            "capability": {"requirements": {"reasoning": "deep", "risk": "moderate"}},
        },
        "risk": "moderate",
        "review": {
            "status": "parsed",
            "reviewed_head_sha": RECORDED_HEAD,
            "base_revision": "b731c734",
            "total": 0.75,
            "scores": dict.fromkeys(REVIEW_DIMENSIONS, 0.75),
            "timestamp": _utc(1),
        },
    }
    row.update(overrides)
    return row


@pytest.fixture()
def ledger_repo(tmp_path, monkeypatch):
    """A throwaway git repository holding one row in the aggregate and its file."""
    root = tmp_path / "repo"
    state = root / "docs" / "state"
    runs = state / PROJECT / "runs"
    runs.mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state))

    row = _row()
    (state / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    envelope = {
        "updated": "2026-10-18T00:00:00",
        "project": PROJECT,
        "doc": "crew",
        "data": {
            "members": [],
            "runs": [row],
            "holds": [],
            "_version": 1,
        },
    }
    (state / PROJECT / "crew.json").write_text(json.dumps(envelope) + "\n")
    (runs / f"{row['run_id']}.json").write_text(
        json.dumps(row, sort_keys=True, indent=2) + "\n"
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs"],
        ["commit", "-q", "-m", "chore: seed the ledger"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root, state / PROJECT


def _run_path(state_dir: Path, run_id: str) -> Path:
    return state_dir / "runs" / f"{run_id}.json"


def _commit_count(root: Path) -> int:
    done = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return int(done.stdout.strip())


def _supersede(project: str = PROJECT, run_id: str | None = None, reason: str = "ok"):
    row_id = run_id or _row()["run_id"]
    return CliRunner().invoke(
        cli.main,
        [
            "crew",
            "supersede-review",
            "--project",
            project,
            "--run",
            row_id,
            "--reason",
            reason,
        ],
    )


def _reader_views(rows: list[dict]) -> dict:
    """Every named reader's view of the review block on one row."""
    from reckon import ledger

    run = rows[0]
    request = PickRequest(
        project=PROJECT,
        node=TaskNode(
            id="node-a",
            goal="read one review row",
            plan="plan-a",
            role="review",
            spec_level="standard",
        ),
        capability={"requirements": {"reasoning": "deep", "risk": "moderate"}},
    )
    counts, _peaks, at_requirement, _moved_in, _moved_off = (
        snapshot_module._candidates_scan(rows, request, now=datetime.now(UTC))
    )
    entry = (at_requirement or {}).get(("local", "local"), {})
    outcome = outcomes_module.summarize({PROJECT: rows}, {})
    group = (outcome.get("routed_outcomes") or [{}])[0]
    return {
        "accessor": ledger.review_block(run),
        "ledger_dimensions": ledger.review_scores(rows),
        "capabilities_total": _arm_total(run),
        "picker_reviews": list(entry.get("reviews") or []),
        "picker_reviewed": entry.get("reviewed", 0),
        "picker_counts": dict(entry.get("counts") or {}),
        "outcomes_median": group.get("review_score_median"),
        "counts_present": bool(counts),
    }


def _arm_total(run: dict):
    from reckon import capabilities

    return capabilities._arm_figures(run)["review_total"]


def test_an_uncorrected_row_is_read_with_its_review(ledger_repo):
    """The control: with no correction the same readers do see the review."""
    from reckon import ledger

    data, _version = ledger.load(PROJECT)
    views = _reader_views(data["runs"])
    assert views["accessor"] is not None
    assert views["ledger_dimensions"][0]["total"] == 0.75
    assert views["capabilities_total"] == 0.75
    assert views["picker_reviews"] == [0.75]
    assert views["picker_reviewed"] == 1
    assert views["outcomes_median"] == 0.75


def test_supersede_appends_one_correction_naming_both_heads_and_one_commit(ledger_repo):
    root, state_dir = ledger_repo
    row_id = _row()["run_id"]
    target = _run_path(state_dir, row_id)
    before = json.loads(target.read_text(encoding="utf-8"))
    commits_before = _commit_count(root)

    result = _supersede()
    assert result.exit_code == 0, result.output

    after = json.loads(target.read_text(encoding="utf-8"))
    correction = after.pop("review_correction")
    assert before == after, "a recorded field changed"
    assert correction["dispatched_head"] == DISPATCHED_HEAD
    assert correction["recorded_head"] == RECORDED_HEAD
    assert correction["superseded_field"] == "review"
    assert correction["reason"] == "ok"
    assert correction["time"]

    aggregate = json.loads((state_dir / "crew.json").read_text(encoding="utf-8"))[
        "data"
    ]["runs"]
    assert aggregate[0]["review_correction"] == correction
    assert _commit_count(root) == commits_before + 1
    subjects = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=root, check=True, capture_output=True
    ).stdout.decode()
    assert "supersede-review(" in subjects


def test_a_corrected_row_reads_as_unreviewed_through_every_reader(ledger_repo):
    from reckon import ledger

    assert _supersede().exit_code == 0
    data, _version = ledger.load(PROJECT)
    views = _reader_views(data["runs"])
    assert views["accessor"] is None
    assert views["ledger_dimensions"] == []
    assert views["capabilities_total"] is None
    assert views["picker_reviews"] == []
    assert views["picker_reviewed"] == 0
    assert views["outcomes_median"] is None
    # The row is still counted; only its review is withdrawn.
    assert views["picker_counts"].get("passed") == 1


def test_the_verb_refuses_a_row_whose_recorded_head_is_its_dispatched_head(ledger_repo):
    root, state_dir = ledger_repo
    _write_row(
        state_dir,
        _row(review={**_row()["review"], "reviewed_head_sha": DISPATCHED_HEAD}),
    )
    before = _commit_count(root)
    result = _supersede()
    assert result.exit_code != 0
    assert "correctly attributed" in result.output
    assert _commit_count(root) == before


def test_the_verb_refuses_a_twelve_character_prefix_of_its_recorded_head(ledger_repo):
    """A row whose dispatched head is a prefix of its recorded head is correct."""
    root, state_dir = ledger_repo
    full = "45213bf1cd6216df9a0b1c2d3e4f5061728394a5"
    prefix = full[:12]
    row = _row(
        node=round_node_id("review-of-", "vertical-pin", prefix),
        node_definition={
            "write_paths": [],
            "capability": {"requirements": {"reasoning": "deep", "risk": "moderate"}},
        },
        review={**_row()["review"], "reviewed_head_sha": full},
    )
    _write_row(state_dir, row)
    before = _commit_count(root)
    result = _supersede(run_id=row["run_id"])
    assert result.exit_code != 0
    assert "correctly attributed" in result.output
    assert _commit_count(root) == before


def test_the_verb_refuses_a_row_with_no_dispatched_head(ledger_repo):
    root, state_dir = ledger_repo
    row = _row(
        node="r-20261018T192200408612-review-of-hand-dispatched",
        node_definition={
            "write_paths": [],
            "capability": {"requirements": {"reasoning": "deep", "risk": "moderate"}},
        },
    )
    _write_row(state_dir, row)
    before = _commit_count(root)
    result = _supersede(run_id=row["run_id"])
    assert result.exit_code != 0
    assert "no dispatched head" in result.output
    assert _commit_count(root) == before


def test_the_verb_refuses_a_row_that_is_already_corrected(ledger_repo):
    root, _state_dir = ledger_repo
    assert _supersede().exit_code == 0
    before = _commit_count(root)
    result = _supersede()
    assert result.exit_code != 0
    assert "already carries a review correction" in result.output
    assert _commit_count(root) == before


def _write_row(state_dir: Path, row: dict) -> None:
    """Replace the fixture's single row with ``row`` in the file and the aggregate."""
    path = state_dir / "crew.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["data"]["runs"] = [row]
    path.write_text(json.dumps(envelope) + "\n")
    for stale in (state_dir / "runs").glob("*.json"):
        if stale.name != f"{row['run_id']}.json":
            stale.unlink()
    _run_path(state_dir, row["run_id"]).write_text(
        json.dumps(row, sort_keys=True, indent=2) + "\n"
    )
