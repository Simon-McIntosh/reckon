"""The fleet node's allocation: its script, how it is found, read and placed on.

Scheduler calls are answered by a fake ``subprocess.run`` that records every
argv, so a case asserts the operands a query was written from rather than only
the text the command printed. Nothing here reaches a real scheduler.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import fleet_node
from reckon.crew.fleet_node import (
    ACCOUNT_ENV,
    CPUS_ENV,
    DEFAULT_ACCOUNT,
    FLEET_COMMENT,
    MEMORY_ENV,
    PARTITION_ENV,
    REMAINING_WARNING_SECONDS,
    find_allocation,
    fleet_size,
    generate_hold_script,
    node_is_draining,
    parse_node_state,
    placement_argv,
    remaining_seconds,
)

# The scheduler writes a finite wall clock as digits, colons and an optional
# day separator. Anything else is an unbounded marker, so this matches the
# property "the limit expires" instead of one accepted spelling of "it does not".
_FINITE_WALL_CLOCK = re.compile(r"[0-9][0-9:\-]*")

LOG = "/state/fleet-%j.log"


@pytest.fixture(autouse=True)
def _default_size(monkeypatch, tmp_path) -> None:
    """Every case reads the built-in size and its own empty fleet state.

    The machine's own fleet record names the live allocation, so a case that
    read it would depend on what this host happens to be running.
    """
    for name in (PARTITION_ENV, ACCOUNT_ENV, CPUS_ENV, MEMORY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path / "fleet-state"))


def _record(tmp_path, job: str) -> None:
    """The record a running supervisor publishes, naming the job it runs in."""
    state = tmp_path / "fleet-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "record.json").write_text(json.dumps({"job_id": job, "node": "rigel-04"}))


def _directives(script: str) -> list[str]:
    return [line for line in script.splitlines() if line.startswith("#SBATCH")]


def _directive_value(directives: list[str], name: str) -> str | None:
    prefix = f"#SBATCH --{name}="
    for line in directives:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def test_the_hold_script_requests_an_unbounded_whole_node() -> None:
    size = fleet_size()
    script = generate_hold_script(size, log_path=LOG)
    directives = _directives(script)

    assert _directive_value(directives, "partition") == size.partition
    assert _directive_value(directives, "account") == size.account
    assert _directive_value(directives, "cpus-per-task") == str(size.cpus)
    assert _directive_value(directives, "mem") == size.memory
    assert _directive_value(directives, "output") == LOG
    assert "#SBATCH --nodes=1" in directives
    assert "#SBATCH --exclusive" in directives
    assert f"#SBATCH --comment={FLEET_COMMENT}" in directives
    assert "export TMPDIR=/tmp" in script
    # A debug partition is finite by construction, so the fleet must not land
    # on one.
    assert not size.partition.endswith("_debug")


def test_the_hold_script_runs_the_supervisor_in_the_batch_step() -> None:
    script = generate_hold_script(fleet_size(), log_path=LOG)

    supervisor = script.index('exec "$HOME/.local/bin/fleet-supervisor"')
    assert supervisor < script.index("exec sleep infinity")
    assert '[ -x "$HOME/.local/bin/fleet-supervisor" ]' in script


def test_the_hold_script_requests_no_finite_wall_clock() -> None:
    directives = _directives(generate_hold_script(fleet_size(), log_path=LOG))
    limits = [line for line in directives if line.startswith("#SBATCH --time=")]

    assert len(limits) <= 1
    for line in limits:
        value = line.split("=", 1)[1]
        assert not _FINITE_WALL_CLOCK.fullmatch(value), value


def test_the_hold_script_requests_no_zero_memory() -> None:
    directives = _directives(generate_hold_script(fleet_size(), log_path=LOG))
    # A zero memory request reserves the whole node, so the job's own OOM
    # killer could never act before the node itself runs out.
    assert _directive_value(directives, "mem") != "0"


def test_each_size_field_is_overridable(monkeypatch) -> None:
    monkeypatch.setenv(PARTITION_ENV, "sirius")
    monkeypatch.setenv(ACCOUNT_ENV, "other")
    monkeypatch.setenv(CPUS_ENV, "12")
    monkeypatch.setenv(MEMORY_ENV, "40G")
    directives = _directives(generate_hold_script(fleet_size(), log_path=LOG))

    assert _directive_value(directives, "partition") == "sirius"
    assert _directive_value(directives, "account") == "other"
    assert _directive_value(directives, "cpus-per-task") == "12"
    assert _directive_value(directives, "mem") == "40G"


def test_hold_prints_without_submitting(monkeypatch) -> None:
    def unexpected_submit(_script: str) -> str:
        raise AssertionError("printing the script must not submit a job")

    monkeypatch.setattr(fleet_node, "submit", unexpected_submit)
    result = CliRunner().invoke(main, ["fleet-node", "hold"])

    assert result.exit_code == 0, result.output
    assert f"#SBATCH --comment={FLEET_COMMENT}" in result.output
    assert "#SBATCH --nodes=1" in result.output


def test_hold_submits_the_generated_script_once(monkeypatch, tmp_path) -> None:
    submitted: list[str] = []

    def submit(script: str) -> str:
        submitted.append(script)
        return "1275000"

    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(fleet_node, "submit", submit)
    result = CliRunner().invoke(main, ["fleet-node", "hold", "--submit"])

    assert result.exit_code == 0, result.output
    assert submitted == [
        generate_hold_script(fleet_size(), log_path=tmp_path / "state" / "fleet-%j.log")
    ]
    # cx reads the job id from this line.
    assert re.search(r"[Ss]ubmitted[^0-9]*1275000", result.output), result.output
    assert (tmp_path / "state").is_dir()


# squeue rows in the field order the query asks for: %i|%j|%T|%M|%R|%b|%k|%L
_SERVE_ROW = (
    "1275001|deepseek-v4-flash|RUNNING|3:00:00|gpu-node|gpu:h200:4|null|1-00:00:00"
)

# Two held allocations as the scheduler reports them on two different days.
# Everything except the identifier is identical, so only the pair separates an
# answer assembled from a remembered identifier from a resolved one.
HELD_IDENTIFIERS = ("1275000", "1289017")


def _fleet_row(
    time_left: str, jobid: str = "1275000", comment: str = FLEET_COMMENT
) -> str:
    return (
        f"{jobid}|{comment}|RUNNING|1:05:00|rigel-03|"
        f"billing=28,cpu=28,mem=96G,node=1|{comment}|{time_left}"
    )


def _scheduler(
    rows: str,
    monkeypatch,
    *,
    node_state: str = "IDLE",
    node_returncode: int = 0,
    step_returncode: int = 0,
) -> list[list[str]]:
    """Answer scheduler calls with fixed rows; return every argv handed over."""
    commands: list[list[str]] = []

    def fake_run(command, *args, **kwargs):
        commands.append(list(command))
        if command and command[0] == "scontrol":
            node = command[-1]
            stdout = (
                "" if node_returncode else f"NodeName={node}\n   State={node_state}\n"
            )
            return subprocess.CompletedProcess(command, node_returncode, stdout, "")
        if command and command[0] == "srun":
            return subprocess.CompletedProcess(command, step_returncode, "", "")
        return subprocess.CompletedProcess(command, 0, rows, "")

    monkeypatch.setattr(fleet_node.subprocess, "run", fake_run)
    return commands


def _status() -> object:
    return CliRunner().invoke(main, ["fleet-node", "status"])


def test_status_reports_a_healthy_finite_allocation(monkeypatch) -> None:
    _scheduler(f"{_fleet_row('2:30:00')}\n{_SERVE_ROW}\n", monkeypatch)
    result = _status()

    assert result.exit_code == 0, result.output
    assert "1275000" in result.output
    assert "rigel-03" in result.output
    assert "RUNNING" in result.output
    assert "1:05:00" in result.output
    assert "2h30m" in result.output
    assert "WARNING" not in result.output


def test_status_finds_an_allocation_held_under_the_earlier_comment(monkeypatch) -> None:
    """An allocation submitted before reckon held the fleet is still found."""
    _scheduler(f"{_fleet_row('UNLIMITED', comment='ambix-fleet')}\n", monkeypatch)
    result = _status()

    assert result.exit_code == 0, result.output
    assert "Fleet allocation 1275000" in result.output


def test_status_warns_inside_the_threshold(monkeypatch) -> None:
    _scheduler(f"{_fleet_row('0:10:00')}\n", monkeypatch)
    result = _status()

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "10m" in result.output


def _wall_clock(seconds: int) -> str:
    """Render a second count the way ``squeue %L`` reports a finite limit."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours}:{minutes:02d}:{secs:02d}"


