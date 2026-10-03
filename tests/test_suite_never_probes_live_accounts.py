"""No test spawns a real provider account probe, whatever the machine's codex home.

A lane whose receipt-derived position is stale makes the lanes view re-query the
backend's account probe for a fresh figure. For a codex-backed lane that probe
is ``reckon._backends.run_probe``, which spawns ``<command> app-server``. Whether
it answers depends on the machine's live codex login, so before the suite-wide
guard a test's outcome and runtime depended on ambient account state: a logged-in
home answered in about four seconds and the lane adopted the probe's own stamp;
an unanswerable home burned the probe's twenty-second wait. This file pins the
closed behaviour — and, as its negative control, fails when the guard is removed.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

from reckon import _backends, budget, mcp_views
from reckon.crew import rollout as rollout_module

NOW = datetime(2026, 9, 21, 18, 0, 0, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return (
        moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _reset_in(hours: float) -> int:
    return int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())


def _quota(window_minutes: int, used_percent: float) -> object:
    return rollout_module.QuotaReading(
        window_minutes=window_minutes,
        used_percent=used_percent,
        resets_at=_reset_in(100.0),
    )


def _rollout_receipt(readings: object) -> object:
    return rollout_module.RolloutReceipt(
        cumulative_input_tokens=0,
        cumulative_cached_input_tokens=0,
        cumulative_output_tokens=0,
        maximum_request_input_tokens=0,
        requests_over_threshold=0,
        model_context_window=0,
        quota_readings=readings,
        plan_type="pro",
        generation_seconds=0.0,
        machine_seconds=0.0,
    )


def _stale_lane_run() -> dict:
    """A live run whose only record stamp is two hours old.

    The lanes view's default shelf life is sixty minutes, so this run's receipt
    is stale and the view re-queries the backend's probe for a fresh figure —
    the exact path that reached the live account surface.
    """
    return {
        "run_id": "r-stale",
        "backend": "solo",
        "session_id": "sess-stale",
        "created_at": _iso(NOW - timedelta(hours=2)),
    }


def _compose(runs: list[dict]):
    return mcp_views.crew_lanes_view(
        {"backends": {"solo": {"launch": "cli", "command": "codex"}}},
        runs,
        receipt_reader=lambda session_id: _rollout_receipt({300: _quota(300, 11.0)}),
        composed_at=_iso(NOW),
    )


class _SubprocessSpy:
    """Records every Popen/run the code under test attempts, then delegates."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, object]] = []
        original_popen = subprocess.Popen
        original_run = subprocess.run

        def popen(*args, **kwargs):
            self.calls.append(("Popen", args[0] if args else kwargs.get("args")))
            return original_popen(*args, **kwargs)

        def run(*args, **kwargs):
            self.calls.append(("run", args[0] if args else kwargs.get("args")))
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(subprocess, "run", run)


def test_no_subprocess_is_spawned_when_a_stale_lane_requeries(tmp_path, monkeypatch):
    """A stale lane re-query reaches the guard, not the machine's codex login.

    ``CODEX_HOME`` points at a home that would let a real probe answer, so the
    only thing standing between this composition and a spawned app server is
    the suite-wide guard. With it in place no subprocess is attempted and the
    lane keeps the run's own record stamp instead of a live observation.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "would-answer-honestly"))
    spy = _SubprocessSpy(monkeypatch)

    run = _stale_lane_run()
    recomposed = _compose([run])

    assert spy.calls == [], f"a live account probe was spawned: {spy.calls!r}"
    lane = next(item for item in recomposed["lanes"] if item["backend"] == "solo")
    assert lane["observed_at"] == budget.run_observed_stamp(run)


def test_a_test_may_opt_in_by_patching_the_launch_seam(monkeypatch):
    """A test whose subject is the probe replaces the guard at its own seam.

    Patching ``_backends.run_probe`` itself is the opt-in the Jev guard allows:
    the replacement is consulted by ``probe_budget`` exactly as the real launch
    would be, so a probe-behaviour test keeps working under the guard.
    """
    calls: list[object] = []

    def answering(probe):
        calls.append(probe)
        return {
            "id": probe.answer_id,
            "result": {
                "rateLimits": {
                    "primary": {"usedPercent": 12, "windowDurationMins": 300},
                }
            },
        }

    monkeypatch.setattr(_backends, "run_probe", answering)

    block = _backends.probe_budget(
        backend_name="solo",
        backend={"launch": "cli", "command": "codex"},
    )

    assert calls, "the opted-in seam was never consulted"
    assert block["headroom"] == "known"
