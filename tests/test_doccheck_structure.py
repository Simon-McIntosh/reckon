"""Structural balance in doccheck — specimens the test builds itself.

The audit reports an unclosed ``<section>``, a stray ``</section>`` and a
document carrying more than one ``<header>``. Every specimen here is built by
the test rather than read from a live document: a check asserted against a
live file is a fuse, because the day that file is legitimately repaired the
test turns red and reads as a doccheck regression.

The failure mode these cover is that an unbalanced document reports OK. A
document whose ``<section>`` never closes makes every following sibling a
child of it, so the section tree no longer matches the authored boundaries —
and because both a browser and an HTML parser repair the nesting silently, the
document renders as though nothing were wrong. That is a confident wrong
answer, which is why an unbalanced section reports at ``error``.
"""

from __future__ import annotations

from reckon.doccheck import audit_html


def _doc(body: str) -> str:
    """A minimal plan document whose only possible findings are structural."""
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="plan-slug" content="structure">'
        '<meta name="plan-status" content="active">'
        "<title>structure</title></head>"
        f'<body><main class="plan-doc">{body}</main></body></html>'
    )


def _codes(html: str) -> list[str]:
    return [f.code for f in audit_html(html)]


# ─ unclosed section ─────────────────────────────────────────────────────────


def test_unclosed_section_is_reported():
    html = _doc('<section id="a"><h2>A</h2><p>text</p>')
    assert "section-unclosed" in _codes(html)


def test_unclosed_unnamed_section_is_reported():
    html = _doc("<section><h2>A</h2><p>text</p>")
    assert "section-unclosed" in _codes(html)


def test_unclosed_section_does_not_also_read_as_a_stray_close():
    # Outer section never closed, inner one closed: one unclosed element and
    # no stray close — the shape the live specimen carries.
    html = _doc('<section id="a"><h2>A</h2><section id="b"><h2>B</h2></section>')
    codes = _codes(html)
    assert "section-unclosed" in codes
    assert "section-stray-close" not in codes


def test_the_unclosed_section_is_the_one_named():
    html = _doc(
        '<section id="outer"><h2>O</h2><section id="inner"><h2>I</h2></section>'
    )
    findings = [f for f in audit_html(html) if f.code == "section-unclosed"]
    assert len(findings) == 1
    assert "outer" in findings[0].message


# ─ stray closing tag ─────────────────────────────────────────────────────────


def test_stray_closing_tag_is_reported():
    html = _doc("<section><h2>A</h2></section></section>")
    codes = _codes(html)
    assert "section-stray-close" in codes
    assert "section-unclosed" not in codes


def test_stray_closing_tag_with_no_section_at_all_is_reported():
    html = _doc("<p>prose</p></section>")
    codes = _codes(html)
    assert "section-stray-close" in codes
    assert "section-unclosed" not in codes


# ─ duplicated document header ────────────────────────────────────────────────


def test_second_header_is_reported():
    html = _doc(
        "<header><h1>One</h1></header>"
        '<section id="a"><h2>A</h2></section>'
        '<header class="plan-header"><h1>Two</h1></header>'
    )
    assert "header-duplicate" in _codes(html)


def test_one_header_is_not_reported():
    html = _doc("<header><h1>One</h1></header>")
    assert "header-duplicate" not in _codes(html)


def test_no_header_is_not_reported():
    # A plan body need not open with a header — the canonical shell permits a
    # section-first document, and a missing header is a different question.
    html = _doc('<section id="a"><h2>A</h2></section>')
    assert "header-duplicate" not in _codes(html)


# ─ the silent cases ──────────────────────────────────────────────────────────


def test_balanced_document_with_one_header_is_completely_clean():
    html = _doc(
        "<header><h1>Doc</h1></header>"
        '<section id="a"><h2>A</h2><p>text</p></section>'
        '<section id="b"><h2>B</h2><p>text</p></section>'
    )
    assert audit_html(html) == []


def test_deliberately_nested_sections_each_closed_stay_silent():
    html = _doc(
        "<header><h1>Doc</h1></header>"
        '<section id="outer"><h2>Outer</h2>'
        '<section id="inner"><h2>Inner</h2></section>'
        "</section>"
    )
    assert audit_html(html) == []


def test_structure_codes_absent_when_prose_merely_names_them():
    html = _doc("<p>An unclosed section and a stray closing tag.</p>")
    codes = _codes(html)
    assert "section-unclosed" not in codes
    assert "section-stray-close" not in codes


def test_escaped_section_tag_in_a_code_span_is_not_a_section():
    html = _doc(
        '<section id="a"><h2>A</h2>'
        "<pre><code>&lt;section&gt;example&lt;/section&gt;</code></pre>"
        "</section>"
    )
    codes = _codes(html)
    assert "section-unclosed" not in codes
    assert "section-stray-close" not in codes
