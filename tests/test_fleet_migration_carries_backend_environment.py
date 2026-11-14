"""A fleet hold carries the environment its backends need, whoever submits it.

``sbatch`` hands the batch step the submitting shell's environment, and a
non-interactive shell returns from its startup files before the block that
adds an installed backend's directory. So a hold submitted from such a shell
starts every agent session without the directory the flight declares for that
backend, and each dispatch from a session on the new node fails to find the
command. The hold composes the declared directories into the PATH its sessions
and services start with, and a migration refuses to switch to an allocation
where a declared command is still missing.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import fleet_migrate, fleet_node

BACKEND = "stub-lane"
COMMAND = "stub-backend"
SUBMITTING_PATH = "/usr/bin:/bin"


def _write_flight(tmp_path: Path, *, search_path: list[Path]) -> Path:
    directory = tmp_path / "flight"
    directory.mkdir(parents=True, exist_ok=True)
    entries = "".join(f"      - {entry}\n" for entry in search_path)
    layer = directory / "flight.yaml"
    layer.write_text(
        "version: 1\n"
        "backends:\n"
        f"  {BACKEND}:\n"
        "    launch: cli\n"
        f"    command: {COMMAND}\n"
        "    search_path:\n" + entries,
        encoding="utf-8",
    )
    return layer


def _write_stub(directory: Path, name: str = COMMAND) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _hold_path_from_script(script: str) -> str:
    """The PATH the hold's own batch step exports to its services."""
    for line in script.splitlines():
        words = shlex.split(line)
        if len(words) >= 2 and words[0] == "export" and words[1].startswith("PATH="):
            return words[1].split("=", 1)[1]
    raise AssertionError(f"the hold script exports no PATH:\n{script}")


@pytest.fixture()
def declared_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "backend-bin"
    _write_stub(bindir)
    layer = _write_flight(tmp_path, search_path=[bindir])
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))
    monkeypatch.setenv("PATH", SUBMITTING_PATH)
    return bindir


def test_a_hold_from_a_bare_submitter_resolves_a_declared_backend(
    declared_backend: Path, tmp_path: Path
) -> None:
    """The stub resolves under the PATH the hold's services start with."""
    script = fleet_node.generate_hold_script(
        fleet_node.fleet_size(),
        log_path=tmp_path / "state" / "fleet-%j.log",
        environ={"PATH": SUBMITTING_PATH},
    )

    started_with = _hold_path_from_script(script)
    assert str(declared_backend) in started_with.split(os.pathsep)
    assert shutil.which(COMMAND, path=SUBMITTING_PATH) is None
    assert shutil.which(COMMAND, path=started_with) == str(declared_backend / COMMAND)


def test_a_hold_keeps_the_submitters_own_path_ahead_of_the_declared_one(
    declared_backend: Path, tmp_path: Path
) -> None:
    """An entry the submitter already holds is not displaced by the declaration."""
    script = fleet_node.generate_hold_script(
        fleet_node.fleet_size(),
        log_path=tmp_path / "state" / "fleet-%j.log",
        environ={"PATH": f"{declared_backend}:{SUBMITTING_PATH}"},
    )

    entries = _hold_path_from_script(script).split(os.pathsep)
    assert entries == [str(declared_backend), "/usr/bin", "/bin"]


# --- the migration's pre-switch check -------------------------------------


def _promote_ledger(state: Path) -> Path:
    """A migration ledger paused one step before it names the new allocation."""
    path = state / "migration" / "move-test" / "ledger.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "completed": ["census", "layout", "stand-up"],
                "next_step": "promote",
                "census": {"sessions": []},
                "stand_up": {
                    "job_id": "new",
                    "node": "new-node",
                    "old_record": {"job_id": "old", "node": "old-node"},
                },
            }
        )
    )
    (state / "record.json").write_text(
        json.dumps({"job_id": "old", "node": "old-node"})
    )
    return path


class Actions:
    """The scheduler and supervisor seams the migration reaches."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.calls: list[str] = []
        self.reservation: dict[str, Any] = {"job_id": "old"}

    def jobs(self, _account: str) -> list[dict[str, str]]:
        self.calls.append("jobs")
        return [{"jobid": "new", "state": "RUNNING", "node": "new-node"}]

    def send(self, _job: dict[str, str], line: str) -> None:
        self.calls.append(line.split(maxsplit=1)[0])
        if line == "promote":
            (self.state / "record.json").write_text(
                json.dumps({"job_id": "new", "node": "new-node"})
            )

    def step(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"the promote step runs no allocation step: {argv}")

    def replace(self, *, job_id: str) -> dict[str, str]:
        self.calls.append("replace")
        self.reservation = {"job_id": job_id}
        return {"detail": "replaced"}

    def reservation_record(self) -> dict[str, Any]:
        return self.reservation.copy()

    def migrate(self, **kwargs: object) -> str:
        return fleet_migrate.migrate(
            state=self.state,
            submit_hold=lambda _script: "new",
            query_jobs=self.jobs,
            run_step=self.step,
            send_supervisor=self.send,
            replace_reservation=self.replace,
            read_reservation=self.reservation_record,
            pause=lambda _seconds: None,
            **kwargs,
        )


def test_the_migration_refuses_a_switch_whose_allocation_misses_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check names the command and the allocation; the record is unchanged."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    layer = _write_flight(tmp_path, search_path=[empty])
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(layer))
    monkeypatch.setenv("PATH", SUBMITTING_PATH)
    state = tmp_path / "state"
    _promote_ledger(state)
    record = state / "record.json"
    before = record.read_bytes()
    actions = Actions(state)

    with pytest.raises(fleet_migrate.MigrationError) as raised:
        actions.migrate()

    message = str(raised.value)
    assert COMMAND in message
    assert "new" in message
    assert "new-node" in message
    assert record.read_bytes() == before
    assert actions.calls == ["jobs"]


def test_the_migration_switches_when_the_allocation_finds_every_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared_backend: Path
) -> None:
    """The same step proceeds when the declared directory carries the command."""
    state = tmp_path / "state"
    _promote_ledger(state)
    actions = Actions(state)

    result = actions.migrate()

    assert "fleet after:" in result
    assert json.loads((state / "record.json").read_text())["job_id"] == "new"
