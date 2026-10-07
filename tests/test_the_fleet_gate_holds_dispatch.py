"""A shared crew gate holds launches while delivery can still be promoted."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, crew, mcp
from tests import test_dispatch_holds_while_the_lane_is_paused as lane_tests
from tests import test_ledger as ledger_tests

pytest_plugins = (
    "tests.test_dispatch_names_its_backend",
    "tests.test_ledger",
)

dispatch = importlib.import_module("reckon.crew.dispatch")
resumption = importlib.import_module("reckon.crew.resumption")
REASON = "move workers to another allocation"


def _command(*arguments: str) -> tuple[dict, int]:
    result = CliRunner().invoke(cli.main, ["crew", "gate", *arguments])
    return json.loads(result.output), result.exit_code


def _pause() -> dict:
    payload, code = _command("--pause", REASON)
    assert code == 0
    return payload


def test_gate_command_prints_both_documents_and_dispatch_waits(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = _pause()
    assert paused["before"] == {"paused": False, "reason": None}
    assert paused["after"] == {"paused": True, "reason": REASON}
    assert json.loads(Path(paused["gate_path"]).read_text()) == paused["after"]

    payload, result = lane_tests._cli(
        dispatch_repo,
        monkeypatch,
        node="held-by-fleet",
        config=lane_tests._config(),
        extra=["--local"],
    )
    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert payload["reason"] == REASON
    assert payload["lane_gate"]["gate"] == "fleet"
    assert payload["lane_gate"]["gate_path"] == paused["gate_path"]
    assert "fleet gate" in payload["detail"]

    opened, code = _command("--open")
    assert code == 0
    assert opened["before"] == paused["after"]
    assert opened["after"] == {"paused": False, "reason": None}
    admitted, result = lane_tests._cli(
        dispatch_repo,
        monkeypatch,
        node="admitted-by-fleet",
        config=lane_tests._config(),
        extra=["--local"],
        dry_run=True,
    )
    assert result.exit_code == 0
    assert admitted["lane_gate"]["state"] == "not-declared"


def test_dispatch_rejects_a_paused_fleet_document(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = dispatch.fleet_gate_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"paused": True, "reason": REASON}), encoding="utf-8")
    payload, result = lane_tests._cli(
        dispatch_repo,
        monkeypatch,
        node="paused-document",
        config=lane_tests._config(),
        extra=["--local"],
    )
    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert payload["lane_gate"]["gate"] == "fleet"
    assert payload["reason"] == REASON


def test_backend_gate_still_holds_when_fleet_gate_is_open(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _command("--open")
    backend_gate = lane_tests._gate_file(tmp_path)
    payload, result = lane_tests._cli(
        dispatch_repo,
        monkeypatch,
        node="held-by-backend",
        config=lane_tests._config(gate_document=backend_gate),
        extra=["--local"],
    )
    assert result.exit_code == 75
    assert payload["lane_gate"]["gate_path"] == str(backend_gate)
    assert payload["reason"] == lane_tests.GATE_REASON


def test_picker_cannot_replace_a_fleet_hold_with_its_own_decision(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pause()
    config = lane_tests._config()
    config["routing"] = {"picker": "route"}

    def fail_if_called(**_kwargs):
        raise AssertionError("the shared gate must hold before picking a lane")

    monkeypatch.setattr(dispatch, "dispatch_picker_selection", fail_if_called)
    payload, result = lane_tests._cli(
        dispatch_repo,
        monkeypatch,
        node="held-before-picker",
        config=config,
    )
    assert result.exit_code == 75
    assert payload["error"] == "lane-paused"
    assert payload["lane_gate"]["gate"] == "fleet"


def test_resume_checks_the_shared_gate_before_building_a_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    _pause()
    monkeypatch.setattr(dispatch, "read_pointer", lambda _run: {"launch": "cli"})
    monkeypatch.setattr(dispatch, "record_process_alive", lambda *_a: False)
    monkeypatch.setattr(
        dispatch,
        "_current_harness_session",
        lambda *_a, **_k: {"resolved": True, "session_id": "s"},
    )
    monkeypatch.setattr(dispatch, "_backend_settings", lambda *_a: {})
    monkeypatch.setattr(dispatch, "_carry_declared_placement", lambda *_a: None)
    monkeypatch.setattr(dispatch, "project_mount_repository", lambda *_a: None)
    with pytest.raises(dispatch.LanePaused) as held:
        dispatch.resume_plan("run", "continue", config={})
    assert held.value.gate["reason"] == REASON
    assert held.value.gate["gate"] == "fleet"


def test_resume_keeps_the_backend_gate_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    backend_gate = lane_tests._gate_file(tmp_path)
    record = {"launch": "cli", "backend": "alpha"}
    monkeypatch.setattr(dispatch, "read_pointer", lambda _run: record)
    monkeypatch.setattr(dispatch, "record_process_alive", lambda *_a: False)
    monkeypatch.setattr(
        dispatch,
        "_current_harness_session",
        lambda *_a, **_k: {"resolved": True, "session_id": "s"},
    )
    monkeypatch.setattr(dispatch, "_backend_settings", lambda *_a: {})
    monkeypatch.setattr(dispatch, "_carry_declared_placement", lambda *_a: None)
    monkeypatch.setattr(dispatch, "project_mount_repository", lambda *_a: None)
    with pytest.raises(dispatch.LanePaused) as held:
        dispatch.resume_plan(
            "run",
            "continue",
            config={"backends": {"alpha": {"gate_document": str(backend_gate)}}},
        )
    assert held.value.gate["gate_path"] == str(backend_gate)
    assert held.value.gate["reason"] == lane_tests.GATE_REASON


def test_redispatch_checks_the_shared_gate_before_stopping_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    _pause()
    monkeypatch.setattr(
        dispatch,
        "read_pointer",
        lambda _run: {"backend": "source", "repo": str(tmp_path), "node": {}},
    )
    monkeypatch.setattr(
        dispatch,
        "_recorded_task_node",
        lambda _record: SimpleNamespace(requires_decisions=[]),
    )
    monkeypatch.setattr(
        dispatch,
        "plan_dispatch",
        lambda **_kwargs: SimpleNamespace(
            validation=SimpleNamespace(ok=True),
            backend_settings={},
            lane_gate=dispatch._dispatch_lane_gate({}),
        ),
    )
    with pytest.raises(dispatch.LanePaused) as held:
        dispatch.change_lane("run", "destination", "move", config={})
    assert held.value.gate["gate"] == "fleet"


@pytest.mark.parametrize("starter", ["detached", "supervisor", "resumption"])
def test_worker_spawn_boundaries_hold_and_admit(
    starter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    directory = tmp_path / "run"
    calls = []
    plan = object()
    if starter == "detached":
        monkeypatch.setattr(
            dispatch,
            "_spawn_detached_worker",
            lambda *_a, **_k: calls.append("spawn") or 42,
        )

        def start():
            return dispatch._spawn(
                plan,
                log_path=directory / "probe.jsonl",
                stderr_path=directory / "probe.stderr.log",
                prompt_path=directory / "prompt.txt",
            )
    elif starter == "supervisor":
        monkeypatch.setattr(dispatch, "_read_fleet_record", lambda: None)
        monkeypatch.setattr(dispatch, "_supervisor_argv", lambda **_k: ["worker"])
        monkeypatch.setattr(
            dispatch,
            "_spawn_detached_supervisor",
            lambda *_a: calls.append("spawn") or 42,
        )
        monkeypatch.setattr(dispatch, "_confirm_supervisor_survived", lambda *_a: None)

        def start():
            return dispatch._start_supervisor(directory / "spec.json", directory, "run")
    else:
        monkeypatch.setattr(resumption, "read_pointer", lambda _run: {})
        monkeypatch.setattr(
            dispatch, "supervised_launch", lambda *_a, **_k: calls.append("spawn") or 42
        )

        def start():
            return resumption._spawn(
                plan,
                log_path=directory / "resume-1.jsonl",
                stderr_path=directory / "resume-1.stderr.log",
                prompt_path=directory / "prompt.txt",
            )

    _pause()
    with pytest.raises(dispatch.LanePaused) as held:
        start()
    assert held.value.gate["gate"] == "fleet"
    assert held.value.gate["reason"] == REASON
    assert calls == []
    _command("--open")
    assert start() == 42
    assert calls == ["spawn"]


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_recovery_launch_surfaces_hold_and_admit(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    plan = SimpleNamespace(stdin_text="continue", as_dict=dict)
    starts = []
    monkeypatch.setattr(crew, "read_pointer", lambda _run: {"project": ""})
    monkeypatch.setattr(crew, "resume_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(crew, "run_dir", lambda _run: tmp_path)
    monkeypatch.setattr(crew, "_manifest_mtime_ns", lambda _path: 0)
    monkeypatch.setattr(crew, "_utc_now", lambda: "2026-10-07T00:00:00Z")
    monkeypatch.setattr(crew, "_spawn", lambda *_a, **_k: starts.append("spawn") or 42)
    monkeypatch.setattr(crew, "record_resumption", lambda *_a, **_k: None)
    monkeypatch.setattr(
        mcp.resumption_module,
        "resolve_session",
        lambda *_a, **_k: {"session_id": "session", "source": "pointer"},
    )

    def invoke():
        if surface == "cli":
            result = CliRunner().invoke(
                cli.main, ["crew", "resume", "--run", "run", "--advice", "continue"]
            )
            return json.loads(result.output), result.exit_code
        payload = mcp._crew_recover("resume", run_id="run", advice="continue")
        return payload, 0 if payload["ok"] else 75

    _pause()
    held, code = invoke()
    assert code == 75
    assert held["error"] == "lane-paused"
    assert held["lane_gate"]["gate"] == "fleet"
    assert held["reason"] == REASON
    assert starts == []
    _command("--open")
    admitted, code = invoke()
    assert code == 0
    assert admitted["pid"] == 42
    assert starts == ["spawn"]


def test_automatic_resumption_reports_the_fleet_hold_and_admits_when_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = {"run_id": "run", "launch": "cli"}
    monkeypatch.setattr(resumption, "record_process_alive", lambda *_a: False)
    monkeypatch.setattr(
        resumption, "_readonly_budget_verdict", lambda *_a, **_k: {"held": False}
    )
    monkeypatch.setattr(resumption, "_backend_settings", lambda *_a, **_k: {})
    monkeypatch.setattr(resumption, "resume_window_refusal", lambda *_a, **_k: None)
    _pause()
    held = resumption._launcher_refusal(record, config={})
    assert isinstance(held, dispatch.LanePaused)
    row = resumption._refusal_entry({"run_id": "run"}, held)
    assert row["reason"] == "lane-paused"
    assert row["lane_gate"]["gate"] == "fleet"
    assert row["lane_gate"]["reason"] == REASON
    _command("--open")
    assert resumption._launcher_refusal(record, config={}) is None


def test_automatic_resumption_checks_the_gate_before_its_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        resumption,
        "resume_plan",
        lambda *_a, **_k: SimpleNamespace(stdin_text="continue"),
    )
    monkeypatch.setattr(resumption, "run_dir", lambda _run: tmp_path)
    monkeypatch.setattr(resumption, "_manifest_mtime_ns", lambda _path: 0)
    monkeypatch.setattr(resumption, "record_resumption", lambda *_a, **_k: None)
    starts = []

    def launch(*_args, **_kwargs):
        starts.append("spawn")
        return 42

    _pause()
    with pytest.raises(dispatch.LanePaused) as held:
        resumption._resume("run", {}, config={}, launcher=launch)
    assert held.value.gate["gate"] == "fleet"
    assert held.value.gate["reason"] == REASON
    assert starts == []
    _command("--open")
    assert resumption._resume("run", {}, config={}, launcher=launch)["pid"] == 42
    assert starts == ["spawn"]


def test_completion_and_promotion_continue_during_a_pause(home, repo) -> None:
    record = ledger_tests._dispatch(repo, fixture="codex-turn.jsonl")
    ledger_tests._deliver(record)
    work = ledger_tests._commit_work(repo)
    ledger_tests._init_ledger(repo)
    _pause()
    result = crew.complete(
        record["run_id"],
        gate="passed",
        commits=[work],
        review_waiver=ledger_tests._UNREVIEWED_PROMOTION_WAIVED,
    )
    assert result["record"]["run_id"] == record["run_id"]
    assert result["pointer_removed"] is True


def test_launch_callers_reach_the_shared_gate() -> None:
    """Search launch calls so a new worker entry must lead through a gate."""
    root = Path(__file__).parents[1]
    launchers = set()
    guarded_spawns = set()
    for path in sorted((root / "reckon").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in (
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ):
            calls = [
                node.func for node in ast.walk(function) if isinstance(node, ast.Call)
            ]
            names = {
                call.id
                if isinstance(call, ast.Name)
                else call.attr
                if isinstance(call, ast.Attribute)
                else ""
                for call in calls
            }
            relative = path.relative_to(root).as_posix()
            if (relative, function.name) in {
                ("reckon/crew/dispatch.py", "_spawn"),
                ("reckon/crew/dispatch.py", "_start_supervisor"),
                ("reckon/crew/resumption.py", "_spawn"),
            }:
                guarded_spawns.add((relative, function.name))
                assert "_require_fleet_gate_open" in names, (relative, function.name)
            starts_worker = any(
                (
                    isinstance(call, ast.Name)
                    and call.id in {"_spawn", "_start_supervisor"}
                )
                or (
                    isinstance(call, ast.Attribute)
                    and call.attr in {"_spawn", "_start_supervisor"}
                    and not (
                        relative == "reckon/crew/session_host.py"
                        and isinstance(call.value, ast.Name)
                        and call.value.id == "self"
                    )
                )
                for call in calls
            )
            if starts_worker:
                launchers.add((relative, function.name))
                assert "_require_fleet_gate_open" in names, (relative, function.name)
    assert guarded_spawns == {
        ("reckon/crew/dispatch.py", "_spawn"),
        ("reckon/crew/dispatch.py", "_start_supervisor"),
        ("reckon/crew/resumption.py", "_spawn"),
    }
    assert launchers >= {
        ("reckon/crew/dispatch.py", "dispatch"),
        ("reckon/crew/dispatch.py", "supervised_launch"),
        ("reckon/crew/resumption.py", "_resume"),
        ("reckon/cli.py", "crew_resume"),
        ("reckon/mcp.py", "_crew_recover"),
    }
