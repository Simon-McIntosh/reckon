"""A structured write must not delete authored section content it never parsed.

Reported from production: a plan authored its locked decisions as
``<article class="r-decision" data-id=…>`` elements inside
``section[data-reckon="decisions"]`` instead of the canonical
``<div class="r-dec" data-key=…>``. ``read_state`` selected nothing, so the
collection parsed as an empty dict, and the first read-modify-write (three
appended comments) spliced an empty section over the authored one, deleting it.
Everything warned about nothing.

``read_state`` cannot distinguish "no decisions were authored" from "I did not
recognise what was authored", and a rewrite must never delete what the model
never contained — so the write itself refuses, naming the section it would
have emptied, the count of authored child elements it did not parse, and the
recognised spelling so the author can correct it.
"""

from __future__ import annotations

import pytest

from reckon._plan_html import (
    UnparsedSectionWriteError,
    read_state,
    write_state,
)

# The incident spelling: decisions authored as <article class="r-decision"
# data-id=…> rather than the canonical <div class="r-dec" data-key=…>. Two
# top-level authored items, each carrying nested content the parser also never
# sees because it never matches the parent.
UNPARSED_DECISIONS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="docs-project" content="reckon">
<meta name="plan-slug" content="synthetic-unparsed-decisions">
<title>Synthetic unparsed decisions | reckon</title>
</head>
<body>
<main class="plan-doc">
<section data-reckon="decisions" id="decisions">
  <article class="r-decision" data-id="speed">
    <p class="q">Speed up the pipeline?</p>
  </article>
  <article class="r-decision" data-id="backend">
    <p class="q">Backend?</p>
  </article>
</section>
<section data-reckon="comments" id="comments" class="r-comments"></section>
</main>
</body>
</html>
"""

# The canonical spelling from the authored template: <div class="r-dec"
# data-key=…> carrying data-choice, options and a rationale.
CANONICAL_DECISIONS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="docs-project" content="reckon">
<meta name="plan-slug" content="synthetic-canonical-decisions">
<title>Synthetic canonical decisions | reckon</title>
</head>
<body>
<main class="plan-doc">
<section data-reckon="decisions" id="decisions" class="r-decisions">
<h2><span class="sec">§</span> Decisions</h2>
<div class="r-dec" data-key="backend" data-choice="torch" data-by="Simon McIntosh" data-when="2026-09-01T09:30:00Z">
    <p class="r-dec-q">Resize backend — cv2 or torch?</p>
    <p class="r-dec-opts">
      <button class="r-opt chosen" data-value="torch">torch F.interpolate</button>
      <button class="r-opt" data-value="cv2">cv2.resize</button></p>
    <p class="r-dec-rat">cv2 absent from venv; torch matches the daemon byte-for-byte.</p>
</div>
</section>
<section data-reckon="comments" id="comments" class="r-comments"></section>
</main>
</body>
</html>
"""

# A genuinely empty decisions section — nothing authored inside, so nothing is
# at risk and the write must not refuse.
EMPTY_DECISIONS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="docs-project" content="reckon">
<meta name="plan-slug" content="synthetic-empty-decisions">
<title>Synthetic empty decisions | reckon</title>
</head>
<body>
<main class="plan-doc">
<section data-reckon="decisions" id="decisions" class="r-decisions"></section>
<section data-reckon="comments" id="comments" class="r-comments"></section>
</main>
</body>
</html>
"""

# The freshly-created-plan shape: sections carry only their regenerable
# <h2> heading, so a first append must not be refused by the guard.
SKELETON_DECISIONS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="docs-project" content="reckon">
<meta name="plan-slug" content="synthetic-skeleton-decisions">
<title>Synthetic skeleton decisions | reckon</title>
</head>
<body>
<main class="plan-doc">
<section data-reckon="decisions" id="decisions" class="r-decisions">
<h2><span class="sec">§</span> Decisions</h2>
</section>
<section data-reckon="comments" id="comments" class="r-comments"></section>
</main>
</body>
</html>
"""


