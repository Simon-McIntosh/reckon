"""A read the persisted parse answers builds no second tree of the document.

A fresh process answers a plan read from the parse the metadata index already
holds, so the state arrives without touching the document: exactly the point of
persisting it. The read's own unparsed-section diagnostic then asked the soup
whether each empty-parsed section held authored children — one whole-document
BeautifulSoup build per such section, after the persisted parse had already
answered the same document. That second parse is what this test measures.

The measure is the number of trees built from the plan's own text. A read
answered from a seeded persisted parse builds none; the same read with no
persisted parse builds exactly one, which is the instrument's positive control
— the wrapper sees a document tree when a document tree exists. The negative
control restores the soup-per-section count and the first case builds at least
one document tree again.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import bs4
import pytest

import reckon._store as store
from reckon import _plan_html, file_memo, metadata_index

PROJECT = "probe"
SLUG = "read-costs"

#: The plan document the read answers about: one section the parser reads
#: (a canonical gate) and one it cannot read (decisions authored as
#: ``article.r-decision``, which the decisions parser never matches).
DOCUMENT = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
    f'<meta name="docs-project" content="{PROJECT}">\n'
    f'<meta name="plan-slug" content="{SLUG}">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-status" content="active">\n'
    '<meta name="plan-impl" content="0.25">\n'
    '<meta name="plan-version" content="3">\n'
    "<title>Read costs | probe</title></head><body>"
    '<main class="plan-doc">\n'
    '<h2 id="one">One</h2>\n<p>Body of one.</p>\n'
    '<section data-reckon="gates" id="gates">\n'
    '  <div class="r-gate" data-id="g1"><p>A gate</p></div>\n'
    "</section>\n"
    '<h2 id="two">Two</h2>\n<p>Body of two.</p>\n'
    '<section data-reckon="decisions" id="decisions">\n'
    '  <article class="r-decision" data-id="speed"><p>Speed?</p></article>\n'
    '  <article class="r-decision" data-id="backend"><p>Backend?</p></article>\n'
    "</section>\n"
    "</main></body></html>\n"
)

#: The warning the base revision returns for the section it cannot read.
EXPECTED_WARNING = (
    'plan read: section data-reckon="decisions" holds 2 authored child '
    "element(s) the parser did not recognise — the parsed collection is empty."
    ' Expected spelling: <div class="r-dec" data-key="...">'
)


def _tree(tmp_path: Path) -> Path:
    """A scratch checkout whose single plan is ``DOCUMENT``."""

    root = tmp_path / "tree"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{SLUG}.html").write_text(DOCUMENT, encoding="utf-8")
    return root


@pytest.fixture()
def document_trees(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Count every soup built, keyed by the text it was built from.

    Both bindings are wrapped: ``_plan_html`` imports the class once at module
    scope, while ``_store`` imports it inside the function that uses it, so a
    wrapper on one binding alone would count half the trees.
    """

    counts: Counter = Counter()
    original = bs4.BeautifulSoup

    class Counting(original):  # type: ignore[misc, valid-type]
        def __init__(self, markup="", *args, **kwargs):
            counts[str(markup)] += 1
            super().__init__(markup, *args, **kwargs)

    monkeypatch.setattr(bs4, "BeautifulSoup", Counting)
    monkeypatch.setattr(_plan_html, "BeautifulSoup", Counting)
    return counts


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config home of our own, so no operator store is read or written."""

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    metadata_index.clear()
    file_memo.clear()
    return config


def _seed_persisted_parse(root: Path) -> None:
    """Persist the discovery parse of the tree, as a serving process would.

    The read under measure is the one that follows, so the trees this seeding
    parse builds are counted by the caller's clear rather than by the wrapper.
    """

    seeded = metadata_index.discovery_state(root / "docs", PROJECT)
    assert seeded, "the persisted parse must cover the plan for the read to reuse it"


def _read(root: Path) -> tuple[dict, int]:
    return store.read_plan(PROJECT, SLUG, root=root)


def _warnings(state: dict) -> list[str]:
    return [
        w
        for w in state.get("compatibility_warnings") or []
        if w.startswith("plan read:")
    ]


def test_a_read_the_persisted_parse_answers_builds_no_document_tree(
    tmp_path: Path, document_trees: Counter, home: Path
) -> None:
    """The read answers from the store and builds no tree of the document."""

    root = _tree(tmp_path)
    _seed_persisted_parse(root)
    document_trees.clear()  # the seeding parse is the parse the read reuses

    state, version = _read(root)

    assert version == 3, "the read did not answer from the persisted parse"
    assert _warnings(state) == [EXPECTED_WARNING], _warnings(state)
    assert document_trees[DOCUMENT] == 0, (
        "the read built "
        f"{document_trees[DOCUMENT]} tree(s) from the plan document; "
        f"every tree built was {dict(document_trees)}"
    )


def test_without_a_persisted_parse_the_read_parses_once_and_warns_the_same(
    tmp_path: Path, document_trees: Counter, home: Path
) -> None:
    """The positive control: no store, one document tree, identical warnings.

    This is the same instrument on the same document with the answer taken
    away, so the zero above is a fact about the store rather than about a
    wrapper that never sees a document build.
    """

    root = _tree(tmp_path)

    state, version = _read(root)

    assert version == 3
    assert _warnings(state) == [EXPECTED_WARNING], _warnings(state)
    assert document_trees[DOCUMENT] == 1, (
        "a read with no persisted parse builds the document exactly once; "
        f"every tree built was {dict(document_trees)}"
    )


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        # Two direct children; the nested paragraph belongs to the first.
        (
            (
                '<section data-reckon="decisions"><div class="a"></div>'
                '<div class="b"><p>x</p></div></section>'
            ),
            2,
        ),
        # Structural children are regenerated by the renderers, not authored.
        (
            '<section data-reckon="decisions"><h3>t</h3><header>x</header></section>',
            0,
        ),
        # Only direct children count; a grandchild is not one of them.
        (
            (
                '<section data-reckon="questions"><div><div></div></div>'
                "<div></div></section>"
            ),
            2,
        ),
        # Void elements are children but enclose nothing.
        ('<section data-reckon="comments"><img src="x"><br></section>', 2),
        # A self-closing child is one child.
        ('<section data-reckon="research"><div/><div></div></section>', 2),
        # The first section carrying a value is the one the diagnostic names.
        (
            (
                '<section data-reckon="followups"><div class="a"></div></section>'
                '<section data-reckon="followups"><div></div><div></div></section>'
            ),
            1,
        ),
        # A section the diagnostic does not name contributes no count.
        (
            '<section data-reckon="section" data-id="s1"><div></div></section>',
            0,
        ),
        # Authored content outside any recognised section is nobody's child.
        ("<p>loose</p><div></div>", 0),
        # Only a section holds a section's children; an element that merely
        # carries the value is a different element and holds none of them.
        ('<div data-reckon="decisions"><div></div><div></div></div>', 0),
        ('<article data-reckon="decisions"><div></div></article><p></p>', 0),
        # A non-section claim must not stand in for the real section either.
        (
            (
                '<div data-reckon="decisions"><div></div><div></div></div>'
                '<section data-reckon="decisions"><div class="r-dec"></div></section>'
            ),
            1,
        ),
    ],
)
def test_the_raw_count_matches_the_soup_walk(markup: str, expected: int) -> None:
    """The diagnostic's raw count is the soup walk's count, case by case.

    The read dropped the soup to answer this question, so the two must agree —
    including on the shapes that tell a direct child from a nested one and a
    structural child from an authored one.
    """

    reckon_id = "decisions"
    if 'data-reckon="' in markup:
        reckon_id = markup.split('data-reckon="', 1)[1].split('"', 1)[0]

    counts = store._authored_child_counts(markup, _plan_html.SECTION_IDS)

    assert counts.get(reckon_id, 0) == expected
    # The soup walk is the definition of this measure, so it is asked on every
    # shape the diagnostic names — including the ones where the two could
    # disagree about which element holds a section's children. An id outside
    # that set is one the diagnostic never reports on, so there is no count of
    # its to compare.
    if reckon_id in _plan_html.SECTION_IDS:
        assert _plan_html._authored_child_count(markup, reckon_id) == expected


def test_a_section_id_on_another_element_is_not_a_section(
    tmp_path: Path, home: Path
) -> None:
    """A document that spells a section id on a div warns about nothing.

    Only a ``section`` holds a section's authored children, so an element that
    merely carries the value must not be counted. The read would otherwise
    report children the write-refusal path, which selects on ``section``, does
    not see — two answers about one document.
    """

    root = tmp_path / "tree"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{SLUG}.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f'<meta name="plan-slug" content="{SLUG}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0.25">'
        '<meta name="plan-version" content="1">'
        '<title>t</title></head><body><main class="plan-doc">'
        '<div data-reckon="decisions"><article></article><article></article></div>'
        "</main></body></html>",
        encoding="utf-8",
    )

    state, _version = _read(root)

    assert _warnings(state) == []
    # The write-refusal path's own count agrees: there is no such section.
    html = (plans / f"{SLUG}.html").read_text(encoding="utf-8")
    assert _plan_html._authored_child_count(html, "decisions") == 0


def test_restoring_the_soup_count_builds_a_document_tree_again(
    tmp_path: Path, document_trees: Counter, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: counting through the soup costs a document tree.

    Restoring the soup-per-section count the diagnostic used to make — the
    mutation this node's measure is judged against — puts a whole-document
    build back into the read, so the zero the first case asserts is a real
    absence and not one the counter cannot see.
    """

    root = _tree(tmp_path)
    _seed_persisted_parse(root)
    document_trees.clear()  # the seeding parse is the parse the read reuses

    def soup_per_section(html_text: str, section_ids) -> dict[str, int]:
        return {
            reckon_id: _plan_html._authored_child_count(html_text, reckon_id)
            for reckon_id in section_ids
        }

    monkeypatch.setattr(store, "_authored_child_counts", soup_per_section)

    state, _version = _read(root)

    assert _warnings(state) == [EXPECTED_WARNING], _warnings(state)
    assert document_trees[DOCUMENT] >= 1, "the restored soup count built no tree"
