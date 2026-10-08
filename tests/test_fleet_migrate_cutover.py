"""A fleet move verifies each conversation before retiring the old allocation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import fleet_migrate


def _ledger(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state"
    path = state / "migration" / "move-test" / "ledger.json"
    path.parent.mkdir(parents=True)
    sessions = [
        {
            "name": name,
            "tabs": [
                {
                    "name": "work",
                    "panes": [
                        {
                            "command": "fleet-claude",
                            "conversation": f"{name}-conversation",
                            "cwd": "/work",
                        }
                    ],
                }
            ],
        }
        for name in ("first-fleet", "second-fleet")
    ]
    path.write_text(
        json.dumps(
            {
                "completed": ["census", "layout", "stand-up", "promote"],
                "next_step": "cutover",
                "census": {"sessions": sessions},
                "stand_up": {
                    "old_record": {"job_id": "1234", "node": "old-node"},
                    "job_id": "5678",
                    "node": "new-node",
                },
            }
        )
    )
    layouts = tmp_path / "zellij" / "layouts"
    layouts.mkdir(parents=True)
    for item in sessions:
        (layouts / f"migrate-{item['name']}.kdl").write_text("layout {}\n")
    return path, layouts


class Actions:
    def __init__(self, path: Path, layouts: Path):
        self.path = path
        self.layouts = layouts
        self.calls: list[tuple] = []
        self.old_sessions = {"first-fleet", "second-fleet"}
        self.steps: list[str] = []
        self.processes_ready = True
        self.relay_fails = False
        self.ready_response = True

    def step(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(("step", tuple(argv)))
        assert argv[:3] == ["srun", "--overlap", "--jobid=1234"]
        if "end-session" in argv:
            name = argv[-1]
            self.old_sessions.discard(name)
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"session": name, "ended": True}), ""
            )
        assert argv[-1] == "list-sessions"
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(sorted(self.old_sessions)), ""
        )

    def send(self, job: dict[str, str], line: str) -> None:
        self.calls.append(("send", job["jobid"], line))
        if self.relay_fails:
            raise fleet_migrate.MigrationError("supervisor request failed")
        assert job["jobid"] == "5678"
        if line.startswith("ready "):
            if self.ready_response:
                token = line.split()[1]
                (self.path.parents[1] / f"ready-{token}.json").write_text(
                    json.dumps({"job_id": "5678", "node": "new-node", "standby": False})
                )
            return
        name = line.split()[1]
        assert line == f"session {name} migrate-{name}"
        assert name not in self.old_sessions

    def inspect(self, job: dict[str, str], name: str) -> dict:
        self.calls.append(("inspect", job["jobid"], name))
        return {
            "session": name,
            "node": "new-node",
            "panes": [
                {
                    "tab": "work",
                    "conversation": f"{name}-conversation",
                    "cwd": "/work",
                    "pid": 123 if self.processes_ready else None,
                    "command": "claude --resume" if self.processes_ready else None,
                }
            ],
        }

    def worker_steps(self, job_id: str) -> list[str]:
        self.calls.append(("steps", job_id))
        return self.steps

    def cancel(self, job_id: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(("cancel", job_id))
        return subprocess.CompletedProcess(
            ["scancel", job_id], 0, "scheduler accepted 1234\n", ""
        )

    def migrate(self, **options) -> str:
        return fleet_migrate.migrate(
            state=self.path.parents[2],
            layouts_dir=self.layouts,
            run_step=self.step,
            send_supervisor=self.send,
            inspect_session=self.inspect,
            list_worker_steps=self.worker_steps,
            cancel_job=self.cancel,
            pause=lambda _: None,
            **options,
        )


@pytest.fixture
def actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Actions:
    path, layouts = _ledger(tmp_path)
    commands = tmp_path / "commands"
    commands.mkdir()
    for command in ("sbatch", "srun", "scancel"):
        stub = commands / command
        stub.write_text("#!/bin/sh\nexit 91\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands))
    return Actions(path, layouts)


def test_cutover_lists_only_remaining_sessions_and_refuses_unknown(actions: Actions):
    before = actions.path.read_bytes()
    assert actions.migrate() == (
        "cutover: sessions still to move: first-fleet, second-fleet"
    )
    assert "first-fleet" in actions.migrate(dry_run=True)
    with pytest.raises(
        fleet_migrate.MigrationError, match="absent from the recorded census"
    ):
        actions.migrate(session="unknown-fleet")
    assert actions.path.read_bytes() == before
    assert actions.calls == []


def test_cutover_ends_old_session_starts_layout_and_verifies_each_pane(
    actions: Actions,
):
    before = actions.path.read_bytes()
    assert "end first-fleet" in actions.migrate(session="first-fleet", dry_run=True)
    assert actions.path.read_bytes() == before
    assert actions.calls == []
    answer = actions.migrate(session="first-fleet")
    assert "1 Claude panes" in answer
    assert [call[0] for call in actions.calls] == ["send", "step", "send", "inspect"]
    assert actions.calls[0][2].startswith("ready ")
    assert actions.calls[1][1][-2:] == ("end-session", "first-fleet")
    assert actions.calls[2][2] == "session first-fleet migrate-first-fleet"
    ledger = json.loads(actions.path.read_text())
    assert ledger["cutovers"]["first-fleet"]["status"] == "complete"
    assert ledger["cutovers"]["first-fleet"]["panes"][0]["conversation"] == (
        "first-fleet-conversation"
    )
    assert ledger["next_step"] == "cutover"
    assert actions.migrate(session="first-fleet").endswith("skipped")
    assert len(actions.calls) == 4
    assert actions.migrate() == "cutover: sessions still to move: second-fleet"
    actions.migrate(session="second-fleet")
    ledger = json.loads(actions.path.read_text())
    assert ledger["next_step"] == "retire"
    assert ledger["completed"][-1] == "cutover"


def test_cutover_requires_live_claude_and_preserves_retry_checkpoint(actions: Actions):
    actions.processes_ready = False
    with pytest.raises(
        fleet_migrate.MigrationError, match="did not show 1 recorded Claude"
    ):
        actions.migrate(session="first-fleet")
    ledger = json.loads(actions.path.read_text())
    assert ledger["cutovers"]["first-fleet"]["phase"] == "old-ended"
    assert ledger["next_step"] == "cutover"
    assert sum(call[0] == "step" for call in actions.calls) == 1
    actions.processes_ready = True
    assert "1 Claude panes" in actions.migrate(session="first-fleet")
    assert sum(call[0] == "step" for call in actions.calls) == 1
    assert sum(call[0] == "send" for call in actions.calls) == 2


def test_cutover_resumes_prior_end_checkpoint(actions: Actions):
    ledger = json.loads(actions.path.read_text())
    ledger["cutovers"] = {"first-fleet": {"phase": "old-ended"}}
    actions.path.write_text(json.dumps(ledger))
    actions.old_sessions.remove("first-fleet")
    assert "1 Claude panes" in actions.migrate(session="first-fleet")
    assert [call[0] for call in actions.calls] == ["send", "inspect"]
    assert actions.calls[0][2] == "session first-fleet migrate-first-fleet"


def test_cutover_retries_end_without_starting_early(actions: Actions):
    original_step = actions.step

    def refused_end(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, json.dumps({"session": "first-fleet", "ended": False}), "refused"
        )

    actions.step = refused_end
    with pytest.raises(fleet_migrate.MigrationError, match="did not confirm"):
        actions.migrate(session="first-fleet")
    assert not any(
        call[0] == "send" and call[2].startswith("session ") for call in actions.calls
    )
    assert "first-fleet" in actions.old_sessions
    actions.step = original_step
    assert "1 Claude panes" in actions.migrate(session="first-fleet")
    assert (
        sum(
            call[0] == "send" and call[2].startswith("session ")
            for call in actions.calls
        )
        == 1
    )


def test_cutover_relay_failure_keeps_old_session(actions: Actions):
    actions.relay_fails = True
    before = actions.path.read_bytes()
    with pytest.raises(fleet_migrate.MigrationError, match="supervisor request failed"):
        actions.migrate(session="first-fleet")
    assert actions.old_sessions == {"first-fleet", "second-fleet"}
    assert not any(call[0] == "step" for call in actions.calls)
    assert not any(
        call[0] == "send" and call[2].startswith("session ") for call in actions.calls
    )
    assert actions.path.read_bytes() == before


def test_cutover_requires_written_ready_response(actions: Actions):
    actions.ready_response = False
    with pytest.raises(fleet_migrate.MigrationError, match="readiness"):
        actions.migrate(session="first-fleet")
    assert actions.old_sessions == {"first-fleet", "second-fleet"}
    assert not any(call[0] == "step" for call in actions.calls)
    assert not any(
        call[0] == "send" and call[2].startswith("session ") for call in actions.calls
    )


def test_cutover_sends_request_through_its_step_argv(
    actions: Actions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    fifo = runtime / fleet_migrate.REQUEST_FIFO_NAME
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    monkeypatch.setenv("FLEET_RUNTIME_DIR", str(runtime))
    relays = []

    def relay(argv: list[str]) -> subprocess.CompletedProcess[str]:
        relays.append(argv)
        result = subprocess.run(
            argv[3:],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
        if result.returncode == 0 and argv[7] == "ready":
            (actions.path.parents[1] / f"ready-{argv[8]}.json").write_text(
                json.dumps({"job_id": "5678", "node": "new-node", "standby": False})
            )
        return result

    monkeypatch.setattr(fleet_migrate, "_run_step", relay)
    actions.send = fleet_migrate._send_supervisor
    try:
        assert "1 Claude panes" in actions.migrate(session="first-fleet")
        lines = os.read(reader, 4096).splitlines()
        assert lines[0].startswith(b"ready ")
        assert lines[1] == b"session first-fleet migrate-first-fleet"
    finally:
        os.close(reader)
    assert relays[0][:8] == [
        "srun",
        "--overlap",
        "--jobid=5678",
        sys.executable,
        "-m",
        "reckon.crew.fleet_migrate",
        "request",
        "ready",
    ]
    assert relays[1:] == [
        [
            "srun",
            "--overlap",
            "--jobid=5678",
            sys.executable,
            "-m",
            "reckon.crew.fleet_migrate",
            "request",
            "session",
            "first-fleet",
            "migrate-first-fleet",
        ]
    ]


def test_request_entry_point_relays_all_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    fifo = runtime / fleet_migrate.REQUEST_FIFO_NAME
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    monkeypatch.setenv("FLEET_RUNTIME_DIR", str(runtime))
    words = ["ready", *(f"word{index}" for index in range(30))]
    try:
        result = subprocess.run(
            [sys.executable, "-m", "reckon.crew.fleet_migrate", "request", *words],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
        assert result.returncode == 0, result.stderr
        assert os.read(reader, 4096) == (" ".join(words) + "\n").encode()
    finally:
        os.close(reader)


def test_retire_refuses_steps_and_sessions_then_requires_confirmation(actions: Actions):
    actions.migrate(session="first-fleet")
    actions.migrate(session="second-fleet")
    before = actions.path.read_bytes()
    with pytest.raises(fleet_migrate.MigrationError, match="cutover step"):
        actions.migrate(session="first-fleet", dry_run=True)
    assert "scancel 1234" in actions.migrate(dry_run=True)
    assert actions.path.read_bytes() == before
    actions.steps = ["1234.4"]
    with pytest.raises(fleet_migrate.MigrationError, match="worker steps"):
        actions.migrate(confirm=True)
    actions.steps = []
    actions.old_sessions.add("stray-fleet")
    with pytest.raises(fleet_migrate.MigrationError, match="zellij sessions"):
        actions.migrate(confirm=True)
    actions.old_sessions.clear()
    answer = actions.migrate()
    assert "scancel 1234" in answer and "--confirm required" in answer
    assert not any(call[0] == "cancel" for call in actions.calls)
    assert actions.path.read_bytes() == before
    answer = actions.migrate(confirm=True)
    assert "scheduler accepted 1234" in answer
    assert [call for call in actions.calls if call[0] == "cancel"] == [
        ("cancel", "1234")
    ]
    ledger = json.loads(actions.path.read_text())
    assert ledger["retirement"]["job_id"] == "1234"
    assert ledger["next_step"] == "done"
    assert actions.migrate() == "migration complete"


def test_retire_requires_all_recorded_sessions_and_a_numeric_job_id(actions: Actions):
    ledger = json.loads(actions.path.read_text())
    ledger["next_step"] = "retire"
    actions.path.write_text(json.dumps(ledger))
    before = actions.path.read_bytes()
    with pytest.raises(fleet_migrate.MigrationError, match="every recorded session"):
        actions.migrate(confirm=True)
    assert actions.path.read_bytes() == before
    ledger["cutovers"] = {
        item["name"]: {"status": "complete"} for item in ledger["census"]["sessions"]
    }
    ledger["stand_up"]["old_record"]["job_id"] = "--user=anyone"
    actions.path.write_text(json.dumps(ledger))
    with pytest.raises(fleet_migrate.MigrationError, match="not numeric"):
        actions.migrate(confirm=True)
    assert actions.calls == []


@pytest.mark.parametrize("step", ["census", "layout", "stand-up", "promote", "retire"])
def test_session_is_refused_outside_cutover(actions: Actions, step: str):
    ledger = json.loads(actions.path.read_text())
    ledger["next_step"] = step
    actions.path.write_text(json.dumps(ledger))
    before = actions.path.read_bytes()
    with pytest.raises(fleet_migrate.MigrationError, match="cutover step"):
        actions.migrate(session="first-fleet")
    assert actions.path.read_bytes() == before
    assert actions.calls == []


def test_local_process_census_requires_live_claude_on_the_new_node(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    record = {
        "pid": 4321,
        "sessionId": "conversation-example",
        "cwd": "/work",
        "procStart": "567",
    }
    (sessions / "4321.json").write_text(json.dumps(record))
    process = tmp_path / "proc" / "4321"
    process.mkdir(parents=True)
    (process / "stat").write_text(
        "4321 (claude) " + " ".join(["S", *(["0"] * 18), "567"])
    )
    (process / "cmdline").write_bytes(
        b"/usr/bin/claude\0--resume\0conversation-example\0"
    )
    layout = (
        'layout {\n  tab name="work" {\n    pane command="fleet-claude" {\n'
        '      args "--resume" "conversation-example"\n    }\n  }\n}\n'
    )

    def run_step(argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv == ["zellij", "--session", "first-fleet", "action", "dump-layout"]
        return subprocess.CompletedProcess(argv, 0, layout, "")

    def observe() -> dict:
        return fleet_migrate._local_session_processes(
            "first-fleet",
            claude_sessions=sessions,
            process_root=tmp_path / "proc",
            active_sessions=lambda: ["first-fleet"],
            run_step=run_step,
            hostname=lambda: "new-node.example",
        )

    observed = observe()
    assert observed["node"] == "new-node"
    assert observed["panes"] == [
        {
            "tab": "work",
            "conversation": "conversation-example",
            "cwd": "/work",
            "pid": 4321,
            "command": "/usr/bin/claude --resume conversation-example ",
        }
    ]
    (process / "cmdline").write_bytes(b"/usr/bin/other\0")
    assert observe()["panes"][0]["pid"] is None
