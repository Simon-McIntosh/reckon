"""A run changes execution backend without changing the work it identifies."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "first-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "beta": {
            "launch": "cli",
            "command": "codex",
            "model": "second-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
        "native": {
            "launch": "in-harness",
            "model": "embedded-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def dispatched_run(tmp_path: Path, monkeypatch) -> tuple[Path, dict]:
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    source_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="movement">Backend movement</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    node = crew.TaskNode(
        id="lane-change-node",
        goal="keep one run identity while its execution backend changes",
        plan="fixture",
        section="movement",
        done_when="pytest reports the backend movement assertions passing",
        write_paths=["package/target.py"],
        time_budget="20m",
        manifest_path=str(config_home / "reports" / "worker.md"),
    )
    record = crew.dispatch(
        node=node,
        project="sample",
        repo=repo,
        config=CONFIG,
        session="coordinator-session",
        launcher=lambda *args, **kwargs: 41001,
    )
    pointer = crew.read_pointer(record["run_id"])
    pointer.update(
        {
            "phase": "working",
            "session_id": "session-from-alpha",
            "pid": 41001,
            "pid_start_time": "old-process",
        }
    )
    crew._write_json(crew.pointer_path(record["run_id"]), pointer)
    assert crew.pointer_path(record["run_id"]).is_relative_to(config_home)
    assert not (real_live / f"{record['run_id']}.json").exists()
    return repo, pointer


def _resolve_config(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "_resolved_flight",
        lambda flight_module, project, checkout_path, overrides: CONFIG,
    )


def test_redispatch_keeps_the_run_node_and_worktree(
    dispatched_run: tuple[Path, dict], monkeypatch
) -> None:
    repo, before = dispatched_run
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    signalled: list[tuple[int, str]] = []
    monkeypatch.setattr(
        dispatch_module,
        "process_alive",
        lambda pid: pid == before["pid"],
    )
    monkeypatch.setattr(
        dispatch_module,
        "_signal_process_group",
        lambda pid, started: signalled.append((pid, started)),
    )
    monkeypatch.setattr(dispatch_module, "_spawn", lambda *args, **kwargs: 42002)
    _resolve_config(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "redispatch",
            "--run",
            before["run_id"],
            "--backend",
            "beta",
            "--reason",
            "the original lane is spent",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    after = crew.read_pointer(before["run_id"])
    assert payload["run_id"] == after["run_id"] == before["run_id"]
    assert after["node"]["id"] == before["node"]["id"]
    assert after["worktree"] == before["worktree"]
    assert Path(after["worktree"]).is_dir()
    assert signalled == [(41001, "old-process")]
    assert after["backend"] == "beta"
    assert after["lane_changes"][-1]["from_backend"] == "alpha"
    assert after["lane_changes"][-1]["to_backend"] == "beta"
    assert after["lane_changes"][-1]["reason"] == "the original lane is spent"
    assert after["lane_changes"][-1]["session"] == "continued"
    assert after["lineage"] == {
        "kind": "lane-change",
        "attempt": 2,
        "root_run_id": before["run_id"],
        "lanes": after["lane_changes"],
    }
    assert after["session_id"] == "session-from-alpha"
    assert "session-from-alpha" in after["argv"]
    assert (
        subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.count(f"worktree {before['worktree']}")
        == 1
    )


def test_resume_backend_override_reports_a_cross_harness_fresh_start(
    dispatched_run: tuple[Path, dict], monkeypatch
) -> None:
    _repo, before = dispatched_run
    pointer = dict(before)
    pointer.update({"phase": "blocked", "pid": None, "pid_start_time": None})
    crew._write_json(crew.pointer_path(before["run_id"]), pointer)
    _resolve_config(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "resume",
            "--run",
            before["run_id"],
            "--advice",
            "continue from the retained worktree",
            "--backend",
            "native",
            "--reason",
            "the process backend cannot serve another turn",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    after = crew.read_pointer(before["run_id"])
    assert payload["lane_change"]["session"] == "fresh"
    assert "fresh" in payload["lane_change"]["detail"]
    assert after["run_id"] == before["run_id"]
    assert after["node"]["id"] == before["node"]["id"]
    assert after["worktree"] == before["worktree"]
    assert after["backend"] == "native"
    assert after["launch"] == "in-harness"
    assert after["session_id"] is None
    assert after["directive"]["attach_with"].startswith(
        f"reckon crew attach --run {before['run_id']}"
    )


def test_a_held_destination_lane_is_named_and_changes_nothing(
    dispatched_run: tuple[Path, dict], monkeypatch
) -> None:
    _repo, before = dispatched_run
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    original_verdict = dispatch_module._budget_verdict

    def held_beta(*, backend_name, **kwargs):
        if backend_name == "beta":
            return {
                "backend": "beta",
                "held": True,
                "reason": "five-hour window is recovering",
                "state": {"utilisation_pct": 99.0, "resets_at": "later"},
            }
        return original_verdict(backend_name=backend_name, **kwargs)

    monkeypatch.setattr(dispatch_module, "_budget_verdict", held_beta)
    _resolve_config(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "redispatch",
            "--run",
            before["run_id"],
            "--backend",
            "beta",
            "--reason",
            "move away from the current lane",
        ],
    )

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["error"] == "budget-hold"
    assert payload["hold"]["backend"] == "beta"
    assert "recovering" in payload["detail"]
    assert crew.read_pointer(before["run_id"]) == before


def test_both_commands_call_the_shared_lane_change_operation(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def change(run_id, backend, reason, **kwargs):
        calls.append((run_id, backend))
        return {
            "run_id": run_id,
            "lane_change": {
                "from_backend": "alpha",
                "to_backend": backend,
                "reason": reason,
                "session": "continued",
                "detail": "session continued",
            },
        }

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch_module, "change_lane", change)
    monkeypatch.setattr(crew, "read_pointer", lambda run_id: {"project": "sample"})
    _resolve_config(monkeypatch)
    runner = CliRunner()

    resume = runner.invoke(
        cli_module.main,
        [
            "crew",
            "resume",
            "--run",
            "r-shared",
            "--advice",
            "continue",
            "--backend",
            "beta",
            "--reason",
            "quota exhausted",
            "--print-only",
        ],
    )
    redispatch = runner.invoke(
        cli_module.main,
        [
            "crew",
            "redispatch",
            "--run",
            "r-shared",
            "--backend",
            "beta",
            "--reason",
            "quota exhausted",
            "--print-only",
        ],
    )

    assert resume.exit_code == redispatch.exit_code == 0
    assert calls == [("r-shared", "beta"), ("r-shared", "beta")]
