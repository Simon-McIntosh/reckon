"""The codex anchored arm: argv overrides, steering, and what the stream counted.

Codex cannot withdraw its own ``apply_patch``, so the arm is a treatment in two
halves — the argv that makes the anchored server callable, and the prompt line
that makes a worker reach for it — plus the stream fold that says afterwards
which path the edits actually took.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import dispatch_launch, runs
from reckon.crew.prompts import (
    ANCHORED_EDIT_HEADING,
    ANCHORED_EDIT_INSTRUCTION,
    with_anchored_edit_instruction,
)

# The package re-exports a `dispatch` *function*, so a plain submodule import
# would bind that instead of the module the composed prompt is built in.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "codex-anchored-edit-stream.jsonl"
)
OPERATOR_CONFIG = 'model = "operator"\napproval_policy = "never"\n'


@pytest.fixture
def launch_case(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    home = tmp_path / "operator"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(OPERATOR_CONFIG)
    (home / ".codex" / "auth.json").write_text('{"token": "operator"}\n')
    run = runs.run_dir("synthetic")
    run.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(_backends.tempfile, "gettempdir", lambda: str(scratch))
    monkeypatch.setattr(
        _backends, "fence_argv", lambda argv, **kwargs: ["fence", *argv]
    )
    monkeypatch.setattr(_backends, "seed_write_lock_namespace", lambda home: None)

    def compose(command="codex", enabled=True, **overrides):
        options = {
            "backend_name": "synthetic",
            "backend": {
                "launch": "cli",
                "command": command,
                "sandbox": "worktree-full",
                **({"anchored_edit": enabled} if enabled is not None else {}),
            },
            "prompt": "node prompt",
            "worktree": worktree,
            "manifest_path": run / "manifest.md",
            "writable_directories": [delivery],
            "fence_home": home,
            "fence_config": {},
        }
        options.update(overrides)
        return _backends.launch_plan(**options)

    compose.worktree = worktree
    compose.delivery = delivery
    compose.home = home
    return compose, run


def _roots(compose):
    return [
        str(root)
        for root in _backends.fence_writable_roots((compose.delivery, compose.worktree))
    ]


def _declared(argv):
    """The ``-c`` overrides as a key → raw value mapping."""
    return {
        argv[index + 1].split("=", 1)[0]: argv[index + 1].split("=", 1)[1]
        for index, word in enumerate(argv)
        if word == "-c"
    }


@pytest.mark.parametrize("enabled", [False, None])
def test_absent_key_leaves_the_codex_argv_byte_identical(launch_case, enabled):
    compose, _ = launch_case
    expected = [
        "fence",
        "codex",
        "exec",
        "--json",
        "-C",
        str(compose.worktree),
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]
    plan = compose(enabled=enabled)
    assert json.dumps(plan.argv).encode() == json.dumps(expected).encode()
    assert plan.anchored_edit is False
    assert plan.anchored_edit_reason == "disabled"


def test_the_key_carries_the_server_its_roots_and_the_approval_mode(launch_case):
    compose, _ = launch_case
    plan = compose()
    declared = _declared(plan.argv)
    server = "mcp_servers.anchored"
    assert json.loads(declared[f"{server}.command"]) == sys.executable
    assert json.loads(declared[f"{server}.args"]) == [
        "-m",
        "reckon.anchored_edit",
        *(word for root in _roots(compose) for word in ("--root", root)),
    ]
    # The worktree the worker edits is among the server's roots; a server with
    # no root refuses every path, so this is what makes the arm usable at all.
    assert str(compose.worktree) in json.loads(declared[f"{server}.args"])
    assert json.loads(declared[f"{server}.default_tools_approval_mode"]) == "approve"
    assert plan.anchored_edit is True
    assert plan.anchored_edit_reason is None


def _spec(plan, run, compose, monkeypatch):
    monkeypatch.setattr(
        dispatch_launch,
        "_persisted_worker_environment",
        lambda environment, **kwargs: dict(environment),
    )
    return dispatch_launch._supervisor_spec(
        run_id=run.name,
        run_directory=run,
        repo_root=compose.worktree,
        worktree=compose.worktree,
        plan=plan,
        fenced=plan.argv[0] == "fence",
        prompt_path=run / "prompt.txt",
        log_path=run / "stream.jsonl",
        stderr_path=run / "stderr.log",
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_the_launch_fact_reaches_the_run_record(launch_case, monkeypatch, enabled):
    compose, run = launch_case
    plan = compose(enabled=enabled)
    assert plan.anchored_edit is enabled
    spec = _spec(plan, run, compose, monkeypatch)
    assert spec["anchored_edit"] is enabled
    payload = dispatch_launch._worker_record_payload(
        spec, pid=0, attempt=1, launched_at="", registered_at=""
    )
    assert payload["anchored_edit"] is enabled
    assert payload["anchored_edit_reason"] == (None if enabled else "disabled")


def test_no_config_toml_is_written(launch_case):
    compose, run = launch_case
    compose()
    # A fenced run adopts a codex home, and that home's config.toml is the
    # operator's own file copied verbatim. The overrides are argv arguments to
    # one process, so nothing here may rewrite it.
    seeded = run / "codex-home" / "config.toml"
    assert seeded.read_text() == OPERATOR_CONFIG
    assert (compose.home / ".codex" / "config.toml").read_text() == OPERATOR_CONFIG
    written = sorted(path for path in run.rglob("config.toml"))
    assert written == [seeded]


def test_an_unfenced_run_names_the_server_without_writing_a_home(launch_case):
    compose, run = launch_case
    plan = compose(fence=False)
    assert "mcp_servers.anchored.args" in _declared(plan.argv)
    assert not (run / "codex-home").exists()


def _observe(path, *, state=None):
    return _backends.observe_stream(
        backend_name="codex",
        backend={"command": "codex"},
        lines=path.read_text().splitlines(),
        state=state,
    )


def test_the_stream_counts_anchored_calls_apart_from_file_change_items():
    # The fixture is read in place: it is the prototype run's own stream, and a
    # copy under tests/ would let the parser drift from the evidence.
    observation = _observe(FIXTURE)
    assert observation.tools == {
        "anchored": {
            "calls": 4,
            "read_anchored": 1,
            "edit_anchored": 3,
            "failed": 1,
        },
        "file_change": {"items": 0},
    }
    assert observation.as_dict()["tools"]["anchored"]["calls"] == 4


def test_a_file_change_item_is_counted_as_the_harness_path(tmp_path):
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "mcp_tool_call",
                "server": "anchored",
                "tool": "edit_anchored",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "item_2", "type": "file_change", "status": "completed"},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_3",
                "type": "mcp_tool_call",
                "server": "other",
                "tool": "search",
                "status": "completed",
            },
        },
        {"type": "turn.completed", "usage": {}},
    ]
    path = tmp_path / "stream.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    observation = _observe(path)
    assert observation.tools["anchored"] == {
        "calls": 1,
        "read_anchored": 0,
        "edit_anchored": 1,
        "failed": 0,
    }
    # A call to some other server is not an edit by either path.
    assert observation.tools["file_change"] == {"items": 1}


def test_an_incremental_read_reports_the_whole_streams_counts():
    first = _observe(FIXTURE)
    second = _backends.observe_stream(
        backend_name="codex",
        backend={"command": "codex"},
        lines=[],
        state=first.stream_state,
    )
    assert second.tools == first.tools


def test_a_started_item_does_not_double_count_its_completed_twin(tmp_path):
    call = {
        "id": "item_7",
        "type": "mcp_tool_call",
        "server": "anchored",
        "tool": "edit_anchored",
        "status": "in_progress",
    }
    events = [
        {"type": "item.started", "item": call},
        {"type": "item.completed", "item": {**call, "status": "completed"}},
    ]
    path = tmp_path / "stream.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    assert _observe(path).tools["anchored"]["calls"] == 1


def test_the_instruction_is_stated_only_for_an_enabled_codex_launch():
    prompt = "BASE PROMPT"
    treated = with_anchored_edit_instruction(
        prompt, {"command": "codex", "anchored_edit": True}
    )
    assert treated.startswith(prompt)
    assert ANCHORED_EDIT_HEADING in treated
    assert ANCHORED_EDIT_INSTRUCTION in treated
    assert (
        with_anchored_edit_instruction(
            prompt, {"command": "codex", "anchored_edit": False}
        )
        == prompt
    )
    assert with_anchored_edit_instruction(prompt, {"command": "codex"}) == prompt
    # A Claude-dialect launch has its built-in edits withdrawn instead, so it is
    # steered by what it cannot do rather than told about apply_patch.
    assert (
        with_anchored_edit_instruction(
            prompt, {"command": "claude", "anchored_edit": True}
        )
        == prompt
    )


def test_dispatch_composes_through_the_instruction():
    assert (
        dispatch_module._with_anchored_edit_instruction
        is with_anchored_edit_instruction
    )
