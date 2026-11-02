"""A section can be descoped, with a reason, and that is not the same as done.

``descoped`` is a declaration of its own: it closes the section for the landing
guard and for the critical path, so a plan whose only remaining work was
descoped can land, while the impl figure it derives leaves the section out of
its denominator rather than counting it as built. The declaration carries a
reason, which the plan's record keeps, and a descoped declaration with no
reason is refused — a record that says only "descoped" is the same false
record the ``done`` tick this declaration replaces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from reckon._plan_html import (
    derive_impl_with_source,
    write_state,
)
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "descope-project"
SLUG = "descope-case"


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
    headings = "".join(
        f'<h2 id="{record["id"]}">§{record["id"]} — work</h2>'
        for record in base.get("sections") or []
    )
    bare = (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head>\n"
        f'<body><main class="plan-doc">{headings}</main></body>\n</html>\n'
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


def _section(ident: str, status: str, hours: float = 2.0, reason: str | None = None):
    record = {
        "id": ident,
        "effort_hours": hours,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "deep",
                "verification": "strict",
                "risk": "moderate",
            },
        },
        "attempts": 0,
        "status": status,
        "links": [],
    }
    if reason is not None:
        record["descope_reason"] = reason
    return record


DESCOPED_REASON = "the downstream consumer retired the interface this section builds"


def test_a_plan_whose_only_open_section_is_descoped_closes(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        {
            "version": 0,
            "section_declarations": {"s1": "done", "s2": "descoped"},
            "sections": [
                _section("s1", "done"),
                _section("s2", "descoped", reason=DESCOPED_REASON),
            ],
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
                "outcome": (
                    "the built section landed and the other was descoped — "
                    "done, no followup"
                ),
                "by": "reckon-build",
            }
        ],
        0,
    )

    assert result["ok"] is True, result
    data, _version = _store_module.read_plan(project, SLUG)
    assert data["status"] == "done"


def test_the_impl_figure_leaves_a_descoped_section_out(setup) -> None:
    sections = [_section("s1", "done", 2.0), _section("s2", "descoped", 6.0, "retired")]
    declarations = {"s1": "done", "s2": "descoped"}

    figure, source = derive_impl_with_source(sections, declarations)

    assert figure == 1.0, "the descoped section is outside the denominator"
    assert source == "computed"


def test_the_descoped_declaration_and_reason_are_retained(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        {
            "version": 0,
            "section_declarations": {"s1": "done", "s2": "descoped"},
            "sections": [
                _section("s1", "done"),
                _section("s2", "descoped", reason=DESCOPED_REASON),
            ],
        },
    )

    data, _version = _store_module.read_plan(project, SLUG)

    assert data["section_declarations"]["s2"] == "descoped"
    record = {row["id"]: row for row in data["sections"]}["s2"]
    assert record["status"] == "descoped"
    assert record["descope_reason"] == DESCOPED_REASON


def test_a_reasoned_descoped_declaration_is_accepted(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        {
            "version": 0,
            "section_declarations": {"s1": "implementable"},
            "sections": [_section("s1", "implementable")],
        },
    )

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations.s1",
                "value": {"status": "descoped", "reason": DESCOPED_REASON},
            }
        ],
        0,
    )

    assert result["ok"] is True, result
    data, _version = _store_module.read_plan(project, SLUG)
    assert data["section_declarations"]["s1"] == "descoped"
    record = {row["id"]: row for row in data["sections"]}["s1"]
    assert record["descope_reason"] == DESCOPED_REASON


def test_a_descoped_declaration_without_a_reason_is_refused(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        {
            "version": 0,
            "section_declarations": {"s1": "implementable"},
            "sections": [_section("s1", "implementable")],
        },
    )

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [{"op": "set", "path": "section_declarations.s1", "value": "descoped"}],
        0,
    )

    assert result["ok"] is False, result
    detail = str(result.get("detail") or "")
    assert "reason" in detail.lower(), detail
    data, version = _store_module.read_plan(project, SLUG)
    assert version == 0, "a refused write stores nothing"
    assert data["section_declarations"]["s1"] == "implementable"
