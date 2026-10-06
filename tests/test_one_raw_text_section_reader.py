"""Raw section offsets preserve authored bytes across every reader."""

from __future__ import annotations

import ast
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from reckon import _plan_html, _store, interface_counts

ROOT = Path(__file__).resolve().parents[1]


class _ProtectedSpanReference(HTMLParser):
    """Locate owned section spans and section-heading attribute spans."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer(r"\n", source))
        self.section_stack: list[tuple[bool, int]] = []
        self.spans: list[tuple[int, int]] = []
        self.headings: list[tuple[str, int, int, int]] = []
        self.heading_start: tuple[str, int, int] | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h2":
            start = self._offset()
            end = start + len(self.get_starttag_text())
            values = dict(attrs)
            self.heading_start = (values.get("id") or "", start, end)
            if values.get("data-reckon") == "section":
                self.spans.append((start, end))
        if tag == "section":
            protected = any(name == "data-reckon" for name, _value in attrs)
            self.section_stack.append((protected, self._offset()))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "section" or not any(name == "data-reckon" for name, _value in attrs):
            return
        start = self._offset()
        self.spans.append((start, start + len(self.get_starttag_text())))

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self.heading_start is not None:
            end = self.source.find(">", self._offset()) + 1
            self.headings.append((*self.heading_start, end))
            self.heading_start = None
        if tag != "section" or not self.section_stack:
            return
        protected, start = self.section_stack.pop()
        if not protected:
            return
        close_start = self._offset()
        close_end = self.source.find(">", close_start)
        self.spans.append((start, len(self.source) if close_end < 0 else close_end + 1))


def _reference_protected_spans(html_text: str) -> tuple[tuple[int, int], ...]:
    """Return source ranges that authored-text operations must not overlap."""

    parser = _ProtectedSpanReference(html_text)
    parser.feed(html_text)
    parser.close()
    for protected, start in parser.section_stack:
        if protected:
            parser.spans.append((start, len(html_text)))
    return tuple(sorted(parser.spans))


@pytest.fixture(scope="module")
def documents():
    paths = sorted((ROOT / "docs" / "plans").rglob("*.html"))
    assert paths, "the parity population must contain committed plans"
    return [(path.relative_to(ROOT), path.read_text()) for path in paths]


def test_protected_span_projection_matches_reference_on_every_plan(documents):
    protected = 0
    for path, source in documents:
        expected = _reference_protected_spans(source)
        assert _plan_html.structured_section_spans(source) == expected, path
        protected += len(expected)
    assert protected > len(documents)


def test_raw_heading_ids_match_soup_order_on_every_plan(documents):
    identified = 0
    for path, source in documents:
        expected = [
            str(tag.get("id") or "")
            for tag in BeautifulSoup(source, "html.parser").find_all("h2", id=True)
        ]
        actual = [
            str(heading.own_id or "")
            for heading in _plan_html.plan_headings(source)
            if heading.level == 2 and heading.own_id is not None
        ]
        assert actual == expected, path
        identified += len(expected)
    assert identified > len(documents)


@pytest.mark.parametrize("wrapper", [False, True])
def test_collapse_replaces_exactly_the_reader_span(wrapper):
    heading = (
        "<h2>Authored <em>section</em></h2>"
        if wrapper
        else '<h2 id="s5.1">Authored <em>section</em></h2>'
    )
    section = (
        heading
        + '<section data-reckon="section" data-id="s5-1"></section><p>Remove this body.</p>'
    )
    if wrapper:
        section = '<section id="s5.1">' + section + "</section>"
    source = (
        "<main><p>Before.</p>" + section + '<h2 id="tail">Tail</h2><p>Keep.</p></main>'
    )
    selected = next(
        item for item in _plan_html.plan_headings(source) if item.identity == "s5-1"
    )
    assert source[slice(*selected.span)] == section
    assert selected.raw_id == "s5.1"
    assert selected.text == "Authored section"
    request = {
        "section": "s5-1",
        "summary": "Delivered.",
        "evidence_anchor": "/sample/evidence/record#section",
    }
    result = _store._collapse_authored_section(source, request)
    landed = next(
        item for item in _plan_html.plan_headings(result) if item.identity == "s5-1"
    )
    assert landed.is_card
    assert result[: landed.span[0]] == source[: selected.span[0]]
    assert result[landed.span[1] :] == source[selected.span[1] :]
    assert "Remove this body." not in result
    assert _plan_html.landed_section_ids(result) == {"s5-1"}


def test_all_levels_raw_attributes_and_wrapper_precedence():
    source = '<section id="s4.1"><h2>Wrapper</h2><h3 id="detail">Detail</h3><p>Text</p></section><h2 id="">Empty</h2>'
    wrapper, detail, empty = _plan_html.plan_headings(source)
    assert (wrapper.identity, wrapper.raw_id, wrapper.level) == ("s4-1", "s4.1", 2)
    assert source[slice(*wrapper.identity_span)].startswith("<section")
    assert (detail.identity, detail.level) == ("detail", 3)
    assert detail.identity_span == detail.heading_span
    assert empty.own_id == ""
    assert _plan_html.section_record_id({}, {"id": "s4.1"}) == "s4-1"
    assert _plan_html.section_record_id({"id": "own"}, {"id": "other"}) == "own"
    assert (
        _plan_html.section_record_id(
            {}, {_plan_html.RECKON_ATTRIBUTE: "comments", "id": "other"}
        )
        == ""
    )


def test_machinery_marker_distinguishes_presence_from_kind():
    assert _plan_html.machinery_kind({}) is None
    assert _plan_html.machinery_kind([(_plan_html.RECKON_ATTRIBUTE, None)]) == ""
    assert (
        _plan_html.machinery_kind({_plan_html.RECKON_ATTRIBUTE: "section"}) == "section"
    )


@pytest.mark.parametrize("body", ["<h2>Nested</h2>", "<h3>Nested</h3>"])
def test_fragment_guard_refuses_nested_headings(body):
    with pytest.raises(_store.OpError, match="must not contain another"):
        _store._insert_authored_section(
            "<main></main>", {"id": "new-section", "title": "Title", "body": body}
        )


def test_ambiguous_card_is_refused():
    source = '<main><section class="section-landed"><h2 id="s1">First</h2><h2 id="s2">Second</h2></section></main>'
    assert _plan_html.landed_section_ids(source) == set()
    with pytest.raises(_store.OpError, match="unambiguously"):
        _store._collapse_authored_section(
            source,
            {
                "section": "s1",
                "summary": "Done",
                "evidence_anchor": "/sample/evidence/record",
            },
        )


@pytest.mark.parametrize(
    "statement",
    [
        'soup.find_all("h2", id=True)',
        'soup.select("h2[id], section[id]")',
        'soup.find(re.compile(r"^h[1-6]$"))',
        'soup.find_next_sibling("h3")',
        'text.find("</h2>")',
        "_H2_OPEN_RE.search(text)",
        'tag == "h2"',
        'tag in {"h1", "h2"}',
        're.fullmatch(r"h[1-6]", tag)',
        'pattern = re.compile(r"<h2\\b"); pattern.search(text)',
    ],
)
def test_heading_rule_sees_each_recognition_form(statement):
    tree = ast.parse("def recognise():\n    " + statement + "\n")
    assert interface_counts._heading_walks(interface_counts.definitions(tree))


def test_heading_rule_reports_only_the_parser_and_shape_exemptions():
    found = {}
    for path in sorted((ROOT / "reckon").rglob("*.py")):
        tree = ast.parse(path.read_text())
        rows = interface_counts._heading_walks(interface_counts.definitions(tree), tree)
        if rows:
            found[str(path.relative_to(ROOT))] = rows
    assert set(found) == {"reckon/_plan_html.py"}, found
    assert {name for name, _line in found["reckon/_plan_html.py"]} == {
        "_StructuredSectionSpanParser.handle_starttag",
        "_read_section_records",
        "_splice_section_records",
        "_authored_child_count",
    }, found
    shape_sites = [
        name
        for name, _line in found["reckon/_plan_html.py"]
        if not name.startswith("_StructuredSectionSpanParser.")
    ]
    assert sorted(shape_sites) == sorted(
        [
            "_read_section_records",
            "_read_section_records",
            "_splice_section_records",
            "_authored_child_count",
        ]
    )


def test_heading_rule_follows_module_collections_and_local_aliases():
    source = 'HEADINGS = frozenset({"h2", "h3"})\ndef recognise(tag):\n    names = HEADINGS\n    return tag in names\n'
    tree = ast.parse(source)
    assert interface_counts._heading_walks(
        interface_counts.definitions(tree), tree
    ) == [("recognise", 4)]


def test_card_summary_subheadings_do_not_become_landed_sections():
    source = '<section class="section-landed"><h2 id="work">Work</h2><h3 id="detail">Detail</h3></section>'
    assert _plan_html.landed_section_ids(source) == {"work"}


@pytest.mark.parametrize("wrapper", [False, True])
def test_section_view_uses_normalised_identity_and_raw_element(wrapper):
    from reckon.mcp_views import ResourceSelector, _section_response

    section = (
        "<h2>Chosen</h2><p>Chosen body.</p>"
        if wrapper
        else '<h2 id="s5.1">Chosen</h2><p>Chosen body.</p>'
    )
    if wrapper:
        section = '<section id="s5.1">' + section + "</section>"
    source = "<main>" + section + '<h2 id="s6">Next</h2><p>Next body.</p></main>'
    for spelling in ("s5.1", "s5-1"):
        response = _section_response(
            ResourceSelector("sample", "plan", "subject"),
            1,
            {},
            section=spelling,
            html_text=source,
        )
        assert response["section"]["id"] == "s5-1"
        assert response["section"]["text"] == "Chosen Chosen body."


def test_level_bound_keeps_nested_subsections_and_excludes_next_peer():
    from reckon.crew.dispatch import _plan_section_text

    source = '<main><h1 id="title">Title</h1><h2 id="work">Work</h2><p>Body.</p><section id="detail"><h3>Detail</h3><p>Nested.</p></section><section id="peer"><h2>Peer</h2><p>Next.</p></section></main>'
    assert _plan_section_text(source, "work") == "Work Body. Detail Nested."
    assert _plan_section_text(source, "detail") == "Detail Nested."
    assert _plan_section_text(source, "peer") == "Peer Next."
    assert (
        _plan_section_text(source, "title")
        == "Title Work Body. Detail Nested. Peer Next."
    )


@pytest.mark.parametrize(
    "identified", ['<h2 id="chosen">By id</h2>', '<div id="chosen">By id</div>']
)
def test_explicit_identity_takes_precedence_over_heading_text(identified):
    from reckon.crew.dispatch import _plan_section_text

    source = "<h2>chosen</h2><p>By text</p>" + identified
    assert _plan_section_text(source, "chosen") == "By id"


def test_identity_functions_and_importers_have_one_owner():
    moved = {
        "section_record_id",
        "section_id_candidates",
        "section_anchor",
        "landed_section_ids",
    }
    definitions = {name: [] for name in moved}
    for path in (ROOT / "reckon").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in moved:
                definitions[node.name].append(str(path.relative_to(ROOT)))
            if isinstance(node, ast.ImportFrom) and any(
                alias.name in moved for alias in node.names
            ):
                assert node.module == "reckon._plan_html", (path, node.lineno)
    assert definitions == {name: ["reckon/_plan_html.py"] for name in moved}


def test_markup_tokens_and_heading_record_type_stay_in_the_parser():
    tokens = {_plan_html.RECKON_ATTRIBUTE, _plan_html.LANDED_SECTION_CLASS}
    owners = {token: [] for token in tokens}
    record_name = type(_plan_html.plan_headings('<h2 id="work">Work</h2>')[0]).__name__
    assert record_name.startswith("_")
    for path in (ROOT / "reckon").rglob("*.py"):
        source = path.read_text()
        if path.name != "_plan_html.py":
            assert record_name not in source, path
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in tokens
            ):
                owners[node.value].append(str(path.relative_to(ROOT)))
    assert owners == {token: ["reckon/_plan_html.py"] for token in tokens}


def test_typed_section_identity_round_trips_through_the_same_derivation():
    source = '<main><h2 id="s5.1">Work</h2><section data-reckon="section" data-id="s5-1" data-effort-hours="1" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="standard" data-capability-verification="strict" data-capability-risk="low" data-status="implementable"></section><p>Keep.</p></main>'
    state = _plan_html.read_state(source)
    assert state["sections"][0]["id"] == "s5-1"
    state["sections"][0]["effort_hours"] = 2
    updated = _plan_html.write_state(source, state)
    assert '<h2 id="s5.1">Work</h2>' in updated
    assert updated.count('data-id="s5-1"') == 1
    assert _plan_html.read_state(updated)["sections"][0]["effort_hours"] == 2


def test_raw_heading_id_presence_is_not_a_typed_record_attribute():
    source = (
        '<h2 id>Present but empty</h2><h2 data-id="record-only">No authored id</h2>'
    )
    expected = [
        str(tag.get("id") or "")
        for tag in BeautifulSoup(source, "html.parser").find_all("h2", id=True)
    ]
    headings = _plan_html.plan_headings(source)
    assert (
        [heading.own_id for heading in headings if heading.own_id is not None]
        == expected
        == [""]
    )
    assert headings[1].identity == ""


def test_a_heading_record_requires_its_own_authored_id():
    source = '<h2 data-reckon="section" data-id="record-only" data-effort-hours="1" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="standard" data-capability-verification="strict" data-capability-risk="low" data-status="implementable">Work</h2>'
    with pytest.raises(ValueError, match="id"):
        _plan_html.read_state(source)
