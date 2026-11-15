"""A stopping station exits within a bound and hands its claims back.

A station asked to stop waits on work it did not start: a derivation reading
storage, a dispatcher between its claim and its worker. Nothing on either path
reads the flag a signal sets, so on 15 November two of three restarts were still
running 120 s after SIGTERM and were answered with SIGKILL. A killed station
hands back nothing: it orphaned seven claims at 13:00Z, six of them other
sessions'. This file holds the station to the two promises instead — it exits
inside the bound it states however blocked its work is, and it gives back every
claim that never reached a worker.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims as claims
from reckon.crew import dispatch_refusals, runs, station

REPO_ROOT = Path(__file__).resolve().parents[1]

#: How long past the station's own bound the case waits for its process before
#: calling the station stuck: interpreter start, the lease, the beats, and the
#: shutdown the bound covers. The figure the station states is read from the
#: module, so no case here encodes a number the station owns.
EXIT_SLACK_SECONDS = 60.0


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every run path this process resolves at a home of its own."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    return tmp_path / "crew"


def _publish(
    run_id: str,
    *,
    step: str,
    holder_pid: int | None = None,
    worker_pid: int | None = None,
    project: str = "sample",
) -> dict:
    """Publish one run's launch claim and live pointer the way a launch does.

    The claim carries the fields ``_launch_holder`` writes, so a case turns on
    the step and the holder rather than on a shape invented here.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    launched = time.time()
    claims._write_json(
        directory / "claim.json",
        {
            "run_id": run_id,
            "claim_registered_at": _at(launched),
            "holder_pid": os.getpid() if holder_pid is None else holder_pid,
            "holder_host": socket.gethostname(),
            "launched_at": _at(launched),
            "step": step,
            "step_at": _at(launched),
        },
    )
    pointer: dict = {
        "run_id": run_id,
        "project": project,
        "session": "s-1",
        "phase": "starting",
        "created_at": _at(launched),
    }
    if worker_pid is not None:
        pointer["phase"] = "dispatched"
        pointer["pid"] = worker_pid
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def test_a_claim_short_of_its_worker_is_handed_back_and_a_working_one_is_left(
    crew_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The step, not the phase, decides whose claim is given up.

    A claim short of its worker belongs to a launch that will never finish, so
    handing it back is the only thing that lets the run be dispatched again. A
    claim at the worker's step stands behind a running worker, and giving it up
    would let the queue launch a second worker over the first. A claim another
    process published is not this process's to give back at all.
    """
    unlaunched = "r-20261115T130000000000-unlaunched-a"
    running = "r-20261115T130000000000-running-b"
    foreign = "r-20261115T130000000000-foreign-c"
    _publish(unlaunched, step=claims.LAUNCH_CLAIM_STEP)
    _publish(running, step=claims.LAUNCH_WORKER_STEP, worker_pid=os.getpid())
    _publish(foreign, step=claims.LAUNCH_CLAIM_STEP, holder_pid=os.getpid() + 1)

    released = claims.release_unlaunched_claims()

    assert released == (unlaunched,)
    assert not runs.pointer_path(unlaunched).exists()
    assert not runs.run_dir(unlaunched).exists()
    refusal = dispatch_refusals.launch_refusal(unlaunched)
    assert refusal is not None, "a handed-back claim records the step it reached"
    assert refusal["step"] == claims.LAUNCH_CLAIM_STEP
    assert runs.pointer_path(running).exists()
    assert claims.read_launch_claim(running)["step"] == claims.LAUNCH_WORKER_STEP
    assert runs.pointer_path(foreign).exists()
    assert claims.read_launch_claim(foreign)["step"] == claims.LAUNCH_CLAIM_STEP
    assert "1 unlaunched" in capsys.readouterr().err


def test_a_queued_request_is_put_back_in_the_queue_not_deleted(crew_home: Path) -> None:
    """A launch taken from the queue carries the queue's only copy of it.

    Removing its run directory takes the request with it, which is how queued
    requests vanished on 14 November. Handing the claim back puts the request
    where the next admission pass finds it, and keeps the brief the launch had
    already stored beside it.
    """
    run_id = "r-20261115T130000000000-queued"
    pointer = _publish(run_id, step=claims.LAUNCH_CLAIM_STEP)
    request = {
        **pointer,
        "phase": "queued",
        "backend": "clive",
        "queued_at": pointer["created_at"],
        "node": {"id": "queued-node", "plan": "plan-a", "section": "s1"},
    }
    runs.write_handover(request, admitted_by={"pid": os.getpid()})
    brief = runs.run_dir(run_id) / "brief.md"
    brief.write_text("the request's brief\n", encoding="utf-8")

    assert claims.release_unlaunched_claims() == (run_id,)

    restored = runs.read_pointer(run_id)
    assert restored["phase"] == "queued"
    assert restored["node"]["id"] == "queued-node"
    assert brief.is_file(), "the request keeps the brief it was launched with"
    assert not (runs.run_dir(run_id) / "claim.json").exists()
    assert runs.find_handover(run_id) is None


#: The station this case starts, as a script. It publishes one claim short of a
#: worker and one at the same step as a blocked dispatcher would, then blocks
#: its own tick and its own drain — a derivation reading storage and a dispatch
#: between its claim and its worker, neither of which reads the stop flag.
_CHILD = textwrap.dedent(
    """
    import os
    import socket
    import threading
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from reckon.crew import dispatch_claims as claims, runs, station

    BLOCK = threading.Event()


    def publish(run_id, step, *, worker_pid=None):
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        launched = time.time()
        claims._write_json(
            directory / "claim.json",
            {
                "run_id": run_id,
                "claim_registered_at": claims._utc_now(),
                "holder_pid": os.getpid(),
                "holder_host": socket.gethostname(),
                "launched_at": claims._utc_now(),
                "step": step,
                "step_at": claims._utc_now(),
            },
        )
        pointer = {
            "run_id": run_id,
            "project": "sample",
            "session": "s-1",
            "phase": "starting",
            "created_at": claims._utc_now(),
        }
        if worker_pid is not None:
            pointer["phase"] = "dispatched"
            pointer["pid"] = worker_pid
        runs._write_json(runs.pointer_path(run_id), pointer)


    class Duties:
        def drain(self, timeout=None):
            BLOCK.wait()

        def quiesce(self):
            return False

        def resume(self):
            BLOCK.clear()


    class Reflex:
        def start(self):
            return None

        def stop(self):
            return None


    class Stuck:
        # The tick and the derivation are both blocked, as a station in
        # trouble has to be for its own stop check never to be reached.

        def __init__(self, store):
            self.store = store
            self.home = Path(os.environ["RECKON_HOME"])
            self.clock = lambda: datetime.now(tz=UTC)
            self.duties = Duties()
            self.reflex = Reflex()

        def tick(self, *, now=None):
            publish("r-child-unlaunched", claims.LAUNCH_CLAIM_STEP)
            publish(
                "r-child-running", claims.LAUNCH_WORKER_STEP, worker_pid=os.getpid()
            )
            BLOCK.wait()
            return []


    from reckon.crew import station_store

    store = station_store.StationStore(Path(os.environ["RECKON_HOME"]) / "store")
    station.run_station(interval=0.05, station_instance=Stuck(store), store=store)
    """
)


def test_a_station_stopped_while_blocked_exits_inside_its_bound(
    crew_home: Path,
) -> None:
    """SIGTERM is answered by an exit inside the stated bound, not ignored.

    The child blocks a tick and a drain, then waits to be signalled. A station
    whose shutdown waits for that work never reaches its own stop check, which
    is how SIGTERM came to be ignored for 120 s and answered with SIGKILL. The
    exit inside the bound is what keeps the claims from being orphaned, so the
    case measures both: the process ends without a SIGKILL, and the claims it
    could not have launched are gone from the fleet while the one whose worker
    it named stays.
    """
    bound = station.STATION_STOP_BOUND_SECONDS
    environment = dict(
        os.environ, PYTHONPATH=str(REPO_ROOT), RECKON_HOME=str(crew_home)
    )
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        unlaunched = runs.pointer_path("r-child-unlaunched")
        running = runs.pointer_path("r-child-running")
        deadline = time.monotonic() + EXIT_SLACK_SECONDS
        while time.monotonic() < deadline:
            if unlaunched.exists() and running.exists():
                break
            assert child.poll() is None, child.communicate()
            time.sleep(0.05)
        assert unlaunched.exists() and running.exists(), "the station never published"
        # The handlers install before the first tick, so a claim on disk is
        # proof the child is listening for the signal the case now sends.
        signalled_at = time.monotonic()
        child.send_signal(signal.SIGTERM)
        _stdout, stderr = child.communicate(timeout=bound + EXIT_SLACK_SECONDS)
        elapsed = time.monotonic() - signalled_at
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()

    assert child.returncode == 0, stderr
    assert elapsed < bound + EXIT_SLACK_SECONDS, f"exit\n{elapsed:.1f}s\n{stderr}"
    assert "station-stop:" in stderr, stderr
    assert not unlaunched.exists(), "a claim short of its worker is handed back"
    assert not runs.run_dir("r-child-unlaunched").exists()
    assert running.exists(), "a claim whose worker is running is left in place"
    kept = runs.run_dir("r-child-running") / "claim.json"
    assert json.loads(kept.read_text(encoding="utf-8"))["step"] == (
        claims.LAUNCH_WORKER_STEP
    )
