"""A review in flight is a wait no coordinator action shortens.

A run whose head a review is reading carries a duty naming the review as
working. The row's own text tells the coordinator to read the run with ``crew
observe``: the reviewer holds a live pointer and the reflex behind it re-fires
when the review ends, so nothing the coordinator runs moves the wait. A stop
that refused on it asked for work nobody at the session could do, and turned a
ten-minute foreground wait into the end of a turn.

The cases here drive the real derivation to name the duty, then the hook -- as
a subprocess, hook JSON on stdin and a published snapshot -- to show the stop is
allowed and the prompt still lists the row under the waits it counts. The last
case holds the other direction: a duty the coordinator can act on still blocks,
and the review in flight beside it is still only counted.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, recovery, runs
from reckon.crew import obligations as obligations_module

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"

PROJECT = "review-in-flight-fixture"
SESSION = "review-in-flight-session"
RUN_ID = "run-scoring-fixture"
NODE_ID = "scoring-fixture-node"
REVIEW_RUN_ID = "run-review-in-flight"
REVIEW_AGE_SECONDS = 1_500

# The blocked run's own row, which is a duty the coordinator acts on: the run
# holds its turn open and the remark is the session's to answer.
BLOCKED_RUN_ID = "run-blocked-fixture"
BLOCKED_NODE_ID = "blocked-fixture-node"

# An age the hook renders from wall-clock distances, such as "25m0s".
AGE = re.compile(r"\d+d\d+h|\d+h\d+m|\d+m\d+s|\d+s")

# A pid no process can hold, so a drive that registers no follower still names a
# harness identity rather than letting the runner's own environment steer it.
_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def checkout(tmp_path: Path, config_home: Path) -> Path:
    """A directory the mounts file names, so the payload's cwd resolves."""
    root = tmp_path / "checkout"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _review_in_flight_row() -> dict[str, object]:
    """One classifier row for a run whose review the fleet lists as working."""
    return {
        "run_id": RUN_ID,
        "node": NODE_ID,
        "plan": "fixture-plan",
        "age_seconds": REVIEW_AGE_SECONDS,
        "classification": "scoring",
        "review_run_id": REVIEW_RUN_ID,
        "review_run_phase": "working",
        "detail": f"{REVIEW_RUN_ID} is reading the head",
        "next_action": f"read the review with `crew observe {REVIEW_RUN_ID}`",
    }


def _review_in_flight_item() -> dict[str, object]:
    """The duty the derivation builds for that row, through the real builder."""
    pointer = {"run_id": RUN_ID, "role": "implement"}
    assert not recovery._is_review_run(pointer)
    item = obligations_module._review_duty_item(
        PROJECT,
        _review_in_flight_row(),
        pointer,
        now=datetime.now(tz=UTC),
        grace=600,
    )
    assert item is not None
    return item


def _blocked_item() -> dict[str, object]:
    """One duty a coordinator can act on, in the shape the derivation emits."""
    return obligations_module._live_item(
        {
            "run_id": BLOCKED_RUN_ID,
            "node": BLOCKED_NODE_ID,
            "plan": "fixture-plan",
            "age_seconds": REVIEW_AGE_SECONDS,
            "next_action": "read the run's manifest; resolve the blocker",
        },
        kind="blocked",
        now=datetime.now(tz=UTC),
    )


def _publish(items: list[dict[str, object]]) -> None:
    """Publish a snapshot carrying exactly these duties, as a producer would."""
    computed_at = datetime.now(tz=UTC)
    document = obligation_snapshot.document_for(
        {
            "project": PROJECT,
            "session": SESSION,
            "obligations": items,
            "summary": {
                "outstanding": len(items),
                "oldest_age_seconds": max(
                    int(item.get("age_seconds") or 0) for item in items
                ),
            },
        },
        computed_at=computed_at,
        stream_offset=0,
        producer={
            "pid": os.getpid(),
            "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
            "started_at": computed_at.isoformat(),
            "code_stamp": obligation_snapshot.source_code_stamp(),
        },
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _hook(mode: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _payload(directory: Path, event: str, **extra: object) -> dict[str, object]:
    return {
        "session_id": SESSION,
        "cwd": str(directory),
        "hook_event_name": event,
        **extra,
    }


def test_the_derivation_names_a_working_review_as_a_wait_no_action_shortens() -> None:
    item = _review_in_flight_item()

    assert item["kind"] == obligations_module.REVIEW_IN_FLIGHT_DUTY_KIND
    assert item["kind"] == "review-in-flight"
    assert item["kind"] in obligations_module.NON_BLOCKING_DUTY_KINDS


def test_a_review_in_flight_does_not_block_a_stop(
    checkout: Path,
) -> None:
    _publish([_review_in_flight_item()])
    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("stop", _payload(checkout, "Stop"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout == "", "a review in flight must not hold a turn open"


def test_the_checklist_counts_a_review_in_flight_with_the_waits_it_cannot_shorten(
    checkout: Path,
) -> None:
    _publish([_review_in_flight_item()])
    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("prompt", _payload(checkout, "UserPromptSubmit"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    checklist = json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    assert re.search(
        r"- \[1 waiting, no action shortens them\] review-in-flight 1 \(oldest \S+\)",
        checklist,
    ), checklist
    assert "work the list to empty" not in checklist


def test_a_duty_the_coordinator_can_act_on_still_blocks_a_stop(
    checkout: Path,
) -> None:
    _publish([_review_in_flight_item(), _blocked_item()])
    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("stop", _payload(checkout, "Stop"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    decision = json.loads(completed.stdout)
    assert decision["decision"] == "block"
    reason = decision["reason"]
    assert re.search(
        rf"- \[blocked\] {BLOCKED_RUN_ID} \({BLOCKED_NODE_ID}, \S+ old\)", reason
    ), reason
    assert re.search(
        r"- \[1 waiting, no action shortens them\] review-in-flight 1 \(oldest \S+\)",
        reason,
    ), reason
    assert "work the list to empty" in reason
