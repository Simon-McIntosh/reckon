"""One section-prose reader feeds the digests, the search text and the view.

The reviewer of a plan reads authored text through one reader. Its slices must
reproduce the first node's slicer exactly, or a stored review covers different
bytes than the plan now carries and every landing earns a fresh review. The
checks here pin that parity on every plan in this project, in both card modes,
and pin the two surfaces — the search text and the section view — against the
retired algorithms, held here as reference functions so a plan edit moves both
sides alike instead of tripping a snapshot.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest
from bs4 import BeautifulSoup, Tag

from reckon import _plan_html, mcp_views
from reckon._plan_html import RECKON_ATTRIBUTE, machinery_kind
from reckon.mcp_views import ResourceSelector

ROOT = Path(__file__).resolve().parents[1]
# The merge that landed the first node's slicer; its _plan_html.py is the
# parser output this node's reader must reproduce byte for byte.
FIRST_NODE_MERGE = "6d06a5290"


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _reference_search_text(html_text: str) -> str:
    """The retired search walk: drop script, style and every marked section.

    Every ``section`` carrying the reckon marker is dropped, whatever its kind;
    a ``div``/``p`` note and a landed card's interior are retained and joined
    with the remaining stripped strings. This is the surface's base algorithm,
    kept here as a reference rather than a stored snapshot.
    """
    soup = BeautifulSoup(html_text or "", "html.parser")
    scope = soup.body or soup
    for element in scope.select(
        f"script, style, section[{RECKON_ATTRIBUTE}]:not([{RECKON_ATTRIBUTE}='section'])"
    ):
        element.decompose()
    for element in scope.select(f"section[{RECKON_ATTRIBUTE}='section']"):
        element.decompose()
    return " ".join(scope.stripped_strings)


@pytest.fixture(scope="module")
def plans() -> list[tuple[Path, str]]:
    paths = sorted((ROOT / "docs" / "plans").rglob("*.html"))
    assert paths, "the parity population must contain committed plans"
    return [(path.relative_to(ROOT), path.read_text()) for path in paths]


def _load_first_node_plan_html():
    """Load the first node's _plan_html module from its merge commit."""
    try:
        source = subprocess.run(
            ["git", "show", f"{FIRST_NODE_MERGE}:reckon/_plan_html.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("git object for the first node's parser is unavailable")
    spec = importlib.util.spec_from_loader(
        "first_node_plan_html", loader=None, origin="<first-node>"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["first_node_plan_html"] = module
    exec(compile(source, "<first-node>", "exec"), module.__dict__)
    return module


def _first_node_slices(base, document: str, *, skip_landed_cards: bool):
    """Reproduce the first node's slicer over the base plan_html."""
    headings = [
        heading
        for heading in base.plan_headings(document)
        if heading.level == 2 and heading.identity and not heading.machinery
    ]
    protected = list(
        base.structured_section_spans(
            document,
            every_marked_element=True,
            skip_landed_cards=skip_landed_cards,
        )
    )
    body = re.search(r"<body\b[^>]*>", document, re.IGNORECASE)
    start = body.end() if body else 0
    closing = re.search(r"</body\s*>", document[start:], re.IGNORECASE)
    end = start + closing.start() if closing else len(document)
    cuts = {start, end}
    for left, right in protected + [heading.span for heading in headings]:
        cuts.update((max(start, min(end, left)), max(start, min(end, right))))
    points = sorted(cuts)
    out = []
    for left, right in pairwise(points):
        identity = next(
            (
                heading.identity
                for heading in headings
                if heading.span[0] <= left and right <= heading.span[1]
            ),
            "_document",
        )
        raw = (
            ""
            if any(low <= left and right <= high for low, high in protected)
            else document[left:right]
        )
        out.append((identity, base.strip_tags(raw)))
    return out


def test_section_prose_reproduces_the_first_node_slicer_on_every_plan(plans):
    base = _load_first_node_plan_html()
    for keep_landed_cards in (False, True):
        for path, source in plans:
            got = list(
                _plan_html.section_prose(source, keep_landed_cards=keep_landed_cards)
            )
            expected = _first_node_slices(
                base, source, skip_landed_cards=not keep_landed_cards
            )
            assert got == expected, (path, keep_landed_cards)


def test_section_prose_keys_match_the_first_node_slicer_on_every_plan(plans):
    base = _load_first_node_plan_html()
    for path, source in plans:
        got = [identity for identity, _ in _plan_html.section_prose(source)]
        expected = [
            identity
            for identity, _ in _first_node_slices(base, source, skip_landed_cards=True)
        ]
        assert got == expected, path


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
