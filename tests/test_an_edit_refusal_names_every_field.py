"""An edit refusal names every missing field at once, not one per round trip.

``append_evidence``, ``close`` and ``append`` each check several required
fields. Collecting them before raising means a caller learns every problem in
one response instead of retrying to discover the next one, while a call with a
single problem keeps exactly the message it gave before.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon import mcp as mcp_module

PROJECT = "sample"
PLAN = "demo-plan"

DECLARATIONS = {"s1": "done"}
CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}


def _plan_state() -> dict:
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "Demo plan",
        "status": "active",
        "modified": "2026-09-25",
        "version": 0,
        "section_declarations": deepcopy(DECLARATIONS),
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": deepcopy(CAPABILITY),
                "attempts": 0,
                "status": status,
                "links": [],
            }
            for section_id, status in DECLARATIONS.items()
        ],
        "followups": [
            {"id": "next-action", "status": "open", "prompt": f"/reckon-build {PLAN}"}
        ],
    }


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    path = repo / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True)
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        "<title>Demo plan</title></head><body>"
        '<main class="plan-doc">'
        '<h2 id="s1">First section</h2><p>First body.</p>'
        "</main></body></html>"
    )
    path.write_text(_plan_html.write_state(authored, _plan_state()), encoding="utf-8")
    return repo


def _edit(checkout: Path, op: dict) -> dict:
    plan_path = checkout / "docs" / "plans" / f"{PLAN}.html"
    state = _plan_html.read_state(plan_path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[op],
    )


def _detail(result: dict) -> str:
    assert result["ok"] is False, result
    assert result["error"] == "op_error", result
    return str(result["detail"])


def test_append_evidence_with_every_field_missing_names_all_four(checkout) -> None:
    detail = _detail(_edit(checkout, {"op": "append_evidence"}))

    for field in ("plan", "anchor", "title", "body"):
        assert f"'{field}'" in detail, (field, detail)
    # One refusal carrying four problems, not four refusals discovered in turn.
    assert detail.count("append_evidence op requires") == 4, detail


def test_append_evidence_with_two_malformed_fields_names_both(checkout) -> None:
    detail = _detail(
        _edit(
            checkout,
            {
                "op": "append_evidence",
                "plan": "not a slug",
                "anchor": "also not one",
                "title": "The beat",
                "body": "<p>What landed.</p>",
            },
        )
    )

    assert "'plan'" in detail, detail
    assert "'anchor'" in detail, detail
    assert detail.count("append_evidence op requires") == 2, detail


def test_close_with_every_field_missing_names_all_three(checkout) -> None:
    detail = _detail(_edit(checkout, {"op": "close"}))

    assert "terminal status" in detail, detail
    assert "'outcome'" in detail, detail
    assert "'by'" in detail, detail
    assert detail.count("close") >= 3, detail


def test_one_missing_field_keeps_the_message_it_gave_before(checkout) -> None:
    assert (
        _detail(
            _edit(
                checkout,
                {
                    "op": "append_evidence",
                    "anchor": "the-beat",
                    "title": "The beat",
                    "body": "<p>What landed.</p>",
                },
            )
        )
        == "append_evidence op requires a 'plan' slug matching "
        "[A-Za-z0-9][A-Za-z0-9._-]*"
    )

    assert (
        _detail(
            _edit(
                checkout,
                {
                    "op": "append_evidence",
                    "plan": PLAN,
                    "anchor": "the-beat",
                    "body": "<p>What landed.</p>",
                },
            )
        )
        == "append_evidence op requires a non-empty string 'title'"
    )

    assert (
        _detail(
            _edit(
                checkout,
                {"op": "close", "status": "done", "outcome": "closing"},
            )
        )
        == "close op requires a 'by'"
    )

    assert (
        _detail(_edit(checkout, {"op": "append"}))
        == "append op requires a 'target' collection"
    )