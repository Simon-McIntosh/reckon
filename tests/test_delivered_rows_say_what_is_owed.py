"""A delivered run owes one promotion, while requested and landed runs do not."""

from __future__ import annotations

import importlib
import json
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

import tests.test_review_promotion_waits_for_its_record as review_fixtures
from reckon import cli, ledger
from reckon.crew import (
    obligation_snapshot,
    promotion_requests,
    recovery,
    recovery_watch,
    runs,
)
from reckon.crew import review as review_module
from reckon.crew.ticker import Ticker
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git
from tests.test_review_promotion_waits_for_its_record import (
    PLAN,
    PROJECT,
    _store_record,
    _write_exit_record,
    _write_pointer,
)
from tests.test_review_promotion_waits_for_its_record import (
    repository as review_repository,
)

repository = review_repository

obligations = importlib.import_module("reckon.crew.obligations")
SESSION = "delivered-session"


@pytest.fixture(autouse=True)
def project_identity(request, monkeypatch):
    """Independent mounted repositories have independent project identities."""
    import hashlib

    project = (
        "delivered-fixture-"
        + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]
    )
    monkeypatch.setitem(globals(), "PROJECT", project)
    monkeypatch.setattr(review_fixtures, "PROJECT", project)


def _review(run_id: str, base: str, head: str) -> None:
    record = review_module.parse_review(
        "\n".join(f"SCORE {name}: 20" for name in review_module.REVIEW_DIMENSIONS)
    )
    record.update(
        project=PROJECT,
        reviewed_run_id=run_id,
        review_run_id=f"review-of-{run_id}",
        reviewed_base_sha=base,
        reviewed_head_sha=head,
    )
    review_module.store_review(record)


def _seed(repository: Path, tmp_path: Path, run_id: str, *, role="implement") -> dict:
    base = git(repository, "rev-parse", "HEAD")
    head = base
    if role == "implement":
        (repository / "candidate.py").write_text("answer = 42\n")
        git(repository, "add", "candidate.py")
        git(repository, "commit", "-qm", "test: deliver a candidate")
        head = git(repository, "rev-parse", "HEAD")
    manifest = tmp_path / f"{run_id}.md"
    log = tmp_path / f"{run_id}.log"
    log.write_text("1 passed\nEXIT=0\n")
    arm = {
        "revision": head,
        "command": EXECUTABLE_GATE_COMMAND,
        "exit_status": 0,
        "log_path": str(log),
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n"
        f"changed_paths: {json.dumps(['candidate.py'] if role == 'implement' else [])}\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
        f"baseline_suite: {json.dumps({**arm, 'revision': base})}\n"
        f"after_suite: {json.dumps(arm)}\n"
        "checkpoint: Completed the delivered fixture report.\n"
        "landing: Delivered the candidate and its passing check.\n"
        "negative_control: none: fixture delivery\n"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": base,
        "role": role,
        "phase": "blocked",
        "launch": "in-harness",
        "process_alive": False,
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": PLAN,
            "section": "delivered",
            "role": role,
            "write_paths": ["candidate.py"] if role == "implement" else [],
            "time_budget": "20m",
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    _review(run_id, base, head)
    return pointer


def _review_run(repository: Path, tmp_path: Path, run_id="review-delivered") -> dict:
    _write_pointer(repository, tmp_path, run_id, "reviewed-target", pid=None)
    _store_record(run_id, "reviewed-target")
    _write_exit_record(run_id)
    pointer = runs.read_pointer(run_id)
    pointer.update(session=SESSION, phase="blocked")
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _request(pointer: dict, *, requested_at: datetime, phase: str) -> None:
    runs._write_json(
        promotion_requests._request_path(pointer["run_id"]),
        {
            "run_id": pointer["run_id"],
            "project": PROJECT,
            "session": pointer.get("session", SESSION),
            "phase": phase,
            "kind": "command",
            "requested_at": requested_at.isoformat(),
            "priority_override": None,
            "options": {},
        },
    )


def _snapshots(pointers: list[dict], now: datetime) -> dict:
    return {
        pointer["run_id"]: recovery_watch._watch_snapshot(
            pointer, moment=now.timestamp(), stall_seconds=3600, pointers=pointers
        )
        for pointer in pointers
    }


def test_delivered_counts_and_owed_rows_share_the_classified_population(
    repository, tmp_path, monkeypatch
):
    now = datetime.now(UTC)
    monkeypatch.setattr(obligations, "_utc_now", lambda: now)
    pointers = [_seed(repository, tmp_path, "delivered")]
    pointers += [
        _seed(repository, tmp_path, "requested", role="investigate"),
        _seed(repository, tmp_path, "waiting", role="investigate"),
        _seed(repository, tmp_path, "landed", role="investigate"),
    ]
    assert all(pointer["phase"] == "blocked" for pointer in pointers)
    assert all(
        recovery.classify_pointer(pointer)["classification"] == "promotable"
        for pointer in pointers
    ), "the control population must be deliverable before requests and landing"
    _request(
        pointers[1],
        requested_at=now - timedelta(minutes=2),
        phase=promotion_requests.REQUESTED,
    )
    _request(
        pointers[2],
        requested_at=now - timedelta(minutes=1),
        phase=promotion_requests.WAITING,
    )
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="landed",
            plan=PLAN,
            section="delivered",
            node="landed",
            gate="not-run",
            member_id="",
            session_id=SESSION,
            base_sha="",
            commits=[],
        ),
        root=repository,
    )
    snapshots = _snapshots(pointers, now)
    counts = recovery_watch._fleet_counts(snapshots, session=SESSION)
    print("classified census:", counts)
    assert counts["unpromoted"] == 1, counts
    assert counts["promotion_queued"] == 2, counts
    assert counts["oldest_promotion_position"] == 1, counts
    event = recovery_watch._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshots["delivered"],
        previous="working",
        current="complete",
        counts=recovery_watch._transition_counts(snapshots),
        spend_runs=pointers,
        rate_statuses={},
        pointers=pointers,
    )
    scoped = recovery_watch._scoped_watch_event(event, SESSION)
    pane = Ticker(color=False).render(scoped)
    print("pane:", pane)
    assert "1d" in pane and "2p" in pane and "@ 1" in pane, pane
    view = obligations.obligations(PROJECT, SESSION)
    assert [row["run_id"] for row in view["obligations"]] == ["delivered"], view
    assert view["remove_pointers"] == ["landed"]
    assert view["summary"]["delivered"] == 1
    assert view["summary"]["promotion_queued"] == 2
    assert view["summary"]["landed_pointers"] == 1
    state = obligation_snapshot.fleet_state(PROJECT, now=now)
    published = obligation_snapshot.payload_for(state, SESSION)
    assert published["summary"] == view["summary"]
    assert published["obligations"] == view["obligations"]
    assert published["remove_pointers"] == ["landed"]
    from reckon import mcp

    result = mcp._crew(PROJECT, view="obligations", session=SESSION)
    assert result["ok"], result
    for key in ("delivered", "promotion_queued", "landed_pointers"):
        assert result["summary"][key] == view["summary"][key]


