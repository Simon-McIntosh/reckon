"""A live reading with no process behind it is settled, and its owner told once.

A run's phase is judged from its pointer, and a pointer whose launch writer set
a pre-delivery phase and never advanced it keeps reading live for as long as the
record survives. Nothing is behind it: the scheduler no longer lists the step
its worker ran in, and its stream stopped growing far longer ago than a worker
legitimately thinks for. Such a run is settled as the worker that ended without
delivering, which is a phase that already exists, so its owner is told once and
a resume or a fresh review can proceed.

The reading is narrow in both directions. A worker that is alive and quiet is
not this case, and neither is a step the scheduler still lists. A run younger
than the settle bound is untouched, and so is one whose step cannot be asked
about at all.

``SCHEDULER_LISTS_THE_STEP`` is the negative control: with it set true the
scheduler answers that the step is still listed, and the two settled cases below
must fail. It changes the scheduler's answer and nothing else — the same
pointers, streams and clocks.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime

import pytest

from reckon.crew import obligations, process_liveness, recovery, runs

PROJECT = "settled-fixture"
SESSION = "coordinator-fixture"
NOW = time.time()
SUITE_JOB = "4112"
SETTLE_BOUND_SECONDS = 6.0 * 3600.0

#: A step another run placed in the same reservation is running in. Its name is
#: what separates a listing that names only this run's step from one that names
#: the whole job.
OTHER_RUNS_STEP = ("4112.1", "r-other-run")


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _install_scheduler(
    monkeypatch: pytest.MonkeyPatch, steps: list[tuple[str, str]]
) -> dict:
    """Model the scheduler's step listing at the probe the reader really calls.

    Two properties of the real instrument are reproduced rather than smoothed
    over. The model answers the WHOLE listing, because the shared reader
    (``process_liveness._ask_scheduler``) returns only its last line: a model
    that answered one line would hide the truncation the query has to survive.
    And the model honours ``--name=`` as ``squeue`` does, so a query naming this
    run's step gets only that step back however busy the job is.

    ``steps`` is the job's inventory in print order, as ``(step id, name)``.
    """
    seen: dict = {}

    def probe(argv):
        seen["argv"] = list(argv)
        wanted = next(
            (
                str(word).removeprefix("--name=")
                for word in argv
                if str(word).startswith("--name=")
            ),
            None,
        )
        lines = [
            f"{step_id} {name}" for step_id, name in steps if wanted in (None, name)
        ]
        return "\n".join(lines) + ("\n" if lines else "")

    monkeypatch.setattr(process_liveness, "_run_scheduler_query", probe)
    return seen


def _write_run(
    run_id: str,
    *,
    phase: str,
    started_hours_ago: float,
    stream_hours_ago: float,
    process_alive: bool | None = None,
) -> dict:
    """A pointer, a placed-worker record, and a stream that has stopped growing."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "argv": [
                    "srun",
                    f"--jobid={SUITE_JOB}",
                    f"--job-name={run_id}",
                    "python",
                    "-m",
                    "reckon",
                    "--print",
                ],
            }
        ),
        encoding="utf-8",
    )
    stream = directory / "stream.jsonl"
    stream.write_text('{"type":"assistant","message":"working"}\n', encoding="utf-8")
    stamp = NOW - stream_hours_ago * 3600.0
    os.utime(stream, (stamp, stamp))
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": run_id, "plan": "fixture-plan"},
        "phase": phase,
        "created_at": _at(NOW - started_hours_ago * 3600.0),
        "attempt_started_at": _at(NOW - started_hours_ago * 3600.0),
        "process_alive": process_alive,
        "pid": None,
        "log_path": str(stream),
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def test_a_starting_pointer_with_no_step_and_no_stream_past_the_setting_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "r-settled-starting"
    record = _write_run(
        run_id, phase="starting", started_hours_ago=10.0, stream_hours_ago=7.0
    )
    seen = _install_scheduler(monkeypatch, [OTHER_RUNS_STEP])

    row = recovery.classify_pointer(record, now_seconds=NOW)

    assert row["classification"] == "interrupted", row["detail"]
    settled = row["settled"]
    assert settled["step_state"] == recovery.LAUNCH_STEP_GONE
    assert settled["quiet_after_seconds"] == int(SETTLE_BOUND_SECONDS)
    assert settled["quiet_seconds"] == int(7.0 * 3600.0)
    assert settled["stream"] == str(runs.run_dir(run_id) / "stream.jsonl")
    # The query names the run's own job and its own step. The name is what
    # keeps a busy reservation's listing from being read as this run's, and it
    # is load-bearing rather than tidiness: the shared scheduler reader answers
    # one line, so an unfiltered listing loses this run's step whenever another
    # step prints after it.
    assert seen["argv"] == [
        "squeue",
        "--steps",
        "-h",
        "--jobs",
        SUITE_JOB,
        f"--name={run_id}",
        "--format=%i %j",
    ]
    assert "settled" in row["detail"]


