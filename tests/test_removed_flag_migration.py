"""The removed attention flag lets an armed follower reconnect, never narrows.

The follower's state filter was removed; the ``--attention`` option went with
it, and a follower that reconnects by re-running the arming line it was given
died at the new option parser with "No such option --attention" and exit 2 —
a failed monitor where a removed flag would have told the operator more. The
shim keeps the flag as a deprecated no-op: it parses, changes nothing about
what is delivered, names itself as gone on stderr, and forwards nothing into
the delivery path. A follower passing the flag must see exactly what one
omitting it sees, so the assertions compare both readers against the same
transitions rather than merely checking that the flag is accepted.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew
from reckon.crew import runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The date the deprecated flag stops being accepted, stated in the deprecation
# line so the shim has an end rather than becoming permanent.
ATTENTION_REMOVAL_HORIZON = "2027-06-30"

# The one stderr line the armed follower receives, bound to the command's own
# string so the announcement and the test cannot drift apart silently.
_DEPRECATION_LINE = cli._ATTENTION_DEPRECATION


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep registrations and pointer state in temporary storage."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


# One session's fleet spread as a live run would produce it: a baseline start,
# a working transition, and a delivered completion. The old filter would have
# narrowed this set to the completion; the current follower must deliver all of
# it, --attention or not.
_TRANSITIONS = (
    {
        "event": "baseline",
        "run_id": "r-one",
        "to_state": "dispatched",
        "node": "one-node",
        "session": "mine",
        "observed_at": "2026-09-06T10:00:00Z",
    },
    {
        "event": "transition",
        "run_id": "r-one",
        "to_state": "working",
        "node": "one-node",
        "session": "mine",
        "observed_at": "2026-09-06T10:00:01Z",
    },
    {
        "event": "transition",
        "run_id": "r-one",
        "to_state": "complete",
        "node": "one-node",
        "session": "mine",
        "observed_at": "2026-09-06T10:00:02Z",
    },
)


class _FollowerStoppedError(Exception):
    """Unwind the never-exiting follower once its transitions are delivered."""


def _bounded_follower(calls: list[dict]):
    """Stand in for the watch generator: deliver the set, then unwind.

    The follower is not supposed to end, so a bounded test has to stop it.
    Recording the keyword arguments is what makes the no-op honest: the flag
    must not resurface as a routing argument handed into the generator.
    """

    def follow(project: str, **kwargs):
        calls.append({"project": project, "kwargs": dict(kwargs)})
        yield from _TRANSITIONS
        raise _FollowerStoppedError()

    return follow


def _delivered_rows(result) -> list[dict]:
    return [json.loads(line) for line in result.output.splitlines()]


def _routing(kwargs: dict) -> dict:
    """The follower's selection inputs, minus the caller's callback objects."""
    return {key: value for key, value in kwargs.items() if key != "on_poll"}


def _capture_stderr(monkeypatch) -> list[str]:
    """Divert the command's stderr line into a list.

    Click's test runner mixes the two channels into one buffer in this
    version, so a test that must keep them apart records the err=True writes
    here and asserts the delivered stream stays pure JSON.
    """
    writes: list[str] = []
    real_echo = cli.click.echo

    def recording(message=None, **kwargs):
        if kwargs.get("err"):
            writes.append("" if message is None else str(message))
        else:
            real_echo(message, **kwargs)

    monkeypatch.setattr(cli.click, "echo", recording)
    return writes


def test_the_removed_flag_reconnects_instead_of_exiting_two(home, monkeypatch) -> None:
    """A follower armed before the removal re-runs its line and survives.

    Before the shim, the same invocation died at the option parser: the arming
    line the follower was given carries --attention, and the parser no longer
    owned the option. Now the flag parses, the follower's body runs, and the
    only trace of the flag is the deprecation line, written down the stderr
    channel — never over the stream a machine reader parses.
    """
    calls: list[dict] = []
    stderr_writes = _capture_stderr(monkeypatch)
    monkeypatch.setattr(cli, "_follow_watch_lines", _bounded_follower(calls))
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "crew",
            "follow",
            "--project",
            "proj",
            "--session",
            "mine",
            "--json",
            "--attention",
        ],
    )

    assert isinstance(result.exception, _FollowerStoppedError), (
        "the flag must parse and the follower body must run, not exit 2"
    )
    assert "No such option" not in result.output
    assert "Did you mean" not in result.output
    assert stderr_writes == [_DEPRECATION_LINE]
    assert "no longer filters" in stderr_writes[0]
    assert "every transition" in stderr_writes[0]
    assert ATTENTION_REMOVAL_HORIZON in stderr_writes[0], (
        "the deprecation states when the flag stops being accepted"
    )
    assert _delivered_rows(result) == [
        {"ok": True, **event} for event in _TRANSITIONS
    ], "the delivered stream is rows alone, with no deprecation prose among them"
    assert "attention" not in calls[0]["kwargs"], (
        "the deprecated flag must not be routed into the delivery path"
    )


def test_an_armed_follower_delivers_the_same_rows_as_an_unarmed_one(
    home, monkeypatch
) -> None:
    """Both invocations see the same transitions; the flag changes nothing.

    The comparison is over the delivered stream, and the streams must be
    identical, with every one of the transitions present in each — an armed
    follower is not a narrower reader than an unarmed one. The unarmed
    invocation receives no deprecation line at all.
    """
    calls: list[dict] = []
    stderr_writes = _capture_stderr(monkeypatch)
    monkeypatch.setattr(cli, "_follow_watch_lines", _bounded_follower(calls))
    runner = CliRunner()
    armed = runner.invoke(
        cli.main, ["crew", "follow", "--project", "proj", "--json", "--attention"]
    )
    bare = runner.invoke(cli.main, ["crew", "follow", "--project", "proj", "--json"])

    assert both_unwound(armed, bare)
    assert armed.output == bare.output
    rows = _delivered_rows(armed)
    assert [row["to_state"] for row in rows] == ["dispatched", "working", "complete"]
    assert _routing(calls[0]["kwargs"]) == _routing(calls[1]["kwargs"]), (
        "the flag must leave the follower's routing identical"
    )
    assert stderr_writes[0] == _DEPRECATION_LINE, "the armed follower is told, once"
    assert not any("no longer filters" in line for line in stderr_writes[1:]), (
        "the bare follower is told nothing"
    )


def both_unwound(*results) -> bool:
    return all(
        isinstance(result.exception, _FollowerStoppedError) for result in results
    )


def test_the_deprecation_announcement_reaches_stderr_and_not_the_stream(
    home, tmp_path
) -> None:
    """The real command, real channels: rows on stdout, the notice on stderr.

    A monitor consumes the follower's stdout as the machine stream, so the
    deprecated flag must leave that stream untouched while still telling the
    operator on stderr. The test runner mixes the two channels in this Click
    version, so this runs the actual command as a subprocess with the channels
    sent to separate files — the way production separates them — and reads
    each after a bounded run.
    """
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    _write_pointer(home, "r-one", "one-node", session="mine", phase="working")
    lock = runs.watch_lock_path("proj")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        runs._write_watch_record(
            handle,
            {
                "project": "proj",
                "pid": _os.getpid(),
                "pid_start_time": runs._process_start_time(_os.getpid()),
                "parent_pid": _os.getppid(),
                "parent_start_time": runs._process_start_time(_os.getppid()),
                "started_at": runs._utc_now(),
            },
        )

    out_file = tmp_path / "out.jsonl"
    err_file = tmp_path / "err.txt"
    env = {
        **_os.environ,
        "RECKON_HOME": str(home),
        "PYTHONPATH": str(Path(runs.__file__).resolve().parents[1]),
    }
    with out_file.open("w") as out, err_file.open("w") as err:
        proc = _subprocess.Popen(
            [
                _sys.executable,
                "-u",
                "-c",
                "from reckon.cli import main; main()",
                "crew",
                "follow",
                "--project",
                "proj",
                "--json",
                "--attention",
            ],
            stdout=out,
            stderr=err,
            env=env,
        )
        try:
            time.sleep(1.5)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    stream = out_file.read_text()
    notice = err_file.read_text()
    rows = [json.loads(line) for line in stream.splitlines() if line.strip()]
    assert rows, "the real follower delivered at least the baseline row"
    assert all(
        "no longer filters" not in row for row in stream.splitlines() if row.strip()
    ), "the machine stream carries rows, never the announcement"
    assert "no longer filters" in notice
    assert ATTENTION_REMOVAL_HORIZON in notice


def test_armed_and_bare_followers_register_the_same_scope(home, monkeypatch) -> None:
    """The registration records no attention key, in either invocation.

    The old filtering routed through a scope key that recorded the flag; the
    scope must stay a plain run list so nothing about a follower that passed
    the flag survives to be acted on.
    """
    scopes: list[dict] = []
    real_registration = runs.follower_registration

    def recording(project: str, session: str, *, delivery=None, scope=None):
        scopes.append(scope)
        return real_registration(project, session, delivery=delivery, scope=scope)

    monkeypatch.setattr(runs, "follower_registration", recording)
    monkeypatch.setattr(cli, "_follow_watch_lines", _bounded_follower([]))
    runner = CliRunner()
    armed = runner.invoke(
        cli.main,
        ["crew", "follow", "--project", "proj", "--session", "mine", "--attention"],
    )
    bare = runner.invoke(
        cli.main, ["crew", "follow", "--project", "proj", "--session", "mine"]
    )

    assert both_unwound(armed, bare)
    assert scopes == [{"runs": []}, {"runs": []}]
    assert all("attention" not in scope for scope in scopes)


def test_only_the_follow_command_carries_the_deprecated_flag() -> None:
    """The shim belongs to the arming line alone; no sibling command gains it."""
    for name, command in cli.crew.commands.items():
        options = [
            option for param in command.params for option in getattr(param, "opts", ())
        ]
        if name == "follow":
            assert "--attention" in options
        else:
            assert "--attention" not in options, (
                f"{name} must not grow the deprecated flag"
            )


# ── The delivery path itself carries no filter ──────────────────────────────


def _write_pointer(home: Path, run_id: str, node: str, *, session: str, phase: str):
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "session": session,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
        },
    )


def _deliver(home: Path, run_id: str, status: str) -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: n\nstatus: {status}\ncommits: HEAD\nblockers: none\n")


def _wait_for(predicate, *, timeout: float = WATCHER_LOAD_BOUND_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("the follower did not receive the expected transition in time")


def test_the_delivery_path_itself_carries_no_state_filter(home) -> None:
    """A real follower delivers every state of the feed it reads.

    The no-op above is only honest if the generator it feeds is unfiltered: a
    hidden seam that narrowed delivery would survive a flag that is not
    threaded into it. One reader over a fleet that moves start to completion
    must report each state.
    """
    _write_pointer(home, "r-one", "one-node", session="mine", phase="starting")
    _write_pointer(home, "r-two", "two-node", session="mine", phase="working")
    received: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        for event in cli._follow_watch_lines(
            "proj", session="mine", poll_interval=0.001, stop=stop
        ):
            if event.get("event") in {"attached", "reattached"}:
                continue
            received.append(event)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        with runs._project_watch_claim("proj", "1h"):
            crew.list_live(project="proj")
            _wait_for(lambda: any(e["to_state"] == "working" for e in received))
            _deliver(home, "r-one", "complete")
            crew.list_live(project="proj")
            _wait_for(lambda: any(e["to_state"] == "complete" for e in received))
    finally:
        stop.set()
        thread.join(timeout=2)

    assert sorted(event["to_state"] for event in received) == [
        "complete",
        "dispatched",
        "working",
    ]