@pytest.mark.parametrize("role", ["implement", "investigate", "review"])
def test_the_owed_command_promotes_the_run_verbatim(repository, tmp_path, role):
    pointer = (
        _review_run(repository, tmp_path)
        if role == "review"
        else _seed(repository, tmp_path, "delivered", role=role)
    )
    view = obligations.obligations(PROJECT, SESSION)
    [row] = [
        item for item in view["obligations"] if item["run_id"] == pointer["run_id"]
    ]
    command = row["next_command"]
    print("owed command:", command)
    assert "<" not in command
    assert row["next_action_form"] is not None
    argv = shlex.split(command)
    assert argv.pop(0) == "reckon"
    result = CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["pointer_removed"] is True, receipt
    assert Path(receipt["ledger_path"]).is_relative_to(repository), receipt[
        "ledger_path"
    ]
    print(
        "command receipt:",
        receipt["run_id"],
        receipt["pointer_removed"],
        receipt["record"]["gate"],
    )
    assert not runs.pointer_path(pointer["run_id"]).exists(), result.output
    assert any(
        row["run_id"] == pointer["run_id"]
        for row in ledger.runs(PROJECT, root=repository)
    )


@pytest.mark.parametrize(
    "phase",
    [
        promotion_requests.REQUESTED,
        promotion_requests.RUNNING,
        promotion_requests.WAITING,
    ],
)
def test_a_cached_read_reclassifies_an_unmarked_promotion_request(
    repository, tmp_path, phase
):
    pointer = _seed(repository, tmp_path, "delivered", role="investigate")
    now = datetime.now(UTC)
    cache = {}
    before = recovery_watch._watch_snapshot(
        pointer, moment=now.timestamp(), stall_seconds=3600, cache=cache
    )
    assert before["delivery"]["status"] == "delivered"
    _request(pointer, requested_at=now, phase=phase)
    after = recovery_watch._watch_snapshot(
        pointer, moment=now.timestamp(), stall_seconds=3600, cache=cache
    )
    assert after["delivery"]["status"] == "promotion-queued"
    assert runs.read_pointer(pointer["run_id"])["phase"] == "blocked"
    assert obligations.obligations(PROJECT, SESSION)["obligations"] == []
    _request(pointer, requested_at=now, phase=promotion_requests.REFUSED)
    refused = recovery_watch._watch_snapshot(
        pointer, moment=now.timestamp(), stall_seconds=3600, cache=cache
    )
    assert refused["delivery"]["status"] == "delivered"


