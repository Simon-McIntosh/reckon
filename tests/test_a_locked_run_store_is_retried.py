"""A promotion that loses the run store's write lock is retried by the service.

The run store is the durable authority for every promoted run, so a promotion
that cannot record its row is a refusal rather than a landed run. When the
refusal is the store's write lock outlasting its bounded wait, the cause is a
busy lane and not a broken delivery: nothing a coordinator can repair, and no
consumer of the delivery that has changed. The service therefore owes the retry
itself, on its next pass, once the lock is free.
"""

from __future__ import annotations

import sqlite3

from reckon import ledger
from reckon.crew import promotion_requests as requests
from reckon.crew.node import CrewError

CODE = "0" * 40
PROJECT = "locked-store-project"
SESSION = "locked-store-session"


def _running(monkeypatch) -> None:
    # Pin the loaded identity so a refusal is judged by the code that made it
    # rather than by the checkout's identity.
    monkeypatch.setattr(requests, "_JUDGING_CODE_REVISION", CODE, raising=False)
    monkeypatch.setattr(requests, "_current_code_revision", lambda: CODE)


def _request(run_id: str) -> dict[str, object]:
    requests.runs._write_json(
        requests.runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "phase": "complete",
            "project": PROJECT,
            "session": SESSION,
        },
    )
    return requests.request_promotion(run_id, options={"verdict": "passed"})


def _locked_store_refusal(run_id: str) -> ledger.LedgerError:
    """The refusal the ledger raises from the store's own lock error."""
    refusal = ledger.LedgerError(
        f"cannot record run {run_id!r} in the run store: "
        "OperationalError: database is locked. The run store is the "
        "durable authority for every promoted run, so this write is "
        "refused rather than leaving a run file whose row no store "
        "holds; the store was locked past the wait bound (120 s); "
        "promote the run again"
    )
    refusal.__cause__ = sqlite3.OperationalError("database is locked")
    return refusal


def test_a_locked_store_is_promoted_again_on_the_next_pass(monkeypatch) -> None:
    _running(monkeypatch)
    request = _request("r-locked-store")
    attempts: list[str] = []

    def execute(_request):
        attempts.append(str(_request["run_id"]))
        if len(attempts) == 1:
            raise _locked_store_refusal(str(_request["run_id"]))
        return {"ok": True}

    service = requests.PromotionService(width=1, execute=execute)
    assert service.tick() == [request["run_id"]]
    service.settle(timeout=10)
    failed = requests.read_promotion(str(request["run_id"]))
    print(f"after the locked pass: {failed['phase']} {failed.get('error')}")
    assert failed["phase"] == requests.FAILED

    # The next pass owes the retry; nobody re-requests the run.
    assert service.tick() == [request["run_id"]], (
        "the service left the request at "
        f"{requests.read_promotion(str(request['run_id']))['phase']} after its pass"
    )
    service.settle(timeout=10)
    promoted = requests.read_promotion(str(request["run_id"]))
    assert promoted["phase"] == requests.PROMOTED, promoted
    assert attempts == [request["run_id"], request["run_id"]]


def test_while_it_waits_the_obligation_names_the_locked_store(monkeypatch) -> None:
    _running(monkeypatch)
    request = _request("r-locked-obligation")

    def execute(_request):
        raise _locked_store_refusal(str(_request["run_id"]))

    service = requests.PromotionService(width=1, execute=execute)
    service.tick()
    service.settle(timeout=10)

    items = requests.failure_obligations(PROJECT, SESSION)
    item = next(item for item in items if item["run_id"] == request["run_id"])
    print(f"obligation: {item['reason']}")
    reason = str(item["reason"])
    assert "lock" in reason.lower(), reason
    assert "the service retries" in reason, reason


def test_a_gate_refusal_is_not_retried(monkeypatch) -> None:
    _running(monkeypatch)
    request = _request("r-gate-refused")
    attempts: list[str] = []

    def refuse(_request):
        attempts.append(str(_request["run_id"]))
        raise CrewError("the gate did not pass")

    service = requests.PromotionService(width=1, execute=refuse)
    assert service.tick() == [request["run_id"]]
    service.settle(timeout=10)
    refused = requests.read_promotion(str(request["run_id"]))
    assert refused["phase"] == requests.REFUSED

    assert service.tick() == []
    assert len(attempts) == 1
    items = requests.failure_obligations(PROJECT, SESSION)
    item = next(item for item in items if item["run_id"] == request["run_id"])
    assert "the service retries" not in str(item["reason"])
