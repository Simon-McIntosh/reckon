"""A run on another node is described as on another node.

The classifier's catch-all running arm covers every run whose liveness this
host cannot prove. §10 gave it a way in for a foreign run: an unknown liveness
with a stream still moving is read from that life rather than as gone. But the
arm's detail text was written for the other cause of an unknown liveness — a
pid-less run launched in the coordinator's own harness — and told the reader
"an in-harness run: liveness belongs to the calling harness". A coordinator
reading a row for a CLI worker on another host was told the run belonged to its
own harness.

The arm now tells the two cases apart. A pointer naming another machine gets a
detail naming that host and saying its stream is moving; an in-harness run
keeps today's wording. The classification itself does not change — both read
``running`` — only the sentence that describes why.

Everything the classifier resolves through the environment is pointed at
``tmp_path`` so the real crew directories are untouched.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from reckon.crew import recovery

ASSISTANT_RECORD = '{"type":"assistant","message":"working"}\n'
OTHER_HOST = "another-login-node"


def _pointer(
    run_id: str,
    *,
    stream: Path,
    created_at: float,
    launcher_host: str | None,
    pid: int | None = None,
    process_alive: bool | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "40m"},
        "phase": "working",
        "attempt": 1,
        "created_at": recovery.datetime.fromtimestamp(
            created_at, tz=recovery.timezone.utc
        ).isoformat(),
        "manifest_path": str(stream.parent / "manifest.md"),
        "log_path": str(stream),
        "process_alive": process_alive,
        "pid": pid,
        "launcher_host": launcher_host,
    }


def _stream(tmp_path: Path, run_id: str, *, age_seconds: float) -> Path:
    stream = tmp_path / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(ASSISTANT_RECORD)
    stamp = time.time() - age_seconds
    os.utime(stream, (stamp, stamp))
    return stream


def test_a_foreign_run_with_a_moving_stream_names_its_host(
    tmp_path: Path, home: Path
) -> None:
    # The pointer names another machine, so this host's process table cannot be
    # asked about its pid and liveness reads unknown; its newest stream carries
    # an assistant turn inside the stall window, so the run is in motion. §10
    # reads that as running, and the running arm must name the host rather than
    # call the run one of this harness's.
    now = time.time()
    pointer = _pointer(
        "r-foreign-moving",
        stream=_stream(tmp_path, "r-foreign-moving", age_seconds=10),
        created_at=now - 30,
        launcher_host=OTHER_HOST,
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] == "running"
    assert OTHER_HOST in row["detail"]
    assert "in-harness" not in row["detail"]
    assert "moving" in row["detail"]


def test_an_in_harness_run_keeps_its_wording(tmp_path: Path, home: Path) -> None:
    # A pid-less run launched on this host belongs to the calling harness; no
    # process table here will ever prove its liveness, so it is reported as
    # running with the sentence written for that case, unchanged.
    now = time.time()
    pointer = _pointer(
        "r-in-harness-moving",
        stream=_stream(tmp_path, "r-in-harness-moving", age_seconds=10),
        created_at=now - 30,
        launcher_host=socket.gethostname(),
    )

    row = recovery.classify_pointer(pointer, now_seconds=now)

    assert row["classification"] == "running"
    assert "in-harness" in row["detail"]
    assert OTHER_HOST not in row["detail"]


def test_the_real_crew_home_is_untouched(tmp_path: Path, home: Path) -> None:
    # Every environment-resolved directory this module reads is under the test's
    # own home, so classifying a foreign run writes nothing to the operator's.
    from tests.conftest import real_store_is_not_a_fixture_target

    real_store_is_not_a_fixture_target(home)
