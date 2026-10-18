"""A held dispatch attaches its session before the admission service retries it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_admission import LaneHeld
from tests import test_dispatch_session_host as host_tests

pytestmark = pytest.mark.arms_watch_producer
isolated_project = host_tests.isolated_project


@pytest.fixture()
def queued_with_host(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config_home, repo = isolated_project
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    host_tests._claude_env(monkeypatch, runtime)
    fifo = host_tests._session_host_fifo(runtime)
    fifo.parent.mkdir(parents=True, exist_ok=True)
    import os

    os.mkfifo(fifo)
    host = host_tests._StubHost(fifo, "sample", "hosted-session")
    watcher = host_tests._spawn_runner()
    lane = tmp_path / "lane.json"
    config = json.loads(json.dumps(host_tests.CONFIG))
    config["default_backend"] = "clive"
    config["local_backend"] = "clive"
    config["backends"]["clive"] = {
        **config["backends"].pop("alpha"),
        "lane_document": str(lane),
    }

    def slots(count: int) -> None:
        lane.write_text(
            json.dumps(
                {
                    "headroom": 4,
                    "admission": {
                        "worker_slots": count,
                        "observed_seconds": 900,
                        "sessions": {"hosted-session": {"worker_slots": count}},
                    },
                }
            ),
            encoding="utf-8",
        )

    slots(0)
    try:
        host_tests._register_watcher("sample", watcher.pid)
        host.start()
        with pytest.raises(LaneHeld) as held:
            crew.dispatch(
                node=host_tests._node(config_home, "hosted-session"),
                project="sample",
                repo=repo,
                config=config,
                session="hosted-session",
                local=True,
                launcher=lambda *args, **kwargs: 4242,
                watch_required=True,
                dispatch_options={"local": True},
            )
        request = runs.read_pointer(held.value.queued["run_id"])
        yield request, host, slots, config, monkeypatch
    finally:
        host.stop()
        watcher.terminate()
        watcher.wait(timeout=5)


def test_queued_request_attaches_before_dispatch_returns(queued_with_host) -> None:
    request, host, _slots, _config, _monkeypatch = queued_with_host
    assert request["phase"] == "queued"
    assert host.line == b'{"project": "sample", "session": "hosted-session"}'
    assert crew.watch_state("sample", session="hosted-session")["session_attached"]


def test_one_admission_pass_launches_without_follower_refusal(queued_with_host) -> None:
    request, _host, slots, config, monkeypatch = queued_with_host
    slots(1)
    monkeypatch.delenv("CLAUDE_PID")
    result = queue_admission.admit_once(
        config_for=lambda _pointer: config,
        launcher=lambda *args, **kwargs: 4242,
    )
    assert len(result["launched"]) == 1, result
    assert not result["failed"]
    assert "_FollowerAdmissionUnmet" not in str(result)
    assert (
        runs.read_pointer(result["launched"][0])["admitted_from"] == request["run_id"]
    )