def _appended_comment() -> dict:
    """The 'touch only comments' edit: one added comment on the top section."""
    return {"id": "c1", "who": "worker", "when": "2026-09-05", "body": "appended"}


def _state_with_added_comment(html_text: str) -> dict:
    state = read_state(html_text)
    state["comments"].setdefault("_top", []).append(_appended_comment())
    return state


def test_unparsed_decisions_still_parse_to_an_empty_collection():
    """Pin the parser's current reading: the unrecognised spelling yields an
    empty collection even though two authored items are present in the source.
    This is the 'absence reported as data' the guard exists to catch."""
    state = read_state(UNPARSED_DECISIONS_HTML)

    assert state["decisions"] == {}
    assert "r-decision" in UNPARSED_DECISIONS_HTML  # the items really are there


def test_write_touching_only_comments_is_refused_over_unparsed_decisions():
    state = _state_with_added_comment(UNPARSED_DECISIONS_HTML)

    with pytest.raises(UnparsedSectionWriteError) as excinfo:
        write_state(UNPARSED_DECISIONS_HTML, state)

    msg = str(excinfo.value)
    assert 'data-reckon="decisions"' in msg  # the section it would have emptied
    assert "2 authored child element" in msg  # the count it did not parse
    # nothing is spliced: the authored articles survive in the source untouched
    assert "r-decision" in UNPARSED_DECISIONS_HTML


def test_write_succeeds_on_a_plan_with_canonical_r_dec_decisions():
    state = _state_with_added_comment(CANONICAL_DECISIONS_HTML)
    assert set(state["decisions"]) == {"backend"}

    rendered = write_state(CANONICAL_DECISIONS_HTML, state)  # must not raise

    parsed = read_state(rendered)
    assert set(parsed["decisions"]) == {"backend"}
    assert parsed["decisions"]["backend"]["choice"] == "torch"
    assert parsed["decisions"]["backend"]["rationale"] == (
        "cv2 absent from venv; torch matches the daemon byte-for-byte."
    )
    assert parsed["comments"]["_top"][0]["body"] == "appended"


def test_write_is_not_refused_when_decisions_section_is_genuinely_empty():
    state = _state_with_added_comment(EMPTY_DECISIONS_HTML)

    rendered = write_state(EMPTY_DECISIONS_HTML, state)  # nothing to lose

    assert read_state(rendered)["comments"]["_top"][0]["body"] == "appended"
    # canonical re-render of an empty section yields no section — that is the
    # existing empty-section handling, not a refusal
    assert read_state(rendered)["decisions"] == {}


def test_write_is_not_refused_when_section_holds_only_a_regenerable_heading():
    """The freshly-created-plan shape: an <h2> header with no decision items is
    scaffolding, not authored content — a first append must go through."""
    state = _state_with_added_comment(SKELETON_DECISIONS_HTML)

    rendered = write_state(SKELETON_DECISIONS_HTML, state)  # must not raise

    assert read_state(rendered)["comments"]["_top"][0]["body"] == "appended"


def test_refusal_message_names_the_recognised_spelling():
    state = _state_with_added_comment(UNPARSED_DECISIONS_HTML)

    with pytest.raises(UnparsedSectionWriteError) as excinfo:
        write_state(UNPARSED_DECISIONS_HTML, state)

    msg = str(excinfo.value)
    # the canonical element the author should have used, so they can correct it
    assert '<div class="r-dec" data-key="...">' in msg
    assert "r-dec" in msg


def test_meta_only_write_is_not_refused_even_with_unparsed_decisions():
    """A write that does not splice sections (no section keys in the state —
    the archiving stamp, for example) must not refuse: it removes nothing."""
    rendered = write_state(UNPARSED_DECISIONS_HTML, {"archived": "1"})

    assert "r-decision" in rendered
