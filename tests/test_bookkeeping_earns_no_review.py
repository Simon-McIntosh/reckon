"""Bookkeeping earns no review: comments, followups and derived scalars stay out.

The document unit of a plan review digests the design a worker builds from.
Landing comments, followups and the derived ``impl_source`` scalar record work
done and next steps rather than that design, so folding them in re-armed a
review of the whole plan on every landing beat. These cases hold the boundary:
an authored comment or a followup leaves every unit covered, a decision edit
does not, setting an impl and flipping it between authored and computed leaves
coverage whole, the metadata-scalar sweep over every live plan moves only the
excluded keys, and a stored review's own snapshot — not its stored digests —
decides its coverage, so a digest definition change re-reviews nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIVE_PLANS = _REPO_ROOT / "docs" / "plans"

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
    '</head><body><main class="plan-doc">'
    '<h2 id="a">&sect;1 &mdash; Alpha</h2><p>Build alpha.</p>'
    '<h2 id="b">&sect;2 &mdash; Beta</h2><p>Build beta.</p>'
    "</main></body></html>"
)


def _sections(status: str = "implementable") -> list[dict]:
    return [
        {
            "id": section,
            "effort_hours": 1.0,
            "status": status,
            "capability": dict(_CAPABILITY),
            "attempts": 0,
            "links": [],
        }
        for section in ("a", "b")
    ]


def _state(**overrides) -> dict:
    state = {
        "project": "sample",
        "type": "plan",
        "slug": "fixture",
        "title": "Fixture",
        "status": "active",
        "modified": "2026-10-06",
        "version": 3,
        "section_declarations": {"a": "implementable", "b": "implementable"},
        "sections": _sections(),
        "decisions": {
            "choice": {
                "title": "Which owner?",
                "context": "",
                "choices": ["shared", "own"],
                "option_labels": {},
                "choice": "shared",
                "recommended": "",
                "recommended_by": "",
                "rationale": "Reuse the owner.",
                "when": "",
                "by": "",
                "sections": [],
            }
        },
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
        "run": "review-bookkeeping",
    }


def _store_review(
    env,
    document: str,
    *,
    digests: dict | None = None,
    fingerprint: str | None = None,
    run: str | None = None,
) -> dict:
    run_id = run or env["run"]
    record = {
        "project": env["project"],
        "plan_slug": env["slug"],
        "plan_version": 3,
        "reviewed_blob_sha": "a" * 40,
        "plan_fingerprint": fingerprint or plan_review.plan_fingerprint(document),
        "section_digests": (
            digests if digests is not None else plan_review._section_digests(document)
        ),
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
    return plan_review._review_coverage(
        env["project"], env["slug"], plan=document, base_dir=env["base_dir"]
    )


def _snapshot_path(env, run: str | None = None) -> Path:
    return (
        plan_review.review_report_directory(
            env["project"], env["slug"], run or env["run"]
        )
        / plan_review._REVIEW_SNAPSHOT_NAME
    )


# ── Comments, followups and decisions ───────────────────────────────────────


def test_an_authored_comment_leaves_every_unit_covered(store) -> None:
    base = _plan()
    _store_review(store, base)
    commented = _plan(
        comments={
            "a": [
                {
                    "id": "c-landing",
                    "who": "coordinator",
                    "when": "2026-10-06",
                    "body": "Node landed.",
                }
            ]
        }
    )
    assert commented != base
    # Excluding the comment means the fingerprint itself does not move.
    assert plan_review.plan_fingerprint(commented) == plan_review.plan_fingerprint(base)
    records, uncovered = _coverage(store, commented)
    assert uncovered == set()
    assert records


def test_a_followup_leaves_every_unit_covered(store) -> None:
    base = _plan()
    _store_review(store, base)
    with_followup = _plan(
        followups=[{"id": "f1", "status": "open", "title": "Next", "body": "Do next."}]
    )
    assert with_followup != base
    assert plan_review.plan_fingerprint(with_followup) == plan_review.plan_fingerprint(
        base
    )
    assert _coverage(store, with_followup)[1] == set()


def test_resolving_one_followup_and_appending_another_leaves_every_unit_covered(
    store,
) -> None:
    base = _plan(
        followups=[{"id": "f1", "status": "open", "title": "Next", "body": "Do next."}]
    )
    _store_review(store, base)
    mutated = _plan(
        followups=[
            {
                "id": "f1",
                "status": "open",
                "title": "Next",
                "body": "Do next.",
                "resolved_at": "2026-10-06",
                "resolved_by": "coordinator",
                "outcome": "Done.",
            },
            {"id": "f2", "status": "open", "title": "More", "body": "Do more."},
        ]
    )
    assert mutated != base
    assert _coverage(store, mutated)[1] == set()


def test_a_decision_edit_uncovers_the_document(store) -> None:
    base = _plan()
    _store_review(store, base)
    changed = _plan(
        decisions={
            "choice": {
                "title": "Which owner?",
                "context": "",
                "choices": ["shared", "own"],
                "option_labels": {},
                "choice": "shared",
                "recommended": "",
                "recommended_by": "",
                "rationale": "Choose a different owner.",
                "when": "",
                "by": "",
                "sections": [],
            }
        }
    )
    records, uncovered = _coverage(store, changed)
    assert uncovered == {"_document"}
    assert records


# ── impl and its derived source ─────────────────────────────────────────────


def test_setting_impl_on_a_plan_that_had_none_leaves_every_unit_covered(store) -> None:
    bare = _plan(sections=[])
    assert "impl" not in _plan_html.read_state(bare)
    _store_review(store, bare)
    with_impl = _plan(sections=[], impl=0.4)
    assert _plan_html.read_state(with_impl).get("impl_source") == "authored"
    assert _coverage(store, with_impl)[1] == set()


def test_flipping_impl_between_authored_and_computed_leaves_every_unit_covered(
    store,
) -> None:
    computed = _plan(impl=0.4)
    assert _plan_html.read_state(computed).get("impl_source") == "computed"
    _store_review(store, computed)
    authored = _plan(
        impl=0.4,
        section_declarations={"a": "deferred", "b": "deferred"},
        sections=_sections("deferred"),
    )
    assert _plan_html.read_state(authored).get("impl_source") == "authored"
    # The two definitions disagree on the source, yet the fingerprint holds.
    assert plan_review.plan_fingerprint(authored) == plan_review.plan_fingerprint(
        computed
    )
    assert _coverage(store, authored)[1] == set()


# ── The metadata-scalar sweep over every live plan ──────────────────────────

_METADATA_EDITS = (
    ("effort_hours", "plan-effort-hours", "123.0"),
    ("version", "plan-version", "99"),
    ("modified", "plan-modified", "2031-01-01"),
    ("impl", "plan-impl", "0.42"),
    ("status", "plan-status", "blocked"),
    ("roi", "plan-roi", "low"),
    ("owner", "plan-owner", "Someone Else"),
    ("sprint", "plan-sprint", "S99"),
    ("tags", "plan-tags", "x,y"),
    ("archived", "plan-archived", "1"),
)


def _write_meta(html: str, name: str, value: str) -> str:
    pattern = re.compile(f'<meta name="{re.escape(name)}" content="[^"]*"')
    if pattern.search(html):
        return pattern.sub(f'<meta name="{name}" content="{value}"', html, count=1)
    return html.replace(
        "</head>", f'<meta name="{name}" content="{value}">\n</head>', 1
    )


def test_metadata_sweep_over_every_live_plan_moves_only_excluded_keys() -> None:
    allowed = (
        set(plan_review.PLAN_METADATA_SCALARS)
        | set(plan_review.PLAN_DERIVED_SCALARS)
        | {"section_declarations"}
    )
    plans = sorted(_LIVE_PLANS.glob("*.html"))
    assert plans, "no live plans under docs/plans to sweep"
    checked = 0
    for path in plans:
        html = path.read_text(encoding="utf-8")
        base_state = _plan_html.read_state(html)
        base_fingerprint = plan_review.plan_fingerprint(html)
        for _key, meta, value in _METADATA_EDITS:
            edited = _write_meta(html, meta, value)
            if edited == html:
                continue
            edited_state = _plan_html.read_state(edited)
            moved = {
                key
                for key in set(base_state) | set(edited_state)
                if base_state.get(key) != edited_state.get(key)
            }
            unexpected = moved - allowed
            assert not unexpected, f"{path.name} {meta}: {sorted(unexpected)} moved"
            assert plan_review.plan_fingerprint(edited) == base_fingerprint, (
                f"{path.name}: the metadata edit {meta} moved the fingerprint"
            )
            checked += 1
    assert checked > 0, "no metadata edit reached any live plan"


# ── The snapshot is the authority, the stored digests a cache ───────────────


def test_a_snapshot_outweighs_stored_digests_from_another_definition(
    store, monkeypatch
) -> None:
    commented = _plan(
        comments={
            "a": [
                {
                    "id": "c-landing",
                    "who": "coordinator",
                    "when": "2026-10-06",
                    "body": "Node landed.",
                }
            ]
        }
    )
    original = plan_review._without_comments_and_followups
    monkeypatch.setattr(
        plan_review, "_without_comments_and_followups", lambda state: state
    )
    legacy = plan_review._section_digests(commented)
    monkeypatch.setattr(plan_review, "_without_comments_and_followups", original)
    # The two definitions disagree on the document unit, or the case is vacuous.
    assert legacy["_document"] != plan_review._section_digests(commented)["_document"]

    # A fingerprint the present definition never produces forces the per-unit
    # path, so coverage cannot be answered by the whole-document match alone.
    _store_review(store, commented, digests=legacy, fingerprint="legacy-definition")

    # The snapshot matches the plan, so the review still covers every unit.
    assert _coverage(store, commented)[1] == set()

    # Remove the snapshot and the predicate falls back to the stored digests,
    # which disagree with the present definition, uncovering the document.
    _snapshot_path(store).unlink()
    records, uncovered = _coverage(store, commented)
    assert uncovered == {"_document"}
    assert records
