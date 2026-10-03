"""An unreadable review record is skipped, named, and cannot stop a sweep.

A review worker composes its record file itself, so a reader can arrive while
the file is still being written. A truncated record once raised out of
``stored_record`` with ``json.decoder.JSONDecodeError`` and killed the
producer's sweep thread, so every session's snapshot went stale behind one
unreadable file. These cases pin the three readings that replace it: the reader
answers from the records it could read and names the file it could not, one
failed sweep or slice costs only what it touched, and the next sweep is free to
publish.

The fleet is synthesised under the test's own temporary path and a temporary
configuration home: the sweep is driven explicitly, never by waiting on a
clock.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew import review as review_module

PROJECT = "unreadable-record-fixture"
SESSION = "coordinator-fixture"
PRODUCER = {
    "pid": os.getpid(),
    "pid_start_time": None,
    "started_at": None,
    "code_stamp": "stamp-not-read-by-these-cases",
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A synthesised project under a temporary configuration home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # A declared floor makes the sweep's sub-floor lookup read each live run's
    # review, which is the write-time-unprotected read the truncation used to
    # reach. Without it the sweep would depend only on the classifier, whose
    # own review read was already guarded.
    (config_home / "flight.yaml").write_text(
        "gates:\n  dimension_floors:\n    evidence: 15\n", encoding="utf-8"
    )
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the unreadable-record fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return {"home": config_home, "repo": root}


def _write_pointer(
    fleet: dict[str, Path], run_id: str, *, phase: str, status: str
) -> None:
    """Write one live pointer and the manifest its delivery is read from."""
    root = fleet["repo"]
    manifest = fleet["home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": head,
            "process_alive": False,
            "phase": phase,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _state_dirs(fleet: dict[str, Path]) -> list[Path]:
    return [
        fleet["repo"] / "docs" / "state" / PROJECT,
        fleet["repo"] / "docs" / "plans",
    ]


def _store_valid_review(run_id: str, head: str) -> Path:
    """Store one complete review record naming the revision it read."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
        }
    )
    return review_module.store_review(record)


def _sweep(fleet: dict[str, Path], sessions: list[str]) -> list[Path]:
    """Drive exactly one sweep over the fixture's files."""
    return obligation_snapshot.sweep(
        PROJECT,
        sessions=sessions,
        producer=PRODUCER,
        stream_offset=0,
        transition_fired=True,
        state_dirs=_state_dirs(fleet),
    )


def _truncated_record() -> str:
    """A record caught mid-write: valid JSON cut off inside a string."""
    return (
        f'{{"project": "{PROJECT}", "reviewed_run_id": "r-reviewed", '
        '"scores": {"goal_fidelity": 19, "evid'
    )


def test_a_truncated_record_is_skipped_and_named_in_the_snapshot(
    fleet: dict[str, Path],
) -> None:
    """The valid record's duty publishes, and the unreadable path is a finding."""
    head = _git(fleet["repo"], "rev-parse", "HEAD")
    _write_pointer(fleet, "r-reviewed", phase="complete", status="complete")
    _store_valid_review("r-reviewed", head)
    truncated = review_module.review_path(PROJECT, "r-reviewed")
    truncated.write_text(_truncated_record(), encoding="utf-8")
    assert truncated.is_file()

    written = _sweep(fleet, [SESSION])

    assert written, "the sweep publishes despite the truncated record"
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    duties = [
        item for item in document["obligations"] if item["run_id"] == "r-reviewed"
    ]
    assert duties, (
        "the valid record beside the truncated one must still read as evidence",
        document["obligations"],
    )
    findings = {row["path"]: row for row in document["findings"]}
    assert str(truncated) in findings, document["findings"]
    assert "JSONDecodeError" in findings[str(truncated)]["error"]


def test_one_sessions_failure_does_not_stop_the_others(
    fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A slice that raises is logged with its traceback; other sessions publish."""
    _write_pointer(fleet, "r-working", phase="working", status="working")
    real_payload_for = obligation_snapshot.payload_for

    def flaky(state: obligation_snapshot.FleetState, session: str) -> dict[str, Any]:
        if session == "s-failing":
            raise RuntimeError("this session's slice cannot be built")
        return real_payload_for(state, session)

    monkeypatch.setattr(obligation_snapshot, "payload_for", flaky)

    written = _sweep(fleet, ["s-failing", "s-published"])

    assert [path.name for path in written] == [
        obligation_snapshot.snapshot_path(PROJECT, "s-published").name
    ]
    assert obligation_snapshot.read_snapshot(PROJECT, "s-published") is not None
    assert obligation_snapshot.read_snapshot(PROJECT, "s-failing") is None
    stderr = capsys.readouterr().err
    assert "obligation-sweep-failure" in stderr
    assert "this session's slice cannot be built" in stderr


def test_a_failed_sweep_leaves_the_next_free_to_publish(
    fleet: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sweep that raises for any other reason is logged, and the next publishes."""
    _write_pointer(fleet, "r-working", phase="working", status="working")
    real_fleet_state = obligation_snapshot.fleet_state
    calls = {"n": 0}

    def flaky(project: str, *, now: Any = None) -> obligation_snapshot.FleetState:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the fleet state could not be derived")
        return real_fleet_state(project, now=now)

    monkeypatch.setattr(obligation_snapshot, "fleet_state", flaky)

    assert _sweep(fleet, [SESSION]) == []
    assert obligation_snapshot.read_snapshot(PROJECT, SESSION) is None
    stderr = capsys.readouterr().err
    assert "the fleet state could not be derived" in stderr

    assert _sweep(fleet, [SESSION]), "the next sweep publishes"
    assert obligation_snapshot.read_snapshot(PROJECT, SESSION) is not None
