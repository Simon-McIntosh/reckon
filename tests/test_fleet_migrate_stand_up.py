"""A migration advances from standby allocation to published fleet ownership."""

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import fleet_migrate


def _ledger(state: Path) -> Path:
    path = state / "migration" / "move-test" / "ledger.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "completed": ["census", "layout"],
                "next_step": "stand-up",
                "census": {"sessions": []},
            }
        )
    )
    (state / "record.json").write_text(
        json.dumps({"job_id": "old", "node": "old-node"})
    )
    return path


class Actions:
    def __init__(self, state: Path, *, new_node: str = "new-node") -> None:
        self.state = state
        self.new_node = new_node
        self.calls: list[str] = []
        self.reservation = {"job_id": "old"}

    def submit(self, script: str) -> str:
        assert 'fleet-supervisor" --standby' in script
        self.calls.append("submit")
        return "new"

    def jobs(self, _account: str) -> list[dict[str, str]]:
        self.calls.append("jobs")
        return [{"jobid": "new", "state": "RUNNING", "node": self.new_node}]

    def send(self, _job: dict[str, str], line: str) -> None:
        self.calls.append(line.split(maxsplit=1)[0])
        if line.startswith("ready "):
            token = line.split()[1]
            (self.state / "migration" / f"ready-{token}.json").write_text(
                json.dumps(
                    {
                        "job_id": "new",
                        "node": self.new_node,
                        "standby": True,
                        "ready_at": "now",
                    }
                )
            )
        elif line == "promote":
            (self.state / "record.json").write_text(
                json.dumps({"job_id": "new", "node": self.new_node})
            )

    def step(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append("step")
        assert argv[:3] == ["srun", "--overlap", "--jobid=new"]
        assert argv[-2:] == ["hostname", "-s"]
        return subprocess.CompletedProcess(argv, 0, self.new_node + "\n", "")

    def replace(self, *, job_id: str) -> dict[str, str]:
        self.calls.append("replace")
        assert job_id == "new"
        self.reservation = {"job_id": job_id, "replaced": "old"}
        return {"detail": "replaced"}

    def reservation_record(self) -> dict[str, str]:
        return self.reservation.copy()

    def migrate(self, **kwargs: object) -> str:
        return fleet_migrate.migrate(
            state=self.state,
            submit_hold=self.submit,
            query_jobs=self.jobs,
            run_step=self.step,
            send_supervisor=self.send,
            replace_reservation=self.replace,
            read_reservation=self.reservation_record,
            pause=lambda _seconds: None,
            **kwargs,
        )


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Actions]:
    state = tmp_path / "state"
    path = _ledger(state)
    commands = tmp_path / "commands"
    commands.mkdir()
    for command in ("sbatch", "srun", "scancel"):
        stub = commands / command
        stub.write_text("#!/bin/sh\nexit 91\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands))
    return path, Actions(state)


def test_stand_up_then_promote_resumes_from_ledger_with_injected_actions(setup):
    path, actions = setup
    before = path.read_bytes()
    assert actions.migrate(dry_run=True).startswith("stand-up: submit a standby hold")
    assert path.read_bytes() == before
    assert actions.calls == []

    result = actions.migrate()
    assert "Standby job new runs on new-node" in result
    assert "step ran on new-node" in result
    ledger = json.loads(path.read_text())
    assert ledger["completed"] == ["census", "layout", "stand-up"]
    assert ledger["next_step"] == "promote"
    assert ledger["stand_up"]["readiness"]["standby"] is True
    assert ledger["stand_up"]["step_node"] == "new-node"
    assert actions.calls == ["submit", "jobs", "ready", "step"]

    before = path.read_bytes()
    assert actions.migrate(dry_run=True).startswith("promote: replace")
    assert path.read_bytes() == before
    assert actions.calls == ["submit", "jobs", "ready", "step"]

    result = actions.migrate()
    assert 'fleet before: {"job_id": "old", "node": "old-node"}' in result
    assert 'fleet after: {"job_id": "new", "node": "new-node"}' in result
    assert 'reservation before: {"job_id": "old"}' in result
    assert 'reservation after: {"job_id": "new", "replaced": "old"}' in result
    assert actions.calls == [
        "submit",
        "jobs",
        "ready",
        "step",
        "jobs",
        "replace",
        "promote",
    ]
    ledger = json.loads(path.read_text())
    assert ledger["completed"] == ["census", "layout", "stand-up", "promote"]
    assert ledger["next_step"] == "cutover"


def test_stand_up_refuses_a_job_on_the_old_node(setup):
    path, actions = setup
    actions.new_node = "old-node"
    with pytest.raises(fleet_migrate.MigrationError, match="old job runs on old-node"):
        actions.migrate()
    assert actions.calls == ["submit", "jobs"]
    ledger = json.loads(path.read_text())
    assert ledger["next_step"] == "stand-up"
    assert ledger["stand_up"]["job_id"] == "new"
    actions.new_node = "new-node"
    assert "next step: promote" in actions.migrate()
    assert actions.calls.count("submit") == 1


def test_retry_requires_a_fresh_supervisor_readiness_response(setup):
    path, actions = setup
    actions.step = lambda argv: subprocess.CompletedProcess(argv, 1, "", "step failed")
    with pytest.raises(fleet_migrate.MigrationError, match="did not confirm node"):
        actions.migrate()
    first_token = json.loads(path.read_text())["stand_up"]["ready_token"]
    assert (actions.state / "migration" / f"ready-{first_token}.json").exists()
    actions.step = lambda argv: subprocess.CompletedProcess(argv, 0, "new-node\n", "")
    actions.send = lambda _job, _line: None
    with pytest.raises(fleet_migrate.MigrationError, match="readiness"):
        actions.migrate()
    assert json.loads(path.read_text())["stand_up"]["ready_token"] != first_token
    assert json.loads(path.read_text())["next_step"] == "stand-up"


def test_promote_retry_preserves_records_from_before_the_first_attempt(setup):
    path, actions = setup
    actions.migrate()
    send = actions.send

    def interrupted(_job, line):
        if line == "promote":
            raise fleet_migrate.MigrationError("request interrupted")
        send(_job, line)

    actions.send = interrupted
    with pytest.raises(fleet_migrate.MigrationError, match="request interrupted"):
        actions.migrate()
    ledger = json.loads(path.read_text())
    assert ledger["next_step"] == "promote"
    assert ledger["promotion"]["reservation_before"] == {"job_id": "old"}
    assert actions.reservation["job_id"] == "new"
    actions.send = send
    result = actions.migrate()
    assert 'reservation before: {"job_id": "old"}' in result
    assert json.loads(path.read_text())["next_step"] == "cutover"
    assert actions.calls.count("submit") == 1


@pytest.mark.parametrize("step", ["stand-up", "promote"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_session_is_refused_before_either_step_changes_state(setup, step, dry_run):
    path, actions = setup
    if step == "promote":
        ledger = json.loads(path.read_text())
        ledger["next_step"] = "promote"
        ledger["stand_up"] = {"job_id": "new", "node": "new-node"}
        path.write_text(json.dumps(ledger))
    before = path.read_bytes()
    with pytest.raises(fleet_migrate.MigrationError, match="cutover step"):
        actions.migrate(session="one-fleet", dry_run=dry_run)
    assert path.read_bytes() == before
    assert actions.calls == []
