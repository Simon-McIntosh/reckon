"""One section-prose reader feeds the digests, the search text and the view.

The reviewer of a plan reads authored text through one reader, and the search
text and the section view take their prose from that same reader. The checks
here pin both surfaces against the retired algorithms on every plan in this
project, held as reference functions so a plan edit moves both sides alike
instead of tripping a snapshot.

The reference functions carry the retired algorithms with one deliberate
correction. The retired walk dropped every ``section`` carrying the reckon
marker, so a section authored inside its own marked wrapper — an element whose
interior is that section's heading and prose — produced no text at all; the
reader now treats that element as transparent. The search reference below
therefore keeps a marked section element's interior, subtracting only the marked
collections nested inside it, and the section reference keeps the retired
heading-plus-body extraction unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup, Comment, Tag

from reckon import _plan_html, mcp_views
from reckon._plan_html import RECKON_ATTRIBUTE, machinery_kind
from reckon.mcp_views import ResourceSelector

ROOT = Path(__file__).resolve().parents[1]


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _drop_comments(scope: Tag) -> None:
    for node in scope.find_all(string=lambda text: isinstance(text, Comment)):
        node.extract()


def _reference_search_text(html_text: str) -> str:
    """The retired search walk, corrected for the transparent section record.

    Every marked element is dropped except a ``section`` carrying the section
    marker, whose interior is that section's authored prose: the marked
    collections nested inside it are dropped and its remaining text is kept. A
    marked ``div`` or ``p`` — a landed note — is retained, as the retired walk
    retained it. The result is the plan's search text.
    """
    soup = BeautifulSoup(html_text or "", "html.parser")
    scope = soup.body or soup
    _drop_comments(scope)
    for element in scope.select("script, style"):
        element.decompose()
    for element in list(scope.select(f"[{RECKON_ATTRIBUTE}]")):
        if element.name in ("div", "p"):
            continue
        if element.name == "section" and machinery_kind(element.attrs) == "section":
            for nested in list(element.select(f"[{RECKON_ATTRIBUTE}]")):
                if nested.name not in ("div", "p"):
                    nested.decompose()
            continue
        element.decompose()
    return " ".join(scope.stripped_strings)


@pytest.fixture(scope="module")
def plans() -> list[tuple[Path, str]]:
    paths = sorted((ROOT / "docs" / "plans").rglob("*.html"))
    assert paths, "the parity population must contain committed plans"
    return [(path.relative_to(ROOT), path.read_text()) for path in paths]


def _reference_heading_html(heading: Tag) -> str:
    rendered = BeautifulSoup(str(heading), "html.parser").find(True)
    if rendered is None:  # pragma: no cover
        return str(heading)
    if machinery_kind(rendered.attrs) == "section":
        for attribute in tuple(rendered.attrs):
            if (
                attribute == RECKON_ATTRIBUTE
                or attribute
                in {
                    "data-effort-hours",
                    "data-attempts",
                    "data-status",
                    "data-links",
                }
                or attribute.startswith("data-capability-")
            ):
                del rendered.attrs[attribute]
    return str(rendered)


def _reference_section_text(source: str, heading) -> str:
    """The retired section extraction: heading plus the section body.

    The heading's own extent leads, then the raw body between the heading and
    the section close, with any nested marked ``section`` removed. The result
    is the section's rendered text. Kept here as a reference, not a snapshot.
    """
    opening, closing = heading.heading_span
    rendered_heading = BeautifulSoup(source[opening:closing], "html.parser").find(True)
    fragments = [_reference_heading_html(rendered_heading)]
    body_source = source[slice(*heading.body_span)]
    body = BeautifulSoup(body_source, "html.parser")
    _drop_comments(body)
    for element in body.select(f"section[{RECKON_ATTRIBUTE}]"):
        if machinery_kind(element.attrs) is not None:
            element.decompose()
    fragments.extend(
        str(element).strip() for element in body.contents if str(element).strip()
    )
    return " ".join(BeautifulSoup("\n".join(fragments), "html.parser").stripped_strings)


def test_surfaces_reproduce_their_reference_outputs_on_every_plan(plans):
    """The search text and every section text must match the retired algorithms.

    A plan edit changes the plan and the reference alike, so no stored snapshot
    is involved. Any plan whose surface cannot be reproduced is named.
    """
    differing: list[str] = []
    for path, source in plans:
        if _normalise(mcp_views.authored_plan_text(source)) != _normalise(
            _reference_search_text(source)
        ):
            differing.append(f"{path} (search text)")
        data = _plan_html.read_state(source)
        for identity, heading in mcp_views._authored_section_headings(source):
            got = _normalise(
                mcp_views._section_response(
                    ResourceSelector(project="reckon", type="plan", id=path.stem),
                    1,
                    data,
                    section=identity,
                    html_text=source,
                )["section"]["text"]
            )
            expected = _normalise(_reference_section_text(source, heading))
            if got != expected:
                differing.append(f"{path}#{identity} (section text)")
    assert not differing, "surfaces differ from their reference outputs: " + "; ".join(
        differing
    )


def test_a_wrapped_section_yields_its_prose_by_default_and_subtracts_nested_collections():
    """A section authored inside its own marked wrapper is not empty prose.

    The section element carrying the section marker is transparent: its interior
    is that section's authored prose. A marked collection nested inside it is
    machinery and is subtracted, so a decision record inside a wrapped section
    never reaches the prose.
    """
    document = (
        "<html><body><main>"
        '<section id="s1" data-reckon="section">'
        "<h2>&#167;1 &#8212; Wrapped</h2>"
        "<p>authored body text</p>"
        f'<section {RECKON_ATTRIBUTE}="decisions">'
        "<p>machinery decision text</p>"
        "</section>"
        "</section>"
        "</main></body></html>"
    )
    prose = " ".join(
        text
        for identity, text in _plan_html.section_prose(document)
        if identity == "s1"
    )
    assert "authored body text" in prose
    assert "machinery decision text" not in prose
    assert "machinery decision text" not in mcp_views.authored_plan_text(document)


def test_a_kept_landed_card_starts_at_its_heading_not_its_generated_badge():
    """A landed card's badge is generated, so a section read starts at the h2.

    When the card interior is kept, the stretch before its first heading — the
    generated badge in the card header — is reported under the document unit, so
    the section read of the identity excludes it while a search surface that
    joins every slice still keeps it.
    """
    document = (
        "<html><body><main>"
        '<section class="section-landed">'
        "<header>"
        '<span class="badge">&#10003; landed 2026-10-02</span>'
        '<h2 id="s3">&#167;3 &#8212; Landed</h2>'
        "</header>"
        '<p class="landed-summary">landed summary text</p>'
        "</section>"
        "</main></body></html>"
    )
    slices = list(
        _plan_html.section_prose(
            document, keep_landed_cards=True, keep_landed_notes=True
        )
    )
    section_text = " ".join(prose for identity, prose in slices if identity == "s3")
    assert "landed summary text" in section_text
    assert "landed 2026-10-02" not in section_text
    joined = " ".join(prose for _identity, prose in slices)
    assert "landed 2026-10-02" in joined


def test_authored_section_list_matches_its_base_rule_on_every_plan(plans):
    for path, source in plans:
        expected = []
        claimed = set()
        soup = BeautifulSoup(source, "html.parser")
        for heading in _plan_html.plan_headings(source):
            if heading.level != 2 or not heading.raw_id:
                continue
            element = soup.find(id=heading.raw_id)
            inside = element is None or any(
                isinstance(parent, Tag)
                and parent.name == "section"
                and machinery_kind(parent.attrs) not in (None, "section")
                for parent in element.parents
            )
            if inside or heading.identity in claimed or heading.machinery:
                continue
            claimed.add(heading.identity)
            expected.append(heading.identity)
        got = [
            identity
            for identity, _heading in mcp_views._authored_section_headings(source)
        ]
        assert got == expected, path


def _grep(pattern: str, roots: tuple[str, ...]) -> list[str]:
    hits = []
    for root in roots:
        for path in sorted((ROOT / root).rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if re.search(pattern, line):
                    hits.append(f"{path.relative_to(ROOT)}:{number}:{line}")
    return hits


def test_the_prose_reader_has_one_home_and_no_second_slicer():
    assert len(_grep(r"\bdef section_prose\b", ("reckon",))) == 1
    assert _grep(r"\bdef strip_tags\b", ("reckon",)) == []
    assert len(_grep(r"\bdef _strip_tags\b", ("reckon",))) == 1
    assert _grep("_prose_" + "slices", ("reckon", "tests")) == []
    assert _grep(r"_inside_structured_region", ("reckon",)) == []
    assert _grep(r'r"<body|r"</body', ("reckon",)) == []


def test_span_rule_keywords_have_one_home_and_document_unit_one_owner():
    keywords = _grep(r"every_marked_element|skip_landed_cards", ("reckon",))
    assert keywords, "the private span rule must name its modes"
    homes = {hit.split(":")[0] for hit in keywords}
    assert homes == {"reckon/_plan_html.py"}, homes
    document_literals = _grep(r'"_document"', ("reckon",))
    assert len(document_literals) == 1
    assert document_literals[0].startswith("reckon/_plan_html.py:")


def test_tag_expression_keeps_one_home_and_two_readers():
    lines = _grep(r"\b_TAG_RE\b", ("reckon",))
    assert len(lines) == 3, lines
    assert all(line.startswith("reckon/_plan_html.py:") for line in lines)
