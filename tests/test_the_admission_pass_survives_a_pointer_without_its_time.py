"""The admission pass survives a queued pointer that carries no arrival time.

The pass ranks its lane's queued pointers by when they arrived, and the
ordering refuses a pointer whose arrival time cannot be read. That refusal
belongs to the pointer, not to the pass. Left to propagate, one such pointer —
a fixture, or a hand-edited record in a real crew home — ends the pass, and the
admit service with it, so no queued request on the lane launches for any
session until someone removes the pointer by hand.

So the pass leaves that pointer out of the ordering, names it in a verdict
with the reason it was not placed, and admits every other queued request in the
order it would otherwise have given them. The refusal stays in the ordering
itself, which is what the last case here holds: the narrowing lives in the
pass, so the read surfaces and the ordering keep their own behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs

PROJECT = "proj"
SESSION = "coordinator-a"
LANE = "clive"
PLACED_OLDER = "r-placed-older"
PLACED_NEWER = "r-placed-newer"
UNREADABLE = "r-unplaceable"

CONFIG = {
    "default_backend": LANE,
    "local_backend": LANE,
    "backends": {
        LANE: {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and a checkout the shared dispatch path can launch."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    quote = '"'
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>",
                f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>",
                f"<meta name={quote}plan-slug{quote} content={quote}plan-a{quote}>",
                f"<h2 id={quote}dispatch{quote}>Dispatch</h2>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "add", "--", "skills")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _config_with_lane(lane_document: Path) -> tuple[dict[str, Any], Any]:
    config = json.loads(json.dumps(CONFIG))
    config["backends"][LANE]["lane_document"] = str(lane_document)

    def resolve(_pointer):
        return config

    return config, resolve


def _lane_document(tmp_path: Path, *, worker_slots: float) -> Path:
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": worker_slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": worker_slots}},
                    "verdict": "open",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _queued_request(repository: Path, run_id: str, *, node_id: str) -> dict[str, Any]:
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id=node_id,
        goal="admit this queued node",
        plan="plan-a",
        section="dispatch",
        done_when=(
            "pytest tests/test_the_admission_pass_survives_a_pointer_without_its_time.py"
            " reports passed"
        ),
        write_paths=[f"{node_id}.txt"],
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
        "backend": LANE,
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


def _stamp_queued_at(run_id: str, moment: datetime) -> None:
    """Rewrite one queued pointer's arrival time, as a hand-edited record reads."""
    from reckon import crew

    pointer = runs.read_pointer(run_id)
    pointer["queued_at"] = moment.isoformat()
    crew._write_json(runs.pointer_path(run_id), pointer)


def _queued_without_its_time(tmp_path: Path, run_id: str) -> None:
    """Write one queued pointer carrying no readable ``queued_at`` at all."""
    from reckon import crew

    stream = tmp_path / "unplaceable-stream.jsonl"
    stream.write_text("", encoding="utf-8")
    crew._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "backend": LANE,
            "launch": "cli",
            "local": True,
            "phase": "queued",
            "created_at": datetime.now(UTC).isoformat(),
            "log_path": str(stream),
            "manifest_path": str(tmp_path / f"{run_id}-manifest.md"),
            "worktree": str(tmp_path / f"{run_id}-worktree"),
            "node": {"id": run_id, "plan": PROJECT, "time_budget": "200s"},
        },
    )


def _launcher(recorded: list) -> Any:
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _verdict_log() -> Path:
    return runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG


def _queue_with_one_unplaceable(
    repository: Path, tmp_path: Path, *, worker_slots: float
) -> tuple[dict[str, Any], Any]:
    """Queue two placeable requests and one without a readable arrival time.

    The newer request is queued first, so the order the pass admits them in is
    its own ordering rather than the order the records were written in.
    """
    lane = _lane_document(tmp_path, worker_slots=worker_slots)
    config, resolve = _config_with_lane(lane)
    now = datetime.now(UTC)
    runs.queue_dispatch(_queued_request(repository, PLACED_NEWER, node_id="newer-node"))
    runs.queue_dispatch(_queued_request(repository, PLACED_OLDER, node_id="older-node"))
    _stamp_queued_at(PLACED_OLDER, now - timedelta(minutes=3))
    _stamp_queued_at(PLACED_NEWER, now - timedelta(minutes=1))
    _queued_without_its_time(tmp_path, UNREADABLE)
    return config, resolve


def test_the_pass_admits_the_placed_requests_and_names_the_unplaceable_one(
    admit_repo: Path, tmp_path: Path
) -> None:
    _config, resolve = _queue_with_one_unplaceable(admit_repo, tmp_path, worker_slots=2)
    recorded: list = []

    # The instrument check: the malformed pointer must be in the snapshot the
    # pass reads, or its absence from the order would prove nothing.
    held = {str(pointer.get("run_id") or ""): pointer for pointer in runs.list_live()}
    assert {PLACED_OLDER, PLACED_NEWER, UNREADABLE} <= set(held), sorted(held)
    assert held[UNREADABLE].get("queued_at") is None

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert len(result["launched"]) == 2, result["launched"]
    # A launch re-publishes the request under its own run id, so the ordering is
    # read from the marker each launched run carries back to the request it was
    # admitted from rather than from the ids themselves.
    admitted_from = [
        str(runs.read_pointer(run_id).get("admit_request", {}).get("run_id") or "")
        for run_id in result["launched"]
    ]
    assert admitted_from == [PLACED_OLDER, PLACED_NEWER], admitted_from
    assert len(recorded) == 2, "both placeable requests reached the launch"
    verdicts = {row["run_id"]: row for row in result["verdicts"]}
    assert verdicts[PLACED_OLDER]["verdict"] == "launched"
    assert verdicts[PLACED_NEWER]["verdict"] == "launched"
    unplaceable = verdicts[UNREADABLE]
    assert unplaceable["verdict"] == "unplaced"
    assert "queued_at" in unplaceable["detail"]
    assert runs.read_pointer(UNREADABLE)["phase"] == "queued"


def test_the_unplaceable_verdict_reaches_the_admission_verdict_log(
    admit_repo: Path, tmp_path: Path
) -> None:
    # No slot is granted, so the pass holds the placeable requests rather than
    # launching them: the verdict record is what this case is about, and a held
    # pass writes it the same way.
    _config, resolve = _queue_with_one_unplaceable(admit_repo, tmp_path, worker_slots=0)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    queue_admission._record_pass_verdicts(result, lane="", code_stamp="test-stamp")

    lines = [
        json.loads(line)
        for line in _verdict_log().read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert lines, "a pass that named an unplaceable pointer wrote a line"
    rows = {row["run_id"]: row for row in lines[-1]["pointers"]}
    assert rows[UNREADABLE]["verdict"] == "unplaced"
    assert "queued_at" in rows[UNREADABLE]["detail"]


def test_the_admission_ordering_still_refuses_a_pointer_without_its_time() -> None:
    """The narrowing lives in the pass: the ordering keeps its own refusal."""
    from reckon.crew.queue_order import admission_order

    now = datetime.now(UTC)
    queued = [
        {
            "run_id": PLACED_OLDER,
            "session": SESSION,
            "backend": LANE,
            "queued_at": (now - timedelta(minutes=3)).isoformat(),
        },
        {"run_id": UNREADABLE, "session": SESSION, "backend": LANE},
    ]
    with pytest.raises(ValueError, match="no readable queued_at"):
        admission_order(queued, [], now=now)
