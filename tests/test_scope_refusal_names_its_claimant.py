"""A scope refusal names the live claim, never the call it refused.

A refusal carries two run identities that are not the same: the live run
holding the path, and the call that was turned away. The refusal payload gave
only the first, under the key a caller reads as its own — so a coordinator
whose hand dispatch was refused by a run its own review reflex had launched
read the refusal as its own dispatch conflicting with itself. Two coordinators
mis-read it that way over two days, and the run named was working normally
throughout.

The case here is the incident's shape: a live claim holds the two durable
review-store paths of a review node, outside any repository, and a second
dispatch of that same node is refused. The refused call reports no run id of
its own, and the claim is named separately as the conflicting run, with the
conflicting node — which is how a reflex review of a node already in flight is
recognisable.

The real pointer directory under the user's config home is asserted untouched
across every case, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew.runs import list_live, pointer_path

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "scope-refusal-fixture"
NODE_ID = "review-out-of-repo"
SESSION = "session-scope-refusal"
CLAIMANT_RUN = "r-20260101T000000000000-review-in-flight"
CLAIMANT_NODE = NODE_ID
REVIEWED_RUN = "r-20260101T000000000001-reviewed-run"
REVIEWED_HEAD = "0123456789abcdef0123456789abcdef01234567"
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
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
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
    body = '<body><h2 id="s5">A review writes outside the repository</h2></body>'
    return f"{head}{metas}</head>{body}</html>"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
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
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> Any:
    """No case may write this fixture's pointer into the real crew home."""

    def fixture_pointers() -> list[str]:
        found = []
        for path in REAL_LIVE.glob("*.json"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if PROJECT in text or NODE_ID in text:
                found.append(path.name)
        return found

    assert fixture_pointers() == []
    yield
    assert fixture_pointers() == []


def _store_paths(config_home: Path) -> list[str]:
    """The two durable paths a review of one run at one head writes.

    They live under the crew configuration home, which no repository contains,
    and the node that declares them declares nothing inside the repository.
    """
    store = config_home / "crew" / "reviews" / PROJECT
    base = str(store / (REVIEWED_RUN + ".json"))
    at_head = str(store / (REVIEWED_RUN + ".at-" + REVIEWED_HEAD + ".json"))
    return [base, at_head]


def _done_when() -> str:
    return (
        "review.read_review parses both granted paths: 2 records, each with "
        "status parsed and 0 added failures"
    )


def _node(config_home: Path, write_paths: list[str]) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="review the recorded run and store the review outside the repository",
        plan="fixture",
        section="s5",
        role="review",
        spec_level="guided",
        done_when=_done_when(),
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / (NODE_ID + ".md")),
    )


def _publish_claim(
    repo: Path,
    run_id: str,
    node_id: str,
    write_paths: list[str],
) -> None:
    """A live pointer holding the paths, as a launch in flight leaves it."""
    crew._write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "pid": os.getpid(),
            "phase": "starting",
            "node": {"id": node_id, "write_paths": write_paths},
        },
    )


def _cli_dispatch(config_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch):
    """The dispatch an operator's hand issues, refused or not.

    The backend is resolved from the fixture's own config, and the refusal
    happens before any worker is launched, so nothing here starts a process.
    """
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *a, **k: CONFIG)
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            "fixture",
            "--section",
            "s5",
            "--spec-level",
            "guided",
            "--node",
            NODE_ID,
            "--goal",
            "review the recorded run and store the review outside the repository",
            "--done-when",
            _done_when(),
            "--role",
            "review",
            "--write-path",
            _store_paths(config_home)[0],
            "--session",
            SESSION,
            "--repo",
            str(repo),
        ],
    )


def test_a_refusal_names_the_claimant_apart_from_the_refused_call(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: the claimant's id was reported as the refused call's own.

    The claim here is a review of the same node, in flight — the reflex case a
    coordinator met, where the run named looked like the dispatch it refused.
    """
    config_home, repo = home
    _publish_claim(repo, CLAIMANT_RUN, CLAIMANT_NODE, _store_paths(config_home))

    result = _cli_dispatch(config_home, repo, monkeypatch)
    payload = json.loads(result.output)

    assert result.exit_code == 7, result.output
    assert payload["error"] == "scope-conflict"
    assert payload["run_id"] is None
    assert payload["run_id"] != CLAIMANT_RUN
    assert payload["conflicting_run_id"] == CLAIMANT_RUN
    assert payload["conflicting_node_id"] == CLAIMANT_NODE
    assert payload["node"] == NODE_ID
    assert CLAIMANT_RUN in payload["detail"]


def test_a_refusal_names_a_claimant_of_another_node(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conflicting node is the claimant's own, not the refused call's."""
    config_home, repo = home
    other_run = "r-20260101T000000000002-holding-the-store"
    other_node = "review-of-another-task"
    _publish_claim(repo, other_run, other_node, _store_paths(config_home))

    result = _cli_dispatch(config_home, repo, monkeypatch)
    payload = json.loads(result.output)

    assert result.exit_code == 7, result.output
    assert payload["run_id"] is None
    assert payload["conflicting_run_id"] == other_run
    assert payload["conflicting_node_id"] == other_node
    assert payload["node"] == NODE_ID
    assert other_run in payload["detail"]


def test_an_unclaimed_store_path_is_dispatched(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shown present: with no claimant the same call is accepted.

    The positive control the refusal cases depend on — the fixture reaches a
    real dispatch, and the pointer written is the live run's own.
    """
    config_home, repo = home
    worktree = tmp_path / "worktrees" / NODE_ID

    def prepare_worktree(_repo: Path, _session: str, _node: str, base: str) -> dict:
        worktree.mkdir(parents=True, exist_ok=True)
        return {"path": str(worktree), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)

    record = crew.dispatch(
        node=_node(config_home, _store_paths(config_home)),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=SESSION,
        launcher=lambda *_args, **_kwargs: os.getpid(),
        check_budget=False,
    )

    run_id = str(record["run_id"])
    assert run_id.startswith("r-")
    assert [str(row["run_id"]) for row in list_live(project=PROJECT)] == [run_id]
    assert crew.read_pointer(run_id)["pid"] == os.getpid()
