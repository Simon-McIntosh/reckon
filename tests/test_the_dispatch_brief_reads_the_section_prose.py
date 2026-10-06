"""The dispatch brief reads a plan section through the one section-prose reader.

``dispatch._plan_section_text`` resolves a requested section spelling to a
heading and then reads that section's authored prose. The read comes from
:func:`reckon._plan_html.section_prose` — the one reader the review digests and
the section view also use — so a second soup walk no longer extracts a plan
section's text. The checks here pin that convergence: a spy proves the read
comes from the one reader, and every section of every plan in this project is
compared to the retired span read, so a plan edit moves both sides alike.

The retired span read took the heading's whole extent, which carries machinery
the reader deliberately excludes: a landed card's generated badge and interior,
and any marked collection nested inside a section body. Where those exclusions
account for the difference the retired text is a superset of the reader's, so
the reader's tokens are a subsequence of the retired tokens. A plan whose
committed markup the two reads genuinely disagree on is named in
:data:`EXEMPT_PLANS` with its reason and the set is asserted below.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from reckon._plan_html import (
    DOCUMENT_UNIT,
    RECKON_ATTRIBUTE,
    plan_headings,
    section_id_candidates,
    section_prose,
)

# ``reckon.crew.dispatch`` names both the module and a function re-exported from
# ``reckon.crew``, so bind the module by import rather than attribute lookup.
dispatch = importlib.import_module("reckon.crew.dispatch")

ROOT = Path(__file__).resolve().parents[1]

# Plans whose committed markup the retired span read and the reader parse
# differently, so their section texts cannot agree even after the reader's
# declared exclusions. Each entry states the divergence; the set is frozen and
# asserted below so it cannot grow without a reviewer seeing it.
EXEMPT_PLANS: dict[str, str] = {
    "docs/plans/fleet-monitor-legibility.html": (
        "two level-two headings share the id 's7'; the reader keys both extents to "
        "that one identity while the retired span read resolves the first only"
    ),
}


def _normalise(text: str) -> str:
    return " ".join((text or "").split())


def _retired_section_text(source: str, heading) -> str:
    """The retired read: visible text over the heading's whole source extent."""
    return _normalise(
        BeautifulSoup(source[slice(*heading.span)], "html.parser").get_text(
            " ", strip=True
        )
    )


def _is_subsequence(shorter: list[str], longer: list[str]) -> bool:
    """Whether ``shorter`` is ``longer`` with runs removed, order preserved."""
    remaining = iter(longer)
    return all(token in remaining for token in shorter)


def _resolve(source: str, soup: BeautifulSoup, headings, section: str):
    """The requested spelling resolved to a heading record, or ``None``.

    Mirrors the resolution ``_plan_section_text`` keeps: an element whose id is
    a candidate spelling is preferred, and when it is not a heading the read is
    of that element; otherwise a heading whose text or identity matches.
    """
    requested = re.sub(r"\s+", " ", section.strip()).casefold()
    if not requested:
        return None
    ids = section_id_candidates(requested)
    identified = next(
        (
            tag
            for tag in soup.find_all(id=True)
            if str(tag.get("id") or "").casefold() in ids
        ),
        None,
    )
    if identified is not None:
        heading = next(
            (item for item in headings if item.raw_id == identified.get("id")), None
        )
        if heading is None:
            return None
    else:
        heading = next(
            (
                item
                for item in headings
                if dispatch._section_heading_matches(item, requested, ids)
            ),
            None,
        )
    return heading


def _has_marked_machinery(source: str, heading) -> bool:
    """Whether a marked collection sits inside the heading's source extent."""
    soup = BeautifulSoup(source[slice(*heading.span)], "html.parser")
    return bool(soup.select(f"[{RECKON_ATTRIBUTE}]"))


@pytest.fixture(scope="module")
def plans() -> list[tuple[Path, str]]:
    paths = sorted((ROOT / "docs" / "plans").rglob("*.html"))
    assert paths, "the parity population must contain committed plans"
    return [(path.relative_to(ROOT), path.read_text()) for path in paths]


