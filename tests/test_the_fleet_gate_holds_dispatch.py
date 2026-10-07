"""A shared crew gate holds launches while delivery can still be promoted."""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, crew, mcp
from tests import test_a_repair_hold_names_its_cause as repair_tests
from tests import test_dispatch_holds_while_the_lane_is_paused as lane_tests
from tests import test_ledger as ledger_tests
from tests import test_review_plan_command as review_plan_tests

pytest_plugins = (
    "tests.test_dispatch_names_its_backend",
    "tests.test_ledger",
)

dispatch = importlib.import_module("reckon.crew.dispatch")
recovery = importlib.import_module("reckon.crew.recovery")
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


@pytest.mark.parametrize(
    "starter", ["detached", "supervisor", "resumption", "legacy-supervisor"]
)
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
    elif starter == "resumption":
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
    else:
        monkeypatch.setattr(
            dispatch, "_supervisor_command", lambda *_a: calls.append("spawn") or 42
        )

        def start():
            return dispatch._peer_command([dispatch.SUPERVISOR_ENTRY, "--spec", "run"])

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


def test_review_reflex_holds_and_admits_under_the_fleet_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home, repo, worktrees = lane_tests.isolated_project.__wrapped__(
        tmp_path, monkeypatch
    )
    held_record = lane_tests._scoring_pointer(config_home, repo, "r-held-review")
    _pause()
    held = recovery.dispatch_review_for_run(
        held_record,
        config=lane_tests._review_config(None),
        launcher=lambda *_a, **_k: os.getpid(),
    )
    assert held["error"] == "lane-paused"
    assert held["lane_gate"]["gate"] == "fleet"
    assert REASON in held["reason"]
    assert worktrees == []
    _command("--open")
    open_record = lane_tests._scoring_pointer(config_home, repo, "r-open-review")
    admitted = recovery.dispatch_review_for_run(
        open_record,
        config=lane_tests._review_config(None),
        launcher=lambda *_a, **_k: os.getpid(),
    )
    assert admitted["dispatched"] is True
    assert worktrees


def test_repair_reflex_keeps_a_fleet_hold_pending_then_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home, repo, head = repair_tests.isolated_project.__wrapped__(
        tmp_path, monkeypatch
    )
    record = repair_tests._reviewed_pointer(config_home, repo, backend="alpha")
    repair_tests._store_review(head, repair_tests.FINDINGS)
    monkeypatch.setattr(
        resumption,
        "resume_plan",
        lambda *_a, **_k: SimpleNamespace(stdin_text="continue"),
    )
    monkeypatch.setattr(resumption, "record_resumption", lambda *_a, **_k: None)
    _pause()
    held = repair_tests._dispatch_repair(record, repair_tests.CONFIG_WITH_A_LANE)
    assert held["error"] == "lane-paused"
    assert held["lane_gate"]["gate"] == "fleet"
    assert REASON in held["reason"]
    stored = crew.read_pointer(repair_tests.RUN_ID)[recovery.REPAIR_DISPATCH_FIELD]
    assert stored["status"] == "lane-paused"
    _command("--open")
    admitted = repair_tests._dispatch_repair(
        crew.read_pointer(repair_tests.RUN_ID), repair_tests.CONFIG_WITH_A_LANE
    )
    assert admitted["resumed"] is True


