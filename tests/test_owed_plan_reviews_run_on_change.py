"""The owed-plan-review pass runs on a change or on its interval, not a sweep.

The pass weighs the project's authored plans against its stored reviews, and it
used to run on every sweep whether or not anything it reads had moved. This test
drives sweeps over one temporary project:

* thirty sweeps at one second each with nothing changing run the pass once, and
  never again inside its interval;
* editing one plan file, storing one plan review and recording one plan-authoring
  run each run the pass once, on the sweep after the change;
* the clock passing the interval runs it once more with nothing changed.

A second limb replays a scenario in which reviews really compose, in two arms:
one with the interval held at zero, which is the pass running on every sweep as
the base revision runs it, and one gated. Every review the ungated arm composes
must be composed by the ungated arm's moment, within one interval, by the gated
arm, and the two arms must agree on what each pass they share weighed, once the
age a plan's skip reason carries is normalised out and the per-pass rotation
order is dropped.

Every environment-resolved directory points at the temporary project, and the
test asserts the real reflex directory gained nothing: a reader can only be
wrong, while a writer can make someone else wrong.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import file_memo, run_store
from reckon.crew import plan_review, recovery, resumption

PROJECT = "owed-review-gate-fixture"
QUIET_SWEEPS = 30
START = datetime(2030, 1, 1, tzinfo=UTC)
# The interval the gate holds, read before any arm overrides it. It is read with
# a default so that the file also runs against a revision that has no gate: what
# it then asserts is the sweeps, which is exactly the comparison the negative
# control makes.
INTERVAL = float(getattr(plan_review, "OWED_PLAN_REVIEW_PASS_SECONDS", 60.0))

# The plan's age when a scenario starts: past the quiet period and inside the
# authoring burst, so the pass weighs it rather than filing it away. It is a
# distance from the scenario's own clock, never a date.
PLAN_AGE = 900.0
AUTHOR_AGE = 120.0
# The sweep on which the parity scenario makes its second plan authorable.
CHANGE_SWEEP = 5
# How many sweeps a parity arm runs.
ARM_SWEEPS = 10

# The pass as the module defines it, held before any arm wraps it. A second arm
# in the same test must call the pass itself rather than the wrapper the first
# arm left in place, or the two arms' records would count each other's passes.
_REAL_COMPOSE = plan_review.compose_owed_plan_reviews


def _plan_document(slug: str, prose: str = "Deliver the behaviour.") -> str:
    return (
        "<html><head>"
        f"<title>{slug}</title>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        "</head><body>"
        '<h2 id="work">Work</h2>'
        f"<p>{prose}</p>"
        "</body></html>"
    )


def _authoring_record(run_id: str, plan: str, moment: datetime) -> dict[str, object]:
    """One plan-authoring run as the run store holds it, naming its plan."""
    return {
        "run_id": run_id,
        "plan": plan,
        "session": "author-session",
        "role": "implement",
        "member": "member-a",
        "dispatched_at": (moment - timedelta(seconds=200)).isoformat(),
        "completed_at": (moment - timedelta(seconds=AUTHOR_AGE)).isoformat(),
    }


def _write_plan(path: Path, slug: str, prose: str, edited: datetime) -> None:
    """Write a plan and stamp the edit the scenario says it carries."""
    path.write_text(_plan_document(slug, prose))
    stamp = edited.timestamp()
    os.utime(path, (stamp, stamp))


def _project(root: Path) -> SimpleNamespace:
    """A temporary project of two plans, both written before its clock starts."""
    home = root / "home"
    docs = root / "repo" / "docs"
    (docs / "plans").mkdir(parents=True)
    home.mkdir(parents=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    paths = {}
    for slug in ("topic-00", "topic-01"):
        path = docs / "plans" / f"{slug}.html"
        _write_plan(path, slug, "Deliver the behaviour.", START - timedelta(days=2))
        paths[slug] = path
    return SimpleNamespace(
        home=home,
        docs=docs,
        reviews=home / "crew" / "reviews",
        paths=paths,
    )


def _forget_change_detection() -> None:
    """Drop the memories that would carry one arm's answer into the next."""
    file_memo.clear()
    for name in ("_PLAN_LISTINGS", "_PLAN_FACTS", "_RECORD_SUMMARIES"):
        held = getattr(plan_review, name, None)
        if isinstance(held, dict):
            held.clear()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Point every environment-resolved directory at the temporary project."""
    population = _project(tmp_path)
    monkeypatch.setenv("RECKON_HOME", str(population.home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(population.home / "runs.db"))
    _forget_change_detection()
    yield population
    _forget_change_detection()


def _count_passes(monkeypatch) -> list[float | None]:
    """Stand in for the pass and record the moment of every sweep it runs on."""
    passes: list[float | None] = []

    def compose(project_name: str, **kwargs):
        passes.append(kwargs.get("now_seconds"))
        return {}

    monkeypatch.setattr(plan_review, "compose_owed_plan_reviews", compose)
    return passes


def _sweep_at(passes: list[float | None], second: float) -> int:
    """One sweep `second` seconds after the start, answering passes it ran."""
    before = len(passes)
    resumption.sweep(PROJECT, pointers=[], now=START + timedelta(seconds=second))
    return len(passes) - before


def test_thirty_quiet_sweeps_run_the_pass_once(project, monkeypatch):
    """Nothing changed: the pass runs on the cold sweep and on no other."""
    passes = _count_passes(monkeypatch)
    ran = [_sweep_at(passes, second) for second in range(QUIET_SWEEPS)]
    assert sum(ran) == 1, f"the quiet sweeps ran the pass {sum(ran)} times: {ran}"
    # The one pass is the cold sweep's, which is the stand-in proving it can
    # count a pass at all: a counter that saw nothing would pass the sum above.
    assert ran[0] == 1, ran


def test_each_changed_input_runs_the_pass_once_more(project, monkeypatch):
    """A plan edit, a stored review and an authoring run each run it once."""
    passes = _count_passes(monkeypatch)
    for second in range(QUIET_SWEEPS):
        _sweep_at(passes, second)
    assert _sweep_at(passes, QUIET_SWEEPS) == 0, "nothing changed, nothing runs"

    path = project.paths["topic-00"]
    _write_plan(
        path,
        "topic-00",
        "Deliver the changed behaviour.",
        START - timedelta(seconds=PLAN_AGE),
    )
    assert _sweep_at(passes, QUIET_SWEEPS + 1) == 1, "a plan edit asks for the pass"
    assert _sweep_at(passes, QUIET_SWEEPS + 2) == 0, "the pass spent that change"

    staging = project.reviews / PROJECT
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "plan-topic-01.v2.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "plan_slug": "topic-01",
                "plan_version": 2,
                "rubric": "content",
                "findings": [],
                "review_run_id": "review-of-topic-01",
                "timestamp": START.isoformat(),
            }
        )
    )
    assert _sweep_at(passes, QUIET_SWEEPS + 3) == 1, "a stored review asks for it"
    assert _sweep_at(passes, QUIET_SWEEPS + 4) == 0, "the pass spent that change"

    run_store.append(PROJECT, _authoring_record("r-author-b", "topic-01", START))
    assert _sweep_at(passes, QUIET_SWEEPS + 5) == 1, "a recorded run asks for it"
    assert _sweep_at(passes, QUIET_SWEEPS + 6) == 0, "the pass spent that change"
    assert len(passes) == 4, passes


def test_the_pass_runs_again_when_the_interval_expires(project, monkeypatch):
    """With nothing changing, the clock alone asks for one more pass."""
    passes = _count_passes(monkeypatch)
    assert _sweep_at(passes, 0) == 1
    quiet = [_sweep_at(passes, second) for second in range(1, int(INTERVAL))]
    assert sum(quiet) == 0, f"the interval expired early: {quiet}"
    assert _sweep_at(passes, int(INTERVAL)) == 1, "the interval asks for the pass"
    assert _sweep_at(passes, int(INTERVAL) + 1) == 0, "and the next sweep does not"


def test_the_gate_records_under_the_temporary_home_only(project, monkeypatch):
    """The gate's record is written where the environment points, and nowhere else."""
    passes = _count_passes(monkeypatch)
    assert _sweep_at(passes, 0) == 1
    recorded = project.home / "crew" / "plan-review-reflex" / f"{PROJECT}.pass.json"
    assert recorded.exists(), f"the gate recorded no pass at {recorded}"
    real = (
        Path.home()
        / ".config"
        / "reckon"
        / "crew"
        / "plan-review-reflex"
        / f"{PROJECT}.pass.json"
    )
    assert not real.exists(), f"the gate wrote outside the temporary home: {real}"


