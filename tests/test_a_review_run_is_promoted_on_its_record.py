"""A review run's promotion reads its stored record, not a gate command.

A review run's whole deliverable is the record it stores, so a hand promotion
of one has that record as its evidence: its status, its findings and the report
path it names. A review run has no executable check to name — the record is the
check — and its manifest's ``tests`` line is a worker's summary of what it did.
Requiring an executable command of a review run therefore refuses a review
whose evidence is already on disk, which is what the review loop's own
self-promotion path already knows: it promotes on the record alone.

The executable-gate rule stays in force for every other role, because those
runs claim work in a tree and a command is what re-measures it at the
integrated head.

The fixture runs a temporary crew home and a real repository, and asserts
afterwards that the workstation's real crew pointer directory is untouched,
because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-record-fixture"
PLAN = "review-record-target"
REVIEWED_RUN_ID = "r-20261103T000000000001-reviewed-run"
REVIEW_RUN_ID = "r-20261103T010000000000-review-of-reviewed-run"
IMPLEMENT_RUN_ID = "r-20261103T020000000000-build-review-record-target"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"
IMPLEMENT_NODE_ID = f"build-{PLAN}"
REVIEWED_BASE = "b" * 40
REVIEWED_HEAD = "a" * 40

REVIEW_TESTS_PROSE = (
    "snapshot plan.html == worktree ... == HEAD — diff clean; "
    "the review is the run's deliverable"
)

RUN_IDS = [REVIEW_RUN_ID, IMPLEMENT_RUN_ID]


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Review Record Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def run_tree(repository: Path, tmp_path: Path) -> tuple[Path, str]:
    """A real worktree at the repository's HEAD, and the base sha it sits on."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = tmp_path / "worktrees" / "run"
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", "--quiet", str(tree), base)
    return tree, base


def _record_path() -> Path:
    """The head-keyed review-store path the reviewer was dispatched to write."""
    return review_module.review_path(
        PROJECT, REVIEWED_RUN_ID, reviewed_head_sha=REVIEWED_HEAD
    )


def _store_review_record() -> Path:
    """Write a complete parsed review record and return its path."""
    record = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": REVIEWED_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "total": 20 * len(review_module.REVIEW_DIMENSIONS),
        "findings": [{"id": "wiring-1", "type": "wiring"}],
        "timestamp": "2026-11-03T01:10:00Z",
    }
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _write_pointer(
    repository: Path,
    tmp_path: Path,
    tree: Path,
    base: str,
    *,
    run_id: str,
    role: str,
    node_id: str,
    tests_line: str,
    record_path: Path | None,
    commits: str = "",
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    declared_commits = commits or f"none ({role} role; no repository change)"
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        f"commits: {declared_commits}\n"
        f"tests: {tests_line}\n",
        encoding="utf-8",
    )
    node: dict = {
        "id": node_id,
        "plan": PLAN,
        "section": "s4",
        "time_budget": "25m",
        "role": role,
    }
    if record_path is not None:
        node["write_paths"] = [str(record_path)]
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": base,
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-11-03T01:00:00Z",
            "manifest_path": str(manifest),
            "node": node,
        },
    )
    directory = Path(run_dir(run_id))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": run_id, "exit_code": 0}), encoding="utf-8"
    )


def _review_run(
    repository: Path,
    tmp_path: Path,
    tree: Path,
    base: str,
    *,
    tests_line: str = REVIEW_TESTS_PROSE,
    with_record: bool = True,
) -> Path:
    record = _store_review_record() if with_record else _record_path()
    _write_pointer(
        repository,
        tmp_path,
        tree,
        base,
        run_id=REVIEW_RUN_ID,
        role="review",
        node_id=REVIEW_NODE_ID,
        tests_line=tests_line,
        record_path=record,
    )
    return record


def _implement_run(repository: Path, tmp_path: Path, tree: Path, base: str) -> str:
    """A run that really wrote something, so only its gate line is at fault."""
    (tree / "delivered.txt").write_text("work\n", encoding="utf-8")
    _git(tree, "add", "--", "delivered.txt")
    _git(tree, "commit", "-q", "-m", "feature: deliver the target")
    sha = _git(tree, "rev-parse", "HEAD")
    _write_pointer(
        repository,
        tmp_path,
        tree,
        base,
        run_id=IMPLEMENT_RUN_ID,
        role="implement",
        node_id=IMPLEMENT_NODE_ID,
        tests_line=REVIEW_TESTS_PROSE,
        record_path=None,
        commits=sha,
    )
    return sha


def _promote(run_id: str, repository: Path) -> dict:
    return crew.complete(run_id, gate="passed", root=repository)


def _refusal(run_id: str, repository: Path) -> str:
    with pytest.raises(crew.CrewError) as raised:
        _promote(run_id, repository)
    return str(raised.value)


# ── A review run promotes on the record it stored ──────────────────────────


def test_a_review_run_with_a_stored_record_promotes_as_passed(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """The record is the evidence, so a prose tests line does not refuse it."""
    tree, base = run_tree
    _review_run(repository, tmp_path, tree, base)

    promoted = _promote(REVIEW_RUN_ID, repository)

    assert promoted["record"]["gate"] == "passed", promoted["record"]
    assert promoted["record"]["commits"] == []
    assert not pointer_path(REVIEW_RUN_ID).exists()


def test_a_review_run_with_no_stored_record_is_refused(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """With no record there is no evidence, so the run is refused naming it."""
    tree, base = run_tree
    _review_run(repository, tmp_path, tree, base, with_record=False)

    refusal = _refusal(REVIEW_RUN_ID, repository)

    assert "no stored review record was delivered" in refusal, refusal
    assert pointer_path(REVIEW_RUN_ID).exists()


def test_an_implement_run_with_a_prose_tests_line_is_still_refused(
    repository: Path, tmp_path: Path, run_tree: tuple[Path, str]
) -> None:
    """The executable-gate rule stays in force for every other role."""
    tree, base = run_tree
    _implement_run(repository, tmp_path, tree, base)

    refusal = _refusal(IMPLEMENT_RUN_ID, repository)

    assert "describes the check rather than running it" in refusal, refusal
    assert pointer_path(IMPLEMENT_RUN_ID).exists()
