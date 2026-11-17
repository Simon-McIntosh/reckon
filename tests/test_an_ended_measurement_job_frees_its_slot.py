"""A measurement job the queue has forgotten is read from the accounting record.

The lander holds a bounded number of scheduler measurements in flight. A job
that ended is dropped from the queue after a few minutes, and the queue then
refuses a query naming it. Its slot must be released on the accounting record's
answer; otherwise two ended jobs hold both slots and nothing lands again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reckon.crew import landing_measure


def _scheduler(queue, accounting):
    """A scheduler whose two reads answer ``(returncode, stdout)`` each."""
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv[0])
        code, out = queue if argv[0] == "squeue" else accounting
        return SimpleNamespace(returncode=code, stdout=out, stderr="refused")

    return run, calls


def _job(job_id="41"):
    return {"job_id": job_id, "log_path": "/logs/measure.log", "status": "pending"}


@pytest.mark.parametrize("ended", ["COMPLETED", "TIMEOUT", "FAILED"])
def test_a_forgotten_job_is_read_from_accounting(monkeypatch, ended) -> None:
    run, calls = _scheduler(queue=(1, ""), accounting=(0, f"{ended}\n"))
    monkeypatch.setattr(landing_measure, "_SCHEDULER_RUN", run)

    assert landing_measure.measurement_job_state(_job()) == ended
    assert calls == ["squeue", "sacct"]


def test_ended_jobs_hold_no_measurement_slot(monkeypatch) -> None:
    run, _calls = _scheduler(queue=(1, ""), accounting=(0, "TIMEOUT\n"))
    monkeypatch.setattr(landing_measure, "_SCHEDULER_RUN", run)
    pointers = [
        {"run_id": "first", "landing_measurement_job": _job("41")},
        {"run_id": "second", "landing_measurement_job": _job("42")},
    ]

    assert landing_measure.active_measurement_jobs(pointers) == set()


def test_a_queued_job_still_holds_its_slot(monkeypatch) -> None:
    run, calls = _scheduler(queue=(0, "RUNNING\n"), accounting=(0, "RUNNING\n"))
    monkeypatch.setattr(landing_measure, "_SCHEDULER_RUN", run)
    pointers = [{"run_id": "live", "landing_measurement_job": _job()}]

    assert landing_measure.active_measurement_jobs(pointers) == {"live"}
    assert calls == ["squeue"]


def test_a_refused_accounting_read_keeps_the_slot(monkeypatch) -> None:
    run, _calls = _scheduler(queue=(1, ""), accounting=(1, ""))
    monkeypatch.setattr(landing_measure, "_SCHEDULER_RUN", run)
    pointers = [{"run_id": "unknown", "landing_measurement_job": _job()}]

    with pytest.raises(ValueError, match="scheduler query failed"):
        landing_measure.measurement_job_state(_job())
    assert landing_measure.active_measurement_jobs(pointers) == {"unknown"}
