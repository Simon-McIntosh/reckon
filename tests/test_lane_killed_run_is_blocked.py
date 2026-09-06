"""A run the local lane killed is blocked and resumable, never abandoned.

The local lane reports a spent consumer as ``system/api_retry`` records rather
than as a refusal event, so a run that died mid-retry records retries and no
terminal result. From the stream alone that shape is indistinguishable from a
live worker mid-retry-burst, and the stream observer deliberately refuses to
label it — a successful run carries seven retries, so the count alone neither
blocks anything nor announces anything. Recovery joins the two facts that do
distinguish: the retry count the budget block already surfaces, and process
liveness. A dead process with retries and no terminal record is a lane kill —
the most triageable stop a fleet can suffer — so it reads blocked and resumes;
an alive process in the same shape is still working and must keep reading
running.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from reckon import _backends
from reckon.crew import recovery


def _init() -> dict[str, Any]:
    """The session-carrying event every real run stream opens with."""
    return {"type": "system", "subtype": "init", "session_id": "sess-local"}


def _retry(*, cause: str = "rate_limit", status: int = 429) -> dict[str, Any]:
    """One ``system/api_retry`` record in the recorded lane's own shape."""
    return {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 10,
        "retry_delay_ms": 5000,
        "error": cause,
        "error_status": status,
        "session_id": "sess-local",
    }


def _result(*, is_error: bool) -> dict[str, Any]:
    """One terminal ``result`` event; the error text defaults to a lane shape."""
    if is_error:
        message = "API Error: Request rejected (429) · consumer queue full"
    else:
        message = ""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": message,
        "api_error_status": 429 if is_error else 200,
    }


