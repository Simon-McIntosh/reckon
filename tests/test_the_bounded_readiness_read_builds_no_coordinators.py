"""The bounded readiness read builds no coordinator resolution.

A dispatch judges one section's readiness, and admission asks that question once
per queued request through the bounded per-plan read. The answer is a readiness
and a set of section verdicts; no coordinator is derived from it. Resolving a
plan's coordinator reads every run's ledger document through a deep copy, which
is the cost the bounded read exists to avoid, so the read builds its roadmap
with coordinator resolution off and enters that scope itself. Every caller of
the bounded read is then covered without each one having to remember the scope.

Three arms: the bounded read resolves no coordinator; its rows equal the base
revision's derivation for every plan and ref it reads; and an MCP roadmap read
outside the scope still names coordinators.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import mcp, roadmap, serve
from reckon.crew import plan_coordinator, plan_review
from tests import test_a_dispatch_reads_one_sections_readiness as documents
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
PLAN = "work-00"
UPSTREAM = "work-01"
REF = f"{PLAN}#s1"
NOW = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


def _live_runs() -> list[dict[str, Any]]:
    """Ten live runs, each on its own plan, away from the plans the read judges."""
    return [
        {
            "run_id": f"live-{index}",
            "project": PROJECT,
            "plan": f"work-{10 + index:02d}",
            "section": "s1",
            "session": f"session-{index % 3}",
            "backend": "local",
            "phase": "working",
            "dispatched_at": (NOW - timedelta(minutes=5)).isoformat(),
            "node": {"id": f"node-{index}", "role": "implement", "section": "s1"},
        }
        for index in range(10)
    ]


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project of twenty plans and ten live runs.

    ``work-00`` waits on ``work-01``'s only section, so the plans the bounded
    read of ``work-00`` judges carry an open critical path: its head is a ready,
    idle section, which is the one case that asks for a plan's coordinator.
    """
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    plans = repository / "docs" / "plans"
    for index in range(20):
        slug = f"work-{index:02d}"
        document = documents._plan_document(
            slug=slug,
            declarations={"s1": "implementable"},
            sections=1,
            waits={"s1": [f"{UPSTREAM}#s1"]} if index == 0 else None,
        )
        document = document.replace(
            '<h2 id="s1">§1</h2>\n',
            '<h2 id="s1">§1</h2>\n'
            '<section data-reckon="section" data-id="s1" '
            'data-effort-hours="1" data-capability-version="1.0" '
            'data-capability-class="general" data-capability-reasoning="standard" '
            'data-capability-verification="standard" data-capability-risk="low" '
            'data-status="implementable" data-links=""></section>\n',
        )
        document += (
            '<section data-reckon="followups"><article class="r-fu" '
            'data-id="f-next" data-status="open">'
            '<h4 class="r-fu-title">carry the work</h4></article></section>\n'
        )
        (plans / f"{slug}.html").write_text(document, encoding="utf-8")
    fixtures._run(repository, "add", "--", "docs/plans")
    fixtures._run(repository, "commit", "-q", "-m", "chore: seed project")
    serve._DISC_CACHE.clear()
    monkeypatch.setattr("reckon.crew.list_live", lambda *args, **kwargs: _live_runs())
    return repository


def _count_coordinators(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return {
            "plans": {
                f"work-{index:02d}": {"coordinator": "session-owner"}
                for index in range(20)
            }
        }

    monkeypatch.setattr(plan_coordinator, "resolve_plan_coordinators", counting)
    return calls


def test_the_bounded_read_resolves_no_coordinators(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bounded read answers a section's verdict without resolving a coordinator."""
    calls = _count_coordinators(monkeypatch)

    rows, inventory = plan_review.section_readiness_rows(
        PROJECT, project / "docs", PLAN, refs=[REF]
    )

    assert inventory, "the fixture must read at least the plan it was asked about"
    assert rows, rows
    assert calls == [], calls


def test_rows_equal_the_base_derivation(project: Path) -> None:
    """The bounded read's rows are the ones the unscoped build derives.

    The base revision built the roadmap outside any scope, so this derivation —
    one build with coordinator resolution on, over the same inventory — is the
    answer the read gave before the scope was entered.
    """
    rows, inventory = plan_review.section_readiness_rows(
        PROJECT, project / "docs", PLAN, refs=[REF]
    )

    expected = roadmap.section_verdicts_by_plan(
        roadmap.build_roadmap(
            PROJECT, list(inventory.values()), [], docs_dir=project / "docs"
        )
    )

    assert set(rows) == set(expected)
    for slug, verdict in expected.items():
        assert rows[slug]["readiness"] == verdict["readiness"], slug
        assert rows[slug]["sections"] == verdict["sections"], slug


def test_mcp_roadmap_outside_scope_keeps_coordinators(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-project roadmap read still names the plan's coordinator."""
    calls = _count_coordinators(monkeypatch)

    result = mcp._roadmap_scoped(PROJECT, checkout_path=str(project), view="summary")
    links = result["critical_path"]["links"]

    assert links, result["critical_path"]
    assert calls, "the fixture must reach coordinator resolution"
    assert any(link.get("coordinator") == "session-owner" for link in links), links
