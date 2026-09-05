"""The read-side partner of the unparsed-section write guard.

The write path refuses to splice an empty collection over a ``data-reckon``
section whose authored children the parser never recognised (see
test_plan_write_preserves_unparsed.py). This is the read side: ``read_plan``
reports the same hazard in the warnings it already returns, so an agent that
reads a plan's version before an edit is told "this section will not survive a
rewrite" before it spends the rewrite.

Reading must never raise and must never change the parsed value — the warning
is additive, carried in the same ``compatibility_warnings`` channel every other
read/write warning already uses; a caller doing ``data, version =
read_plan(...)`` sees it without asking for anything extra.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reckon._store as store
from reckon._plan_html import _SECTION_RECOGNISED_SPELLING, SECTION_IDS

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def plans_dir(tmp_path: Path) -> Path:
    """A scratch ``docs/plans`` tree under a checkout root."""
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    return plans


def _write_plan(plans_dir: Path, slug: str, body: str) -> Path:
    """Write a minimal parseable plan whose body holds data-reckon sections."""
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="docs-project" content="reckon">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f"<title>{slug} | reckon</title>\n"
        '</head>\n<body>\n<main class="plan-doc">\n'
        f"{body}\n"
        "</main>\n</body>\n</html>\n"
    )
    path = plans_dir / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _root(plans_dir: Path) -> Path:
    """The checkout root for the scratch docs tree (parent of ``docs``)."""
    return plans_dir.parent.parent


# Each section's parser selects one canonical child spelling; any other spelling
# is invisible to it, so an authored item in that spelling parses to nothing.
# One clearly non-canonical direct child per section type — the class never
# equals the selector token the parser keys on, so nothing is recognised.
UNRECOGNISED_CHILD = {
    "gates": '<div class="gate-card">Evidence gate</div>',
    "decisions": (
        '<article class="r-decision" data-id="speed">'
        '<p class="q">Speed up the pipeline?</p></article>'
    ),
    "followups": '<div class="work-item" data-id="f1">A task</div>',
    "questions": '<div class="question-card">An open question</div>',
    "research": '<div class="reference-card">A citation</div>',
    "comments": '<div class="note-card">A note</div>',
}


# ── Every section type ─────────────────────────────────────────────────────


@pytest.mark.parametrize("reckon_id", SECTION_IDS)
def test_unrecognised_children_warn_for_every_section_type(plans_dir, reckon_id):
    """Every data-reckon section a read-modify-write splices — not just the
    incident decisions spelling — surfaces the same read warning."""
    child = UNRECOGNISED_CHILD[reckon_id]
    body = (
        f'<section data-reckon="{reckon_id}" id="{reckon_id}">\n'
        f"  {child}\n  {child}\n"
        "</section>"
    )
    _write_plan(plans_dir, f"unparsed-{reckon_id}", body)

    data, _ = store.read_plan("reckon", f"unparsed-{reckon_id}", root=_root(plans_dir))

    # The warning rides in the same state read_plan already returns.
    warnings = data["compatibility_warnings"]
    hit = [w for w in warnings if f'data-reckon="{reckon_id}"' in w]
    assert hit, (reckon_id, warnings)
    message = hit[0]
    assert "2 authored child element" in message
    assert _SECTION_RECOGNISED_SPELLING[reckon_id] in message
    # The warning must not change the parsed value: the collection is still
    # exactly what the parser reads — only the warning was added.
    assert not data[reckon_id]


def test_incident_spelling_article_r_decision_warns(plans_dir):
    """The reported production spelling — decisions authored as
    ``article.r-decision`` with ``data-id`` — warns with the section, the
    count of authored children the parser did not recognise, and the expected
    spelling, and the parsed collection is unchanged (empty)."""
    _write_plan(
        plans_dir,
        "incident-decisions",
        '<section data-reckon="decisions" id="decisions">\n'
        '  <article class="r-decision" data-id="speed">'
        '<p class="q">Speed up?</p></article>\n'
        '  <article class="r-decision" data-id="backend">'
        '<p class="q">Backend?</p></article>\n'
        "</section>",
    )

    data, _ = store.read_plan("reckon", "incident-decisions", root=_root(plans_dir))

    warning = next(
        w
        for w in (data.get("compatibility_warnings") or [])
        if w.startswith("plan read:")
    )
    assert 'data-reckon="decisions"' in warning  # the section
    assert "2 authored child element" in warning  # the count
    assert '<div class="r-dec" data-key="...">' in warning  # the expected spelling
    assert data["decisions"] == {}  # value unchanged by the warning


def test_caller_taking_version_before_an_edit_sees_the_warning(plans_dir):
    """A caller reading the version to prepare a write gets the hazard in the
    same call — no second read, no extra argument."""
    _write_plan(
        plans_dir,
        "pre-edit",
        '<section data-reckon="decisions" id="decisions">'
        '<div class="decision-card">A decision</div></section>',
    )

    data, version = store.read_plan("reckon", "pre-edit", root=_root(plans_dir))

    assert version == 0
    assert any(
        'data-reckon="decisions"' in w for w in data.get("compatibility_warnings") or []
    )


# ── Clean plans must not warn ──────────────────────────────────────────────


def test_canonical_decision_spelling_returns_no_warning(plans_dir):
    body = (
        '<section data-reckon="decisions" id="decisions" class="r-decisions">\n'
        '  <div class="r-dec" data-key="backend" data-choice="torch">\n'
        '    <p class="r-dec-q">Resize backend — cv2 or torch?</p>\n'
        "  </div>\n"
        "</section>"
    )
    _write_plan(plans_dir, "canonical-decisions", body)

    data, _ = store.read_plan("reckon", "canonical-decisions", root=_root(plans_dir))

    assert set(data["decisions"]) == {"backend"}
    assert not [
        w
        for w in (data.get("compatibility_warnings") or [])
        if "parser did not recognise" in w
    ]


def test_genuinely_empty_section_returns_no_warning(plans_dir):
    _write_plan(
        plans_dir,
        "empty-decisions",
        '<section data-reckon="decisions" id="decisions" class="r-decisions">'
        "</section>",
    )

    data, _ = store.read_plan("reckon", "empty-decisions", root=_root(plans_dir))

    assert data["decisions"] == {}
    assert not [
        w
        for w in (data.get("compatibility_warnings") or [])
        if "parser did not recognise" in w
    ]


def test_every_live_plan_under_docs_plans_reads_without_the_warning():
    """No canonical plan in this repo trips the hazard the guard reports —
    iterate every live plan under ``docs/plans`` and require none warns."""
    repo_root = Path(__file__).resolve().parents[1]
    live_plans = sorted((repo_root / "docs" / "plans").glob("*.html"))
    assert live_plans  # the iteration covers something real

    offending = []
    for plan in live_plans:
        data, _ = store.read_plan("reckon", plan.stem, root=repo_root)
        assert data, f"cannot resolve {plan.name}"  # non-vacuous read
        offending.extend(
            (plan.name, warning)
            for warning in data.get("compatibility_warnings") or []
            if "parser did not recognise" in warning
        )
    assert not offending
