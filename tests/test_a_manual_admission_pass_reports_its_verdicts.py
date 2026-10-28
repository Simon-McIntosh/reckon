"""A manual admission pass reports the verdicts it reaches, as it reaches them.

The bare ``crew admit`` verb is the fleet's admit service, and it decided every
queued request in silence: a coordinator that ran it by hand could not tell
whether it was working or waiting, and killed it without ever learning what it
had decided. The loop now prints each pass's verdicts as one JSON line — the
same record ``admission-verdicts.jsonl`` holds for that pass — so a caller sees
every decision as it is made. ``--once`` stays the single bounded pass.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import queue_admission, runs
from tests.test_each_admission_pass_records_its_verdict import (  # noqa: F401 - fixtures
    _config_with_lane,
    _session_lane,
    admit_repo,
)

PROJECT = "proj"
SESSION = "coordinator-a"
ADMITTED_ID = "r-admitted"
REFUSED_ID = "r-refused"
HOLDER_ID = "r-holder"

ADMITTED_PATH = "admitted-node.txt"
REFUSED_PATH = "refused-node.txt"


def _queue(
    repository: Path, *, run_id: str, node_id: str, write_path: str
) -> dict[str, Any]:
    """One queued request, ready for the admission pass to read."""
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id=node_id,
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when=(
            "pytest tests/test_a_manual_admission_pass_reports_its_verdicts.py passes"
        ),
        write_paths=[write_path],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": "clive",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "hold": {"held": True},
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": SESSION,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


def _running_holder(repository: Path) -> None:
    """A live run already holding the path the refused request claims.

    Queued first, because that is how a pointer is published, then moved to the
    running phase with a live pid and a worktree: a claim travels with those,
    and a stopped holder's claim would not bind.
    """
    runs.queue_dispatch(
        _queue(
            repository, run_id=HOLDER_ID, node_id="holder-node", write_path=REFUSED_PATH
        )
    )

    def claiming(pointer: dict[str, Any]) -> dict[str, Any]:
        return {
            **pointer,
            "phase": "running",
            "pid": os.getpid(),
            "worktree": str(repository),
        }

    runs._mutate_pointer(HOLDER_ID, claiming)


def _launcher(recorded: list) -> Any:
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _verdict_log() -> Path:
    return runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG


def _logged_passes() -> list[dict[str, Any]]:
    path = _verdict_log()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _printed_passes(output: str) -> list[dict[str, Any]]:
    """The JSON lines the bare verb printed that are admission passes."""
    lines: list[dict[str, Any]] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "pointers" in parsed:
            lines.append(parsed)
    return lines


def _drive_bare_verb(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane_document: Path,
    resolve: Any,
    launcher: Any,
    lanes: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Drive the bare ``crew admit`` verb for ``len(lanes)`` passes.

    ``admit_once`` is wrapped so the loop is driven with a stub launch and a
    fixed config, and the wrapper ends the loop after the last pass asked for.
    The publishing under test stays inside ``admit_until_stopped``, which is
    the loop the bare verb runs.
    """
    calls: list[int] = []
    real_admit_once = queue_admission.admit_once

    def wrapper(*, lane: str = "") -> dict[str, Any]:
        number = len(calls) + 1
        if number > len(lanes):
            raise KeyboardInterrupt
        calls.append(number)
        lane_document.write_text(json.dumps(lanes[number - 1]), encoding="utf-8")
        return real_admit_once(lane=lane, config_for=resolve, launcher=launcher)

    monkeypatch.setattr(queue_admission, "admit_once", wrapper)
    monkeypatch.setattr(queue_admission, "_pause", lambda _seconds: None)
    invoked = CliRunner().invoke(cli.main, ["crew", "admit"])
    assert invoked.exit_code == 0, invoked.output
    final = json.loads(
        [line for line in invoked.output.splitlines() if line.strip().startswith("{")][
            -1
        ]
    )
    return final, _printed_passes(invoked.output)


def test_each_pass_prints_its_verdicts_as_one_json_line(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two passes over one admitted and one scope-refused request are visible.

    The first pass has no room, so both requests are held for their session's
    allowance. The second has room for both: the admitted request is launched
    through the shared dispatch path, and the one claiming a path a live run
    already holds is held on that claim, naming the holder. Every pass prints
    one line naming both requests, and each line is the record the verdict log
    holds for that same pass.
    """
    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)
    runs.queue_dispatch(
        _queue(
            admit_repo, run_id=ADMITTED_ID, node_id="a-node", write_path=ADMITTED_PATH
        )
    )
    runs.queue_dispatch(
        _queue(admit_repo, run_id=REFUSED_ID, node_id="b-node", write_path=REFUSED_PATH)
    )
    _running_holder(admit_repo)
    recorded: list = []

    final, printed = _drive_bare_verb(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=_launcher(recorded),
        lanes=[_session_lane(0.0), _session_lane(2.0)],
    )

    assert final["passes"] == 2
    assert len(printed) == 2, printed
    # Every pass names both queued requests.
    for line in printed:
        assert {row["run_id"] for row in line["pointers"]} == {
            ADMITTED_ID,
            REFUSED_ID,
        }
    # The printed lines are the log's own rows for the same passes.
    assert printed == _logged_passes()

    held = {row["run_id"]: row for row in printed[0]["pointers"]}
    assert held[ADMITTED_ID]["verdict"] == "held-for-allowance"
    assert held[REFUSED_ID]["verdict"] == "held-for-allowance"

    decided = {row["run_id"]: row for row in printed[1]["pointers"]}
    assert decided[ADMITTED_ID]["verdict"] == "launched"
    assert decided[REFUSED_ID]["verdict"] == "held-on-claim"
    # The hold names the holder and the contested path, which is the detail a
    # coordinator needs to clear it. Nothing was attempted, so no failure is
    # recorded for it.
    hold = decided[REFUSED_ID]["detail"]
    assert HOLDER_ID in hold, hold
    assert REFUSED_PATH in hold, hold
    assert "failure" not in decided[REFUSED_ID], decided[REFUSED_ID]


def test_the_once_flag_prints_one_pass_and_no_loop_line(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--once`` keeps reporting its single pass to its caller."""
    lane = tmp_path / "lane.json"
    _config, resolve = _config_with_lane(lane)
    lane.write_text(json.dumps(_session_lane(0.0)), encoding="utf-8")
    runs.queue_dispatch(
        _queue(
            admit_repo, run_id=ADMITTED_ID, node_id="a-node", write_path=ADMITTED_PATH
        )
    )
    real_admit_once = queue_admission.admit_once

    def wrapper(*, lane: str = "") -> dict[str, Any]:
        return real_admit_once(lane=lane, config_for=resolve, launcher=_launcher([]))

    monkeypatch.setattr(queue_admission, "admit_once", wrapper)
    invoked = CliRunner().invoke(cli.main, ["crew", "admit", "--once"])

    assert invoked.exit_code == 0, invoked.output
    lines = [line for line in invoked.output.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    payload = json.loads(lines[0])
    assert payload["verdicts"][0]["run_id"] == ADMITTED_ID
    # The single pass answers with its own result, not with the loop's record.
    assert "pointers" not in payload


def test_the_help_names_the_loop_the_single_pass_and_the_pass_bound() -> None:
    """A caller reading the help learns which form returns, and how bounded."""
    invoked = CliRunner().invoke(cli.main, ["crew", "admit", "--help"])

    assert invoked.exit_code == 0, invoked.output
    help_text = " ".join(invoked.output.split())
    assert "is the loop" in help_text
    assert "is the single manual pass" in help_text
    assert "--once" in help_text
    assert "five-second" in help_text
