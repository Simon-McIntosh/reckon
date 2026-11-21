"""A resumed launch records the lane its first launch ran on.

One lane may be served by more than one local backend: different worker
harnesses in front of the same model server, each declaring the same lane
document. The lane a backend serves is therefore the document it publishes,
not the name it carries, and a resumed launch must resolve that same lane
rather than fall back to the backend's name.

Two things are checked here. A run launched and then resumed on each local
backend records one lane across both launches. And for a configuration whose
two local backends name one lane document spelled two ways, the admission
drainer serves both and both launches record that one lane, so the drainer's
decision about the local lane and the lane a launch records cannot disagree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import reckon.crew.dispatch_sessions as dispatch_sessions_module
from reckon import _backends, crew
from reckon.crew import recovery, worker_behaviour
from reckon.crew.admission.selection import gate_admits_request

PROJECT = "sample"
RUN = "r-resumed-keeps-its-lane"

# The published lane document, and the same file spelled a second way: a
# redundant component the filesystem resolves away. Two backends naming these
# two spellings are one lane.
LANE_DOCUMENT = "/srv/lanes/local.json"
LANE_DOCUMENT_OTHER_SPELLING = "/srv/lanes/./local.json"

# Two local backends in front of one model server: a claude-shaped wrapper and
# oh-my-pi, publishing one lane document between them.
LOCAL_BACKENDS = {
    "clive": {"lane_document": LANE_DOCUMENT},
    "omp-local": {"lane_document": LANE_DOCUMENT_OTHER_SPELLING},
}


# The command each local backend launches: two worker harnesses in front of
# one model server, named by the distinct command each speaks.
BACKEND_COMMANDS = {"clive": "clive", "omp-local": "omp"}


def _lane_of(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path]:
    """A run the fixture stands beside, plus a runnable local configuration."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for command in BACKEND_COMMANDS.values():
        binary = bindir / command
        binary.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
        binary.chmod(0o755)

    backends = {
        name: {
            "launch": "cli",
            "command": str(bindir / BACKEND_COMMANDS[name]),
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "lane_document": settings["lane_document"],
        }
        for name, settings in LOCAL_BACKENDS.items()
    }
    config = {
        "default_backend": "clive",
        "local_backend": "clive",
        "backends": backends,
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "exhausted_statuses": [],
        },
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }

    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "tree"
    worktree.mkdir()
    directory = crew.run_dir(RUN)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stream.jsonl").write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "clive-session",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "prompt.txt").write_text("carry the node home\n", encoding="utf-8")
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps({"run_id": RUN, "exit_code": 0}), encoding="utf-8"
    )
    manifest = tmp_path / "manifest.md"
    manifest.write_text("node: lane\nstatus: waiting\n", encoding="utf-8")

    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(worktree),
        "launch": "cli",
        "backend": "clive",
        "dialect": "clive",
        "role": "implement",
        "pid": None,
        "pid_start_time": None,
        "session_id": "clive-session",
        "session_harness": "clive",
        "attempt": 1,
        "base_sha": "HEAD",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(directory / "prompt.txt"),
        "phase": "blocked",
        "command": str(bindir / "clive"),
        "agent": {"dialect": "clive"},
        "node": {
            "id": "lane-node",
            "goal": "keep one lane across a resume",
            "plan": "fixture",
            "section": "resume",
            "spec_level": "guided",
            "done_when": "the resumed launch records the lane its first launch ran on",
            "role": "implement",
            "time_budget": "25m",
            "write_paths": ["package/target.py"],
        },
    }
    crew._write_json(crew.pointer_path(RUN), pointer)

    monkeypatch.setattr(dispatch_sessions_module, "FENCE_WORKERS", False)
    monkeypatch.setattr(dispatch_sessions_module, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        dispatch_sessions_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    yield config, worktree
    landed = [
        path
        for path in (
            Path.home() / ".config" / "reckon" / "crew" / "runs" / RUN,
            Path.home() / ".config" / "reckon" / "crew" / "live" / f"{RUN}.json",
        )
        if path.exists()
    ]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


def _first_launch(config: dict, worktree: Path, name: str) -> _backends.LaunchPlan:
    backend = dict(config["backends"][name])
    return _backends.launch_plan(
        backend_name=name,
        backend=backend,
        prompt="one node, one brief",
        worktree=worktree,
        manifest_path=worktree / "manifest.md",
        fence=False,
        behaviour_role=worker_behaviour.IMPLEMENT_ROLE,
        flight_config=config,
    )


def _resumed_launch(
    config: dict, name: str, monkeypatch: pytest.MonkeyPatch
) -> _backends.LaunchPlan:
    record = crew.read_pointer(RUN)
    record["backend"] = name
    record["dialect"] = name
    record["command"] = str(config["backends"][name]["command"])
    record["agent"] = {"dialect": name}
    crew._write_json(crew.pointer_path(RUN), record)
    return dispatch_sessions_module.resume_plan(RUN, "carry on", config=config)


@pytest.mark.parametrize("name", tuple(LOCAL_BACKENDS))
def test_a_resumed_launch_records_the_lane_its_first_launch_ran_on(
    run: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    config, worktree = run
    first = _first_launch(config, worktree, name)
    resumed = _resumed_launch(config, name, monkeypatch)
    expected = _lane_of(LANE_DOCUMENT)
    assert first.worker_behaviour["lane"] == expected, first.worker_behaviour
    assert resumed.worker_behaviour["lane"] == expected, (
        f"backend {name!r} resumed on lane "
        f"{resumed.worker_behaviour['lane']!r}, not the lane it launched on "
        f"({expected!r})"
    )
    assert resumed.worker_behaviour["lane"] == first.worker_behaviour["lane"]


def test_two_backends_naming_one_document_are_served_and_recorded_as_one_lane(
    run: tuple[dict, Path],
) -> None:
    config, worktree = run
    expected = _lane_of(LANE_DOCUMENT)
    # The drainer started for either backend serves a request queued under the
    # other, because they publish one lane. The gate's answer must agree with
    # the lane each launch records.
    for serving in LOCAL_BACKENDS:
        for queued in LOCAL_BACKENDS:
            assert gate_admits_request(queued, serving, config), (
                f"a drainer for {serving!r} does not serve {queued!r}, yet both "
                "backends name one lane document"
            )
    for name in LOCAL_BACKENDS:
        launch = _first_launch(config, worktree, name)
        assert launch.worker_behaviour["lane"] == expected, launch.worker_behaviour
