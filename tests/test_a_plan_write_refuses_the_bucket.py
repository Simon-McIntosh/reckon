"""A plan write naming the deferred-work term in prose is refused.

Work found beyond a node is a plan section or an instruction in the dispatching
prompt, never a bucket no roadmap reads. Every authoring route reaches one
predicate: the term in an authored body is refused through the same check that
refuses a forged heading, ``edit_*`` reports it as ``op_error``, and text mode
reports it as ``text_edit_error`` after comparing the fragment it writes with
the fragment it replaces, so a plan whose older prose already carries the term
stays editable. A citation of the term inside ``<code>`` names a symbol rather
than a bucket and is accepted; ``reckon audit-doc`` warns on a plan that
already carries it in prose.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon import mcp as mcp_module
from reckon.cli import main

PROJECT = "bucket-proj"
PLAN = "bucket-plan"
TERM = "follow-on"
RULE = "additional work is a plan section or an instruction in the dispatching prompt"
TEXT_MODE_HINT = "edit this section's body in text mode"

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}
DECLARATIONS = {"s1": "done", "s2": "implementable"}
LEGACY_PROSE = (
    '<p id="legacy">An older remainder, recorded as follow-on work before the '
    "rule landed.</p>"
)


def _records(declarations: dict[str, str]) -> list[dict]:
    return [
        {
            "id": section_id,
            "effort_hours": 1.0,
            "capability": deepcopy(CAPABILITY),
            "attempts": 0,
            "status": status,
            "links": [],
        }
        for section_id, status in declarations.items()
    ]


def _insert_op(section_id: str, title: str, body: str) -> dict:
    """An insert_section request carrying the typed record its contract requires."""
    return {
        "op": "insert_section",
        "id": section_id,
        "title": title,
        "body": body,
        "effort_hours": 1.25,
        "capability": deepcopy(CAPABILITY),
        "links": [],
    }


def _section_item(section_id: str, body: str) -> dict:
    """A section item via the append route, which authors its heading and body."""
    return {
        "id": section_id,
        "title": "First section",
        "body": body,
        "effort_hours": 1.0,
        "capability": deepcopy(CAPABILITY),
        "links": [],
    }


def _write_plan(
    checkout: Path,
    body: str,
    *,
    slug: str = PLAN,
    version: int = 0,
    declarations: dict[str, str] | None = None,
) -> Path:
    """Write one plan whose prose is ``body``, as an earlier writer left it."""
    declared = DECLARATIONS if declarations is None else declarations
    path = checkout / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f"<title>{slug}</title></head><body>"
        '<main class="plan-doc">'
        f"{body}"
        "</main></body></html>"
    )
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": slug,
        "title": slug,
        "status": "active",
        "standalone": "a fixture wired to nothing",
        "modified": "2026-10-23",
        "version": version,
        "section_declarations": deepcopy(declared),
        "sections": _records(declared),
    }
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A checkout holding one plan with two authored sections."""
    root = tmp_path / "repo"
    _write_plan(
        root,
        '<h2 id="s1">First section</h2><p>First body.</p>'
        '<h2 id="s2">Second section</h2><p>Second body.</p>'
        '<p id="intro">Introductory prose.</p>',
    )
    return root


def _edit(checkout: Path, ops: list[dict], *, slug: str = PLAN, **extra) -> dict:
    path = checkout / "docs" / "plans" / f"{slug}.html"
    if slug == PLAN:
        version = _plan_html.read_state(path.read_text(encoding="utf-8"))["version"]
        extra["expected_version"] = version
    return mcp_module._edit_plan_tool(
        PROJECT,
        slug,
        doc_type="plan",
        checkout_path=str(checkout),
        ops=ops,
        **extra,
    )


def _edit_text(checkout: Path, old_html: str, new_html: str, **extra) -> dict:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    version = _plan_html.read_state(path.read_text(encoding="utf-8"))["version"]
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        doc_type="plan",
        checkout_path=str(checkout),
        mode="text",
        old_html=old_html,
        new_html=new_html,
        **extra,
    )


# ── the term in an authored body ───────────────────────────────────────────


