"""A typed section record attaches to a heading the file already carries.

A section is an authored heading plus the typed record that states its effort,
capability and links. A plan written before the record existed has the heading
and lacks the record, so the state-mode append target must accept the record
alone and write it beside that heading without touching a byte of the heading,
its attributes or the prose around it.

Every specimen is synthesised into a temporary plan so the heading asserted
here is one this test wrote: a check run against a live plan turns red the day
that plan is legitimately edited, and reads as a defect in the writer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from reckon import _store as store
from reckon import mcp
from reckon._schema import PlanState
from reckon.doccheck import audit_file

# The classes and the vocabulary are reached through their modules rather than
# bound at import: a fixture elsewhere in the suite reloads these two modules in
# place to re-point the mounts, and a reload redefines every class in them, so a
# name bound here would be a class the writer no longer raises.

_HEADING = '<h2 id="s1">§1 — Existing work</h2>'
_PROSE = '<p class="deliverable">Existing prose stays.</p>'

#: The span the attach writes, matched without its attributes so the assertion
#: reads whether a record was written beside the heading, not how it rendered.
_RECORD_ELEMENT = re.compile(
    r'<section data-reckon="section" data-id="s1"[^>]*></section>'
)


def _plan_html(*, declarations: dict[str, str] | None = None) -> str:
    declaration_meta = ""
    if declarations is not None:
        declaration_meta = (
            '<meta name="plan-section-declarations" content=\''
            + json.dumps(declarations)
            + "'>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="sample">'
        '<meta name="plan-title" content="Sample work">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="0">'
        + declaration_meta
        + "<title>Sample work</title></head><body>"
        + f'<main class="plan-doc">\n{_HEADING}\n{_PROSE}\n</main></body></html>'
    )


def _write_plan(root: Path, *, declarations: dict[str, str] | None = None) -> Path:
    path = root / "docs" / "plans" / "sample.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html(declarations=declarations))
    return root


@pytest.fixture
def plan(tmp_path: Path) -> tuple[Path, Path]:
    root = _write_plan(tmp_path)
    return root, root / "docs" / "plans" / "sample.html"


def _item(**updates) -> dict:
    """The record a reader holds for a section whose heading is in the file."""
    return {
        "id": "s1",
        "effort_hours": 1.25,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "standard",
                "verification": "strict",
                "risk": "low",
            },
        },
        "links": ["inputs#ready"],
        **updates,
    }


def _append(root: Path, *items: dict) -> int:
    working, version = store.read_plan("sample", "sample", root=root)
    store.apply_ops(
        working,
        [{"op": "append", "target": "sections", "item": item} for item in items],
        is_index=False,
    )
    PlanState.model_validate(working).validate_for_write()
    return store.write_plan("sample", "sample", working, version, root=root)


def _body(html_text: str) -> str:
    """The authored region between <main> and </main>, the metas excluded."""
    return html_text[html_text.index("<main") : html_text.index("</main>")]


def test_attach_writes_the_record_and_leaves_the_heading_and_prose_alone(plan):
    root, path = plan
    before = path.read_text()

    assert _append(root, _item()) == 1

    after = path.read_text()
    element = _RECORD_ELEMENT.search(after)
    assert element is not None, "the attach must write the typed record"

    # The only bytes added inside the document body are the record span and the
    # line break it lands on, so removing exactly that span returns the authored
    # heading and prose to the bytes this test wrote.
    assert _body(after).replace("\n" + element.group(0), "", 1) == _body(before)
    assert _HEADING in after
    assert _PROSE in after

    # The record rides on the line directly after the heading, before the prose.
    # The reader recovers a record only from there, so a span written anywhere
    # else would be silently dropped on the next read.
    lines = _body(after).splitlines()
    assert lines[lines.index(_HEADING) + 1] == element.group(0)
    state, version = store.read_plan("sample", "sample", root=root)
    assert version == 1
    assert state["sections"] == [{**_item(), "attempts": 0, "status": "implementable"}]
    assert state["section_declarations"] == {"s1": "implementable"}


def test_audit_stops_reporting_the_section_once_its_record_attaches(plan):
    root, path = plan
    before = [f for f in audit_file(path) if f.code == "section-without-contract"]
    assert [f.severity for f in before] == ["warn"]
    assert "s1" in before[0].message

    _append(root, _item())

    after = [f for f in audit_file(path) if f.code == "section-without-contract"]
    assert after == []


def test_a_record_for_an_id_with_no_heading_is_refused_naming_it(plan):
    root, path = plan
    before = path.read_text()

    with pytest.raises(store.OpError) as refused:
        _append(root, _item(id="s5"))

    message = str(refused.value)
    assert "s5" in message
    assert "no authored h2 heading" in message
    assert "'title' and 'body'" in message
    assert path.read_text() == before
    assert store.read_plan("sample", "sample", root=root)[1] == 0


def test_a_second_record_for_an_id_that_carries_one_is_refused_as_a_duplicate(plan):
    root, path = plan
    _append(root, _item())
    written = path.read_text()

    with pytest.raises(store.OpError) as refused:
        _append(root, _item(effort_hours=2.0))

    message = str(refused.value)
    assert "s1" in message
    assert "already exists" in message
    assert path.read_text() == written


def test_attach_takes_the_status_the_plan_already_declares(tmp_path: Path):
    root = _write_plan(tmp_path, declarations={"s1": "done"})

    assert _append(root, _item()) == 1

    state, _ = store.read_plan("sample", "sample", root=root)
    assert [record["status"] for record in state["sections"]] == ["done"]


def test_an_item_carrying_a_body_still_authors_the_heading_and_prose(plan):
    root, path = plan

    assert (
        _append(root, _item(id="s2", title="New work", body="<p>New prose.</p>")) == 1
    )

    html = path.read_text()
    assert '<h2 id="s2">New work</h2>' in html
    assert "<p>New prose.</p>" in html
    assert _body(html).count(_HEADING) == 1


def test_the_op_vocabulary_names_the_record_fields_on_both_write_routes():
    for op in ("insert_section", "append"):
        entry = mcp._OP_VOCAB[op]
        for field in ("effort_hours", "capability", "links"):
            assert field in entry, f"{op} vocabulary omits {field!r}"
        assert "required" in entry, f"{op} vocabulary omits what is required"
    assert "sections" in mcp._OP_VOCAB["append"]
