"""Promotion belongs to a durable fleet service, and callers only request it."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main
from reckon.crew import (
    obligation_snapshot,
    obligations,
    promotion,
    queue_landing,
    resumption,
    runs,
)
from reckon.crew import promotion_requests as requests
from reckon.crew import recovery_review_acceptance as acceptance
from tests.helpers.git import git

PROJECT = "promotion-fixture"
SESSION = "promotion-session"
DELAY = 30.0


@pytest.fixture()
def repository(tmp_path: Path, isolated_reckon_home: Path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "marker.txt").write_text("delivered\n")
    git(root, "add", "marker.txt")
    git(
        root,
        "commit",
        "-q",
        "-m",
        "feat: seed delivery",
        "-m",
        "Provide a committed delivery for promotion requests.",
    )
    sha = git(root, "rev-parse", "HEAD")

    def plant(run_id: str, *, role: str = "implement") -> dict:
        manifest = runs.run_dir(run_id) / "manifest.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(f"node: delivery\nstatus: complete\ncommits: [{sha}]\n")
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": sha,
            "phase": "complete",
            "role": role,
            "process_alive": False,
            "manifest_path": str(manifest),
            "node": {"id": "delivery", "role": role, "write_paths": []},
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        return pointer

    return SimpleNamespace(root=root, sha=sha, plant=plant)


@pytest.fixture()
def slow_promotion(repository, monkeypatch):
    calls = []
    finished = []
    lock = threading.Lock()

    def perform(run_id, **kwargs):
        with lock:
            calls.append(run_id)
        time.sleep(DELAY)
        with lock:
            finished.append(run_id)
        return {"ok": True, "run_id": run_id, "record": {"commits": [repository.sha]}}

    monkeypatch.setattr(crew, "complete", perform)
    monkeypatch.setattr(promotion, "complete", perform)
    return SimpleNamespace(calls=calls, finished=finished, perform=perform)


def _complete(run_id: str):
    return CliRunner().invoke(
        main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "delivery",
        ],
    )


def test_complete_requests_and_returns_before_the_promotion_finishes(
    repository, slow_promotion
):
    run_id = "r-coordinator-request"
    repository.plant(run_id)
    started = time.monotonic()
    response = _complete(run_id)
    elapsed = time.monotonic() - started
    assert response.exit_code == 0, response.output
    assert elapsed < 5, (
        f"crew complete waited {elapsed:.3f} s on a {DELAY:g} s promotion"
    )
    assert json.loads(response.output)["phase"] == requests.REQUESTED
    assert runs.read_pointer(run_id)["phase"] == requests.REQUESTED
    assert not slow_promotion.calls
    service = requests.PromotionService(width=2)
    assert service.tick() == [run_id]
    service.settle(timeout=45)
    receipt = requests.read_promotion(run_id)
    assert receipt["phase"] == requests.PROMOTED, receipt
    assert receipt["commits"] == [repository.sha]
    assert slow_promotion.finished == [run_id]
    print(
        f"complete returned in {elapsed:.3f} s; service completed the {DELAY:g} s promotion with {repository.sha}"
    )


def test_the_sweep_requests_without_waiting(repository, slow_promotion, monkeypatch):
    run_id = "r-sweep-request"
    pointer = repository.plant(run_id, role="review")
    monkeypatch.setattr(resumption, "external_wait", lambda *a, **k: None)
    monkeypatch.setattr(resumption, "_stream_refusal_block", lambda *a, **k: None)
    monkeypatch.setattr(
        resumption.plan_review, "compose_owed_plan_reviews", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        acceptance,
        "promote_review_run_itself",
        lambda p, **k: {
            **slow_promotion.perform(p["run_id"]),
            "promoted": True,
        },
    )
    started = time.monotonic()
    report = resumption.sweep(
        PROJECT, session=SESSION, pointers=[pointer], launcher=lambda *a, **k: 0
    )
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"sweep waited {elapsed:.3f} s on a promotion"
    assert report["reviews"]["promotion_requested"] == [run_id], report
    assert requests.read_promotion(run_id)["phase"] == requests.REQUESTED
    assert not slow_promotion.calls
    service = requests.PromotionService()
    service.tick()
    service.settle(timeout=45)
    assert requests.read_promotion(run_id)["phase"] == requests.PROMOTED
    assert slow_promotion.finished == [run_id]
    print(f"sweep returned in {elapsed:.3f} s before the {DELAY:g} s promotion")


def test_the_landing_pass_requests_without_waiting(
    repository, slow_promotion, monkeypatch
):
    run_id = "r-landing-request"
    repository.plant(run_id)
    monkeypatch.setattr(
        queue_landing.recovery,
        "classify_pointer",
        lambda *a, **k: {
            "manifest_status": "complete",
            "manifest_commits": [repository.sha],
        },
    )
    monkeypatch.setattr(
        queue_landing.recovery,
        "_review_head_and_tree",
        lambda *a, **k: (repository.sha, repository.sha),
    )
    monkeypatch.setattr(queue_landing, "_owed_review_tiers", lambda *a: ())
    monkeypatch.setattr(queue_landing, "_suite_arms", lambda *a: {})
    monkeypatch.setattr(queue_landing, "_red_suite_guard", lambda *a: "")
    monkeypatch.setattr(queue_landing, "_primary_revision", lambda *a: "")
    monkeypatch.setattr(queue_landing, "_merge_conflicts", lambda *a: ())
    monkeypatch.setattr(queue_landing, "_landed_elsewhere", lambda *a: False)
    monkeypatch.setattr(queue_landing, "_measure_config", lambda *a: {})
    monkeypatch.setattr(queue_landing, "_pass_history", lambda *a: [])
    monkeypatch.setattr(queue_landing, "_measure_delivery", lambda *a: None)
    monkeypatch.setattr(queue_landing, "_measure_guard", lambda *a: "")
    monkeypatch.setattr(queue_landing, "_measurement_record", lambda *a: {})
    from reckon import crew_run_commands

    monkeypatch.setattr(
        crew_run_commands, "_land_a_promoted_run", lambda result, **k: (result, 0)
    )
    started = time.monotonic()
    report = queue_landing.land_once()
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"landing waited {elapsed:.3f} s on a promotion"
    assert report["landed"] == [], report
    assert report["promotion_requested"] == [run_id], report
    assert runs.read_pointer(run_id)["phase"] == requests.REQUESTED
    assert not slow_promotion.calls
    service = requests.PromotionService()
    service.tick()
    service.settle(timeout=45)
    receipt = requests.read_promotion(run_id)
    assert receipt["phase"] == requests.PROMOTED, receipt
    assert receipt["commits"] == [repository.sha]
    assert slow_promotion.finished == [run_id]
    print(f"landing returned in {elapsed:.3f} s before the {DELAY:g} s promotion")


def test_two_runs_at_width_two_finish_in_one_delay(repository, slow_promotion):
    names = ["r-concurrent-left", "r-concurrent-right"]
    for run_id in names:
        repository.plant(run_id)
        requests.request_promotion(run_id, options={"gate": "not-run"})
    service = requests.PromotionService(width=2)
    started = time.monotonic()
    assert sorted(service.tick()) == names
    service.settle(timeout=45)
    elapsed = time.monotonic() - started
    assert sorted(slow_promotion.finished) == names
    assert DELAY <= elapsed < 50, (
        f"two {DELAY:g} s promotions at width two took {elapsed:.3f} s"
    )
    assert all(
        requests.read_promotion(name)["commits"] == [repository.sha] for name in names
    )
    print(f"width two: two {DELAY:g} s promotions finished in {elapsed:.3f} s")


def test_a_failure_is_durable_and_is_a_session_obligation(repository, monkeypatch):
    run_id = "r-failed-promotion"
    repository.plant(run_id)

    def fail(*args, **kwargs):
        raise RuntimeError("injected promotion failure")

    monkeypatch.setattr(promotion, "complete", fail)
    requests.request_promotion(run_id, options={"gate": "not-run"})
    service = requests.PromotionService()
    service.tick()
    service.settle(timeout=10)
    receipt = requests.read_promotion(run_id)
    assert receipt["phase"] == requests.FAILED
    assert "RuntimeError: injected promotion failure" in receipt["error"]
    monkeypatch.setattr(obligations, "_classified_rows", lambda *a, **k: [])
    view = obligations.obligations(PROJECT, SESSION)
    failures = [
        item for item in view["obligations"] if item["kind"] == "promotion-failed"
    ]
    assert len(failures) == 1, view
    assert failures[0]["run_id"] == run_id
    assert "injected promotion failure" in failures[0]["reason"]
    assert requests.failure_obligations(PROJECT, "another-session") == []
    state = SimpleNamespace(
        project=PROJECT,
        now=datetime.now(UTC),
        reviews_in_flight={},
        pointers={},
        rows=[],
        sub_floor={},
        held_worktrees={},
        findings=[],
        acknowledged={},
        unreconciled={},
    )
    published = obligation_snapshot.payload_for(state, SESSION)
    failures = [
        item for item in published["obligations"] if item["kind"] == "promotion-failed"
    ]
    assert len(failures) == 1, published
    assert "injected promotion failure" in failures[0]["reason"]


def test_a_fresh_service_adopts_an_unfinished_request_once(repository):
    run_id = "r-restarted-promotion"
    repository.plant(run_id)
    request = requests.request_promotion(run_id, options={"gate": "not-run"})
    runs._write_json(
        requests._request_path(run_id), {**request, "phase": requests.RUNNING}
    )
    calls = []

    def perform(request):
        calls.append(request["run_id"])
        return {"ok": True, "record": {"commits": [repository.sha]}}

    service = requests.PromotionService(execute=perform)
    assert service.tick() == [run_id]
    service.settle(timeout=10)
    assert requests.read_promotion(run_id)["phase"] == requests.PROMOTED
    assert requests.PromotionService(execute=perform).tick() == []
    duplicate = requests.request_promotion(run_id)
    assert duplicate["duplicate"]
    assert calls == [run_id]


def test_a_second_pending_request_adds_no_second_promotion(repository):
    run_id = "r-duplicate-request"
    repository.plant(run_id)
    first = requests.request_promotion(run_id, options={"gate": "not-run"})
    second = requests.request_promotion(run_id, options={"gate": "passed"})
    assert second["duplicate"]
    assert second["request_id"] == first["request_id"]
    assert second["options"] == first["options"]
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def perform(request):
        calls.append(run_id)
        entered.set()
        assert release.wait(10)
        return {"record": {"commits": [repository.sha]}}

    service = requests.PromotionService(execute=perform)
    try:
        service.tick()
        assert entered.wait(5)
        assert requests.request_promotion(run_id)["duplicate"]
        assert requests.PromotionService(execute=perform).tick() == []
    finally:
        release.set()
        service.settle(timeout=10)
    assert calls == [run_id]


def test_a_terminal_receipt_prevents_replay_of_its_unretired_request(repository):
    run_id = "r-completed-before-restart"
    repository.plant(run_id)
    request = requests.request_promotion(run_id)
    runs._write_json(
        requests._receipt_path(run_id),
        {
            **request,
            "phase": requests.PROMOTED,
            "commits": [repository.sha],
        },
    )
    calls = []
    service = requests.PromotionService(execute=calls.append)
    service.tick()
    service.settle(timeout=10)
    assert not calls
    assert not requests._request_path(run_id).exists()
    assert requests.read_promotion(run_id)["commits"] == [repository.sha]


def test_the_admission_service_drains_a_request(repository, monkeypatch):
    from reckon.crew.admission import service as admission

    run_id = "r-admission-request"
    repository.plant(run_id)
    requests.request_promotion(run_id)
    calls = []

    def perform(request):
        calls.append(request["run_id"])
        return {"record": {"commits": [repository.sha]}}

    service = requests.PromotionService(execute=perform)
    monkeypatch.setattr(requests, "PromotionService", lambda **kwargs: service)
    monkeypatch.setattr(admission._selection, "admit_once", lambda **kwargs: {})
    monkeypatch.setattr(admission._release, "_release_tick", dict)
    admission.admit_until_stopped(once=True, lander=dict)
    service.settle(timeout=10)
    assert calls == [run_id]
    assert requests.read_promotion(run_id)["phase"] == requests.PROMOTED


def test_a_refused_promotion_is_recorded_and_not_silently_retried(repository):
    run_id = "r-refused-request"
    repository.plant(run_id)
    requests.request_promotion(run_id)
    service = requests.PromotionService(
        execute=lambda request: {
            "promoted": False,
            "reason": "the stored review refuses this head",
        }
    )
    service.tick()
    service.settle(timeout=10)
    receipt = requests.read_promotion(run_id)
    assert receipt["phase"] == requests.REFUSED
    assert "stored review refuses" in receipt["error"]
    assert service.tick() == []
    assert requests.request_promotion(run_id)["duplicate"]
    assert requests.request_promotion(run_id, retry=True)["phase"] == requests.REQUESTED


def test_the_service_records_commits_from_the_landing_and_acceptance_receipts(
    repository,
):
    for run_id, kind in [
        ("r-landing-receipt", "landing"),
        ("r-acceptance-receipt", "acceptance"),
    ]:
        repository.plant(run_id)
        requests.request_promotion(run_id)
        if kind == "landing":
            result = {
                "ok": True,
                "promotion": {"record": {"commits": [repository.sha]}},
            }
        else:
            ledger = repository.root / "accepted-run.json"
            ledger.write_text(json.dumps({"commits": [repository.sha]}))
            result = {
                "accepted": True,
                "promoted": True,
                "ledger": {"path": str(ledger)},
            }
        service = requests.PromotionService(
            execute=lambda request, result=result: result
        )
        service.tick()
        service.settle(timeout=10)
        assert requests.read_promotion(run_id)["commits"] == [repository.sha]


def test_a_pending_landing_is_not_finished_beside_its_requested_promotion(
    repository, monkeypatch
):
    from reckon.crew import pending_landing

    run_id = "r-pending-landing"
    pointer = repository.plant(run_id)
    pending_landing.write_pending(run_id, PROJECT, pointer=pointer)
    requests.request_promotion(run_id)

    def unexpected(*args):
        raise AssertionError(
            "the pending landing was inspected while its service owned it"
        )

    monkeypatch.setattr(pending_landing, "_promoted_row", unexpected)
    assert pending_landing.finish_pending_landings() == [
        {
            "run_id": run_id,
            "status": "promotion-requested",
        }
    ]


def test_wait_returns_the_synchronous_outcome(repository, monkeypatch):
    run_id = "r-explicit-wait"
    repository.plant(run_id)
    called = []

    def perform(run_id, **kwargs):
        called.append(run_id)
        return {"ok": True, "run_id": run_id, "record": {"commits": [repository.sha]}}

    monkeypatch.setattr(crew, "complete", perform)
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "complete",
            "--wait",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "delivery",
        ],
    )
    assert result.exit_code == 0, result.output
    assert called == [run_id]
    assert json.loads(result.output)["record"]["commits"] == [repository.sha]
    assert not requests._request_path(run_id).exists()


@pytest.mark.parametrize(
    ("kind", "operation_name", "promoted_by"),
    [
        ("acceptance", "accept_clean_review", "review-acceptance"),
        ("review-self", "promote_review_run_itself", "review-self"),
    ],
)
def test_a_review_promotion_survives_pointer_removal_before_its_receipt(
    repository, isolated_reckon_home, monkeypatch, kind, operation_name, promoted_by
):
    from reckon import ledger

    run_id = f"r-interrupted-{kind}"
    repository.plant(run_id)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository.root / "docs")})
    )
    request = requests.request_promotion(run_id, kind=kind)
    calls = []
    row = {
        "run_id": run_id,
        "commits": [repository.sha],
        "promoted_by": promoted_by,
    }

    def promote(pointer, **kwargs):
        calls.append(pointer["run_id"])
        ledger.run_path(PROJECT, run_id, repository.root).parent.mkdir(
            parents=True, exist_ok=True
        )
        path = ledger.write_run_file(PROJECT, row, root=repository.root)
        runs.pointer_path(run_id).unlink()
        return {"promoted": True, "ledger": {"path": str(path)}}

    class ServiceInterrupted(BaseException):
        pass

    def interrupt_receipt(*args, **kwargs):
        assert not kwargs.get("error"), kwargs
        assert args[1] == requests.PROMOTED
        raise ServiceInterrupted

    monkeypatch.setattr(acceptance, operation_name, promote)
    with monkeypatch.context() as crash:
        crash.setattr(requests, "_finish", interrupt_receipt)
        with pytest.raises(ServiceInterrupted):
            requests.PromotionService()._run(request)

    assert not runs.pointer_path(run_id).exists()
    assert not requests._receipt_path(run_id).exists()
    assert requests.read_promotion(run_id)["phase"] == requests.RUNNING
    row_path = ledger.run_path(PROJECT, run_id, repository.root)
    assert json.loads(row_path.read_text()) == row
    row_bytes = row_path.read_bytes()

    service = requests.PromotionService()
    assert service.tick() == [run_id]
    service.settle(timeout=10)
    receipt = requests.read_promotion(run_id)
    assert receipt["phase"] == requests.PROMOTED, receipt
    assert receipt["commits"] == [repository.sha]
    assert receipt["result"]["already_promoted"] is True
    assert calls == [run_id]
    assert row_path.read_bytes() == row_bytes
    assert not requests._request_path(run_id).exists()
    assert not requests.failure_obligations(PROJECT, SESSION)
    assert requests.PromotionService().tick() == []
