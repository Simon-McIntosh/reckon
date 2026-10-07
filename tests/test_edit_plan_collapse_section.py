"""The collapse_section op replaces a landed section's body with its card."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon import mcp as mcp_module
from reckon.cli import main

PROJECT = "sample"
PLAN = "demo-plan"
ANCHOR = f"/reckon/evidence/archive/{PLAN}-landed#s2"
SUMMARY = "Built the thing; suite green."
ANCHOR_2 = f"/reckon/evidence/archive/{PLAN}-landed#s2-refreshed"
SUMMARY_2 = "Refreshed after a second pass; the suite is still green."

DECLARATIONS = {"s1": "done", "s2": "implementable"}
CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}
SECTION_RECORDS = [
    {
        "id": section_id,
        "effort_hours": 1.0,
        "capability": deepcopy(CAPABILITY),
        "attempts": 0,
        "status": status,
        "links": [],
    }
    for section_id, status in DECLARATIONS.items()
]

AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-standalone" content="fixture wiring reason">'
    "<title>Demo plan</title></head><body>"
    '<main class="plan-doc">'
    '<h2 id="s1">First section</h2><p>First body.</p>'
    '<h2 id="s2">Second section</h2>'
    "<p>Second body.</p><ul><li>authored detail</li></ul>"
    "</main></body></html>"
)


def _collapse_op(
    section: str = "s2", summary: str = SUMMARY, anchor: str = ANCHOR
) -> dict:
    """A collapse_section request for one landed section."""
    return {
        "op": "collapse_section",
        "section": section,
        "summary": summary,
        "evidence_anchor": anchor,
    }


@pytest.fixture()
def plan(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "repo"
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True)
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "Demo plan",
        "status": "active",
        "modified": "2026-09-25",
        "version": 0,
        "section_declarations": deepcopy(DECLARATIONS),
        "sections": deepcopy(SECTION_RECORDS),
        "decisions": {"transport": {"title": "Which transport?"}},
        "followups": [
            {
                "id": "next-action",
                "status": "open",
                "prompt": f"/reckon-build {PLAN}",
            }
        ],
        "comments": {
            "s1": [
                {
                    "id": "c-one",
                    "who": "reckon-build",
                    "when": "2026-09-25T01:00:00Z",
                    "body": "<p>First section landed.</p>",
                }
            ]
        },
    }
    path.write_text(_plan_html.write_state(AUTHORED, state), encoding="utf-8")
    return checkout, path


THREE_DECLARATIONS = {"s1": "done", "s2": "implementable", "s3": "implementable"}
THREE_AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-standalone" content="fixture wiring reason">'
    "<title>Demo plan</title></head><body>"
    '<main class="plan-doc">'
    '<h2 id="s1">First section</h2><p>First body.</p>'
    '<h2 id="s2">Second section</h2><p>Second body.</p>'
    '<h2 id="s3">Third section</h2><p>Third body.</p>'
    "</main></body></html>"
)


@pytest.fixture()
def three_section_plan(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "repo"
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True)
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "Demo plan",
        "status": "active",
        "modified": "2026-09-25",
        "version": 0,
        "section_declarations": deepcopy(THREE_DECLARATIONS),
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": deepcopy(CAPABILITY),
                "attempts": 0,
                "status": status,
                "links": [],
            }
            for section_id, status in THREE_DECLARATIONS.items()
        ],
        "followups": [
            {"id": "next-action", "status": "open", "prompt": f"/reckon-build {PLAN}"}
        ],
    }
    path.write_text(_plan_html.write_state(THREE_AUTHORED, state), encoding="utf-8")
    return checkout, path


def _edit(checkout: Path, op: dict) -> dict:
    """Apply one op through the plan-editing tool in the fixture checkout."""
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[op],
    )


def _structured_tail(text: str) -> str:
    """The authored structured-state regions (decisions/followups/comments)."""
    starts = [
        text.index(f'<section data-reckon="{name}"')
        for name in ("decisions", "followups", "comments")
    ]
    return text[min(starts) : text.index("</main>")]


def test_collapse_replaces_the_body_and_keeps_the_heading(plan) -> None:
    checkout, path = plan

    result = _edit(checkout, _collapse_op())

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    # The authored prose under the heading is gone.
    assert "Second body." not in text
    assert "authored detail" not in text
    # The heading and its id survive, carried by the landed card.
    assert '<h2 id="s2">Second section</h2>' in text
    # The card carries the badge, the summary and the evidence link.
    assert 'class="section-landed"' in text
    assert "&#10003; landed" in text
    assert SUMMARY in text
    assert f'<a href="{ANCHOR}">full record</a>' in text


def test_collapse_sets_the_section_declaration_to_done(plan) -> None:
    checkout, path = plan

    result = _edit(checkout, _collapse_op())

    assert result["ok"] is True, result
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert state["section_declarations"] == {"s1": "done", "s2": "done"}
    by_id = {record["id"]: record["status"] for record in state["sections"]}
    assert by_id == {"s1": "done", "s2": "done"}


def test_collapse_leaves_structured_state_regions_byte_identical(plan) -> None:
    checkout, path = plan
    before_tail = _structured_tail(path.read_text(encoding="utf-8"))

    result = _edit(checkout, _collapse_op())

    assert result["ok"] is True, result
    assert _structured_tail(path.read_text(encoding="utf-8")) == before_tail


def test_collapse_refuses_a_missing_section_and_leaves_the_file_unchanged(plan) -> None:
    checkout, path = plan
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, _collapse_op(section="s9"))

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "s9" in result["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_collapsed_result_passes_audit_doc_without_a_duplicate_header(plan) -> None:
    checkout, path = plan
    assert _edit(checkout, _collapse_op())["ok"] is True

    audit = CliRunner().invoke(main, ["audit-doc", str(path)])

    assert audit.exit_code == 0, audit.output
    assert "header-duplicate" not in audit.output


def test_collapsing_twice_leaves_one_card_with_the_latest_summary(plan) -> None:
    checkout, path = plan
    assert _edit(checkout, _collapse_op())["ok"] is True

    result = _edit(checkout, _collapse_op(summary=SUMMARY_2, anchor=ANCHOR_2))

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    # Exactly one card remains: the first was replaced cleanly, not nested.
    assert text.count('<section class="section-landed">') == 1
    assert SUMMARY not in text
    assert f'<a href="{ANCHOR}">full record</a>' not in text
    assert SUMMARY_2 in text
    assert f'<a href="{ANCHOR_2}">full record</a>' in text
    assert '<h2 id="s2">Second section</h2>' in text
    audit = CliRunner().invoke(main, ["audit-doc", str(path)])
    assert audit.exit_code == 0, audit.output


@pytest.mark.parametrize("order", [("s2", "s3"), ("s3", "s2")])
def test_collapsing_a_neighbour_keeps_both_cards(three_section_plan, order) -> None:
    checkout, path = three_section_plan
    first, second = order
    assert _edit(checkout, _collapse_op(first))["ok"] is True

    result = _edit(checkout, _collapse_op(second, summary=SUMMARY_2, anchor=ANCHOR_2))

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    assert text.count('<section class="section-landed">') == 2
    assert SUMMARY in text
    assert SUMMARY_2 in text
    assert '<h2 id="s2">Second section</h2>' in text
    assert '<h2 id="s3">Third section</h2>' in text
    audit = CliRunner().invoke(main, ["audit-doc", str(path)])
    assert audit.exit_code == 0, audit.output


def test_a_span_reaching_a_second_heading_is_refused_and_leaves_the_file_unchanged(
    plan,
) -> None:
    checkout, path = plan
    assert _edit(checkout, _collapse_op())["ok"] is True
    text = path.read_text(encoding="utf-8")
    card_close = text.index("</section>", text.index("landed-summary"))
    # One cardinality fault only: a second heading sits in the card's span with
    # no structural boundary (a second card or a structured-state region) beside
    # it, so this exercises the second-heading check on its own.
    extra = '<h2 id="stray-heading">Stray heading</h2>\n'
    path.write_text(text[:card_close] + extra + text[card_close:], encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, _collapse_op(summary=SUMMARY_2, anchor=ANCHOR_2))

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "cannot determine the extent of 's2' unambiguously" in result["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_a_span_reaching_a_structured_region_is_refused_and_leaves_the_file_unchanged(
    plan,
) -> None:
    checkout, path = plan
    assert _edit(checkout, _collapse_op())["ok"] is True
    text = path.read_text(encoding="utf-8")
    card_close = text.index("</section>", text.index("landed-summary"))
    # One fault only: a structured-state region sits in the card's span and
    # carries no heading, so this exercises the boundary check on its own.
    region = '<section data-reckon="evidence"></section>\n'
    path.write_text(text[:card_close] + region + text[card_close:], encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, _collapse_op(summary=SUMMARY_2, anchor=ANCHOR_2))

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "cannot determine the extent of 's2' unambiguously" in result["detail"]
    assert path.read_text(encoding="utf-8") == before


def test_a_card_without_a_closing_tag_is_refused_and_leaves_the_file_unchanged(
    plan,
) -> None:
    checkout, path = plan
    assert _edit(checkout, _collapse_op())["ok"] is True
    text = path.read_text(encoding="utf-8")
    card_close = text.index("</section>", text.index("landed-summary"))
    malformed = text[:card_close] + text[card_close + len("</section>") :]
    path.write_text(malformed, encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, _collapse_op(summary=SUMMARY_2, anchor=ANCHOR_2))

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert path.read_text(encoding="utf-8") == before


def _edit_batch(checkout: Path, ops: list[dict]) -> dict:
    """Apply several ops as one versioned write in the fixture checkout."""
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=ops,
    )


def _evidence_op(anchor: str) -> dict:
    """An append_evidence request writing one section of this plan's record."""
    return {
        "op": "append_evidence",
        "plan": PLAN,
        "anchor": anchor,
        "title": "Second section landed",
        "body": "<p>Built the thing; suite green.</p>",
    }


