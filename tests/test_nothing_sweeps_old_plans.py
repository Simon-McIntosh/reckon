"""A follower pass composes no review of a plan, and still composes a run's.

The review a plan is owed is composed where its author is present — on the edit
that changes it and at the build gate — so a follower sweep no longer walks the
project's plans. These cases hold both halves: a pass over a project holding an
active plan with no review composes no plan review and leaves the plan-review
report root untouched, while a finished run awaiting its review in the same pass
still has that review composed. ``crew review-plan`` composes on demand, which
is the surface an author reaches through the edit tool's owed-review invocation.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from click.testing import CliRunner

from reckon import cli, review_tiers
from reckon.crew import recovery_repair_dispatch
from reckon.crew import recovery_review_acceptance
from reckon.crew import recovery_review_delivery
from reckon.crew import recovery_review_dispatch
from reckon.crew import recovery_watch
from reckon.crew import plan_review, recovery
from tests.test_review_plan_command import CONFIG
from tests.test_review_plan_command import project as project  # noqa: PLC0414


def _backdate(path: Path) -> None:
    """Age the plan past the settle window the retired walk once read."""
    stamp = time.time() - 1000
    os.utime(path, (stamp, stamp))


def _report_listing() -> list[str]:
    root = plan_review.review_report_directory("sample", "fixture", "unused").parent
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _record_calls(monkeypatch) -> list:
    calls: list = []

    def record(record, *args, **kwargs):
        calls.append(record)
        return {
            "run_id": str(record.get("run_id") or ""),
            "dispatched": True,
            "review_run_id": "review-of-" + str(record.get("run_id") or ""),
        }

    (monkeypatch.setattr(recovery_review_acceptance, "dispatch_review_for_run", record), monkeypatch.setattr(recovery_watch, "dispatch_review_for_run", record))
    monkeypatch.setattr(recovery_review_acceptance, "dispatch_repair_for_run", lambda *a, **k: {})
    return calls


def test_a_follower_pass_composes_no_plan_review(project, monkeypatch):
    _, _, path = project
    _backdate(path)
    (monkeypatch.setattr(recovery_review_delivery, "list_live", lambda **kwargs: []), monkeypatch.setattr(recovery_review_dispatch, "list_live", lambda **kwargs: []), monkeypatch.setattr(recovery_repair_dispatch, "list_live", lambda **kwargs: []), monkeypatch.setattr(recovery_review_acceptance, "list_live", lambda **kwargs: []), monkeypatch.setattr(recovery_watch, "list_live", lambda **kwargs: []))
    calls = _record_calls(monkeypatch)
    before = _report_listing()

    report = recovery.dispatch_awaiting_reviews(
        project="sample", config=CONFIG, session="coordinator"
    )

    assert calls == [], "a plan walk must compose no plan review"
    assert report["dispatched"] == []
    assert _report_listing() == before == []


def test_a_finished_run_still_gets_its_review_in_the_same_pass(project, monkeypatch):
    pointer = {
        "run_id": "r-awaiting-review",
        "project": "sample",
        "session": "coordinator",
        "node": {"id": "r-awaiting-review", "plan": "fixture", "section": "delivery"},
        "phase": "complete",
    }
    (monkeypatch.setattr(recovery_review_delivery, "list_live", lambda **kwargs: [pointer]), monkeypatch.setattr(recovery_review_dispatch, "list_live", lambda **kwargs: [pointer]), monkeypatch.setattr(recovery_repair_dispatch, "list_live", lambda **kwargs: [pointer]), monkeypatch.setattr(recovery_review_acceptance, "list_live", lambda **kwargs: [pointer]), monkeypatch.setattr(recovery_watch, "list_live", lambda **kwargs: [pointer]))
    (monkeypatch.setattr(
        recovery_review_dispatch,
        "classify_pointer",
        lambda record, **kwargs: {"classification": "scoring", "manifest_commits": []},
    ), monkeypatch.setattr(
        recovery_review_acceptance,
        "classify_pointer",
        lambda record, **kwargs: {"classification": "scoring", "manifest_commits": []},
    ), monkeypatch.setattr(
        recovery_watch,
        "classify_pointer",
        lambda record, **kwargs: {"classification": "scoring", "manifest_commits": []},
    ))
    monkeypatch.setattr(
        recovery_review_acceptance, "_sweep_review_tier", lambda record, commits: review_tiers.FULL
    )
    calls = _record_calls(monkeypatch)

    report = recovery.dispatch_awaiting_reviews(
        project="sample", config=CONFIG, session="coordinator"
    )

    assert [str(record.get("run_id")) for record in calls] == ["r-awaiting-review"]
    assert report["dispatched"] == ["review-of-r-awaiting-review"]


def test_review_plan_still_composes_a_review_on_demand(project):
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review-plan",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--session",
            "coordinator",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["node"]["id"] == "plan-review-of-fixture"
    assert payload["node"]["role"] == "review"
