"""A review of a run that merged main reads only the run's own commits.

A run's changes are the commits it authored, not everything reachable from its
head that its base or the integration branch lacks. A run that merges main
mid-flight brings the integration branch's whole advance into a two-dot range
taken off the reviewed head, and the review composed from it is sized on work
the run never touched. The range is resolved through one helper,
:func:`reckon.clones.own_changed_paths`, against the remote-tracking ref of the
integration branch rather than a local branch that may lag it.

Every case is driven against a real git history: the run's commits, the
integration branch's advance, the merge that consumes it and, where the merge
conflicts, the resolution the run wrote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import clones
from reckon.crew import recovery_review_dispatch as recovery
from reckon.crew import recovery_review_subject as review_subject
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "delta-fixture"
RUN_ID = "r-own-delta"
EVIDENCE_PATH = "docs/evidence/fragments/delta-fixture/own.html"
TEST_PATH = "tests/test_feature.py"


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    _git(repository, "add", *paths)
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _seed_repository(tmp_path: Path) -> dict:
    """A checkout, its bare origin, and the dispatch base both start from."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    base = _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    # The run works on a detached head, so the local ``main`` stays at the
    # dispatch base while the remote-tracking ref advances — the state the
    # range must be taken against.
    _git(repo, "checkout", "-q", "--detach")
    return {"origin": origin, "repo": repo, "base": base}


def _integration_clone(tmp_path: Path, origin: Path) -> Path:
    clone = tmp_path / "integration"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "maintainer@example.invalid")
    _git(clone, "config", "user.name", "Maintainer")
    return clone


def _advance_integration(tmp_path: Path, origin: Path, files: dict[str, str]) -> None:
    clone = _integration_clone(tmp_path, origin)
    _commit(clone, files, "merge: advance main")
    _git(clone, "push", "-q", "origin", "main")


def _write_run(home: Path, repo: Path, *, base: str, head: str) -> dict:
    manifest = home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {RUN_ID}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": "coordinator-fixture",
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {"id": RUN_ID, "plan": "fixture-plan", "section": "s3"},
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    return runs.read_pointer(RUN_ID)


def _store_review(*, base: str, head: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    stored = review_module.parse_review(emitted)
    stored.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "review_run_id": f"review-of-{RUN_ID}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(stored)


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: home / "reviews",
    )
    seeded = _seed_repository(tmp_path)
    seeded["tmp_path"] = tmp_path
    seeded["home"] = home
    return seeded


def test_a_run_that_merged_main_reads_only_its_own_paths(world):
    repo = world["repo"]
    base = world["base"]
    reviewed = _commit(
        repo, {"pkg/feature.py": "VALUE = 1\n"}, "feat: the reviewed change"
    )
    _store_review(base=base, head=reviewed)
    _advance_integration(
        world["tmp_path"],
        world["origin"],
        {
            "pkg/integration_runtime.py": "INTEGRATED = 1\n",
            "pkg/integration_helper.py": "HELPER = 1\n",
            "docs/guides/integration.html": "<p>integration</p>\n",
        },
    )
    _git(repo, "fetch", "-q", "origin")
    _commit(
        repo,
        {
            TEST_PATH: "def test_feature():\n    assert True\n",
            EVIDENCE_PATH: "<p>the run's own evidence</p>\n",
        },
        "test: cover the change and record its evidence",
    )
    _git(repo, "merge", "-q", "--no-ff", "origin/main", "-m", "merge: incorporate main")
    head = _git(repo, "rev-parse", "HEAD")
    record = _write_run(world["home"], repo, base=base, head=head)

    move = recovery.review_head_move(record)

    # The path set and the classification are read together: the integration
    # branch's runtime path entering the delta is what flips the run to a
    # runtime-source move, so one assertion reports whichever of the two a
    # regression breaks first.
    assert (set(move["paths"]), move["changes_runtime_source"]) == (
        {TEST_PATH, EVIDENCE_PATH},
        False,
    )
    assert move["deliverable_paths"] == []
    fields = review_subject._review_dispatch_fields(record, delta=move, write=False)
    assert TEST_PATH in fields["goal"]
    assert EVIDENCE_PATH in fields["goal"]
    assert "integration" not in fields["goal"]

    # The lagging local ``main`` is the selector this range must not use: it
    # never carried the integration branch's commits, so the range taken
    # against it readmits them.
    lagging = clones.own_changed_paths(repo, reviewed, head, integration_ref="main")
    assert lagging is not None
    assert "pkg/integration_runtime.py" in lagging


def test_a_run_owned_merge_keeps_the_conflict_resolution_it_wrote(world):
    repo = world["repo"]
    base = world["base"]
    reviewed = _commit(repo, {"pkg/shared.py": "RUN = 1\n"}, "feat: the run's own side")
    _store_review(base=base, head=reviewed)
    _advance_integration(
        world["tmp_path"],
        world["origin"],
        {
            "pkg/shared.py": "INTEGRATION = 1\n",
            "pkg/unrelated.py": "UNRELATED = 1\n",
        },
    )
    _git(repo, "fetch", "-q", "origin")
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "origin/main", "-m", "merge: incorporate main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge.returncode != 0
    (repo / "pkg/shared.py").write_text("RUN = 1\nINTEGRATION = 1\n", encoding="utf-8")
    _git(repo, "add", "pkg/shared.py")
    _git(repo, "commit", "-q", "--no-edit")
    head = _git(repo, "rev-parse", "HEAD")
    record = _write_run(world["home"], repo, base=base, head=head)

    move = recovery.review_head_move(record)

    assert move["paths"] == ["pkg/shared.py"]
    assert move["changes_runtime_source"] is True

    # The merge pass is what keeps the resolution: ``--no-merges`` alone walks
    # no commit that carries it.
    first_pass = _git(
        repo, "rev-list", "--no-merges", head, "^origin/main", f"^{reviewed}"
    )
    assert first_pass == ""


def test_a_repository_without_a_remote_reads_the_move_as_uncomparable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(
        review_module, "review_store_root", lambda base_dir=None: home / "reviews"
    )
    repo = tmp_path / "local-only"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    base = _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    reviewed = _commit(repo, {"pkg/feature.py": "VALUE = 1\n"}, "feat: reviewed")
    _store_review(base=base, head=reviewed)
    head = _commit(repo, {TEST_PATH: "def test():\n    pass\n"}, "test: later work")
    record = _write_run(home, repo, base=base, head=head)

    assert recovery._integration_ref(repo) == ""
    assert clones.own_changed_paths(repo, reviewed, head, integration_ref="") is None
    assert recovery.review_head_move(record) == {}
