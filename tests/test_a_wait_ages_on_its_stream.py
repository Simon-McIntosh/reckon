"""A declared wait is aged on its stream, and its probe must be able to differ.

Two readings put a healthy worker in alarm, both measured on live runs.

''The age.'' The wait's age was read from the manifest's modification time, and
the manifest is precisely the artifact a busy worker stops touching: two runs
read aged 1838 s and 1672 s while their newest streams were 0 s and 3 s old, so
the alarm fired while the output ran and cleared when the manifest was
rewritten with nothing about the work changed. The clock that decides the age
is the stream's own freshness — the last output — with the declaration (or, for
a declaration naming no start, the manifest) as the fallback a run with no
stream has.

''The probe.'' A wait was accepted when its probe merely ran and its declared
terminal was reachable. A probe that cannot fail cannot do that job: ``echo
pending`` prints a constant and ``git rev-parse HEAD`` reports the worker's own
tree, so both are satisfied unconditionally, test nothing, and read the same on
every sweep however the awaited work is doing. A probe counts only when its
result can differ, and the references a wait actually rests on are outside the
worker: a job id, a pid, a port or a path.

Every case synthesises its run under the temporary home the suite already
isolates, and the workstation's own pointer directory is asserted untouched,
because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "wait-aging-fixture"
STALE_AFTER_SECONDS = 900
NOW_SECONDS = 1_788_853_920.0
RUN_IDS = (
    "r-wait-stale-manifest-fresh-stream",
    "r-wait-both-stale",
    "r-wait-echo-pending",
    "r-wait-git-rev-parse",
    "r-wait-probe-pid",
    "r-wait-probe-job-id",
    "r-wait-probe-port",
    "r-wait-probe-path",
)


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory.

    Every run this file synthesises is written under the isolated home the
    suite sets up, and a write — unlike a read — can make the real store wrong,
    so the real directory is checked for this file's own run ids both before
    and after every case rather than assumed isolated. The live fleet's own
    pointers churn on their own and are not this test's subject.
    """
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in RUN_IDS]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _waiting_run(
    run_id: str,
    *,
    probe: list[str],
    terminal: list[str] = ("COMPLETED", "FAILED"),
    manifest_age_seconds: float = 10.0,
    stream_age_seconds: float | None = None,
    condition: str = "scheduler job 1271081 has left the queue",
) -> dict:
    """One parked run, its manifest, and its stream when one was written."""
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                f"node: {run_id}",
                "status: waiting",
                f"wait_condition: {condition}",
                f"wait_probe: {json.dumps(probe)}",
                f"wait_terminal: {json.dumps(list(terminal))}",
                "resume_brief: collect the scheduler result and finish the report",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    if manifest_age_seconds is not None:
        stamp = NOW_SECONDS - manifest_age_seconds
        os.utime(manifest, (stamp, stamp))
    stream = directory / "stream.jsonl"
    if stream_age_seconds is not None:
        stream.write_text('{"type":"assistant","text":"working"}\n', encoding="utf-8")
        stamp = NOW_SECONDS - stream_age_seconds
        os.utime(stream, (stamp, stamp))
    tree = runs_dir() / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(runs_dir()),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "codex",
        "role": "implement",
        "session_id": f"fixture-session-{run_id}",
        "created_at": "2026-09-15T00:00:00Z",
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "waiting",
        "process_alive": False,
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "a-wait-ages-on-its-stream",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _condition_test(_pointer, _wait):
    """A probe verdict that never runs a command: the age is the subject."""
    return {
        "state": "pending",
        "observed": "RUNNING",
        "detail": "the job is still queued",
    }


def _row(run: dict) -> dict:
    return recovery.classify_pointer(
        run,
        now_seconds=NOW_SECONDS,
        stale_after_seconds=STALE_AFTER_SECONDS,
        condition_test=_condition_test,
    )


# ── The age is the stream's, not the manifest's ───────────────────────────


def test_a_stale_manifest_over_a_fresh_stream_is_not_in_alarm() -> None:
    """The busy worker: its manifest is old, its output is seconds old.

    The manifest is the artifact a busy worker stops touching, so an age read
    from it fires exactly when the worker is most active. The stream's own
    freshness is the clock, and a wait whose stream ran seconds ago is young
    however long ago the manifest was written.
    """
    run = _waiting_run(
        "r-wait-stale-manifest-fresh-stream",
        probe=["squeue", "-h", "-j", "1271081"],
        manifest_age_seconds=1800.0,
        stream_age_seconds=2.0,
    )

    row = _row(run)

    assert row["external_wait"] is not None
    assert row["external_wait"]["valid"] is True
    assert row["external_wait"]["age_seconds"] <= 2
    assert row["wait_overdue"] is False
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"

    snapshot = recovery._watch_snapshot(
        run, moment=NOW_SECONDS, stall_seconds=STALE_AFTER_SECONDS
    )
    assert snapshot["state"] == "waiting"
    assert snapshot["state"] != "wait-aged"
    assert recovery._fleet_counts({run["run_id"]: snapshot}) == {
        "working": 0,
        "blocked": 0,
        "unpromoted": 0,
        "waiting": 1,
    }


