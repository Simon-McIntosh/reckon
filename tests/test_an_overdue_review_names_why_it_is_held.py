"""An overdue review names the refusal that holds it.

When an armed follower's review window has passed and the review still has not
been dispatched, the duty used to send the coordinator to inspect the follower.
The follower is usually healthy: the reflex's own dispatch rule declines because
the run's newest review still carries a blocking finding with no disposition,
and the refusal names the ``crew dispose`` command that lifts it. The duty now
reads that refusal through the predicate ``dispatch_review_for_run`` applies, so
the reasons cannot disagree.

Both cases run on a synthesised repository, a temporary configuration home and
an armed follower, so no reader reaches a real checkout or the operator's crew
home.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import runs
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")
dispatch_module = importlib.import_module("reckon.crew.recovery_review_dispatch")

PROJECT = "overdue-review-fixture"
SESSION = "overdue-review-session"
BLOCKING = {
    "file": "reckon/crew/thing.py",
    "line": "10",
    "text": "the guard never fires",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _commit(root, "seed.txt", "seed\n")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _store_review(
    run_id: str, *, head: str, findings: list[dict] | None = None
) -> None:
    """Store a complete review of ``run_id`` at the revision it read."""
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
            "findings": list(findings or []),
        }
    )
    review_module.store_review(record)


def _delivered_run(
    repository: Path, tmp_path: Path, *, run_id: str, head: str, age_seconds: int
) -> dict:
    """One complete, unpromoted run whose tree carries ``head``.

    ``age_seconds`` is stamped on the manifest the classifier ages the row by,
    so a run can be delivered long enough ago for the reflex's review window to
    have passed.
    """
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}-node\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    stamped = time.time() - age_seconds
    os.utime(manifest, (stamped, stamped))
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": f"{run_id}-node",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _overdue_run(
    repository: Path, tmp_path: Path, *, run_id: str, finding: bool
) -> None:
    """A run delivered past the reflex window with a review at an earlier head."""
    reviewed = _git(repository, "rev-parse", "HEAD")
    _store_review(run_id, head=reviewed, findings=[dict(BLOCKING)] if finding else None)
    head = _commit(repository, "reckon/repair_source.py", f"VALUE = {run_id!r}\n")
    _delivered_run(
        repository,
        tmp_path,
        run_id=run_id,
        head=head,
        age_seconds=obligations_module._reflex_review_window_seconds() + 60,
    )


def _duty(result: dict) -> dict:
    duties = [
        item for item in result["obligations"] if item["kind"] == "review-missing"
    ]
    assert len(duties) == 1, [
        (item["kind"], item["run_id"]) for item in result["obligations"]
    ]
    return duties[0]


def test_an_overdue_review_names_the_undisposed_finding(
    repository: Path, tmp_path: Path
) -> None:
    """The duty names the refusal and the dispose command that lifts it."""
    run_id = "run-held-by-finding"
    _overdue_run(repository, tmp_path, run_id=run_id, finding=True)
    pointer_path = runs.pointer_path(run_id)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        before = pointer_path.read_bytes()
        result = obligations_module.obligations(PROJECT, SESSION)
        after = pointer_path.read_bytes()

    # Reporting a held review must not itself act on it: the duty reads the
    # refusal through the dispatch predicate's read-only form, so a reader that
    # only reports leaves the run's live pointer byte-identical.
    assert after == before, (
        "the obligations read rewrote the run's live pointer; it now also "
        f"carries {sorted(set(json.loads(after)) - set(json.loads(before)))}"
    )

    duty = _duty(result)
    command = str(duty["next_command"])
    # Asserted before the reason is read so a dropped refusal read is reported
    # as the stale follower text the duty used to print, not as a missing key.
    assert "inspect the follower's review pass" not in command, duty
    reason = str(duty["reason"])

    fold, exempt = dispatch_module._finding_dispose_commands(PROJECT, run_id, 0)
    assert fold in command, duty
    assert exempt in command, duty
    assert "blocking finding" in reason, duty
    # The refusal is the reflex's, not the follower's, so the duty is not a wait.
    assert "review_owner" not in duty, duty

    # The duty reads the same predicate the dispatch applies, so the two cannot
    # name different reasons for the same held review.
    report = dispatch_module.dispatch_review_for_run(
        runs.read_pointer(run_id), dry_run=True
    )
    assert report.get("refused") is True, report
    assert report["reason"] == reason, (report, duty)


def test_an_overdue_review_with_no_held_finding_keeps_the_follower_text(
    repository: Path, tmp_path: Path
) -> None:
    """With nothing refusing the dispatch, the duty reads as it did before."""
    run_id = "run-no-held-finding"
    _overdue_run(repository, tmp_path, run_id=run_id, finding=False)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        result = obligations_module.obligations(PROJECT, SESSION)

    duty = _duty(result)
    command = str(duty["next_command"])
    assert "inspect the follower's review pass" in command, duty
    assert "crew dispose" not in command, duty
