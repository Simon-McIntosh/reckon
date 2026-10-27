"""A request's broken wait leaves unrelated dispatches free to proceed."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import queue_admission, runs
from reckon.crew.node import CrewError, ScopeConflict, TaskNode
from tests import test_overlapping_queued_requests_launch_in_turn as scope_cases
from tests import test_queue_dag_waits as wait_cases

dispatch_module = importlib.import_module("reckon.crew.dispatch")


@pytest.fixture()
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    return wait_cases.projects.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scope_cases.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _broken_request(first: Path) -> None:
    wait_cases._queue(
        first,
        wait_cases._node("broken", "s1"),
        run_ids={"broken": "r-broken"},
    )
    pointer = runs.read_pointer("r-broken")
    assert pointer is not None
    pointer["waits"] = ["run:r-vanished"]
    runs._write_json(runs.pointer_path("r-broken"), pointer)
    assert not runs.pointer_path("r-vanished").exists()
    assert not ledger.run_path(wait_cases.fixtures.PROJECT, "r-vanished").exists()
    assert not ledger.run_path("other", "r-vanished").exists()


def test_foreign_broken_wait_does_not_refuse_own_ready_section(
    projects: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = projects
    _broken_request(first)
    plan = second / "docs" / "plans" / "plan-b.html"
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "</body>",
            '<section data-reckon="decisions"><div class="r-dec" '
            'data-key="route" data-choice="ready"><p class="r-dec-q">'
            "Which route?</p></div></section></body>",
        ),
        encoding="utf-8",
    )
    wait_cases._git(second, "add", "--", "docs/plans/plan-b.html")
    wait_cases._git(second, "commit", "-q", "-m", "chore: record section choice")
    validated: list[str] = []
    verdicts: list[str] = []
    original_validate = queue_admission._validated_batch_waits
    original_rows = queue_admission._readiness_rows
    from reckon.crew import plan_review

    original_readiness = plan_review.project_section_readiness

    def readiness(project, docs):
        answer = original_readiness(project, docs)
        if project == "other":
            answer["plan-b"]["sections"]["s1"] = {
                "section": "s1",
                "ready": False,
                "blockers": [{"kind": "decision", "key": "approval"}],
            }
        return answer

    def validate(*args, **kwargs):
        validated.append(str(kwargs["project"]))
        assert kwargs.get("read_own_verdict", True) is True
        return original_validate(*args, **kwargs)

    def rows(project, *args):
        verdicts.append(project)
        return original_rows(project, *args)

    monkeypatch.setattr(queue_admission, "_validated_batch_waits", validate)
    monkeypatch.setattr(queue_admission, "_readiness_rows", rows)
    monkeypatch.setattr(plan_review, "project_section_readiness", readiness)
    node = TaskNode(
        id="independent",
        goal="run independent section",
        plan="plan-b",
        section="s1",
        done_when="pytest reports passed",
        write_paths=["independent.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    try:
        result = crew.dispatch(
            node=node,
            project="other",
            repo=second,
            config=wait_cases.fixtures.CONFIG,
            session=wait_cases.fixtures.SESSION,
            run_id="r-independent",
            check_budget=False,
            watch_override=True,
            unreviewed_plan_override=True,
            local=True,
            launcher=scope_cases._launcher([]),
        )
    except wait_cases.LaneHeld as held:
        result = dict(held.queued or {})
    assert result["run_id"] == "r-independent"
    assert validated == ["other"]
    assert "other" in verdicts


def test_own_missing_wait_still_refuses(projects: tuple[Path, Path]) -> None:
    first, _ = projects
    _broken_request(first)
    with pytest.raises(CrewError, match="wait names missing request r-vanished"):
        wait_cases._dispatch(
            first,
            wait_cases._node("bad", "s1", after=["r-vanished"]),
            run_id="r-bad",
        )


def test_cycle_through_existing_requests_still_refuses(
    projects: tuple[Path, Path],
) -> None:
    first, _ = projects
    wait_cases._queue(
        first,
        wait_cases._node("prior", "s1"),
        run_ids={"prior": "r-prior"},
    )
    pointer = runs.read_pointer("r-prior")
    assert pointer is not None
    pointer["waits"] = ["run:r-cycle"]
    runs._write_json(runs.pointer_path("r-prior"), pointer)
    with pytest.raises(CrewError, match=r"wait cycle.*r-cycle.*r-prior"):
        wait_cases._dispatch(
            first,
            wait_cases._node("cycle", "s2", after=["r-prior"]),
            run_id="r-cycle",
        )


def test_blocked_request_does_not_bind_but_launched_run_does(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope_cases._configure(dispatch_repo, monkeypatch, session_slots=4)
    scope_cases._live(
        dispatch_repo,
        "r-blocked",
        phase="blocked-upstream",
        write_paths=[scope_cases.SHARED],
    )
    allowed = scope_cases._dispatch(dispatch_repo, "ready", paths=(scope_cases.SHARED,))
    assert allowed.exit_code == 0, allowed.output
    assert scope_cases._payload(allowed)["phase"] == "starting"
    runs.pointer_path(scope_cases._payload(allowed)["run_id"]).unlink()
    scope_cases._live(
        dispatch_repo,
        "r-launched",
        phase="starting",
        write_paths=[scope_cases.SHARED],
    )
    refused = scope_cases._dispatch(
        dispatch_repo, "another", paths=(scope_cases.SHARED,)
    )
    assert refused.exit_code == 7, refused.output
    assert scope_cases._payload(refused)["error"] == "scope-conflict"


def test_queued_pointer_survives_refused_launch_and_own_launch_proceeds(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = scope_cases._configure(dispatch_repo, monkeypatch, session_slots=4)
    dependent_config = json.loads(json.dumps(config))
    dependent_lane = dispatch_repo.parent / "dependent-lane.json"
    dependent_lane.write_text(
        json.dumps({"headroom": 0, "admission": {"worker_slots": 0}}),
        encoding="utf-8",
    )
    dependent_config["backends"][scope_cases.LANE]["lane_document"] = str(
        dependent_lane
    )
    pointer = scope_cases._queue(
        dispatch_repo,
        node_id="queued",
        run_id="r-queued",
        session=scope_cases.SESSION_A,
        write_paths=[scope_cases.SHARED],
        queued_at=scope_cases.START.isoformat(),
    )
    runs.queue_dispatch(pointer)
    scope_cases._live(
        dispatch_repo,
        "r-peer",
        phase="starting",
        write_paths=[scope_cases.SHARED],
    )
    real_dispatch = dispatch_module.dispatch
    observed: list[bool] = []

    def observed_dispatch(**kwargs):
        observed.append(runs.read_pointer("r-queued") is not None)
        dependent = TaskNode(
            id="dependent",
            goal="follow queued request",
            plan="plan-a",
            section="dispatch",
            after=["r-queued"],
            done_when="pytest reports passed",
            write_paths=["dependent.txt"],
            time_budget="20m",
            spec_level="exact",
        )
        try:
            answer = real_dispatch(
                node=dependent,
                project=scope_cases.PROJECT,
                repo=dispatch_repo,
                config=dependent_config,
                session=scope_cases.SESSION_B,
                run_id="r-dependent",
                check_budget=False,
                watch_override=True,
                unreviewed_plan_override=True,
                local=True,
                backend_override=scope_cases.LANE,
                launcher=scope_cases._launcher([]),
            )
        except wait_cases.LaneHeld as held:
            answer = dict(held.queued or {})
        assert answer["run_id"] == "r-dependent"
        assert "run:r-queued" in answer["waits"]
        assert runs.pointer_path("r-queued").exists()
        return real_dispatch(**kwargs)

    monkeypatch.setattr(dispatch_module, "dispatch", observed_dispatch)
    with pytest.raises(ScopeConflict):
        queue_admission._launch_queued(
            pointer, launcher=scope_cases._launcher([]), config_for=lambda _: config
        )
    assert observed == [True]
    assert runs.pointer_path("r-queued").exists()
    runs.pointer_path("r-peer").unlink()
    monkeypatch.setattr(dispatch_module, "dispatch", real_dispatch)
    launched = queue_admission._launch_queued(
        pointer, launcher=scope_cases._launcher([]), config_for=lambda _: config
    )
    assert launched["run_id"] == "r-queued"
    assert launched["phase"] != "queued"
