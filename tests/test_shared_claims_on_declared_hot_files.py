"""A declared shared file admits co-claimants on the real dispatch path.

Four cases run against a synthesized repository driven through ``crew.dispatch``
-- the entry a real ``crew dispatch`` reaches, whose refusal is raised by
``dispatch._raise_repository_scope_conflict``. The list names ``pkg/hot.py``:
two dispatches claiming that file both launch; an unlisted file is refused
against a live claim on it; a directory claim overlapping the shared file is
refused; and with the list absent the shared file is refused. A fifth case shows
both co-claimants remain visible in the read model.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew

PROJECT = "shared-proj"
PLAN = "shared-claims"
SHARED_FILE = "pkg/hot.py"
UNLISTED_FILE = "pkg/cold.py"
DIRECTORY = "pkg"
REASON = "concurrent editors work in separate functions"

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


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    repo = tmp_path / "repository"
    (repo / "pkg").mkdir(parents=True)
    (repo / SHARED_FILE).write_text("# shared hot module\n", encoding="utf-8")
    (repo / UNLISTED_FILE).write_text("# unlisted module\n", encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / f"{PLAN}.html").write_text(
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{PLAN}">\n'
        '</head><body><h2 id="s1">Shared claims</h2></body></html>\n',
        encoding="utf-8",
    )
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
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
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["add", "seed.txt", "pkg", "docs", "skills"],
        [
            "-c",
            "user.email=worker@example.invalid",
            "-c",
            "user.name=Worker",
            "commit",
            "-q",
            "-m",
            "chore: seed fixture",
        ],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _declare(repo: Path, paths: list[dict]) -> None:
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "shared-write-paths.json").write_text(
        json.dumps({"project": PROJECT, "paths": paths}), encoding="utf-8"
    )


def _node(node_id: str, *write_paths: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="edit one region of a declared module",
        plan=PLAN,
        section="s1",
        spec_level="guided",
        done_when="pytest reports the shared-claim case passing",
        write_paths=list(write_paths),
        time_budget="20m",
    )


def _dispatch(repo: Path, node_id: str, *write_paths: str) -> dict:
    """Dispatch one node through the entry the ``crew dispatch`` verb calls.

    The launcher stand-in reports this live process, because a write claim is
    judged by the disposition of the run's recorded process: a stub naming an
    exited pid leaves the owner's claim disregarded, and the co-claim would then
    be admitted for the wrong reason.
    """
    return crew.dispatch(
        node=_node(node_id, *write_paths),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=f"session-{node_id}",
        check_budget=False,
        launcher=lambda *_args, **_kwargs: os.getpid(),
    )


def test_two_dispatches_on_a_listed_file_both_launch(repository: Path) -> None:
    """Case one: both co-claimants of the declared file launch."""
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])

    owner = _dispatch(repository, "owner", SHARED_FILE)
    joiner = _dispatch(repository, "joiner", SHARED_FILE)

    assert owner["run_id"] != joiner["run_id"]
    assert crew.pointer_path(owner["run_id"]).is_file()
    assert crew.pointer_path(joiner["run_id"]).is_file()


def test_an_unlisted_file_is_refused_against_a_live_claim(repository: Path) -> None:
    """Case two: a path not named in the list keeps the whole-file refusal."""
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])
    owner = _dispatch(repository, "owner", UNLISTED_FILE)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(repository, "joiner", UNLISTED_FILE)

    assert refusal.value.run_id == owner["run_id"]


def test_a_directory_claim_containing_a_shared_file_is_refused(
    repository: Path,
) -> None:
    """Case three: only the named file is shareable, not the directory."""
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])
    owner = _dispatch(repository, "owner", SHARED_FILE)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(repository, "joiner", DIRECTORY)

    assert refusal.value.run_id == owner["run_id"]


def test_a_shared_file_is_refused_when_no_list_is_present(repository: Path) -> None:
    """Case four: an absent list leaves the whole-file refusal unchanged."""
    owner = _dispatch(repository, "owner", SHARED_FILE)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(repository, "joiner", SHARED_FILE)

    assert refusal.value.run_id == owner["run_id"]


def test_both_co_claimants_stay_visible_in_the_read_model(repository: Path) -> None:
    """Case five: the permitted claims remain, so scope_claims lists both."""
    _declare(repository, [{"path": SHARED_FILE, "reason": REASON}])
    _dispatch(repository, "owner", SHARED_FILE)
    _dispatch(repository, "joiner", SHARED_FILE)

    shared_claimants = sorted(
        claim["node"]
        for claim in crew.scope_claims(PROJECT, repository)
        if claim["path"] == SHARED_FILE
    )
    assert shared_claimants == ["joiner", "owner"]
