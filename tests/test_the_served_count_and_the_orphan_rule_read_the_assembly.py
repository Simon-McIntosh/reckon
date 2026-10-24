"""The served blocker count and the orphan rule read one assembled blocker list.

A plan stored ``blocked`` with no recorded blocker row is held by a wait nothing
has written down. The assembly synthesizes a ``persisted`` placeholder so the
plan does not read open to a reader. Two readers consume that assembly
differently, and the difference is the point: the served view publishes how many
blockers a plan is held by, so the placeholder must be in that count, while the
audit asks whether the stored status has a *recorded* hold, so the placeholder
must not be in the list it reads. Counting the recorded rows made the served
count 0 for a plan the same row reported blocked — a reader of the count saw no
wait at all.

The roadmap's orphan rule is the one that answers whether the status has an
explanation, and it answers in the lifecycle category: the finding is about a
plan's state, not about its wiring. A plan whose cleared blocker a probe
released is exempt, because its blocker resource exists and carries the clear.

Every home read here is a temporary path: ``RECKON_HOME`` covers the
configuration home, the crew home and the cache, and ``RECKON_STATE_ROOT`` and
``RECKON_MOUNTS_PATH`` name the state root and the mounts table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.serve as serve_module
from reckon import _plan_html, project_state

PROJECT = "proj"

PLAN_PAGE = (
    '<!doctype html>\n<html lang="en">\n<head>'
    '<meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    "<title>{slug}</title></head>\n"
    '<body><main class="plan-doc">'
    '<section data-reckon="gates"></section>'
    "</main></body>\n</html>\n"
)


def _write_plan(docs: Path, slug: str, state: dict) -> None:
    payload = {
        "slug": slug,
        "title": slug.title(),
        "status": "active",
        "type": "plan",
    }
    payload.update(state)
    path = docs / f"{slug}.html"
    path.write_text(
        _plan_html.write_state(PLAN_PAGE.format(slug=slug), payload), encoding="utf-8"
    )


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """A project whose every environment-resolved home is a temporary path."""

    docs = tmp_path / "docs"
    docs.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")

    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setattr(serve_module, "_MOUNTS_FILE", mounts_file, raising=False)
    monkeypatch.setattr(serve_module, "_STATE_ROOT", state_root, raising=False)

    return docs, state_root


def _project_with_a_held_and_a_released_plan(docs: Path, state_root: Path) -> None:
    """One plan stored blocked with nothing recorded, one whose hold cleared.

    Composition drops a cleared blocker from the sprint item that named it, so
    the released plan is left stored blocked with no blocker row either. Only
    the raw sprint record still attaches the hold to the plan, which is where
    the roadmap reads the release from — so the two plans differ by release
    and by nothing else.
    """

    project_state.create_project_state(docs, PROJECT)
    _write_plan(docs, "unrecorded", {"status": "blocked"})
    _write_plan(docs, "released", {"status": "blocked"})
    project_state.write_resource(
        docs,
        PROJECT,
        "blocker",
        "waiting",
        {
            "kind": "held",
            "summary": "Waiting for an outcome",
            "probe": "path-exists",
            "subject": "research/outcome.html",
        },
        0,
        create=True,
    )
    project_state.write_resource(
        docs,
        PROJECT,
        "sprint",
        "current",
        {
            "theme": "Current",
            "status": "active",
            "items": [{"slug": "released", "blocked_by": ["waiting"]}],
        },
        0,
        create=True,
    )
    subject = docs / "research" / "outcome.html"
    subject.parent.mkdir(parents=True, exist_ok=True)
    subject.write_text("arrived", encoding="utf-8")
    report = project_state.evaluate_held_blocker(docs, PROJECT, "waiting")
    assert report["status"] == "cleared", report


def _surfaces(docs: Path, state_root: Path) -> tuple[dict[str, dict], dict]:
    from reckon.roadmap import build_roadmap

    discovered = serve_module.discover_plans(docs, PROJECT, state_root)
    served = {
        plan["slug"]: plan
        for plan in discovered["inventory"]
        if plan.get("type") == "plan"
    }
    report = build_roadmap(
        PROJECT,
        discovered["inventory"],
        discovered["sprints"],
        active_sprint_id=discovered["active_sprint_id"],
        project_manifest=discovered,
        docs_dir=docs,
    )
    return served, report


def _orphans(report: dict, slug: str) -> list[dict]:
    findings = report["wiring_findings"]
    return [
        finding
        for finding in findings
        if finding.get("code") == "orphaned-blocked-status"
        and finding.get("slug") == slug
    ]


def test_the_served_count_includes_the_assembled_placeholder(homes) -> None:
    docs, state_root = homes
    _project_with_a_held_and_a_released_plan(docs, state_root)

    served, _ = _surfaces(docs, state_root)

    plan = served["unrecorded"]
    assert plan["effective_status"] == "blocked"
    assert plan["blockers"] >= 1, (
        "a plan reported blocked must not report a blocker count of "
        f"{plan['blockers']}: {plan!r}"
    )
    # The audit asks whether the status has a recorded hold, and this plan has
    # none. Publishing the placeholder into the list it reads would hide the
    # finding the roadmap is about to report.
    assert plan["blocking"] == [], plan["blocking"]


def test_the_orphan_rule_reports_a_status_with_no_recorded_hold_in_lifecycle(
    homes,
) -> None:
    docs, state_root = homes
    _project_with_a_held_and_a_released_plan(docs, state_root)

    _, report = _surfaces(docs, state_root)

    orphans = _orphans(report, "unrecorded")
    assert len(orphans) == 1, orphans
    assert orphans[0]["category"] == "lifecycle", orphans[0]
    assert orphans[0]["slug"] == "unrecorded"


def test_a_released_plan_gets_no_orphan_finding(homes) -> None:
    docs, state_root = homes
    _project_with_a_held_and_a_released_plan(docs, state_root)

    served, report = _surfaces(docs, state_root)

    assert _orphans(report, "released") == []
    # The released plan carries no hold, so nothing is assembled to count.
    assert served["released"]["blockers"] == 0, served["released"]
    assert served["released"]["effective_status"] != "blocked", served["released"]
