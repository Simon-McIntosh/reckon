"""Promotion sizes each run's review from the tier the run changed its way into.

A run that changes no runtime source — tests, plans, evidence, research data,
figures — promotes with the review gate standing down and its tier recorded on
the ledger row as the reason no review exists. A run that changes runtime source
owes a review unless one is already stored at the promoted head, and the tier it
resolves to (light for a small diff at a fixing spec level, full otherwise or
under a declared elevated risk) is what the row records once it lands.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

_SMALL_SOURCE = "def answer():\n    return 42\n"
_LARGE_SOURCE = "".join(f"VALUE_{index} = {index}\n" for index in range(80))


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_plan(root: Path, *, risk: str | None = None) -> None:
    plan = root / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state: dict = {
        "type": "plan",
        "slug": PLAN,
        "title": "Plan A",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    if risk is not None:
        state["capability"] = {
            "version": "1.0",
            "class": "orchestrator",
            "requirements": {"risk": risk},
        }
    plan.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _commit_change(repository: Path, relative_path: str, content: str) -> str:
    path = repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repository, "add", relative_path)
    _git(repository, "commit", "-q", "-m", "feature: change a path")
    return _git(repository, "rev-parse", "HEAD")


def _write_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    commit: str,
    changed_paths: tuple[str, ...],
    role: str = "implement",
    spec_level: str = "exact",
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {commit}\n"
        f"changed_paths: {', '.join(changed_paths)}\n"
        "tests: focused check passed\n",
        encoding="utf-8",
    )
    node: dict = {
        "id": "node-a",
        "plan": PLAN,
        "section": "s2",
        "time_budget": "25m",
        "write_paths": list(changed_paths),
    }
    if spec_level:
        node["spec_level"] = spec_level
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-21T06:00:00Z",
            "manifest_path": str(manifest),
            "node": node,
        },
    )


def _store_review(run_id: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    review = review_module.parse_review(emitted)
    review.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(review)


def _row(repository: Path, run_id: str) -> dict:
    return next(
        row
        for row in ledger.load(PROJECT, repository)[0]["runs"]
        if row["run_id"] == run_id
    )


def test_tests_only_run_promotes_unreviewed_with_tier_none(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140000000000-tests-only"
    commit = _commit_change(
        repository, "tests/test_thing.py", "def test_it():\n    pass\n"
    )
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("tests/test_thing.py",),
        role="implement",
    )

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    row = _row(repository, run_id)
    assert row["review_tier"] == "none"
    assert not row.get("review")


def test_small_runtime_source_run_is_light_and_refused_without_review(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140100000000-light"
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "light"


def test_large_runtime_source_run_is_full_and_refused_without_review(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140200000000-full"
    commit = _commit_change(repository, "reckon/big.py", _LARGE_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/big.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "full"


def test_elevated_plan_risk_makes_a_small_source_run_full(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140300000000-elevated"
    _write_plan(repository, risk="elevated")
    _git(repository, "add", f"docs/plans/{PLAN}.html")
    _git(repository, "commit", "-q", "-m", "docs: declare elevated risk")
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "full"


def test_open_spec_level_forces_a_full_review_on_a_small_diff(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140400000000-open"
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="open",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "full"


def test_a_run_declaring_no_scope_is_refused_rather_than_read_as_none(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140500000000-silent"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: []\nchanged_paths: []\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-09-21T06:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
            },
        },
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", root=repository)