def test_plan_review_waits_for_the_fleet_gate_and_then_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, repo, _ = review_plan_tests.project.__wrapped__(tmp_path, monkeypatch)
    subject = review_plan_tests._subject()
    _pause()
    held = recovery.dispatch_review_for_run(subject, config=review_plan_tests.CONFIG)
    assert held["error"] == "lane-paused"
    assert held["lane_gate"]["gate"] == "fleet"
    assert REASON in held["reason"]
    _command("--open")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    def prepare(*_args):
        path = tmp_path / "review-worktree"
        path.mkdir()
        return {"path": str(path), "base": "HEAD", "base_sha": head}

    monkeypatch.setattr(dispatch, "_create_worktree", prepare)
    admitted = recovery.dispatch_review_for_run(
        subject, config=review_plan_tests.CONFIG
    )
    assert admitted["dispatched"] is True


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


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _worker_argv_source(node: ast.AST, launch_names: set[str]) -> bool:
    return any(
        (
            isinstance(part, ast.Attribute)
            and part.attr == "argv"
            and isinstance(part.value, ast.Name)
            and part.value.id in launch_names
        )
        or (
            isinstance(part, ast.Subscript)
            and isinstance(part.value, ast.Name)
            and part.value.id in launch_names
            and isinstance(part.slice, ast.Constant)
            and part.slice.value == "argv"
        )
        for part in ast.walk(node)
    )


def _worker_popen_calls(function: ast.FunctionDef) -> list[ast.Call]:
    """Select processes fed a resolved worker argv, not tool or service argv."""
    launch_names = {
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
        if argument.annotation is not None
        and any(
            isinstance(part, (ast.Name, ast.Attribute))
            and (part.id if isinstance(part, ast.Name) else part.attr) == "LaunchPlan"
            for part in ast.walk(argument.annotation)
        )
    }
    if "plan" in {
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }:
        launch_names.add("plan")
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Subscript)
            and isinstance(node.value.slice, ast.Constant)
            and node.value.slice.value == "plan"
        ):
            launch_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    worker_argv_names = {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and _worker_argv_source(node.value, launch_names)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    selected = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or _call_name(node) != "Popen":
            continue
        command = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "args"),
            node.args[0] if node.args else None,
        )
        environment = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "env"),
            None,
        )
        worker_environment = environment is not None and any(
            isinstance(part, ast.Call)
            and _call_name(part) == "_worker_process_environment"
            for part in ast.walk(environment)
        )
        if (
            worker_environment
            or (command is not None and _worker_argv_source(command, launch_names))
            or (isinstance(command, ast.Name) and command.id in worker_argv_names)
        ):
            selected.append(node)
    return selected


def _direct_calls(function: ast.FunctionDef) -> list[ast.Call]:
    """Calls in this function's own body, excluding nested function bodies."""
    calls = []

    def visit(node: ast.AST) -> None:
        if node is not function and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            return
        if isinstance(node, ast.Call):
            calls.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(function)
    return calls


