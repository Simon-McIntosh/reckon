"""Repository identity and claim liveness for live write claims.

Every fixture here synthesises its own repository and points ``RECKON_HOME`` at
a temporary directory, and each test asserts the real live-pointer directory is
untouched afterwards: an isolated read does not prove an isolated write, and a
test that writes a live pointer where the fleet keeps its own can corrupt a
running session's view of what is in flight.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module


def _directory_snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    live_directory = crew.live_dir()
    before = _directory_snapshot(live_directory)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    assert _directory_snapshot(live_directory) == before


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(root: Path, projects: tuple[str, ...]) -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    for project in projects:
        (root / "docs" / "plans" / f"{project}-plan.html").write_text(
            f'<meta name="docs-project" content="{project}">'
            '<meta name="reckon-type" content="plan">'
            f'<meta name="plan-slug" content="{project}-plan">'
            '<h2 id="dispatch">Dispatch</h2>',
            encoding="utf-8",
        )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "package"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        _git(root, *arguments)
    return root


def _worktree(repo: Path, path: Path) -> Path:
    _git(repo, "worktree", "add", "--detach", "-q", str(path), "HEAD")
    return path


def _reaped_pid() -> int:
    """Return a process id that has certainly exited and been collected."""
    worker = subprocess.Popen(["true"])
    worker.wait()
    return worker.pid


def _pointer(
    *,
    repo: Path,
    worktree: Path | None,
    project: str,
    paths: tuple[str, ...],
    pid: int | None,
    run_id: str = "r-owner",
    base_sha: str = "",
) -> dict:
    record: dict = {
        "run_id": run_id,
        "project": project,
        "repo": str(repo.resolve()),
        "phase": "working",
        "pid": pid,
        "node": {
            "id": "owner-node",
            "plan": f"{project}-plan",
            "write_paths": list(paths),
        },
    }
    if worktree is not None:
        record["worktree"] = str(worktree)
    if base_sha:
        record["base_sha"] = base_sha
    return record


# ── Repository identity ─────────────────────────────────────────────────────


def test_a_worktree_and_its_checkout_are_one_repository(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    linked = _worktree(repo, tmp_path / "linked")

    assert node_module.repository_identity(linked) == repo.resolve()
    assert node_module.repository_identity(repo) == repo.resolve()


def test_two_spellings_of_one_checkout_resolve_alike(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    indirect = tmp_path / "shared" / "docs" / ".."

    assert node_module.repository_identity(indirect) == repo.resolve()


def test_claim_repository_prefers_the_worktree_over_the_project_mount(
    tmp_path: Path,
) -> None:
    """The two fields disagree, and only the worktree contains the paths."""
    work = _repository(tmp_path / "work", ("project-b",))
    elsewhere = _repository(tmp_path / "elsewhere", ("project-a",))
    linked = _worktree(work, tmp_path / "linked")
    pointer = _pointer(
        repo=elsewhere,
        worktree=linked,
        project="project-a",
        paths=("package/target.py",),
        pid=os.getpid(),
    )

    assert node_module.claim_repository(pointer) == work.resolve()


def test_claim_repository_falls_back_to_the_recorded_repository(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    pointer = _pointer(
        repo=repo,
        worktree=None,
        project="project-a",
        paths=("package/target.py",),
        pid=os.getpid(),
    )

    assert node_module.claim_repository(pointer) == repo.resolve()


# ── Claim liveness ──────────────────────────────────────────────────────────


def test_a_running_run_still_binds_its_paths(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    linked = _worktree(repo, tmp_path / "linked")
    pointer = _pointer(
        repo=repo,
        worktree=linked,
        project="project-a",
        paths=("package/target.py",),
        pid=os.getpid(),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )

    disposition = node_module.claim_disposition(pointer)

    assert disposition.binding is True
    assert "still running" in disposition.reason
    assert "r-owner" in disposition.reason


def test_a_stopped_run_holding_a_commit_still_binds_and_names_itself(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    linked = _worktree(repo, tmp_path / "linked")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (linked / "package" / "target.py").write_text("value = 2\n", encoding="utf-8")
    _git(linked, "add", "package/target.py")
    _git(
        linked,
        "-c",
        "user.email=w@example.invalid",
        "-c",
        "user.name=W",
        "commit",
        "-q",
        "-m",
        "fix(package): raise the value",
    )
    pointer = _pointer(
        repo=repo,
        worktree=linked,
        project="project-a",
        paths=("package/target.py",),
        pid=_reaped_pid(),
        base_sha=base_sha,
    )

    disposition = node_module.claim_disposition(pointer)

    assert disposition.binding is True
    assert "r-owner" in disposition.reason
    assert any("commit(s) beyond its base" in item for item in disposition.unintegrated)


def test_a_stopped_run_holding_a_dirty_path_still_binds_and_names_itself(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    linked = _worktree(repo, tmp_path / "linked")
    (linked / "package" / "target.py").write_text("value = 3\n", encoding="utf-8")
    pointer = _pointer(
        repo=repo,
        worktree=linked,
        project="project-a",
        paths=("package/target.py",),
        pid=_reaped_pid(),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )

    disposition = node_module.claim_disposition(pointer)

    assert disposition.binding is True
    assert "r-owner" in disposition.reason
    assert any("uncommitted change" in item for item in disposition.unintegrated)


def test_a_stopped_clean_integrated_run_is_disregarded(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    linked = _worktree(repo, tmp_path / "linked")
    pointer = _pointer(
        repo=repo,
        worktree=linked,
        project="project-a",
        paths=("package/target.py",),
        pid=_reaped_pid(),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )

    disposition = node_module.claim_disposition(pointer)

    assert disposition.binding is False
    assert disposition.unintegrated == ()
    assert "r-owner" in disposition.reason
    assert "disregarded" in disposition.reason


def test_a_commit_the_integration_branch_reaches_is_not_unintegrated(
    tmp_path: Path,
) -> None:
    """A promoted run's commit is on main, so its claim no longer holds."""
    repo = _repository(tmp_path / "shared", ("project-a",))
    linked = _worktree(repo, tmp_path / "linked")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (linked / "package" / "target.py").write_text("value = 4\n", encoding="utf-8")
    _git(linked, "add", "package/target.py")
    _git(
        linked,
        "-c",
        "user.email=w@example.invalid",
        "-c",
        "user.name=W",
        "commit",
        "-q",
        "-m",
        "fix(package): raise the value",
    )
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge worker",
        _git(linked, "rev-parse", "HEAD"),
    )
    pointer = _pointer(
        repo=repo,
        worktree=linked,
        project="project-a",
        paths=("package/target.py",),
        pid=_reaped_pid(),
        base_sha=base_sha,
    )

    assert node_module.claim_disposition(pointer).binding is False


