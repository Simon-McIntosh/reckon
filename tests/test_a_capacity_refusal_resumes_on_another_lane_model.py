"""A model-capacity refusal resumes on another model of the same lane.

Codex refuses a launch with "Selected model is at capacity" as a ``turn.failed``
event in the run's stream. The account is not spent and the lane is fine — only
the one model is unavailable — so a resume of that run relaunches another model
of the same lane, keeping the session, rather than repeating the refused model.

The refusal is read from the stream's own terminal event, never from prose: a
stream that merely quotes the message and ends cleanly is an ordinary resume on
the recorded model, as are a clean end and any other terminal failure.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import time
from pathlib import Path

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import crew
from reckon.crew.runs import pointer_path
from tests import test_a_live_run_never_reads_dead as liveness

picker_module = importlib.import_module("reckon.crew.picker")
dispatch_picker_module = importlib.import_module("reckon.crew.dispatch_picker")

PROJECT = "proj"
SESSION = "session-carry"

CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."

# A codex lane declaring three models: ``sol`` the default, ``astra`` and
# ``luna``. The models are named among the backends so lane expansion creates an
# entry per model — the shape a host layer resolves and the entry a chosen model
# is read back from — and the efforts differ, so a launch that names a model and
# its effort is told from an ignored swap. ``clive`` is the local lane and
# declares a single model, so it offers no choice.
CONFIG = {
    "default_backend": "codex",
    "local_backend": "clive",
    "routing": {"picker": "route"},
    "backends": {
        "codex": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
            "lane": "codex",
            "model": "gpt-6-sol",
            "effort": "high",
        },
        "codex-astra": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
            "lane": "codex",
            "model_key": "astra",
            "model": "gpt-6-astra",
            "effort": "medium",
            "derived_from": "lanes.codex.models.astra",
        },
        "codex-luna": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
            "lane": "codex",
            "model_key": "luna",
            "model": "gpt-6-luna",
            "effort": "xhigh",
            "derived_from": "lanes.codex.models.luna",
        },
        "clive": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
            "lane": "clive",
            "model": "gpt-6-clive",
            "effort": "high",
        },
    },
    "lanes": {
        "codex": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
            "default_model": "sol",
            "models": {
                "sol": {"model": "gpt-6-sol", "effort": "high"},
                "astra": {"model": "gpt-6-astra", "effort": "medium"},
                "luna": {"model": "gpt-6-luna", "effort": "xhigh"},
            },
        },
        "clive": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
            "default_model": "sol",
            "models": {
                "sol": {"model": "gpt-6-clive", "effort": "high"},
            },
        },
    },
    "roles": {"implement": {}, "review": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


class _Answer:
    """A picker answer shaped like the object the selection reader reads."""

    def __init__(self, action, backend, model=None, family="codex"):
        self._fields = {
            "action": action,
            "backend": backend,
            "family": family,
            "model": model,
            "effort": "xhigh",
            "probabilities": {backend: 0.9} if backend else {"hold": 0.9},
            "confidence": 0.9,
            "jev_model": "jev-test",
            "fallback_reason": None,
            "latency_ms": 1.0,
            "excluded": [],
        }

    def as_dict(self):
        return dict(self._fields)


def _route(model):
    return _Answer("route", "codex", model, "codex")


def _install_picker(monkeypatch, answer_factory, offered=None):
    """Stub the picker, recording the model keys each ask was offered.

    ``offered`` records the codex lane's declared model keys the ask was handed.
    That is where the exclusion is visible: a key absent here is a model the
    picker was never offered, and the answer's model is validated against the
    same offered keys.
    """

    def pick(request, config, *, repo, cached_only, **inputs):
        if offered is not None:
            lane = (config.get("lanes") or {}).get("codex") or {}
            offered.append(sorted(lane.get("models") or {}))
        selected = (
            answer_factory(config) if callable(answer_factory) else answer_factory
        )
        if isinstance(selected, Exception):
            raise selected
        return selected

    monkeypatch.setattr(picker_module, "pick", pick)


@pytest.fixture()
def operator_home(tmp_path, monkeypatch):
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
def crew_home(operator_home, monkeypatch):
    config_home = operator_home / ".config" / "reckon"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path, crew_home):
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s41">the capacity followup</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "target.py").write_text("value = 1\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs/plans/plan-a.html", "target.py"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *args)
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_stream(path, events):
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _capacity_stream(path):
    _write_stream(
        path,
        [
            {"type": "thread.started", "thread_id": SESSION},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "planning the attempt"},
            },
            {"type": "turn.failed", "error": CAPACITY_MESSAGE},
        ],
    )


def _stopped_pointer(tmp_path, repo, run_id, *, backend="codex", model="gpt-6-sol"):
    """A cli run as a resume finds it: gone, on this host, with a recorded model."""
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = crew.reports_dir() / f"{run_id}-node" / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: waiting\n", encoding="utf-8")
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text("the original dispatch prompt\n", encoding="utf-8")
    log_path = directory / "stream.jsonl"
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
        "session_id": SESSION,
        "session_harness": "codex",
        "attempt": 1,
        "base_sha": "HEAD",
        "created_at": "2026-10-01T09:59:00+00:00",
        "log_path": str(log_path),
        "manifest_path": str(manifest),
        "prompt_path": str(prompt_path),
        "phase": "working",
        "agent": {"model": model, "effort": "high"},
        "lane_changes": [{"from_backend": "clive", "to_backend": "codex"}],
        "lane": backend,
        "model_key": None,
        "node": {
            "id": "node-a",
            "plan": "plan-a",
            "section": "s41",
            "time_budget": "25m",
            "write_paths": ["target.py"],
        },
    }
    crew._write_json(pointer_path(run_id), record)
    return record


def _stub_gates(monkeypatch):
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )


def _argv(plan):
    return [str(token) for token in plan.argv]


def test_a_capacity_refusal_resumes_on_the_model_jev_names(
    operator_home, crew_home, repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    _stub_gates(monkeypatch)
    run_id = "r-capacity-luna"
    record = _stopped_pointer(tmp_path, repo, run_id)
    _capacity_stream(Path(record["log_path"]))
    offered = []
    _install_picker(monkeypatch, _route("luna"), offered)

    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    argv = _argv(plan)
    assert "gpt-6-luna" in argv, argv
    assert argv[argv.index("gpt-6-luna") - 1] == "-m", argv
    assert "model_reasoning_effort=xhigh" in argv, argv
    assert SESSION in argv, argv
    assert offered == [["astra", "luna"]], offered

    pointer = crew.read_pointer(run_id)
    assert pointer["agent"]["model"] == "gpt-6-luna"
    assert pointer["agent"]["effort"] == "xhigh"
    assert pointer["model_key"] == "luna"
    changes = pointer["model_changes"]
    assert len(changes) == 1, changes
    entry = changes[0]
    assert entry["lane"] == "codex"
    assert entry["from_model"] == "gpt-6-sol"
    assert entry["to_model"] == "gpt-6-luna"
    assert entry["reason"] == "model-capacity"
    assert pointer["lane_changes"] == [{"from_backend": "clive", "to_backend": "codex"}]


@pytest.mark.parametrize(
    "answer",
    [
        _Answer("hold", None, model=None),
        RuntimeError("picker exploded"),
        _Answer("route", "codex", model="gpt-6-sol"),
    ],
    ids=["hold", "raises", "answers-sol"],
)
def test_a_failed_capacity_ask_resumes_on_the_first_other_model(
    operator_home, crew_home, repo, tmp_path, monkeypatch, answer
):
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    _stub_gates(monkeypatch)
    run_id = "r-capacity-fall-" + type(answer).__name__
    record = _stopped_pointer(tmp_path, repo, run_id)
    _capacity_stream(Path(record["log_path"]))
    _install_picker(monkeypatch, answer)

    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    argv = _argv(plan)
    assert "gpt-6-astra" in argv, argv
    assert "gpt-6-sol" not in argv, argv
    pointer = crew.read_pointer(run_id)
    assert pointer["agent"]["model"] == "gpt-6-astra"
    assert pointer["model_key"] == "astra"


def test_a_capacity_ask_that_outlasts_the_deadline_resumes_on_astra(
    operator_home, crew_home, repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    _stub_gates(monkeypatch)
    monkeypatch.setattr(dispatch_picker_module, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.3)

    def slow_pick(*_args, **_kwargs):
        time.sleep(2.0)
        return _route("luna")

    monkeypatch.setattr(picker_module, "pick", slow_pick)
    run_id = "r-capacity-timeout"
    record = _stopped_pointer(tmp_path, repo, run_id)
    _capacity_stream(Path(record["log_path"]))

    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    assert "gpt-6-astra" in _argv(plan)


@pytest.mark.parametrize(
    "events",
    [
        [
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "turn.failed", "error": "the model is unknown"},
        ],
        [
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "turn.completed", "usage": {"output_tokens": 1}},
        ],
        [
            {"type": "thread.started", "thread_id": SESSION},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": CAPACITY_MESSAGE},
            },
            {"type": "turn.completed", "usage": {"output_tokens": 1}},
        ],
    ],
    ids=["other-failure", "clean-end", "quoted-message"],
)
def test_another_ending_resumes_on_the_recorded_model(
    operator_home, crew_home, repo, tmp_path, monkeypatch, events
):
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    _stub_gates(monkeypatch)
    calls = []

    def pick(*_args, **_kwargs):
        calls.append("asked")
        raise AssertionError("no ask without a capacity refusal")

    monkeypatch.setattr(picker_module, "pick", pick)
    run_id = "r-capacity-other-" + events[-1]["type"].replace(".", "-")
    record = _stopped_pointer(tmp_path, repo, run_id)
    _write_stream(Path(record["log_path"]), events)

    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    assert "gpt-6-sol" in _argv(plan)
    assert calls == []
    pointer = crew.read_pointer(run_id)
    assert pointer["model_key"] is None
    assert not pointer.get("model_changes")


def test_a_one_model_lane_resumes_on_it_with_no_ask(
    operator_home, crew_home, repo, tmp_path, monkeypatch
):
    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", True)
    _stub_gates(monkeypatch)
    calls = []

    def pick(*_args, **_kwargs):
        calls.append("asked")
        raise AssertionError("a lane declaring no other model must not ask")

    monkeypatch.setattr(picker_module, "pick", pick)
    run_id = "r-capacity-single"
    record = _stopped_pointer(
        tmp_path, repo, run_id, backend="clive", model="gpt-6-clive"
    )
    _capacity_stream(Path(record["log_path"]))

    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    assert "gpt-6-clive" in _argv(plan)
    assert calls == []
    assert not crew.read_pointer(run_id).get("model_changes")
