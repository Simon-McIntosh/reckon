"""The time fence follows the attempt through dispatch, a lane change, and a blocker.

A fence states the attempt's launch instant and the deadline the budget sets
from it. Three wiring defects leave that statement attached to the wrong
attempt or unchecked:

* ``dispatch()`` reads the clock once and passes the instant to the composer,
  but nothing drove the real dispatch to show the composed prompt states the
  deadline that instant sets;
* ``change_lane`` relaunches a run from its retained prompt, so unless the
  fence is restated the moved attempt reads the previous attempt's deadline;
* a blocker that blames an expired fence is accepted as a blocker even when the
  attempt's own record shows the deadline had not passed when the manifest was
  recorded — a claim about the clock that the clock refutes.

The checks are wiring checks, so each drives the real door rather than the
helper behind it: ``dispatch()`` with a stub launcher, ``change_lane`` against a
stopped pointer, and ``audit_manifest`` against a manifest with an attempt
record beside it.

The declared negative control restores ``change_lane``'s unrestated prompt —
the retained prompt written through unchanged — under which the lane-change
case's deadline assertions fail.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import reports as reports_module
from reckon.crew.runs import pointer_path

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        },
        "beta": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"implement": {"time_budget": "25m"}, "needs_help_after_failures": 2},
}

BRIEF_TEXT = "State the attempt's own deadline in the fence it launches under.\n"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _plus(instant: str, seconds: int) -> str:
    """The ISO-8601 UTC instant ``seconds`` after ``instant``."""
    moment = datetime.fromisoformat(instant)
    return (
        (moment + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repository carrying a plan, mounted under a temporary home."""
    config_home = tmp_path / "config"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs/plans/plan-a.html"),
        ("commit", "-q", "-m", "chore: seed fixture"),
    ):
        _git(root, *args)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


# ── The dispatch prompt states the attempt's own deadline ───────────────────


