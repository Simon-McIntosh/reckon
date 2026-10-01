"""A re-promotion commits the plan comment its own earlier attempt recorded.

Promotion writes the landing comment to the plan HTML, then appends the ledger
row, then commits both stores as one landing. When that landing commit fails —
a held ``index.lock`` is the measured case — the rollback restores what it can:
a newly created ledger run file is dropped, but the tracked plan file survives
the failed rollback because the same lock that blocked the commit also blocks
``git restore``. The plan comment therefore stays on disk, uncommitted.

The next promotion reads that comment back, finds it already recorded with the
same narrative, and must carry the plan file into its landing commit so the
checkout is left clean. It does not: the already-recorded branch excluded the
plan file from the landing commit under the assumption that an idempotent retry
leaves the plan unchanged, which is false when a passing gate's own earlier
attempt wrote the comment and could not commit it. The file then stays modified
until someone commits it by hand.

The fix carries the plan file whenever it differs from HEAD, whether this call
recorded the comment or found it already recorded. These tests synthesise a
fixture repository and a fixture crew home per case, and assert the real plan
and crew directories are untouched, because an isolated read does not prove an
isolated write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "repromotion-fixture"
PLAN = "repromotion-target"
RUN_IDS = (
    "r-20260917T130000000001-commit-refused-then-retried",
    "r-20260917T130000000002-clean-idempotent-retry",
)
PLAN_RELATIVE = f"docs/plans/{PLAN}.html"


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _write_plan(root: Path, comments: dict[str, list[dict]] | None = None) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Repromotion target",
        "status": "active",
        "version": 0,
        "comments": comments or {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    """No fixture may reach this checkout's own plan or the real crew home."""
    checkout = Path(__file__).resolve().parents[1]
    real_plan = checkout / "docs" / "plans" / f"{PLAN}.html"
    real_state = checkout / "docs" / "state" / PROJECT
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_hook = tmp_path / "config"
    config_hook.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
    root = tmp_path / "repo"
    _write_plan(root)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_hook / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(repository: Path, run_id: str) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-17T11:00:00Z",
            "manifest_path": "/durable/manifest.json",
            "node": {
                "id": "repromotion-target",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": [PLAN_RELATIVE],
            },
        },
    )


def _promote(repository: Path, run_id: str, outcome: str) -> dict[str, Any]:
    return crew.complete(run_id, gate="passed", outcome=outcome, root=repository)


def _dirty(repository: Path, relative: str) -> str:
    return _git(repository, "status", "--porcelain", "--", relative)


def _promotion_commit_files(repository: Path, run_id: str) -> list[str]:
    """The file list of the run's own landing commit, found by its subject."""
    for line in _git(repository, "log", "--format=%H%x09%s").splitlines():
        sha, _, subject = line.partition("\t")
        if subject.startswith(f"promote({run_id})"):
            shown = _git(
                repository, "show", "--no-walk", "--name-only", "--format=", sha
            )
            return [path for path in shown.splitlines() if path]
    raise AssertionError(f"no landing commit for {run_id}")


# ── The named case: the retry commits the comment the first attempt wrote ─────


def test_a_failed_landing_then_a_repromotion_commits_the_recorded_comment(
    repository: Path,
) -> None:
    """A held index.lock fails the first landing; the retry leaves the plan clean.

    The first promotion writes the plan comment, then cannot commit it because
    ``index.lock`` is held. The comment survives on disk. Releasing the lock and
    promoting again finds the comment already recorded, and the landing commit
    must now carry the plan file so ``git status`` reads clean for it.
    """
    run_id = RUN_IDS[0]
    narrative = "the landing the first attempt could not commit"
    _pointer(repository, run_id)

    lock = repository / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    try:
        with pytest.raises(CrewError):
            _promote(repository, run_id, narrative)
    finally:
        lock.unlink()

    # The reproduction: the comment is on disk and the plan file is dirty, an
    # uncommitted landing write the failed rollback could not reach.
    assert _dirty(repository, PLAN_RELATIVE), "the plan file should be dirty"

    result = _promote(repository, run_id, narrative)

    assert result["plan_comment"]["recorded"] is True
    assert result["plan_comment"]["already_recorded"] is True
    # The measure: the plan file is committed, not left as uncommitted state.
    assert _dirty(repository, PLAN_RELATIVE) == ""
    assert PLAN_RELATIVE in _promotion_commit_files(repository, run_id)

    state, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    bodies = [item["body"] for item in state["comments"]["s2"]]
    assert sum(narrative in body for body in bodies) == 1


def test_a_clean_idempotent_repromotion_leaves_the_plan_untouched(
    repository: Path,
) -> None:
    """The boundary: a plan file already committed is not carried again.

    After an ordinary landing the plan file matches HEAD, so a re-promotion of
    the same run finds the comment already recorded and the file unchanged. It
    must not stage or commit it, so HEAD and the plan version both stay put.
    """
    run_id = RUN_IDS[1]
    narrative = "a landing that commits its plan comment in the boundary case"
    _pointer(repository, run_id)
    _promote(repository, run_id, narrative)

    _state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    head_before = _git(repository, "rev-parse", "HEAD")

    # A landing deletes the pointer; restore it to re-promote the landed run.
    _pointer(repository, run_id)
    result = _promote(repository, run_id, narrative)

    assert result["plan_comment"]["already_recorded"] is True
    assert _git(repository, "rev-parse", "HEAD") == head_before
    assert _dirty(repository, PLAN_RELATIVE) == ""
    state_after, version_after = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert version_after == version
    assert len(state_after["comments"]["s2"]) == 1
