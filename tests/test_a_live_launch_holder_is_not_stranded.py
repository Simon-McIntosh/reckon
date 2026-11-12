"""Only a local holder whose process predates its claim protects a launch."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import dispatch_refusals, queue_admission, recovery_liveness, runs

RUN_ID = "r-held-launch"
SOURCE_RUN = "r-reviewed-source"


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.fixture()
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, float]:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    launched = time.time()
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    pointer = {
        "run_id": RUN_ID,
        "project": "sample",
        "phase": "starting",
        "launch": "cli",
        "created_at": _at(launched),
        "attempt_started_at": _at(launched),
        "node": {"id": "fixture-launch", "role": "implement"},
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    runs._write_json(
        directory / "claim.json",
        {
            "run_id": RUN_ID,
            "claim_registered_at": _at(launched),
            "holder_pid": os.getpid(),
            "holder_host": socket.gethostname(),
            "launched_at": _at(launched),
            "step": "claim-published",
        },
    )
    return pointer, launched + recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS + 1


def _change_claim(**values: object) -> None:
    from reckon.crew.dispatch_claims import read_launch_claim

    claim = read_launch_claim(RUN_ID)
    claim.update(values)
    runs._write_json(runs.run_dir(RUN_ID) / "claim.json", claim)


def test_a_live_local_holder_keeps_its_pointer_and_directory_through_admission(
    launch: tuple[dict, float], monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer, now = launch
    release = queue_admission._release_stranded_launches
    monkeypatch.setattr(
        queue_admission,
        "_release_stranded_launches",
        lambda live, **kwargs: release(live, **kwargs, now_seconds=now),
    )
    monkeypatch.setattr(
        queue_admission, "_dispatch_fleet_gate", lambda: {"state": "open"}
    )

    report = queue_admission.admit_once(lock=False)

    assert report["released_stranded"] == []
    assert runs.pointer_path(RUN_ID).is_file()
    assert runs.run_dir(RUN_ID).is_dir()
    assert dispatch_refusals.launch_refusal(RUN_ID) is None
    assert recovery_liveness._stranded_launch(pointer, now_seconds=now) is False


def test_a_dead_holder_releases_its_pointer_and_directory(
    launch: tuple[dict, float],
) -> None:
    pointer, now = launch
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    _change_claim(holder_pid=process.pid)
    assert recovery_liveness.process_alive(process.pid) is False

    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]
    assert not runs.pointer_path(RUN_ID).exists()
    assert not runs.run_dir(RUN_ID).exists()
    assert dispatch_refusals.launch_refusal(RUN_ID)["reason"] == "launch-span-exceeded"


def test_a_live_pid_started_after_the_claim_does_not_protect_it(
    launch: tuple[dict, float],
) -> None:
    pointer, now = launch
    _change_claim(launched_at=_at(1))
    assert recovery_liveness.process_alive(os.getpid()) is True

    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]
    assert not runs.pointer_path(RUN_ID).exists()
    assert not runs.run_dir(RUN_ID).exists()


def test_a_holder_on_another_host_keeps_the_existing_release_rule(
    launch: tuple[dict, float],
) -> None:
    pointer, now = launch
    _change_claim(holder_host=socket.gethostname() + "-elsewhere")

    assert queue_admission._release_stranded_launches([pointer], now_seconds=now) == [
        RUN_ID
    ]
    assert not runs.run_dir(RUN_ID).exists()


@pytest.mark.parametrize("node_source", [SOURCE_RUN, "source-node"])
def test_a_released_review_trace_names_the_run_it_reviews(
    launch: tuple[dict, float],
    node_source: str,
) -> None:
    pointer, now = launch
    pointer["node"] = {"id": f"review-of-{node_source}", "role": "review"}
    _change_claim(launched_at=_at(1))
    source = {"run_id": SOURCE_RUN, "project": "sample", "node": {"id": "source-node"}}

    assert queue_admission._release_stranded_launches(
        [pointer, source], now_seconds=now
    ) == [RUN_ID]
    assert not runs.run_dir(RUN_ID).exists()
    trace = dispatch_refusals.launch_refusal(RUN_ID)
    assert trace["run_id"] == RUN_ID
    assert trace["holder"]["reviewed_run_id"] == SOURCE_RUN
