"""List-declared tool arguments accept a JSON-text delivery.

Some clients serialise a list argument into a JSON string rather than handing
over a native list, and the strict argument wrapper in ``reckon.mcp`` must
accept that delivery without letting a genuine type error through. Only an
argument the model declares as a list is ever recognised from JSON text; a
text argument whose content happens to parse as JSON is passed through
unchanged, text that does not parse is left for normal validation, and the
published schemas still declare the arguments as lists.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
import reckon.serve as serve_module

# ── Fixtures ────────────────────────────────────────────────────────────────


def _plan(docs_dir, slug, *, project="proj", version=3, prose=""):
    from reckon._plan_html import write_state

    state = {
        "slug": slug,
        "title": "Readable Plan",
        "summary": "A readable plan.",
        "version": version,
        "type": "plan",
        "status": "active",
        "impl": 0.4,
        "decisions": {
            "open-choice": {
                "title": "Choose a route",
                "choices": ["a", "b"],
                "choice": "",
            }
        },
        "followups": [
            {
                "id": "next",
                "status": "open",
                "title": "Continue",
                "body": "<p>Do the next thing.</p>",
                "prompt": "Project: proj\nDone-when\n  1. shipped",
                "written_by": "test",
                "written_at": "2026-01-01",
            }
        ],
        "comments": [
            {
                "id": "c1",
                "body": "<p>existing</p>",
                "written_by": "test",
                "written_at": "2026-01-01",
            }
        ],
    }
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        "<title>Readable</title></head>"
        f'<body><main class="plan-doc">{prose}</main></body></html>'
    )
    path = docs_dir / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    serve_module._MOUNTS_FILE = mounts_file
    serve_module._STATE_ROOT = state_root
    importlib.reload(store_module)
    importlib.reload(mcp_module)
    return docs_dir, project


def _mcp_tool(name: str):
    """Return one client-facing tool entry rather than its implementation helper."""
    return next(
        item for item in mcp_module.mcp._tool_manager.list_tools() if item.name == name
    )


def _call_mcp(name: str, **arguments):
    """Exercise the argument validation and dispatch path an MCP client reaches."""
    return asyncio.run(_mcp_tool(name).run(arguments))


def _strict_argument_model(name: str):
    """The strict wrapper that validates the tool's incoming arguments."""
    return _mcp_tool(name).fn_metadata.arg_model


# ── A list argument delivered as JSON text ──────────────────────────────────


def _comment_append(body: str) -> list[dict]:
    return [
        {
            "op": "append",
            "target": "comments",
            "item": {"body": body, "written_by": "test", "written_at": "2026-01-01"},
        }
    ]


def test_edit_plan_accepts_ops_delivered_as_json_text_identically_to_a_list(setup):
    """A list argument as JSON text produces the same edit outcome as the list.

    Two identical plans are edited with the same ``ops`` payload, one delivered
    as a native list and one as its JSON text; the outcomes must be equal rather
    than merely successful. The body is a long comment like the one the peer's
    client serialised into text.
    """
    docs_dir, project = setup
    _plan(docs_dir, "as-list")
    _plan(docs_dir, "as-text")

    body = "A long comment body " + ("content that keeps going " * 60)
    ops = _comment_append(body)
    ops_text = json.dumps(ops)

    as_list = _call_mcp(
        "_edit_plan",
        project=project,
        slug="as-list",
        expected_version=3,
        ops=ops,
    )
    as_text = _call_mcp(
        "_edit_plan",
        project=project,
        slug="as-text",
        expected_version=3,
        ops=ops_text,
    )

    assert as_list["ok"] is True, as_list
    assert as_text["ok"] is True, as_text
    assert as_text["message"] == as_list["message"]
    assert as_text["new_version"] == as_list["new_version"]
    assert "version 4" in as_text["message"]


def test_the_strict_argument_model_parses_every_declared_list_from_json_text():
    """Every list-declared argument is recognised from JSON text, not one named one.

    ``_edit_plan.ops`` and both list arguments on ``_crew`` must parse a JSON-text
    delivery into exactly the same value as a native list delivery.
    """
    edit_model = _strict_argument_model("_edit_plan")
    ops = [{"op": "append", "target": "comments", "item": {"body": "<p>hi</p>"}}]
    as_list = edit_model.model_validate(
        {"project": "p", "slug": "s", "expected_version": 1, "ops": ops}
    ).model_dump()
    as_text = edit_model.model_validate(
        {
            "project": "p",
            "slug": "s",
            "expected_version": 1,
            "ops": json.dumps(ops),
        }
    ).model_dump()
    assert as_text == as_list
    assert as_text["ops"] == ops

    crew_model = _strict_argument_model("_crew")
    candidates = [{"run_id": "run-1"}]
    fields = ["project", "view"]
    as_list = crew_model.model_validate(
        {"project": "p", "candidates": candidates, "fields": fields}
    ).model_dump()
    as_text = crew_model.model_validate(
        {
            "project": "p",
            "candidates": json.dumps(candidates),
            "fields": json.dumps(fields),
        }
    ).model_dump()
    assert as_text == as_list
    assert as_text["candidates"] == candidates
    assert as_text["fields"] == fields