RECORD_HREF = f"/{PROJECT}/evidence/archive/{PLAN}-landed.html"


def test_a_bare_anchor_links_the_section_appended_in_the_same_batch(plan) -> None:
    checkout, path = plan

    result = _edit_batch(
        checkout, [_evidence_op("s2-landed"), _collapse_op(anchor="s2-landed")]
    )

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    assert f'<a href="{RECORD_HREF}#s2-landed">full record</a>' in text
    record = checkout / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    assert 'id="s2-landed"' in record.read_text(encoding="utf-8")


def test_a_bare_anchor_links_a_section_the_record_already_holds(plan) -> None:
    checkout, path = plan
    assert _edit_batch(checkout, [_evidence_op("s2-landed")])["ok"] is True

    result = _edit(checkout, _collapse_op(anchor="#s2-landed"))

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    assert f'<a href="{RECORD_HREF}#s2-landed">full record</a>' in text


def test_a_bare_anchor_the_record_lacks_is_refused_and_leaves_the_file_unchanged(
    plan,
) -> None:
    checkout, path = plan
    assert _edit_batch(checkout, [_evidence_op("s2-landed")])["ok"] is True
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, _collapse_op(anchor="s2-typo"))

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "s2-typo" in result["detail"]
    assert path.read_text(encoding="utf-8") == before
