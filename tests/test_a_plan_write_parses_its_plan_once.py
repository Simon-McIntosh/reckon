"""One landing's plan write parses the plan document once.

A plan write consults the document three ways: the collection splices and the
authored-child census read a soup, the section-record splice regenerates record
elements from a soup, and those elements take their source extents from a
structural scan. Each pass used to build its own parse of the whole document —
five document-sized soups for one landing on a real plan — and the writing
functions held 71% of a promotion's body at a small ledger, a flat cost on
every landing.

The write now builds one parse and hands it to every pass. The count below is
the check: one document-sized soup per landing's write, and the bytes written
are exactly what the per-pass writer produces for the same inputs.
"""

from __future__ import annotations

import pytest

from reckon import _plan_html

# One authored section with a record, a gate and one existing comment, so the
# write exercises the record splice, the collection splices and the body
# comment census on a real document shape.
PLAN = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
    '<meta name="docs-project" content="sample">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="sample-plan">\n'
    '<meta name="plan-status" content="active">\n'
    '<meta name="plan-version" content="1">\n'
    "<title>sample-plan</title></head>\n"
    '<body><main class="plan-doc">\n'
    '<h2 id="s1">One</h2>\n'
    '<section data-reckon="section" data-id="s1" data-effort-hours="1.0" '
    'data-capability-version="1.0" data-capability-class="general" '
    'data-capability-reasoning="standard" '
    'data-capability-verification="strict" data-capability-risk="low" '
    'data-status="implementable" data-links=""></section>\n'
    "<p>Section one prose.</p>\n"
    '<section data-reckon="gates" id="gates"><div class="r-gate" data-id="g1" '
    'data-section="s1" data-status="open"><div class="r-gate-measure">m</div>'
    "</div></section>\n"
    '<section data-reckon="comments" id="comments"><div class="r-comment" '
    'data-section="s1" data-id="c1" data-who="w" '
    'data-when="2026-01-01T00:00:00Z"><div class="r-comment-body">first</div>'
    "</div></section>\n"
    "</main></body></html>\n"
)

# What the per-pass writer produced for this fixture at the base revision.
EXPECTED = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8">\n'
    '<meta name="docs-project" content="sample">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="sample-plan">\n'
    '<meta name="plan-status" content="active">\n'
    '<meta name="plan-version" content="2">\n'
    '<title>sample-plan</title><meta name="plan-title" content="sample-plan">\n'
    "</head>\n"
    '<body><main class="plan-doc">\n'
    '<h2 id="s1">One</h2>\n'
    '<section data-reckon="section" data-id="s1" data-effort-hours="1.0" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="standard" data-capability-verification="strict" data-capability-risk="low" data-status="implementable" data-links=""></section>\n'
    "<p>Section one prose.</p>\n"
    '<section data-reckon="gates" id="gates" class="r-gates">\n'
    '<h2><span class="sec">§</span> Evidence gates</h2>\n'
    '<div class="r-gate" data-id="g1" data-section="s1" data-gated-sections="" data-status="open" data-verdict="">\n'
    '    <h4 class="r-gate-measure">m</h4>\n'
    '    <p class="r-gate-required-evidence"></p>\n'
    "    </div>\n"
    "</section>\n"
    '<section data-reckon="comments" id="comments" class="r-comments">\n'
    '<div class="r-comment" data-section="s1" data-id="c1" data-who="w" data-when="2026-01-01T00:00:00Z">\n'
    '    <div class="r-comment-body">first</div>\n'
    "</div>\n"
    '<div class="r-comment" data-section="s1" data-id="run-sample-landing" data-who="worker" data-when="2026-11-11T07:00:00Z">\n'
    '    <div class="r-comment-body"><p>landed</p></div>\n'
    "</div>\n"
    "</section>\n"
    "</main></body></html>\n"
)


def _landing_payload(text: str = PLAN) -> dict:
    """The plan state a landing writes: its comment appended, version bumped."""
    state = _plan_html.read_state(text)
    comments = {
        key: list(value) for key, value in (state.get("comments") or {}).items()
    }
    comments.setdefault("s1", []).append(
        {
            "id": "run-sample-landing",
            "who": "worker",
            "when": "2026-11-11T07:00:00Z",
            "body": "<p>landed</p>",
        }
    )
    return {
        **state,
        "comments": comments,
        "version": int(state.get("version") or 0) + 1,
    }


def _landing_write(text: str = PLAN) -> str:
    """Write one landing's comment into the plan, as the store does."""
    return _plan_html.write_state(
        text, _landing_payload(text), drop_authored_impl_meta=True
    )


@pytest.fixture()
def document_parses(monkeypatch) -> list:
    """Every soup built from a document-sized markup, counted in order."""
    seen: list[int] = []
    original = _plan_html.BeautifulSoup
    threshold = len(PLAN) // 2

    def counting(markup="", *args, **kwargs):
        if len(str(markup)) > threshold:
            seen.append(len(str(markup)))
        return original(markup, *args, **kwargs)

    monkeypatch.setattr(_plan_html, "BeautifulSoup", counting)
    return seen


def test_one_landing_write_parses_the_document_once(document_parses) -> None:
    payload = _landing_payload()
    document_parses.clear()
    _plan_html.write_state(PLAN, payload, drop_authored_impl_meta=True)

    # The plan is document-sized, so a parse of it is what this counts; one
    # means every pass read the same parse.
    assert document_parses == [len(PLAN)], (
        "a landing's plan write parsed the document "
        f"{len(document_parses)} times, not once"
    )


def test_the_write_is_byte_identical_to_the_per_pass_writer() -> None:
    assert _landing_write() == EXPECTED


def test_the_reader_still_returns_the_landing_comment() -> None:
    written = _landing_write()
    comments = _plan_html.read_state(written)["comments"]["s1"]
    assert [c["id"] for c in comments] == ["c1", "run-sample-landing"]
    assert comments[-1]["body"] == "<p>landed</p>"