def test_the_warning_tracks_the_configured_threshold(monkeypatch) -> None:
    """Both fixtures derive from the constant, so the boundary moves with it."""
    _scheduler(
        f"{_fleet_row(_wall_clock(max(1, REMAINING_WARNING_SECONDS - 60)))}\n",
        monkeypatch,
    )
    warned = _status()
    assert warned.exit_code == 0, warned.output
    assert "WARNING" in warned.output

    _scheduler(
        f"{_fleet_row(_wall_clock(REMAINING_WARNING_SECONDS + 60))}\n", monkeypatch
    )
    quiet = _status()
    assert quiet.exit_code == 0, quiet.output
    assert "WARNING" not in quiet.output


def test_status_queries_the_account_the_allocation_is_charged_to(monkeypatch) -> None:
    """A job charged to another account is invisible to a filtered query."""
    commands = _scheduler(f"{_fleet_row('2:30:00')}\n", monkeypatch)
    result = _status()

    assert result.exit_code == 0, result.output
    squeue = next(command for command in commands if command[0] == "squeue")
    assert squeue[squeue.index("-A") + 1] == DEFAULT_ACCOUNT


def test_an_unbounded_allocation_warns_nothing_on_a_healthy_node(monkeypatch) -> None:
    _scheduler(f"{_fleet_row('UNLIMITED')}\n", monkeypatch, node_state="ALLOCATED")
    result = _status()

    assert result.exit_code == 0, result.output
    assert "unbounded" in result.output
    assert "WARNING" not in result.output


