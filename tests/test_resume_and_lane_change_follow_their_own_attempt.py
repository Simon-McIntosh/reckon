"""A resumed or lane-changed run follows the fence of the attempt it launches.

Primary dispatch hands the fence ``run_dir(run_id)`` as the run directory and
records whether the composed argv carries the fence on the pointer's ``fenced``
field. ``resume_plan`` and ``change_lane`` build their argv through the same
plan builder, so both must bind the run's own directory — not only the recorded
manifest's parent — and must set ``fenced`` from the attempt they compose rather
than leave the prior attempt's value in place.

Three properties, each on the launch path it names:

* a resumed fenced run whose manifest lies outside its run directory composes an
  argv that binds both the run directory and the manifest's directory;
* the same run, lane-changed, composes the same two binds;
* a fenced CLI run lane-changed to an in-harness backend reads ``fenced`` false
  on its pointer, because that attempt composes no fence at all.

The declared negative control reverts this change: it passes the manifest parent
as the fence's run directory (reddening cases 1 and 2) and leaves ``fenced``
untouched on a lane change (reddening case 3). The red log's first line prints
that mutation verbatim and the run ends ``EXIT=1``.

Run directly, this module prints the mutation and the command that produced the
red log beneath it; it spawns nothing itself.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import crew
from reckon.crew.runs import pointer_path
from tests import test_a_live_run_never_reads_dead as liveness

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"

# The declared mutation, printed verbatim as the red log's first line.
NEGATIVE_CONTROL_MUTATION = (
    "passing the manifest parent reddens cases 1 and 2; leaving fenced "
    "untouched on lane change reddens case 3"
)

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
        "native": {"launch": "in-harness", "time_budget": "25m"},
    },
    "roles": {"implement": {}, "inline": {"backend": "native"}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"implement": {"time_budget": "25m"}, "needs_help_after_failures": 2},
}


@pytest.fixture()
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary operator home exposing the shipped default protected set.

    ``HOME`` is patched so the composed fence, the built-in protected set and
    the run directory all resolve against this tree and name nothing in the
    operator's real home.
    """
    home = tmp_path / "operator-home"
    for name in (
        ".claude",
        ".codex",
        ".ssh",
        ".agents",
        ".config/git",
        "public",
        ".local/bin",
    ):
        (home / name).mkdir(parents=True)
    for name in (".claude.json", ".gitconfig"):
        (home / name).write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def crew_home(operator_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew config home at the operator home's own reckon config.

    The run directory must lie inside a protected path for the fence to grant
    it writable, so ``RECKON_HOME`` is placed under the patched ``HOME`` at the
    ``.config/reckon`` the shipped default protects. ``crew_home()`` then
    resolves to ``<home>/.config/reckon/crew`` and ``run_dir`` to a path the
    fence is expected to re-bind writable.
    """
    config_home = operator_home / ".config" / "reckon"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, crew_home: Path) -> Path:
    """A throwaway repository carrying a plan."""
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
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt", "docs/plans/plan-a.html"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *args)
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _stopped_pointer(tmp_path: Path, repo: Path, run_id: str, *, backend: str) -> dict:
    """A cli run as a resume or a lane change finds it: gone, on this host.

    The manifest is placed in a report directory outside the run directory, so
    the run directory is only bound when the launch path passes it explicitly.
    The supervisor's exit record is the observation that licenses the second
    worker, so the launch under test is reached rather than refused.
    """
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = crew.reports_dir() / "node-a" / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: waiting\n", encoding="utf-8")
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text("the original dispatch prompt\n", encoding="utf-8")
    liveness._write_exit_record(run_id)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "backend": backend,
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
            "write_paths": ["allowed.txt"],
        },
    }
    crew._write_json(pointer_path(run_id), record)
    return record


def _binds(argv, target: Path | str) -> bool:
    """Whether ``argv`` re-binds ``target`` writable as ``--bind <t> <t>``."""
    words = [str(token) for token in argv]
    resolved = str(Path(target).resolve())
    return any(
        words[index] == "--bind"
        and words[index + 1 : index + 3] == [resolved, resolved]
        for index in range(len(words) - 2)
    )


def _cli_resolution(backend_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        backend=backend_name,
        launch="cli",
        backend_settings=dict(CONFIG["backends"][backend_name]),
        lane_gate={
            "state": "open",
            "gate_path": "fixture-gate.json",
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_detail": "backend declares no lane document to publish a config_path",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )


def _in_harness_resolution() -> SimpleNamespace:
    return SimpleNamespace(
        backend="native",
        launch="in-harness",
        backend_settings=dict(CONFIG["backends"]["native"]),
        lane_gate={
            "state": "open",
            "gate_path": "fixture-gate.json",
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_detail": "backend declares no lane document to publish a config_path",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )


def _stub_move_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the budget and ledger gates a lane change runs before composing."""
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "resolve_dispatch_ledger_root", lambda authority: authority
    )