def test_a_dispatch_prompt_states_the_deadline_its_own_attempt_started_under(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dispatch() pins the attempt start into the prompt it composes."""
    base_sha = _git(repo, "rev-parse", "HEAD")

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    node = crew.TaskNode(
        id="dispatch-fence",
        goal="pin the attempt's deadline into the dispatch prompt",
        plan="",
        brief=str(brief),
        role="implement",
        spec_level="exact",
        done_when=(
            "the gate command pytest tests/test_time_fence_follows_the_attempt.py "
            "exits 0 and prints 1 passed"
        ),
        write_paths=["result.json"],
        time_budget="25m",
        manifest_path=str(tmp_path / "manifests" / "dispatch-fence.md"),
    )

    record = crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session="session-dispatch-fence",
        launcher=lambda plan, **kwargs: os.getpid(),
    )

    started = str(record["attempt_started_at"])
    prompt = Path(record["prompt_path"]).read_text(encoding="utf-8")
    deadline = _plus(started, 25 * 60)

    assert f"Launched {started} UTC; deadline {deadline}" in prompt, (
        "the dispatch prompt does not state the deadline the attempt's own "
        f"launch instant sets (started {started}, expected deadline {deadline})"
    )
    assert "`date -u`" in prompt


# ── A lane change restates the fence for the attempt it launches ────────────


def _stopped_pointer(tmp_path: Path, repo: Path, run_id: str) -> dict:
    """A cli run as a lane change finds it: gone, with a retained prompt."""
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "reports" / "node-a" / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: waiting\n", encoding="utf-8")
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    first_attempt = "2026-10-01T09:59:00Z"
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text(
        "the original dispatch prompt\n\n"
        f"FENCE — TIME\n  Launched {first_attempt} UTC; deadline "
        f"{_plus(first_attempt, 25 * 60)} — 25m from launch.\n",
        encoding="utf-8",
    )
    from tests import test_a_live_run_never_reads_dead as liveness

    liveness._write_exit_record(run_id)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "backend": "alpha",
        "dialect": "codex",
        "role": "implement",
        "fenced": True,
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": f"sess-{run_id}",
        "session_harness": "codex",
        "attempt": 1,
        "base_sha": "HEAD",
        "created_at": "2026-10-01T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(prompt_path),
        "phase": "working",
        "node": {
            "id": "node-a",
            "plan": "plan-a",
            "section": "§3",
            "time_budget": "25m",
            "write_paths": ["seed.txt"],
        },
    }
    crew._write_json(pointer_path(run_id), record)
    return record


def test_a_lane_change_restates_the_fence_for_the_attempt_it_launches(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The moved attempt reads its own deadline, not the first attempt's."""
    from types import SimpleNamespace

    run_id = "r-lane-fence"
    _stopped_pointer(tmp_path, repo, run_id)
    first_deadline = _plus("2026-10-01T09:59:00Z", 25 * 60)
    resolution = SimpleNamespace(
        backend="beta",
        launch="cli",
        backend_settings=dict(CONFIG["backends"]["beta"]),
        lane_gate={"state": "open", "paused": False, "reason": None, "detail": ""},
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )
    monkeypatch.setattr(dispatch_module, "plan_dispatch", lambda **kwargs: resolution)
    monkeypatch.setattr(
        dispatch_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_module, "resolve_dispatch_ledger_root", lambda authority: authority
    )

    moved = dispatch_module.change_lane(
        run_id,
        "beta",
        "the lane is spent",
        config=CONFIG,
        launch=True,
        launcher=lambda *args, **kwargs: 4242,
    )

    changed_at = str(moved["lane_change"]["changed_at"])
    prompt = Path(moved["prompt_path"]).read_text(encoding="utf-8")

    assert "FENCE — TIME (resumed attempt)" in prompt, (
        "the lane-changed prompt carries no restated fence"
    )
    assert (
        f"Launched {changed_at} UTC; deadline {_plus(changed_at, 25 * 60)}" in prompt
    ), "the lane-changed prompt does not restate the moved attempt's deadline"
    assert first_deadline not in prompt, (
        "the lane-changed prompt still carries the first attempt's deadline"
    )


# ── A blocker that blames a fence the clock had not yet spent ───────────────

BLOCKER_TEXT = "the node's 25-minute fence expired"


def _claim_manifest(
    tmp_path: Path, *, started_at: str
) -> tuple[str, Path, crew.TaskNode]:
    """A blocked manifest and the attempt record that dates its fence."""
    directory = tmp_path / "claim"
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.md"
    text = f"node: claim-node\nstatus: blocked\nblockers: {BLOCKER_TEXT}\n"
    manifest_path.write_text(text, encoding="utf-8")
    (directory / "attempt.json").write_text(
        json.dumps({"attempt": 1, "attempt_started_at": started_at}),
        encoding="utf-8",
    )
    node = crew.TaskNode(
        id="claim-node",
        goal="judge a fence claim against the attempt's record",
        plan="plan-a",
        role="implement",
        time_budget="25m",
        manifest_path=str(manifest_path),
    )
    return text, manifest_path, node


def test_an_early_fence_spent_blocker_is_reported_as_a_defect(tmp_path: Path) -> None:
    """A blocker written before the recorded deadline is a defect, not a blocker."""
    text, manifest_path, node = _claim_manifest(tmp_path, started_at=_utc_now())

    audit = reports_module.audit_manifest(text, node, manifest_path=manifest_path)

    assert audit["ok"] is False
    assert any(
        "expired time fence" in finding and "not a blocker" in finding
        for finding in audit["findings"]
    ), audit["findings"]


def test_a_fence_spent_blocker_after_the_recorded_deadline_stays_a_blocker(
    tmp_path: Path,
) -> None:
    """A genuine fence stop is left alone: the clock agrees with the claim."""
    long_ago = "2020-01-01T00:00:00Z"
    text, manifest_path, node = _claim_manifest(tmp_path, started_at=long_ago)

    audit = reports_module.audit_manifest(text, node, manifest_path=manifest_path)

    assert not any("expired time fence" in finding for finding in audit["findings"])
    assert audit["ok"] is True, audit["findings"]
