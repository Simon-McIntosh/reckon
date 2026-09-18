"""The literal-markdown check reads prose, not verbatim subtrees.

A document whose subject is a literal spelling quotes it — ``**status:**``
inside a ``<code>`` span or a ``<pre>`` block is the subject under discussion,
not markdown a reader was meant to see rendered. The literal-markdown check
must therefore ignore text that those elements carry literally, and still
speak about an asterisk pair in ordinary prose.
"""

from __future__ import annotations

import re
from pathlib import Path

from reckon.doccheck import audit_html

ROOT = Path(__file__).parents[1]
PLAN_QUOTING_ASTERISKS = ROOT / "docs" / "plans" / "crew-observability-truth.html"

# The specimen the exclusion is asserted against. It is owned by this file and
# carries the shapes that motivated the exclusion — an asterisk pair inside a
# ``<code>`` span and one inside a ``<pre>`` block — plus one pair in genuine
# prose, so the check is shown separating the two outcomes. The quoted lines
# reproduce the spellings measured against the live parser.
QUOTED_SPECIMEN = (
    "<p>the block spells the status key <code>**status:**</code> for a worker to"
    " copy</p>"
    "<pre>- status: done                    -&gt; REFUSED\n"
    "- **status**: done                  -&gt; REFUSED</pre>"
)
PROSE_SPECIMEN = (
    "<p>this sentence literally spells **bold** where a reader would see stars</p>"
)


def _bare(body: str = "") -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="plan-slug" content="test">'
        '<meta name="plan-status" content="active">'
        "<title>test</title></head>"
        f'<body><main class="plan-doc">{body}</main></body></html>'
    )


def _codes(body: str) -> list[str]:
    return [f.code for f in audit_html(_bare(body))]


def test_code_span_asterisks_are_not_rendered_markdown():
    codes = _codes(
        "<p>the block spells the status key <code>**status:**</code> for a"
        " worker to copy</p>"
    )
    assert "md-bold" not in codes


def test_pre_block_asterisks_are_not_rendered_markdown():
    codes = _codes(
        "<p>against the live parser:</p>"
        "<pre>**status:** complete      -&gt; 'complete'\n"
        "- **status**: done          -&gt; REFUSED</pre>"
    )
    assert "md-bold" not in codes


def test_code_span_nested_in_a_body_class_is_not_rendered_markdown():
    codes = _codes(
        '<div class="r-comment-body"><p>Five of seven forms read; the one that'
        " motivated the node carries <code>**status**:</code> and does"
        " not.</p></div>"
    )
    assert "md-bold" not in codes


def test_pre_block_nested_in_a_body_class_is_not_rendered_markdown():
    codes = _codes(
        '<div class="r-fu-body"><pre>- status: done        -&gt; REFUSED\n'
        "- **status**: done    -&gt; REFUSED</pre></div>"
    )
    assert "md-bold" not in codes


def test_dictionary_unpacking_in_a_code_span_is_not_rendered_markdown():
    codes = _codes(
        "<p>it reads <code>{**current[run_id], &quot;state&quot;:"
        ' "dispatched"}</code> as a literal</p>'
    )
    assert "md-bold" not in codes


def test_asterisk_pair_in_prose_still_reports():
    codes = _codes("<p>this paragraph literally spells **bold** in prose</p>")
    assert "md-bold" in codes


def test_asterisk_pair_in_a_body_class_prose_still_reports():
    codes = _codes(
        '<div class="r-fu-body">this outcome literally spells **bold** in prose</div>'
    )
    assert "md-bold" in codes


def test_prose_asterisks_still_report_beside_quoted_ones():
    codes = _codes(
        '<div class="r-comment-body"><p>the author literally wrote **bold**'
        " here, while the key <code>**status:**</code> is quoted</p></div>"
    )
    assert "md-bold" in codes


def test_leading_marker_warning_is_untouched_by_the_verbatim_exclusion():
    codes = _codes('<div class="r-q-body">- a leading marker still warns</div>')
    assert "md-list-or-heading" in codes


def test_a_document_quoting_asterisks_only_in_verbatim_subtrees_has_no_bold_finding():
    codes = _codes(QUOTED_SPECIMEN)
    assert "md-bold" not in codes


def test_the_same_quoted_spellings_report_when_they_are_prose():
    codes = _codes(
        "<p>the block spells the status key **status:** for a worker to copy,"
        " and the parser prints **status**: done              -&gt; REFUSED</p>"
    )
    assert "md-bold" in codes


def test_a_specimen_separates_a_prose_asterisk_pair_from_the_quoted_ones():
    findings = audit_html(_bare(QUOTED_SPECIMEN + PROSE_SPECIMEN))
    bold = [f.message for f in findings if f.code == "md-bold"]
    assert len(bold) == 1, bold
    assert "**bold**" in bold[0]


def test_the_live_plan_reports_no_pair_from_its_verbatim_subtrees():
    """The plan that motivated the exclusion stays in the set, read live.

    Auditing it must report the same asterisk pairs whether or not its verbatim
    subtrees are present, because none of the pairs those subtrees carry is
    prose. That comparison is insensitive to the plan's content: a pair added to
    or removed from any of its parts — verbatim or prose — yields the identical
    finding on both sides, so no edit a session makes to that living document
    can fail this file. Only a parser change that started reporting a pair
    inside a ``<pre>`` or ``<code>`` subtree as prose would move the two apart.
    """
    plan_text = PLAN_QUOTING_ASTERISKS.read_text(encoding="utf-8")
    without_verbatim = re.sub(
        r"<(?:pre|code)\b.*?</(?:pre|code)>", "", plan_text, flags=re.DOTALL
    )
    assert without_verbatim != plan_text, "the plan carries no verbatim subtree"

    def bold(html: str) -> list[str]:
        return [f.message for f in audit_html(html) if f.code == "md-bold"]

    assert bold(plan_text) == bold(without_verbatim)
