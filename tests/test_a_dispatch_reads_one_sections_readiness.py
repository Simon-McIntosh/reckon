"""A dispatch reads one section's readiness, not the project's.

Every dispatch used to check its own section's gates, decisions and waits by
building the whole project's roadmap: on a 67-plan project that was 45 seconds
per call, dominated by a resource scan of every plan and 57,435 stat calls. A
section's verdict is derived from its own plan document and the documents of
the plans its declared waits name, so a dispatch reads those and nothing else,
through the same verdict function the whole-project build uses.

These arms measure four things about that read. It touches no project-wide
resource scan. Its verdict equals the whole-project verdict for the same
section under every hold a fixture carries. The whole-project read itself
builds the roadmap once. And on a 60-plan project the bounded read costs less
than a tenth of the whole-project build.

A fifth arm covers the plan-review gate's store read: admission asks the gate
once per queued request, and the pass now reads each project's stored reviews
once rather than once per request.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import plan_review, queue_admission
from reckon.crew.node import TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
PLAN = fixtures.PLAN
UPSTREAM = "plan-upstream"
ELSEWHERE = "plan-elsewhere"


def _run(repository: Path, *arguments: str) -> None:
    fixtures._run(repository, *arguments)


def _plan_document(
    *,
    slug: str,
    declarations: dict[str, str],
    sections: int,
    waits: dict[str, list[str]] | None = None,
    gate: tuple[str, str] | None = None,
    decision: tuple[str, str] | None = None,
) -> str:
    """One plan document: its declarations, its waits, and its optional holds."""
    quote = '"'
    declared = json.dumps(declarations).replace(quote, "&quot;")
    awaited = json.dumps(waits or {}).replace(quote, "&quot;")
    headings = "".join(
        f"<h2 id={quote}s{index}{quote}>§{index}</h2>\n"
        f"<p>{'prose about the section and its measure. ' * 20}</p>\n"
        for index in range(1, sections + 1)
    )
    parts = [
        "<!doctype html>\n",
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n",
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n",
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n",
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n",
        f"<meta name={quote}plan-section-declarations{quote} content={quote}{declared}{quote}>\n",
        f"<meta name={quote}plan-section-depends-on{quote} content={quote}{awaited}{quote}>\n",
        headings,
    ]
    if gate is not None:
        gate_id, gated = gate
        # A plan carrying execution gates is dispatchable only while it carries
        # an open followup, so the gated fixture carries one.
        parts.append(
            '<section data-reckon="followups"><article class="r-fu" '
            'data-id="f-next" data-status="open">'
            '<h4 class="r-fu-title">carry the work</h4></article></section>\n'
        )
        parts.append(
            '<section data-reckon="gates"><div class="r-gate" '
            f'data-id="{gate_id}" data-status="open" data-verdict="" '
            f'data-gated-sections="{gated}">'
            '<h4 class="r-gate-measure">approve the release</h4></div></section>\n'
        )
    if decision is not None:
        decision_id, held = decision
        parts.append(
            '<section data-reckon="decisions"><article class="r-dec" '
            f'data-key="{decision_id}" data-status="open" data-sections="{held}">'
            '<h4 class="r-dec-question">which route?</h4></article></section>\n'
        )
    return "".join(parts)


def _write(repository: Path, slug: str, document: str) -> None:
    path = repository / "docs" / "plans" / f"{slug}.html"
    path.write_text(document, encoding="utf-8")
    _run(repository, "add", "--", str(path.relative_to(repository)))
    _run(repository, "commit", "-q", "-m", f"chore: author {slug}")


@pytest.fixture()
def held_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project carrying one section under each kind of hold.

    ``plan-a`` (the fixture's own plan) declares four implementable sections:
    one held by an open gate, one by an open decision, one waiting on a section
    of ``plan-upstream`` that has not landed, and one carrying no hold at all.
    """
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    _write(
        repository,
        UPSTREAM,
        _plan_document(
            slug=UPSTREAM,
            declarations={"s1": "implementable"},
            sections=1,
        ),
    )
    _write(
        repository,
        PLAN,
        _plan_document(
            slug=PLAN,
            declarations={
                "s1": "implementable",
                "s2": "implementable",
                "s3": "implementable",
                "s4": "implementable",
            },
            sections=4,
            waits={"s3": [f"{UPSTREAM}#s1"]},
            gate=("ship", "s1"),
            decision=("route", "s2"),
        ),
    )
    # A plan in the project that nothing this dispatch reads names: a
    # project-wide scan would resolve it, a bounded read never does.
    _write(
        repository,
        ELSEWHERE,
        _plan_document(
            slug=ELSEWHERE,
            declarations={"s1": "implementable"},
            sections=1,
        ),
    )
    return repository


