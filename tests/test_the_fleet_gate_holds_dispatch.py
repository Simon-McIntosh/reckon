"""A shared crew gate holds launches while delivery can still be promoted."""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, crew
from tests import test_dispatch_holds_while_the_lane_is_paused as lane_tests
from tests import test_ledger as ledger_tests

pytest_plugins = (
    "tests.test_dispatch_names_its_backend",
    "tests.test_ledger",
)

dispatch = importlib.import_module("reckon.crew.dispatch")
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
            lane_gate=dispatch._dispatch_lane_gate({}),
        ),
    )
    with pytest.raises(dispatch.LanePaused) as held:
        dispatch.change_lane("run", "destination", "move", config={})
    assert held.value.gate["gate"] == "fleet"


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
    sources = [
        root / "reckon" / "cli.py",
        *sorted((root / "reckon" / "crew").glob("*.py")),
    ]
    launchers = []
    for path in sources:
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
            symbols = {
                node.id for node in ast.walk(function) if isinstance(node, ast.Name)
            }
            if (
                "_spawn" in names or "_start_supervisor" in names
            ) and "plan" in symbols:
                launchers.append((path.name, function.name, names))
    assert launchers
    for path, function, calls in launchers:
        if function == "supervised_launch":
            assert "_dispatch_fleet_gate" in calls
            continue
        assert {"plan_dispatch", "resume_plan", "change_lane"} & calls, (path, function)
    assert "_dispatch_fleet_gate" in Path(dispatch.__file__).read_text(encoding="utf-8")
