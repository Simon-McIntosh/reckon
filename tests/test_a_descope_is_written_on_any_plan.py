"""A reasoned descope is written on a plan that carries no section records.

A plan may declare its work as a ``plan-section-declarations`` map beside bare
``<h2>`` headings that carry no ids, so there is no typed section record for a
descope reason to live on. Every form of the reasoned descope was refused on
such a plan, including the form the refusal itself recommended, because the
guard read reasons only from records that did not exist.

The reason is stored in a ``plan-descope-reasons`` head meta map for a section
with no record, and on the record where the plan has one; the guard and the
read consult the record first and the map second. A descope without a reason is
still refused, and the refusal names a form that succeeds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "descope-any-project"
SLUG = "descope-any-case"

DESCOPED_REASON = "the downstream consumer retired the interface this section builds"


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


def _recordless_plan(slug: str, state: dict) -> str:
    """A plan with a declarations map and bare, id-less headings."""
    base = dict(state)
    base.setdefault("slug", slug)
    base.setdefault("title", slug.title())
    base.setdefault("type", "plan")
    base.setdefault("status", "active")
    headings = "".join(
        f"<h2>part {n}</h2>" for n in range(1, len(base["section_declarations"]) + 1)
    )
    bare = (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head>\n"
        f'<body><main class="plan-doc">{headings}</main></body>\n</html>\n'
    )
    return write_state(bare, base)


def _write_plan(docs_dir: Path, slug: str, html_text: str) -> Path:
    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")
    return path


def _section(ident: str, status: str, hours: float = 2.0):
    return {
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


def _recorded_plan(slug: str, state: dict) -> str:
    base = dict(state)
    base.setdefault("slug", slug)
    base.setdefault("title", slug.title())
    base.setdefault("type", "plan")
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


def _recordless(docs_dir: Path) -> None:
    _write_plan(
        docs_dir,
        SLUG,
        _recordless_plan(SLUG, {"version": 0, "section_declarations": {"s1": "implementable", "s4": "implementable"}}),
    )


def test_the_dotted_form_with_a_reason_stores_the_descope(setup) -> None:
    docs_dir, _, project = setup
    _recordless(docs_dir)

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
    assert data["section_declarations"]["s1"] == "descoped", "the descope is stored"
    assert data["descope_reasons"]["s1"] == DESCOPED_REASON, "the reason is stored"
    assert data["section_declarations"]["s4"] == "implementable", "siblings untouched"


def test_the_whole_map_form_takes_a_reasoned_value(setup) -> None:
    docs_dir, _, project = setup
    _recordless(docs_dir)

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations",
                "value": {
                    "s1": {"status": "descoped", "reason": DESCOPED_REASON},
                    "s4": "implementable",
                },
            }
        ],
        0,
    )

    assert result["ok"] is True, result
    data, _version = _store_module.read_plan(project, SLUG)
    assert data["section_declarations"] == {"s1": "descoped", "s4": "implementable"}, (
        "the map stays a map of strings"
    )
    assert data["descope_reasons"]["s1"] == DESCOPED_REASON, "the reason is stored"


def test_an_unreasoned_descope_is_refused_naming_a_form_that_works(setup) -> None:
    docs_dir, _, project = setup
    _recordless(docs_dir)

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [{"op": "set", "path": "section_declarations.s1", "value": "descoped"}],
        0,
    )

    assert result.get("ok") is not True, result
    message = str(result)
    assert '{"status": "descoped", "reason":' in message, (
        "the refusal names the reasoned form the dotted write accepts"
    )

    # The refusal names a form, and that form is the one that works: the same
    # plan, the same section, the named shape.
    accepted = mcp_module._edit_plan(
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

    assert accepted["ok"] is True, accepted
    data, _version = _store_module.read_plan(project, SLUG)
    assert data["descope_reasons"]["s1"] == DESCOPED_REASON


def test_two_sequential_descopes_leave_both_reasons(setup) -> None:
    docs_dir, _, project = setup
    _recordless(docs_dir)
    second_reason = "the second interface was never adopted by any consumer"

    version = 0
    for section, reason in (("s1", DESCOPED_REASON), ("s4", second_reason)):
        result = mcp_module._edit_plan(
            project,
            SLUG,
            [
                {
                    "op": "set",
                    "path": f"section_declarations.{section}",
                    "value": {"status": "descoped", "reason": reason},
                }
            ],
            version,
        )
        assert result["ok"] is True, result
        version = result["new_version"]

    data, _version = _store_module.read_plan(project, SLUG)
    assert data["section_declarations"] == {"s1": "descoped", "s4": "descoped"}
    assert data["descope_reasons"] == {
        "s1": DESCOPED_REASON,
        "s4": second_reason,
    }, "the second descope lands beside the first rather than replacing it"


def test_a_declaration_leaving_descoped_clears_the_map_entry(setup) -> None:
    docs_dir, _, project = setup
    _recordless(docs_dir)

    descoped = mcp_module._edit_plan(
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
    assert descoped["ok"] is True, descoped

    reinstated = mcp_module._edit_plan(
        project,
        SLUG,
        [{"op": "set", "path": "section_declarations.s1", "value": "implementable"}],
        1,
    )

    assert reinstated["ok"] is True, reinstated
    data, _version = _store_module.read_plan(project, SLUG)
    assert data["section_declarations"]["s1"] == "implementable"
    assert "s1" not in (data.get("descope_reasons") or {}), (
        "a reason beside a declaration that is no longer descoped contradicts it"
    )


def test_the_whole_map_form_works_on_a_plan_with_records(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        _recorded_plan(
            SLUG,
            {
                "version": 0,
                "section_declarations": {"s1": "implementable", "s2": "implementable"},
                "sections": [
                    _section("s1", "implementable"),
                    _section("s2", "implementable"),
                ],
            },
        ),
    )

    result = mcp_module._edit_plan(
        project,
        SLUG,
        [
            {
                "op": "set",
                "path": "section_declarations",
                "value": {
                    "s1": {"status": "descoped", "reason": DESCOPED_REASON},
                    "s2": "done",
                },
            }
        ],
        0,
    )

    assert result["ok"] is True, result
    data, _version = _store_module.read_plan(project, SLUG)
    assert data["section_declarations"] == {"s1": "descoped", "s2": "done"}
    records = {row["id"]: row for row in data["sections"]}
    assert records["s1"]["descope_reason"] == DESCOPED_REASON, (
        "the reasoned entry reaches the record on a plan that has one"
    )
    assert records["s2"]["status"] == "done"
    assert "s1" not in (data.get("descope_reasons") or {})


def test_a_plan_with_records_keeps_the_reason_on_the_record(setup) -> None:
    docs_dir, _, project = setup
    _write_plan(
        docs_dir,
        SLUG,
        _recorded_plan(
            SLUG,
            {
                "version": 0,
                "section_declarations": {"s1": "implementable"},
                "sections": [_section("s1", "implementable")],
            },
        ),
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
    record = {row["id"]: row for row in data["sections"]}["s1"]
    assert record["descope_reason"] == DESCOPED_REASON, "the record holds the reason"
    assert "s1" not in (data.get("descope_reasons") or {}), (
        "the map holds no duplicate of a reason the record already carries"
    )
