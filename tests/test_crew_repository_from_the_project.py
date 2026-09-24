"""Every repository-resolving surface answers with the project's own mount.

The fault: two nodes were dispatched for one project from a shell whose working
directory was a different project's checkout, so the dispatch cut its worktree
from the repository enclosing the caller and one worker oriented without
finding a single declared write path. The project's mount is the authority, so
a caller that names no repository is given the mount and a named repository
that resolves to a different one is refused before any worktree, pointer or
ledger row exists. A linked worktree shares the mount's git common directory,
so it is the same repository under a second path and is admitted.

Two temporary mounts are built here, each a git checkout carrying its own plan
and its own fleet script, so a cross-project dispatch can be observed rather
than argued about.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")
promotion_module = importlib.import_module("reckon.crew.promotion")
resumption_module = importlib.import_module("reckon.crew.resumption")

AMBER = "amber"
BASALT = "basalt"


def _config(bin_dir: Path) -> dict:
    return {
        "default_backend": "worker",
        "backends": {
            "worker": {
                "launch": "cli",
                "command": "codex",
                "model": "some-model",
                "effort": "high",
                "sandbox": "worktree-full",
                "session_reuse": True,
                "time_budget": "20m",
                "environment": {"PATH": str(bin_dir)},
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }


def _make_checkout(root: Path, slug: str) -> Path:
    """One git repository carrying a plan and the fleet script it dispatches with."""
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="fixture">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Repository guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "seed.txt").write_text(f"{slug}\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture()
def mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two registered projects, each in a repository of its own."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    amber = _make_checkout(tmp_path / "amber", AMBER)
    basalt = _make_checkout(tmp_path / "basalt", BASALT)
    (config_home / "mounts.json").write_text(
        json.dumps({AMBER: str(amber / "docs"), BASALT: str(basalt / "docs")}),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "backend-bin"
    bin_dir.mkdir()
    launcher = bin_dir / "codex"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    return {"amber": amber, "basalt": basalt, "home": config_home, "bin": bin_dir}


def _admit_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the watch seat only; delivery is read from the real registry."""

    def watch_state(project: str, *, session: str | None = None) -> dict:
        return {
            "arming_line": f"reckon crew watch --project {project}",
            "watcher": {},
            "attach_line": runs._watch_attach_line(project, session=session),
            "watcher_live": True,
            "session": session,
            "session_attached": None,
            "follower": {},
        }

    monkeypatch.setattr(dispatch_module, "watch_state", watch_state)


def _node(manifest_path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id="node-repository",
        goal="resolve the repository from the project's mount",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="one repository resolution case passes",
        write_paths=["src/resolved.txt"],
        time_budget="20m",
        manifest_path=manifest_path,
    )


def _dispatch(
    repo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bin_dir: Path,
    *,
    project: str = AMBER,
) -> dict:
    _admit_watcher(monkeypatch)
    session = "session-repository"
    return crew.dispatch(
        node=_node(str(tmp_path / "manifest.md")),
        project=project,
        repo=repo,
        config=_config(bin_dir),
        session=session,
        launcher=lambda _plan, **_kwargs: 4242,
        watch_required=False,
    )


def test_a_dispatch_naming_no_repository_is_cut_from_the_project_mount(
    mounts: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell is not consulted: the working directory is a different checkout."""
    monkeypatch.chdir(mounts["basalt"])

    record = _dispatch(None, monkeypatch, tmp_path, mounts["bin"])

    assert Path(record["repo"]).resolve() == mounts["amber"].resolve()
    # The fleet places a worktree beside the repository it belongs to, so the
    # check is whose repository it is, not which directory tree it sits under:
    # its git common directory must be amber's, never the caller's checkout.
    assert (
        dispatch_module.repository_identity(record["worktree"])
        == mounts["amber"].resolve()
    )


def test_a_named_repository_that_is_not_the_mount_is_refused(
    mounts: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names both resolved roots and the flag, and writes nothing."""
    monkeypatch.chdir(mounts["basalt"])

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(mounts["basalt"], monkeypatch, tmp_path, mounts["bin"])

    message = str(refusal.value)
    assert str(mounts["basalt"].resolve()) in message
    assert str(mounts["amber"].resolve()) in message
    assert "--repo" in message
    assert crew.list_live() == []
    assert not (tmp_path / "manifest.md").exists()


def test_a_worktree_of_the_mount_is_admitted(
    mounts: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked worktree is the same repository, so it resolves to the mount."""
    checkout = tmp_path / "amber-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked", str(checkout)],
        cwd=mounts["amber"],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(checkout)

    record = _dispatch(checkout, monkeypatch, tmp_path, mounts["bin"])

    assert Path(record["repo"]).resolve() == mounts["amber"].resolve()
    assert (
        dispatch_module.repository_identity(record["worktree"])
        == mounts["amber"].resolve()
    )


def test_a_resume_refuses_a_run_recorded_in_another_repository(
    mounts: dict[str, Path],
) -> None:
    """Resume takes the repository from the mount, so a mismatch is refused."""
    record = {
        "run_id": "r-recorded-elsewhere",
        "project": AMBER,
        "repo": str(mounts["basalt"]),
        "worktree": str(mounts["basalt"] / "worktree"),
        "launch": "cli",
        "node": {},
    }

    with pytest.raises(crew.CrewError) as refusal:
        resumption_module._resume(
            "r-recorded-elsewhere",
            record,
            config=_config(mounts["bin"]),
            launcher=lambda _plan, **_kwargs: 4242,
        )

    message = str(refusal.value)
    assert str(mounts["basalt"].resolve()) in message
    assert str(mounts["amber"].resolve()) in message


def test_a_promotion_refuses_a_checkout_that_is_not_the_mount(
    mounts: dict[str, Path],
) -> None:
    """Promotion writes the ledger row into the mount, so a mismatch is refused."""
    run_id = "r-promoted-elsewhere"
    pointer = runs.pointer_path(run_id)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    runs._write_json(
        pointer,
        {
            "run_id": run_id,
            "project": AMBER,
            "repo": str(mounts["amber"]),
            "worktree": str(mounts["amber"]),
            "phase": "working",
            "node": {"write_paths": []},
        },
    )

    with pytest.raises(crew.CrewError) as refusal:
        promotion_module.complete(
            run_id,
            gate="not-run",
            outcome="the run produced no gate",
            root=str(mounts["basalt"]),
        )

    message = str(refusal.value)
    assert str(mounts["basalt"].resolve()) in message
    assert str(mounts["amber"].resolve()) in message
    assert "--checkout-path" in message