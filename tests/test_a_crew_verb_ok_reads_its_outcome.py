"""Crew command verdicts follow the result each command publishes."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import (
    budget,
    cli,
    crew_dispatch_commands,
    crew_follow_commands,
    crew_run_commands,
    flight,
)
from reckon.crew import standing_suite


def _payload(result):
    return json.loads(result.output)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"ok": False, "run_id": "held"}, False),
        ({"ok": True, "finding": "gate diverged"}, True),
        ({"error": "refused"}, False),
        ({"skipped": [{"reason": "not eligible"}]}, True),
        ({"exit_status": None}, False),
        ({"exit_status": 1}, False),
        ({"exit_status": 0}, True),
        ({"ran": False}, False),
        ({"action": "hold", "backend": None}, False),
        ({"action": "refuse", "backend": None}, False),
        ({"action": "skipped", "backend": None}, True),
        ({"action": "fallback", "backend": "worker"}, True),
        ({"reviews_refused": [{"run_id": "review"}]}, False),
        ({"reviews_awaiting_lane": ["review"]}, True),
        ({"event": "watcher-live", "watcher_live": True}, True),
        ({"held": True, "held_backends": ["metered"]}, False),
        ({"held": True, "record": {"job_id": "123"}}, True),
        ({"drained": False, "unreconciled_runs": 2}, True),
        ({"dry_run": True, "projects": {"sample": {"skipped": "no ledger"}}}, True),
        ({"dry_run": False, "projects": {"sample": {"stopped": "differs"}}}, True),
        ({"run_id": "launched"}, True),
    ],
)
def test_result_shapes_have_a_single_verdict_rule(result, expected):
    assert cli._crew_result_ok(result) is expected


def test_observed_worker_status_does_not_change_the_read_verdict():
    assert (
        cli._crew_result_ok(
            {"run_id": "running", "exit_status": None}, observation=True
        )
        is True
    )


def test_crew_emit_sites_do_not_put_literal_success_beside_a_result():
    sources = [
        Path(module.__file__).read_text(encoding="utf-8")
        for module in (crew_dispatch_commands, crew_follow_commands, crew_run_commands)
    ]
    offenders = []
    for function in (node for source in sources for node in ast.parse(source).body):
        if not isinstance(function, ast.FunctionDef) or not function.name.startswith(
            "crew_"
        ):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Dict) or not any(
                key is None for key in node.keys
            ):
                continue
            if any(
                isinstance(key, ast.Constant)
                and key.value == "ok"
                and isinstance(value, ast.Constant)
                and value.value is True
                for key, value in zip(node.keys, node.values, strict=True)
            ):
                offenders.append(f"{function.name}:{node.lineno}")
    assert offenders == [], offenders


def test_preflight_hold_and_clear_result_have_matching_exit_codes(monkeypatch):
    monkeypatch.setattr(crew_dispatch_commands, "_dispatch_resolved_flight", lambda *args: {})
    monkeypatch.setattr(budget, "recorded_windows", lambda *args, **kwargs: {})
    monkeypatch.setattr(budget, "record_checks", lambda *args, **kwargs: [])
    report = {"held": True, "held_backends": ["metered"], "backends": []}
    monkeypatch.setattr(budget, "preflight", lambda *args, **kwargs: report)

    held = CliRunner().invoke(cli.main, ["crew", "preflight", "--project", "sample"])
    assert held.exit_code == 3
    assert _payload(held)["ok"] is False

    report = {"held": False, "held_backends": [], "backends": []}
    clear = CliRunner().invoke(cli.main, ["crew", "preflight", "--project", "sample"])
    assert clear.exit_code == 0
    assert _payload(clear)["ok"] is True


def test_suite_without_exit_status_is_not_reported_as_success(monkeypatch, tmp_path):
    monkeypatch.setattr(crew_run_commands, "_suite_project_root", lambda *args: tmp_path)
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config={})
    )
    monkeypatch.setattr(flight, "review_suite", lambda config: {"command": "pytest"})
    monkeypatch.setattr(standing_suite, "suite_runs_dir", lambda *args: tmp_path)
    monkeypatch.setattr(
        standing_suite,
        "run",
        lambda *args: {
            "exit_status": None,
            "over_budget": True,
            "log_path": "gate.log",
        },
    )
    monkeypatch.setattr(standing_suite, "record", lambda *args: None)

    result = CliRunner().invoke(
        cli.main, ["crew", "suite", "run", "--project", "sample"]
    )
    assert result.exit_code == 1
    assert _payload(result)["ok"] is False
