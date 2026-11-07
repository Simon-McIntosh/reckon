"""A landing record an append creates states no counts and no verdict.

The frame an ``append_evidence`` op writes has read no ledger and no plan
comments, so it must not publish a count sentence or a ``plan-verdict``. Only
a synthesis, which reads both, may. The checks below also carry three negative
controls showing each check sees the state it is meant to refuse.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, _store, evidence, ledger
from reckon import mcp as mcp_module
from reckon.cli import main

PROJECT = "sample"
PLAN = "demo-plan"
TITLE = "Demo plan"

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}

_COUNT_SENTENCES = (
    "committed run(s)",
    "section-anchored comment(s)",
    "landed commit(s)",
)
_APPEND_SUMMARY = f"Landing record for {TITLE}: evidence appended by section."


def _append_op(anchor: str, title: str, body: str) -> dict:
    return {
        "op": "append_evidence",
        "plan": PLAN,
        "anchor": anchor,
        "title": title,
        "body": body,
    }


def _write_plan(checkout: Path) -> Path:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        "<title>Demo plan</title></head><body>"
        '<main class="plan-doc">'
        '<h2 id="s1">First section</h2><p>First body.</p>'
        '<h2 id="s2">Second section</h2><p>Second body.</p>'
        "</main></body></html>"
    )
    sections = [
        {
            "id": section_id,
            "effort_hours": 1.0,
            "capability": deepcopy(CAPABILITY),
            "attempts": 0,
            "status": status,
            "links": [],
        }
        for section_id, status in {"s1": "done", "s2": "implementable"}.items()
    ]
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": TITLE,
        "status": "active",
        "modified": "2026-11-07",
        "version": 0,
        "section_declarations": {"s1": "done", "s2": "implementable"},
        "sections": sections,
        "followups": [],
    }
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    _write_plan(root)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    return root


def _record_path(checkout: Path) -> Path:
    return checkout / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"


def _append(checkout: Path, op: dict) -> dict:
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


def _summary_meta(text: str) -> str:
    match = re.search(r'<meta name="plan-summary" content="([^"]*)"', text)
    assert match is not None, text
    return match.group(1)


def _body_summary(text: str) -> str:
    match = re.search(r"<h1>[^<]*</h1>\s*<p>([^<]*)</p>", text)
    assert match is not None, text
    return match.group(1)


def test_an_appended_record_states_no_counts_and_no_verdict(checkout: Path) -> None:
    record = _record_path(checkout)
    assert not record.exists()

    result = _append(
        checkout,
        _append_op("first-beat", "The first beat", "<p>What landed first.</p>"),
    )

    assert result["ok"] is True, result
    text = record.read_text(encoding="utf-8")
    meta = _summary_meta(text)
    body = _body_summary(text)
    assert meta == _APPEND_SUMMARY
    assert body == _APPEND_SUMMARY
    for sentence in _COUNT_SENTENCES:
        assert sentence not in meta, text
        assert sentence not in body, text
    assert 'name="plan-verdict"' not in text
    # The append's own anchor still lands in the frame it created.
    assert '<section id="first-beat">' in text


def test_an_appended_record_still_names_its_plan_for_the_audit(
    checkout: Path,
) -> None:
    record = _record_path(checkout)
    result = _append(
        checkout,
        _append_op("first-beat", "The first beat", "<p>What landed first.</p>"),
    )
    assert result["ok"] is True, result

    text = record.read_text(encoding="utf-8")
    assert '<meta name="reckon-type" content="evidence">' in text
    assert f'<meta name="plan-evidence-for" content="{PLAN}">' in text

    audit = CliRunner().invoke(main, ["audit-doc", str(record)])
    assert audit.exit_code == 0, audit.output
    assert "evidence-for-missing" not in audit.output


def test_a_synthesis_still_states_the_counts_it_read(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(checkout.parent / "config"))
    record = ledger.build_record(
        run_id="run-first",
        plan=PLAN,
        section="s1",
        node="s1-node",
        gate="passed",
        completed_at="2026-11-07T10:00:00Z",
        completed_at_source="provided",
        worker_seconds=60,
        commits=("aaaaaaa",),
        tests_added=1,
        changed_lines={"insertions": 4, "deletions": 1},
    )
    second = ledger.build_record(
        run_id="run-second",
        plan=PLAN,
        section="s2",
        node="s2-node",
        gate="passed",
        completed_at="2026-11-07T11:00:00Z",
        completed_at_source="provided",
        worker_seconds=60,
        commits=("bbbbbbb",),
        tests_added=1,
        changed_lines={"insertions": 5, "deletions": 2},
    )
    ledger.append_run(PROJECT, record, root=checkout)
    ledger.append_run(PROJECT, second, root=checkout)

    result = evidence.synthesize_landed_record(checkout / "docs", PROJECT, PLAN)
    text = result.path.read_text(encoding="utf-8")

    assert result.runs == 2
    assert "2 committed run(s)" in text
    assert "2 committed run(s)" in _summary_meta(text)
    assert 'name="plan-verdict"' in text


def test_control_the_renderer_without_the_flag_claims_zero_counts() -> None:
    """Negative control: the shell's old call is what case one refuses."""
    plan = {"slug": PLAN, "title": TITLE, "comments": {}}
    text = evidence._render_document(PROJECT, plan, "", [], Path("."))

    assert "0 committed run(s)" in _summary_meta(text)
    assert "0 committed run(s)" in _body_summary(text)
    assert 'name="plan-verdict"' in text
    assert _summary_meta(text) != _APPEND_SUMMARY


def test_control_a_meta_left_holding_counts_fails_case_one() -> None:
    """A parameter that swaps only the body leaves the meta's counts behind."""
    healthy = _store._landed_record_shell(PROJECT, PLAN, TITLE)
    assert _summary_meta(healthy) == _APPEND_SUMMARY

    old_summary = (
        f"Closure synthesis for {TITLE}: 0 committed run(s), "
        "0 section-anchored comment(s), and 0 landed commit(s)."
    )
    half_fixed = healthy.replace(
        f'<meta name="plan-summary" content="{_APPEND_SUMMARY}">',
        f'<meta name="plan-summary" content="{old_summary}">',
    )

    assert _body_summary(half_fixed) == _APPEND_SUMMARY
    assert "committed run(s)" in _summary_meta(half_fixed)
    assert "committed run(s)" not in _body_summary(half_fixed)


def test_control_a_shell_built_apart_from_the_renderer_loses_the_plan_link() -> None:
    """A frame hand-built instead of delegated misses plan-evidence-for."""
    hand_built = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="evidence">'
        f'<meta name="plan-slug" content="{PLAN}-landed">'
        f'<meta name="plan-title" content="{TITLE} — landed record">'
        f'<meta name="plan-summary" content="{_APPEND_SUMMARY}">'
        "</head><body><main></main></body></html>"
    )

    assert 'name="plan-evidence-for"' not in hand_built
    assert 'name="plan-evidence-for"' in _store._landed_record_shell(
        PROJECT, PLAN, TITLE
    )
