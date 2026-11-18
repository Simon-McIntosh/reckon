"""A step's own run identity protects its scratch before its pointer is visible."""

import importlib
import os
import subprocess
import sys

import pytest

from reckon.crew.gc import RUN_SCRATCH_LIVE, settle_run_scratch
from reckon.crew.runs import pointer_path, run_dir

launch = importlib.import_module("reckon.crew.dispatch_launch")
RESUMED_RUN = "r-20200101T000000000001-resumed"
ENDED_RUN = "r-20200101T000000000002-ended"


@pytest.fixture()
def scratch_root(tmp_path, monkeypatch):
    root = (tmp_path / "scratch").resolve()
    root.mkdir()
    monkeypatch.setenv(launch.WORKER_SCRATCH_ROOT_ENV, str(root))
    return root


def _ended_attempt(run_id, record_name="exit.json"):
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / record_name).write_text("{}\n", encoding="utf-8")
    assert not pointer_path(run_id).exists()


@pytest.mark.parametrize("record_name", ["exit.json", "attempt-first-exit.json"])
@pytest.mark.parametrize("node_name", ["launch-host", "reservation-host"])
def test_a_resumed_step_reaches_its_worker_without_a_visible_pointer(
    tmp_path, monkeypatch, scratch_root, record_name, node_name
):
    root = scratch_root / node_name
    root.mkdir()
    monkeypatch.setenv(launch.WORKER_SCRATCH_ROOT_ENV, str(root))
    _ended_attempt(RESUMED_RUN, record_name)
    _ended_attempt(ENDED_RUN)
    ended_scratch = root / ENDED_RUN
    ended_scratch.mkdir()
    resumed_scratch = root / RESUMED_RUN
    reached = tmp_path / "worker-reached"
    environment = launch._worker_runtime_environment(
        os.environ,
        run_id=RESUMED_RUN,
        manifest_path=str(tmp_path / "manifest"),
        attempt_started_at="2020-01-02T00:00:00Z",
        coordinator_session="test-session",
        claude_headers=False,
        placed_step=True,
    )
    environment.update(
        {
            launch.STEP_NODE_DIRS_ENV: str(resumed_scratch),
            "RECKON_STEP_RECORD": str(tmp_path / "step-record"),
            "SLURM_JOB_ID": "1",
            "SLURM_STEP_ID": "1",
        }
    )
    worker = (
        "import os; from pathlib import Path; "
        "scratch = Path(os.environ['TMPDIR']); "
        "assert scratch.is_dir(), 'resumed scratch removed before worker'; "
        "(scratch / 'worker-temp').write_text('worker reached scratch'); "
        "Path(__import__('sys').argv[1]).write_text('worker reached')"
    )

    completed = subprocess.run(
        [
            "bash",
            "-c",
            launch._STEP_TASK_WRAPPER,
            "step",
            sys.executable,
            "-c",
            worker,
            str(reached),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert resumed_scratch.is_dir(), completed.stderr
    assert completed.returncode == 0, completed.stderr
    assert reached.read_text() == "worker reached"
    assert (resumed_scratch / "worker-temp").read_text() == "worker reached scratch"
    assert not ended_scratch.exists(), "the step must still settle an ended peer"


@pytest.mark.parametrize("apply", [False, True])
def test_settle_reports_its_current_run_as_live_without_a_pointer(
    monkeypatch, scratch_root, apply
):
    _ended_attempt(RESUMED_RUN)
    _ended_attempt(ENDED_RUN)
    own = scratch_root / RESUMED_RUN
    own.mkdir()
    peer = scratch_root / ENDED_RUN
    peer.mkdir()
    monkeypatch.setenv("RECKON_RUN_ID", RESUMED_RUN)

    report = settle_run_scratch(apply=apply)

    entry = next(row for row in report["entries"] if row["run_id"] == RESUMED_RUN)
    assert entry["state"] == RUN_SCRATCH_LIVE
    assert entry["withheld"] == "current run"
    assert entry["bytes"] is None
    assert own.is_dir()
    assert peer.exists() is not apply
    assert report["removed"] == ([str(peer)] if apply else [])
