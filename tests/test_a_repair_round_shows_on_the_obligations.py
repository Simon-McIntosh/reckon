"""An automatic repair round reads as its own obligation while it runs.

A review that files a blocking finding resumes the reviewed run's own worker
with the findings as advice, and records the round on that run's pointer. The
run is working again at the revision it had already delivered, so the classifier
reads it as running and the coordinator's duty list showed nothing for it: a
round answering a finding was indistinguishable from a delivery whose promotion
the coordinator still owed.

These cases pin the reading: while the round's record is open and its worker is
alive the run owes one ``repair-round`` row naming the review that raised the
finding, the finding itself and the worker pid; the stop hook lists that row
without holding the turn open; and once the round delivers, the run's head moves
past the reviewed revision and the ordinary review duty for the new head stands
again. The negative control derives the row while the round's stored review is
not read, and the naming case fails.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligations, recovery_classification, runs
from reckon.crew import review as review_module
from reckon.hooks import coordinator_obligations as hook
from tests.helpers.git import git

PROJECT = "repair-round-fixture"
SESSION = "repair-round-session"
IMPORTED_AT = datetime.now(tz=UTC)
# A duty ages a run against the clock it is handed, and the fixture stamps its
# records from the same instant, so the age the cases read is a fact about the
# fixture rather than how long the suite took to reach them.
NOW = IMPORTED_AT + timedelta(minutes=2)
FINDING = {
    "file": "src/guard.py",
    "line": "8",
    "text": "the guard allows the blocked path",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}
REVIEW_RUN_ID = "r-review-of-the-round"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setattr(obligations, "_utc_now", lambda: NOW)
    # The rows are classified without a caller-supplied instant, so the
    # classifier's own clock boundary is pinned here too.
    monkeypatch.setattr(recovery_classification, "_utc_seconds", NOW.timestamp)
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed fixture"),
    ):
        git(root, *args)
    (config / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root, git(root, "rev-parse", "HEAD")


def _dead_pid() -> int:
    """A pid that has exited and been reaped, so liveness reads it as gone."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _pointer(
    home: tuple[Path, str],
    run_id: str,
    *,
    head: str,
    pid: int,
    round_record: bool = True,
) -> dict:
    """One reviewed run's pointer, resumed by a repair round at ``head``."""
    root, _seed = home
    manifest = root.parent / f"{run_id}.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(root),
        "worktree": str(root),
        "base_sha": head,
        "phase": "working",
        # The worker the round resumed, alive on this host: the pid is this
        # process, so the liveness read is a real one rather than a faked answer.
        "launch": "spawned",
        "launcher_host": socket.gethostname(),
        "pid": pid,
        "role": "implement",
        "manifest_path": str(manifest),
        "resume_superseded_head": {"head": head, "at": NOW.isoformat()},
        "node": {
            "id": run_id,
            "plan": "fixture",
            "section": "fixture",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    if round_record:
        pointer["repair_dispatch"] = {
            "status": "resumed",
            "reason": "the reviewed run was resumed with the composed findings",
            "round_id": f"{run_id}@{head}",
            "at": NOW.isoformat(),
            "attempt": 1,
        }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _review(run_id: str, head: str, *, findings: list[dict] | None = None) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": REVIEW_RUN_ID,
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
            "findings": findings or [],
        }
    )
    review_module.store_review(record)


def _rows(run_id: str) -> list[dict]:
    return [
        item
        for item in obligations.obligations(PROJECT, SESSION)["obligations"]
        if item["run_id"] == run_id
    ]


def test_a_repair_round_in_flight_names_its_review_finding_and_pid(
    home: tuple[Path, str],
) -> None:
    """The resumed run owes one repair-round row carrying its own evidence."""
    run_id = "r-round-in-flight"
    _pointer(home, run_id, head=home[1], pid=os.getpid())
    _review(run_id, home[1], findings=[FINDING])

    rows = _rows(run_id)

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == obligations.REPAIR_ROUND_DUTY_KIND, row
    assert row["review_run_id"] == REVIEW_RUN_ID, row
    assert row["finding"] == FINDING["text"], row
    assert row["pid"] == os.getpid(), row
    assert row["round_id"] == f"{run_id}@{home[1]}", row
    # The reflex owns the round, so the row names no command to run.
    assert not row.get("next_command"), row


def test_the_stop_hook_lists_the_round_without_blocking(
    home: tuple[Path, str],
) -> None:
    """The row is printed and never holds the coordinator's turn open."""
    run_id = "r-round-listed"
    _pointer(home, run_id, head=home[1], pid=os.getpid())
    _review(run_id, home[1], findings=[FINDING])

    payload = obligations.obligations(PROJECT, SESSION)

    kinds = [row["kind"] for row in payload["obligations"]]
    assert obligations.REPAIR_ROUND_DUTY_KIND in kinds, payload["obligations"]
    assert hook._blocking_items(payload["obligations"]) == []
    assert "repair-round" in hook._NON_BLOCKING_KINDS
    assert obligations.REPAIR_ROUND_DUTY_KIND in obligations.NON_BLOCKING_DUTY_KINDS
    text = hook.format_checklist(payload)
    assert "repair-round" in text, text


def test_a_live_worker_without_a_round_record_derives_no_round_row(
    home: tuple[Path, str],
) -> None:
    """The round's own record is what makes the row, not a live worker alone."""
    run_id = "r-no-round-record"
    _pointer(home, run_id, head=home[1], pid=os.getpid(), round_record=False)
    _review(run_id, home[1], findings=[FINDING])

    rows = _rows(run_id)

    assert [row["kind"] for row in rows] != [obligations.REPAIR_ROUND_DUTY_KIND], rows
    assert obligations.REPAIR_ROUND_DUTY_KIND not in {row["kind"] for row in rows}, rows


def test_after_delivery_the_row_reads_the_review_duty_for_the_new_head(
    home: tuple[Path, str],
) -> None:
    """A delivered round hands the run back to the ordinary review duty."""
    root, seeded = home
    run_id = "r-round-delivered"
    _pointer(home, run_id, head=seeded, pid=os.getpid())
    _review(run_id, seeded, findings=[FINDING])
    assert _rows(run_id)[0]["kind"] == obligations.REPAIR_ROUND_DUTY_KIND

    # The round's worker answers the finding and delivers a new head, then ends.
    (root / "guard.py").write_text("blocked = False\n", encoding="utf-8")
    git(root, "add", "guard.py")
    git(root, "commit", "-q", "-m", "fix: close the blocked path")
    new_head = git(root, "rev-parse", "HEAD")
    manifest = Path(str(runs.read_pointer(run_id)["manifest_path"]))
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{seeded}, {new_head}]\n",
        encoding="utf-8",
    )
    pointer = dict(runs.read_pointer(run_id))
    pointer["pid"] = _dead_pid()
    pointer["phase"] = ""
    runs._write_json(runs.pointer_path(run_id), pointer)

    rows = _rows(run_id)

    kinds = [row["kind"] for row in rows]
    assert obligations.REPAIR_ROUND_DUTY_KIND not in kinds, rows
    assert kinds == ["review-missing"], rows
    row = rows[0]
    assert row.get("head") == new_head, row
    assert row.get("review_run_id") != REVIEW_RUN_ID, row
