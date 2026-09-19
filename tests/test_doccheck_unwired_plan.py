"""Tests for the ``unwired-plan`` finding: a plan is born wired or says why not.

The rule is a positive control at the two moments a plan is born or grows, so
the tests exercise the audit path and the write-boundary refusal, and assert
the declaration round-trips through a read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _plan_html as plan_html_module
from reckon import _store as store_module
from reckon import mcp as mcp_module
from reckon.doccheck import audit_html, unwired_plan_finding

ENFORCED_FROM = "2026-09-19"


def _plan_html(
    *,
    slug: str = "sample",
    status: str = "active",
    modified: str = "",
    links: dict[str, str] | None = None,
    gates: bool = False,
    standalone: str | None = None,
    declared: bool = True,
) -> str:
    metas = [("plan-slug", slug), ("plan-status", status)]
    if modified:
        metas.append(("plan-modified", modified))
    if declared:
        metas.append(("reckon-type", "plan"))
    for name, value in (links or {}).items():
        metas.append((name, value))
    if standalone is not None:
        metas.append(("plan-standalone", standalone))
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    gates_html = (
        '<section data-reckon="gates" id="gates" class="r-gates">'
        '<h2><span class="sec">§</span> Evidence gates</h2>'
        '<div class="r-gate" data-id="g-one"><p>gates what follows</p></div>'
        "</section>"
        if gates
        else ""
    )
    return (
        f'<!doctype html><html lang="en"><head>{head}<title>{slug}</title></head>'
        f'<body><main class="plan-doc">{gates_html}</main></body></html>'
    )


def _wiring(findings):
    return [f for f in findings if f.code == "unwired-plan"]


# ── the four audit paths ─────────────────────────────────────────────────────


def test_wired_plan_is_silent():
    html = _plan_html(modified=ENFORCED_FROM, links={"plan-depends-on": "other"})

    assert _wiring(audit_html(html)) == []


def test_plan_wired_only_by_a_gate_is_silent():
    html = _plan_html(modified=ENFORCED_FROM, gates=True)

    assert _wiring(audit_html(html)) == []


def test_unwired_plan_modified_after_the_rule_lands_is_an_error():
    html = _plan_html(modified=ENFORCED_FROM)

    (finding,) = _wiring(audit_html(html))

    assert finding.severity == "error"
    assert (
        finding.message
        == unwired_plan_finding(
            doc_type="plan",
            status="active",
            modified=ENFORCED_FROM,
            links=[],
            gate_count=0,
            standalone=None,
            slug="sample",
        ).message
    )


def test_unwired_plan_predating_the_rule_is_a_warning():
    html = _plan_html(modified="2026-08-01")

    (finding,) = _wiring(audit_html(html))

    assert finding.severity == "warn"


def test_standalone_declaration_silences_the_finding():
    html = _plan_html(
        modified=ENFORCED_FROM,
        standalone="Feeds nothing and waits on nothing; it is a one-file fix.",
    )

    assert _wiring(audit_html(html)) == []


def test_empty_standalone_declaration_is_not_a_declaration():
    html = _plan_html(modified=ENFORCED_FROM, standalone="")

    (finding,) = _wiring(audit_html(html))

    assert finding.severity == "error"


def test_terminal_plan_is_silent():
    html = _plan_html(status="shipped", modified=ENFORCED_FROM)

    assert _wiring(audit_html(html)) == []


def test_undeclared_document_is_not_held_to_the_plan_contract():
    """A prose fragment carrying a slug is not a plan making a wiring claim."""

    html = _plan_html(modified=ENFORCED_FROM, declared=False)

    assert _wiring(audit_html(html)) == []


def test_message_carries_the_checklist():
    html = _plan_html(modified=ENFORCED_FROM)

    (finding,) = _wiring(audit_html(html))

    assert "does my first section wait on it" in finding.message
    assert "does it consume my evidence" in finding.message
    assert "does one section of mine wait on one section of it" in finding.message
    assert "plan-standalone" in finding.message


# ── the write boundary ──────────────────────────────────────────────────────


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A temporary checkout whose docs/ tree is the plan store under test."""

    root = tmp_path / "checkout"
    (root / "docs" / "plans").mkdir(parents=True)
    return root