def test_status_warns_when_the_allocation_node_is_draining(monkeypatch) -> None:
    """An allocation with no wall clock is warned about by its node's drain."""
    _scheduler(f"{_fleet_row('UNLIMITED')}\n", monkeypatch, node_state="DRAINING")
    result = _status()

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "DRAINING" in result.output
    assert "rigel-03" in result.output


@pytest.mark.parametrize(
    "state", ["MIXED+DRAIN+REBOOT_REQUESTED", "ALLOCATED+DRAIN+REBOOT_REQUESTED"]
)
def test_status_warns_on_the_drain_spelling_this_cluster_reports(
    monkeypatch, state
) -> None:
    """The drain token is not the leading one, and the warning still fires."""
    _scheduler(f"{_fleet_row('UNLIMITED')}\n", monkeypatch, node_state=state)
    result = _status()

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output


@pytest.mark.parametrize("state", ["ALLOCATED", "IDLE", "MIXED", "MIXED+RESERVED"])
def test_status_does_not_warn_on_a_healthy_node(monkeypatch, state) -> None:
    _scheduler(f"{_fleet_row('UNLIMITED')}\n", monkeypatch, node_state=state)
    result = _status()

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output


def test_a_pending_reason_is_not_queried_as_a_node(monkeypatch) -> None:
    row = (
        f"1275000|{FLEET_COMMENT}|PENDING|0:00|(Priority)|"
        f"billing=28,cpu=28,mem=96G,node=1|{FLEET_COMMENT}|N/A"
    )
    commands = _scheduler(f"{row}\n", monkeypatch)
    result = _status()

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output
    assert not any(command[0] == "scontrol" for command in commands)


def test_a_failed_node_query_reads_as_unknown(monkeypatch) -> None:
    commands = _scheduler(
        f"{_fleet_row('UNLIMITED')}\n", monkeypatch, node_returncode=1
    )
    result = _status()

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output
    assert any(command[0] == "scontrol" for command in commands)


def test_status_reads_the_allocation_node(monkeypatch) -> None:
    commands = _scheduler(f"{_fleet_row('2:30:00')}\n", monkeypatch)
    _status()

    scontrol = next(command for command in commands if command[0] == "scontrol")
    assert scontrol[-1] == "rigel-03"


def test_status_reports_no_allocation_in_words(monkeypatch) -> None:
    _scheduler(f"{_SERVE_ROW}\n", monkeypatch)
    result = _status()

    assert result.exit_code == 0, result.output
    assert "No fleet allocation is held" in result.output
    assert "1275001" not in result.output


def test_parse_node_state_reads_the_state_field_and_keeps_the_set() -> None:
    assert parse_node_state("NodeName=rigel-03\n   State=IDLE\n") == "IDLE"
    assert (
        parse_node_state("   State=MIXED+DRAIN+REBOOT_REQUESTED\n")
        == "MIXED+DRAIN+REBOOT_REQUESTED"
    )
    assert parse_node_state("NodeName=rigel-03\n") is None


def test_node_is_draining_matches_a_drain_token_anywhere_in_the_set() -> None:
    warned = (
        "DRAIN",
        "DRAINED",
        "DRAINING",
        "DRAINING+NOT_RESPONDING",
        "MIXED+DRAIN+REBOOT_REQUESTED",
        "ALLOCATED+DRAIN+REBOOT_REQUESTED",
    )
    quiet = (
        None,
        "",
        "ALLOCATED",
        "IDLE",
        "MIXED",
        "MIXED+RESERVED",
        "ALLOCATED+MAINTENANCE+RESERVED",
        "ALLOCATED+REBOOT_REQUESTED",
        "RESUME",
        "DOWN",
        "DOWN+NOT_RESPONDING",
    )
    for state in warned:
        assert node_is_draining(state), state
    for state in quiet:
        assert not node_is_draining(state), state