def test_an_inserted_section_naming_the_term_is_refused(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit(
        checkout,
        [_insert_op("s3", "Third section", f"<p>Recorded as {TERM} work.</p>")],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert RULE in result["detail"], result
    assert f'"{TERM}"' in result["detail"], result
    assert path.read_bytes() == before
    assert 'id="s3"' not in path.read_text(encoding="utf-8")


def test_a_created_plan_naming_the_term_in_a_section_body_is_refused(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)

    result = mcp_module._edit_plan_tool(
        PROJECT,
        "fresh-plan",
        expected_version=0,
        create=True,
        doc_type="plan",
        checkout_path=str(root),
        ops=[
            {"op": "set", "path": "standalone", "value": "an unwired scratch plan"},
            {
                "op": "append",
                "target": "sections",
                "item": _section_item("s1", f"<p>Left as {TERM} work.</p>"),
            },
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert RULE in result["detail"], result
    assert not (root / "docs" / "plans" / "fresh-plan.html").exists()


def test_a_section_body_set_naming_the_term_is_refused(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit(
        checkout,
        [
            {
                "op": "set",
                "path": "sections.s2.body",
                "value": f"<p>A remainder parked as {TERM} work.</p>",
            }
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "sections.s2.body" in result["detail"], result
    assert RULE in result["detail"], result
    assert TEXT_MODE_HINT in result["detail"], result
    assert path.read_bytes() == before


def test_a_section_body_that_also_forges_a_heading_is_refused_for_the_heading(
    checkout: Path,
) -> None:
    """Text mode is the route out of the term refusal, not of every refusal."""
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit(
        checkout,
        [
            {
                "op": "set",
                "path": "sections.s2.body",
                "value": (
                    "<h2>An authored heading</h2>"
                    f"<p>A remainder parked as {TERM} work.</p>"
                ),
            }
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "must not contain another h2" in result["detail"], result
    assert RULE not in result["detail"], result
    assert TEXT_MODE_HINT not in result["detail"], result
    assert path.read_bytes() == before


def test_an_evidence_body_naming_the_term_is_refused(checkout: Path) -> None:
    result = _edit(
        checkout,
        [
            {
                "op": "append_evidence",
                "plan": PLAN,
                "anchor": "first-beat",
                "title": "The first beat",
                "body": f"<p>What landed, plus {TERM} work.</p>",
            }
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "append_evidence body" in result["detail"], result
    assert RULE in result["detail"], result


def test_a_text_edit_adding_the_term_is_refused(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit_text(
        checkout,
        '<p id="intro">Introductory prose.</p>',
        f'<p id="intro">Introductory prose, plus {TERM} work.</p>',
    )

    assert result["ok"] is False, result
    assert result["error"] == "text_edit_error"
    assert RULE in result["detail"], result
    assert f'"{TERM}"' in result["detail"], result
    assert path.read_bytes() == before


# ── what stays writable ────────────────────────────────────────────────────


def test_the_term_inside_code_is_a_citation_and_is_accepted(checkout: Path) -> None:
    result = _edit(
        checkout,
        [_insert_op("s3", "Third section", f"<p>Cites <code>{TERM}s</code>.</p>")],
    )

    assert result["ok"] is True, result
    text = (checkout / "docs" / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    assert f"<code>{TERM}s</code>" in text


def test_a_section_body_citing_the_term_in_code_is_accepted(checkout: Path) -> None:
    result = _edit(
        checkout,
        [
            {
                "op": "set",
                "path": "sections.s2.body",
                "value": f"<p>Renames the <code>{TERM}</code> severity.</p>",
            }
        ],
    )

    assert result["ok"] is True, result
    text = (checkout / "docs" / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    assert f"<code>{TERM}</code>" in text


def test_a_text_edit_that_adds_no_term_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_plan(
        root,
        f'<h2 id="s1">First section</h2><p>First body.</p>{LEGACY_PROSE}'
        '<p id="intro">Introductory prose.</p>',
        declarations={"s1": "done"},
    )

    result = _edit_text(
        root,
        '<p id="intro">Introductory prose.</p>',
        '<p id="intro">Edited introductory prose.</p>',
    )

    assert result["ok"] is True, result
    text = (root / "docs" / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    assert "Edited introductory prose." in text
    assert LEGACY_PROSE in text


# ── the audit reports what a plan already carries ──────────────────────────


def _audit(checkout: Path, tmp_path: Path) -> str:
    result = CliRunner().invoke(
        main,
        ["audit-doc", str(checkout / "docs" / "plans" / f"{PLAN}.html")],
        env={"RECKON_HOME": str(tmp_path / "config-home")},
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_audit_doc_warns_on_the_term_in_prose(checkout: Path, tmp_path: Path) -> None:
    _write_plan(
        checkout,
        '<h2 id="s1">First section</h2><p>First body.</p>'
        f'<p id="legacy">An older remainder, recorded as {TERM} work.</p>',
        declarations={"s1": "done"},
    )

    output = _audit(checkout, tmp_path)

    assert "deferred-work-term-in-prose" in output, output
    assert TERM in output, output


def test_audit_doc_is_silent_without_the_term(checkout: Path, tmp_path: Path) -> None:
    """Control: the audit sees the clean arm green, so the warning is not constant."""
    output = _audit(checkout, tmp_path)

    assert "deferred-work-term-in-prose" not in output, output


def test_audit_doc_is_silent_when_the_term_is_a_code_only_citation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _write_plan(
        root,
        '<h2 id="s1">First section</h2>'
        f"<p>Renames the <code>{TERM}</code> severity.</p>",
        declarations={"s1": "done"},
    )

    output = _audit(root, tmp_path)

    assert "deferred-work-term-in-prose" not in output, output
