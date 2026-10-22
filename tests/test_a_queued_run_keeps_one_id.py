"""A queued run keeps the one id its dispatch returned, from queue to launch.

The id a dispatch prints is the id a coordinator records, cites and later acts
on, so an admission retry and the launch that follows must carry it rather than
minting another. When they mint another, the printed id is dead the moment
admission runs: ``crew discard`` on it finds no live run, and the monitor draws
the request as ``dropped`` beside the run that replaced it.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew import queue_admission, recovery, runs
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    _config_for,
    _lane_document,
    _launcher,
    _queue,
    admit_repo,
)

PROJECT = "proj"
# The admission fold's own words for a run that left without landing. The monitor
# groups these as ``dropped``, which a run that launched must never be drawn as
# under the id it was dispatched with.
DROPPED_WORDS = frozenset({"withdrawn", "discarded", "departed"})


def _fleet(project: str, *, moment: float) -> dict[str, dict]:
    """Every live pointer of the project, reduced to the state a ticker compares."""
    return {
        str(pointer["run_id"]): recovery._watch_snapshot(
            pointer, moment=moment, stall_seconds=3600
        )
        for pointer in runs._list_live_records(project=project)
    }


def test_an_admission_retry_and_the_launch_keep_the_queued_id(
    admit_repo: Path, tmp_path: Path
) -> None:
    """Two held passes and the pass that launches all answer to one run id."""
    config, resolve = _config_for()
    lane = _lane_document(tmp_path, worker_slots=0)
    config["backends"]["clive"]["lane_document"] = str(lane)
    queued, _ = runs.queue_dispatch(
        _queue(admit_repo, node_id="continuity", run_id="r-continuity")
    )

    assert queued["run_id"] == "r-continuity"
    for attempt in range(2):
        retried = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
        assert retried["launched"] == [], f"pass {attempt} found no lane room"
        assert "r-continuity" in retried["held"]
        assert runs.read_pointer("r-continuity")["phase"] == "queued"

    # The lane opens: the launch must publish under the id the dispatch printed.
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 1,
                    "observed_seconds": 900,
                    "sessions": {"coordinator-a": {"worker_slots": 1}},
                    "verdict": "open",
                },
            }
        ),
        encoding="utf-8",
    )
    launched = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert launched["launched"] == ["r-continuity"]
    assert runs.read_pointer("r-continuity")["phase"] != "queued"


def test_the_id_the_dispatch_printed_discards_while_the_request_waits(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The printed id is the one a coordinator can still act on, queued or not."""
    config, _ = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=0)
    )
    runs.queue_dispatch(_queue(admit_repo, node_id="waiting", run_id="r-waiting"))

    discarded = CliRunner().invoke(cli.main, ["crew", "discard", "--run", "r-waiting"])

    assert discarded.exit_code == 0
    assert json.loads(discarded.output)["pointer_removed"] is True
    assert runs.list_live(project=PROJECT) == []


def test_a_launched_run_draws_no_dropped_row_for_the_queued_id(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The monitor draws one run that moved, not one withdrawn and one arriving."""
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=1)
    )
    runs.queue_dispatch(_queue(admit_repo, node_id="continuity", run_id="r-continuity"))
    known = _fleet(PROJECT, moment=1_800_000.0)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    current = _fleet(PROJECT, moment=1_800_001.0)
    folded, _ = recovery.fleet_transitions(known, current, ledger_run_ids=dict)

    assert result["launched"] == ["r-continuity"]
    rows = [
        row
        for row in folded
        if str((row[0] or {}).get("run_id") or "") == "r-continuity"
    ]
    assert [row[2] for row in rows if row[2] in DROPPED_WORDS] == []