def _dispatch_stand_in(monkeypatch) -> list[str]:
    """Replace the review dispatch with a recorder that starts nothing."""
    composed: list[str] = []

    def subject(project_name, plan_slug, session, *, rubric="design", local=False):
        return {
            "subject": "plan",
            "project": project_name,
            "plan_slug": plan_slug,
            "session": session,
            "rubric": rubric,
            "run_id": f"composed-{plan_slug}-{len(composed)}",
        }

    def dispatch(brief, *, config=None, launcher=None):
        composed.append(str(brief["plan_slug"]))
        return {"dispatched": True}

    monkeypatch.setattr(recovery, "plan_review_subject", subject)
    monkeypatch.setattr(recovery, "dispatch_review_for_run", dispatch)
    return composed


def _without_age(reason: str) -> str:
    """A skip reason with the figures it carries — the ages — collapsed.

    Two arms run the same scenario against their own clocks, so a collapsing
    quiet period or a differing edit age is a difference in the arms' clocks and
    not in what the pass concluded. The plan's own name is a separate field of
    the entry, so no plan identity is normalised away here.
    """
    return re.sub(r"\d+", "#", reason)


def _weighed(entry: dict) -> dict:
    """What one pass composed and skipped, ignoring the rotation order."""
    return {
        "composed": sorted(
            str(item.get("plan") or "") for item in entry.get("composed") or []
        ),
        "skipped": sorted(
            (
                str(item.get("plan") or ""),
                _without_age(str(item.get("reason") or "")),
            )
            for item in entry.get("skipped") or []
        ),
    }


