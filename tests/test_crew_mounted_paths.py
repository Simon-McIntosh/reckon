"""Hermetic coverage for dispatch writes against the project's mount.

A project's registered mount decides both roots: the repository the plan is read
from and the repository its work is written in are the same one. A dispatch that
names a different repository is refused before a worktree, pointer or ledger row
exists, and a write path that lands outside that repository and outside Reckon's
delivery directories is refused with both the work repository and the delivery
routes named.

The two temporary repositories below are both mounts, so a refusal that names
one of them proves the authority set is resolved from the mount rather than from
whatever repository the caller happened to name.
"""

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
    (work_repo / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (work_repo / "docs").mkdir()
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (work_repo / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
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
        spec_level="guided",
        done_when="pytest reports all mounted delivery checks passing",
        write_paths=[str(path)],
        time_budget="20m",
    )


def test_dispatch_accepts_a_write_in_the_mounted_plan_repository_and_records_authority(
    home: Path, repositories: tuple[Path, Path]
) -> None:
    work_repo, authority_repo = repositories
    assert work_repo != authority_repo
    delivery = authority_repo / "delivery" / "result.txt"

    # Read the base before the dispatch: the run records the commit it was cut
    # from, and a dispatch may leave a commit of its own behind.
    plan_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=authority_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    record = crew.dispatch(
        node=_node(delivery),
        project="authority-project",
        repo=authority_repo,
        config=CONFIG,
        session="mounted-delivery-session",
        check_budget=False,
        launcher=lambda *args, **kwargs: 0,
    )

    # One repository under both roots: the plan is read from the mount and the
    # work is written there, so the authority names it once.
    assert record["authority"] == {
        "plan": {
            "project": "authority-project",
            "docs": str((authority_repo / "docs").resolve()),
            "repository": str(authority_repo.resolve()),
            "source": "mount",
            "base_sha": plan_sha,
        },
        "write": {
            "projects": ["authority-project"],
            "repository": str(authority_repo.resolve()),
            "source": "mount",
        },
        "repositories": [str(authority_repo.resolve())],
    }
    assert record["node"]["write_paths"][0] == str(delivery)
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
    _work_repo, authority_repo = repositories
    outside = tmp_path / "outside" / "result.txt"

    with pytest.raises(crew.CrewError) as excinfo:
        crew.plan_dispatch(
            node=_node(outside),
            project="authority-project",
            repo=authority_repo,
            config=CONFIG,
        )

    detail = str(excinfo.value)
    assert str(outside) in detail
    assert str(authority_repo.resolve()) in detail
    assert str(crew.runs_dir()) in detail
    assert str(crew.reports_dir()) in detail
    assert crew.runs_dir().is_relative_to(home)


@pytest.mark.parametrize("delivery_root", [crew.runs_dir, crew.reports_dir])
def test_non_repository_delivery_roots_remain_valid_for_report_only_nodes(
    home: Path,
    repositories: tuple[Path, Path],
    delivery_root,
) -> None:
    _work_repo, authority_repo = repositories
    delivery = delivery_root() / "authority-project" / "verification.json"

    resolution = crew.plan_dispatch(
        node=_node(delivery),
        project="authority-project",
        repo=authority_repo,
        config=CONFIG,
    )

    assert resolution.validation.ok is True
    assert resolution.node.write_paths[0] == str(delivery)
    assert delivery.is_relative_to(home)


def test_write_authority_needs_a_mount_entry_and_nothing_in_the_repository(
    home: Path, repositories: tuple[Path, Path], tmp_path: Path
) -> None:
    """Registering a project's mount is a config-home fact, not a repo change.

    A mounts.json entry in the config home is written without copying reckon's
    UI scaffolding into the repository, so a data-only catalog can be registered
    without a pull request against it. What the entry buys is authority over the
    project's *own* repository, and nothing more: a repository named as ``--repo``
    is admitted only when it is the repository the project's mount resolves to.
    The catalog repository is left byte-identical, which is what the entry was
    supposed to cost.
    """
    _work_repo, authority_repo = repositories  # the fixture registers the mounts
    catalog = tmp_path / "data-only-catalog"
    catalog.mkdir()
    (catalog / "records.csv").write_text("id,value\n1,2\n", encoding="utf-8")
    _commit_repository(catalog, "records.csv")
    delivery = catalog / "records.csv"

    # Before any catalog entry exists, naming that repository is refused: the
    # project's mount decides where its work is written.
    with pytest.raises(crew.CrewError) as refusal:
        crew.plan_dispatch(
            node=_node(delivery),
            project="authority-project",
            repo=catalog,
            config=CONFIG,
        )
    detail = str(refusal.value)
    assert str(catalog.resolve()) in detail
    assert str(authority_repo.resolve()) in detail
    assert "--repo" in detail

    # A config-home entry registers the catalog as a project of its own and
    # creates no file in the repository — but it does not give another project
    # write authority there, because the authority is the project's own mount.
    mounts = json.loads((home / "mounts.json").read_text())
    mounts["catalog-project"] = str(catalog / "docs")
    (home / "mounts.json").write_text(json.dumps(mounts), encoding="utf-8")
    assert not (catalog / "docs").exists(), "nothing was written to the repository"

    with pytest.raises(crew.CrewError) as still_refused:
        crew.plan_dispatch(
            node=_node(delivery),
            project="authority-project",
            repo=catalog,
            config=CONFIG,
        )
    assert str(authority_repo.resolve()) in str(still_refused.value)
    assert [path.name for path in catalog.iterdir() if path.name != ".git"] == [
        "records.csv"
    ], "the repository is untouched"
