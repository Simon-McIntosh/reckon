"""A delivered run on another node waits for the reflex before it owes a review.

The stop hook derives a ``review-missing`` duty from a run's classification the
moment its manifest reads ``complete``, and the review reflex dispatches on the
run's next sweep well after that. A worker whose liveness this host can prove is
held at ``running`` by ``deferred_outcome`` and raises no duty, so the hook never
races the reflex for it. A producer classifying the fleet from another node
cannot prove a worker's liveness, and ``deferred_outcome`` once tested
``alive is True`` alone: a delivered worker on another host, still in its final
turn, read ``scoring`` at once and raised the duty the reflex was about to
discharge itself.

The deferral now consults the hoisted, stream-aware verdict that already answers
whether the worker is gone, so such a run reads ``running`` while its stream is
in motion. The exit record the supervisor writes on the shared home crosses the
node boundary a pid cannot, and its presence ends the run at once, so an exited
worker on another host reads ``scoring`` without waiting for its stream to pass
the stall window.

Everything the classifier resolves through the environment is pointed at
``tmp_path`` so the real crew directories are untouched.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from reckon.crew import obligations, recovery, runs

ASSISTANT_RECORD = '{"type":"assistant","message":"working"}\n'
OTHER_HOST = "another-login-node"


def _pointer(
    run_id: str,
    *,
    stream: Path,
    manifest: Path,
    created_at: float,
) -> dict:
    """A live pointer for a run launched on another machine."""
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "40m"},
        "phase": "working",
        "attempt": 1,
        "created_at": recovery.datetime.fromtimestamp(
            created_at, tz=recovery.timezone.utc
        ).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "process_alive": None,
        "pid": None,
        "launcher_host": OTHER_HOST,
        "worktree": str(stream.parent / "tree"),
        "base_sha": "",
    }


def _stream(tmp_path: Path, run_id: str, *, age_seconds: float) -> Path:
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(ASSISTANT_RECORD)
    stamp = time.time() - age_seconds
    os.utime(stream, (stamp, stamp))
    return stream


def _delivered_manifest(tmp_path: Path, run_id: str) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {run_id}\nstatus: complete\n")
    return manifest


def _write_exit_record(run_id: str, *, attempt: int) -> None:
    """The supervisor's exit record for the run's current attempt."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "exit_code": 0,
                "exited_at": recovery.datetime.fromtimestamp(
                    time.time(), tz=recovery.timezone.utc
                ).isoformat(),
                "ended_during": "run",
                "stream_records_seen": 4,
            }
        ),
        encoding="utf-8",
    )


def test_a_delivered_run_on_another_node_waits_while_its_stream_moves(
    tmp_path: Path, home: Path
) -> None:
    # The worker wrote a complete manifest and is still in its final turn: its
    # pointer names another machine, so its liveness is unknown here, and its
    # newest stream carries an assistant turn inside the stall window. The
    # deferral reads that life, holds the run at running, and derives no review
    # duty — the reflex will dispatch the review itself.
    now = time.time()
    run_id = "r-foreign-delivered-moving"
    pointer = _pointer(
        run_id,
        stream=_stream(tmp_path, run_id, age_seconds=10),
        manifest=_delivered_manifest(tmp_path, run_id),
        created_at=now - 30,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] == "running", row["detail"]
    assert obligations.CLASSIFICATION_DUTY_KINDS.get(row["classification"]) is None
    assert "scoring" not in obligations.CLASSIFICATION_DUTY_KINDS.get(
        row["classification"], ""
    )


def test_a_delivered_run_whose_stream_has_quieted_reads_scoring(
    tmp_path: Path, home: Path
) -> None:
    # The same delivered run whose newest stream last moved past the stall
    # window: nothing here shows a worker in motion, so the run is not deferred
    # and reads scoring, raising the review duty as it does today.
    now = time.time()
    run_id = "r-foreign-delivered-quiet"
    pointer = _pointer(
        run_id,
        stream=_stream(
            tmp_path,
            run_id,
            age_seconds=recovery.LOG_STALE_AFTER_SECONDS + 100000,
        ),
        manifest=_delivered_manifest(tmp_path, run_id),
        created_at=now - 100300,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] == "scoring", row["detail"]
    assert (
        obligations.CLASSIFICATION_DUTY_KINDS.get(row["classification"])
        == "review-missing"
    )


def test_a_recorded_exit_ends_a_delivered_run_before_its_stream_quiets(
    tmp_path: Path, home: Path
) -> None:
    # The same delivered run on another host with its stream still in motion,
    # but the supervisor has recorded its exit for the current attempt. The
    # record crosses the node boundary a pid cannot, so the run reads scoring at
    # once rather than waiting a quarter of an hour for the stream to go quiet.
    now = time.time()
    run_id = "r-foreign-delivered-exited"
    pointer = _pointer(
        run_id,
        stream=_stream(tmp_path, run_id, age_seconds=10),
        manifest=_delivered_manifest(tmp_path, run_id),
        created_at=now - 30,
    )
    _write_exit_record(run_id, attempt=1)

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] == "scoring", row["detail"]
    assert (
        obligations.CLASSIFICATION_DUTY_KINDS.get(row["classification"])
        == "review-missing"
    )


def test_the_real_crew_home_is_untouched(tmp_path: Path, home: Path) -> None:
    # Every environment-resolved directory this module reads is under the test's
    # own home, so classifying a foreign run writes nothing to the operator's.
    from tests.conftest import real_store_is_not_a_fixture_target

    real_store_is_not_a_fixture_target(home)