def _assert_primitive_launch_paths_reach_gate(root: Path) -> None:
    functions: dict[tuple[str, str], ast.FunctionDef] = {}
    for path in sorted((root / "reckon").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                functions[(relative, node.name)] = node
            elif isinstance(node, ast.ClassDef):
                for method in node.body:
                    if isinstance(method, ast.FunctionDef):
                        functions[(relative, f"{node.name}.{method.name}")] = method

    by_name: dict[str, list[tuple[str, str]]] = {}
    for key in functions:
        by_name.setdefault(key[1].rsplit(".", 1)[-1], []).append(key)
    callers: dict[tuple[str, str], list[tuple[tuple[str, str], ast.Call]]] = {}
    for caller, function in functions.items():
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            name = _call_name(call)
            local_method = (
                caller[0],
                f"{caller[1].split('.', 1)[0]}.{name}",
            )
            local_function = (caller[0], name)
            same_class_call = isinstance(call.func, ast.Name) or (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in {"self", "cls"}
            )
            target = (
                local_method
                if "." in caller[1] and same_class_call and local_method in functions
                else None
            )
            if target is None and local_function in functions:
                target = local_function
            if target is None and len(by_name.get(name, ())) == 1:
                target = by_name[name][0]
            if target is not None:
                callers.setdefault(target, []).append((caller, call))

    def gate_precedes(function: ast.FunctionDef, call: ast.Call) -> bool:
        return any(
            _call_name(candidate) == "_require_fleet_gate_open"
            and candidate.lineno < call.lineno
            for candidate in _direct_calls(function)
        )

    # The supervisor is a fresh interpreter. Its normal entry is the argv
    # written into the worker spec and launched by _start_supervisor; the
    # direct module command remains a separately checked caller.
    supervisor_argv = functions[("reckon/crew/dispatch.py", "_supervisor_argv")]
    supervisor_spec = functions[("reckon/crew/dispatch.py", "_supervisor_spec")]
    assert any(
        isinstance(node, ast.Constant) and node.value == "reckon.crew.supervisor_main"
        for node in ast.walk(supervisor_argv)
    )
    assert any(
        _call_name(call) == "_supervisor_argv"
        for call in ast.walk(supervisor_spec)
        if isinstance(call, ast.Call)
    )
    launcher = ("reckon/crew/dispatch.py", "_start_supervisor")
    launch_calls = [
        call
        for call in ast.walk(functions[launcher])
        if isinstance(call, ast.Call)
        and _call_name(call) in {"_spawn_through_fleet", "_spawn_detached_supervisor"}
    ]
    assert {_call_name(call) for call in launch_calls} == {
        "_spawn_through_fleet",
        "_spawn_detached_supervisor",
    }
    callers[("reckon/crew/supervisor_main.py", "main")] = [
        *callers.get(("reckon/crew/supervisor_main.py", "main"), []),
        *((launcher, call) for call in launch_calls),
    ]

    def require_guard(key: tuple[str, str], path: tuple[tuple[str, str], ...]) -> None:
        assert key not in path, f"cyclic ungated launch path: {(*path, key)}"
        incoming = callers.get(key, [])
        assert incoming, f"ungated worker launch root: {key}; path: {path}"
        for caller, call in incoming:
            if not gate_precedes(functions[caller], call):
                require_guard(caller, (*path, key))

    worker_popens = {
        key: calls
        for key, function in functions.items()
        if (calls := _worker_popen_calls(function))
    }
    assert worker_popens.keys() >= {
        ("reckon/crew/dispatch.py", "_spawn_detached_worker"),
        ("reckon/crew/dispatch.py", "_supervisor_spawn_worker"),
    }
    assert (
        "reckon/crew/dispatch.py",
        "_start_shadow_picker_selection",
    ) not in worker_popens
    assert (
        "reckon/crew/fleet_supervisor.py",
        "start_health_sampler",
    ) not in worker_popens
    direct_worker = ast.parse(
        "def launch(plan: LaunchPlan):\n    subprocess.Popen(plan.argv)\n"
    ).body[0]
    tool_probe = ast.parse(
        "def probe(probe: BudgetProbe):\n"
        "    subprocess.Popen(['git', 'status'])\n"
        "    subprocess.Popen(probe.argv)\n"
    ).body[0]
    assert len(_worker_popen_calls(direct_worker)) == 1
    assert _worker_popen_calls(tool_probe) == []
    for key, popens in worker_popens.items():
        for call in popens:
            if not gate_precedes(functions[key], call):
                require_guard(key, ())
    require_guard(("reckon/crew/dispatch.py", "_spawn_detached_supervisor"), ())
    fleet_gateway = ("reckon/crew/dispatch.py", "_spawn_through_fleet")
    fleet_calls = callers.get(fleet_gateway, [])
    assert fleet_calls
    watcher_calls = 0
    for caller, call in fleet_calls:
        request_id = call.args[1] if len(call.args) > 1 else None
        watcher_request = (
            isinstance(request_id, ast.JoinedStr)
            and bool(request_id.values)
            and isinstance(request_id.values[0], ast.Constant)
            and str(request_id.values[0].value).startswith("watch-")
        )
        if watcher_request:
            watcher_calls += 1
            continue
        if not gate_precedes(functions[caller], call):
            require_guard(caller, (fleet_gateway,))
    assert watcher_calls == 1


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
    _assert_primitive_launch_paths_reach_gate(root)
