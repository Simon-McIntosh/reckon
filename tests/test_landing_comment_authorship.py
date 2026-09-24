"""Landing comments distinguish their writer and reject stale narratives."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "landing-comment-fixture"
PLAN = "landing-target"
RUN_IDS = (
    "r-20260917T100000000001-coordinator-writes",
    "r-20260917T100000000002-worker-writes",
    "r-20260917T100000000003-narrative-conflict",
    "r-20260917T100000000004-idempotent",
    "r-20260917T100000000005-retry",
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
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
        "title": "Landing target",
        "status": "active",
        "version": 0,
        "comments": comments or {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    repository = Path(__file__).resolve().parents[1]
    real_plan = repository / "docs" / "plans" / f"{PLAN}.html"
    real_state = repository / "docs" / "state" / PROJECT
    real_home = Path.home() / ".config" / "reckon" / "crew"
    real_run_paths = [
        path
        for run_id in RUN_IDS
        for path in (real_home / "live" / f"{run_id}.json", real_home / "runs" / run_id)
    ]
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_run_paths)
    yield
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_run_paths)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _record(
    repository: Path,
    run_id: str,
    narrative: str,
    *,
    worker_tree: Path | None = None,
    worker_commits: tuple[str, ...] = (),
) -> dict:
    return promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="§2",
        run_id=run_id,
        narrative=narrative,
        author="reckon-build",
        when="2026-09-17T10:00:00Z",
        root=repository,
        worker_tree=worker_tree,
        worker_commits=worker_commits,
    )


def test_coordinator_written_comment_names_the_coordinator(
    repository: Path,
) -> None:
    run_id = RUN_IDS[0]
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
            "created_at": "2026-09-17T09:59:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "coordinator-writes",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )

    crew.complete(
        run_id,
        gate="passed",
        outcome="the coordinator records the landing",
        root=repository,
    )

    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    comment = plan["comments"]["s2"][0]
    assert comment["who"] == "reckon-build"
    assert comment["who"] != "worker-a"


def test_worker_authored_comment_keeps_the_worker_attribution(
    repository: Path, tmp_path: Path
) -> None:
    run_id = RUN_IDS[1]
    comment_id = f"c-run-{run_id}"
    worker = tmp_path / "worker"
    worker_comment = {
        "id": comment_id,
        "who": "worker-a",
        "when": "2026-09-17T10:00:00Z",
        "body": "<p>the worker records its own landing</p>",
    }
    _write_plan(worker, {"s2": [worker_comment]})
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "docs: record worker landing"),
    ):
        _git(worker, *arguments)
    worker_commit = _git(worker, "rev-parse", "HEAD")

    result = _record(
        repository,
        run_id,
        "the coordinator would otherwise write this narrative",
        worker_tree=worker,
        worker_commits=(worker_commit,),
    )

    assert result["reason"] == "worker_authored_landing_record"
    worker_plan, _version = _store.read_plan(
        PROJECT, PLAN, worker, artifact_type="plan"
    )
    assert worker_plan["comments"]["s2"][0]["who"] == "worker-a"
    coordinator_plan, _version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert coordinator_plan["comments"].get("s2", []) == []


def test_changed_narrative_refuses_to_preserve_stale_text(repository: Path) -> None:
    run_id = RUN_IDS[2]
    _record(repository, run_id, "original narrative")
    plan_file = repository / "docs" / "plans" / f"{PLAN}.html"
    original = plan_file.read_bytes()

    with pytest.raises(promotion.CrewError, match="different narrative"):
        _record(repository, run_id, "corrected narrative")

    assert plan_file.read_bytes() == original


def test_unchanged_narrative_is_idempotent_and_writes_nothing(
    repository: Path,
) -> None:
    run_id = RUN_IDS[3]
    _record(repository, run_id, "stable narrative")
    plan_file = repository / "docs" / "plans" / f"{PLAN}.html"
    original = plan_file.read_bytes()

    result = _record(repository, run_id, "stable narrative")

    assert result["recorded"] is True
    assert result["already_recorded"] is True
    assert plan_file.read_bytes() == original


def test_version_conflicts_retry_four_times(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    write_plan = promotion._store.write_plan

    def conflicting_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 4:
            raise _store.VersionConflict(0, calls, {})
        return write_plan(*args, **kwargs)

    monkeypatch.setattr(promotion._store, "write_plan", conflicting_write)

    result = _record(repository, RUN_IDS[4], "the fourth write succeeds")

    assert calls == 4
    assert result["recorded"] is True
    assert result["already_recorded"] is False
