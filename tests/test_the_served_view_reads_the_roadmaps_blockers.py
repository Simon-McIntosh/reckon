"""The served view and the roadmap answer one blocker assembly.

A plan's blocked state is one fact with two readers: the roadmap a coordinator
reads, and the plan list the SPA renders. Derived twice, they drifted — a plan
held by a plan-level open decision read blocked through the roadmap and open
through the served view, because the served view assembled a blocker list from
dependencies and gates alone.

The three plans here separate the holds. ``deciding`` carries an unscoped open
decision, ``gated`` carries an unpassed execution gate over its only
implementable section, and ``free`` carries neither. Each case compares the two
surfaces against each other rather than asserting a literal about either one:
two literals can drift in step while the surfaces disagree.
"""

from __future__ import annotations

from pathlib import Path

from reckon._plan_html import write_state

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
    page = PLAN_PAGE.format(slug=slug)
    (docs / f"{slug}.html").write_text(write_state(page, payload), encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(
        docs,
        "deciding",
        {"decisions": {"transport": {"title": "Which transport?", "choice": ""}}},
    )
    _write_plan(
        docs,
        "gated",
        {
            "section_declarations": {"s1": "implementable"},
            "gates": [
                {
                    "id": "s1-evidence",
                    "section": "s1",
                    "gated_sections": [],
                    "status": "open",
                    "measure": "Evidence for s1",
                    "verdict": "open",
                    "evidence": "",
                }
            ],
        },
    )
    _write_plan(docs, "free", {"section_declarations": {"s1": "implementable"}})
    _released_project(docs)
    return docs


def _released_project(docs: Path) -> None:
    """A plan stored blocked whose held blocker a probe has already released.

    Composition drops a cleared blocker from the sprint item that named it, so
    the plan is left stored blocked with no blocker row to explain it. Only the
    raw sprint record still attaches the hold to the plan, which is where the
    roadmap reads the release from.
    """

    from reckon import project_state

    project_state.create_project_state(docs, PROJECT)
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


def _served_and_roadmap(docs: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    from reckon.roadmap import build_roadmap
    from reckon.serve import discover_plans

    discovered = discover_plans(docs, PROJECT, None)
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
    roadmap = {
        row["slug"]: row
        for bucket in ("ready_now", "blocked", "deferred")
        for row in report.get(bucket, [])
    }
    return served, roadmap


def _served_blockers(plan: dict) -> dict[str, set[str]]:
    """The served plan's blocker list, grouped by kind."""

    grouped: dict[str, set[str]] = {}
    for row in plan.get("blocking") or []:
        kind = str(row.get("kind") or "dependency")
        grouped.setdefault(kind, set()).add(str(row.get("id") or row.get("ref") or ""))
    return grouped


def _roadmap_blockers(row: dict) -> dict[str, set[str]]:
    """The roadmap row's blocker list, grouped by the kind each field reports."""

    fields = {
        "explicit": "explicit_blockers",
        "held": "held_blockers",
        "gate": "gate_blockers",
        "decision": "decision_blockers",
    }
    grouped: dict[str, set[str]] = {}
    for kind, field in fields.items():
        ids = {str(blocker.get("id") or "") for blocker in row.get(field) or []}
        if ids:
            grouped[kind] = ids
    return grouped


def _blocked(plan: dict) -> bool:
    return plan["effective_status"] == "blocked"


def _roadmap_blocked(row: dict) -> bool:
    return row["readiness"] == "blocked"


def test_the_served_view_and_the_roadmap_agree_on_every_plan(tmp_path: Path) -> None:
    served, roadmap = _served_and_roadmap(_project(tmp_path))

    expected = {"deciding", "gated", "free", "released"}
    assert set(served) == expected
    assert set(roadmap) == expected

    for slug in sorted(expected):
        plan = served[slug]
        row = roadmap[slug]
        assert _served_blockers(plan) == _roadmap_blockers(row), (
            f"{slug}: served {_served_blockers(plan)} vs roadmap "
            f"{_roadmap_blockers(row)}"
        )
        assert _blocked(plan) == _roadmap_blocked(row), (
            f"{slug}: served {plan['effective_status']} vs roadmap {row['readiness']}"
        )


def test_the_decision_held_plan_reads_blocked_on_both_surfaces(tmp_path: Path) -> None:
    served, roadmap = _served_and_roadmap(_project(tmp_path))

    assert _blocked(served["deciding"])
    assert _roadmap_blocked(roadmap["deciding"])
    assert _served_blockers(served["deciding"]) == {"decision": {"transport"}}


def test_the_gate_held_plan_reads_blocked_on_both_surfaces(tmp_path: Path) -> None:
    served, roadmap = _served_and_roadmap(_project(tmp_path))

    assert _blocked(served["gated"])
    assert _roadmap_blocked(roadmap["gated"])
    assert _served_blockers(served["gated"]) == {"gate": {"s1-evidence"}}


def test_a_free_plan_reads_open_on_both_surfaces(tmp_path: Path) -> None:
    served, roadmap = _served_and_roadmap(_project(tmp_path))

    assert not _blocked(served["free"])
    assert not _roadmap_blocked(roadmap["free"])
    assert _served_blockers(served["free"]) == {}
    assert _roadmap_blockers(roadmap["free"]) == {}


def test_a_statusless_dependency_row_reads_as_a_draft() -> None:
    """A row with no status anywhere reports the same status the roadmap ranks by.

    A dependency row built from a plan payload that carries neither status
    spelling has to answer something, and the answer is the roadmap's own
    default: a draft plan. Reading the precedence anywhere but ``_status``
    leaves an empty string here while every other reader of the same row calls
    it a draft.
    """

    from reckon.roadmap import dependency_row_state

    assert dependency_row_state({"slug": "plain"}, "") == "draft"
    assert dependency_row_state({}, "") == "draft"
    assert dependency_row_state(None, "") == "draft"


def test_a_workflow_status_that_differs_from_status_reads_the_same() -> None:
    """A row held at one status and stored at another reads one way.

    A plan can carry a workflow status beside the status its file records: a
    stored ``blocked`` whose work has since reopened is the live case. The
    roadmap reads the workflow status first, so the shared blocker assembly
    reads it from the same place — otherwise it would rank the plan by one
    status and report it by the other.
    """

    from reckon.roadmap import build_roadmap
    from reckon.serve import _derive_lifecycle

    row = {
        "slug": "moved",
        "title": "Moved",
        "type": "plan",
        "status": "blocked",
        "workflow_status": "active",
        "section_declarations": {"s1": "implementable"},
    }
    served_rows, _ = _derive_lifecycle(PROJECT, [dict(row)], [])
    served = served_rows[0]
    roadmap = {
        candidate["slug"]: candidate
        for bucket in ("ready_now", "blocked", "deferred")
        for candidate in build_roadmap(PROJECT, [dict(row)], []).get(bucket, [])
    }["moved"]

    assert served["workflow_status"] == "active", served["workflow_status"]
    assert roadmap["status"] == "active", roadmap["status"]
    assert _served_blockers(served) == _roadmap_blockers(roadmap), (
        f"served {_served_blockers(served)} vs roadmap {_roadmap_blockers(roadmap)}"
    )
    assert not _blocked(served), (
        f"served effective_status {served['effective_status']} "
        f"vs roadmap readiness {roadmap['readiness']}"
    )
    assert not _roadmap_blocked(roadmap), (
        f"roadmap readiness {roadmap['readiness']} "
        f"vs served effective_status {served['effective_status']}"
    )


def test_a_released_hold_reads_open_on_both_surfaces(tmp_path: Path) -> None:
    """A plan stored blocked whose cleared blocker is released reads the same.

    The released plan carries no blocker row, so the blocker assembly synthesizes
    a placeholder for a stored blocked status unless the caller says the plan was
    released. Both surfaces take the released set from the same roadmap read, so
    neither invents a wait the probe already ended.
    """

    served, roadmap = _served_and_roadmap(_project(tmp_path))

    assert served["released"]["status"] == "blocked"
    assert not _blocked(served["released"])
    assert not _roadmap_blocked(roadmap["released"])
    assert _served_blockers(served["released"]) == {}
    assert _roadmap_blockers(roadmap["released"]) == {}