def test_create_refuses_an_unwired_plan_with_the_checklist(checkout: Path):
    result = mcp_module._edit_plan(
        "sample",
        "unwired",
        [],
        expected_version=0,
        create=True,
        checkout_path=str(checkout),
        doc_type="plan",
    )

    assert result["ok"] is False
    assert result["error"] == "unwired_plan"
    assert "does my first section wait on it" in result["detail"]
    assert "does one section of mine wait on one section of it" in result["detail"]
    # A refused create leaves no trace.
    assert not (checkout / "docs" / "plans" / "unwired.html").exists()


def test_create_accepts_a_plan_that_declares_its_wire(checkout: Path):
    result = mcp_module._edit_plan(
        "sample",
        "wired",
        [{"op": "set", "path": "depends_on", "value": ["other"]}],
        expected_version=0,
        create=True,
        checkout_path=str(checkout),
        doc_type="plan",
    )

    assert result["ok"] is True
    assert (checkout / "docs" / "plans" / "wired.html").exists()


def test_create_accepts_and_round_trips_a_standalone_declaration(checkout: Path):
    reason = "Single-file doc fix; it feeds nothing and waits on nothing."

    created = mcp_module._edit_plan(
        "sample",
        "alone",
        [{"op": "set", "path": "standalone", "value": reason}],
        expected_version=0,
        create=True,
        checkout_path=str(checkout),
        doc_type="plan",
    )

    assert created["ok"] is True
    header = (checkout / "docs" / "plans" / "alone.html").read_text(encoding="utf-8")
    assert f'<meta name="plan-standalone" content="{reason}">' in header

    read = mcp_module._read_plan(
        project="sample",
        slug="alone",
        checkout_path=str(checkout),
        doc_type="plan",
    )
    assert read["data"]["standalone"] == reason
    assert _wiring(audit_html(header)) == []


def test_edit_sets_a_standalone_declaration_on_an_existing_plan(checkout: Path):
    """The declaration is reachable through the ordinary state write, not only
    at creation, so an existing plan can be declared standalone after the fact."""

    reason = "Feeds nothing and waits on nothing; it is a one-file fix."

    created = mcp_module._edit_plan(
        "sample",
        "grow",
        [{"op": "set", "path": "depends_on", "value": ["other"]}],
        expected_version=0,
        create=True,
        checkout_path=str(checkout),
        doc_type="plan",
    )
    assert created["ok"] is True

    current = mcp_module._read_plan(
        project="sample",
        slug="grow",
        checkout_path=str(checkout),
        doc_type="plan",
    )
    edited = mcp_module._edit_plan(
        "sample",
        "grow",
        [{"op": "set", "path": "standalone", "value": reason}],
        expected_version=current["version"],
        checkout_path=str(checkout),
        doc_type="plan",
    )

    assert edited["ok"] is True
    header = (checkout / "docs" / "plans" / "grow.html").read_text(encoding="utf-8")
    assert f'<meta name="plan-standalone" content="{reason}">' in header

    read = mcp_module._read_plan(
        project="sample",
        slug="grow",
        checkout_path=str(checkout),
        doc_type="plan",
    )
    assert read["data"]["standalone"] == reason


def test_store_write_path_accepts_the_standalone_declaration():
    """The generic store write path carries the declaration, not only the MCP
    route that splits it out of the batch and writes the meta directly."""

    working = {"status": "active"}
    warnings: list[str] = []

    store_module._apply_set(
        working,
        {"op": "set", "path": "standalone", "value": "One-file fix."},
        False,
        warnings,
    )

    assert working["standalone"] == "One-file fix."

    state = plan_html_module.read_state(_plan_html(standalone="One-file fix."))
    assert state["standalone"] == "One-file fix."
