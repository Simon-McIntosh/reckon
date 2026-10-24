"""The MCP plan summary, the served view and the roadmap answer one blocker set.

A plan's blocked state is one fact with three readers: the resource summary an
agent reads, the served plan row the SPA renders, and the roadmap a coordinator
plans from. The summary derived its own list from explicit blockers, unresolved
dependencies and unpassed gates, so a plan held by a plan-level open decision or
by a held blocker read open there while the other two surfaces reported it
blocked. The summary now takes its list and its effective status from the
roadmap's own assembly, and every case here compares the surfaces against each
other rather than asserting a literal about any one of them.

The last part is the audit's orphan rule. A plan stored blocked with nothing
recorded to explain it is reported once, in the lifecycle category, and a plan
whose cleared hold a probe released is not reported at all — a second copy of
the rule in the MCP audit made that plan appear twice, once in each category.

Every home read here is a temporary path: ``RECKON_HOME`` covers the
configuration home, the crew home and the cache, while ``RECKON_MOUNTS_PATH``
and ``RECKON_STATE_ROOT`` name the mounts table and the state root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
import reckon.serve as serve_module
from reckon import project_state
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

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


def _project(docs: Path) -> None:
    """Seven plans, one per hold, plus a stored blocked status with no hold.

    ``deciding`` carries an unscoped open decision, ``held`` a held blocker,
    ``gated`` an unpassed execution gate over its only implementable section,
    ``explicit`` a plain blocker, and ``dependent`` an unresolved dependency.
    ``released`` is stored blocked with a hold a probe already cleared, and
    ``free`` carries nothing; ``unrecorded`` is stored blocked with no blocker
    row at all.
    """

    project_state.create_project_state(docs, PROJECT)
    _write_plan(docs, "deciding", {"decisions": {"transport": {"choice": ""}}})
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
    _write_plan(docs, "held", {})
    _write_plan(docs, "explicit", {})
    _write_plan(docs, "dependent", {"depends_on": ["free"]})
    _write_plan(docs, "released", {"status": "blocked"})
    _write_plan(docs, "free", {})
    _write_plan(docs, "unrecorded", {"status": "blocked"})

    for blocker_id, kind in (("awaiting-outcome", "held"), ("access", "explicit")):
        project_state.write_resource(
            docs,
            PROJECT,
            "blocker",
            blocker_id,
            {"kind": kind, "summary": f"Waiting on {blocker_id}"},
            0,
            create=True,
        )
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
            "items": [
                {"slug": "held", "blocked_by": ["awaiting-outcome"]},
                {"slug": "explicit", "blocked_by": ["access"]},
                {"slug": "released", "blocked_by": ["waiting"]},
            ],
        },
        0,
        create=True,
    )
    subject = docs / "research" / "outcome.html"
    subject.parent.mkdir(parents=True, exist_ok=True)
    subject.write_text("arrived", encoding="utf-8")
    report = project_state.evaluate_held_blocker(docs, PROJECT, "waiting")
    assert report["status"] == "cleared", report


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """A project whose every environment-resolved home is a temporary path."""

    docs = tmp_path / "docs"
    docs.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")

    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    monkeypatch.setattr(serve_module, "_MOUNTS_FILE", mounts_file, raising=False)
    monkeypatch.setattr(serve_module, "_STATE_ROOT", state_root, raising=False)
    reload_mcp_family()

    _project(docs)
    return docs, state_root


def _mcp_summary(slug: str) -> dict:
    return mcp_module._read_plan(
        resource={"project": PROJECT, "type": "plan", "id": slug}
    )


def _served_and_roadmap(docs: Path, state_root: Path) -> tuple[dict, dict]:
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
    roadmap = {
        row["slug"]: row
        for bucket in ("ready_now", "blocked", "deferred")
        for row in report.get(bucket, [])
    }
    return served, roadmap


def _blockers_by_kind(rows) -> dict[str, set[str]]:
    """A published blocker list grouped by kind and identity.

    The summary publishes an explicit blocker as its bare id and a resolved
    dependency as a row, so both shapes are read here; every other kind arrives
    as a named row.
    """

    grouped: dict[str, set[str]] = {}
    for row in rows or []:
        if isinstance(row, str):
            grouped.setdefault("explicit", set()).add(row)
            continue
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "dependency")
        ident = str(row.get("id") or row.get("ref") or "")
        grouped.setdefault(kind, set()).add(ident)
    return grouped


def _roadmap_blockers(row: dict) -> dict[str, set[str]]:
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
    dependencies = {
        str(dep.get("ref") or "")
        for dep in row.get("depends_on") or []
        if not dep.get("satisfied")
    }
    if dependencies:
        grouped["dependency"] = dependencies
    return grouped


def _blocked(status: str) -> bool:
    return status == "blocked"


HELD_KINDS = {
    "deciding": {"decision": {"transport"}},
    "held": {"held": {"awaiting-outcome"}},
    "gated": {"gate": {"s1-evidence"}},
    "explicit": {"explicit": {"access"}},
    "dependent": {"dependency": {"free"}},
}


def test_the_three_surfaces_agree_on_every_held_plan(homes) -> None:
    docs, state_root = homes
    served, roadmap = _served_and_roadmap(docs, state_root)

    for slug in sorted(HELD_KINDS):
        summary = _mcp_summary(slug)
        assert summary.get("error") is None, summary
        mcp_blocked = _blocked(summary["state"]["effective_status"])
        served_blocked = _blocked(served[slug]["effective_status"])
        roadmap_blocked = _blocked(roadmap[slug]["effective_status"])

        assert mcp_blocked == served_blocked == roadmap_blocked is True, (
            f"{slug}: summary {summary['state']['effective_status']}, served "
            f"{served[slug]['effective_status']}, roadmap "
            f"{roadmap[slug]['effective_status']}"
        )
        mcp_kinds = _blockers_by_kind(summary["blocking"])
        assert (
            mcp_kinds
            == _blockers_by_kind(served[slug]["blocking"])
            == (_roadmap_blockers(roadmap[slug]))
            == HELD_KINDS[slug]
        ), (
            f"{slug}: summary {mcp_kinds}, served "
            f"{_blockers_by_kind(served[slug]['blocking'])}, roadmap "
            f"{_roadmap_blockers(roadmap[slug])}"
        )


def test_a_released_and_a_free_plan_read_open_everywhere(homes) -> None:
    docs, state_root = homes
    served, roadmap = _served_and_roadmap(docs, state_root)

    for slug in ("released", "free"):
        summary = _mcp_summary(slug)
        assert not _blocked(summary["state"]["effective_status"]), summary["state"]
        assert not _blocked(served[slug]["effective_status"]), served[slug]
        assert not _blocked(roadmap[slug]["effective_status"]), roadmap[slug]
        assert summary["blocking"] == [], summary["blocking"]
        assert _blockers_by_kind(served[slug]["blocking"]) == {}
        assert _roadmap_blockers(roadmap[slug]) == {}
    # The released plan carries no hold to count, while the free plan never had
    # one: neither surface reports a blocker for either.
    assert served["released"]["blockers"] == 0, served["released"]
    assert served["free"]["blockers"] == 0, served["free"]


def test_a_stored_blocked_status_with_no_hold_reads_blocked_everywhere(homes) -> None:
    docs, state_root = homes
    served, roadmap = _served_and_roadmap(docs, state_root)

    summary = _mcp_summary("unrecorded")
    assert _blocked(summary["state"]["effective_status"]), summary["state"]
    assert summary["blocking"] == [{"kind": "persisted", "id": "unrecorded"}], summary[
        "blocking"
    ]
    assert _blocked(served["unrecorded"]["effective_status"]), served["unrecorded"]
    assert served["unrecorded"]["blockers"] >= 1, served["unrecorded"]
    # The served row publishes the recorded rows, so the count carries the
    # placeholder while the list the audit reads does not.
    assert served["unrecorded"]["blocking"] == [], served["unrecorded"]
    assert _blocked(roadmap["unrecorded"]["effective_status"]), roadmap["unrecorded"]


def test_the_audit_reports_an_orphaned_status_once_and_not_a_released_plan(
    homes,
) -> None:
    result = mcp_module._audit(PROJECT)

    orphans = [
        finding
        for finding in result["findings"]
        if finding.get("code") == "orphaned-blocked-status"
    ]
    assert [(row.get("slug"), row.get("category")) for row in orphans] == [
        ("unrecorded", "lifecycle")
    ], orphans


def test_the_summary_falls_back_to_its_own_assembly_without_the_key(homes) -> None:
    """A direct view call carries no assembled key, and keeps its old answer."""

    summary = mcp_module._read_plan(
        resource={"project": PROJECT, "type": "plan", "id": "deciding"}
    )
    # The assembled path is what the fixture's summary takes; the same plan
    # viewed without an inventory row must still answer from the summary's own
    # assembly rather than failing.
    from reckon.mcp_views import ResourceSelector, resource_view

    selector = ResourceSelector(PROJECT, "plan", "deciding")
    direct = resource_view(
        selector,
        1,
        {"title": "Deciding", "status": "active"},
        view="summary",
        deps=[],
        provenance={
            "checkout": "/synthetic/test-checkout",
            "branch": "main",
            "content_digest": "sha256:" + "0" * 64,
        },
    )
    assert direct["blocking"] == [], direct["blocking"]
    assert direct["state"]["effective_status"] == "active", direct["state"]
    assert _blocked(summary["state"]["effective_status"]), summary["state"]
