"""A skipped review record reaches the hook's checklist.

A review worker composes its record file itself, so a reader can meet one
mid-write. The reader skips it and the sweep records the path as a finding;
these cases pin the two readings that carry it from there to the checklist a
coordinator reads. The finding travels through ``payload_for`` into the
published snapshot and back out of ``live_payload`` as a duty row naming the
path, and a read made outside a sweep is nobody's finding, so nothing it met
reaches a later sweep's snapshot.

The fleet is synthesised under the test's own temporary path and a temporary
configuration home, in the shape
``tests/test_unreadable_review_record_is_reported.py`` uses, and the prompt
hook is driven as the harness drives it: the hook script with hook JSON on
stdin in a fresh process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew import review as review_module

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "skipped-record-fixture"
SESSION = "skipped-record-session"
RUN_ID = "r-reviewed"
_ABSENT_HARNESS_PID = 2_147_483_647


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
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    # A declared floor makes the sweep's sub-floor lookup read each live run's
    # review, so a record caught mid-write is reached by the derivation rather
    # than only by a reader that asks for it.
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
        ("commit", "-q", "-m", "test: seed the skipped-record fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return {"home": config_home, "repo": root}


@pytest.fixture()
def follower(fleet: dict[str, Path]):
    """Register the session as a follower arm would, for the hook to resolve."""
    with runs.follower_claim(PROJECT, SESSION) as claim:
        assert claim[0] is True, claim[1]
        yield


def _producer() -> dict[str, Any]:
    """A producer a fresh snapshot can name: this live process's own identity."""
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": datetime.now(tz=UTC).isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }


def _write_pointer(fleet: dict[str, Path], run_id: str) -> str:
    """Write one live pointer and the manifest its delivery is read from."""
    root = fleet["repo"]
    manifest = fleet["home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
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
            "phase": "complete",
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


def _plant_truncated_record(fleet: dict[str, Path]) -> Path:
    """Store one valid record and plant a second, caught mid-write, beside it."""
    head = _write_pointer(fleet, RUN_ID)
    _store_valid_review(RUN_ID, head)
    truncated = review_module.review_path(PROJECT, RUN_ID)
    truncated.write_text(
        f'{{"project": "{PROJECT}", "reviewed_run_id": "{RUN_ID}", '
        '"scores": {"goal_fidelity": 19, "evid',
        encoding="utf-8",
    )
    assert truncated.is_file()
    return truncated


def _sweep(fleet: dict[str, Path]) -> list[Path]:
    """Drive exactly one sweep over the fixture's files."""
    return obligation_snapshot.sweep(
        PROJECT,
        sessions=[SESSION],
        producer=_producer(),
        stream_offset=0,
        transition_fired=True,
        state_dirs=[
            fleet["repo"] / "docs" / "state" / PROJECT,
            fleet["repo"] / "docs" / "plans",
        ],
    )


def _findings_in(fleet: dict[str, Path]) -> list[str]:
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None, "the sweep must have published a snapshot"
    return [str(row.get("path") or "") for row in document["findings"]]


def _drive_prompt(fleet: dict[str, Path]) -> str:
    """Run the prompt hook as the harness does and return the injected text."""
    payload = {
        "session_id": SESSION,
        "cwd": str(fleet["repo"]),
        "hook_event_name": "UserPromptSubmit",
    }
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    # A pid no process can hold, so the drive's session resolves through the
    # registration's own name rather than the runner's ambient harness identity.
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    completed = subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    assert completed.stdout.strip(), "a fresh snapshot must be spoken"
    return json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]


def test_a_skipped_record_is_named_in_the_prompt_checklist(
    fleet: dict[str, Path], follower: None
) -> None:
    """The path the sweep could not read is a line of the injected checklist."""
    truncated = _plant_truncated_record(fleet)

    assert _sweep(fleet), "the sweep publishes the session's snapshot"
    assert _findings_in(fleet) == [str(truncated)], (
        "the instrument must see the record it cannot read before an absence "
        "elsewhere is believed"
    )

    checklist = _drive_prompt(fleet)

    assert checklist.startswith(f"reckon obligations for session {SESSION}"), checklist
    named = [line for line in checklist.splitlines() if str(truncated) in line]
    assert named, f"the checklist names no skipped record:\n{checklist}"
    assert named[0].startswith(f"- [{obligation_snapshot._UNREADABLE_RECORD_KIND}]"), (
        named[0]
    )


def test_the_reader_reproduces_the_findings_the_writer_carried(
    fleet: dict[str, Path], follower: None
) -> None:
    """payload_for and live_payload render the same row for one finding."""
    truncated = _plant_truncated_record(fleet)
    assert _sweep(fleet)
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    state = obligation_snapshot.fleet_state(PROJECT)
    kind = obligation_snapshot._UNREADABLE_RECORD_KIND

    written = [
        item
        for item in obligation_snapshot.payload_for(state, SESSION)["obligations"]
        if item["kind"] == kind
    ]
    read_back = [
        item
        for item in obligation_snapshot.live_payload(document)["obligations"]
        if item["kind"] == kind
    ]

    assert [item["run_id"] for item in written] == [str(truncated)]
    assert written == read_back, "the reader must reconstruct the writer's row"


def test_a_read_outside_a_sweep_leaves_the_next_sweep_free(
    fleet: dict[str, Path], follower: None
) -> None:
    """A bare read's skips are nobody's finding; the next sweep names nothing."""
    truncated = _plant_truncated_record(fleet)
    assert _sweep(fleet), "the first sweep publishes"
    assert _findings_in(fleet) == [str(truncated)]

    # A review worker's own read, outside any sweep, meets the same file.
    path, record = review_module.stored_record(PROJECT, RUN_ID)
    assert path is not None and record is not None, (
        "the read must answer from the valid record beside the truncated one"
    )

    truncated.unlink()
    assert _sweep(fleet), "the next sweep publishes"
    assert _findings_in(fleet) == [], (
        "a read outside a sweep must not reach a later sweep's findings"
    )