def test_remaining_seconds_removes_the_unbounded_token_from_arithmetic() -> None:
    assert remaining_seconds("UNLIMITED") is None
    assert remaining_seconds("2:30:00") == 9000
    assert remaining_seconds("1-00:00:00") == 86400
    assert remaining_seconds("45:00") == 2700
    assert remaining_seconds("N/A") is None


@pytest.mark.parametrize("jobid", HELD_IDENTIFIERS)
def test_placement_uses_the_identifier_of_the_row_it_matched(jobid) -> None:
    serving = {"jobid": "1275001", "name": "deepseek-v4-flash", "comment": "null"}
    held = {"jobid": jobid, "name": FLEET_COMMENT, "comment": FLEET_COMMENT}

    matched = find_allocation([serving, held])

    assert matched is held
    assert placement_argv(matched, ("hostname",)) == [
        "srun",
        "--overlap",
        f"--jobid={jobid}",
        "hostname",
    ]


@pytest.mark.parametrize("jobid", HELD_IDENTIFIERS)
def test_place_names_the_allocation_it_found(jobid, monkeypatch) -> None:
    commands = _scheduler(f"{_fleet_row('UNLIMITED', jobid=jobid)}\n", monkeypatch)
    result = CliRunner().invoke(main, ["fleet-node", "place", "--", "hostname"])

    assert result.exit_code == 0, result.output
    step = next(command for command in commands if command[0] == "srun")
    assert step == ["srun", "--overlap", f"--jobid={jobid}", "hostname"]


def test_place_queries_the_account_the_allocation_is_charged_to(monkeypatch) -> None:
    commands = _scheduler(f"{_fleet_row('UNLIMITED')}\n", monkeypatch)
    CliRunner().invoke(main, ["fleet-node", "place", "--", "hostname"])

    squeue = next(command for command in commands if command[0] == "squeue")
    assert squeue[squeue.index("-A") + 1] == DEFAULT_ACCOUNT


def test_place_refuses_when_no_allocation_is_held(monkeypatch) -> None:
    """No allocation is a refusal, never a launch on the login node."""
    commands = _scheduler(f"{_SERVE_ROW}\n", monkeypatch)
    result = CliRunner().invoke(main, ["fleet-node", "place", "--", "hostname"])

    assert result.exit_code != 0, result.output
    assert "No fleet allocation is held" in result.output
    assert not any(command[0] == "srun" for command in commands)


def test_place_propagates_the_step_exit_status(monkeypatch) -> None:
    _scheduler(f"{_fleet_row('UNLIMITED')}\n", monkeypatch, step_returncode=3)
    result = CliRunner().invoke(main, ["fleet-node", "place", "--", "hostname"])

    assert result.exit_code == 3, result.output


def test_the_batch_log_sits_in_the_fleet_state_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FLEET_STATE_DIR", str(tmp_path))
    assert fleet_node.batch_log_path() == Path(tmp_path) / "fleet-%j.log"


def test_status_marks_the_allocation_the_record_names(monkeypatch, tmp_path) -> None:
    """Two allocations can carry the comment; the record tells which hosts."""
    _record(tmp_path, "1289017")
    _scheduler(
        f"{_fleet_row('UNLIMITED', jobid='1275000')}\n"
        f"{_fleet_row('UNLIMITED', jobid='1289017')}\n",
        monkeypatch,
    )
    result = _status()

    assert result.exit_code == 0, result.output
    blocks = result.output.split("Fleet allocation ")
    hosting = [block for block in blocks if "hosts" in block]
    assert len(hosting) == 1, result.output
    assert hosting[0].startswith("1289017"), result.output
    assert "2 fleet allocations are held; only 1289017 hosts the sessions." in (
        result.output
    )


def test_place_prefers_the_allocation_the_record_names(monkeypatch, tmp_path) -> None:
    _record(tmp_path, "1289017")
    commands = _scheduler(
        f"{_fleet_row('UNLIMITED', jobid='1275000')}\n"
        f"{_fleet_row('UNLIMITED', jobid='1289017')}\n",
        monkeypatch,
    )
    result = CliRunner().invoke(main, ["fleet-node", "place", "--", "hostname"])

    assert result.exit_code == 0, result.output
    step = next(command for command in commands if command[0] == "srun")
    assert step[2] == "--jobid=1289017", step


def test_place_takes_the_first_allocation_when_no_record_names_one(monkeypatch) -> None:
    commands = _scheduler(
        f"{_fleet_row('UNLIMITED', jobid='1275000')}\n"
        f"{_fleet_row('UNLIMITED', jobid='1289017')}\n",
        monkeypatch,
    )
    result = CliRunner().invoke(main, ["fleet-node", "place", "--", "hostname"])

    assert result.exit_code == 0, result.output
    step = next(command for command in commands if command[0] == "srun")
    assert step[2] == "--jobid=1275000", step
