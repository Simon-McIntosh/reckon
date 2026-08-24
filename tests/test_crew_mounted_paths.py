"""Hermetic coverage for dispatch writes spanning registered repositories."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew


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


def _commit_repository(root: Path, *paths: str) -> None:
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", *paths],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repositories(tmp_path: Path, home: Path) -> tuple[Path, Path]:
    work_repo = tmp_path / "work-repository"
    (work_repo / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (work_repo / "docs").mkdir()
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (work_repo / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (work_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _commit_repository(work_repo, "seed.txt", "skills")

    authority_repo = tmp_path / "authority-repository"
    (authority_repo / "docs" / "plans").mkdir(parents=True)
    (authority_repo / "delivery").mkdir()
    (authority_repo / "docs" / "plans" / "remote-plan.html").write_text(
        '<meta name="docs-project" content="authority-project">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="remote-plan">'
        '<h2 id="dispatch">Dispatch</h2>',
        encoding="utf-8",
    )
    (authority_repo / "delivery" / "result.txt").write_text(
        "pending\n", encoding="utf-8"
    )
    _commit_repository(authority_repo, "docs", "delivery")

    (home / "mounts.json").write_text(
        json.dumps(
            {
                "work-project": str(work_repo / "docs"),
                "authority-project": str(authority_repo / "docs"),
            }
        ),
        encoding="utf-8",
    )
    return work_repo, authority_repo


def _node(path: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="mounted-delivery",
        goal="record one delivery in the repository that owns it",
        plan="remote-plan",
        section="dispatch",
        done_when="pytest reports all mounted delivery checks passing",
        write_paths=[str(path)],
        time_budget="20m",
    )


def test_dispatch_accepts_a_write_in_the_mounted_plan_repository_and_records_authority(
    home: Path, repositories: tuple[Path, Path]
) -> None:
    work_repo, authority_repo = repositories
    delivery = authority_repo / "delivery" / "result.txt"

    record = crew.dispatch(
        node=_node(delivery),
        project="authority-project",
        repo=work_repo,
        config=CONFIG,
        session="mounted-delivery-session",
        check_budget=False,
        launcher=lambda *args, **kwargs: 0,
    )

    plan_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=authority_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert record["authority"] == {
        "plan": {
            "project": "authority-project",
            "docs": str((authority_repo / "docs").resolve()),
            "repository": str(authority_repo.resolve()),
            "source": "mount",
            "base_sha": plan_sha,
        },
        "write": {
            "projects": ["work-project"],
            "repository": str(work_repo.resolve()),
            "source": "mount",
        },
        "repositories": sorted(
            {str(work_repo.resolve()), str(authority_repo.resolve())}
        ),
    }
    assert record["node"]["write_paths"] == [str(delivery)]
    assert crew.pointer_path(record["run_id"]).is_file()
    assert crew.runs_dir().is_relative_to(home)


def test_write_in_an_unmounted_repository_names_the_missing_mount(
    home: Path, repositories: tuple[Path, Path]
) -> None:
    work_repo, authority_repo = repositories
    (home / "mounts.json").write_text(
        json.dumps({"work-project": str(work_repo / "docs")}), encoding="utf-8"
    )
    delivery = authority_repo / "delivery" / "result.txt"

    with pytest.raises(crew.CrewError) as excinfo:
        crew.plan_dispatch(
            node=_node(delivery),
            project="authority-project",
            repo=work_repo,
            config=CONFIG,
        )

    detail = str(excinfo.value)
    assert str(delivery) in detail
    assert "missing from mounts.json" in detail
    authority_list = detail.split("dispatch authority (")[1].split(")")[0]
    assert str(authority_repo) not in authority_list


def test_write_outside_all_authority_and_delivery_roots_is_refused(
    home: Path, repositories: tuple[Path, Path], tmp_path: Path
) -> None:
    work_repo, _authority_repo = repositories
    outside = tmp_path / "outside" / "result.txt"

    with pytest.raises(crew.CrewError) as excinfo:
        crew.plan_dispatch(
            node=_node(outside),
            project="authority-project",
            repo=work_repo,
            config=CONFIG,
        )

    detail = str(excinfo.value)
    assert str(outside) in detail
    assert str(crew.runs_dir()) in detail
    assert str(crew.reports_dir()) in detail
    assert crew.runs_dir().is_relative_to(home)


@pytest.mark.parametrize("delivery_root", [crew.runs_dir, crew.reports_dir])
def test_non_repository_delivery_roots_remain_valid_for_report_only_nodes(
    home: Path,
    repositories: tuple[Path, Path],
    delivery_root,
) -> None:
    work_repo, _authority_repo = repositories
    delivery = delivery_root() / "authority-project" / "verification.json"

    resolution = crew.plan_dispatch(
        node=_node(delivery),
        project="authority-project",
        repo=work_repo,
        config=CONFIG,
    )

    assert resolution.validation.ok is True
    assert resolution.node.write_paths == [str(delivery)]
    assert delivery.is_relative_to(home)
