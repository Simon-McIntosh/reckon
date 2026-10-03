"""Two concurrent dispatches of one node cannot race into one worktree.

Two dispatches of one node for one session resolve to one worktree path, and
both may read the live-pointer claims before either publishes its own, so a
check-then-act guard cannot separate them: both reach ``git worktree add`` and
each creation loses directories the other is writing. The dispatch path
therefore takes an exclusive claim file under the crew store before the
worktree is cut — the kernel decides which dispatch owns the path — and the
loser reads the holder's record and refuses, naming it, before touching the
worktree.

The first case drives the interleaving the defect was measured in: both
dispatches read the live claims before either publishes, one then holds the
node claim inside worktree creation while the other is refused. The second
case is the release side: a dispatch that fails after taking the claim gives
it back, so the retry launches.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew.runs import list_live

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "concurrent-dispatch-fixture"
NODE_ID = "node-one-worktree"
SESSION = "session-concurrent-dispatch"
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
    "roles": {"implement": {}},
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
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        '<meta name="plan-version" content="0">'
        '</head><body><h2 id="s5">One worktree, two dispatches</h2></body></html>'
    )


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
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> None:
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


def _node(config_home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="cut one worktree for two concurrent dispatches of this node",
        plan="fixture",
        section="s5",
        role="implement",
        spec_level="guided",
        done_when="pytest reports exactly one of two concurrent dispatches launches",
        write_paths=["src/one.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{NODE_ID}.md"),
    )


def _dispatch(config_home: Path, repo: Path, *, launcher=None) -> dict:
    return crew.dispatch(
        node=_node(config_home),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=SESSION,
        launcher=launcher or (lambda *_args, **_kwargs: os.getpid()),
        check_budget=False,
    )


def _claim_path() -> Path:
    return dispatch_module._node_dispatch_claim_path(PROJECT, SESSION, NODE_ID)


def _worktree_seam(tmp_path: Path, calls: list[str], on_first=None):
    """Stand in for the fleet script, counting the creations that reach it."""

    def seam(_repo: Path, _session: str, node: str, base: str) -> dict:
        calls.append(node)
        path = tmp_path / "worktrees" / f"dispatch-{len(calls)}"
        path.mkdir(parents=True, exist_ok=True)
        if len(calls) == 1 and on_first is not None:
            on_first()
        return {"path": str(path), "base": base, "base_sha": base}

    return seam


def test_two_concurrent_dispatches_of_one_node_launch_exactly_one(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One dispatch holds the node claim; the other refuses naming it.

    Both threads read the live-pointer claims before either publishes its own,
    which is the interleaving that leaves a check-then-act guard with nothing
    to see. The first dispatch to take the claim then stands inside worktree
    creation, held there, while the other runs its whole admission against the
    held claim and refuses.
    """
    config_home, repo = home

    seam_calls: list[str] = []
    inside_worktree = threading.Event()
    release_winner = threading.Event()

    def hold_inside_worktree() -> None:
        inside_worktree.set()
        release_winner.wait(30)

    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        _worktree_seam(tmp_path, seam_calls, on_first=hold_inside_worktree),
    )

    read_together = threading.Barrier(2)
    thread_state = threading.local()
    read_claims = dispatch_module._repository_scope_claims

    def claims_read_before_any_publication(*args: Any, **kwargs: Any):
        snapshot = read_claims(*args, **kwargs)
        if not getattr(thread_state, "read", False):
            thread_state.read = True
            read_together.wait(timeout=30)
        return snapshot

    monkeypatch.setattr(
        dispatch_module, "_repository_scope_claims", claims_read_before_any_publication
    )

    outcomes: list[tuple[str, Any]] = []
    outcomes_lock = threading.Lock()

    def run_dispatch() -> None:
        try:
            outcome: tuple[str, Any] = ("launched", _dispatch(config_home, repo))
        except crew.CrewError as exc:
            outcome = ("refused", exc)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            outcome = ("error", exc)
        with outcomes_lock:
            outcomes.append(outcome)

    first = threading.Thread(target=run_dispatch, name="dispatch-first")
    second = threading.Thread(target=run_dispatch, name="dispatch-second")
    first.start()
    second.start()

    deadline = time.monotonic() + 30
    while not inside_worktree.is_set() and not outcomes and time.monotonic() < deadline:
        time.sleep(0.05)
    assert inside_worktree.is_set(), outcomes
    # The winner is held inside worktree creation, so the first outcome to
    # arrive is the refused dispatch. It must arrive while the claim is held:
    # releasing earlier would let it take the claim and create a second
    # worktree, which is exactly the race under test.
    while not outcomes and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(outcomes) == 1, outcomes
    release_winner.set()
    first.join(timeout=30)
    second.join(timeout=30)

    assert not first.is_alive()
    assert not second.is_alive()
    launched = [payload for kind, payload in outcomes if kind == "launched"]
    refused = [payload for kind, payload in outcomes if kind == "refused"]
    assert seam_calls == [NODE_ID], "exactly one worktree creation must run"
    assert len(launched) == 1
    assert len(refused) == 1
    winner_run_id = str(launched[0]["run_id"])
    assert winner_run_id in str(refused[0])
    # The refused dispatch left nothing of itself behind, and the winner gave
    # the claim back when it finished.
    live_ids = [str(record["run_id"]) for record in list_live(project=PROJECT)]
    assert live_ids == [winner_run_id]
    assert not _claim_path().exists()


def test_a_failed_dispatch_releases_its_claim(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that fails hands the node claim back, so the retry launches."""
    config_home, repo = home
    seam_calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _worktree_seam(tmp_path, seam_calls)
    )

    def refusing_launcher(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("the harness executable is absent")

    with pytest.raises(crew.CrewError):
        _dispatch(config_home, repo, launcher=refusing_launcher)

    assert not _claim_path().exists()

    record = _dispatch(config_home, repo)

    assert record["run_id"]
    assert seam_calls == [NODE_ID, NODE_ID]
