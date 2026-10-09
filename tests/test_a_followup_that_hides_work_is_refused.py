"""The plan write path refuses a new followup that hides work.

A followup that names work no surface can dispatch silently keeps the chain
alive while nothing moves — it appears in the list, never in a schedule. The
refusal names the reason and the remedy: add the work as a section of this
plan, or create a new plan when this one is complete, and point the followup
at it.

The plans are written into a temporary docs directory the mounted store
serves, so each case travels the real edit_plan ops path against parsed plan
state rather than against dicts shaped to please the predicate. Resolving an
existing followup that hides work must keep succeeding, because that is how
the backlog clears.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "temp-followup-refusal-project"
HOST_DECLARATIONS = {"s1": "done", "s2": "implementable"}
REMEDY = "add the work as a section of this plan"
DECISION_REMEDY = "recorded as an open decision"


def _write_plan(
    docs_dir: Path,
    slug: str,
    *,
    status: str = "active",
    declarations: dict[str, str] | None = None,
    followups: tuple[tuple[str, str], ...] = (),
) -> Path:
    """Write one plan HTML into ``docs_dir/plans`` in the store's own layout."""

    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    metas = [
        ("docs-project", PROJECT),
        ("reckon-type", "plan"),
        ("plan-slug", slug),
        ("plan-status", status),
        ("plan-modified", "2026-10-02"),
        (
            "plan-section-declarations",
            html.escape(json.dumps(declarations or {}, separators=(",", ":"))),
        ),
    ]
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    articles = "".join(
        f'<article class="r-fu" data-id="{fid}" data-status="open"'
        f' data-written-by="test" data-written-at="2026-10-02">'
        f'<h4 class="r-fu-title">{fid}</h4>'
        f'<div class="r-fu-body"><p>{fid}</p></div>'
        f'<pre class="r-fu-prompt">{html.escape(prompt)}</pre>'
        "</article>"
        for fid, prompt in followups
    )
    path.write_text(
        "<!doctype html><html><head>"
        f"{head}<title>{slug}</title></head>"
        '<body><main class="plan-doc">'
        '<section data-reckon="followups" id="followups">'
        f"<h2>§ Followups</h2>{articles}</section>"
        "</main></body></html>",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def setup(tmp_path, monkeypatch):
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
    return docs_dir


def _append(plan_slug: str, followup_id: str, prompt: str, version: int) -> dict:
    return mcp_module._edit_plan(
        PROJECT,
        plan_slug,
        [
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": followup_id,
                    "written_by": "test",
                    "written_at": "2026-10-02",
                    "title": followup_id,
                    "body": "why",
                    "prompt": prompt,
                },
            }
        ],
        version,
    )


def _plan_ids(plan_slug: str) -> list[str]:
    data, _ = _store_module.read_plan(PROJECT, plan_slug)
    return [str(f.get("id")) for f in data.get("followups") or []]


def test_a_pointer_to_another_plan_is_written(setup) -> None:
    _write_plan(setup, "host", declarations=HOST_DECLARATIONS)
    _write_plan(setup, "other-plan", declarations={"s1": "implementable"})

    result = _append("host", "f1", "/reckon-build other-plan §1", 0)

    assert result["ok"] is True
    assert _plan_ids("host") == ["f1"]


def test_a_pointer_to_own_implementable_section_is_written(setup) -> None:
    _write_plan(setup, "host", declarations=HOST_DECLARATIONS)

    result = _append("host", "f2", "/reckon-build host §2", 0)

    assert result["ok"] is True
    assert _plan_ids("host") == ["f2"]


def test_a_followup_naming_a_done_section_is_refused(setup) -> None:
    _write_plan(setup, "host", declarations=HOST_DECLARATIONS)

    result = _append("host", "f3", "/reckon-build host §1", 0)

    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert "section-not-implementable" in result["detail"]
    assert REMEDY in result["detail"]
    assert DECISION_REMEDY in result["detail"]
    assert _plan_ids("host") == []


def test_a_followup_naming_no_section_is_refused(setup) -> None:
    _write_plan(setup, "host", declarations=HOST_DECLARATIONS)

    result = _append("host", "f4", "/reckon-build host", 0)

    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert "no-section" in result["detail"]
    assert REMEDY in result["detail"]
    assert DECISION_REMEDY in result["detail"]
    assert _plan_ids("host") == []


def test_a_followup_on_a_shipped_plan_is_refused(setup) -> None:
    _write_plan(setup, "omega", status="shipped", declarations={"s1": "implementable"})

    result = _append("omega", "f5", "/reckon-build omega §1", 0)

    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert "host-complete" in result["detail"]
    assert _plan_ids("omega") == []


def test_resolving_a_followup_that_hides_work_succeeds(setup) -> None:
    _write_plan(
        setup,
        "host",
        declarations=HOST_DECLARATIONS,
        followups=(("f6", "/reckon-build host"),),
    )

    result = mcp_module._edit_plan(
        PROJECT,
        "host",
        [
            {
                "op": "resolve",
                "target": "followups",
                "id": "f6",
                "by": "test",
                "outcome": "handled elsewhere: work moved to a section",
            }
        ],
        0,
    )

    assert result["ok"] is True
    data, _ = _store_module.read_plan(PROJECT, "host")
    assert data["followups"][0]["status"] == "resolved"
