"""A follower's deadline is fixed at its first arming, not at its reload.

A follower replaces its own image when reckon's source changes, re-entering
through ``crew follow`` with the arguments it was armed with, ``--lifetime``
among them. If the replacement anchored its deadline at its own start, every
reload would restart the clock: during an active sprint, when code is landing
and the final line matters most, the deadline would slide past the host's
thirty-minute cap and the arming would end silently in the host's words instead
of its own.

The deadline is an absolute instant fixed at the first arming, carried to the
replacement image in the environment so it survives ``os.execv``. The measure is
therefore taken from the *original* arm: with the reload forced about three
seconds in, the follower still ends within eight seconds of that arm — a bound a
restarted clock cannot meet, because the replacement arms several seconds later
and would then run a further six.

The test observes the follower's real process, because the property is about an
image replacement and a fabricated generator would measure the test's own
assumptions. The reload is forced through the same trigger the follower uses for
a source change — a stamp computed over the follower's source files — and its
occurrence is confirmed by the replacement's own command line, so a run in which
the reload never happened cannot pass by measuring only the carried deadline.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import cli, crew
from reckon.crew import runs

REPO_ROOT = Path(cli.__file__).resolve().parents[1]
FOLLOWER_SOURCE = Path(cli.__file__).resolve()
PROJECT = "proj"
SESSION = "s1"
RUN_ID = "r-1"
NODE = "n1"

# The grant an arming carries, the deadline's own name in the environment, the
# bound on ending once the reload has been forced, the bound on observing the
# reload itself, the bound on seeing a child the follower started, and — for a
# case whose sweep runs a probe child on every cadence — a looser bound on
# ending. The graced end carries the replacement image's own setup and its first
# sweep, so a case that probes continuously reaches its end later than the reload
# case does; the reload case is the one that holds the tighter bound.
LIFETIME = "6s"
DEADLINE_ENV = "RECKON_FOLLOWER_LIFETIME_DEADLINE"
ARM_WITHIN_SECONDS = 30.0
END_WITHIN_SECONDS = 8.0
SWEPT_END_WITHIN_SECONDS = 15.0
RELOAD_WITHIN_SECONDS = 6.0
CHILD_WITHIN_SECONDS = 6.0
POLL_SECONDS = 0.05

# The replacement image is launched by the follower with a launcher that
# inserts its import root on ``sys.path`` before entering the command. That
# string is absent from the argv the test arms, so finding it on the process is
# direct evidence the image was replaced.
RELOAD_LAUNCHER_MARKER = "sys.path.insert"


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


def _probe_code(dump: Path) -> str:
    """The probe's program: record the environment the child was handed.

    It appends one comma-joined line of variable names per run, so a follower
    that sweeps more than once — before and after an image replacement — leaves
    a line from each image rather than overwriting the first.
    """
    return (
        "import os\n"
        f"open({str(dump)!r}, 'a', encoding='utf-8').write("
        "','.join(sorted(os.environ)) + '\\n')\n"
        "print('recorded')\n"
    )


def _write_parked_run(home: Path, dump: Path) -> None:
    """One live run parked on a wait whose probe records its own environment.

    The sweep runs a parked run's declared probe through ``subprocess.run``
    inheriting the follower's environment, so this is the follower's own
    subprocess path: whatever the probe's child inherits is what every child the
    follower starts would inherit. The terminal state is never printed, so the
    run stays parked and the probe runs again on the replacement image's first
    sweep.
    """
    log = home / "logs" / f"{RUN_ID}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    worktree = home / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    probe = [sys.executable, "-c", _probe_code(dump)]
    manifest.write_text(
        "status: waiting\n"
        "wait_condition: a child's environment has been recorded\n"
        f"wait_probe: {json.dumps(probe)}\n"
        'wait_terminal: ["absent"]\n'
        "resume_brief: read the recorded environment and finish\n",
        encoding="utf-8",
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
            "worktree": str(worktree),
            "manifest_path": str(manifest),
            "log_path": str(log),
            "manifest_baseline_mtime_ns": manifest.stat().st_mtime_ns - 1_000_000_000,
        },
    )


def _recorded_environments(dump: Path) -> list[str]:
    if not dump.exists():
        return []
    return [
        line for line in dump.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _wait_for_child(dump: Path, process: subprocess.Popen) -> list[str]:
    """Wait for the follower's sweep to run the probe, or fail saying which.

    A missing dump is not evidence about the environment: it is a probe that
    never ran, so the wait has to be satisfied before any absence is reported.
    """
    deadline = time.monotonic() + CHILD_WITHIN_SECONDS
    while time.monotonic() < deadline:
        lines = _recorded_environments(dump)
        if lines:
            return lines
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        "the follower ran no probe child within "
        f"{CHILD_WITHIN_SECONDS!r}s of its arm, so the environment it hands a "
        f"child was never observed; stdout={stdout!r} stderr={stderr!r}"
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
    """Wait for the follower to hold its registration, or fail saying which."""
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


def _process_argv(pid: int) -> str:
    """Read a live image's command line, or an empty string once it is gone."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _wait_until_reloaded(process: subprocess.Popen) -> float:
    """Wait for the replacement image, and report when it was first seen.

    The replacement's command line carries the launcher that inserts the import
    root, which the armed argv does not. An image replacement is the only thing
    that can put it there, so this is the reload itself rather than a proxy.
    """
    deadline = time.monotonic() + RELOAD_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if RELOAD_LAUNCHER_MARKER in _process_argv(process.pid):
            return time.monotonic()
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        "the follower did not replace its image within "
        f"{RELOAD_WITHIN_SECONDS!r}s of the source change, so the reload "
        f"survival was not exercised; argv={_process_argv(process.pid)!r} "
        f"stdout={stdout!r} stderr={stderr!r}"
    )


