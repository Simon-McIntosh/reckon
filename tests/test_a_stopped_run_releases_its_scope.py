"""A stopped run's scope is released by a command, and its worktree is kept.

A stopped run whose worktree holds commits keeps its write paths claimed until
it is promoted or recovered. When it can be neither -- the worker is gone and
no promotion route will take its evidence -- a fresh node for the same section
is refused on the scope conflict and the section waits behind work nobody can
move.

``crew release --scope`` releases that claim: the worktree and every commit in
it stay where they are, the record names the kept head so a fresh node merges
by its revision, and two runs are refused -- one whose worker still runs, and
one whose promotion dry run would pass, promotion being the route for it.

The real live pointer directory is asserted untouched across every case,
because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew import promotion
from reckon.crew import resume as resume_module
from reckon.crew.node import SCOPE_RELEASE_FIELD, claim_disposition
from reckon.crew.runs import pointer_path, run_dir

PROJECT = "release-scope-fixture"
SECTION = "s5"
STOPPED_RUN = "r-20260101T000000000000-stopped-run"
STOPPED_NODE = "stalled-implement"
FRESH_NODE = "fresh-implement"
SESSION = "session-release-scope"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

CONFIG: dict[str, Any] = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _plan_document() -> str:
    head = "<!doctype html><html><head>"
    metas = (
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        '<meta name="plan-version" content="0">'
    )
    body = f'<body><h2 id="{SECTION}">A stopped run keeps its scope</h2></body>'
    return f"{head}{metas}</head>{body}</html>"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "checkout"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(_plan_document(), encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _git(repo, "add", "seed.txt", "docs")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}),
        encoding="utf-8",
    )
    return config_home, repo


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> Any:
    from tests.real_home_traces import traces_naming

    def fixture_pointers() -> list[str]:
        return traces_naming(REAL_LIVE.parent, ("live",), (PROJECT, STOPPED_RUN))

    assert fixture_pointers() == []
    yield
    assert fixture_pointers() == []


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _write_path(repo: Path) -> str:
    """A generated-state path, which a textual merge cannot reconcile.

    A source path is admitted concurrently under a project that declares no
    sharing policy, so it cannot demonstrate a claim. Generated state is one of
    the classes that always fences, which keeps the negative control measuring
    the claim rather than the sharing policy.
    """
    return "docs/state/held.json"


def _done_when() -> str:
    return "the section's work records 0 added failures"


def _node(config_home: Path, repo: Path, node_id: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="write the section's change at the declared path",
        plan="fixture",
        section=SECTION,
        role="review",
        spec_level="guided",
        done_when=_done_when(),
        write_paths=[_write_path(repo)],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / (node_id + ".md")),
    )


def _stopped_run(
    config_home: Path,
    repo: Path,
    tmp_path: Path,
    *,
    alive: bool = False,
    commits: int = 2,
) -> dict[str, Any]:
    """A live pointer for a stopped run whose worktree carries real commits."""
    base = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worktrees" / STOPPED_NODE
    _git(repo, "worktree", "add", "--detach", "-q", str(worktree), base)
    for number in range(commits):
        target = worktree / "docs" / ("work-" + str(number) + ".txt")
        target.write_text("work " + str(number) + "\n", encoding="utf-8")
        _git(worktree, "add", str(target))
        _git(worktree, "commit", "-q", "-m", "feat: work " + str(number))
    head = _git(worktree, "rev-parse", "HEAD")
    pid = os.getpid() if alive else _dead_pid()
    pointer = {
        "run_id": STOPPED_RUN,
        "project": PROJECT,
        "repo": str(repo),
        "session": SESSION,
        "worktree": str(worktree),
        "base_sha": base,
        "head": head,
        "pid": pid,
        "phase": "working",
        "attempt": 1,
        "node": {
            "id": STOPPED_NODE,
            "plan": "fixture",
            "section": SECTION,
            "time_budget": "20m",
            "write_paths": [_write_path(repo)],
        },
    }
    crew._write_json(pointer_path(STOPPED_RUN), pointer)
    return {"pointer": pointer, "worktree": worktree, "base": base, "head": head}


def _dry_dispatch(config_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict[str, Any]]:
    """The fresh node's dispatch preview over the same path, launching nothing.

    The preview runs the launch's own live-claim check, so its verdict is the
    verdict a real dispatch would reach. It returns the exit code and the last
    JSON record the call printed: a refusal carries ``scope-conflict`` and the
    conflicting run, an admission the resolved plan.
    """
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--dry-run",
            "--project",
            PROJECT,
            "--plan",
            "fixture",
            "--section",
            SECTION,
            "--spec-level",
            "guided",
            "--node",
            FRESH_NODE,
            "--goal",
            "write the section's change at the declared path",
            "--done-when",
            _done_when(),
            "--role",
            "review",
            "--write-path",
            _write_path(repo),
            "--session",
            SESSION,
            "--repo",
            str(repo),
            "--no-watch",
        ],
    )
    payload: dict[str, Any] = {}
    for line in reversed(result.output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        break
    return result.exit_code, payload


def test_without_the_release_the_fresh_dispatch_is_refused(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: the stopped run's claim refuses the fresh node."""
    config_home, repo = home
    _stopped_run(config_home, repo, tmp_path)

    exit_code, payload = _dry_dispatch(config_home, repo, monkeypatch)

    assert exit_code == 7, payload
    assert payload["error"] == "scope-conflict"
    assert payload["conflicting_run_id"] == STOPPED_RUN
    assert payload["conflicting_node_id"] == STOPPED_NODE


