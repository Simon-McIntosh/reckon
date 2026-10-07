"""A probe session can be cut back onto its own node without scheduler changes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import fleet_migrate

SOURCE_LAYOUT = (
    "layout {\n"
    '  tab name="conversation" {\n'
    '    pane command="fleet-claude" cwd="/work" {\n'
    '      args "--session-id" "conversation-id"\n'
    "    }\n"
    "  }\n"
    '  tab name="shell" { pane command="bash" cwd="/work" }\n'
    "}\n"
)


class Probe:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.active = True
        self.ready = True
        self.calls: list[tuple[str, ...]] = []
        transcript = root / "transcripts" / "-work" / "conversation-id.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        self.source = {
            "layouts": {"probe-trial": SOURCE_LAYOUT, "unrelated-fleet": SOURCE_LAYOUT},
            "claude": {
                "conversation-id": {
                    "conversation": "conversation-id",
                    "cwd": "/work",
                }
            },
            "starts": ["conversation-id"],
            "transcript_root": str(root / "transcripts"),
        }

    def step(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        if argv == ["zellij", "list-sessions", "--no-formatting"]:
            return subprocess.CompletedProcess(
                argv, 0, "probe-trial\n" if self.active else "", ""
            )
        if argv == ["zellij", "kill-session", "probe-trial"]:
            self.active = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected step: {argv}")

    def send(self, job: dict[str, str], line: str) -> None:
        assert job == {"jobid": "local"}
        assert line == "session probe-trial rehearse-probe-trial"
        self.calls.append(("supervisor", line))
        self.active = True

    def inspect(self, job: dict[str, str], name: str) -> dict:
        assert job == {"jobid": "local"} and name == "probe-trial"
        self.calls.append(("inspect", name))
        return {
            "session": name,
            "node": "trial-node",
            "tabs": ["conversation", "shell"]
            if self.ready
            else ["shell", "conversation"],
            "panes": [
                {
                    "tab": "conversation",
                    "conversation": "conversation-id",
                    "cwd": "/work",
                    "pid": 42 if self.ready else None,
                    "command": "claude --resume conversation-id"
                    if self.ready
                    else None,
                }
            ],
        }

    def migrate(self, **options: object) -> str:
        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("rehearsal called a migration or scheduler action")

        return fleet_migrate.migrate(
            rehearse=True,
            session="probe-trial",
            state=self.root / "state",
            layouts_dir=self.root / "layouts",
            observation=lambda: self.source,
            run_step=self.step,
            send_supervisor=self.send,
            inspect_session=self.inspect,
            submit_hold=forbidden,
            query_jobs=forbidden,
            replace_reservation=forbidden,
            read_reservation=forbidden,
            read_fleet_record=forbidden,
            list_worker_steps=forbidden,
            cancel_job=forbidden,
            hostname=lambda: "trial-node.example",
            pause=lambda _: None,
            **options,
        )


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Probe:
    commands = tmp_path / "commands"
    commands.mkdir()
    for command in ("sbatch", "srun", "scancel"):
        stub = commands / command
        stub.write_text("#!/bin/sh\nexit 91\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands))
    return Probe(tmp_path)


def test_rehearsal_records_each_step_and_preserves_tabs_and_conversation(
    probe: Probe,
) -> None:
    ledger_path = probe.root / "state/migration/rehearsals/probe-trial/ledger.json"
    assert probe.migrate(dry_run=True).startswith("census:")
    assert not ledger_path.exists() and probe.calls == []
    assert "next step: layout" in probe.migrate()
    census = json.loads(ledger_path.read_text())
    assert [item["name"] for item in census["census"]["sessions"]] == ["probe-trial"]
    before = ledger_path.read_bytes()
    assert probe.migrate(dry_run=True).startswith("layout:")
    assert ledger_path.read_bytes() == before and probe.calls == []
    assert "next step: end" in probe.migrate()
    layout = probe.root / "layouts/rehearse-probe-trial.kdl"
    assert [tab["name"] for tab in fleet_migrate.parse_layout(layout.read_text())] == [
        "conversation",
        "shell",
    ]
    assert 'args "--resume" "conversation-id"' in layout.read_text()
    assert "next step: start" in probe.migrate()
    before = ledger_path.read_bytes()
    assert probe.migrate(dry_run=True).startswith("start:")
    assert ledger_path.read_bytes() == before
    assert "next step: verify" in probe.migrate()
    assert "next step: done" in probe.migrate()
    ledger = json.loads(ledger_path.read_text())
    assert ledger["completed"] == ["census", "layout", "end", "start", "verify"]
    assert ledger["old_end"]["ended"] is True
    assert ledger["verification"]["tabs"] == ["conversation", "shell"]
    assert ledger["verification"]["panes"][0]["conversation"] == "conversation-id"
    assert probe.migrate() == f"rehearsal complete for probe-trial in {ledger_path}"
    assert not any(call[0] in {"sbatch", "srun", "scancel"} for call in probe.calls)


def test_rehearsal_refuses_non_probe_names_before_any_action(probe: Probe) -> None:
    before = list(probe.root.rglob("*"))
    with pytest.raises(fleet_migrate.MigrationError, match="starting with probe-"):
        fleet_migrate.migrate(
            rehearse=True,
            session="ordinary-fleet",
            state=probe.root / "state",
            observation=lambda: probe.source,
            run_step=probe.step,
        )
    assert list(probe.root.rglob("*")) == before
    assert probe.calls == []


def test_rehearsal_retries_verification_without_ending_again(probe: Probe) -> None:
    for _ in range(4):
        probe.migrate()
    probe.ready = False
    with pytest.raises(fleet_migrate.MigrationError, match="tab order"):
        probe.migrate()
    ledger_path = probe.root / "state/migration/rehearsals/probe-trial/ledger.json"
    assert json.loads(ledger_path.read_text())["next_step"] == "verify"
    probe.ready = True
    assert "next step: done" in probe.migrate()
    assert sum(call[:2] == ("zellij", "kill-session") for call in probe.calls) == 1
    assert sum(call[0] == "supervisor" for call in probe.calls) == 1


def test_rehearsal_cli_requires_probe_name() -> None:
    answer = CliRunner().invoke(
        main, ["fleet-node", "migrate", "--rehearse", "--session", "other"]
    )
    assert answer.exit_code != 0
    assert "starting with probe-" in answer.output


def test_rehearsal_cli_dry_run_has_no_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        fleet_migrate,
        "state_directory",
        lambda: tmp_path / "state",
    )
    answer = CliRunner().invoke(
        main,
        [
            "fleet-node",
            "migrate",
            "--rehearse",
            "--session",
            "probe-trial",
            "--dry-run",
        ],
    )
    assert answer.exit_code == 0
    assert answer.output.startswith("census: read only probe-trial")
    assert list(tmp_path.iterdir()) == []


def test_default_supervisor_adapter_uses_local_request_writer(
    probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []
    monkeypatch.setattr(fleet_migrate, "_local_request", requests.append)
    options = {
        "rehearse": True,
        "session": "probe-trial",
        "state": probe.root / "state",
        "layouts_dir": probe.root / "layouts",
        "observation": lambda: probe.source,
        "run_step": probe.step,
        "inspect_session": probe.inspect,
        "hostname": lambda: "trial-node",
        "pause": lambda _: None,
    }
    for _ in range(3):
        fleet_migrate.migrate(**options)
    assert "next step: verify" in fleet_migrate.migrate(**options)
    assert requests == ["session probe-trial rehearse-probe-trial"]