def test_case_1_a_resumed_run_binds_its_run_directory(
    operator_home: Path,
    crew_home: Path,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume binds the run directory and the manifest's directory."""
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    run_id = "r-resume-binds"
    record = _stopped_pointer(tmp_path, repo, run_id, backend="alpha")

    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    argv = [str(token) for token in plan.argv]
    assert _binds(argv, crew.run_dir(run_id)), (
        "the resumed fence did not bind the run's own directory"
    )
    assert _binds(argv, Path(record["manifest_path"]).parent), (
        "the resumed fence did not bind the manifest's directory"
    )


def test_case_2_a_lane_changed_run_binds_its_run_directory(
    operator_home: Path,
    crew_home: Path,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane change to a cli backend binds both the run and manifest dirs."""
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    run_id = "r-lane-binds"
    record = _stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    resolution = _cli_resolution("beta")
    monkeypatch.setattr(dispatch_sessions_module, "plan_dispatch", lambda **kwargs: resolution)
    _stub_move_gates(monkeypatch)

    moved = dispatch_module.change_lane(
        run_id,
        "beta",
        "the lane is spent",
        config=CONFIG,
        launch=True,
        launcher=lambda *args, **kwargs: 4242,
    )

    argv = [str(token) for token in moved["argv"]]
    assert _binds(argv, crew.run_dir(run_id)), (
        "the lane-changed fence did not bind the run's own directory"
    )
    assert _binds(argv, Path(record["manifest_path"]).parent), (
        "the lane-changed fence did not bind the manifest's directory"
    )


def test_case_3_a_lane_change_to_in_harness_clears_fenced(
    operator_home: Path,
    crew_home: Path,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fenced CLI run moved to an in-harness backend reads fenced false."""
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    run_id = "r-lane-in-harness"
    _stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    assert crew.read_pointer(run_id)["fenced"] is True
    resolution = _in_harness_resolution()
    monkeypatch.setattr(dispatch_sessions_module, "plan_dispatch", lambda **kwargs: resolution)
    _stub_move_gates(monkeypatch)

    moved = dispatch_module.change_lane(
        run_id,
        "native",
        "the lane cannot follow to an in-harness backend",
        config=CONFIG,
        launch=True,
        launcher=lambda *args, **kwargs: 4242,
    )

    assert moved["fenced"] is False, (
        "the lane-changed pointer kept the prior attempt's fenced value"
    )
    assert crew.read_pointer(run_id)["fenced"] is False


if __name__ == "__main__":  # pragma: no cover - names the red-log recipe
    sys.stdout.write(
        f"{NEGATIVE_CONTROL_MUTATION}\n"
        "apply by reverting run_directory to Path(manifest_path).parent in "
        "resume_plan and change_lane and dropping their fenced assignments, "
        "then pytest this file under the scratch tree\n"
    )
