"""Hermetic coverage for sandbox-aware delivery reachability."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Iterator
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
    "roles": {
        "implement": {"execution_capable": True, "sandbox": "worktree-full"},
        "investigate": {"execution_capable": False, "sandbox": "read-only"},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _directory_snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    real_live = crew.live_dir()
    before = _directory_snapshot(real_live)
    config_home = tmp_path / "config"
    worker_temp = tmp_path / "worker-temp"
    config_home.mkdir()
    worker_temp.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(tempfile, "tempdir", str(worker_temp))
    yield config_home
    assert _directory_snapshot(real_live) == before


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
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
    (root / "docs" / "plans" / "sandbox-delivery.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="sandbox-delivery">'
        '<h2 id="dispatch">Dispatch</h2>',
        encoding="utf-8",
    )
    (root / "package" / "input.txt").write_text("input\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "package", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _node(
    path: str | Path,
    *,
    role: str = "investigate",
    manifest: str | Path,
    node_id: str = "sandbox-candidate",
) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="record one sandbox delivery result",
        plan="sandbox-delivery",
        section="dispatch",
        role=role,
        done_when="command output records 1 sandbox delivery finding",
        write_paths=[str(path)],
        manifest_path=str(manifest),
        time_budget="20m",
    )


def _worktrees(repo: Path) -> str:
    return subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_investigation_delivers_to_the_shared_reports_directory(
    home: Path, repo: Path
) -> None:
    manifest = crew.reports_dir() / "investigation" / "manifest.md"
    launched: dict = {}

    def launcher(plan, **_kwargs):
        launched["plan"] = plan
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("status: complete\n", encoding="utf-8")
        return 4242

    record = crew.dispatch(
        node=_node(manifest, manifest=manifest),
        project="proj",
        repo=repo,
        config=CONFIG,
        session="reports-delivery",
        check_budget=False,
        launcher=launcher,
    )

    assert manifest.read_text(encoding="utf-8") == "status: complete\n"
    plan = launched["plan"]
    assert plan.cwd == str(manifest.parent)
    add_dirs = {
        plan.argv[index + 1]
        for index, argument in enumerate(plan.argv)
        if argument == "--add-dir"
    }
    assert str(crew.reports_dir()) in add_dirs
    assert str(crew.reports_dir()) in record["sandbox_write_roots"]
    assert str(crew.run_dir(record["run_id"])) in record["sandbox_write_roots"]
    assert crew.live_dir().is_relative_to(home)


def test_unreachable_write_path_is_invalid_before_launch(
    home: Path, repo: Path
) -> None:
    manifest = crew.reports_dir() / "investigation" / "manifest.md"
    output = repo / "package" / "output.txt"
    node = _node(output, manifest=manifest)

    resolution = crew.plan_dispatch(
        node=node,
        project="proj",
        repo=repo,
        config=CONFIG,
    )

    assert resolution.validation.ok is False
    detail = " ".join(finding["detail"] for finding in resolution.validation.findings)
    assert str(output) in detail
    assert "read-only" in detail

    worktrees_before = _worktrees(repo)
    with pytest.raises(crew.CrewError, match="not dispatchable") as excinfo:
        crew.dispatch(
            node=_node(output, manifest=manifest),
            project="proj",
            repo=repo,
            config=CONFIG,
            session="unreachable-delivery",
            check_budget=False,
            launcher=lambda *args, **kwargs: pytest.fail("worker must not launch"),
        )
    assert str(output) in str(excinfo.value)
    assert "read-only" in str(excinfo.value)
    assert _worktrees(repo) == worktrees_before
    assert crew.list_live() == []
    assert crew.live_dir().is_relative_to(home)


def test_worktree_full_node_keeps_repository_delivery(home: Path, repo: Path) -> None:
    manifest = crew.run_dir("r-worktree-delivery") / "manifest.md"
    resolution = crew.plan_dispatch(
        node=_node(
            "package/output.txt",
            role="implement",
            manifest=manifest,
            node_id="worktree-candidate",
        ),
        run_id="r-worktree-delivery",
        project="proj",
        repo=repo,
        config=CONFIG,
    )

    assert resolution.validation.ok is True
    assert resolution.sandbox_write_roots is None
    assert resolution.as_dict()["sandbox"] == {
        "tier": "worktree-full",
        "write_roots": None,
    }
    assert crew.live_dir().is_relative_to(home)


@pytest.mark.parametrize("delivery", ["run", "temporary"])
def test_read_only_tier_retains_runtime_delivery_grants(
    home: Path, repo: Path, delivery: str
) -> None:
    run_id = f"r-{delivery}-delivery"
    manifest = crew.reports_dir() / "investigation" / f"{delivery}.md"
    write_path = crew.run_dir(run_id) / "finding.json"
    granted_path = (
        write_path
        if delivery == "run"
        else Path(tempfile.gettempdir()) / f"{run_id}.json"
    )

    resolution = crew.plan_dispatch(
        node=_node(write_path, manifest=manifest, node_id=f"{delivery}-candidate"),
        run_id=run_id,
        project="proj",
        repo=repo,
        config=CONFIG,
    )

    assert resolution.validation.ok is True
    assert resolution.sandbox_write_roots is not None
    assert any(
        granted_path.resolve().is_relative_to(root)
        for root in resolution.sandbox_write_roots
    )
    assert crew.live_dir().is_relative_to(home)
