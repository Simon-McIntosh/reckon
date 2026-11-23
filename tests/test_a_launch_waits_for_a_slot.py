"""A queued request holds no process until the router grants it a slot.

The lane's router publishes the extra workers its gate can still carry. A
request that waits for one of them is a stored pointer and nothing else: no
harness, no supervisor, no pid. This test drives the real admission pass over a
temporary crew home against a stand-in router of width 4, queues 20 requests,
and reads two facts at every pass:

* the live worker count is never more than the router's width plus two, so a
  pass never launches past the gate; and
* every request still queued carries no pid, and every process a launch started
  is named by a launched pointer's own pid.

The stand-in router publishes its width the way the real one does: the extra
workers it can carry are the width less the workers already live. A pass that
ignored the figure would launch a fresh batch every pass and walk the live
count past the width.

The router's figure is written once per pass, before the pass runs, the way a
router publishing on an interval is read: a launch a pass has just made is not
in the figure it was admitted against. The two-worker slack is that interval,
and it is what the section's bound allows.

A worker finishing is what a queued request waits for: the last phase reaps one
stand-in process between passes and reads the next request admitted into its
place.

Negative control: making the admission pass read no lane allowance (so every
queued request launches at once) makes the bound's assertion fail on the first
pass.
"""

# ruff: noqa: F811 - the fixture is imported and also named as a test parameter.

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

from reckon.crew import queue_admission, runs
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    SESSION,
    _config_for,
    _queue,
    admit_repo,
)

WIDTH = 4
SLACK = 2
REQUESTS = 20
PASSES = 6


def _live_worker_pointers() -> list[dict]:
    """Every live pointer that stands for a launcher-started worker."""
    return [
        pointer
        for pointer in runs.list_live(project=PROJECT)
        if str(pointer.get("phase") or "") != "queued"
    ]


def _publish_router(lane: Path, *, live_workers: int) -> int:
    """Publish the gate's remaining worker slots as the router computes them.

    The router's ``worker_slots`` is the width less the workers already live,
    never below zero. Both the lane-wide figure and this session's share carry
    it, because the admission pass reads the session's share first.
    """
    slots = max(0, WIDTH - live_workers)
    lane.write_text(
        json.dumps(
            {
                "headroom": WIDTH - live_workers,
                "admission": {
                    "worker_slots": slots,
                    "new_session_worker_slots": slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": slots}},
                    "verdict": "full" if slots <= 0 else "open",
                },
            }
        ),
        encoding="utf-8",
    )
    return slots


def _spawning_launcher(started: list[subprocess.Popen]):
    """A launcher that starts a real long-lived process per launched request.

    The process is the worker stand-in: it outlives the launch and ends only
    when the test reaps it, so a live process in this test is a worker, never
    the test's own interpreter. The launch records the pid on the run, so the
    pointer is what ties a process to a request.
    """

    def launch(plan, *, log_path, stderr_path, prompt_path):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(process)
        return process.pid

    return launch


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a stuck stand-in
        process.wait(timeout=10)


def _reap(started: list[subprocess.Popen]) -> None:
    for process in started:
        _stop(process)


def test_a_queued_request_holds_no_process_until_it_holds_a_slot(
    admit_repo: Path, tmp_path: Path
) -> None:
    config, resolve = _config_for()
    lane = tmp_path / "lane.json"
    config["backends"]["clive"]["lane_document"] = str(lane)
    _publish_router(lane, live_workers=0)
    for index in range(REQUESTS):
        runs.queue_dispatch(
            _queue(
                admit_repo,
                node_id=f"queued-node-{index:02d}",
                run_id=f"r-queued-{index:02d}",
            )
        )
    started: list[subprocess.Popen] = []
    launcher = _spawning_launcher(started)
    try:
        for pass_index in range(PASSES):
            live_before = len(_live_worker_pointers())
            _publish_router(lane, live_workers=live_before)
            queue_admission.admit_once(config_for=resolve, launcher=launcher)

            live_after = _live_worker_pointers()
            assert len(live_after) <= WIDTH + SLACK, (
                f"pass {pass_index}: {len(live_after)} live workers against the "
                f"router's width {WIDTH} plus {SLACK}"
            )
            assert len(live_after) <= len([p for p in started if p.poll() is None]), (
                f"pass {pass_index}: a launched pointer names no live process"
            )
            observed = runs.list_live(project=PROJECT)
            assert len(observed) == REQUESTS
            launched_pids = {
                int(pointer["pid"]) for pointer in live_after if pointer.get("pid")
            }
            assert launched_pids == {
                process.pid for process in started if process.poll() is None
            }, (
                f"pass {pass_index}: {len(launched_pids)} launched pids against "
                f"{len([p for p in started if p.poll() is None])} live processes"
            )
            for pointer in observed:
                if str(pointer.get("phase") or "") != "queued":
                    continue
                run_id = str(pointer["run_id"])
                assert not pointer.get("pid"), (
                    f"queued request {run_id} carries pid {pointer.get('pid')}"
                )
                assert pointer.get("pid_start_time") in (None, ""), (
                    f"queued request {run_id} carries a process start time"
                )
            assert live_after, f"pass {pass_index} launched nothing at all"
            assert len(observed) - len(live_after) > 0, (
                f"pass {pass_index}: no request was left holding"
            )

        # A finished worker frees the gate: the next pass admits the head of
        # the queue into the slot the process held, and every other request
        # keeps holding.
        _stop(started[0])
        del started[0]
        _publish_router(lane, live_workers=len(started))
        queue_admission.admit_once(config_for=resolve, launcher=launcher)
        assert len(started) == WIDTH, (
            "a freed slot did not admit the head of the queue: "
            f"{len(started)} workers against a width of {WIDTH}"
        )
        live_after = _live_worker_pointers()
        assert started[-1].pid in {
            int(pointer["pid"]) for pointer in live_after if pointer.get("pid")
        }, "the request admitted into the freed slot names no process"
        assert len(live_after) <= WIDTH + SLACK
    finally:
        _reap(started)
