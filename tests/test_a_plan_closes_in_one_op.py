"""A plan closes in one write, without a placeholder followup.

``{op:'close', status, outcome, by}`` is one write: it sets a terminal status
and resolves every open followup with the outcome, running every guard the
transition fires and reporting them together. A ``set`` of ``status`` to a
terminal value is refused, naming ``close`` — the terminal path has one
spelling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from reckon.lifecycle import TERMINAL_STATUSES
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "close-project"
SLUG = "close-case"


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root
    reload_mcp_family()
    return docs_dir, state_root, PROJECT


def _bare_plan(slug: str, state: dict) -> str:
    base = dict(state)
    base.setdefault("slug", slug)
    base.setdefault("title", slug.title())
    base.setdefault("type", "plan")
    if base["type"] == "plan":
        base.setdefault("status", "active")
    bare = (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    return write_state(bare, base)


def _write_plan(docs_dir: Path, slug: str, state: dict) -> Path:
    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_bare_plan(slug, state), encoding="utf-8")
    return path


def _write_evidence(docs_dir: Path, slug: str) -> Path:
    path = docs_dir / "evidence" / "archive" / f"{slug}-landed.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _bare_plan(
            f"{slug}-landed",
            {"version": 0, "type": "evidence", "evidence_for": [slug]},
        ),
        encoding="utf-8",
    )
    return path


def _open_followup(ident: str) -> dict:
    return {
        "id": ident,
        "status": "open",
        "title": f"work {ident}",
        "body": "why",
        "written_by": "reckon-build",
        "written_at": "2026-10-19",
        "prompt": f"/reckon-build {SLUG} §1",
    }


def _pending_gate() -> dict:
    return {
        "id": "outcome-gate",
        "section": "s1",
        "gated_sections": ["s1"],
        "transition": "plan-terminal",
        "gating_plan": "producer#outcome",
        "measure": "The producer records its outcome",
        "verdict": "",
    }


def test_a_finished_plan_closes_in_one_write(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        {
            "version": 0,
            "section_declarations": {"s1": "done"},
            "followups": [_open_followup("f1")],
        },
    )
    _write_evidence(docs_dir, SLUG)

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "close",
                "status": "done",
                "outcome": "every section landed — no followup",
                "by": "reckon-build",
            }
        ],
        0,
    )

    assert result["ok"] is True, result
    data, version = _store_module.read_plan(project, SLUG)
    assert data["status"] == "done"
    assert version == 1, "the close is one version step"
    assert [f["id"] for f in data["followups"]] == ["f1"], (
        "no placeholder followup is appended"
    )
    assert all(f["status"] == "resolved" for f in data["followups"])
    assert data["followups"][0]["outcome"] == "every section landed — no followup"


def test_three_failing_guards_are_reported_together(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        {
            "version": 0,
            "section_declarations": {"s1": "implementable"},
            "followups": [_open_followup("f1")],
            "gates": [_pending_gate()],
        },
    )
    # No evidence record is written: the terminal status needs one.

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "close",
                "status": "done",
                "outcome": "closing",
                "by": "reckon-build",
            }
        ],
        0,
    )

    assert result["ok"] is False, result
    detail = result["detail"]
    assert "s1" in detail, "the open section is named"
    assert "evidence" in detail, "the missing evidence record is named"
    assert "outcome-gate" in detail, "the pending transition gate is named"

    _, version = _store_module.read_plan(project, SLUG)
    assert version == 0, "a refused close writes nothing"


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_a_terminal_set_is_refused_naming_close(setup, status: str) -> None:
    docs_dir, _, project = setup
    _write_plan(docs_dir, SLUG, {"version": 0})

    result = mcp_module._edit_plan(
        project, SLUG, [{"op": "set", "path": "status", "value": status}], 0
    )

    assert result["ok"] is False, result
    assert "close" in result["detail"], "the refusal names the close op"


def test_a_sprint_status_set_still_applies() -> None:
    working = {"sprints": [{"id": "S1", "status": "active", "items": []}]}
    _store_module.apply_ops(
        working, [{"op": "set", "path": "sprints.S1.status", "value": "done"}], True
    )
    assert working["sprints"][0]["status"] == "done"


def test_a_superseded_close_skips_the_landed_only_guard(setup) -> None:
    # The section-ticked guard fires only for a landed status; a superseded or
    # archived close is not newly refused by it.
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        {
            "version": 0,
            "section_declarations": {"s1": "implementable"},
            "followups": [_open_followup("f1")],
        },
    )
    _write_evidence(docs_dir, SLUG)

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "close",
                "status": "superseded",
                "outcome": "superseded by the successor plan",
                "by": "reckon-build",
                "superseded_by": "successor",
            }
        ],
        0,
    )

    assert result["ok"] is True, result
    data, _ = _store_module.read_plan(project, SLUG)
    assert data["status"] == "superseded"


def test_a_close_batch_that_appends_a_hiding_followup_is_refused(setup) -> None:
    # A close batch that also appends a followup hiding work is refused, and the
    # refusal names that followup. Guard (d) lives in the same list the close
    # consults, so the two writers cannot disagree about it.
    docs_dir, _, project = setup
    _write_plan(docs_dir, SLUG, {"version": 0, "section_declarations": {"s1": "done"}})
    _write_evidence(docs_dir, SLUG)

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "close",
                "status": "done",
                "outcome": "the sections landed — no followup",
                "by": "reckon-build",
            },
            {"op": "append", "target": "followups", "item": _open_followup("f-hide")},
        ],
        0,
    )

    assert result["ok"] is False, result
    assert "f-hide" in result["detail"], result["detail"]
    assert "hides work" in result["detail"], result["detail"]


def test_the_close_guard_list_carries_the_hiding_followup(setup) -> None:
    """The hiding-followup guard is in the list a close consults.

    Driven here rather than through an op batch, because on the op path an
    appended open followup is refused by the append op before the batch is
    judged, so a close batch can never reach this guard with one: the guard is
    the merge-patch writer's, and the close consults the same list. The failure
    comes back beside the missing evidence record, which is the point — one
    list, every failing guard in one answer.
    """
    docs_dir, _, project = setup
    _write_plan(docs_dir, SLUG, {"version": 0, "section_declarations": {"s1": "done"}})

    state = {
        "project": project,
        "slug": SLUG,
        "type": "plan",
        "status": "done",
        "followups": [_open_followup("f-hide")],
    }
    failures = _store_module.validate_landing_patch(
        state,
        {"status": "done", "followups": state["followups"]},
        None,
        report_all=True,
    )

    assert any("hides work" in failure for failure in failures), failures
    assert any("evidence" in failure for failure in failures), failures
