"""Both plan reads carry a derived, read-only section checklist.

The plan opens with one todo per authored section. The two readers — the typed
read (:func:`reckon._store.read_plan`) and the payload the SPA fetches
(:func:`reckon._plan_html.parse_plan`) — call one derivation at their own
boundary, so both carry the same ``todos`` field. Nothing is stored: the field
is derived on read, so it never enters the review fingerprint and the write
schema drops it rather than rejecting it.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from reckon import _plan_html, _store
from reckon._plan_html import (
    SECTION_DECLARATION_DEFERRED,
    SECTION_DECLARATION_DONE,
    SECTION_DECLARATION_IMPLEMENTABLE,
    open_sections,
    parse_plan,
    read_state,
    write_state,
)
from reckon._schema import PlanState
from reckon.crew.plan_review import plan_fingerprint

PROJECT = "todo-project"
SLUG = "todo-shape"

AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-standalone" content="fixture wiring reason">'
    "<title>Todo fixture</title></head><body>"
    '<main class="plan-doc">'
    '<h2 id="s1">§1 — The landed section</h2><p>First section prose.</p>'
    '<h2 id="s2">§2 — The open section</h2><p>Second section prose.</p>'
    '<h2 id="s3">§3 — The deferred section</h2><p>Third section prose.</p>'
    "</main></body></html>"
)

DECLARATIONS = {
    "s1": SECTION_DECLARATION_DONE,
    "s2": SECTION_DECLARATION_IMPLEMENTABLE,
    "s3": SECTION_DECLARATION_DEFERRED,
}

CLOSE_COMMENT = {
    "id": "c-close-s1",
    "who": "crew-worker",
    "when": "2026-10-07",
    "quote": None,
    "body": "<p>§1 landed; evidence attached.</p>",
}
PROMOTION_COMMENT = {
    "id": "c-run-abc123",
    "who": "reckon-build",
    "when": "2026-10-08",
    "quote": None,
    "body": "<p>promotion wrote this after the close.</p>",
}


def _plan_state() -> dict:
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": SLUG,
        "title": "Todo fixture",
        "status": "active",
        "version": 0,
        "section_declarations": deepcopy(DECLARATIONS),
        # The closing record comes first, then a promotion comment appended
        # after it: the checklist must select the close by id, never by recency.
        "comments": {"s1": [deepcopy(CLOSE_COMMENT), deepcopy(PROMOTION_COMMENT)]},
    }


def _canonical_html() -> str:
    """The fixture as the writer renders it, so a no-change write is stable."""
    return write_state(AUTHORED, _plan_state())


@pytest.fixture()
def plan_file(tmp_path: Path) -> Path:
    """A checkout whose docs carry the three-section fixture plan."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / f"{SLUG}.html"
    path.write_text(_canonical_html(), encoding="utf-8")
    return path


def _checklist(data: dict) -> list[dict]:
    return list(data.get("todos") or [])


def test_both_readers_carry_the_checklist(plan_file: Path) -> None:
    # The typed read: _store.read_plan reaches the plan through read_state and
    # renders it through mcp_views, so the derivation must reach this boundary.
    typed, _version = _store.read_plan(PROJECT, SLUG, root=plan_file.parents[2])
    # The SPA payload: parse_plan backs GET /plan/<project>/<slug>.
    payload = parse_plan(plan_file)

    for reader, data in (("typed", typed), ("spa", payload)):
        todos = _checklist(data)
        assert [entry["id"] for entry in todos] == ["s1", "s2", "s3"], reader
        assert [entry["link"] for entry in todos] == ["#s1", "#s2", "#s3"], reader
        assert [entry["declaration"] for entry in todos] == [
            SECTION_DECLARATION_DONE,
            SECTION_DECLARATION_IMPLEMENTABLE,
            SECTION_DECLARATION_DEFERRED,
        ], reader
        assert "§1" in todos[0]["heading"], reader
        # The done entry carries its closing record, not the promotion comment
        # appended after it.
        assert todos[0]["close"]["id"] == "c-close-s1", reader
        assert todos[1]["close"] is None, reader
        assert todos[2]["close"] is None, reader


def test_open_sections_are_the_implementable_and_deferred(
    plan_file: Path,
) -> None:
    state = read_state(plan_file.read_text(encoding="utf-8"))

    assert open_sections(state) == ["s2", "s3"]


def test_ticking_a_section_leaves_the_review_fingerprint_unchanged(
    plan_file: Path,
) -> None:
    before_html = plan_file.read_text(encoding="utf-8")
    before = plan_fingerprint(before_html)

    state = read_state(before_html)
    _store.apply_ops(
        state,
        [
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": SECTION_DECLARATION_DONE,
            }
        ],
        False,
    )
    after_html = write_state(before_html, state)

    # The tick landed, so the comparison below is not vacuous.
    assert (
        read_state(after_html)["section_declarations"]["s2"] == SECTION_DECLARATION_DONE
    )
    assert plan_fingerprint(after_html) == before


def test_round_trip_is_byte_identical_and_schema_accepts_todos(
    plan_file: Path,
) -> None:
    text = plan_file.read_text(encoding="utf-8")
    state = read_state(text)

    # A read-modify-write with no change reproduces the document exactly.
    assert write_state(text, state) == text

    # The derived field is not stored: the schema drops it rather than refusing
    # the state that carries it, and a write round-trips it away.
    carried = dict(state)
    carried["todos"] = _plan_html.derive_section_todos(text, state)
    assert carried["todos"], "the fixture derives a checklist"
    validated = PlanState.model_validate(carried)
    assert "todos" not in validated.model_dump()
    assert write_state(text, carried) == text


def test_payload_is_json_serialisable(plan_file: Path) -> None:
    # The SPA payload crosses an HTTP boundary, so the checklist must encode.
    payload = parse_plan(plan_file)
    encoded = json.dumps(payload["todos"])
    assert "c-close-s1" in encoded
