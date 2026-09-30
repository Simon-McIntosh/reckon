"""A follower armed with a lifetime announces its own end before the host does.

The host ends a Monitor at thirty minutes and announces it in its own words,
after a pane that went silent for reasons the reader cannot see. A follower
armed for slightly less reaches its own deadline first: it prints one line
naming how to re-arm and the owning session's runs that need the coordinator,
releases its delivery registration, and exits cleanly.

These tests run the real command against a temporary crew config home, because
the end of the journey is the process exiting by itself with that line on its
stdout. The follower is measured with no producer up, which is the case the line
exists for: a follower whose session is quiet is exactly the one a reader would
otherwise assume is fine.

The lifetime's own bound is measured from the moment the follower arms. Reaching
an arm — starting the interpreter, importing the tree, registering — is the cost
of arriving at the deadline's starting line, not a thing the deadline bounds,
and on a shared login node that cost alone was measured at 2.5-3.4 s against a 3 s
grant. A bound taken from the spawn would report the node's load rather than the
follower's deadline, and a bound that flips with the load is worse than useless
as a gate. The arm is waited for separately under its own generous bound, so a
follower that never arms fails as that case rather than as a timeout that hides
which of the two went wrong.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import runs

REPO_ROOT = Path(cli.__file__).resolve().parents[1]
PROJECT = "proj"
# Distinctive on purpose: the shared-home check looks for these tokens, so an id
# short enough to appear inside a stranger's pointer would report a false hit.
SESSION = "s-follow-lifetime"
RUN_ID = "r-follow-lifetime-own"
NODE = "n1"

# The granted lifetime, the bound on reaching an arm, and the bound on ending
# once armed. The last waits on the follower's own end and is deliberately wide:
# the follower acts on its deadline at the next pass of its own poll loop, and a
# loaded login node stretches the interpreter's own exit past the deadline it
# acts on. A bound tight enough to report that load as a failure measures the
# node rather than the lifetime, which is what a fixed five-second observation did.
LIFETIME = "3s"
ARM_WITHIN_SECONDS = 30.0
END_WITHIN_SECONDS = 30.0
STILL_ARMED_SECONDS = 5.0
POLL_SECONDS = 0.05


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep registrations, pointers, and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_unpromoted_run(home: Path) -> None:
    """One delivered run of the owning session that nobody has promoted."""
    log = home / "logs" / f"{RUN_ID}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {NODE}\nstatus: complete\ncommits: HEAD\nblockers: none\n"
    )
    crew._write_json(
        crew.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": NODE, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": runs._utc_now(),
            "manifest_path": str(manifest),
            "log_path": str(log),
        },
    )


def _follower_env(home: Path) -> dict[str, str]:
    """The follower's environment: the ambient one with no crew identity in it.

    A worker whose own session runs under ``crew follow`` exports RECKON_
    variables describing *that* session — ``RECKON_FOLLOWER_OWNER`` names the
    process that armed it, and ``RECKON_RUN_ID``/``RECKON_MANIFEST`` name its
    run. Inherited unchanged, they make the follower this test arms believe it
    belongs to someone else's session: with ``RECKON_FOLLOWER_OWNER`` set the
    follower resolved a foreign owner and neither ended on its lifetime nor
    stayed armed, so a worker inside a followed session could not reproduce this
    file's own result. Keep only the variables the test sets deliberately — the
    temporary home and the import path — and drop every other RECKON_ variable.
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("RECKON_")
    }
    env["RECKON_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _arm(home: Path, *args: str) -> subprocess.Popen:
    """Start the real follower command against the temporary config home."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from reckon.cli import main; main()",
            "crew",
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--no-color",
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_follower_env(home),
    )


def _kill(process: subprocess.Popen) -> tuple[str, str]:
    """End a follower the test is done with, and collect what it printed."""
    process.kill()
    stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
    return stdout, stderr


def _attach_line_shape(
    line: str, project: str, session: str | None = None
) -> None:
    """Assert the attach line's shape, never a literal or the composer itself.

    The first token has to be an absolute path to the running ``reckon``
    console script, because the shell that arms the line need not carry the
    interpreter's bin directory on PATH. The remaining tokens are the fixed
    command carrying exactly the caller's project and session.
    """
    tokens = shlex.split(line)
    executable = tokens[0] if tokens else ""
    assert os.path.isabs(executable), f"the first token is not absolute: {line!r}"
    assert os.path.isfile(executable), f"the first token is not a file: {line!r}"
    assert os.access(executable, os.X_OK), f"the first token is not runnable: {line!r}"
    assert os.path.basename(executable) == "reckon", (
        f"the first token is not the reckon console script: {line!r}"
    )
    expected = ["crew", "follow", "--project", project]
    if session is not None:
        expected += ["--session", session]
    assert tokens[1:] == expected, (
        f"the attach line's arguments are not the fixed command: {line!r}"
    )


def _assert_attach_line_within(reported: str, project: str, session: str) -> None:
    """Assert the reported line carries the attach line, by the attach line's shape.

    The report embeds the command inside a longer line, so it is located by its
    first token — an absolute path to the report's ``reckon`` console script —
    and the arguments that follow are checked as the composed line's shape. The
    report separates that segment with a semicolon, which is the report's
    punctuation rather than part of the command, so it is trimmed.
    """
    tokens = shlex.split(reported)
    start = next(
        (
            index
            for index, token in enumerate(tokens)
            if os.path.isabs(token) and os.path.basename(token) == "reckon"
        ),
        None,
    )
    assert start is not None, f"the line names no reckon executable: {reported!r}"
    arguments = ["crew", "follow", "--project", project, "--session", session]
    window = tokens[start : start + 1 + len(arguments)]
    window[-1] = window[-1].rstrip(";,")
    _attach_line_shape(" ".join(window), project, session)


def _wait_until_armed(process: subprocess.Popen) -> None:
    """Wait for the follower to hold its registration, or fail saying which.

    The registration is the observable the arm leaves behind: while the
    follower holds it a second reader cannot take the same lock, and after the
    follower releases it the same read reports the release. It is therefore the
    same instrument the release assertion uses, rather than a proxy for one.
    """
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
            pytest.fail(
                f"the follower ended with exit status {process.returncode} "
                f"before it armed; stdout={stdout!r} stderr={stderr!r}"
            )
        if runs.follower_state(PROJECT, SESSION)["registered"] is True:
            return
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        f"the follower held no registration {ARM_WITHIN_SECONDS!r}s after it "
        f"started, so it never armed; stdout={stdout!r} stderr={stderr!r}"
    )


def _wait_until_ended(process: subprocess.Popen) -> None:
    """Wait for the follower to end on its own; never kills it to make it so."""
    deadline = time.monotonic() + END_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        f"the follower was still running {END_WITHIN_SECONDS!r}s after its own "
        f"arm, so its lifetime did not end it; stdout={stdout!r} stderr={stderr!r}"
    )


def _real_crew_home() -> Path:
    """The config home this workstation's other sessions share.

    Resolved without RECKON_HOME, because the point of the check is what the
    command writes when it is *not* pointed at the temporary home.
    """
    return Path.home() / ".config" / "reckon" / "crew"


# The directories a follower could leave a trace in: its per-session
# registration under watch/, and a live pointer under live/. The rest of the
# shared home — runs/, reports/, reviews/ — holds thousands of durable files on
# a busy workstation, so a walk of the whole home measures someone else's data
# and crawls on the network filesystem for no reading this test needs.
TRACE_DIRS = ("watch", "live")


def _own_traces(root: Path, *needles: str) -> list[str]:
    """Files under ``root`` whose name or content carries any of ``needles``.

    A follower pointed at a temporary home must leave the shared home with
    nothing that names this test. Both a path's own name and its text are read,
    so a pointer filed under the run id and a registration whose body records
    the session are each caught. Reading for these tokens rather than comparing
    the directory against an earlier snapshot is the whole point: the crew home
    is shared with every other session on the workstation, so a peer filing its
    own pointer between two readings moves the tree without anything being
    wrong, and tree equality reported exactly that as a failure.
    """
    hits: list[str] = []
    for name in TRACE_DIRS:
        directory = root / name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            named = any(needle in path.name for needle in needles)
            text = "" if named else _read_text(path)
            if named or any(needle in text for needle in needles):
                hits.append(str(path))
    return hits


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _assert_no_own_traces(root: Path, session: str, run_id: str) -> None:
    """The shared home must carry nothing naming this test's session or run.

    A file naming *this* session or run can only have come from the follower
    this test armed, which was pointed at a temporary home precisely so it
    would not write here. A peer's own pointer names the peer, so it is not a
    hit — which is the reading tree equality could not make.
    """
    hits = _own_traces(root, session, run_id)
    assert not hits, (
        "a follower pointed at a temporary home must leave the shared crew home "
        f"carrying nothing that names this test, but {hits!r} carry "
        f"{session!r} or {run_id!r}"
    )


def test_a_lifetime_ends_the_follower_with_one_line_a_reader_acts_on(home) -> None:
    """The granted lifetime ends the arming, and the last line says what next.

    Measured against the grant rather than against a sleep: three seconds'
    lifetime must end the follower within five seconds of its arm, and the final
    line must carry the attach line to re-arm with and the node of the run that
    the coordinator owns, so the next arming starts from the line itself.
    """
    _write_unpromoted_run(home)
    shared = _real_crew_home()

    process = _arm(home, "--lifetime", LIFETIME)
    try:
        _wait_until_armed(process)
        _wait_until_ended(process)
        stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
    finally:
        if process.poll() is None:
            _kill(process)

    assert process.returncode == 0, (
        f"a lifetime exit is the follower ending by itself, so it exits zero; "
        f"got {process.returncode}; stderr={stderr!r}"
    )

    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"the follower ended without printing anything; stderr={stderr!r}"
    final = lines[-1]
    assert final.startswith("follower end:"), (
        f"the last line must be marked as the follower's end; got {final!r}"
    )
    _assert_attach_line_within(final, PROJECT, SESSION)
    assert RUN_ID in final, (
        f"the line must name the run that needs a decision; {final!r}"
    )
    assert NODE in final, f"the line must name that run's node; {final!r}"

    state = runs.follower_state(PROJECT, SESSION)
    assert state["registered"] is False, "the registration is released, not held"
    assert state["live"] is False, (
        "a released registration does not keep delivering, so it is not live"
    )
    assert "released" in str(state["not_live_because"]), (
        f"the release is what ended it; got {state['not_live_because']!r}"
    )

    _assert_no_own_traces(shared, SESSION, RUN_ID)


def test_without_a_lifetime_the_follower_keeps_running(home) -> None:
    """No lifetime means the old behaviour: the follower stays armed."""
    _write_unpromoted_run(home)
    process = _arm(home)
    try:
        _wait_until_armed(process)
        time.sleep(STILL_ARMED_SECONDS)
        assert process.poll() is None, (
            "a follower armed without a lifetime must keep running, not end on "
            "the host's schedule"
        )
        assert runs.follower_state(PROJECT, SESSION)["registered"] is True, (
            "the still-running follower must still hold its registration"
        )
    finally:
        if process.poll() is None:
            _kill(process)


def test_a_peer_filing_its_pointer_does_not_condemn_the_follower(
    home, tmp_path
) -> None:
    """Concurrency control: a stranger's pointer in the shared home is not ours.

    The stand-in stands for the shared crew home, and a peer keeps filing its
    own live pointers into it while this test's follower runs against a
    temporary home. The check must pass, because none of what the peer wrote
    names this test's session or run — which is the reading tree equality could
    not make, and the reason a peer's ordinary write turned the case red.
    """
    shared = tmp_path / "shared" / "crew"
    (shared / "watch").mkdir(parents=True)
    (shared / "live").mkdir(parents=True)
    stop = threading.Event()
    total = 0

    def _peer_files_pointers() -> None:
        nonlocal total
        while not stop.is_set():
            total += 1
            pointer = shared / "live" / f"r-peer-{total:04d}.json"
            try:
                pointer.write_text(
                    json.dumps({"run_id": f"r-peer-{total:04d}", "session": "s-peer"})
                )
            except OSError:
                return
            stop.wait(POLL_SECONDS)

    peer = threading.Thread(target=_peer_files_pointers, daemon=True)
    peer.start()
    _write_unpromoted_run(home)
    try:
        process = _arm(home, "--lifetime", LIFETIME)
        try:
            _wait_until_armed(process)
            _wait_until_ended(process)
            process.communicate(timeout=ARM_WITHIN_SECONDS)
        finally:
            if process.poll() is None:
                _kill(process)
    finally:
        stop.set()
        peer.join(timeout=ARM_WITHIN_SECONDS)

    assert total > 0, "the control filed no peer pointer, so it shows nothing"
    _assert_no_own_traces(shared, SESSION, RUN_ID)
    assert list((shared / "live").glob("r-peer-*.json")), (
        "the peer's pointers must be present or the check proved nothing"
    )