def test_a_working_pointer_whose_step_is_gone_and_stream_stale_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "r-settled-working"
    record = _write_run(
        run_id, phase="working", started_hours_ago=10.0, stream_hours_ago=7.0
    )
    _install_scheduler(monkeypatch, [OTHER_RUNS_STEP])

    row = recovery.classify_pointer(record, now_seconds=NOW)

    assert row["classification"] == "interrupted", row["detail"]
    assert row["settled"]["step_state"] == recovery.LAUNCH_STEP_GONE
    assert "settled" in row["detail"]
    assert run_id in row["next_action"] or "redispatch" in row["next_action"]


def test_a_working_pointer_whose_step_is_listed_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "r-still-placed"
    record = _write_run(
        run_id, phase="working", started_hours_ago=10.0, stream_hours_ago=7.0
    )
    # The run's step is still listed, and it is not the last step the job
    # holds: two other runs placed in the same reservation print after it. A
    # listing read without naming the step would end on another run's line and
    # this run would be settled while its worker is still placed.
    _install_scheduler(
        monkeypatch,
        [
            OTHER_RUNS_STEP,
            (f"{SUITE_JOB}.9", run_id),
            ("4112.4", "r-other-run-later"),
        ],
    )

    row = recovery.classify_pointer(record, now_seconds=NOW)

    assert row["settled"] is None
    assert row["classification"] == "running", row["detail"]


def test_a_starting_pointer_younger_than_the_setting_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "r-young-launch"
    record = _write_run(
        run_id, phase="starting", started_hours_ago=1.0, stream_hours_ago=0.5
    )
    _install_scheduler(monkeypatch, [OTHER_RUNS_STEP])

    row = recovery.classify_pointer(record, now_seconds=NOW)

    assert row["settled"] is None
    assert row["classification"] == "running", row["detail"]


def test_a_worker_alive_and_quiet_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "r-alive-and-quiet"
    record = _write_run(
        run_id,
        phase="working",
        started_hours_ago=10.0,
        stream_hours_ago=7.0,
        process_alive=True,
    )
    _install_scheduler(monkeypatch, [OTHER_RUNS_STEP])

    row = recovery.classify_pointer(record, now_seconds=NOW)

    assert row["settled"] is None
    assert row["classification"] == "running", row["detail"]


def test_a_settled_run_appears_once_in_its_owners_obligations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "r-settled-obligation"
    record = _write_run(
        run_id, phase="starting", started_hours_ago=10.0, stream_hours_ago=7.0
    )
    _install_scheduler(monkeypatch, [OTHER_RUNS_STEP])

    view = obligations.obligations(PROJECT, SESSION)
    rows = [row for row in view["obligations"] if str(row.get("run_id")) == run_id]

    assert len(rows) == 1, view
    assert rows[0]["kind"] == "turn-ended-early"
    assert rows[0]["next_command"], rows[0]
    # The evidence behind the settle is on the classification row itself, where
    # a run's phase, classification and process liveness are judged.
    settled = recovery.classify_pointer(record, now_seconds=NOW)
    assert settled["classification"] == "interrupted"
    assert settled["settled"]["step_state"] == recovery.LAUNCH_STEP_GONE
    assert "settled" in settled["detail"]
    assert record["phase"] == "starting"
