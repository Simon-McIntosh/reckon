"""One refused promotion reports every failing independent precondition.

A record can fall short in several independent ways at once, and an operator
who learns them one per call pays a round trip for each. The preconditions are
judged together and reported in one refusal, in the order the promotion checks
them, with the leading refusal's wording unchanged for callers matching on it.
Each test makes the guarded thing happen and reads the refusal rather than
resting on the suite being green.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "proj"
PLAN = "plan-a"
FILE = "reckon/region.py"
# A stored record's carried revisions are resolved against the reviewed run's
# own repository, so the store below passes the revision the run was
# dispatched from rather than a stand-in.


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
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
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


def _set_plan(repository: Path, **fields) -> None:
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state.update(fields)
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")


def _manifest(tmp_path: Path, run_id: str, *, commits: Sequence[str] = ()) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    listed = ", ".join(commits)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: [{listed}]\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path,
    run_id: str,
    *,
    manifest: Path,
    worktree: Path | None = None,
    plan_impl_at_dispatch: float | None = None,
    base_sha: str = "",
) -> None:
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(worktree or repository),
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
            "write_paths": [],
        },
    }
    if base_sha:
        record["base_sha"] = base_sha
    if plan_impl_at_dispatch is not None:
        record["plan_impl_at_dispatch"] = plan_impl_at_dispatch
    _write_json(pointer_path(run_id), record)


def _store_complete_review(run_id: str, *, base: str, head: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(record)


GATE_CHECK_WITHOUT_A_LOG = {
    "command": EXECUTABLE_GATE_COMMAND,
    "exit_status": 0,
}


def test_two_independent_preconditions_are_reported_by_one_refusal(
    repository: Path, tmp_path: Path
) -> None:
    """An unmoved impl and a passing gate with no log, in one call."""
    _set_plan(repository, impl=0.5)
    run_id = "r-20260918T090000000000-node-a"
    _pointer(
        repository,
        run_id,
        manifest=_manifest(tmp_path, run_id),
        plan_impl_at_dispatch=0.5,
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            root=repository,
            gate="passed",
            outcome="the guard landed",
            gate_check=GATE_CHECK_WITHOUT_A_LOG,
            require_gate_check=True,
        )

    message = str(refusal.value)
    assert "impl did not move" in message
    assert "requires the check that produced it" in message
    # The order is the order the promotion checks them, so the refusal that
    # would have been raised first still leads.
    assert message.index("impl did not move") < message.index(
        "requires the check that produced it"
    )
    assert "--no-impl-change" in message
    # A refusal consumes nothing: the pointer survives for the retry.
    assert pointer_path(run_id).exists()


def test_a_single_failing_precondition_keeps_its_wording(
    repository: Path, tmp_path: Path
) -> None:
    _set_plan(repository, impl=0.5)
    run_id = "r-20260918T090100000000-node-a"
    _pointer(
        repository,
        run_id,
        manifest=_manifest(tmp_path, run_id),
        plan_impl_at_dispatch=0.5,
    )

    with pytest.raises(crew.CrewError) as refusal:
        # The review gate passes on its waiver, so the impl is the one
        # precondition this record fails.
        crew.complete(
            run_id,
            root=repository,
            gate="passed",
            outcome="landed",
            review_waiver="the plan's impl is moved by a sibling node",
        )

    message = str(refusal.value)
    assert message.startswith(
        f"run {run_id!r} promotes a passing implement run, but plan {PLAN!r} "
        "impl did not move: 0.5 at dispatch and 0.5 at completion."
    )
    assert "This promotion also fails" not in message


def test_the_reviewed_head_is_named_as_the_commit_to_cite(
    repository: Path, tmp_path: Path
) -> None:
    (repository / FILE).parent.mkdir(parents=True, exist_ok=True)
    (repository / FILE).write_text("seed\n", encoding="utf-8")
    _git(repository, "add", FILE)
    _git(repository, "commit", "-q", "-m", "chore: seed runtime source")
    base = _git(repository, "rev-parse", "HEAD")

    run_tree = tmp_path / "run-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    target = run_tree / FILE
    target.write_text("repair\n", encoding="utf-8")
    _git(run_tree, "add", FILE)
    _git(run_tree, "commit", "-q", "-m", "test: repair")
    cited = _git(run_tree, "rev-parse", "HEAD")
    # The amendment the review actually read: the manifest's commit list still
    # names the pre-amend revision.
    _git(run_tree, "commit", "--amend", "-q", "-m", "test: repair, amended")
    reviewed_head = _git(run_tree, "rev-parse", "HEAD")
    assert reviewed_head != cited

    run_id = "r-20260918T090200000000-node-a"
    manifest = _manifest(tmp_path, run_id, commits=(cited,))
    _pointer(
        repository,
        run_id,
        manifest=manifest,
        worktree=run_tree,
        base_sha=base,
    )
    _store_complete_review(run_id, base=base, head=reviewed_head)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, root=repository, gate="passed", commits=[cited])

    message = str(refusal.value)
    assert cited[:12] in message and reviewed_head[:12] in message
    assert f"--commit {reviewed_head}" in message
