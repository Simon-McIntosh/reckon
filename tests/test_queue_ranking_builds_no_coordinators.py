"""Ranking and readiness reuse discovery without resolving plan coordinators."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import mcp, roadmap, serve
from reckon.crew import plan_coordinator, plan_review, queue_order
from tests import test_a_dispatch_reads_one_sections_readiness as documents
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
NOW = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)


def _pointers() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queued = [
        {
            "run_id": f"queued-{index}",
            "project": PROJECT,
            "plan": f"work-{index:02d}",
            "section": "s1",
            "session": f"session-{index % 4}",
            "backend": "local",
            "phase": "queued",
            "queued_at": (NOW - timedelta(seconds=20 + index)).isoformat(),
            "node": {"role": "implement"},
        }
        for index in range(20)
    ]
    live = [
        {
            "run_id": f"live-{index}",
            "project": PROJECT,
            "plan": f"work-{index + 20:02d}",
            "section": "s1",
            "session": f"session-{index % 4}",
            "backend": "local",
            "phase": "working",
            "node": {"role": "implement"},
        }
        for index in range(15)
    ]
    return queued, live


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    plans = repository / "docs" / "plans"
    (plans / f"{fixtures.PLAN}.html").unlink()
    for index in range(40):
        slug = f"work-{index:02d}"
        roi = "low" if index == 0 else "mid"
        document = documents._plan_document(
            slug=slug,
            declarations={"s1": "implementable"},
            sections=1,
            waits={"s1": ["work-00#s1"]} if index == 1 else None,
        )
        document = document.replace(
            '<meta name="plan-status"',
            f'<meta name="plan-roi" content="{roi}">\n<meta name="plan-status"',
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
    queue_order._PRIORITY_KEYS.clear()
    _, live = _pointers()
    monkeypatch.setattr("reckon.crew.list_live", lambda *args, **kwargs: live)
    return repository


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    builds: list[int] = []
    original = roadmap.build_roadmap

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(roadmap, "build_roadmap", counting)
    return builds


def _count_coordinators(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return {
            "plans": {
                f"work-{index:02d}": {"coordinator": "session-owner"}
                for index in range(40)
            }
        }

    monkeypatch.setattr(plan_coordinator, "resolve_plan_coordinators", counting)
    return calls


def test_ranking_and_readiness_build_once_without_coordinators(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued, live = _pointers()
    calls = _count_coordinators(monkeypatch)
    builds = _count_builds(monkeypatch)
    assert queue_order.admission_order_details(queued, live, now=NOW)
    assert len(builds) == 1, builds
    assert calls == [], calls

    builds.clear()
    assert plan_review.project_section_readiness(PROJECT, project / "docs")
    assert len(builds) <= 1, builds
    assert calls == [], calls


def test_mcp_roadmap_outside_scope_keeps_coordinators(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_coordinators(monkeypatch)
    result = mcp._roadmap_scoped(PROJECT, checkout_path=str(project), view="summary")
    links = result["critical_path"]["links"]
    assert links, result["critical_path"]
    assert calls, "the fixture must reach coordinator resolution"
    assert any(link.get("coordinator") == "session-owner" for link in links)


def test_order_and_signals_match_the_recorded_base(
    project: Path,
) -> None:
    queued, live = _pointers()
    rows = queue_order.admission_order_details(queued, live, now=NOW)
    assert [(pointer["run_id"], reason) for pointer, reason in rows] == BASE_ORDER


def test_unchanged_discovery_reuses_priority_keys(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued, live = _pointers()
    builds = _count_builds(monkeypatch)
    queue_order.admission_order_details(queued, live, now=NOW)
    builds.clear()
    queue_order.admission_order_details(queued, live, now=NOW)
    assert builds == [], builds


def test_plan_edit_changes_priority_keys(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued, _ = _pointers()
    builds = _count_builds(monkeypatch)
    before = queue_order._roadmap_priority_keys(queued)
    path = project / "docs" / "plans" / "work-00.html"
    path.write_text(
        path.read_text(encoding="utf-8").replace('content="low"', 'content="high"'),
        encoding="utf-8",
    )
    after = queue_order._roadmap_priority_keys(queued)
    assert len(builds) == 2, builds
    assert before[id(queued[0])] != after[id(queued[0])]


BASE_ORDER = [
    ("queued-0", "critical path"),
    *((f"queued-{index}", "ready") for index in range(2, 19)),
    ("queued-19", "roadmap priority"),
    ("queued-1", "queued at (roadmap priority unavailable)"),
]
