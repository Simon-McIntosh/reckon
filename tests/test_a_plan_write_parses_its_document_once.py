"""One versioned plan write parses each of its two documents exactly once.

A plan write touches two documents: the file it read, which carries the version
its ``expected_version`` is checked against, and the file it renders, which
carries the heading guard and the section-coverage check. Each pass can build
its own soup and run its own structural scan, and a write that does so pays for
the same text several times over.

A **parse** here is one of the two ways a document's text becomes a tree: a
``BeautifulSoup`` construction over the whole document, and one
``_StructuredSectionSpanParser`` scan of it. Both are counted for every
document the operation reaches, including the reads the edit tool performs
around the store write, because those are part of what one edit costs a caller.
The thousand-character floor keeps prose fragments — a section body, a comment
record — out of the count: those are values the write compares, never documents
it parses.

This measures the number of parses, not elapsed time: the count is the part a
busy shared filesystem turns into latency.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, file_memo
from reckon import mcp as mcp_module

PROJECT = "parse-counts"
PLAN = "thirty-sections"
SECTION_COUNT = 30
#: Shorter texts are prose fragments the write compares, never documents it
#: parses, and are outside what this test counts.
DOCUMENT_FLOOR = 1000

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {"reasoning": "standard", "verification": "strict", "risk": "low"},
}


def _section_ids() -> list[str]:
    return [f"s{index}" for index in range(1, SECTION_COUNT + 1)]


def _write_plan(checkout: Path, *, records_for: set[str] | None = None) -> Path:
    """Author a thirty-section plan file as an earlier writer left it.

    ``records_for`` narrows which sections carry a typed record, so a case can
    author a section whose heading and declaration pair without one — the shape
    a write removes a heading from, where the heading guard's coverage half is
    the check that fires rather than its record half.
    """
    declared = dict.fromkeys(_section_ids(), "implementable")
    recorded = set(declared) if records_for is None else records_for
    body = "".join(
        f'<h2 id="{section}">Section {index}</h2><p>Body of section {index}.</p>'
        for index, section in enumerate(_section_ids(), start=1)
    )
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f'<title>{PLAN}</title></head><body><main class="plan-doc">'
        f"{body}</main></body></html>"
    )
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "Thirty sections",
        "status": "active",
        "modified": "2026-11-01",
        "version": 0,
        "section_declarations": deepcopy(declared),
        "sections": [
            {
                "id": section,
                "effort_hours": 1.0,
                "capability": deepcopy(CAPABILITY),
                "attempts": 0,
                "status": status,
                "links": [],
            }
            for section, status in declared.items()
            if section in recorded
        ],
    }
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


@contextlib.contextmanager
def counted() -> Iterator[dict[str, dict[str, int]]]:
    """Count whole-document parses, keyed by document text then parser kind."""
    counts: dict[str, dict[str, int]] = {}

    def record(kind: str, text: str) -> None:
        if len(text or "") < DOCUMENT_FLOOR:
            return
        entry = counts.setdefault(text, {})
        entry[kind] = entry.get(kind, 0) + 1

    real_soup = _plan_html.BeautifulSoup

    class CountingSoup(real_soup):  # type: ignore[misc, valid-type]
        def __init__(self, markup: str = "", *args: Any, **kwargs: Any) -> None:
            record("soup", markup or "")
            super().__init__(markup, *args, **kwargs)

    real_parser = _plan_html._StructuredSectionSpanParser
    real_feed = real_parser.feed

    def counting_feed(self: Any, data: str) -> Any:
        record("structural", data or "")
        return real_feed(self, data)

    _plan_html.BeautifulSoup = CountingSoup
    real_parser.feed = counting_feed
    try:
        yield counts
    finally:
        _plan_html.BeautifulSoup = real_soup
        real_parser.feed = real_feed


def _version_of(checkout: Path) -> int:
    text = (checkout / "docs" / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    return int(_plan_html.read_state(text)["version"])


def _edit(checkout: Path, *, ops: list[dict], expected_version: int):
    """One state-mode edit through the tool the served write runs."""
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=expected_version,
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=ops,
    )


def _cold() -> None:
    """Forget every parse and memo an earlier case in this process left behind."""
    file_memo.clear()
    _plan_html._plan_headings_of.cache_clear()


def _two_documents_parsed_once(counts: dict[str, dict[str, int]], stored: str) -> None:
    """The write parsed the stored document and the one it rendered, once each."""
    assert len(counts) == 2, (
        "expected the stored document and the document the write renders, got "
        f"{sorted(len(text) for text in counts)}: {counts}"
    )
    assert stored in counts, "the write never parsed the document it read"
    for kind in ("soup", "structural"):
        for text, seen in counts.items():
            assert seen.get(kind, 0) == 1, (
                f"{kind} parse of a {len(text)}-character document ran "
                f"{seen.get(kind, 0)} times: {counts}"
            )


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_plan(root)
    return root


def test_a_no_op_edit_parses_each_document_once(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    stored = path.read_text(encoding="utf-8")
    before = path.read_bytes()
    title = _plan_html.read_state(stored)["title"]
    version = _version_of(checkout)

    _cold()
    with counted() as counts:
        result = _edit(
            checkout,
            ops=[{"op": "set", "path": "title", "value": title}],
            expected_version=version,
        )

    assert result["ok"] is True, result
    assert path.read_bytes() == before, "a no-op edit rewrote the document"
    _two_documents_parsed_once(counts, stored)


def test_a_one_op_edit_parses_each_document_once(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    stored = path.read_text(encoding="utf-8")
    before = path.read_bytes()
    version = _version_of(checkout)

    _cold()
    with counted() as counts:
        result = _edit(
            checkout,
            ops=[{"op": "set", "path": "title", "value": "Revised title"}],
            expected_version=version,
        )

    assert result["ok"] is True, result
    assert path.read_bytes() != before
    _two_documents_parsed_once(counts, stored)


def test_a_stale_expected_version_is_refused_before_anything_is_written(
    checkout: Path,
) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    current = _version_of(checkout)
    before = path.read_bytes()

    _cold()
    result = _edit(
        checkout,
        ops=[{"op": "set", "path": "title", "value": "Unreachable"}],
        expected_version=current + 7,
    )

    assert result["ok"] is False, result
    assert result["error"] == "version_conflict", result
    assert result["message"] == (
        f"Could not edit Thirty sections: expected version {current + 7}, "
        f"but the current version is {current}."
    ), result
    assert path.read_bytes() == before


@pytest.fixture()
def unrecorded_last_section(tmp_path: Path) -> Path:
    """The same plan, with the last section carrying no typed record.

    A heading and its declaration pair without one is the shape the heading
    guard's coverage half exists for, so removing that heading fires
    ``todo-without-section`` rather than the record-adjacency refusal.
    """
    root = tmp_path / "repo"
    _write_plan(root, records_for=set(_section_ids()) - {f"s{SECTION_COUNT}"})
    return root


def test_a_write_removing_a_declared_heading_is_refused(
    unrecorded_last_section: Path,
) -> None:
    checkout = unrecorded_last_section
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    current = _version_of(checkout)
    before = path.read_bytes()

    _cold()
    result = mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=current,
        checkout_path=str(checkout),
        doc_type="plan",
        mode="text",
        old_html=f'<h2 id="s{SECTION_COUNT}">Section {SECTION_COUNT}</h2>',
        new_html=f"<p>Section {SECTION_COUNT} prose without a heading.</p>",
    )

    assert result["ok"] is False, result
    assert result["error"] == "text_edit_error", result
    assert result["detail"] == (
        f"section_declarations names 's{SECTION_COUNT}' but the plan carries no "
        "authored section heading with that id (todo-without-section); remove the "
        "stale declaration or restore the heading"
    ), result
    assert path.read_bytes() == before


def test_a_write_adding_an_undeclared_section_is_refused(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    stored = path.read_text(encoding="utf-8")
    before = path.read_bytes()
    declarations = dict(_plan_html.read_state(stored)["section_declarations"])
    declarations.pop(f"s{SECTION_COUNT}")
    version = _version_of(checkout)

    _cold()
    result = _edit(
        checkout,
        ops=[{"op": "set", "path": "section_declarations", "value": declarations}],
        expected_version=version,
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error", result
    assert result["detail"].startswith(
        f"authored section heading 's{SECTION_COUNT}' carries no "
        "section_declarations entry (section-without-todo);"
    ), result
    assert path.read_bytes() == before