def _write_stream(tmp_path: Path, name: str, events: list[dict[str, Any]]) -> Path:
    """Write one synthetic run stream beside the record that names it."""
    stream = tmp_path / "streams" / f"{name}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("".join(json.dumps(event) + "\n" for event in events))
    return stream


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    stream: Path,
    alive: bool,
    phase: str,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A cli pointer reading one synthetic stream, with no manifest verdict.

    No manifest is written, so nothing above the stream and liveness can
    classify the run — the shape this module exists to arbitrate. ``budget``
    may carry the folded block that observe() would have written, to exercise
    the raw-pointer and folded-pointer paths separately.
    """
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "backend": "clive",
        "launch": "cli",
        "argv": ["clive"],
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "phase": phase,
        "process_alive": alive,
        "session": "s",
        "manifest_path": str(tmp_path / "manifests" / f"{run_id}.md"),
    }
    if budget is not None:
        record["budget"] = budget
    return record


def _mid_flight_budget(stream: Path) -> dict[str, Any]:
    """The block observe() would fold from a mid-flight retry stream."""
    observation = _backends.observe_log(
        backend_name="clive",
        backend={"command": "clive"},
        log_path=str(stream),
    )
    return observation.as_dict()["budget"]


def _classify(pointer: dict[str, Any]) -> dict[str, Any]:
    return recovery.classify_pointer(pointer, now_seconds=time.time())


# ── The lane kill: dead, retrying, no terminal record ────────────────────────


def test_a_dead_run_with_rate_limit_retries_and_no_terminal_block_not_abandoned(
    tmp_path,
) -> None:
    # Phase keys the falsifier: a killed run can hold any phase in its pointer,
    # "complete" included, because phase is not a terminal signal — the process
    # table and the stream are. A change keyed on phase would misclassify the
    # lane-killed run below as abandoned no matter which branch it took.
    stream = _write_stream(
        tmp_path, "lane-killed", [_init(), *[_retry() for _ in range(7)]]
    )
    pointer = _pointer(
        tmp_path, "r-killed", stream=stream, alive=False, phase="complete"
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert row["classification"] != "abandoned"
    assert "clive" in row["detail"]
    assert "7 times" in row["detail"]
    assert "rate-limited" in row["detail"]
    assert "no manifest was delivered" in row["detail"]
    assert "resume" in row["next_action"]
    assert "discard" not in row["next_action"]


def test_a_lane_killed_run_is_blocked_at_any_stored_phase(tmp_path) -> None:
    # "starting" is the phase a run killed before its first turn would hold; a
    # stale "working" is the common case. Both must read blocked, never
    # abandoned, because the verdict derives from liveness and the stream.
    stream = _write_stream(
        tmp_path, "lane-killed-phases", [_init(), *[_retry() for _ in range(4)]]
    )
    for phase in ("starting", "working", "complete"):
        pointer = _pointer(
            tmp_path, f"r-killed-{phase}", stream=stream, alive=False, phase=phase
        )
        row = _classify(pointer)
        assert row["classification"] == "blocked", f"phase {phase!r} misclassified"


def test_a_lane_killed_run_reads_blocked_in_the_snapshot_the_ticker_sees(
    tmp_path,
) -> None:
    # The single-event watcher consumes the snapshot, so the lane-killed run
    # must surface there as blocked with its reason, not fall back to the
    # abandoned bucket the liveness check would otherwise assign it.
    stream = _write_stream(
        tmp_path, "lane-killed-ticker", [_init(), *[_retry() for _ in range(6)]]
    )
    pointer = _pointer(
        tmp_path, "r-killed-ticker", stream=stream, alive=False, phase="working"
    )

    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)

    assert snapshot["state"] == "blocked"
    assert "clive" in snapshot["detail"]


def test_a_lane_killed_run_with_a_folded_budget_reads_blocked_too(tmp_path) -> None:
    # observe() folds the stream into the pointer; a reader classifying that
    # already-observed record reaches the same verdict as one reading the raw
    # pointer, because both resolve the budget through the same translation.
    stream = _write_stream(
        tmp_path, "lane-killed-folded", [_init(), *[_retry() for _ in range(9)]]
    )
    pointer = _pointer(
        tmp_path,
        "r-killed-folded",
        stream=stream,
        alive=False,
        phase="working",
        budget=_mid_flight_budget(stream),
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert "9 times" in row["detail"]


def test_a_lane_killed_run_offers_a_resume_remedy(tmp_path) -> None:
    # The session is intact (the stream's init event carries it), so the
    # blocked reading offers the same explicit resume remedy a provider
    # refusal does, rather than asking the reader to recover the run by hand.
    stream = _write_stream(
        tmp_path, "lane-killed-remedy", [_init(), *[_retry() for _ in range(5)]]
    )
    pointer = _pointer(
        tmp_path, "r-killed-remedy", stream=stream, alive=False, phase="working"
    )

    row = _classify(pointer)

    remedy = row["resume_remedy"]
    assert remedy == {
        "command": "reckon crew resume --run r-killed-remedy --advice continue",
        "session_id": "sess-local",
        "source": "stream",
    }


# ── The unchanged cases: exhaustion stays a refusal, alive stays running ──────


def test_a_run_dead_after_terminal_exhaustion_keeps_its_refusal_reason(
    tmp_path,
) -> None:
    # The terminal shape — retries ending in an error result — is already a
    # block, and this node must not restate it: the existing refusal reason
    # names the lane and the reset, and classifying it through the retry shape
    # instead would churn the stored answer.
    stream = _write_stream(
        tmp_path,
        "lane-exhausted",
        [_init(), *[_retry() for _ in range(15)], _result(is_error=True)],
    )
    pointer = _pointer(
        tmp_path, "r-exhausted", stream=stream, alive=False, phase="blocked"
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert "refused the turn" in row["detail"]
    assert "on a rate-limit" in row["detail"]
    assert "died mid-retry" not in row["detail"]


def test_an_alive_run_with_rate_limit_retries_is_still_running(tmp_path) -> None:
    # The falsifier that makes the verdict wait for liveness: two runs that
    # carried seven retries each were still generating and went on to commit
    # real work. A change that marked this shape blocked would report every
    # throttled worker as dead while its work was still landing.
    stream = _write_stream(
        tmp_path, "lane-busy", [_init(), *[_retry() for _ in range(7)]]
    )
    pointer = _pointer(tmp_path, "r-busy", stream=stream, alive=True, phase="working")

    row = _classify(pointer)

    assert row["classification"] == "running"
    assert row["classification"] != "blocked"


def test_a_dead_run_with_no_retries_still_abandons(tmp_path) -> None:
    # No lane signal at all is a genuine vanished worker, unchanged: nothing
    # here reclassifies it, because the stream carries nothing to triage.
    stream = _write_stream(tmp_path, "vanished", [_init()])
    pointer = _pointer(
        tmp_path, "r-vanished", stream=stream, alive=False, phase="starting"
    )

    row = _classify(pointer)

    assert row["classification"] == "abandoned"
    assert row["classification"] != "blocked"
    assert "process is gone" in row["detail"]


def test_a_dead_run_completed_after_retries_with_no_manifest_still_abandons(
    tmp_path,
) -> None:
    # Retries that ended in a terminal success are not a lane kill: the stream
    # says the run finished, and a finished stream with no delivered manifest
    # is the existing lost-delivery reading, not a stop the lane owns. Only the
    # no-terminal-result shape reads as blocked.
    stream = _write_stream(
        tmp_path,
        "lane-completed",
        [_init(), *[_retry() for _ in range(7)], _result(is_error=False)],
    )
    pointer = _pointer(
        tmp_path, "r-completed", stream=stream, alive=False, phase="complete"
    )

    row = _classify(pointer)

    assert row["classification"] == "abandoned"
    assert row["classification"] != "blocked"


def test_server_overload_retries_do_not_block_an_abandoned_run(tmp_path) -> None:
    # A 529 overload retry is capacity, not a spent lane; a dead run whose
    # stream shows only overload retries carries no rate-limit retry signal, so
    # the lane reading has nothing to join with liveness.
    stream = _write_stream(
        tmp_path,
        "lane-overloaded",
        [_init(), *[_retry(cause="overloaded", status=529) for _ in range(9)]],
    )
    pointer = _pointer(
        tmp_path, "r-overloaded", stream=stream, alive=False, phase="starting"
    )

    row = _classify(pointer)

    assert row["classification"] == "abandoned"
    assert row["classification"] != "blocked"
