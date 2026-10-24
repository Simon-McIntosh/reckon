"""A run changes execution backend without changing the work it identifies."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import cli as cli_module
from reckon import crew, crew_run_commands
from reckon.crew import recovery

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
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    source_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
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
        spec_level="guided",
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
        crew_run_commands,
        "_resolved_flight",
        lambda flight_module, project, checkout_path, overrides: CONFIG,
    )


def test_redispatch_keeps_the_run_node_and_worktree(
    dispatched_run: tuple[Path, dict], monkeypatch
) -> None:
    repo, before = dispatched_run
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    signalled: list[tuple[int, str, str]] = []
    monkeypatch.setattr(
        dispatch_sessions_module,
        "process_alive",
        lambda pid: pid == before["pid"],
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "_signal_process_group",
        lambda pid, started, *, reason="", **kwargs: signalled.append(
            (pid, started, reason)
        ),
    )
    monkeypatch.setattr(dispatch_sessions_module, "_spawn", lambda *args, **kwargs: 42002)
    _resolve_config(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "resume",
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
    assert signalled == [(41001, "old-process", "lane-change")]
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


def test_redispatch_continues_a_stream_resolved_session_and_records_its_source(
    dispatched_run: tuple[Path, dict], monkeypatch
) -> None:
    _repo, before = dispatched_run
    stream = Path(before["log_path"])
    fixture = Path(__file__).parent / "fixtures" / "backends" / "codex-turn.jsonl"
    stream.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    pointer = {**before, "session_id": None}
    crew._write_json(crew.pointer_path(before["run_id"]), pointer)

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: pid == 41001)
    monkeypatch.setattr(dispatch_sessions_module, "_signal_process_group", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatch_sessions_module, "_spawn", lambda *args, **kwargs: 42002)
    _resolve_config(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "resume",
            "--run",
            before["run_id"],
            "--backend",
            "beta",
            "--reason",
            "the original lane is spent",
        ],
    )

    assert result.exit_code == 0, result.output
    after = crew.read_pointer(before["run_id"])
    change = after["lane_changes"][-1]
    assert change["session"] == "continued"
    assert change["session_id"] == "019ff509-8a60-7723-94fd-65942a6d8faa"
    assert change["session_source"] == "stream"
    assert after["session_id"] == change["session_id"]
    assert after["argv"][after["argv"].index("resume") + 1] == change["session_id"]


def test_resume_backend_override_reports_a_cross_harness_fresh_start(
    dispatched_run: tuple[Path, dict], monkeypatch
) -> None:
    _repo, before = dispatched_run
    pointer = dict(before)
    pointer.update({"phase": "blocked", "pid": None, "pid_start_time": None})
    crew._write_json(crew.pointer_path(before["run_id"]), pointer)
    # A blocked run whose end nothing observed is refused a lane change, so the
    # fixture records the supervisor's exit: an end observed without a pid, and
    # readable on a host that never launched the worker.
    (crew.run_dir(before["run_id"]) / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps({"run_id": before["run_id"], "exit_code": 0}), encoding="utf-8"
    )
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

    monkeypatch.setattr(dispatch_sessions_module, "_budget_verdict", held_beta)
    _resolve_config(monkeypatch)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "resume",
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
    """One operation serves every spelling of a lane move.

    The backend-move case of ``resume`` reaches ``change_lane``, and the folded
    ``redispatch`` name answers with a tombstone naming that successor rather
    than carrying a second implementation that could drift from it.
    """
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
    retired = runner.invoke(
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

    assert resume.exit_code == 0
    assert json.loads(retired.output)["successor"] == "crew resume --backend"
    assert retired.exit_code != 0
    assert calls == [("r-shared", "beta")]


# ── the figures a lane change names in the file its worker reads ────────────

FIRST_IMAGE = "docs/figures/plan-b/node-b/first.svg"
SECOND_IMAGE = "docs/figures/plan-b/node-b/second.svg"

# The heading a prompt carries when its lane is named its figures. Spelled here
# rather than imported so the count reads the text a worker is handed.
FIGURE_LISTING_MARKER = "Figures attached to this node"
FIGURE_LISTING_HEADING = (
    f"{FIGURE_LISTING_MARKER}. Open each path below with your own Read tool "
    "before you judge anything: a verdict formed without looking at them is "
    "not a verdict."
)


def _clive_lane(command: Path) -> dict:
    """A lane that declares image input and carries no flag to attach one."""
    return {
        "launch": "cli",
        "command": str(command),
        "sandbox": "worktree-full",
        "time_budget": "25m",
        "session_reuse": True,
        "input_modalities": ["text", "image"],
    }


def _stopped_figure_run(
    tmp_path: Path,
    *,
    lane: str,
    run_id: str | None = None,
    brief: str = "carry the two figures through the move",
    figures: tuple[str, ...] = (FIRST_IMAGE, SECOND_IMAGE),
    fence: str = "",
) -> tuple[str, Path]:
    """A clive run as a lane change finds it: ended on this host, its prompt
    file carrying the brief, an optional fence and the listing a fresh dispatch
    wrote, its figures on the node."""
    run_id = run_id or f"r-lane-figures-{lane}"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: waiting\n", encoding="utf-8")
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text(
        f"{brief}\n\n"
        + (f"{fence}\n\n" if fence else "")
        + f"{FIGURE_LISTING_HEADING}\n"
        + "".join(f"- {figure}\n" for figure in figures),
        encoding="utf-8",
    )
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps({"run_id": run_id, "exit_code": 0}), encoding="utf-8"
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "sample",
            "repo": str(tmp_path / "repo"),
            "worktree": str(tree),
            "launch": "cli",
            "backend": lane,
            "dialect": "clive",
            "role": "implement",
            "pid": None,
            "pid_start_time": None,
            "session_id": "session-on-clive",
            "session_harness": "clive",
            "attempt": 1,
            "base_sha": "HEAD",
            "log_path": str(directory / "stream.jsonl"),
            "manifest_path": str(manifest),
            "prompt_path": str(prompt_path),
            "phase": "working",
            "node": {
                "id": "node-b",
                "plan": "fixture",
                "section": "movement",
                "time_budget": "25m",
                "write_paths": ["package/target.py"],
                "images": list(figures),
            },
        },
    )
    return run_id, prompt_path


def _lane_change_resolution(config: dict, destination: str) -> SimpleNamespace:
    return SimpleNamespace(
        backend=destination,
        launch="cli",
        backend_settings=dict(config["backends"][destination]),
        lane_gate={
            "state": "open",
            "gate_path": "fixture-gate.json",
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_handled": False,
            "path_check_detail": "fixture",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )


def test_a_lane_change_names_the_figures_in_the_prompt_file_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane change composes from the run's prompt file, and a fresh dispatch
    has already written that file with its figure listing, so the composition
    strips that listing and names the current figures once. The worker reads
    the lane-change file, so both the restated fence and the naming have to be
    in it: a fence applied at the file write instead would land beside a prompt
    the composition never reached."""
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Both lanes are the one clive harness under two lane names: a dialect is
    # chosen from the command's stem, and a lane change requires a destination
    # whose name differs from the source's.
    stub = bindir / "clive"
    stub.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    stub.chmod(0o755)
    config = {
        "default_backend": "clive",
        "backends": {
            "clive": _clive_lane(stub),
            "clive-b": _clive_lane(stub),
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    run_id, original = _stopped_figure_run(tmp_path, lane="clive")

    monkeypatch.setattr(
        dispatch_sessions_module,
        "plan_dispatch",
        lambda **kwargs: _lane_change_resolution(config, "clive-b"),
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "resolve_dispatch_ledger_root",
        lambda authority: authority,
    )

    moved = dispatch_sessions_module.change_lane(
        run_id,
        "clive-b",
        "the lane is spent",
        config=config,
        launch=True,
        launcher=lambda *args, **kwargs: 43007,
    )

    written = Path(moved["prompt_path"]).read_text(encoding="utf-8")
    assert moved["prompt_path"] != str(original)
    assert "lane-change-2-prompt.txt" in moved["prompt_path"]
    assert "FENCE — TIME (resumed attempt)" in written
    assert written.count(FIRST_IMAGE) == 1, written
    assert written.count(SECOND_IMAGE) == 1, written


# ── a move to a lane that attaches the figures on its own flag ──────────────

CODEX_FIRST_IMAGE = "docs/figures/plan-c/node-c/first.png"
CODEX_SECOND_IMAGE = "docs/figures/plan-c/node-c/second.png"

# The brief and the fence a fresh dispatch had already written into the run's
# prompt file, ahead of the listing appended to it.
CODEX_BRIEF = "carry the two figures through the move onto the codex lane"
CODEX_ATTEMPT_FENCE = (
    "FENCE — TIME\n"
    "  Launched 2026-10-21T09:00:00Z UTC; deadline 2026-10-21T09:25:00Z — "
    "25m from launch."
)


def test_a_lane_change_onto_codex_attaches_the_figures_and_drops_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codex lane carries each figure on its own ``-i`` flag, so the listing
    a fresh dispatch left in the run's prompt file is a second copy of the same
    figures. The composed file the worker reads therefore has to lose the stale
    listing while keeping the brief and the fence restated for this attempt, and
    the flag has to carry exactly the figures the node declares."""
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", False)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Each lane is named by its command's stem, which is how the dialect — and
    # so whether a figure rides on argv or in the prompt — is chosen.
    clive = bindir / "clive"
    clive.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    clive.chmod(0o755)
    codex = bindir / "codex"
    codex.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    codex.chmod(0o755)

    def _lane(command: Path) -> dict:
        return {
            "launch": "cli",
            "command": str(command),
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "input_modalities": ["text", "image"],
        }

    config = {
        "default_backend": "clive",
        "backends": {"clive": _lane(clive), "codex": _lane(codex)},
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    run_id, _prompt = _stopped_figure_run(
        tmp_path,
        lane="clive",
        run_id="r-lane-onto-codex",
        brief=CODEX_BRIEF,
        figures=(CODEX_FIRST_IMAGE, CODEX_SECOND_IMAGE),
        fence=CODEX_ATTEMPT_FENCE,
    )

    monkeypatch.setattr(
        dispatch_sessions_module,
        "plan_dispatch",
        lambda **kwargs: _lane_change_resolution(config, "codex"),
    )
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_sessions_module,
        "resolve_dispatch_ledger_root",
        lambda authority: authority,
    )

    moved = dispatch_sessions_module.change_lane(
        run_id,
        "codex",
        "the clive lane is spent",
        config=config,
        launch=True,
        launcher=lambda *args, **kwargs: 43011,
    )

    written = Path(moved["prompt_path"]).read_text(encoding="utf-8")
    assert CODEX_BRIEF in written
    assert "FENCE — TIME (resumed attempt)" in written
    assert FIGURE_LISTING_HEADING not in written
    assert f"- {CODEX_FIRST_IMAGE}" not in written
    assert f"- {CODEX_SECOND_IMAGE}" not in written
    assert CODEX_FIRST_IMAGE not in written
    assert CODEX_SECOND_IMAGE not in written
    argv = moved["argv"]
    assert argv.count("-i") == 2, argv
    assert argv[argv.index("-i") + 1] == CODEX_FIRST_IMAGE
    second = argv.index("-i", argv.index("-i") + 1)
    assert argv[second + 1] == CODEX_SECOND_IMAGE
