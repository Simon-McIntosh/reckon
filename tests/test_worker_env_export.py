"""Worker launches carry run identity without expanding persisted environments."""

from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import os
from datetime import datetime
from pathlib import Path

from reckon import crew
from tests.test_crew import CONFIG, _node

pytest_plugins = ("tests.test_crew",)
dispatch_module = importlib.import_module("reckon.crew.dispatch")

DROP_RESUME_EXPORT = "drop the export on resume"
DROP_RESUME_EXPORT_ENV = "RECKON_TEST_DROP_RESUME_EXPORT"
RUNTIME_KEYS = {
    "RECKON_RUN_ID",
    "RECKON_MANIFEST",
    "RECKON_ATTEMPT_STARTED_AT",
}


def _config(command: str) -> dict:
    config = copy.deepcopy(CONFIG)
    config["backends"]["alpha"]["command"] = command
    return config


def _launch(
    *,
    home: Path,
    repo: Path,
    command: str,
    node_id: str,
    coordinator_session: str,
    monkeypatch,
) -> tuple[dict, dict[str, str]]:
    launched: dict[str, dict[str, str]] = {}

    class Spawned:
        pid = 900_001

    def popen(argv, **kwargs):
        launched["environment"] = kwargs["env"]
        return Spawned()

    def start_supervisor(spec_path, run_directory, run_id):
        spec = json.loads(spec_path.read_text())
        with monkeypatch.context() as launch_patch:
            launch_patch.setattr(dispatch_module.subprocess, "Popen", popen)
            return dispatch_module._supervisor_spawn_worker(spec)

    monkeypatch.setattr(dispatch_module, "_start_supervisor", start_supervisor)

    def process_start_time(_pid):
        return 900_001

    monkeypatch.setattr(dispatch_module, "_process_start_time", process_start_time)

    manifest = home / f"{node_id}-manifest.md"
    record = crew.dispatch(
        node=_node(
            id=node_id,
            write_paths=[f"reckon/{node_id}.py"],
            manifest_path=str(manifest),
        ),
        project="proj",
        repo=repo,
        config=_config(command),
        session=coordinator_session,
    )
    return record, launched["environment"]


def _assert_attempt_environment(
    environment: dict[str, str], *, run_id: str, manifest: str
) -> None:
    assert environment["RECKON_RUN_ID"] == run_id
    assert environment["RECKON_MANIFEST"] == manifest
    parsed = datetime.fromisoformat(environment["RECKON_ATTEMPT_STARTED_AT"])
    assert parsed.utcoffset() is not None


def test_clive_dispatch_and_resume_carry_identity_and_additive_headers(
    home, repo, monkeypatch
) -> None:
    coordinator_session = "coordinator-session"
    inherited_header = "X-Existing: retained"
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", inherited_header)
    record, launched = _launch(
        home=home,
        repo=repo,
        command="clive",
        node_id="clive-worker",
        coordinator_session=coordinator_session,
        monkeypatch=monkeypatch,
    )

    _assert_attempt_environment(
        launched,
        run_id=record["run_id"],
        manifest=str(record["manifest_path"]),
    )
    expected_headers = (
        f"{inherited_header}\nX-Reckon-Run-Id: {record['run_id']}\n"
        f"X-Reckon-Session: {coordinator_session}"
    )
    assert launched["ANTHROPIC_CUSTOM_HEADERS"] == expected_headers

    def add_session(current):
        current["session_id"] = "worker-session"
        return current

    dispatch_module._mutate_pointer(record["run_id"], add_session)
    resumed = crew.resume_plan(record["run_id"], "continue", config=_config("clive"))
    if os.environ.get(DROP_RESUME_EXPORT_ENV) == DROP_RESUME_EXPORT:
        resumed = dataclasses.replace(
            resumed,
            environment={
                key: value
                for key, value in resumed.environment.items()
                if key != "RECKON_ATTEMPT_STARTED_AT"
            },
        )

    _assert_attempt_environment(
        resumed.environment,
        run_id=record["run_id"],
        manifest=str(record["manifest_path"]),
    )
    assert resumed.environment["ANTHROPIC_CUSTOM_HEADERS"] == expected_headers

    spec = dispatch_module._supervisor_spec(
        run_id=record["run_id"],
        run_directory=crew.run_dir(record["run_id"]),
        repo_root=repo,
        worktree=Path(str(record["worktree"])),
        plan=resumed,
        prompt_path=home / "resume-advice.txt",
        log_path=home / "resume.jsonl",
        stderr_path=home / "resume.stderr.log",
    )
    persisted = spec["plan"]["environment"]
    assert RUNTIME_KEYS.isdisjoint(persisted)
    assert persisted["ANTHROPIC_CUSTOM_HEADERS"] == inherited_header

    for key in ("prompt_path", "log_path", "stderr_path"):
        Path(str(spec[key])).write_text("")
    captured = {}

    class Spawned:
        pid = 900_002

    def popen(argv, **kwargs):
        captured.update(kwargs)
        return Spawned()

    monkeypatch.setattr(dispatch_module.subprocess, "Popen", popen)
    assert dispatch_module._supervisor_spawn_worker(spec) == 900_002
    supervised_environment = captured["env"]
    _assert_attempt_environment(
        supervised_environment,
        run_id=record["run_id"],
        manifest=str(record["manifest_path"]),
    )
    assert supervised_environment["ANTHROPIC_CUSTOM_HEADERS"] == expected_headers


def test_codex_dispatch_carries_identity_without_anthropic_headers(
    home, repo, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Existing: retained")
    record, launched = _launch(
        home=home,
        repo=repo,
        command="codex",
        node_id="codex-worker",
        coordinator_session="coordinator-session",
        monkeypatch=monkeypatch,
    )

    _assert_attempt_environment(
        launched,
        run_id=record["run_id"],
        manifest=str(record["manifest_path"]),
    )
    assert "ANTHROPIC_CUSTOM_HEADERS" not in launched