def _section_population(source: str) -> list[str]:
    """Every identity a section reference may address: the reader's and headings'."""
    identities = {
        identity
        for identity, _prose in section_prose(source)
        if identity != DOCUMENT_UNIT
    }
    for heading in plan_headings(source):
        if heading.identity and heading.level == 2:
            identities.add(heading.identity)
    return sorted(identities)


def test_the_dispatch_section_text_reads_through_the_one_reader(monkeypatch):
    """The section text comes from ``section_prose``, not a second soup walk.

    The reader is replaced with a sentinel yielding a known prose for the
    resolved identity. If the read were still taken from the heading's own soup
    walk the sentinel prose would never surface, so this pins the single reader
    the node exists to install.
    """

    def _sentinel(_document: str):
        yield "s2", "prose from the one reader"

    monkeypatch.setattr(dispatch, "section_prose", _sentinel)
    document = (
        "<html><body><main>"
        '<h2 id="s2">&#167;2 &#8212; A section</h2>'
        "<p>body the retired read would have surfaced</p>"
        "</main></body></html>"
    )
    assert dispatch._plan_section_text(document, "2") == "prose from the one reader"


def test_an_identified_element_that_is_not_a_heading_reads_its_own_text():
    """The branch for an identified non-heading element is unchanged."""
    document = (
        "<html><body><main>"
        '<div id="s9">a plain identified element</div>'
        "</main></body></html>"
    )
    assert dispatch._plan_section_text(document, "9") == "a plain identified element"


def test_every_section_text_matches_the_retired_read_up_to_its_exclusions(plans):
    """Every section text equals the retired read, less the reader's exclusions.

    A section whose retired text the reader matches verbatim must match exactly.
    Where a landed card or a marked collection sits inside the section the reader
    subtracts it, so the reader's tokens must be a subsequence of the retired
    tokens — the reader drops text and never adds or reorders it. A plan whose
    markup cannot be reconciled this way is named in :data:`EXEMPT_PLANS`.
    """
    unjustified: list[str] = []
    exact = 0
    subtracted = 0
    for path, source in plans:
        if str(path) in EXEMPT_PLANS:
            continue
        soup = BeautifulSoup(source, "html.parser")
        headings = plan_headings(source)
        for identity in _section_population(source):
            heading = _resolve(source, soup, headings, identity)
            if heading is None:
                continue
            got = _normalise(dispatch._plan_section_text(source, identity) or "")
            expected = _retired_section_text(source, heading)
            if got == expected:
                exact += 1
                continue
            if heading.is_card or _has_marked_machinery(source, heading):
                if _is_subsequence(got.split(), expected.split()):
                    subtracted += 1
                    continue
            unjustified.append(f"{path}#{identity}")
    assert exact, "the parity population must contain a section read verbatim"
    assert subtracted, "the parity population must exercise a machinery subtraction"
    assert not unjustified, (
        "section texts differ from the retired read for a reason other than the "
        "reader's exclusions: " + "; ".join(unjustified)
    )


def test_the_exempt_plan_list_holds_exactly_the_named_parser_divergences():
    assert set(EXEMPT_PLANS) == {"docs/plans/fleet-monitor-legibility.html"}


def test_the_section_text_no_longer_reads_the_heading_span():
    """An authored section's text comes from the one reader, not a soup walk.

    ``_plan_section_text`` keeps its spelling resolution and the branch for an
    identified non-heading element, so the only soup read it retains besides the
    id lookup is that branch's ``identified.get_text``. A heading the reader
    serves takes its text from ``section_prose``; a heading it does not serve
    keeps its own extent's prose through ``_strip_tags``, which carries no
    ``get_text`` call.
    """
    import inspect

    source = inspect.getsource(dispatch._plan_section_text)
    assert "section_prose(" in source
    assert "_strip_tags(html_text[slice(*heading.span)])" in source
    without_branch = source.replace('identified.get_text(" ", strip=True)', "")
    assert ".get_text(" not in without_branch