def test_json_text_that_parses_to_a_mapping_is_rejected_naming_the_argument(setup):
    docs_dir, project = setup
    _plan(docs_dir, "mapping")

    with pytest.raises(Exception, match="ops") as rejected:
        _call_mcp(
            "_edit_plan",
            project=project,
            slug="mapping",
            expected_version=3,
            ops='{"op": "append"}',
        )
    assert "Input should be a valid list" in str(rejected.value)

    with pytest.raises(Exception, match="ops"):
        _strict_argument_model("_edit_plan").model_validate(
            {"project": "p", "slug": "s", "expected_version": 1, "ops": '{"op": "x"}'}
        )


def test_text_that_does_not_parse_is_rejected_naming_the_argument(setup):
    docs_dir, project = setup
    _plan(docs_dir, "broken")

    with pytest.raises(Exception, match="ops") as rejected:
        _call_mcp(
            "_edit_plan",
            project=project,
            slug="broken",
            expected_version=3,
            ops="{ops but not valid json [",
        )
    assert "Input should be a valid list" in str(rejected.value)


def test_a_list_argument_delivered_as_an_integer_is_rejected(setup):
    docs_dir, project = setup
    _plan(docs_dir, "int")

    with pytest.raises(Exception, match="ops") as rejected:
        _call_mcp(
            "_edit_plan",
            project=project,
            slug="int",
            expected_version=3,
            ops=42,
        )
    assert "Input should be a valid list" in str(rejected.value)


# ── A text argument is never coerced ────────────────────────────────────────


def test_a_text_argument_whose_value_parses_as_json_is_passed_unchanged(setup):
    """An authored HTML argument containing a bracketed list is not rewritten.

    Coercing it would corrupt a document, so the delivered string must reach the
    model exactly as sent.
    """
    docs_dir, project = setup
    prose = '<p class="notes">[options: a, b]</p>'
    _plan(docs_dir, "authored", prose=prose)

    old = '<p class="notes">[options: a, b]</p>'
    new = '<p class="notes">[options: c, d]</p>'
    result = _call_mcp(
        "_edit_plan",
        project=project,
        slug="authored",
        expected_version=3,
        mode="text",
        old_html=old,
        new_html=new,
    )
    assert result["ok"] is True, result
    assert new in (docs_dir / "authored.html").read_text(encoding="utf-8")
    assert old not in (docs_dir / "authored.html").read_text(encoding="utf-8")

    model = _strict_argument_model("_edit_plan")
    dump = model.model_validate(
        {
            "project": "p",
            "slug": "s",
            "expected_version": 1,
            "new_html": "<p>[a, b]</p>",
        }
    ).model_dump()
    assert dump["new_html"] == "<p>[a, b]</p>"


def test_an_unknown_parameter_is_still_rejected_with_the_accepted_list():
    """Unknown names are still refused, and the message lists what is accepted."""
    payloads = {
        "_read_plan": {"document": "x"},
        "_edit_plan": {
            "project": "p",
            "slug": "s",
            "expected_version": 1,
            "document": "x",
        },
    }
    for name, payload in payloads.items():
        tool = _mcp_tool(name)
        accepted = sorted(tool.parameters["properties"])

        with pytest.raises(Exception, match="Unknown parameters") as rejected:
            _call_mcp(name, **payload)

        message = str(rejected.value)
        assert "document" in message
        assert "Accepted parameters:" in message
        assert all(parameter in message for parameter in accepted)


def _declares_array(schema: dict) -> bool:
    """Return whether a JSON-schema property declares the value as an array.

    Optional list fields publish as an ``anyOf`` between the array form and
    ``null``, so the array declaration may sit on a variant rather than on the
    property itself.
    """

    if schema.get("type") == "array":
        return True
    return any(
        _declares_array(variant)
        for variant in schema.get("anyOf") or []
        if isinstance(variant, dict)
    )


def test_the_published_schema_still_declares_list_arguments_as_arrays():
    """Wrapping must not change what a client reading the schema sees."""
    edit_properties = _mcp_tool("_edit_plan").parameters["properties"]
    assert _declares_array(edit_properties["ops"])

    crew_properties = _mcp_tool("_crew").parameters["properties"]
    assert _declares_array(crew_properties["candidates"])
    assert _declares_array(crew_properties["fields"])
