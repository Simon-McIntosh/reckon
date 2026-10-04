"""A worktree-held row is rechecked against the fleet when the hook reads it.

A promotion deletes its run's live pointer, then releases the tree, then writes
the release onto the run's ledger record. A sweep that runs between the pointer
deletion and the release publishes a worktree-held row that is true at the
instant it is taken and false by the time anyone reads it: the tree is already
being reclaimed, and the duty's own remedy — a run-scoped ``crew gc`` — is
refused for a run with no tree. Each stale row costs the coordinator an
acknowledgement, so the hook re-reads the run's committed record and the tree
where the row is offered, and drops it when the record shows the tree released
or the tree is no longer a directory.

The released case is driven by a real ``crew complete``: the snapshot is taken
from inside the promotion, at the moment the row is still a true reading, and
the same promotion then releases the tree before the hook is invoked. Every
fixture is written by the writer that writes it in production — ``crew``
promotes, the ledger appends the record, and the snapshot module publishes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import obligation_snapshot, promotion, runs
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks.coordinator_obligations import format_checklist

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "worktree-row-recheck-fixture"
SESSION = "s22-recheck-fixture"
PLAN = "recheck-fixture-plan"
NODE_ID = "recheck-fixture-node"
RUN_ID = f"r-20261004T070000000000-{NODE_ID}"
COMPLETED_AT = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)

# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than the runner's ambient harness identity.
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


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Worktree row recheck",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _write_plan(repository / "docs" / "plans" / f"{PLAN}.html")
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "docs", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: seed the recheck fixture")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    with runs.follower_claim(PROJECT, SESSION) as claim:
        assert claim[0] is True, claim[1]
        yield repository


def _worktree(repository: Path, tmp_path: Path) -> Path:
    """Register a tree the way a dispatched run leaves one behind.

    The directory's own name is the node id, which is how a run's ledger
    record names its tree when the promotion recorded no retention.
    """
    tree = tmp_path / "trees" / SESSION / NODE_ID
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def _live_pointer(repository: Path, tmp_path: Path, tree: Path) -> None:
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {NODE_ID}\nstatus: complete\n", encoding="utf-8")
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(tree),
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-10-04T06:00:00Z",
        "base_sha": _git(repository, "rev-parse", "HEAD"),
        "manifest_path": str(manifest),
        "session_id": SESSION,
        "node": {
            "id": NODE_ID,
            "plan": PLAN,
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    path = runs.pointer_path(RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _retained_record(tree: Path) -> dict[str, Any]:
    """One committed record whose retention still names the tree."""
    record = ledger.build_record(
        run_id=RUN_ID,
        plan=PLAN,
        gate="failed",
        node=NODE_ID,
        completed_at=COMPLETED_AT.isoformat(),
    )
    record["worktree_retention"] = {
        "classification": "retained-for-resume",
        "worktree": str(tree.resolve()),
        "session_id": SESSION,
        "session_source": "pointer",
        "retained_at": COMPLETED_AT.isoformat(),
    }
    return record


def _producer() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": datetime.now(tz=UTC).isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }


def _publish(payload: dict[str, Any]) -> Path:
    document = obligation_snapshot.document_for(
        payload,
        computed_at=datetime.now(tz=UTC),
        stream_offset=0,
        producer=_producer(),
    )
    return obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _drive(repository: Path, *, mode: str = "stop") -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    completed = subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(
            {
                "session_id": SESSION,
                "cwd": str(repository),
                "hook_event_name": "Stop" if mode == "stop" else "UserPromptSubmit",
            }
        ),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.stderr == "", completed.stderr
    return completed


def _capture_snapshot_before_release(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Publish the session's snapshot at the instant a promotion releases.

    The pointer is already gone and the run's row is committed, while the tree
    is still registered and on disk: the sweep window this row exists in, taken
    deterministically rather than raced.
    """
    original = promotion._release_after_promotion
    captured: dict[str, Any] = {}

    def _observe(run_id, record, retention=None, **kwargs):
        payload = obligations_view(PROJECT, SESSION)
        captured["payload"] = payload
        _publish(payload)
        return original(run_id, record, retention, **kwargs)

    monkeypatch.setattr(promotion, "_release_after_promotion", _observe)
    return captured


def test_a_tree_the_promotion_released_is_not_offered(
    fleet: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale row is dropped where it is offered, and its run is named once."""
    tree = _worktree(fleet, tmp_path)
    _live_pointer(fleet, tmp_path, tree)
    captured = _capture_snapshot_before_release(monkeypatch)

    promoted = crew.complete(
        RUN_ID,
        gate="passed",
        outcome="the synthesized run lands nothing and releases its tree",
        review_waiver="the synthesized fixture has no code review",
        root=fleet,
    )

    assert promoted["release"]["worktree_released"] is True
    assert not tree.exists()
    row = next(
        item
        for item in captured["payload"]["obligations"]
        if item.get("kind") == "worktree-held"
    )
    assert row["run_id"] == RUN_ID
    # The snapshot the hook reads still carries the row, so the drive below is
    # an assertion about the hook and not about a snapshot that lost it.
    assert RUN_ID in format_checklist(dict(captured["payload"]))

    completed = _drive(fleet)

    assert completed.stdout == "", (
        "the released row was the only duty and must not hold the turn open: "
        f"{completed.stdout}"
    )


def test_a_held_tree_is_still_offered(fleet: Path, tmp_path: Path) -> None:
    """The same pipeline still raises the duty while the tree is genuinely held."""
    tree = _worktree(fleet, tmp_path)
    ledger.append_run(PROJECT, _retained_record(tree), root=fleet, allow_create=True)
    _publish(obligations_view(PROJECT, SESSION))

    completed = _drive(fleet)

    assert "[worktree-held]" in completed.stdout, completed.stdout
    assert RUN_ID in completed.stdout, completed.stdout


def test_a_vanished_tree_is_not_offered(fleet: Path, tmp_path: Path) -> None:
    """The tree check alone drops the row when no release was recorded."""
    tree = _worktree(fleet, tmp_path)
    ledger.append_run(PROJECT, _retained_record(tree), root=fleet, allow_create=True)
    _publish(obligations_view(PROJECT, SESSION))
    shutil.rmtree(tree)
    assert str(tree.resolve()) in _git(fleet, "worktree", "list", "--porcelain")

    completed = _drive(fleet)

    assert "worktree-held" not in completed.stdout, completed.stdout


def test_a_recorded_release_is_not_offered_while_the_path_exists(
    fleet: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's own release verdict is read as well as the tree's absence."""
    tree = _worktree(fleet, tmp_path)
    _live_pointer(fleet, tmp_path, tree)
    captured = _capture_snapshot_before_release(monkeypatch)
    crew.complete(
        RUN_ID,
        gate="passed",
        outcome="the synthesized run lands nothing and releases its tree",
        review_waiver="the synthesized fixture has no code review",
        root=fleet,
    )
    assert not tree.exists()
    # A directory reappears at the released path. The record still decides, so
    # the row stays dropped however the path now reads.
    tree.mkdir(parents=True)
    _publish(captured["payload"])

    completed = _drive(fleet)

    assert "worktree-held" not in completed.stdout, completed.stdout
