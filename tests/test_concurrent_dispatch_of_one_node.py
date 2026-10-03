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
it back, so the retry launches. The rest cover reclamation: a holder killed
without releasing would block the node for everyone, so the record carries the
holder's pid and process start time, and a dispatch finding a holder that is
gone — or a pid that now names a different process — moves that claim aside
and takes the path, while a living holder still refuses.
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


def _plant_claim(**record: Any) -> Path:
    """Write a claim record as a holder that never returned to release it."""
    path = _claim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _dead_pid() -> int:
    """A pid whose process has exited and been reaped."""
    child = subprocess.Popen(["true"], stdout=subprocess.DEVNULL)
    child.wait()
    return child.pid


def test_a_claim_left_by_a_dead_holder_is_reclaimed(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatcher killed holding the claim must not block the node."""
    config_home, repo = home
    stale_run_id = "r-20261003T00000000000000-dead-holder"
    dead_pid = _dead_pid()
    _plant_claim(
        run_id=stale_run_id,
        project=PROJECT,
        session=SESSION,
        worktree_identity=SESSION,
        node=NODE_ID,
        pid=dead_pid,
        process_start_time="0",
        created_at="2026-10-03T00:00:00Z",
    )
    seam_calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _worktree_seam(tmp_path, seam_calls)
    )

    record = _dispatch(config_home, repo)

    assert seam_calls == [NODE_ID]
    reclaimed = record["reclaimed_node_claim"]
    assert reclaimed["run_id"] == stale_run_id
    assert reclaimed["pid"] == dead_pid
    # The dead holder's claim was moved aside rather than deleted, and this
    # dispatch's own claim was given back when it finished.
    assert Path(reclaimed["moved_to"]).exists()
    assert not _claim_path().exists()


def test_a_claim_whose_pid_now_names_another_process_is_reclaimed(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reused pid must not read as the recorded holder still owning the path."""
    config_home, repo = home
    stale_run_id = "r-20261003T00000000000000-reused-pid"
    _plant_claim(
        run_id=stale_run_id,
        project=PROJECT,
        session=SESSION,
        worktree_identity=SESSION,
        node=NODE_ID,
        pid=os.getpid(),
        process_start_time="0",
        created_at="2026-10-03T00:00:00Z",
    )
    seam_calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _worktree_seam(tmp_path, seam_calls)
    )

    record = _dispatch(config_home, repo)

    assert seam_calls == [NODE_ID]
    assert record["reclaimed_node_claim"]["run_id"] == stale_run_id


def test_a_claim_held_by_a_live_process_still_refuses(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live holder is named in the refusal and keeps its claim."""
    config_home, repo = home
    live_run_id = "r-20261003T00000000000000-live-holder"
    _plant_claim(
        run_id=live_run_id,
        project=PROJECT,
        session=SESSION,
        worktree_identity=SESSION,
        node=NODE_ID,
        pid=os.getpid(),
        process_start_time=dispatch_module._process_start_time(os.getpid()),
        created_at="2026-10-03T00:00:00Z",
    )
    seam_calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _worktree_seam(tmp_path, seam_calls)
    )

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(config_home, repo)

    assert live_run_id in str(refusal.value)
    assert seam_calls == []
    assert _claim_path().exists()


def test_a_pid_beyond_the_c_int_range_refuses_without_raising(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid the kernel cannot take must reach the refusal, not an exception.

    ``os.kill`` raises ``OverflowError`` for a pid the kernel's int cannot
    hold, so without treating that as an unanswerable record the whole
    dispatch dies with a traceback instead of the D12 refusal.
    """
    config_home, repo = home
    absurd_run_id = "r-20261003T00000000000000-absurd-pid"
    _plant_claim(
        run_id=absurd_run_id,
        project=PROJECT,
        session=SESSION,
        worktree_identity=SESSION,
        node=NODE_ID,
        pid=10**30,
        process_start_time="0",
        created_at="2026-10-03T00:00:00Z",
    )
    seam_calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _worktree_seam(tmp_path, seam_calls)
    )

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(config_home, repo)

    assert absurd_run_id in str(refusal.value)
    assert seam_calls == []
    assert _claim_path().exists()


def _plant_empty_claim(*, age_seconds: float) -> Path:
    """Write a claim file with no record in it, as a dead writer leaves."""
    path = _claim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    written = time.time() - age_seconds
    os.utime(path, (written, written))
    return path


def test_a_stale_empty_claim_is_reclaimed_and_the_dispatch_proceeds(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatcher killed between exclusive create and record write must not wedge."""
    config_home, repo = home
    _plant_empty_claim(age_seconds=3600)
    seam_calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _worktree_seam(tmp_path, seam_calls)
    )

    record = _dispatch(config_home, repo)

    assert seam_calls == [NODE_ID]
    reclaimed = record["reclaimed_node_claim"]
    assert reclaimed["run_id"] is None
    assert Path(reclaimed["moved_to"]).exists()
    assert Path(reclaimed["moved_to"]).read_text(encoding="utf-8") == ""
    assert not _claim_path().exists()


def test_a_fresh_empty_claim_refuses(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty claim still being written must refuse rather than be displaced."""
    config_home, repo = home
    _plant_empty_claim(age_seconds=0)
    seam_calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _worktree_seam(tmp_path, seam_calls)
    )

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(config_home, repo)

    assert "already in flight" in str(refusal.value)
    assert seam_calls == []
    assert _claim_path().exists()