def _release(reason: str = "its worker is gone and promotion cannot take it"):
    return resume_module.release_scope(STOPPED_RUN, reason, by="coordinator")


def test_the_release_admits_a_fresh_dispatch_and_keeps_every_commit(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the release the paths are free, and the kept head is untouched."""
    config_home, repo = home
    stopped = _stopped_run(config_home, repo, tmp_path)
    commits_before = _git(stopped["worktree"], "rev-list", stopped["base"] + "..HEAD")

    released = _release()

    assert released["scope_released"] is True
    assert released["kept_head"] == stopped["head"]
    assert released["commits"] == list(reversed(commits_before.splitlines()))

    exit_code, payload = _dry_dispatch(config_home, repo, monkeypatch)
    assert exit_code == 0, payload
    assert "error" not in payload, payload

    assert Path(stopped["worktree"]).is_dir()
    assert _git(stopped["worktree"], "rev-parse", "HEAD") == stopped["head"]
    assert (
        _git(stopped["worktree"], "rev-list", stopped["base"] + "..HEAD")
        == commits_before
    )


def test_the_record_names_the_kept_head(
    home: tuple[Path, Path], tmp_path: Path
) -> None:
    """The release names the head a fresh node merges, on the pointer and on disk."""
    config_home, repo = home
    stopped = _stopped_run(config_home, repo, tmp_path)

    _release(reason="promotion cannot take it and its worker is gone")

    pointer = crew.read_pointer(STOPPED_RUN)
    release = pointer[SCOPE_RELEASE_FIELD]
    assert release["kept_head"] == stopped["head"]
    assert release["reason"] == "promotion cannot take it and its worker is gone"
    assert release["by"] == "coordinator"
    recorded = json.loads(
        (Path(run_dir(STOPPED_RUN)) / "scope-release.json").read_text(encoding="utf-8")
    )
    assert recorded["scope_release"]["kept_head"] == stopped["head"]

    disposition = claim_disposition(pointer)
    assert disposition.binding is False
    assert stopped["head"] in disposition.reason


def test_a_working_run_is_refused(
    home: tuple[Path, Path], tmp_path: Path) -> None:
    """A run whose worker still runs is not a stopped run."""
    config_home, repo = home
    _stopped_run(config_home, repo, tmp_path, alive=True)

    with pytest.raises(crew.CrewError) as refused:
        _release()

    assert "still running" in str(refused.value)


def test_a_promotable_run_is_refused(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run its promotion dry run would take is promoted, not released."""
    config_home, repo = home
    _stopped_run(config_home, repo, tmp_path)
    monkeypatch.setattr(
        promotion,
        "complete_dry_run",
        lambda _run_id, **_kwargs: {
            "would_promote": True,
            "refusals": [],
            "invocation": "reckon crew complete --run " + STOPPED_RUN + " --land",
        },
    )

    with pytest.raises(crew.CrewError) as refused:
        _release()

    message = str(refused.value)
    assert "can still be promoted" in message
    assert "Promote it instead" in message
    assert "crew complete" in message
    assert crew.read_pointer(STOPPED_RUN).get(SCOPE_RELEASE_FIELD) is None