def test_a_claim_with_no_worktree_reports_no_unintegrated_work(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "shared", ("project-a",))
    pointer = _pointer(
        repo=repo,
        worktree=None,
        project="project-a",
        paths=("package/target.py",),
        pid=_reaped_pid(),
    )

    assert node_module.unintegrated_claim_work(pointer) == []


# ── The registry the dispatch-facing check reads ────────────────────────────


def test_two_projects_one_repository_share_a_claim_repository(
    home: Path, tmp_path: Path
) -> None:
    """The measured admission: two projects, two ``repo`` fields, one resource.

    A run dispatched with one project's plan into another project's checkout
    records that project's mount as ``repo``. Reading the repository from the
    worktree instead puts both claims in the same repository, which is what the
    exclusivity comparison needs before it can see the collision at all.
    """
    work = _repository(tmp_path / "work", ("project-b",))
    elsewhere = _repository(tmp_path / "elsewhere", ("project-a",))
    (home / "mounts.json").write_text(
        json.dumps(
            {
                "project-a": str(elsewhere / "docs"),
                "project-b": str(work / "docs"),
            }
        ),
        encoding="utf-8",
    )
    first = _pointer(
        repo=elsewhere,
        worktree=_worktree(work, tmp_path / "first"),
        project="project-a",
        paths=("package/target.py",),
        pid=os.getpid(),
        run_id="r-first",
    )
    second = _pointer(
        repo=work,
        worktree=_worktree(work, tmp_path / "second"),
        project="project-b",
        paths=("package/target.py",),
        pid=os.getpid(),
        run_id="r-second",
    )
    for record in (first, second):
        crew._write_json(crew.pointer_path(record["run_id"]), record)

    repositories = {
        record["run_id"]: node_module.claim_repository(record)
        for record in (first, second)
    }

    assert repositories == {"r-first": work.resolve(), "r-second": work.resolve()}
