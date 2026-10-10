"""Promotion records the advisory pick and names an overruled hold.

A dispatch that overrides the picker — by forcing deterministic routing, naming
a lane, or asking for the local lane — starts the advisory pick asynchronously
and returns before it has answered. These tests prove that a promotion landing
inside that window waits for the answer, and that a run whose answer was a hold
the dispatch went past records its own route mode rather than a shadow pick's.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew, ledger
from reckon.crew import picker as picker_module
from reckon.crew.runs import read_pointer

PROJECT = "proj"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "local": {
            "launch": "in-harness",
            "model": "test-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}, "inline": {"backend": "local"}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

_UNREVIEWED_PROMOTION_WAIVED = (
    "the fixture exercises promotion plumbing rather than reviewing this run; "
    "the review lifecycle has its own coverage"
)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the transient crew directory at a temp tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MCP_READ_DEADLINE_SECONDS", "240")
    return config_home


@pytest.fixture()
def repo(tmp_path):
    """A throwaway git repository with a docs tree and the fleet script."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    (root / "reckon").mkdir()
    (root / "reckon" / "target.py").write_text("value = 1\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "reckon"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _node(**overrides) -> crew.TaskNode:
    fields = {
        "id": "node-a",
        "goal": "record the advisory pick on the promoted row",
        "plan": "plan-a",
        "section": "§3",
        "done_when": "uv run pytest tests/test_promotion_records_the_pick.py exits 0",
        "write_paths": ["reckon/target.py"],
        "time_budget": "20m",
        "spec_level": "exact",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _deliver(record: dict) -> None:
    manifest = Path(record["manifest_path"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {record['node']['id']}\nstatus: complete\n")


def _commit_work(repo: Path) -> str:
    (repo / "reckon" / "target.py").write_text("value = 2\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "reckon/target.py"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: the run's declared work"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_ledger(repo: Path) -> None:
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=repo)
    subprocess.run(
        ["git", "add", f"docs/state/{PROJECT}/crew.json"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: seed an empty project ledger"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _detached_picker_thread(monkeypatch, workers: list[threading.Thread]) -> None:
    """Run the deferred advisory pick in a thread instead of a subprocess."""
    dispatch = importlib.import_module("reckon.crew.dispatch")
    original_popen = subprocess.Popen

    def detached_runner(argv, **kwargs):
        if "_record_shadow_picker_selection" not in str(argv):
            return original_popen(argv, **kwargs)
        worker = threading.Thread(
            target=dispatch._record_shadow_picker_selection,
            args=(Path(argv[-1]),),
            daemon=True,
        )
        workers.append(worker)
        worker.start()
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(dispatch.subprocess, "Popen", detached_runner)


def _pick(monkeypatch, answer: dict, *, delay: float = 0.0) -> None:
    """Answer the advisory pick, optionally after a delay."""

    def answer_pick(*_args, **_kwargs):
        if delay:
            time.sleep(delay)
        return SimpleNamespace(as_dict=lambda: dict(answer))

    monkeypatch.setattr(picker_module, "pick", answer_pick)


def _dispatch_override(repo: Path) -> dict:
    """Dispatch past the picker, which starts its pick asynchronously."""
    return crew.dispatch(
        node=_node(),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session="sess-a",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
        route="deterministic",
        local=True,
        check_budget=False,
        watch_required=False,
    )


def _promote(run_id: str, repo: Path) -> dict:
    return crew.complete(
        run_id,
        gate="passed",
        review_waiver=_UNREVIEWED_PROMOTION_WAIVED,
    )["record"]


_ROUTE_ANSWER = {
    "action": "route",
    "backend": "alpha",
    "family": "test",
    "model": "test-model",
    "effort": "medium",
    "probabilities": {"alpha": 0.9},
    "confidence": 0.9,
    "jev_model": "test",
    "fallback_reason": None,
    "latency_ms": 40.0,
    "excluded": [],
}

_HOLD_ANSWER = {
    "action": "hold",
    "backend": None,
    "family": None,
    "model": None,
    "effort": None,
    "probabilities": {},
    "confidence": 0.4,
    "jev_model": "test",
    "fallback_reason": None,
    "latency_ms": 30.0,
    "excluded": [],
}


def test_promotion_waits_for_the_in_flight_pick_and_records_it(
    home, repo, monkeypatch
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_pick(*_args, **_kwargs):
        entered.set()
        assert release.wait(10), "the advisory pick was never released"
        return SimpleNamespace(as_dict=lambda: dict(_ROUTE_ANSWER))

    monkeypatch.setattr(picker_module, "pick", blocked_pick)
    workers: list[threading.Thread] = []
    _detached_picker_thread(monkeypatch, workers)
    record = _dispatch_override(repo)
    _deliver(record)
    work = _commit_work(repo)
    _init_ledger(repo)
    assert entered.wait(5), "the advisory pick never started"
    # The pick is still running, so the run carries no answer to promote yet.
    assert read_pointer(record["run_id"])["picker_selection"] is None

    result: dict = {}

    def promote() -> None:
        result["row"] = crew.complete(
            record["run_id"],
            gate="passed",
            commits=[work],
            review_waiver=_UNREVIEWED_PROMOTION_WAIVED,
        )["record"]

    promoter = threading.Thread(target=promote, daemon=True)
    promoter.start()
    time.sleep(0.5)  # the promotion is now waiting for the in-flight pick
    release.set()
    promoter.join(timeout=120)
    assert not promoter.is_alive(), "the promotion never returned"

    assert result["row"]["picker_selection"]["action"] == "route"
    assert result["row"]["picker_selection"]["backend"] == "alpha"
    for worker in workers:
        worker.join(timeout=5)


def test_promotion_names_the_timeout_when_the_pick_never_lands(
    home, repo, monkeypatch
) -> None:
    from reckon.crew import promotion as promotion_module

    monkeypatch.setattr(promotion_module, "_SHADOW_PICK_WAIT_SECONDS", 0.4)
    workers: list[threading.Thread] = []
    _detached_picker_thread(monkeypatch, workers)
    _pick(monkeypatch, _ROUTE_ANSWER, delay=2.0)
    record = _dispatch_override(repo)
    _deliver(record)
    work = _commit_work(repo)
    _init_ledger(repo)

    row = crew.complete(
        record["run_id"],
        gate="passed",
        commits=[work],
        review_waiver=_UNREVIEWED_PROMOTION_WAIVED,
    )["record"]

    assert row["picker_selection"]["action"] == "fallback"
    assert "timeout" in str(row["picker_selection"]["fallback_reason"])


def test_an_overruled_hold_records_its_own_route_mode(home, repo, monkeypatch) -> None:
    workers: list[threading.Thread] = []
    _detached_picker_thread(monkeypatch, workers)
    _pick(monkeypatch, _HOLD_ANSWER)
    record = _dispatch_override(repo)
    _deliver(record)
    _commit_work(repo)
    _init_ledger(repo)

    row = _promote(record["run_id"], repo)

    assert row["picker_selection"]["action"] == "hold"
    assert row["route_mode"] == "overridden-hold"
    for worker in workers:
        worker.join(timeout=5)


def test_pick_outcomes_counts_an_overruled_hold_apart_from_shadow() -> None:
    from reckon.crew.picker import outcomes

    rows = {
        PROJECT: [
            {"picker_selection": _HOLD_ANSWER, "route_mode": "overridden-hold"},
            {"picker_selection": _ROUTE_ANSWER, "route_mode": "shadow"},
        ]
    }
    report = outcomes.summarize(rows, {})
    modes = report["mechanics"]["route_modes"]
    assert modes["overridden-hold"] == 1
    assert modes["shadow"] == 1
