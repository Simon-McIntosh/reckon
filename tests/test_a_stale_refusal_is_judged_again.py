"""A refusal is terminal only for the promotion code that judged it."""

from __future__ import annotations

import json

import pytest

from reckon.crew import promotion_requests as requests
from reckon.crew import recovery_review_acceptance as acceptance
from reckon.crew.node import CrewError

EARLIER_CODE = "a" * 40
CURRENT_CODE = "b" * 40
NEXT_CODE = "c" * 40


def _running(monkeypatch, loaded=CURRENT_CODE, current=CURRENT_CODE):
    # Replacing the loaded identity models a service process replacement.
    monkeypatch.setattr(requests, "_JUDGING_CODE_REVISION", loaded, raising=False)
    monkeypatch.setattr(
        requests, "_current_code_revision", lambda: current, raising=False
    )


def _request(run_id):
    requests.runs._write_json(
        requests.runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "phase": "complete",
            "project": "revision-fixture",
            "session": "revision-session",
        },
    )
    return requests.request_promotion(run_id, options={"verdict": "passed"})


def _refused(run_id, code_revision, *, phase=requests.REFUSED):
    request = _request(run_id)
    receipt = {
        **request,
        "phase": phase,
        "ok": False,
        "error": "the gate did not pass",
        "finished_at": "2026-11-17T16:24:00+00:00",
    }
    if code_revision is not None:
        receipt["code_revision"] = code_revision
    requests.runs._write_json(requests._request_path(run_id), receipt)
    requests.runs._write_json(requests._receipt_path(run_id), receipt)
    return receipt


def test_a_reloaded_service_rejudges_only_the_superseded_refusal(monkeypatch):
    _running(monkeypatch)
    stale = _refused("r-superseded", EARLIER_CODE)
    current = _refused("r-current", CURRENT_CODE)
    seen = []

    def execute(request):
        seen.append(request)
        return {"ok": True}

    service = requests.PromotionService(width=1, execute=execute)
    launched = service.tick()
    service.settle(timeout=10)
    after = requests.read_promotion(stale["run_id"])
    print(f"after reload: launched={launched} stale={after['phase']}")
    assert launched == [stale["run_id"]], after
    assert after["phase"] == requests.PROMOTED, after
    assert after["code_revision"] == CURRENT_CODE
    assert seen[0]["options"] == stale["options"]
    assert after["previous_verdict"]["code_revision"] == EARLIER_CODE
    assert requests.read_promotion(current["run_id"]) == current
    assert service.tick() == []
    assert len(seen) == 1


def test_the_service_waits_until_it_has_loaded_the_current_code(monkeypatch):
    _running(monkeypatch, loaded=EARLIER_CODE)
    stale = _refused("r-awaiting-reload", EARLIER_CODE)
    service = requests.PromotionService(execute=lambda request: {"ok": True})
    assert service.tick() == []
    assert requests.read_promotion(stale["run_id"]) == stale
    _running(monkeypatch)
    assert service.tick() == [stale["run_id"]]
    service.settle(timeout=10)
    assert requests.read_promotion(stale["run_id"])["code_revision"] == CURRENT_CODE


def test_a_second_refusal_under_current_code_stays_terminal(monkeypatch):
    _running(monkeypatch)
    stale = _refused("r-refused-again", EARLIER_CODE)
    seen = []

    def refuse(request):
        seen.append(request)
        raise CrewError("the gate still does not pass")

    service = requests.PromotionService(execute=refuse)
    assert service.tick() == [stale["run_id"]]
    service.settle(timeout=10)
    after = requests.read_promotion(stale["run_id"])
    assert after["phase"] == requests.REFUSED
    assert after["code_revision"] == CURRENT_CODE
    assert "the gate still does not pass" in after["error"]
    assert service.tick() == []
    assert len(seen) == 1


@pytest.mark.parametrize(
    "code_revision,phase",
    [
        (None, requests.REFUSED),
        ("unknown", requests.REFUSED),
        (EARLIER_CODE, requests.FAILED),
    ],
)
def test_an_unproven_code_change_does_not_retry(monkeypatch, code_revision, phase):
    _running(monkeypatch)
    original = _refused("r-no-retry", code_revision, phase=phase)
    service = requests.PromotionService(execute=lambda request: pytest.fail("retried"))
    assert service.tick() == []
    assert requests.read_promotion(original["run_id"]) == original


