"""Hermetic coverage for repository-scoped live write claims."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon import crew, ledger


CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


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


def _repository(root: Path, projects: tuple[str, ...]) -> Path:
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for project in projects:
        (root / "docs" / "plans" / f"{project}-plan.html").write_text(
            f'<meta name="docs-project" content="{project}">'
            '<meta name="reckon-type" content="plan">'
            f'<meta name="plan-slug" content="{project}-plan">'
            '<h2 id="dispatch">Dispatch</h2>',
            encoding="utf-8",
        )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    (root / "package" / "schema.yaml").write_text("value: 1\n", encoding="utf-8")
    (root / "package" / "generated.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "package", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def _mounts(home: Path, projects: dict[str, Path]) -> None:
    (home / "mounts.json").write_text(
        json.dumps({project: str(repo / "docs") for project, repo in projects.items()}),
        encoding="utf-8",
    )


def _node(project: str, path: str, *, node_id: str = "candidate") -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="record one repository claim result",
        plan=f"{project}-plan",
        section="dispatch",
        done_when="pytest reports all repository claim checks passing",
        write_paths=[path],
        time_budget="20m",
    )


def _claim(
    repo: Path,
    *,
    project: str,
    path: str,
    run_id: str = "r-owner",
    member: str = "",
    authority_repositories: tuple[Path, ...] = (),
) -> dict:
    record = {
        "run_id": run_id,
        "project": project,
        "repo": str(repo.resolve()),
        "phase": "working",
        "member": member,
        "node": {
            "id": "owner-node",
            "plan": f"{project}-plan",
            "write_paths": [path],
        },
    }
    if authority_repositories:
        record["authority"] = {
            "repositories": [str(root.resolve()) for root in authority_repositories],
            "write": {"projects": [project]},
        }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


@pytest.mark.parametrize(
    ("claimed", "candidate"),
    [("package", "package/target.py"), ("package/target.py", "package")],
)
def test_cross_project_containment_names_the_owning_project(
    home: Path, tmp_path: Path, claimed: str, candidate: str
) -> None:
    repo = _repository(tmp_path / "shared", ("project-a", "project-b"))
    _mounts(home, {"project-a": repo, "project-b": repo})
    owner = _claim(repo, project="project-a", path=claimed)
    worktrees_before = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    with pytest.raises(crew.ScopeConflict) as excinfo:
        crew.dispatch(
            node=_node("project-b", candidate),
            project="project-b",
            repo=repo,
            config=CONFIG,
            session="cross-project-conflict",
            check_budget=False,
            launcher=lambda *args, **kwargs: pytest.fail("conflict must not launch"),
        )

    refusal = excinfo.value
    assert refusal.run_id == owner["run_id"]
    assert refusal.node_id == "owner-node"
    assert refusal.project == "project-a"
    assert owner["run_id"] in str(refusal)
    assert "project-a" in str(refusal)
    assert (
        subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == worktrees_before
    )


def test_claim_in_a_different_repository_does_not_block_dispatch(
    home: Path, tmp_path: Path
) -> None:
    first = _repository(tmp_path / "first", ("project-a",))
    second = _repository(tmp_path / "second", ("project-b",))
    _mounts(home, {"project-a": first, "project-b": second})
    owner = _claim(first, project="project-a", path="package")

    record = crew.dispatch(
        node=_node("project-b", "package/target.py"),
        project="project-b",
        repo=second,
        config=CONFIG,
        session="separate-repositories",
        check_budget=False,
        launcher=lambda *args, **kwargs: 0,
    )

    assert record["project"] == "project-b"
    assert crew.list_live(project="project-a") == [owner]
    assert crew.pointer_path(record["run_id"]).is_file()


def test_cross_repository_mounted_claim_expands_its_own_derivation(
    home: Path, tmp_path: Path
) -> None:
    dispatch_repo = _repository(tmp_path / "dispatch", ("project-a",))
    delivery_repo = _repository(tmp_path / "delivery", ("project-b",))
    _mounts(home, {"project-a": dispatch_repo, "project-b": delivery_repo})
    state = delivery_repo / "docs" / "state" / "project-b"
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps(
            {
                "project": "project-b",
                "doc": "index",
                "data": {
                    "_version": 0,
                    "projects": [
                        {
                            "name": "project-b",
                            "derivations": {
                                "package/schema.yaml": ["package/generated.py"]
                            },
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    owner = _claim(
        dispatch_repo,
        project="project-a",
        path=str(delivery_repo / "package" / "schema.yaml"),
        authority_repositories=(dispatch_repo, delivery_repo),
    )

    with pytest.raises(crew.ScopeConflict) as excinfo:
        crew.dispatch(
            node=_node("project-b", "package/generated.py"),
            project="project-b",
            repo=delivery_repo,
            config=CONFIG,
            session="derived-cross-repository-claim",
            check_budget=False,
            launcher=lambda *args, **kwargs: pytest.fail("conflict must not launch"),
        )

    assert excinfo.value.run_id == owner["run_id"]
    assert excinfo.value.claimed_path == "package/generated.py"
    assert excinfo.value.project == "project-a"


def test_member_guard_remains_project_scoped(home: Path, tmp_path: Path) -> None:
    repo = _repository(tmp_path / "shared", ("project-a", "project-b"))
    _mounts(home, {"project-a": repo, "project-b": repo})
    owner = _claim(
        repo,
        project="project-a",
        path="package/schema.yaml",
        member="shared-member",
    )
    ledger.register_member("project-b", "shared-member", harness="worker", root=repo)

    record = crew.dispatch(
        node=_node("project-b", "package/target.py", node_id="member-candidate"),
        project="project-b",
        repo=repo,
        config=CONFIG,
        session="project-member-scope",
        member="shared-member",
        check_budget=False,
        launcher=lambda *args, **kwargs: 0,
    )

    assert record["member"] == "shared-member"
    assert crew.list_live(project="project-a") == [owner]
    assert crew.pointer_path(record["run_id"]).is_file()
