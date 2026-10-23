"""An exited review run with a filed record reads complete.

Measured on a live fleet on 2026-10-23: a review run exited 0 and its
supervisor filed the review record it had written into the store, but the
manifest was never advanced past the working word the worker's last turn wrote.
The classifier read the manifest alone, so the run showed as still working with
a dead process for about two hours, its coordinator reported it as running, and
peers waited on it until someone repaired the run by hand.

The reviewer's deliverable is the review it serves, and the filing survives on
the shared home where the pointer's pid does not. These tests drive the
classifier against three fixtures: a review whose process ended and whose latest
attempt filed a record the store recognises, which must read delivered; the same
run with no filed record, which must keep the base reading; and an exited review
whose third attempt filed nothing while its second attempt's record stands on
disk, which must not be taken for a delivery. The negative controls drop the
filed-record reading and the attempt key, one at a time, and each must fail the
fixture it protects.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_classification, runs

# The run is classified as if read from another login node, so liveness comes
# from the run's own exit record rather than from a process table here — the
# same shape as the measured incident, where the pointer's pid was not this
# reader's to probe.
OTHER_HOST = "some-other-login-node"
RUN_ID = "r-20261023T020153063813-review-of-integrated-assembly-stability-4faf"
PROJECT = "sample"

IN_PROGRESS_BODY = (
    "node: an-exited-review-with-a-filed-record-reads-complete\n"
    "status: in-progress\n"
    "checkpoint: driving the classifier\n"
)

# A record as the review module writes it: `parsed` is the status a reviewer's
# record carries once it reads, which is what the classifier treats as a filed
# review.
PARSED_REVIEW = {
    "status": "parsed",
    "total": 82,
    "scores": {},
}


# The declared negative controls: drop one guard at a time and the fixture it
# protects must fail. ``ignore-filed`` removes the whole filed-record read, so
# an exited review with a filed record reads as it did before the reading
# existed. ``ignore-attempt`` keeps the read but drops the attempt key, keying
# the receipt on whichever attempt filed one — so a receipt filed by a
# superseded attempt would let the running attempt read as a delivery.
NEGATIVE_CONTROL = os.environ.get("RECKON_FILED_REVIEW_NEGATIVE_CONTROL", "").strip()


def _receipt_attempt(record: object) -> int | None:
    """The attempt of the newest filed receipt in the run directory.

    The mutation ``ignore-attempt`` keys the filed read on this rather than on
    the attempt the run's own marker names.
    """
    directory = recovery_classification._run_directory(record)
    attempts: list[int] = []
    for path in directory.glob("attempt-*-review-filed.json"):
        try:
            attempts.append(int(path.name.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return max(attempts) if attempts else None


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    if NEGATIVE_CONTROL == "ignore-filed":
        monkeypatch.setattr(
            recovery_classification,
            "_filed_review_record",
            lambda record, *, alive: None,
        )
    elif NEGATIVE_CONTROL == "ignore-attempt":
        monkeypatch.setattr(
            recovery_classification,
            "_current_attempt_number",
            _receipt_attempt,
        )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _filed_record(directory: Path, *, attempt: int) -> tuple[Path, Path]:
    """Write the filed store record and the receipt naming it."""
    stored = directory / f"filed-review-{attempt}.json"
    _write_json(stored, PARSED_REVIEW)
    filed = directory / f"attempt-{attempt}-review-filed.json"
    _write_json(
        filed,
        {"run_id": RUN_ID, "attempt": attempt, "filed": [str(stored)]},
    )
    return filed, stored


def _pointer(
    tmp_path: Path,
    *,
    attempt: int,
    manifest_body: str,
    pid: int | None,
    launcher_host: str,
    exit_record: bool,
) -> dict:
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(
        directory / "attempt.json",
        {
            "run_id": RUN_ID,
            "attempt": attempt,
            "attempt_kind": "dispatch",
            "attempt_started_at": "2026-10-23T02:00:00Z",
        },
    )
    if exit_record:
        _write_json(
            directory / "exit.json",
            {
                "run_id": RUN_ID,
                "attempt": attempt,
                "worker_pid": None,
                "launched_at": "2026-10-23T02:00:00Z",
                "exited_at": "2026-10-23T02:44:46Z",
                "stream_records_seen": 12,
                "exit_code": 0,
            },
        )
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(manifest_body, encoding="utf-8")
    stream = tmp_path / "streams" / f"{RUN_ID}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": "s22-coord",
        "role": "review",
        "node": {
            "id": f"review-of-{RUN_ID}",
            "plan": "the-crew-does-its-own-bookkeeping",
        },
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{RUN_ID}.stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid) if pid else None,
        "launcher_host": launcher_host,
        "worktree": str(tmp_path / "tree"),
        "base_sha": "",
    }


def _classify(pointer: dict) -> dict:
    return recovery.classify_pointer(pointer, now_seconds=_moment())


def _moment() -> float:
    return datetime.now(tz=UTC).timestamp()


def test_an_exited_review_with_a_filed_record_reads_delivered(
    tmp_path: Path,
) -> None:
    directory = runs.run_dir(RUN_ID)
    filed_path, stored_path = _filed_record(directory, attempt=2)
    pointer = _pointer(
        tmp_path,
        attempt=2,
        manifest_body=IN_PROGRESS_BODY,
        pid=None,
        launcher_host=OTHER_HOST,
        exit_record=True,
    )

    row = _classify(pointer)

    assert row["classification"] == "promotable"
    assert row["recovery_classification"] == "promotable"
    assert row["recovery"] == "promote"
    # The row names the receipt it read and the store record that receipt names.
    assert str(filed_path) in row["detail"]
    assert str(stored_path) in row["detail"]


def test_the_delivered_reading_leaves_the_working_bucket(tmp_path: Path) -> None:
    directory = runs.run_dir(RUN_ID)
    _filed_record(directory, attempt=2)
    pointer = _pointer(
        tmp_path,
        attempt=2,
        manifest_body=IN_PROGRESS_BODY,
        pid=None,
        launcher_host=OTHER_HOST,
        exit_record=True,
    )

    snapshot = recovery._watch_snapshot(pointer, moment=_moment(), stall_seconds=3600)

    assert snapshot["state"] == "complete"


def test_an_exited_review_with_no_filed_record_keeps_the_base_reading(
    tmp_path: Path,
) -> None:
    # No receipt is written: the run's own evidence is the exit record and the
    # working word its manifest carries, and the reading must be unchanged.
    pointer = _pointer(
        tmp_path,
        attempt=2,
        manifest_body=IN_PROGRESS_BODY,
        pid=None,
        launcher_host=OTHER_HOST,
        exit_record=True,
    )

    row = _classify(pointer)

    assert row["classification"] == "running"
    assert row["classification"] != "promotable"


def test_an_exited_third_attempt_behind_a_second_attempts_record_is_running(
    tmp_path: Path,
) -> None:
    # The run's process has ended and a receipt is on disk, but it belongs to
    # the attempt before this one; the attempt now on the run filed nothing. The
    # process having ended must not, on its own, let a superseded attempt's
    # record stand in — the attempt key is the only thing keeping this run off
    # the delivered reading, which is what the ``ignore-attempt`` control drops.
    directory = runs.run_dir(RUN_ID)
    _filed_record(directory, attempt=2)
    pointer = _pointer(
        tmp_path,
        attempt=3,
        manifest_body=IN_PROGRESS_BODY,
        pid=None,
        launcher_host=OTHER_HOST,
        exit_record=True,
    )

    row = _classify(pointer)

    assert row["classification"] == "running"
    assert row["classification"] != "promotable"
