"""A refill tick decodes a promotion request only when its file has changed.

The refill thread ticks every two seconds over the whole request directory, and
settled requests stay there as the record of what each run's promotion did. On
a shared filesystem an uncached pass re-reads every one of those files each
tick, which measured 30 MB every two seconds for 1,700 requests. An unchanged
request is decoded once and answered from its file's identity afterwards.
"""

from __future__ import annotations

from reckon.crew import promotion_requests as requests

CODE = "0" * 40


def _settled(run_id: str) -> None:
    requests.runs._write_json(
        requests._request_path(run_id),
        {
            "run_id": run_id,
            "request_id": f"{run_id}-request",
            "phase": requests.SETTLED,
            "settled": "promoted",
            "project": "refill-project",
            "session": "refill-session",
            "code_revision": CODE,
        },
    )


def test_a_second_tick_decodes_no_unchanged_request(monkeypatch) -> None:
    monkeypatch.setattr(requests, "_JUDGING_CODE_REVISION", CODE, raising=False)
    monkeypatch.setattr(requests, "_current_code_revision", lambda: CODE)
    run_ids = [f"r-settled-{number}" for number in range(12)]
    for run_id in run_ids:
        _settled(run_id)
    decoded: list[str] = []
    real_read = requests._read

    def counting_read(path):
        decoded.append(path.name)
        return real_read(path)

    monkeypatch.setattr(requests, "_read", counting_read)
    service = requests.PromotionService(width=1, execute=lambda request: {})

    assert service.tick() == []
    first = sorted(decoded)
    assert first == sorted({f"{run_id}.json" for run_id in run_ids}), first

    decoded.clear()
    assert service.tick() == []
    assert decoded == [], f"the second tick decoded unchanged requests: {decoded}"


def test_a_changed_request_is_decoded_again(monkeypatch) -> None:
    monkeypatch.setattr(requests, "_JUDGING_CODE_REVISION", CODE, raising=False)
    monkeypatch.setattr(requests, "_current_code_revision", lambda: CODE)
    _settled("r-settled-a")
    _settled("r-settled-b")
    service = requests.PromotionService(width=1, execute=lambda request: {})
    service.tick()

    decoded: list[str] = []
    real_read = requests._read

    def counting_read(path):
        decoded.append(path.name)
        return real_read(path)

    monkeypatch.setattr(requests, "_read", counting_read)
    path = requests._request_path("r-settled-b")
    record = requests._read(path)
    decoded.clear()
    record["settled"] = "discarded"
    record["padding"] = "changes the file's size as well as its time"
    requests.runs._write_json(path, record)

    service.tick()
    assert "r-settled-b.json" in decoded
    assert "r-settled-a.json" not in decoded
