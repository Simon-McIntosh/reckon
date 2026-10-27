"""A coordinator can pause queued work and restore its place without launching it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import obligations, queue_admission, queue_edit, runs
from reckon.crew.recovery_classification import classify_pointer
from reckon.mcp_crew_actions import crew_action


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(root))
    (root / "crew" / "live").mkdir(parents=True)
    (root / "crew" / "locks").mkdir(parents=True)
    return root


def _request(
    home: Path,
    run_id: str,
    *,
    plan: str = "alpha",
    phase: str = "queued",
    priority: int = 0,
    queued_at: datetime | None = None,
) -> dict:
    record = {
        "run_id": run_id,
        "project": "fixture",
        "session": "owner",
        "backend": "local",
        "phase": phase,
        "queued_at": (queued_at or datetime.now(UTC)).isoformat(),
        "node": {"id": run_id, "plan": plan, "write_paths": []},
        "priority_override": priority,
        "waits": ["alpha#upstream"] if phase == "awaiting-upstream" else [],
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def test_hold_skips_the_head_and_release_launches_it_next(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _request(home, "r-first", priority=9)
    _request(home, "r-second", priority=1)
    assert [
        p["run_id"]
        for p in queue_admission.fair_share_admission_order(
            runs.list_live(), runs.list_live(), now=datetime.now(UTC), config_for=None
        )
    ] == ["r-first", "r-second"]

    queue_edit.hold("r-first", reason="reviewing")
    assert runs.read_pointer("r-first")["phase"] == queue_admission.HELD_PHASE
    assert [
        p["run_id"]
        for p in queue_admission.fair_share_admission_order(
            [p for p in runs.list_live() if p["phase"] == "queued"],
            runs.list_live(),
            now=datetime.now(UTC),
            config_for=None,
        )
    ] == ["r-second"]

    launched = []
    monkeypatch.setattr(
        queue_admission, "_dispatch_fleet_gate", lambda: {"state": "open"}
    )
    monkeypatch.setattr(
        queue_admission, "_session_allowance_read", lambda *a, **k: (1, "fixture")
    )
    monkeypatch.setattr(queue_admission, "_lane_gate_hold", lambda *a, **k: None)
    monkeypatch.setattr(queue_admission, "_retry_backoff_hold", lambda *a, **k: None)
    monkeypatch.setattr(
        queue_admission, "_require_launched_record", lambda value: value
    )

    def launch(pointer, **kwargs):
        launched.append(pointer["run_id"])
        runs._mutate_pointer(
            pointer["run_id"], lambda current: {**current, "phase": "running"}
        )
        return {"run_id": pointer["run_id"], "phase": "running"}

    monkeypatch.setattr(queue_admission, "_launch_queued", launch)
    queue_admission.admit_once(lock=False)
    assert launched == ["r-second"]
    assert runs.read_pointer("r-first")["phase"] == "held"

    queue_edit.release("r-first")
    queue_admission.admit_once(lock=False)
    assert launched == ["r-second", "r-first"]
    assert (
        runs.read_pointer("r-first")["priority_override"] == first["priority_override"]
    )


def test_plan_hold_restores_each_phase_and_pauses_starvation_age(home: Path) -> None:
    joined = datetime.now(UTC) - timedelta(hours=26)
    _request(home, "r-ready", priority=7, queued_at=joined)
    _request(home, "r-waiting", phase="awaiting-upstream", queued_at=joined)
    _request(home, "r-other", plan="beta", queued_at=joined)
    held = queue_edit.hold(plan="alpha", reason="considering changes")
    assert held["count"] == 2
    assert runs.read_pointer("r-other")["phase"] == "queued"
    for run_id in ("r-ready", "r-waiting"):
        record = runs.read_pointer(run_id)
        assert record["phase"] == "held"
        record["queue_hold"]["started_at"] = (
            datetime.now(UTC) - timedelta(hours=25)
        ).isoformat()
        runs._write_json(runs.pointer_path(run_id), record)

    released = queue_edit.release(plan="alpha")
    assert released["count"] == 2
    ready = runs.read_pointer("r-ready")
    waiting = runs.read_pointer("r-waiting")
    assert (ready["phase"], waiting["phase"]) == ("queued", "awaiting-upstream")
    assert ready["priority_override"] == 7
    assert waiting["waits"] == ["alpha#upstream"]
    assert (
        timedelta(minutes=59)
        < datetime.now(UTC) - datetime.fromisoformat(ready["queued_at"])
        < timedelta(minutes=61)
    )


def test_launched_run_is_refused_with_stop_remedy(home: Path) -> None:
    _request(home, "r-running", phase="running")
    result = CliRunner().invoke(cli.crew, ["queue", "hold", "--run", "r-running"])
    assert result.exit_code != 0
    assert "crew stop" in result.output


def test_session_selector_only_changes_its_waiting_requests(home: Path) -> None:
    _request(home, "r-owned")
    other = _request(home, "r-elsewhere")
    other["session"] = "someone-else"
    runs._write_json(runs.pointer_path("r-elsewhere"), other)
    result = CliRunner().invoke(cli.crew, ["queue", "hold", "--session", "owner"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["count"] == 1
    assert runs.read_pointer("r-owned")["phase"] == "held"
    assert runs.read_pointer("r-elsewhere")["phase"] == "queued"
    result = CliRunner().invoke(cli.crew, ["queue", "release", "--session", "owner"])
    assert result.exit_code == 0, result.output
    assert runs.read_pointer("r-owned")["phase"] == "queued"


def test_old_hold_appears_once_with_reason(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _request(home, "r-old")
    queue_edit.hold("r-old", reason="design review")
    record = runs.read_pointer("r-old")
    record["queue_hold"]["started_at"] = (
        datetime.now(UTC) - timedelta(hours=25)
    ).isoformat()
    runs._write_json(runs.pointer_path("r-old"), record)
    monkeypatch.setattr(
        obligations.flight, "resolve", lambda *a, **k: SimpleNamespace(config={})
    )
    monkeypatch.setattr(obligations, "_live_review_runs", lambda *a: set())
    monkeypatch.setattr(obligations, "_sub_floor_items", lambda *a, **k: [])
    monkeypatch.setattr(obligations, "_held_worktrees", lambda *a, **k: [])
    monkeypatch.setattr(
        obligations.runs, "drain", lambda *a, **k: {"unreconciled_runs": 0}
    )
    view = obligations.obligations("fixture", "owner")
    held = [item for item in view["obligations"] if item["kind"] == "run-held"]
    assert len(held) == 1
    assert held[0]["reason"] == "design review"


def test_held_request_is_classified_as_a_queue_pause(home: Path) -> None:
    _request(home, "r-paused")
    queue_edit.hold("r-paused", reason="review the brief")

    row = classify_pointer(runs.read_pointer("r-paused"))

    assert row["phase"] == "held"
    assert row["classification"] == "held"
    assert "review the brief" in row["detail"]
    assert row["next_action"] == "reckon crew queue release --run r-paused"
    assert "crew observe" not in row["next_action"]


@pytest.mark.parametrize("verb", ["hold", "release"])
def test_mcp_and_cli_return_the_same_document(home: Path, verb: str) -> None:
    _request(home, "r-cli")
    _request(home, "r-mcp")
    if verb == "release":
        queue_edit.hold("r-cli")
        queue_edit.hold("r-mcp")
    cli_result = CliRunner().invoke(cli.crew, ["queue", verb, "--run", "r-cli"])
    assert cli_result.exit_code == 0, cli_result.output
    tool = crew_action(f"queue-{verb}", run_id="r-mcp")
    cli_payload = json.loads(cli_result.output)
    assert [
        {**row, "run_id": "same"}
        for row in cli_payload["held" if verb == "hold" else "released"]
    ] == [
        {**row, "run_id": "same"}
        for row in tool["held" if verb == "hold" else "released"]
    ]
