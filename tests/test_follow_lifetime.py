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

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import runs

REPO_ROOT = Path(cli.__file__).resolve().parents[1]
PROJECT = "proj"
SESSION = "s1"
RUN_ID = "r-1"
NODE = "n1"

# The granted lifetime, the bound on reaching an arm, and the bound on ending
# once armed. The last is generous over the granted lifetime because the
# follower acts on its deadline at the next pass of its own poll loop.
LIFETIME = "3s"
ARM_WITHIN_SECONDS = 30.0
END_WITHIN_SECONDS = 5.0
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
        env={**os.environ, "RECKON_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)},
    )


def _kill(process: subprocess.Popen) -> tuple[str, str]:
    """End a follower the test is done with, and collect what it printed."""
    process.kill()
    stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
    return stdout, stderr


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


def _tree(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def test_a_lifetime_ends_the_follower_with_one_line_a_reader_acts_on(home) -> None:
    """The granted lifetime ends the arming, and the last line says what next.

    Measured against the grant rather than against a sleep: three seconds'
    lifetime must end the follower within five seconds of its arm, and the final
    line must carry the attach line to re-arm with and the node of the run that
    the coordinator owns, so the next arming starts from the line itself.
    """
    _write_unpromoted_run(home)
    shared = _real_crew_home()
    before = (_tree(shared / "watch"), _tree(shared / "live"))

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
    assert runs._watch_attach_line(PROJECT, session=SESSION) in final, (
        f"the line must name the attach line to re-arm with; got {final!r}"
    )
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

    assert (_tree(shared / "watch"), _tree(shared / "live")) == before, (
        "the shared config home must be untouched by a follower pointed at a "
        "temporary home"
    )


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