def test_a_wait_stale_in_both_reads_is_in_alarm() -> None:
    """The control: the guard suppresses an alarm, it does not disable the clock.

    The same declaration whose stream is as old as its manifest still ages into
    the alarm once the run has genuinely gone quiet, so a stream clock cannot
    be an escape from the overdue reading.
    """
    run = _waiting_run(
        "r-wait-both-stale",
        probe=["squeue", "-h", "-j", "1271081"],
        manifest_age_seconds=1800.0,
        stream_age_seconds=1800.0,
    )

    row = _row(run)

    assert row["wait_overdue"] is True
    assert row["recovery_classification"] == "wait-aged"

    snapshot = recovery._watch_snapshot(
        run, moment=NOW_SECONDS, stall_seconds=STALE_AFTER_SECONDS
    )
    assert snapshot["state"] == "wait-aged"
    assert recovery._fleet_counts({run["run_id"]: snapshot}) == {
        "working": 0,
        "blocked": 0,
        "unpromoted": 0,
        "waiting": 1,
    }


# ── The probe must be able to fail ────────────────────────────────────────


@pytest.mark.parametrize(
    "probe",
    [
        ["echo", "pending"],
        ["git", "rev-parse", "HEAD"],
    ],
)
def test_a_probe_that_cannot_fail_is_reported_as_unable_to_fail(probe) -> None:
    """A probe that is satisfied unconditionally is not a probe.

    ``echo pending`` prints a constant and ``git rev-parse HEAD`` reports the
    worker's own tree: neither can report anything about the awaited work, so a
    declaration resting on one reads the same on every sweep. The refusal names
    the probe and says why, so the declaration reaches a reader as something to
    repair rather than as a worker that declared nothing.
    """
    run_id = "r-wait-echo-pending" if probe[0] == "echo" else "r-wait-git-rev-parse"
    run = _waiting_run(run_id, probe=probe, manifest_age_seconds=10.0)

    assert recovery._wait_probe_cannot_fail(probe) is True

    wait = recovery.external_wait(run, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is False
    assert "cannot fail" in wait["error"]
    assert probe[0] in wait["error"]


@pytest.mark.parametrize(
    "probe",
    [
        ["kill", "-0", "424242"],
        ["squeue", "-h", "-j", "1271081"],
        ["nc", "-z", "localhost", "8765"],
        ["test", "-f", "logs/checkpoint.json"],
    ],
    ids=("pid", "job-id", "port", "path"),
)
def test_a_probe_naming_something_outside_the_worker_is_accepted(probe) -> None:
    """The references a wait rests on — id, pid, port, path — still read."""
    run_id = {
        "kill": "r-wait-probe-pid",
        "squeue": "r-wait-probe-job-id",
        "nc": "r-wait-probe-port",
        "test": "r-wait-probe-path",
    }[probe[0]]
    run = _waiting_run(run_id, probe=probe, manifest_age_seconds=10.0)

    assert recovery._wait_probe_cannot_fail(probe) is False

    wait = recovery.external_wait(run, now_seconds=NOW_SECONDS)

    assert wait is not None
    assert wait["valid"] is True
    assert wait["error"] == ""
    assert _row(run)["classification"] == "waiting"


def test_a_cannot_fail_probe_never_lifts_its_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injury the rule prevents: a sweep resuming a parked healthy worker.

    A probe that always succeeds would report terminal on the first sweep and
    end a wait nothing had ended. The refusal must therefore reach the reader
    that lifts a park, not only the row a coordinator reads.
    """
    run = _waiting_run("r-wait-echo-pending", probe=["echo", "pending"])
    launcher_calls: list[str] = []

    def launcher(plan, *, log_path, stderr_path, prompt_path) -> int:
        launcher_calls.append(str(plan))
        return _absent_pid()

    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [run])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])

    report = resumption.sweep(PROJECT, launcher=launcher)

    assert report["resumed"] == []
    assert launcher_calls == []
    assert [row["run_id"] for row in report["skipped"]] == [run["run_id"]]
    assert report["skipped"][0]["reason"] == "condition-declaration-invalid"
    assert "cannot fail" in str(report["skipped"][0]["detail"])


def test_a_dead_worker_on_a_cannot_fail_probe_still_reports_the_refusal() -> None:
    """The refusal is named on the row, so nothing is silently reclassified."""
    run = _waiting_run("r-wait-git-rev-parse", probe=["git", "rev-parse", "HEAD"])

    row = _row(run)

    assert row["external_wait"] is not None
    assert row["external_wait"]["valid"] is False
    assert "cannot fail" in (row["manifest_error"] or "") + (row["detail"] or "")
