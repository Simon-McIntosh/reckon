"""The append_evidence op writes one anchored section into a landing record."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, _store
from reckon import mcp as mcp_module
from reckon.cli import main

PROJECT = "sample"
PLAN = "demo-plan"

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


def _append_op(anchor: str, title: str, body: str, plan: str = PLAN) -> dict:
    """An append_evidence request for one anchored landing-record section."""
    return {
        "op": "append_evidence",
        "plan": plan,
        "anchor": anchor,
        "title": title,
        "body": body,
    }


@pytest.fixture()
def plan(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "repo"
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True)
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
        "followups": [
            {
                "id": "next-action",
                "status": "open",
                "prompt": f"/reckon-build {PLAN}",
            }
        ],
    }
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return checkout, path


def _edit(checkout: Path, op: dict) -> dict:
    """Apply one op through the plan-editing tool in the fixture checkout."""
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


def _edit_many(checkout: Path, ops: list[dict]) -> dict:
    """Apply several ops in one batch through the plan-editing tool."""
    plan_path = checkout / "docs" / "plans" / f"{PLAN}.html"
    state = _plan_html.read_state(plan_path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=ops,
    )


def _record_path(checkout: Path) -> Path:
    return checkout / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"


def _seed_record(checkout: Path, slug: str, extra: str = "") -> Path:
    """Write a landing record for one plan slug as a prior writer left it."""
    record = checkout / "docs" / "evidence" / "archive" / f"{slug}-landed.html"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="reckon-type" content="evidence">'
        f'<meta name="plan-evidence-for" content="{slug}">'
        "<title>Landing record</title></head><body>"
        f"<main>{extra}</main></body></html>",
        encoding="utf-8",
    )
    return record


def test_append_evidence_creates_the_record_with_the_required_meta(plan) -> None:
    checkout, _ = plan
    record = _record_path(checkout)
    assert not record.exists()

    result = _edit(
        checkout,
        _append_op("first-beat", "The first beat", "<p>What landed first.</p>"),
    )

    assert result["ok"] is True, result
    assert record.is_file()
    text = record.read_text(encoding="utf-8")
    assert '<meta name="reckon-type" content="evidence">' in text
    assert f'<meta name="plan-evidence-for" content="{PLAN}">' in text
    assert '<section id="first-beat">' in text
    assert "<h2>The first beat</h2>" in text
    assert "<p>What landed first.</p>" in text
    # The section lands inside main, before its closing tag.
    assert text.index('<section id="first-beat">') < text.index("</main>")


def test_append_evidence_lands_beside_an_existing_anchor(plan) -> None:
    checkout, _ = plan
    first = _edit(checkout, _append_op("first-beat", "The first beat", "<p>One.</p>"))
    assert first["ok"] is True, first

    second = _edit(
        checkout,
        _append_op("second-beat", "The second beat", "<p>Two.</p>"),
    )

    assert second["ok"] is True, second
    text = _record_path(checkout).read_text(encoding="utf-8")
    first_at = text.index('<section id="first-beat">')
    second_at = text.index('<section id="second-beat">')
    assert first_at < second_at < text.index("</main>")
    # A second writer's append does not overwrite the first.
    assert "<p>One.</p>" in text
    assert "<p>Two.</p>" in text
    assert text.count("<section id=") == 2


def test_append_evidence_refuses_a_duplicate_anchor_naming_it(plan) -> None:
    checkout, _ = plan
    first = _edit(checkout, _append_op("first-beat", "The first beat", "<p>One.</p>"))
    assert first["ok"] is True, first
    before = _record_path(checkout).read_text(encoding="utf-8")

    duplicate = _edit(
        checkout,
        _append_op("first-beat", "A second write", "<p>Overwrites.</p>"),
    )

    assert duplicate["ok"] is False, duplicate
    assert duplicate["error"] == "op_error"
    assert "first-beat" in duplicate["detail"]
    assert _record_path(checkout).read_text(encoding="utf-8") == before


def test_append_evidence_record_passes_audit_doc(plan) -> None:
    checkout, _ = plan
    created = _edit(
        checkout,
        _append_op("first-beat", "The first beat", "<p>What landed first.</p>"),
    )
    assert created["ok"] is True, created
    appended = _edit(
        checkout,
        _append_op("second-beat", "The second beat", "<p>What landed second.</p>"),
    )
    assert appended["ok"] is True, appended

    audit = CliRunner().invoke(main, ["audit-doc", str(_record_path(checkout))])
    assert audit.exit_code == 0, audit.output


def test_append_evidence_refuses_a_body_with_its_own_heading(plan) -> None:
    checkout, _ = plan
    result = _edit(
        checkout,
        _append_op("first-beat", "The first beat", "<h2>Nested</h2><p>One.</p>"),
    )
    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "h2" in result["detail"]
    assert not _record_path(checkout).exists()


def test_append_evidence_batch_appends_both_anchors_to_one_record(plan) -> None:
    checkout, _ = plan

    result = _edit_many(
        checkout,
        [
            _append_op("first-beat", "The first beat", "<p>One.</p>"),
            _append_op("second-beat", "The second beat", "<p>Two.</p>"),
        ],
    )

    assert result["ok"] is True, result
    text = _record_path(checkout).read_text(encoding="utf-8")
    assert '<section id="first-beat">' in text
    assert '<section id="second-beat">' in text
    assert "<p>One.</p>" in text
    assert "<p>Two.</p>" in text
    assert text.count("<section id=") == 2


def test_append_evidence_batch_refuses_a_repeated_anchor_untouched(plan) -> None:
    checkout, _ = plan

    result = _edit_many(
        checkout,
        [
            _append_op("beat", "A beat", "<p>One.</p>"),
            _append_op("beat", "A beat again", "<p>Two.</p>"),
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "beat" in result["detail"]
    # Both requests name this one record, so the refusal creates no file.
    assert not _record_path(checkout).exists()


def test_append_evidence_batch_refusal_leaves_other_records_untouched(plan) -> None:
    checkout, _ = plan
    first = _seed_record(checkout, PLAN)
    second = _seed_record(
        checkout,
        "other-plan",
        '<section id="taken"><h2>Taken</h2><p>Already there.</p></section>',
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second)
    }

    result = _edit_many(
        checkout,
        [
            _append_op("first-beat", "The first beat", "<p>One.</p>"),
            _append_op("taken", "Taken again", "<p>Two.</p>", plan="other-plan"),
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "taken" in result["detail"]
    for path in (first, second):
        assert path.read_bytes() == before[path][0]
        assert path.stat().st_mtime_ns == before[path][1]


def test_concurrent_appends_to_one_record_both_land(
    tmp_path: Path, monkeypatch
) -> None:
    """Two writers appending at once land both anchors, not one overwritten."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    docs_dir = tmp_path / "repo" / "docs"
    record = docs_dir / "evidence" / "archive" / f"{PLAN}-landed.html"
    record.parent.mkdir(parents=True)
    record.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="reckon-type" content="evidence">'
        f'<meta name="plan-evidence-for" content="{PLAN}">'
        "<title>Demo plan</title></head><body><main></main></body></html>",
        encoding="utf-8",
    )

    rounds = 12
    for round_index in range(rounds):
        record.write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="reckon-type" content="evidence">'
            f'<meta name="plan-evidence-for" content="{PLAN}">'
            "<title>Demo plan</title></head><body><main></main></body></html>",
            encoding="utf-8",
        )
        barrier = threading.Barrier(2)

        def append(anchor: str, barrier: threading.Barrier = barrier) -> None:
            barrier.wait(timeout=10)
            _store._apply_evidence_appends(
                docs_dir, PROJECT, [_append_op(anchor, anchor, "<p>x.</p>")], None
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(append, f"beat-{side}") for side in ("left", "right")
            ]
            for future in futures:
                future.result(timeout=10)

        text = record.read_text(encoding="utf-8")
        assert '<section id="beat-left">' in text, (round_index, text)
        assert '<section id="beat-right">' in text, (round_index, text)
        assert text.count("<section id=") == 2, (round_index, text)