def test_a_still_stale_service_cannot_retry_another_stale_refusal(monkeypatch):
    _running(monkeypatch, loaded=CURRENT_CODE, current=NEXT_CODE)
    original = _refused("r-still-stale", EARLIER_CODE)
    service = requests.PromotionService(execute=lambda request: pytest.fail("retried"))
    assert service.tick() == []
    assert requests.read_promotion(original["run_id"]) == original


def test_a_gone_pointer_keeps_its_refusal(monkeypatch):
    _running(monkeypatch)
    original = _refused("r-no-pointer", EARLIER_CODE)
    requests.runs.pointer_path(original["run_id"]).unlink()
    service = requests.PromotionService(execute=lambda request: pytest.fail("retried"))
    assert service.tick() == []
    assert requests.read_promotion(original["run_id"]) == original


def test_a_repeated_request_uses_the_same_revision_rule(monkeypatch):
    _running(monkeypatch)
    stale = _refused("r-repeated-stale", EARLIER_CODE)
    current = _refused("r-repeated-current", CURRENT_CODE)
    retried = requests.request_promotion(stale["run_id"])
    assert retried["phase"] == requests.REQUESTED
    assert retried["options"] == stale["options"]
    assert requests.request_promotion(current["run_id"])["phase"] == requests.REFUSED


def test_an_explicit_retry_keeps_its_callers_options(monkeypatch):
    _running(monkeypatch)
    stale = _refused("r-explicit-retry", EARLIER_CODE)
    retried = requests.request_promotion(
        stale["run_id"], kind="command", options={"verdict": "not-run"}, retry=True
    )
    assert retried["phase"] == requests.REQUESTED
    assert retried["kind"] == "command"
    assert retried["options"] == {"verdict": "not-run"}


def test_a_code_reload_invalidates_the_acceptance_refusal_memo(monkeypatch, tmp_path):
    _running(monkeypatch, loaded=EARLIER_CODE, current=EARLIER_CODE)
    request = _request("r-acceptance-memo")
    pointer = requests.runs.read_pointer(request["run_id"])
    pointer["role"] = "review"
    monkeypatch.setattr(
        acceptance, "_review_head_and_tree", lambda _: ("head", tmp_path)
    )
    monkeypatch.setattr(
        acceptance, "select_review_for_head", lambda *a, **k: ({}, "head")
    )
    monkeypatch.setattr(acceptance, "_review_accepts_promotion", lambda _: True)
    monkeypatch.setattr(acceptance, "_recorded_gate_passed", lambda _: ({}, ()))
    monkeypatch.setattr(acceptance, "_acceptance_input_view", lambda *a: {})
    monkeypatch.setattr(acceptance, "_acceptance_remote_inputs", lambda *a: [])
    policy = ["earlier policy"]
    monkeypatch.setattr(acceptance, "delivery_hold_reason", lambda *a: policy[0])

    first = acceptance.accept_clean_review(pointer)
    assert first["accepted"] is False
    assert "earlier policy" in first["reason"]
    assert acceptance.accept_clean_review(pointer)["unchanged"] is True
    _running(monkeypatch)
    policy[0] = "current policy"
    fresh = acceptance.accept_clean_review(pointer)
    print(f"memo after reload: {fresh}")
    assert "current policy" in fresh["reason"], fresh
    assert not fresh.get("unchanged"), fresh
    assert fresh["code_revision"] == CURRENT_CODE
    memo = requests._read(
        requests.runs.run_dir(request["run_id"]) / "acceptance-refusal.json"
    )
    assert memo["code_revision"] == CURRENT_CODE
    assert memo["refusal"]["code_revision"] == CURRENT_CODE
    assert acceptance.accept_clean_review(pointer)["unchanged"] is True


@pytest.mark.parametrize(
    "outcome,phase",
    [
        ({"ok": True}, requests.PROMOTED),
        ({"ok": False, "error": "refused"}, requests.REFUSED),
        (None, requests.DEFERRED),
        (RuntimeError("execution failed"), requests.FAILED),
    ],
)
def test_every_finished_verdict_names_the_loaded_code(monkeypatch, outcome, phase):
    _running(monkeypatch)
    request = _request("r-verdict")

    def execute(_request):
        # The checkout advances during judgement; the imported code did not.
        _running(monkeypatch, current=NEXT_CODE)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    service = requests.PromotionService(execute=execute)
    assert service.tick() == [request["run_id"]]
    service.settle(timeout=10)
    receipt = requests.read_promotion(request["run_id"])
    assert receipt["phase"] == phase, receipt
    assert receipt["code_revision"] == CURRENT_CODE
    drained = [
        json.loads(line)
        for line in requests._drained_log_path().read_text().splitlines()
    ]
    assert drained[-1]["code_revision"] == CURRENT_CODE
