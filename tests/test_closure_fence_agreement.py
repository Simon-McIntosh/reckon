"""Closure and dispatch read one definition of ``unreconciled``.

Two surfaces decide the same word from the same recorded facts: the closure
drain (the count a session signs off on) and the dispatch fence (the pointer
set that refuses the next dispatch). Both call
:func:`reckon.crew.recovery.closure_disposition_valid`, so a pointer the drain
counts as reconciled is never refused by dispatch, and a pointer the drain
still counts as unreconciled stays refused on both surfaces.

The load-bearing cases this file asserts:

- a terminal pointer past the grace carrying no disposition is still refused by
  dispatch and unreconciled on the drain -- the forgotten-work case the fence
  exists for;
- a ``still-working`` disposition on a pointer that has since gone terminal
  excuses nothing on either surface;
- a ``handed-off`` pointer is reconciled on both surfaces;
- over a set mixing those three cases, the count the drain reports and the
  count dispatch refuses on are equal.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path, home):
    """A throwaway git repository carrying the worktree fleet script."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>
"""
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _write_terminal_pointer(
    home: Path,
    run_id: str,
    *,
    age_seconds: int,
    status: str = "complete",
) -> dict:
    """Create one project-scoped pointer whose manifest has a known terminal age."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: delivered-node\nstatus: {status}\ncommits: HEAD\n")
    terminal = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    os.utime(manifest, (terminal.timestamp(), terminal.timestamp()))
    record = {
        "run_id": run_id,
        "project": "proj",
        "repo": "/temporary/repository",
        "node": {
            "id": "delivered-node",
            "plan": "plan-a",
            "time_budget": "20m",
        },
        "phase": "complete",
        "created_at": (terminal - timedelta(days=2)).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "absent-stream.jsonl"),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _grace_config() -> dict:
    return {**CONFIG, "fences": {**CONFIG["fences"], "unreconciled_run_grace": "5m"}}


def _node(**overrides) -> crew.TaskNode:
    """A well-formed node; each test spoils exactly the property it studies."""
    fields = {
        "id": "node-a",
        "goal": "record the launch matrix for one backend",
        "plan": "plan-a",
        "section": "§3",
        "done_when": "uv run pytest tests/test_backends.py reports 34 passed",
        "write_paths": ["reckon/_backends.py"],
        "time_budget": "20m",
        "manifest_path": "/tmp/node-a-manifest.md",  # noqa: S108 — fixture value matching the canonical node helper; never opened
        "spec_level": "guided",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def test_terminal_pointer_without_a_disposition_stays_refused(home) -> None:
    _write_terminal_pointer(home, "r-undisposed", age_seconds=601)

    overdue = recovery.overdue_unreconciled_runs(project="proj", grace="5m")
    report = crew.drain("proj")

    assert [row["run_id"] for row in overdue] == ["r-undisposed"]
    assert report["unreconciled_runs"] == 1


def test_still_working_does_not_excuse_a_terminal_pointer_past_grace(home) -> None:
    _write_terminal_pointer(home, "r-outlived", age_seconds=601)
    crew.record_run_disposition("r-outlived", "still-working", project="proj")

    overdue = recovery.overdue_unreconciled_runs(project="proj", grace="5m")
    report = crew.drain("proj")

    assert [row["run_id"] for row in overdue] == ["r-outlived"]
    assert report["unreconciled_runs"] == 1
    assert report["runs"][0]["disposition_valid"] is False


def test_handed_off_pointer_is_reconciled_on_both_fences(home) -> None:
    _write_terminal_pointer(home, "r-handed", age_seconds=601)
    crew.record_run_disposition("r-handed", "handed-off", project="proj")

    overdue = recovery.overdue_unreconciled_runs(project="proj", grace="5m")
    report = crew.drain("proj")

    assert overdue == []
    assert report["unreconciled_runs"] == 0
    assert report["runs"][0]["disposition_valid"] is True


def test_drain_and_dispatch_counts_agree_over_mixed_pointers(home) -> None:
    _write_terminal_pointer(home, "r-undisposed-1", age_seconds=601)
    _write_terminal_pointer(home, "r-undisposed-2", age_seconds=601)
    _write_terminal_pointer(home, "r-outlived", age_seconds=601)
    crew.record_run_disposition("r-outlived", "still-working", project="proj")
    _write_terminal_pointer(home, "r-handed-off-1", age_seconds=601)
    crew.record_run_disposition("r-handed-off-1", "handed-off", project="proj")
    _write_terminal_pointer(home, "r-handed-off-2", age_seconds=601)
    crew.record_run_disposition("r-handed-off-2", "handed-off", project="proj")

    overdue = recovery.overdue_unreconciled_runs(project="proj", grace="5m")
    report = crew.drain("proj")

    refused = {row["run_id"] for row in overdue}
    assert refused == {"r-undisposed-1", "r-undisposed-2", "r-outlived"}
    # A pointer the drain counts as unreconciled is refused, and one it counts
    # as reconciled is not; the two counts therefore have to agree.
    assert report["unreconciled_runs"] == len(overdue) == len(refused)
    assert report["disposed_runs"] == 2


def test_dispatch_refuses_only_the_unreconciled_subset(home, repo) -> None:
    _write_terminal_pointer(home, "r-undisposed", age_seconds=601)
    _write_terminal_pointer(home, "r-outlived", age_seconds=601)
    crew.record_run_disposition("r-outlived", "still-working", project="proj")
    _write_terminal_pointer(home, "r-handed", age_seconds=601)
    crew.record_run_disposition("r-handed", "handed-off", project="proj")

    with pytest.raises(crew.UnreconciledRuns) as excinfo:
        crew.dispatch(
            node=_node(id="next-node"),
            project="proj",
            repo=repo,
            config=_grace_config(),
            session="sess",
            launcher=lambda *args, **kwargs: pytest.fail("dispatch must be refused"),
        )

    refused = {row["run_id"] for row in excinfo.value.runs}
    assert refused == {"r-undisposed", "r-outlived"}
    assert "r-handed" not in refused


def test_dispatch_accepts_a_handed_off_pointer(home, repo) -> None:
    _write_terminal_pointer(home, "r-handed", age_seconds=601)
    crew.record_run_disposition("r-handed", "handed-off", project="proj")

    record = crew.dispatch(
        node=_node(id="next-node"),
        project="proj",
        repo=repo,
        config=_grace_config(),
        session="sess",
        launcher=lambda *args, **kwargs: 4242,
    )

    assert record["run_id"] != "r-handed"
    assert record["unreconciled_override"] is None