def _parity_arm(root: Path, monkeypatch, *, interval: float) -> SimpleNamespace:
    """Run one arm of the composition scenario and answer what it did."""
    _forget_change_detection()
    population = _project(root)
    monkeypatch.setenv("RECKON_HOME", str(population.home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(population.home / "runs.db"))
    composed = _dispatch_stand_in(monkeypatch)
    monkeypatch.setattr(
        plan_review, "OWED_PLAN_REVIEW_PASS_SECONDS", interval, raising=False
    )

    passes: list[int] = []
    composed_at: dict[str, int] = {}
    reports: dict[int, dict] = {}
    current = {"sweep": 0}

    def composing(project_name, **kwargs):
        passes.append(current["sweep"])
        return _REAL_COMPOSE(project_name, **kwargs)

    monkeypatch.setattr(plan_review, "compose_owed_plan_reviews", composing)

    _write_plan(
        population.paths["topic-00"],
        "topic-00",
        "Deliver the behaviour.",
        START - timedelta(seconds=PLAN_AGE),
    )
    run_store.append(PROJECT, _authoring_record("r-author-a", "topic-00", START))

    for sweep in range(ARM_SWEEPS):
        current["sweep"] = sweep
        moment = START + timedelta(seconds=sweep)
        if sweep == CHANGE_SWEEP:
            _write_plan(
                population.paths["topic-01"],
                "topic-01",
                "Deliver the other behaviour.",
                moment - timedelta(seconds=PLAN_AGE),
            )
            run_store.append(
                PROJECT, _authoring_record("r-author-b", "topic-01", moment)
            )
        report = resumption.sweep(PROJECT, pointers=[], now=moment, config={})
        entry = report.get("plan_reviews") or {}
        reports[sweep] = entry
        for item in entry.get("composed") or []:
            composed_at.setdefault(str(item.get("plan") or ""), sweep)
    return SimpleNamespace(
        composed=composed,
        composed_at=composed_at,
        passes=passes,
        reports=reports,
    )


def test_a_review_the_ungated_pass_composes_is_composed_within_the_interval(
    tmp_path, monkeypatch
):
    """The gate saves sweeps without delaying a review the base pass composes."""
    ungated = _parity_arm(tmp_path / "ungated", monkeypatch, interval=0.0)
    gated = _parity_arm(tmp_path / "gated", monkeypatch, interval=INTERVAL)

    # A positive control for the scenario itself: the ungated arm is the pass
    # running on every sweep, and it composes, so a gate that composed nothing
    # would be caught by the comparison below rather than passing it vacuously.
    assert ungated.passes == list(range(ARM_SWEEPS)), ungated.passes
    assert ungated.composed_at, "the ungated arm composed nothing to compare against"
    assert len(gated.passes) < len(ungated.passes), (
        f"the gate ran the pass on {len(gated.passes)} of {ARM_SWEEPS} sweeps"
    )

    for plan, moment in ungated.composed_at.items():
        assert plan in gated.composed_at, f"the gate never composed {plan}"
        delay = gated.composed_at[plan] - moment
        assert delay <= INTERVAL, (
            f"{plan} was composed {delay} s after the ungated pass composed it"
        )

    # What each pass they share weighed, with the ages a reason carries
    # normalised out and the rotation order dropped: a gate that reached the
    # pass must reach the same conclusions as the pass running on every sweep.
    assert set(gated.passes) <= set(ungated.passes), gated.passes
    for sweep in sorted(set(gated.passes) & set(ungated.passes)):
        assert _weighed(gated.reports[sweep]) == _weighed(ungated.reports[sweep]), (
            f"sweep {sweep} weighed differently under the gate: "
            f"{_weighed(gated.reports[sweep])} != {_weighed(ungated.reports[sweep])}"
        )
