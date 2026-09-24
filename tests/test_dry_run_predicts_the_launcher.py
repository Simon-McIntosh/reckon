"""A dry run reports what the launcher would do, not that it would be asked.

`would_resume` is a claim about the launcher's decision. A dry run that returns
before the launcher is ever consulted reports a weaker claim under a stronger
name, and it errs in the one direction that causes harm: a hand
`crew resume-ready --dry-run` reported four runs to resume four actively-working
sessions.

So the dry run consults the launcher's own guards. These tests hold it to the
claim two ways: end to end, against the report a real sweep produces for the
same record, and by parity — every guard the launcher applies must be a guard
the prediction applies, with the same exception and the same message.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.dispatch import BudgetHold, resume_plan
from reckon.crew.node import CrewError
from reckon.crew.resumption import _launcher_refusal, sweep
from reckon.crew.runs import _write_json, pointer_path
from tests.test_budget import _known
from tests.test_crew_recover import PROJECT, _refused_run, _stated_reset

# The pid a launcher stand-in reports, so a spied launch is recognisable without
# being executed. Liveness is never faked: a test needing a live process records
# its own pid, which is alive for both sides of a parity claim.
LIVE_PID = 4_242_424


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


class _Launcher:
    """Stands in for the spawn, recording what was launched, not launching it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.calls.append(
            {
                "log_path": log_path,
                "stderr_path": stderr_path,
                "prompt_path": prompt_path,
            }
        )
        return LIVE_PID


def _make_live(tmp_path: Path, run_id: str) -> None:
    """Record this test process as the run's still-running process.

    The pid is genuinely alive, which is what makes both sides of a parity
    claim read the same fact: a monkeypatched liveness would be patched at one
    import site and not the other, and the two would then disagree for a reason
    that is the harness's rather than the code's.
    """
    record = crew.read_pointer(run_id)
    record["pid"] = os.getpid()
    _write_json(pointer_path(run_id), record)


def _record_exhausted(record: dict) -> None:
    """A completed run whose report holds its lane at a spent quota."""
    project = str(record["project"])
    if not ledger.ledger_path(project, record["repo"]).exists():
        ledger.write(
            project, {"members": [], "runs": [], "holds": []}, 0, root=record["repo"]
        )
    ledger.append_run(
        project,
        ledger.build_record(
            run_id="r-20260917T000000000000-exhausted",
            plan="plan-a",
            gate="passed",
            agent={"backend": str(record["backend"])},
            completed_at="2026-09-17T00:00:00Z",
            budget=_known(100.0),
        ),
        root=record["repo"],
    )


@pytest.mark.parametrize("guard", ["not-a-spawned-run", "live-process"])
def test_a_run_the_sweep_never_holds_is_never_reported_as_a_resume(
    home: Path, tmp_path: Path, guard: str
) -> None:
    """Neither mode offers a resume for a run the sweep drops as a candidate.

    A live process, and a run not spawned as a CLI run, are both removed by the
    sweep's own classification before a launcher is ever consulted, so the dry
    run reports nothing for them. Their guards are still part of what a launcher
    refuses, so the prediction keeps them and the parity test below exercises
    them directly, because the sweep's candidate filter makes them unreachable
    through this door.
    """
    run_id = f"r-20260917T100000000000-node-{guard}"
    record = _refused_run(tmp_path, run_id)
    if guard == "not-a-spawned-run":
        record["launch"] = "in-harness"
        _write_json(pointer_path(run_id), record)
    else:
        _make_live(tmp_path, run_id)

    dry = sweep(PROJECT, dry_run=True, now=_stated_reset() + timedelta(minutes=1))

    assert dry["resumed"] == [] and dry["skipped"] == []
    assert _launcher_refusal(crew.read_pointer(run_id), config=None) is not None


def test_a_held_lane_is_reported_as_a_hold_in_both_modes(
    home: Path, tmp_path: Path
) -> None:
    """The budget guard is a launcher guard, so the dry run consults the lane."""
    run_id = "r-20260917T110000000000-node-a"
    record = _refused_run(tmp_path, run_id)
    _record_exhausted(record)
    moment = _stated_reset() + timedelta(minutes=1)

    dry = sweep(PROJECT, dry_run=True, now=moment)
    launcher = _Launcher()
    real = sweep(PROJECT, launcher=launcher, now=moment)

    assert dry["skipped"][0]["reason"] == "hold-in-force"
    assert "would_resume" not in dry["skipped"][0]
    assert dry["skipped"] == real["skipped"]


def test_the_dry_run_still_offers_a_run_the_launcher_would_resume(
    home: Path, tmp_path: Path
) -> None:
    """The positive control: consulting guards must not silence a real resume."""
    run_id = "r-20260917T120000000000-node-a"
    _refused_run(tmp_path, run_id)
    moment = _stated_reset() + timedelta(minutes=1)

    dry = sweep(PROJECT, dry_run=True, now=moment)

    assert [item["run_id"] for item in dry["resumed"]] == [run_id]
    assert dry["resumed"][0]["would_resume"] is True
    assert dry["skipped"] == []
    assert "auto_resume" not in crew.read_pointer(run_id)
    assert crew.read_pointer(run_id)["phase"] == "working"


@pytest.mark.parametrize("guard", ["spawned", "live", "budget"])
def test_every_guard_the_launcher_applies_is_a_guard_the_prediction_applies(
    home: Path,
    tmp_path: Path,
    guard: str,
) -> None:
    """Parity, held against the launcher itself rather than restated here.

    Both sides are asked about the same record. Where neither refuses, the
    prediction must offer the resume; where the launcher refuses, the prediction
    must refuse with the launcher's own exception and message.
    """
    run_id = f"r-20260917T130000000000-node-{guard}"
    record = _refused_run(tmp_path, run_id)
    if guard == "spawned":
        record["launch"] = "in-harness"
        _write_json(pointer_path(run_id), record)
    elif guard == "live":
        _make_live(tmp_path, run_id)
    elif guard == "budget":
        _record_exhausted(record)

    mirror = _launcher_refusal(crew.read_pointer(run_id), config=None)
    launcher_exc: BaseException | None = None
    try:
        resume_plan(run_id, "continue", config=None)
    except (BudgetHold, CrewError, OSError) as exc:
        launcher_exc = exc

    if launcher_exc is None:
        assert mirror is None, "the prediction refused what the launcher allowed"
    else:
        assert mirror is not None, f"the prediction missed the {guard!r} guard"
        assert type(mirror) is type(launcher_exc)
        assert str(mirror) == str(launcher_exc)
