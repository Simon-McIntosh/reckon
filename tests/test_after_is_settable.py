"""edit_plan rewrites a plan's whole ``after`` list, one ref grammar for all links.

A review that finds a stale soft edge could only remove it by editing the plan
HTML, because ``after`` was absent from the store's settable plan fields: a
state ``set`` refused it as an unsupported path, and text mode refuses any
change to the parsed state. These cases drive the set through ``edit_plan`` on a
temporary project and read it back two ways — through ``read_plan`` and off the
``plan-after`` meta in the document — so a set that reached only the parsed dict
and not the markup would be caught.

The declared negative control removes ``after`` from the store's settable plan
fields: every case here then fails as an unsupported path again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "proj"
PLAN = "after-plan"
_CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Hermetic temp docs dir + mounts + state root. Returns the docs dir."""
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


def _make_plan(docs_dir: Path) -> Path:
    authored = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        "<title>After plan</title></head>\n"
        '<body><main class="plan-doc">\n'
        '<h2 id="s1">&sect;1 &mdash; One</h2>\n'
        "<p>Body one.</p>\n"
        '<h2 id="s2">&sect;2 &mdash; Two</h2>\n'
        "<p>Body two.</p>\n"
        "</main></body>\n</html>\n"
    )
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "After plan",
        "status": "active",
        "standalone": "fixture plan, unwired by design",
        "version": 0,
        "section_declarations": {"s1": "implementable", "s2": "implementable"},
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": _CAPABILITY,
                "attempts": 0,
                "status": "implementable",
                "links": [],
            }
            for section_id in ("s1", "s2")
        ],
    }
    path = docs_dir / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


def _edit(docs_dir: Path, ops: list[dict], version: int):
    import reckon.mcp as mcp_module

    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        checkout_path=str(docs_dir.parent),
        mode="state",
        ops=ops,
    )


def _set_after(docs_dir: Path, refs: list[str], version: int) -> dict:
    return _edit(docs_dir, [{"op": "set", "path": "after", "value": refs}], version)


def _version(docs_dir: Path) -> int:
    text = (docs_dir / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    return int(_plan_html.read_state(text).get("version", 0))


def _read_after(docs_dir: Path) -> list[str]:
    import reckon.mcp as mcp_module

    result = mcp_module._read_plan_tool(
        project=PROJECT,
        slug=PLAN,
        checkout_path=str(docs_dir.parent),
        view="raw",
    )
    assert result.get("data"), result
    return list(result["data"].get("after") or [])


def _meta_after(docs_dir: Path) -> list[str]:
    """The refs the document's own ``plan-after`` meta carries."""
    return list(
        _plan_html.parse_meta(docs_dir / "plans" / f"{PLAN}.html").get("after") or []
    )


def test_after_is_replaced_as_a_whole_list(setup):
    docs_dir = setup
    _make_plan(docs_dir)

    result = _set_after(docs_dir, ["mm-unshipped"], 0)
    assert result["ok"] is True, result

    # Read back through the plan read, and off the document's own meta, so a
    # set that reached only the parsed state and not the markup is caught.
    assert _read_after(docs_dir) == ["mm-unshipped"]
    assert _meta_after(docs_dir) == ["mm-unshipped"]


def test_after_can_be_emptied(setup):
    docs_dir = setup
    _make_plan(docs_dir)
    assert _set_after(docs_dir, ["mm-unshipped"], 0)["ok"] is True

    result = _set_after(docs_dir, [], _version(docs_dir))
    assert result["ok"] is True, result
    assert _read_after(docs_dir) == []
    assert _meta_after(docs_dir) == []


def test_after_takes_a_qualified_ref(setup):
    docs_dir = setup
    _make_plan(docs_dir)

    result = _set_after(docs_dir, ["nova:other-plan#s3"], 0)
    assert result["ok"] is True, result
    assert _read_after(docs_dir) == ["nova:other-plan#s3"]
    assert _meta_after(docs_dir) == ["nova:other-plan#s3"]


def test_unparseable_after_ref_is_refused_naming_it(setup):
    docs_dir = setup
    _make_plan(docs_dir)

    result = _set_after(docs_dir, ["not a ref"], 0)
    assert result["ok"] is False, result
    assert result["error"] == "schema_validation", result
    joined = "\n".join(result["details"])
    assert "not a ref" in joined
    assert "after" in joined
    # Nothing landed: the plan still reads empty and its version is unmoved.
    assert _read_after(docs_dir) == []
    assert _version(docs_dir) == 0
