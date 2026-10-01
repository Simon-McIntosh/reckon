"""Two dispatches racing for one set of paths: exactly one survives.

Both dispatches publish a claim before either reaches its admission check, so
each reads the other as a live claim and, refusing on sight, both withdraw and
the paths are left with no worker. The claim registered first owns the paths: a
peer claim that has not launched a worker and was registered after this
dispatch's own is disregarded at the check, so only that peer refuses when it
checks, naming the winner. A claim whose run has already launched keeps today's
refusal.

Each case drives one dispatch and plants the second claim where a real second
dispatch would have published it — from inside the worktree seam, which runs
after this dispatch's own claim is registered and before it reads the live
claims again. The planted record carries the same fields the launch claim
carries, so the check reads it exactly as it reads a peer dispatch's own.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew.runs import list_live, pointer_path

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "race-claim-fixture"
NODE_ID = "node-racing"
SESSION = "session-race-claim"
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
        '</head><body><h2 id="s5">One of two racing claims survives</h2></body></html>'
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
        goal="keep the paths one racing dispatch of two owns",
        plan="fixture",
        section="s5",
        role="implement",
        spec_level="guided",
        done_when="pytest reports exactly one of two racing claims proceeds",
        write_paths=["src/claimed.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{NODE_ID}.md"),
    )


def _dispatch(config_home: Path, repo: Path, *, session: str = SESSION) -> dict:
    return crew.dispatch(
        node=_node(config_home),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=lambda *_args, **_kwargs: os.getpid(),
        check_budget=False,
    )


def _plant_peer_claim(
    repo: Path,
    peer_run_id: str,
    *,
    registered_at: str,
    launched: bool = False,
) -> None:
    """Publish a peer dispatch's launch claim as the second racer's claim.

    The fields are the ones the launch-claim write carries: no pid and no
    worktree while the run is still composing, and both once it has launched.
    """
    record: dict[str, Any] = {
        "run_id": peer_run_id,
        "project": PROJECT,
        "repo": str(repo),
        "node": {"id": "node-peer", "write_paths": ["src/claimed.py"]},
        "phase": "starting",
        "created_at": registered_at,
    }
    if launched:
        record["worktree"] = str(repo.parent / "peer-worktree")
        record["pid"] = os.getpid()
    crew._write_json(pointer_path(peer_run_id), record)


def _worktree_seam(tmp_path: Path, plant) -> object:
    calls = {"count": 0}

    def seam(_repo: Path, _session: str, _node: str, base: str) -> dict:
        calls["count"] += 1
        path = tmp_path / "worktrees" / f"race-{calls['count']}"
        path.mkdir(parents=True, exist_ok=True)
        if calls["count"] == 1:
            plant()
        return {"path": str(path), "base": base, "base_sha": base}

    return seam


def live_pointers_naming(node_id: str) -> list[str]:
    """Run ids of live pointers of the fixture project that name the node."""
    naming = []
    for record in list_live(project=PROJECT):
        node = record.get("node") or {}
        if node.get("id") == node_id or node_id in str(record.get("run_id", "")):
            naming.append(str(record.get("run_id")))
    return naming


def _all_live_run_ids() -> list[str]:
    return [str(record.get("run_id")) for record in list_live(project=PROJECT)]


# ── The order of the two registrations decides which survives ───────────────


def test_the_claim_registered_first_keeps_the_paths(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The earlier claim proceeds; the later one is left to refuse itself.

    A second dispatch published its claim moments after this one and has not
    launched. Without the ordering the first dispatch reads that claim at its
    own check and withdraws with it, leaving the paths with no worker. With it,
    the first proceeds and the second, when it checks, is the one that refuses.
    """
    config_home, repo = home
    peer_run_id = "r-20261001T04000000000000-peer-registered-later"

    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        _worktree_seam(
            tmp_path,
            lambda: _plant_peer_claim(
                repo, peer_run_id, registered_at="2999-01-01T00:00:00Z"
            ),
        ),
    )

    record = _dispatch(config_home, repo)
    run_id = str(record["run_id"])

    # This dispatch survived, launched exactly one worker, and left the later
    # claim where it stands for the second racer to meet and refuse itself.
    assert live_pointers_naming(NODE_ID) == [run_id]
    assert peer_run_id in _all_live_run_ids()