def _bounded(repository: Path) -> dict[str, dict[str, Any]]:
    rows, _inventory = plan_review.section_readiness_rows(
        PROJECT, repository / "docs", PLAN
    )
    return rows


def _whole(repository: Path) -> dict[str, dict[str, Any]]:
    return plan_review.project_section_readiness(PROJECT, repository / "docs")


def test_the_read_touches_no_project_wide_resource_scan(
    held_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch's own-section check reads no plan beyond the ones it names."""
    from reckon import resources

    scanned: list[str] = []
    original = resources.resolve_resource

    def counting(*args: Any, **kwargs: Any) -> Any:
        scanned.append(str(args[2]) if len(args) > 2 else "?")
        return original(*args, **kwargs)

    monkeypatch.setattr(resources, "resolve_resource", counting)

    def refuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a dispatch built the whole project's roadmap")

    monkeypatch.setattr(plan_review, "project_section_readiness", refuse)

    node = TaskNode(
        id="n-ready",
        goal="build s4",
        plan=PLAN,
        section="s4",
        done_when="pytest reports passed for s4",
        write_paths=["s4.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    refs, _projects, unmet = queue_admission.dispatch_waits(
        node, repo_root=held_repo, project=PROJECT, live=[]
    )

    assert refs == [], "the fixture's ready section declares no wait of its own"
    assert unmet == []
    # The plan declares a wait on plan-upstream, so the bounded read reads that
    # plan too — and nothing else, while the project holds a third plan no
    # wait names.
    assert set(scanned) <= {PLAN, UPSTREAM}, scanned
    assert ELSEWHERE not in scanned, (
        "a plan no wait names was resolved: the read reached past what the "
        f"dispatch names; saw {scanned}"
    )
    assert {PLAN, UPSTREAM} <= set(scanned), (
        f"the read never resolved the plans it does name; saw {scanned}"
    )


def test_the_bounded_verdict_equals_the_whole_project_verdict(
    held_repo: Path,
) -> None:
    """Every hold kind reads the same through the bounded and the whole read."""
    bounded = _bounded(held_repo)
    whole = _whole(held_repo)

    assert set(bounded) >= {PLAN, UPSTREAM}
    sections = whole[PLAN]["sections"]
    assert set(sections) == {"s1", "s2", "s3", "s4"}
    for section, verdict in sections.items():
        assert bounded[PLAN]["sections"][section] == verdict, section
    # The fixture must actually exercise each hold, or the comparison above is
    # a comparison of four ready sections and proves nothing.
    assert sections["s1"]["ready"] is False, sections["s1"]
    assert sections["s2"]["ready"] is False, sections["s2"]
    assert sections["s3"]["ready"] is False, sections["s3"]
    assert sections["s4"]["ready"] is True, sections["s4"]
    kinds = {
        section: {blocker.get("kind") for blocker in verdict["blockers"]}
        for section, verdict in sections.items()
    }
    assert "gate" in kinds["s1"], kinds
    assert "decision" in kinds["s2"], kinds
    assert bounded[PLAN]["readiness"] == whole[PLAN]["readiness"]


def test_the_whole_project_read_builds_the_roadmap_once(
    held_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One project read is one roadmap build, not two."""
    from reckon import roadmap

    builds: list[int] = []
    original = roadmap.build_roadmap

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(roadmap, "build_roadmap", counting)

    rows = plan_review.project_section_readiness(PROJECT, held_repo / "docs")

    assert rows, "the fixture project must yield a readable roadmap"
    assert len(builds) == 1, builds


def _sixty_plan_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sixty plans, each with twenty implementable sections."""
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    documents = []
    for index in range(60):
        slug = "plan-a" if index == 0 else f"plan-{index:02d}"
        declarations = {f"s{s}": "implementable" for s in range(1, 201)}
        documents.append(
            (
                slug,
                _plan_document(
                    slug=slug,
                    declarations=declarations,
                    sections=200,
                    gate=("ship", "s1"),
                ),
            )
        )
    for slug, document in documents:
        path = repository / "docs" / "plans" / f"{slug}.html"
        path.write_text(document, encoding="utf-8")
    _run(repository, "add", "--", "docs/plans")
    _run(repository, "commit", "-q", "-m", "chore: author sixty plans")
    return repository


def _seconds(call: Any, repeats: int = 5) -> float:
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        best = min(best, time.perf_counter() - started)
    return best


def test_a_single_section_check_costs_a_tenth_of_the_whole_project_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sixty plans: the bounded read is under a tenth of the project build."""
    from reckon import metadata_index, serve

    repository = _sixty_plan_repo(tmp_path, monkeypatch)
    docs = repository / "docs"

    def cold() -> None:
        """What a dispatch process starts from: no index, no discovery cache."""
        metadata_index.clear()
        serve._DISC_CACHE.clear()

    def whole() -> None:
        cold()
        plan_review.project_section_readiness(PROJECT, docs)

    def bounded() -> None:
        cold()
        plan_review.section_readiness_rows(PROJECT, docs, PLAN)

    whole_seconds = _seconds(whole)
    bounded_seconds = _seconds(bounded)

    print(f"bounded {bounded_seconds:.4f}s, whole {whole_seconds:.4f}s")
    assert whole_seconds > 0
    assert bounded_seconds < whole_seconds / 10, (
        f"bounded {bounded_seconds:.4f}s vs whole {whole_seconds:.4f}s"
    )


# ── The plan-review gate's store read, once per pass ────────────────────────


def _seed_stored_review(config_home: Path, slug: str, review_run: str) -> None:
    """One stored plan review at the staging store admission reads."""
    directory = config_home / "crew" / "reviews" / PROJECT
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "review_run_id": review_run,
        "plan_slug": slug,
        "plan_version": 1,
        "rubric": "content",
        "findings": [],
        "timestamp": "2026-10-28T00:00:00Z",
    }
    (directory / f"plan-{slug}.v1.json").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def test_one_pass_reads_each_plans_stored_reviews_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three requests on one plan cost one store read, not three."""
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    config_home = tmp_path / "config"
    _seed_stored_review(config_home, PLAN, "r-review-1")

    loads: list[str] = []
    original = plan_review._load

    def counting(path: Path) -> Any:
        loads.append(str(path))
        return original(path)

    monkeypatch.setattr(plan_review, "_load", counting)

    # Three queued requests on one plan, as an admission pass judges them.
    with plan_review.review_read_pass():
        for _ in range(3):
            records = plan_review.list_plan_reviews(PROJECT)
            assert [record["review_run_id"] for record in records] == ["r-review-1"]

    assert len(loads) == 1, loads

    # Outside a pass the store is read again: the cache belongs to the pass.
    loads.clear()
    plan_review.list_plan_reviews(PROJECT)
    assert len(loads) == 1, loads
    assert repository.is_dir()
