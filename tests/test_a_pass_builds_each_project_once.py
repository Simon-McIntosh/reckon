"""A waiting-request pass shares one project readiness read."""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from reckon.crew import plan_review, queue_admission, runs
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

UPSTREAM_STATES = (
    "done",
    "implementable",
    "blocked",
    "done",
    "implementable",
    "blocked",
    "done",
    "implementable",
)
PHASES = {
    "done": queue_admission.QUEUED_PHASE,
    "implementable": queue_admission.AWAITING_UPSTREAM_PHASE,
    "blocked": queue_admission.BLOCKED_UPSTREAM_PHASE,
}


def _attribute(value: object) -> str:
    return json.dumps(value).replace('"', "&quot;")


def _plan(slug: str, declarations: dict[str, str], waits: dict[str, list[str]]) -> str:
    headings = "".join(f'<h2 id="{section}">Work</h2>' for section in declarations)
    return (
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-section-declarations" content="{_attribute(declarations)}">'
        f'<meta name="plan-section-depends-on" content="{_attribute(waits)}">'
        f"</head><body>{headings}</body></html>"
    )


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    plans = repository / "docs" / "plans"
    (plans / "plan-a.html").unlink()
    for index, state in enumerate(UPSTREAM_STATES):
        slug = f"upstream-{index}"
        (plans / f"{slug}.html").write_text(
            _plan(slug, {"s1": state}, {}), encoding="utf-8"
        )
    for index in range(4):
        slug = f"work-{index}"
        declarations = {f"s{section + 1}": "implementable" for section in range(5)}
        waits = {
            f"s{section + 1}": [f"upstream-{(index * 5 + section) % 8}#s1"]
            for section in range(5)
        }
        (plans / f"{slug}.html").write_text(
            _plan(slug, declarations, waits), encoding="utf-8"
        )
    return repository


def _requests(repository: Path) -> list[dict]:
    requests = [
        {
            "run_id": f"r-{index:02d}",
            "project": fixtures.PROJECT,
            "repo": str(repository),
            "phase": queue_admission.AWAITING_UPSTREAM_PHASE,
            "node": {
                "id": f"work-{index:02d}",
                "plan": f"work-{index // 5}",
                "section": f"s{index % 5 + 1}",
            },
            "waits": [f"upstream-{index % 8}#s1"],
        }
        for index in range(20)
    ]
    for request in requests:
        queue_admission._write_pointer(request["run_id"], request)
    return requests


def _pass(
    monkeypatch: pytest.MonkeyPatch,
    requests: list[dict],
    *,
    shared: bool = True,
    project_wide: bool = True,
) -> tuple[int, list[str]]:
    from reckon import mcp_discovery

    builds = 0
    real = plan_review.project_section_readiness

    def counting(project: str, docs_dir: Path) -> dict:
        nonlocal builds
        builds += 1
        return real(project, docs_dir)

    with monkeypatch.context() as patch:
        patch.setattr(plan_review, "project_section_readiness", counting)
        patch.setattr(
            mcp_discovery,
            "_discover_project",
            lambda *_: pytest.fail("admission entered project discovery"),
        )
        with (
            queue_admission.readiness_pass() if shared else nullcontext(),
            queue_admission.project_read() if project_wide else nullcontext(),
        ):
            queue_admission.advance_waiting_requests(requests)
    phases = [runs.read_pointer(request["run_id"])["phase"] for request in requests]
    assert phases == [request["phase"] for request in requests]
    return builds, phases


@pytest.mark.parametrize("project_wide", [True, False])
def test_waiting_requests_read_named_plans(
    project: Path, monkeypatch: pytest.MonkeyPatch, project_wide: bool
) -> None:
    requests = _requests(project)
    assert len(list((project / "docs" / "plans").glob("*.html"))) == 12
    assert len(requests) == 20
    assert len({ref for request in requests for ref in request["waits"]}) == 8
    builds, phases = _pass(monkeypatch, requests, project_wide=project_wide)
    assert phases == [PHASES[UPSTREAM_STATES[index % 8]] for index in range(20)]
    assert set(phases) == set(PHASES.values())
    assert builds == 0


def test_phases_match_the_request_scoped_reads(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project scope outside admission still derives the same request phases.
    reference_builds, reference_phases = _pass(
        monkeypatch, _requests(project), shared=False
    )
    _, phases = _pass(monkeypatch, _requests(project))
    assert reference_builds == 0
    assert phases == reference_phases


def test_plan_edit_is_seen_by_next_pass(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _requests(project)
    _, before = _pass(monkeypatch, requests)
    path = project / "docs" / "plans" / "upstream-1.html"
    path.write_text(_plan("upstream-1", {"s1": "done"}, {}), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    _, after = _pass(monkeypatch, requests)
    for index in range(20):
        if index % 8 == 1:
            assert before[index] == queue_admission.AWAITING_UPSTREAM_PHASE
            assert after[index] == queue_admission.QUEUED_PHASE
        else:
            assert after[index] == before[index]


def test_bounded_reads_keep_plan_and_refs_in_the_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = []

    def bounded(project_name, docs_dir, plan, *, refs):
        observed.append((plan, tuple(refs)))
        return {plan: {"sections": {}}}, {}

    monkeypatch.setattr(plan_review, "section_readiness_rows", bounded)
    cache = {}
    for plan, refs in (
        ("work-0", ("upstream-0#s1",)),
        ("work-1", ("upstream-0#s1",)),
        ("work-0", ("upstream-1#s1",)),
    ):
        queue_admission._readiness_rows(
            fixtures.PROJECT, project / "docs", cache, plan, refs
        )
        queue_admission._readiness_rows(
            fixtures.PROJECT, project / "docs", cache, plan, refs
        )
    assert len(observed) == 3
