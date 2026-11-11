"""The codex anchored arm: argv overrides, steering, and what the stream counted.

Codex cannot withdraw its own ``apply_patch``, so the arm is a treatment in two
halves — the argv that makes the anchored server callable, and the prompt line
that makes a worker reach for it — plus the stream fold that says afterwards
which path the edits actually took.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import _backends, capabilities, crew
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


PROJECT = "sample"
PLAN = "plan-a"


def _git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def dispatch_case(tmp_path, monkeypatch):
    """A throwaway mount, a stubbed fleet worktree, and two codex lanes.

    ``treated`` carries the key; ``plain`` omits it, so one dispatch path is
    shown to compose the instruction for the key's lane and not for the other.
    """
    home = tmp_path / "operator-home"
    for name in (
        ".claude",
        ".codex",
        ".ssh",
        ".agents",
        ".config/reckon",
        ".config/git",
        "public",
        ".local/bin",
    ):
        (home / name).mkdir(parents=True, exist_ok=True)
    for name in (".claude.json", ".gitconfig"):
        (home / name).write_text("", encoding="utf-8")
    (home / ".codex" / "config.toml").write_text(OPERATOR_CONFIG)
    (home / ".codex" / "auth.json").write_text('{"token": "operator"}\n')
    monkeypatch.setenv("HOME", str(home))

    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("codex", "claude", "clive"):
        stub = bindir / name
        stub.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
        stub.chmod(0o755)

    def lane(anchored_edit):
        settings = {
            "launch": "cli",
            "command": str(bindir / "codex"),
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        }
        if anchored_edit is not None:
            settings["anchored_edit"] = anchored_edit
        return settings

    config = {
        "default_backend": "treated",
        "local_backend": "treated",
        "backends": {
            "treated": lane(True),
            "plain": lane(None),
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }

    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / f"{PLAN}.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<h2 id="s5">A codex worker is steered to the anchored tool</h2>',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", f"docs/plans/{PLAN}.html"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *args)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    base_sha = _git(root, "rev-parse", "HEAD")

    def prepare_worktree(_repo, session, node, base):
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", "--force", str(path), base_sha],
            cwd=root,
            check=True,
            capture_output=True,
        )
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config, root, config_home


class _Recorder:
    """Stands in for the spawn, recording the plan the launcher would run."""

    def __init__(self):
        self.argv = []
        self.stdin_text = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path):
        self.argv.append([str(token) for token in plan.argv])
        self.stdin_text.append(str(plan.stdin_text))
        return os.getpid()


def _node(config_home, run_id):
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    return crew.TaskNode(
        id="anchored-node",
        goal="steer a codex worker to the anchored tool",
        plan=PLAN,
        section="§5",
        done_when="tests/test_anchored_edit_codex_arm.py passes",
        write_paths=["allowed.txt"],
        time_budget="25m",
        manifest_path=str(manifest),
        spec_level="guided",
        role="implement",
    )


@pytest.mark.parametrize(
    ("lane", "stated"),
    [
        (
            "treated",
            True,
        ),
        ("plain", False),
    ],
)
def test_dispatch_composes_the_instruction_for_the_keys_lane_only(
    dispatch_case, lane, stated
):
    """The instruction is asserted on the prompt file the worker is fed.

    An identity check on the composing function says only that dispatch calls
    it; it cannot see whether the resolved backend reached it, so a lane whose
    key is false would still pass.
    """
    config, repo, config_home = dispatch_case
    recorder = _Recorder()
    record = dispatch_module.dispatch(
        node=_node(config_home, f"anchored-{lane}"),
        project=PROJECT,
        repo=repo,
        config=config,
        session=f"anchored-{lane}",
        check_budget=False,
        backend_override=lane,
        launcher=recorder,
    )
    handed = (crew.run_dir(record["run_id"]) / "prompt.txt").read_text(encoding="utf-8")
    assert (ANCHORED_EDIT_HEADING in handed) is stated, handed
    assert (ANCHORED_EDIT_INSTRUCTION in handed) is stated
    # The plan travels on argv's stdin, so the steered text has to be in it too.
    assert (ANCHORED_EDIT_HEADING in recorder.stdin_text[0]) is stated


def test_an_anchored_edit_is_a_write_only_when_it_landed():
    """capabilities counts the anchored tool's edits when it measures time to
    first write; a refused call wrote nothing and must not cut the figure."""
    completed = {
        "item": {
            "id": "item_1",
            "type": "mcp_tool_call",
            "server": "anchored",
            "tool": "edit_anchored",
            "arguments": {"path": "stats.py"},
            "status": "completed",
            "error": None,
        }
    }
    assert capabilities._event_write_targets(completed) == ("stats.py",)

    refused = {
        "item": {
            "id": "item_2",
            "type": "mcp_tool_call",
            "server": "anchored",
            "tool": "edit_anchored",
            "arguments": {"path": "stats.py"},
            "status": "completed",
            "result": {"structured_content": {"result": "refused: nothing written"}},
        }
    }
    assert capabilities._event_write_targets(refused) == ()

    failed = {
        "item": {
            "id": "item_3",
            "type": "mcp_tool_call",
            "server": "anchored",
            "tool": "edit_anchored",
            "arguments": {"path": "stats.py"},
            "status": "failed",
        }
    }
    assert capabilities._event_write_targets(failed) == ()

    # A read is not a write, and neither is a call to another server.
    read = {
        "item": {
            "id": "item_4",
            "type": "mcp_tool_call",
            "server": "anchored",
            "tool": "read_anchored",
            "arguments": {"path": "stats.py"},
            "status": "completed",
        }
    }
    assert capabilities._event_write_targets(read) == ()
    foreign = {
        "item": {
            "id": "item_5",
            "type": "mcp_tool_call",
            "server": "other",
            "tool": "edit_anchored",
            "arguments": {"path": "stats.py"},
            "status": "completed",
        }
    }
    assert capabilities._event_write_targets(foreign) == ()


def test_the_anchored_counts_reach_the_run_pointer(tmp_path, monkeypatch):
    """The run store is what a later comparison reads, so the counts must be
    carried onto the pointer rather than only into the observation dict."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "codex"
    stub.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    stub.chmod(0o755)
    config = {
        "default_backend": "codex",
        "backends": {
            "codex": {
                "launch": "cli",
                "command": str(stub),
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    run_id = "r-anchored-counts"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True)
    stream = directory / "stream.jsonl"
    stream.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    manifest = directory / "manifest.md"
    manifest.write_text("node: anchored\nstatus: complete\n", encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(tmp_path),
        "worktree": str(tmp_path),
        "launch": "cli",
        "backend": "codex",
        "dialect": "codex",
        "role": "implement",
        "pid": None,
        "pid_start_time": None,
        "session_id": "thread-1",
        "session_harness": "codex",
        "attempt": 1,
        "base_sha": "HEAD",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "phase": "working",
        "agent": {"dialect": "codex", "model": "codex-model"},
        "node": {"id": "anchored", "plan": PLAN, "section": "§5"},
    }
    crew._write_json(crew.pointer_path(run_id), pointer)

    dispatch_sessions_module.observe(run_id, config=config)

    stored = json.loads(crew.pointer_path(run_id).read_text(encoding="utf-8"))
    assert stored["tools"] == {
        "anchored": {
            "calls": 4,
            "read_anchored": 1,
            "edit_anchored": 3,
            "failed": 1,
        },
        "file_change": {"items": 0},
    }
