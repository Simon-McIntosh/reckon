"""Ordering never re-opens a review: a plan is reviewed for what it says.

The document unit of a plan review digests the contract a node builds against —
the summary, the decisions and the gates. A plan's dependency wiring and its
effort figures are not that contract: they say when work may run and how long it
should take, so a coordinator re-ordering a queue, or a landing that moves a
section's declaration, must leave every unit the review covered still covered.
These cases hold that boundary, and the two that must still bite: a decision or
a gate measure is the contract, so changing either re-opens the document unit,
and a section's prose is the section's own contract, so editing it re-opens that
section alone.
"""

from __future__ import annotations

import re

import pytest

from reckon import _plan_html
from reckon.crew import plan_review

_CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}

_AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="docs-project" content="sample">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-slug" content="fixture">'
    '<meta name="plan-title" content="Fixture">'
    '<meta name="plan-status" content="active">'
    '<meta name="plan-version" content="3">'
    '<meta name="plan-summary" content="Build the fixture.">'
    '</head><body><main class="plan-doc">'
    '<h2 id="a">&sect;1 &mdash; Alpha</h2><p>Build alpha.</p>'
    '<h2 id="b">&sect;2 &mdash; Beta</h2><p>Build beta.</p>'
    '<h2 id="c">&sect;3 &mdash; Gamma</h2><p>Build gamma.</p>'
    "</main></body></html>"
)

_SECTIONS = ("a", "b", "c")


def _section_records(status: str = "implementable") -> list[dict]:
    return [
        {
            "id": section,
            "effort_hours": 1.0,
            "status": status,
            "capability": dict(_CAPABILITY),
            "attempts": 0,
            "links": [],
        }
        for section in _SECTIONS
    ]


def _decisions(choice: str = "shared") -> dict:
    return {
        "owner": {
            "title": "Which owner?",
            "context": "",
            "choices": ["shared", "own"],
            "option_labels": {},
            "choice": choice,
            "recommended": "",
            "recommended_by": "",
            "rationale": "Reuse the owner.",
            "when": "",
            "by": "",
            "sections": [],
        }
    }


def _gates(measure: str = "alpha answers under 2 s") -> list[dict]:
    return [
        {
            "id": "g1",
            "section": "a",
            "gated_sections": ["a"],
            "status": "open",
            "measure": measure,
            "required_evidence": "a log line",
            "verdict": "",
            "evidence": "",
        }
    ]


def _state(**overrides) -> dict:
    state = {
        "project": "sample",
        "type": "plan",
        "slug": "fixture",
        "title": "Fixture",
        "status": "active",
        "modified": "2026-10-28",
        "version": 3,
        "summary": "Build the fixture.",
        "section_declarations": dict.fromkeys(_SECTIONS, "implementable"),
        "sections": _section_records(),
        "decisions": _decisions(),
        "gates": _gates(),
    }
    state.update(overrides)
    return state


def _plan(**overrides) -> str:
    return _plan_html.write_state(_AUTHORED, _state(**overrides))


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return {
        "base_dir": tmp_path / "store",
        "project": "sample",
        "slug": "fixture",
        "run": "review-write-time",
    }


def _store_review(env, document: str) -> dict:
    run_id = env["run"]
    record = {
        "project": env["project"],
        "plan_slug": env["slug"],
        "plan_version": 3,
        "reviewed_blob_sha": "a" * 40,
        "plan_fingerprint": plan_review.plan_fingerprint(document),
        "section_digests": plan_review._section_digests(document),
        "findings": [],
        "responses": {},
        "status": "ready",
        "review_run_id": run_id,
    }
    plan_review.store_plan_review(record, base_dir=env["base_dir"])
    directory = plan_review.review_report_directory(env["project"], env["slug"], run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / plan_review._REVIEW_SNAPSHOT_NAME).write_text(
        document, encoding="utf-8"
    )
    return record


def _coverage(env, document: str):
    records, uncovered, _changes = plan_review.review_coverage(
        env["project"], env["slug"], plan=document, base_dir=env["base_dir"]
    )
    return records, uncovered


def _write_meta(html: str, name: str, value: str) -> str:
    pattern = re.compile(f'<meta name="{re.escape(name)}" content="[^"]*"')
    if pattern.search(html):
        return pattern.sub(f'<meta name="{name}" content="{value}"', html, count=1)
    return html.replace(
        "</head>", f'<meta name="{name}" content="{value}">\n</head>', 1
    )


# ── Ordering and bookkeeping leave every unit covered ───────────────────────


