"""A plan's review coverage opens only that plan's stored reviews.

The coverage predicate runs behind every text-mode plan edit and every
admission pass, so a walk of the project's whole review store turns one edit
into a read of thousands of files. It asks about a single plan, and the store
is laid out by plan, so the read can be scoped to that plan's committed
directory and its staging prefix.

These cases hold the scoped read to three things: it opens only the records of
the plan asked about, it finds the same records and reaches the same coverage
a full read filtered to the slug reaches, and a slug that prefixes another
plan's slug reads none of the longer plan's records.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import plan_review as module

PROJECT = "review-coverage-one-plan"
PLANS = ("a", "a-b", *(f"p{index:02d}" for index in range(1, 19)))
COMMITTED_PER_PLAN = 5
STAGED_PER_PLAN = 5

_SECTION = (
    '<h2 id="s1">One</h2>'
    '<section data-reckon="section" data-id="s1" data-effort-hours="0.5" '
    'data-capability-version="1.0" data-capability-class="general" '
    'data-capability-reasoning="standard" data-capability-verification="strict" '
    'data-capability-risk="moderate" data-status="implementable" '
    'data-links=""></section>'
)


def _document(slug: str) -> str:
    """A one-section plan whose authored prose is unique to ``slug``."""
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="1">'
        '<meta name="plan-section-declarations" content=\'{"s1": "implementable"}\'>'
        "</head><body>"
        f"{_SECTION}"
        f"<p><strong>Why.</strong> The {slug} plan's authored prose.</p>"
        "</body></html>"
    )


def _record(slug: str, index: int, digests: dict[str, str]) -> dict:
    """One stored review of ``slug``: its section covered, its document not.

    Only the section digest is supplied, so the document unit stays uncovered
    and the coverage verdict is more than "everything is covered".
    """
    return {
        "project": PROJECT,
        "plan_slug": slug,
        "plan_version": index + 1,
        "rubric": "plan_review",
        "review_run_id": f"r-{slug}-{index:02d}",
        "status": "ready",
        "plan_fingerprint": "a fingerprint no plan content produces",
        "section_digests": {"s1": digests["s1"]},
        "findings": [],
        "responses": {},
    }


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A staging store and a committed tree holding 200 records over 20 plans.

    Every crew and project path is environment-resolved under ``tmp_path``, so
    nothing here reads or writes the operator's own store.
    """
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    # An in-process caller's config: the thresholds fall back to their shipped
    # defaults, and no host config is consulted.
    monkeypatch.setattr(flight, "resolve", lambda **kwargs: SimpleNamespace(config={}))

    staging = tmp_path / "staging"
    committed = repo / "docs" / "state" / PROJECT / "reviews" / "plan"
    for slug in PLANS:
        digests = module._section_digests(_document(slug))
        for index in range(COMMITTED_PER_PLAN):
            path = committed / slug / f"r-{slug}-{index:02d}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_record(slug, index, digests)), encoding="utf-8")
        for index in range(STAGED_PER_PLAN):
            version = COMMITTED_PER_PLAN + index
            path = staging / PROJECT / f"plan-{slug}.v{version}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(_record(slug, version, digests)), encoding="utf-8"
            )
    return staging, committed


def _opened_records(monkeypatch: pytest.MonkeyPatch, call):
    """Run ``call`` with every stored-record read recorded."""
    opened: list[Path] = []
    real = module._read_record

    def spy(path: Path):
        opened.append(Path(path))
        return real(path)

    monkeypatch.setattr(module, "_read_record", spy)
    return opened, call()


def _paths_of_slug(staging: Path, committed: Path, slug: str) -> set[Path]:
    """The files a full store read filtered to ``slug`` reads, built here."""
    return {
        *(committed / slug).glob("*.json"),
        *(staging / PROJECT).glob(f"plan-{slug}.v*.json"),
    }


def _own_coverage(document: str, records: list[dict]) -> tuple[set[str], set[str]]:
    """The covered and uncovered units of a full, slug-filtered read.

    Computed here from the records and the plan's own digests rather than
    through the listing under test: a unit is covered when some record of the
    plan carries the digest the plan has now.
    """
    digests = module._section_digests(document)
    outstanding = {module.DOCUMENT_UNIT, "s1"}
    covered = {
        unit
        for unit in outstanding
        if unit in digests
        and any(
            record["section_digests"].get(unit) == digests[unit] for record in records
        )
    }
    return covered, outstanding - covered


def test_coverage_opens_only_the_plans_own_records(
    store: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    staging, committed = store
    opened, (contributing, uncovered, _changes) = _opened_records(
        monkeypatch,
        lambda: module.review_coverage(
            PROJECT, "a", plan=_document("a"), base_dir=staging
        ),
    )

    expected = _paths_of_slug(staging, committed, "a")
    assert len(expected) == COMMITTED_PER_PLAN + STAGED_PER_PLAN
    assert {path.resolve() for path in opened} == {path.resolve() for path in expected}
    assert {record["review_path"] for record in contributing} == {
        str(path) for path in expected
    }
    assert uncovered == {module.DOCUMENT_UNIT}


def test_scoped_read_reaches_the_coverage_a_full_read_reaches(
    store: tuple[Path, Path],
) -> None:
    staging, committed = store
    document = _document("a")
    contributing, uncovered, _changes = module.review_coverage(
        PROJECT, "a", plan=document, base_dir=staging
    )

    # Both trees are read: the scoped read does not answer from the committed
    # archive alone, nor from staging alone while a review sits unpromoted.
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_paths_of_slug(staging, committed, "a"))
    ]
    assert len(records) == COMMITTED_PER_PLAN + STAGED_PER_PLAN
    assert {record["review_path"] for record in contributing} == {
        str(path) for path in _paths_of_slug(staging, committed, "a")
    }
    _covered, expected_uncovered = _own_coverage(document, records)
    assert uncovered == expected_uncovered
    assert uncovered == {module.DOCUMENT_UNIT}


def test_a_prefix_slug_reads_none_of_the_longer_plans_records(
    store: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    staging, committed = store
    opened, (contributing, _uncovered, _changes) = _opened_records(
        monkeypatch,
        lambda: module.review_coverage(
            PROJECT, "a", plan=_document("a"), base_dir=staging
        ),
    )

    longer = _paths_of_slug(staging, committed, "a-b")
    assert longer, "the fixture must hold records for the longer plan"
    assert not (
        {path.resolve() for path in opened} & {path.resolve() for path in longer}
    )
    assert {record.get("plan_slug") for record in contributing} == {"a"}