def test_the_oldest_position_comes_from_the_global_promotion_order(
    repository, tmp_path
):
    now = datetime.now(UTC)
    pointer = _seed(repository, tmp_path, "delivered", role="investigate")
    _request(pointer, requested_at=now, phase=promotion_requests.REQUESTED)
    _request(
        {**pointer, "run_id": "foreign-request", "session": "another-session"},
        requested_at=now - timedelta(minutes=2),
        phase=promotion_requests.REQUESTED,
    )
    view = obligations.obligations(PROJECT, SESSION)
    assert view["summary"]["delivered"] == 0
    assert view["summary"]["promotion_queued"] == 1
    assert view["summary"]["oldest_promotion_position"] == 2


def test_a_stored_promotion_label_is_not_a_request(repository, tmp_path):
    pointer = _seed(repository, tmp_path, "delivered", role="investigate")
    pointer["phase"] = promotion_requests.REQUESTED
    pointer["promotion_status"] = promotion_requests.WAITING
    runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
    row = recovery.classify_pointer(pointer)
    assert row["classification"] == "promotable"
    assert row["delivery"]["status"] == "delivered"
    view = obligations.obligations(PROJECT, SESSION)
    assert view["summary"]["delivered"] == 1
    assert view["summary"]["promotion_queued"] == 0


def test_a_session_pane_carries_the_oldest_request_across_projects(monkeypatch):
    newer = {
        "unpromoted": 1,
        "promotion_queued": 1,
        "oldest_promotion_requested_at": "2026-11-01T12:01:00Z",
        "oldest_promotion_position": 1,
    }
    older = {
        "unpromoted": 2,
        "promotion_queued": 2,
        "oldest_promotion_requested_at": "2026-11-01T12:00:00Z",
        "oldest_promotion_position": 5,
    }
    monkeypatch.setattr(
        recovery_watch, "_session_counts_in_other_projects", lambda *_: older
    )
    event = recovery_watch._scoped_watch_event(
        {"project": "current", "session_counts": {SESSION: newer}}, SESSION
    )
    assert event["unpromoted"] == 3
    assert event["promotion_queued"] == 3
    assert event["oldest_promotion_position"] == 5


def test_an_owed_command_never_invents_a_waiver_for_uncommitted_work(
    repository, tmp_path
):
    pointer = _seed(repository, tmp_path, "delivered", role="investigate")
    (repository / "uncommitted.py").write_text("answer = 42\n")
    manifest = Path(pointer["manifest_path"])
    manifest.write_text(
        manifest.read_text().replace(f"commits: [{pointer['base_sha']}]", "commits: []")
    )
    from reckon.crew.recovery_classification import _promotion_command

    command = _promotion_command(pointer["run_id"], pointer, [])
    result = CliRunner().invoke(cli.main, shlex.split(command)[1:])
    print("uncommitted-work receipt:", result.output)
    assert result.exit_code != 0, result.output
    assert "uncommitted.py" in result.output
    assert "--no-commit" not in command
    assert runs.pointer_path(pointer["run_id"]).exists()
    assert not ledger.runs(PROJECT, root=repository)


def test_the_owed_action_form_promotes_on_the_tool_surface(repository, tmp_path):
    import ast

    from reckon import mcp

    pointer = _seed(repository, tmp_path, "mcp-delivered", role="investigate")
    [row] = obligations.obligations(PROJECT, SESSION)["obligations"]
    form = row["next_action_form"]
    parsed = ast.parse(form, mode="eval").body
    arguments = {
        keyword.arg: ast.literal_eval(keyword.value) for keyword in parsed.keywords
    }
    result = mcp._crew(PROJECT, **arguments)
    assert result["ok"], result
    assert not runs.pointer_path(pointer["run_id"]).exists()
