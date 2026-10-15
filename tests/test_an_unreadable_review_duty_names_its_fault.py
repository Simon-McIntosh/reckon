"""The duty for an unreadable review record names the fault the reader found.

An unreadable review record earns one obligation row, and that row's remedy
must fit the fault. A record met mid-write does not parse, so waiting for the
writer to finish or dropping the unfinished file is right. A record that
parses but holds a finding stating no claim is complete and will never change:
waiting for a writer that already finished would never help, and removing the
file would lose the review. These cases pin the two readings: the truncated
record keeps the read-again remedy, and the claimless record names how many
entries are claimless and the fields a claim may use, with a remedy that
rewrites the record or resumes the reviewing run.

The fleet is synthesised under the test's own temporary path and a temporary
configuration home, in the shape ``tests/test_unreadable_review_record_is_reported.py``
uses, and the sweep is driven explicitly, never by waiting on a clock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

PROJECT = "unreadable-duty-fixture"
SESSION = "coordinator-fixture"
RUN_ID = "r-reviewed"
PRODUCER = {
    "pid": os.getpid(),
    "pid_start_time": None,
    "started_at": None,
    "code_stamp": "stamp-not-read-by-these-cases",
}


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A synthesised project under a temporary configuration home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # A declared floor makes the sweep's sub-floor lookup read each live run's
    # review, which is the read that meets each record and reports its fault.
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
        ("commit", "-q", "-m", "test: seed the unreadable-duty fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return {"home": config_home, "repo": root}


def _write_pointer(
    fleet: dict[str, Path], run_id: str, *, phase: str, status: str
) -> str:
    """Write one live pointer and the manifest its delivery is read from."""
    root = fleet["repo"]
    manifest = fleet["home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head}]\n", encoding="utf-8"
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
    return head


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


def _sweep(fleet: dict[str, Path]) -> list[Path]:
    """Drive exactly one sweep over the fixture's files."""
    return obligation_snapshot.sweep(
        PROJECT,
        sessions=[SESSION],
        producer=PRODUCER,
        stream_offset=0,
        transition_fired=True,
        state_dirs=_state_dirs(fleet),
    )


def _truncated_record() -> str:
    """A record caught mid-write: valid JSON cut off inside a string."""
    return (
        f'{{"project": "{PROJECT}", "reviewed_run_id": "{RUN_ID}", '
        '"scores": {"goal_fidelity": 19, "evid'
    )


def _unreadable_rows(fleet: dict[str, Path]) -> list[dict[str, Any]]:
    """The unreadable-record duty rows a published snapshot carries."""
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None, "the sweep must have published a snapshot"
    return [
        item
        for item in document["obligations"]
        if item["kind"] == obligation_snapshot._UNREADABLE_RECORD_KIND
    ]


def _duty_next_command(fleet: dict[str, Path], path: Path) -> str:
    """The remedy the published duty for one unreadable path carries."""
    rows = [row for row in _unreadable_rows(fleet) if row["run_id"] == str(path)]
    assert rows, (
        "the instrument must name the unreadable path before a remedy means "
        f"anything; it named {[row['run_id'] for row in _unreadable_rows(fleet)]}"
    )
    return str(rows[0]["next_command"])


def test_a_truncated_record_keeps_the_mid_write_remedy(fleet: dict[str, Path]) -> None:
    """A record that does not parse still earns the read-again remedy."""
    head = _write_pointer(fleet, RUN_ID, phase="complete", status="complete")
    _store_valid_review(RUN_ID, head)
    truncated = review_module.review_path(PROJECT, RUN_ID)
    truncated.write_text(_truncated_record(), encoding="utf-8")
    assert truncated.is_file()

    assert _sweep(fleet), "the sweep publishes despite the truncated record"

    remedy = _duty_next_command(fleet, truncated)
    assert "writer finishes" in remedy, remedy
    assert "remove" in remedy, remedy


def test_a_claimless_record_names_its_fault_and_does_not_wait(
    fleet: dict[str, Path],
) -> None:
    """A record that parses but holds a claimless finding is rewritten, not awaited."""
    head = _write_pointer(fleet, RUN_ID, phase="complete", status="complete")
    stored = _store_valid_review(RUN_ID, head)
    record = json.loads(stored.read_text(encoding="utf-8"))
    record["findings"] = [
        {
            "message": "this entry states no claim",
            "path": "reckon/crew/obligation_snapshot.py",
            "severity": "follow-on",
        }
    ]
    stored.write_text(json.dumps(record), encoding="utf-8")

    assert _sweep(fleet), "the sweep publishes for the claimless record"

    remedy = _duty_next_command(fleet, stored)
    assert "1 claimless finding(s)" in remedy, remedy
    for field in obligation_snapshot.FINDING_CLAIM_FIELDS:
        assert field in remedy, remedy
    assert "rewrite" in remedy and "resume the reviewing run" in remedy, remedy
    assert "writer finishes" not in remedy, remedy
    assert "remove" not in remedy, remedy


def test_the_duty_is_the_same_row_read_back_from_the_snapshot(
    fleet: dict[str, Path],
) -> None:
    """The hook's reader rebuilds the same claimless row the sweep published."""
    head = _write_pointer(fleet, RUN_ID, phase="complete", status="complete")
    stored = _store_valid_review(RUN_ID, head)
    record = json.loads(stored.read_text(encoding="utf-8"))
    record["findings"] = [{"message": "no claim here"}]
    stored.write_text(json.dumps(record), encoding="utf-8")
    assert _sweep(fleet)
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    state = obligation_snapshot.fleet_state(PROJECT)
    kind = obligation_snapshot._UNREADABLE_RECORD_KIND

    written = [
        row
        for row in obligation_snapshot.payload_for(state, SESSION)["obligations"]
        if row["kind"] == kind
    ]
    read_back = [
        row
        for row in obligation_snapshot.live_payload(document)["obligations"]
        if row["kind"] == kind
    ]
    assert [row["run_id"] for row in written] == [str(stored)]
    assert written == read_back, "the reader must reconstruct the writer's remedy"
    assert "claimless" in written[0]["next_command"], written[0]


def test_the_remedy_tells_the_two_faults_apart() -> None:
    """The reader's own error string is what the duty reads the fault from."""
    mid_write = {"path": "/store/r.json", "error": "JSONDecodeError: Expecting value"}
    claimless = {
        "path": "/store/r.json",
        "error": "ValueError: 3 finding(s) in this record state no claim",
    }
    assert "writer finishes" in obligation_snapshot._unreadable_record_remedy(mid_write)
    remedy = obligation_snapshot._unreadable_record_remedy(claimless)
    assert "3 claimless finding(s)" in remedy, remedy
    assert "writer finishes" not in remedy
    assert "remove" not in remedy


def test_the_claim_fields_mirror_the_review_reader() -> None:
    """The fields the duty names are the reader's own accepted field set."""
    assert (
        obligation_snapshot.FINDING_CLAIM_FIELDS == review_module.FINDING_CLAIM_FIELDS
    )