def _wait_until_ended(
    process: subprocess.Popen, *, arm_at: float, within: float = END_WITHIN_SECONDS
) -> float:
    """Wait for the follower to end by itself; report when, from the arm.

    Never killed to make it so: the property under measure is that the carried
    deadline ends the arming, and a killed process would answer a different
    question.
    """
    deadline = arm_at + within
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return time.monotonic()
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        f"the follower was still running {within!r}s after its "
        f"original arm, so the reload restarted its deadline; "
        f"stdout={stdout!r} stderr={stderr!r}"
    )


def _force_source_change() -> tuple[float, float]:
    """Advance the follower's source stamp, and hand back what to restore.

    The follower reloads when a stamp computed over its own source files
    changes, so a bare mtime bump is exactly the trigger it watches. The size is
    left alone, so no bytes move and the tree stays clean; the caller restores
    the timestamps afterwards.
    """
    stat = FOLLOWER_SOURCE.stat()
    now = time.time()
    os.utime(FOLLOWER_SOURCE, (now, now))
    return stat.st_atime_ns, stat.st_mtime_ns


def _restore_source_times(times: tuple[float, float]) -> None:
    os.utime(FOLLOWER_SOURCE, ns=(times[0], times[1]))


def test_a_reload_does_not_restart_the_lifetime(home) -> None:
    """The deadline carried across a reload still ends the follower on time.

    Armed with six seconds, forced to reload about three seconds in, the
    follower ends within eight seconds of the original arm and prints its own
    final line. A deadline anchored at the replacement's start cannot meet that
    bound, which is what the declared negative control shows.
    """
    _write_unpromoted_run(home)
    restore = None
    process = _arm(home, "--lifetime", LIFETIME)
    try:
        _wait_until_armed(process)
        arm_at = time.monotonic()
        restore = _force_source_change()
        reloaded_at = _wait_until_reloaded(process)
        assert reloaded_at > arm_at, "the reload was seen before the arm"
        ended_at = _wait_until_ended(process, arm_at=arm_at)
        stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
    finally:
        if process.poll() is None:
            _kill(process)
        if restore is not None:
            _restore_source_times(restore)

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
    assert RUN_ID in final, (
        f"the line must name the run that needs a decision; {final!r}"
    )
    elapsed = ended_at - arm_at
    assert elapsed <= END_WITHIN_SECONDS, (
        f"the carried deadline must end the arming within "
        f"{END_WITHIN_SECONDS!r}s of the original arm; took {elapsed:.3f}s"
    )
    # The line must survive the reload intact: the attach line is what a reader
    # re-arms from, and it is composed by the replacement image.
    tokens = shlex.split(final)
    assert any(
        os.path.isabs(token) and os.path.basename(token) == "reckon" for token in tokens
    ), f"the final line names no re-arm executable: {final!r}"


def test_the_deadline_reaches_no_child_the_follower_starts(home) -> None:
    """The carried deadline bounds the follower, never a child it starts.

    The deadline is handed to the replacement image through the environment the
    re-exec passes, and read and removed in the same step, so it never sits in
    the follower's ``os.environ``. Every child the follower starts — the sweep's
    probe among them — inherits ``os.environ`` and would otherwise carry a
    deadline that governs no process but the follower itself. The probe records
    the environment it was handed, so the property is observed in the one place
    a leaked variable would show, and the reload case still ends the arming
    within the eight-second bound.
    """
    dump = home / "child-environment.txt"
    _write_parked_run(home, dump)
    restore = None
    process = _arm(home, "--lifetime", LIFETIME)
    try:
        _wait_until_armed(process)
        arm_at = time.monotonic()
        lines = _wait_for_child(dump, process)
        # The instrument must be shown to see a known-present variable before an
        # absence means anything: the follower was armed with RECKON_HOME, so a
        # child inheriting its environment carries it.
        assert any("RECKON_HOME" in line for line in lines), (
            f"the probe did not observe the environment it was handed, so it "
            f"cannot report what is absent from it; lines={lines!r}"
        )
        assert not any(DEADLINE_ENV in line for line in lines), (
            f"a child the follower started inherited {DEADLINE_ENV}, so the "
            f"deadline was placed in the follower's own os.environ; lines={lines!r}"
        )

        restore = _force_source_change()
        _wait_until_reloaded(process)
        # The arm must still end by itself: a follower that hangs here would
        # leave the leak unobserved. The tight timing property — the end landing
        # within eight seconds of the original arm — belongs to the reload case,
        # whose sweep load is lighter; this one runs a probe child every cadence
        # and reaches its end later, so it asserts only that it ends.
        _wait_until_ended(process, arm_at=arm_at, within=SWEPT_END_WITHIN_SECONDS)
        _stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
    finally:
        if process.poll() is None:
            _kill(process)
        if restore is not None:
            _restore_source_times(restore)

    assert process.returncode == 0, (
        f"a lifetime exit is the follower ending by itself, so it exits zero; "
        f"got {process.returncode}; stderr={stderr!r}"
    )
    # The replacement image swept too, so its own child is covered: read the
    # dump again after the reload, and require no image to have leaked it.
    lines = _recorded_environments(dump)
    assert any("RECKON_HOME" in line for line in lines), (
        f"the replacement's probe did not observe its environment; lines={lines!r}"
    )
    assert not any(DEADLINE_ENV in line for line in lines), (
        f"a child started by the replacement image inherited {DEADLINE_ENV}; "
        f"lines={lines!r}"
    )