def test_section_waits_leave_every_unit_covered(store) -> None:
    base = _plan()
    _store_review(store, base)

    # The section-weight mapping is a head meta the state parser does not read,
    # so this asserts the document stays whole where the reader carries it.
    waited = _write_meta(
        base,
        "plan-section-depends-on",
        '{"c": ["reckon:other-plan#s1"]}',
    )
    assert waited != base
    assert plan_review._section_digests(waited) == plan_review._section_digests(base)
    records, uncovered = _coverage(store, waited)
    assert uncovered == set()
    assert records

    # A caller that hands in already-parsed state — a roadmap row does — carries
    # the mapping on the state itself, so the exclusion is what keeps it out.
    state = _plan_html.read_state(base)
    earlier = dict(state, section_depends_on={"c": ["reckon:other-plan#s1"]})
    later = dict(state, section_depends_on={"c": ["reckon:other-plan#s2"]})
    assert plan_review.plan_fingerprint(earlier) == plan_review.plan_fingerprint(later)
    assert plan_review._digest_state(earlier) == plan_review._digest_state(later)


def test_plan_dependencies_leave_every_unit_covered(store) -> None:
    base = _plan()
    _store_review(store, base)
    for name, value in (
        ("plan-depends-on", "some-earlier-plan"),
        ("plan-after", "some-earlier-plan"),
        ("plan-blocks", "some-later-plan"),
        ("plan-informs", "some-other-plan"),
    ):
        edited = _write_meta(base, name, value)
        assert edited != base, f"{name} was already {value!r}"
        assert plan_review._section_digests(edited) == plan_review._section_digests(
            base
        )
        records, uncovered = _coverage(store, edited)
        assert uncovered == set(), f"{name} uncovered {sorted(uncovered)}"
        assert records


def test_effort_figures_leave_every_unit_covered(store) -> None:
    base = _plan()
    _store_review(store, base)

    # The plan's own effort, as the named worker-hours and as the legacy letter.
    for name, value in (("plan-effort-hours", "9.5"), ("plan-effort", "L")):
        edited = _write_meta(base, name, value)
        assert edited != base, f"{name} was already {value!r}"
        records, uncovered = _coverage(store, edited)
        assert uncovered == set(), f"{name} uncovered {sorted(uncovered)}"
        assert records

    # One section's own estimate, which a coordinator re-estimating a queue
    # edits beside the plan total.
    re_estimated = _plan(
        sections=[
            dict(record, effort_hours=7.0) if record["id"] == "b" else record
            for record in _section_records()
        ]
    )
    assert re_estimated != base
    records, uncovered = _coverage(store, re_estimated)
    assert uncovered == set()
    assert records


def test_a_section_declaration_change_leaves_every_remaining_unit_covered(
    store,
) -> None:
    base = _plan()
    _store_review(store, base)

    # A landing collapses section b: its declaration and its record's own status
    # move in the same write that replaces its body.
    landed = _plan(
        section_declarations={"a": "implementable", "b": "done", "c": "implementable"},
        sections=[
            dict(record, status="done") if record["id"] == "b" else record
            for record in _section_records()
        ],
    )
    assert landed != base
    records, uncovered = _coverage(store, landed)
    assert uncovered == set()
    assert records


# ── The contract still earns a review ───────────────────────────────────────


def test_a_decision_change_uncovers_the_document(store) -> None:
    base = _plan()
    _store_review(store, base)
    changed = _plan(decisions=_decisions(choice="own"))
    assert changed != base
    records, uncovered = _coverage(store, changed)
    assert uncovered == {"_document"}
    assert records


def test_a_gate_measure_change_uncovers_the_document(store) -> None:
    base = _plan()
    _store_review(store, base)
    changed = _plan(gates=_gates(measure="alpha answers under 200 ms"))
    assert changed != base
    records, uncovered = _coverage(store, changed)
    assert uncovered == {"_document"}
    assert records


def test_a_section_prose_edit_uncovers_that_section(store) -> None:
    base = _plan()
    _store_review(store, base)
    edited = base.replace(
        "Build beta.", "Build beta against the re-ordered queue instead."
    )
    assert edited != base
    records, uncovered = _coverage(store, edited)
    assert uncovered == {"b"}
    assert records


def test_the_ordering_exclusion_names_no_section_contract() -> None:
    # The exclusion is a closed set of ordering and bookkeeping keys. A
    # decision, a gate, the summary or a section's prose left the exclusion the
    # day it named one, and this holds that boundary against a later widening.
    assert set(plan_review.PLAN_ORDERING_KEYS) == {
        "depends_on",
        "blocks",
        "after",
        "informs",
        "section_depends_on",
        "effort",
        "wall_clock_hours",
    }
    for key in ("decisions", "gates", "summary", "sections"):
        assert key not in plan_review.PLAN_ORDERING_KEYS