def test_the_claim_registered_second_is_refused_naming_the_first(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later claim withdraws, naming the earlier one that owns the paths."""
    config_home, repo = home
    peer_run_id = "r-20261001T04000000000000-peer-registered-first"

    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        _worktree_seam(
            tmp_path,
            lambda: _plant_peer_claim(
                repo, peer_run_id, registered_at="1999-01-01T00:00:00Z"
            ),
        ),
    )

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(config_home, repo)

    assert refusal.value.run_id == peer_run_id
    assert "src/claimed.py" in str(refusal.value)
    # The refused dispatch left nothing of itself behind.
    assert live_pointers_naming(NODE_ID) == []


def test_a_launched_claim_refuses_a_newcomer_whatever_the_order(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim whose worker has launched is no longer a racing arrival.

    It registered after this dispatch and would be outranked on order alone,
    but it has already passed its own admission and launched, so it refuses
    every newcomer exactly as it does today.
    """
    config_home, repo = home
    peer_run_id = "r-20261001T04000000000000-peer-launched"

    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        _worktree_seam(
            tmp_path,
            lambda: _plant_peer_claim(
                repo,
                peer_run_id,
                registered_at="2999-01-01T00:00:00Z",
                launched=True,
            ),
        ),
    )

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(config_home, repo)

    assert refusal.value.run_id == peer_run_id
    assert live_pointers_naming(NODE_ID) == []


# ── The ordering itself ─────────────────────────────────────────────────────


def _claim(run_id: str, registered_at: str, *, launched: bool = False):
    return dispatch_module._RepositoryScopeClaim(
        project=PROJECT,
        repository=None,
        run_id=run_id,
        node_id="node-peer",
        path="src/claimed.py",
        absolute_path=Path("/repo/claimed.py"),
        declared_path="src/claimed.py",
        registered_at=registered_at,
        launched=launched,
    )


def test_a_later_registration_is_outranked_and_an_earlier_one_is_not() -> None:
    """Ordered by registration time, then by run id when the times are equal."""
    order = dispatch_module._peer_claim_is_a_later_racing_arrival

    assert order(
        _claim("r-b", "2026-10-01T04:00:01Z"),
        own_run_id="r-a",
        own_registered_at="2026-10-01T04:00:00Z",
    )
    assert not order(
        _claim("r-a", "2026-10-01T03:59:59Z"),
        own_run_id="r-b",
        own_registered_at="2026-10-01T04:00:00Z",
    )
    # Equal times fall to the run id: the larger id is the later arrival.
    assert order(
        _claim("r-2", "2026-10-01T04:00:00Z"),
        own_run_id="r-1",
        own_registered_at="2026-10-01T04:00:00Z",
    )
    assert not order(
        _claim("r-1", "2026-10-01T04:00:00Z"),
        own_run_id="r-2",
        own_registered_at="2026-10-01T04:00:00Z",
    )
    # A launched claim, an unorderable one, and a dispatch holding no claim of
    # its own are all treated as established and refuse exactly as before.
    assert not order(
        _claim("r-b", "2026-10-01T04:00:01Z", launched=True),
        own_run_id="r-a",
        own_registered_at="2026-10-01T04:00:00Z",
    )
    assert not order(
        _claim("r-b", ""),
        own_run_id="r-a",
        own_registered_at="2026-10-01T04:00:00Z",
    )
    assert not order(
        _claim("r-b", "2026-10-01T04:00:01Z"),
        own_run_id=None,
        own_registered_at=None,
    )
