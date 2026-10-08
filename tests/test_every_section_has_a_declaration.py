"""An authored heading and its section declaration must cover each other.

A plan's todo list is one item per authored section, ticked from its
``section_declarations`` entry. A heading with no declaration has no item to
tick; a declaration naming no heading points at a section the reader cannot
open. ``reckon audit-doc`` reports both, in every plan that carries one, and a
write is refused only when it introduces a gap the plan did not carry before it:
most live plans predate section declarations, so refusing a plan for a gap that
was already there would refuse it at its next write.
"""

from __future__ import annotations

import http.client
import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from reckon import _plan_html, doccheck
from reckon import mcp as mcp_module

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}


def _record(section_id: str, status: str) -> dict:
    return {
        "id": section_id,
        "effort_hours": 1.0,
        "capability": deepcopy(CAPABILITY),
        "attempts": 0,
        "status": status,
        "links": [],
    }


def _write_plan(
    tmp_path: Path,
    headings: list[tuple[str, str]],
    declarations: dict[str, str],
    records_for: set[str] | None = None,
    followups: list[dict] | None = None,
) -> tuple[Path, Path]:
    """Author a plan file directly, bypassing the write path.

    The fixture is written with ``write_state`` so it carries whatever coverage
    the test needs; the guard only governs writes made through ``edit_plan``. A
    typed record is written only for an id that also carries a heading, because
    ``write_state`` refuses a record whose heading is absent.
    """
    checkout = tmp_path / "repo"
    path = checkout / "docs" / "plans" / "coverage.html"
    path.parent.mkdir(parents=True)
    body = "".join(
        f'<h2 id="{sid}">{title}</h2><p>{title} body.</p>' for sid, title in headings
    )
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<title>Coverage</title></head><body><main class="plan-doc">'
        f"{body}"
        "</main></body></html>"
    )
    heading_ids = {sid for sid, _ in headings}
    wanted = heading_ids if records_for is None else records_for
    plan_state = {
        "project": "sample",
        "type": "plan",
        "slug": "coverage",
        "title": "Coverage",
        "status": "active",
        "modified": "2026-10-08",
        "version": 0,
        "section_declarations": deepcopy(declarations),
        "followups": deepcopy(followups or []),
        "sections": [
            _record(sid, status)
            for sid, status in declarations.items()
            if sid in heading_ids and sid in wanted
        ],
    }
    path.write_text(_plan_html.write_state(authored, plan_state), encoding="utf-8")
    return checkout, path


def _edit(checkout: Path, path: Path, op: dict) -> dict:
    state = _plan_html.read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        "sample",
        "coverage",
        expected_version=state["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=[op],
    )


def _codes(html_text: str) -> set[str]:
    return {finding.code for finding in doccheck.audit_html(html_text)}


def _set_declarations(value: dict[str, str]) -> dict:
    return {"op": "set", "path": "section_declarations", "value": value}


def test_a_write_adding_an_undeclared_heading_is_refused_and_reported(
    tmp_path: Path,
) -> None:
    # Gap-free to start: both headings carry a declaration, so nothing is owed
    # before the write.
    checkout, path = _write_plan(
        tmp_path, [("s1", "One"), ("s2", "Two")], {"s1": "done", "s2": "implementable"}
    )
    before = path.read_text(encoding="utf-8")
    assert doccheck._SECTION_WITHOUT_TODO not in _codes(before)

    result = _edit(checkout, path, _set_declarations({"s1": "done"}))

    assert result["ok"] is False, result
    assert "s2" in result["detail"], result
    assert "section-without-todo" in result["detail"], result
    assert path.read_text(encoding="utf-8") == before

    # The audit still reports the same gap on a plan that carries it, refusing
    # nothing: the guard governs writes, not the report.
    _, gapped = _write_plan(
        tmp_path / "gapped", [("s1", "One"), ("s2", "Two")], {"s1": "done"}
    )
    assert doccheck._SECTION_WITHOUT_TODO in _codes(gapped.read_text(encoding="utf-8"))


def test_a_write_adding_an_orphan_declaration_is_refused_and_reported(
    tmp_path: Path,
) -> None:
    checkout, path = _write_plan(tmp_path, [("s1", "One")], {"s1": "done"})
    before = path.read_text(encoding="utf-8")
    assert doccheck._TODO_WITHOUT_SECTION not in _codes(before)

    result = _edit(
        checkout,
        path,
        _set_declarations({"s1": "done", "s9": "implementable"}),
    )

    assert result["ok"] is False, result
    assert "s9" in result["detail"], result
    assert "todo-without-section" in result["detail"], result
    assert path.read_text(encoding="utf-8") == before

    _, gapped = _write_plan(
        tmp_path / "gapped",
        [("s1", "One")],
        {"s1": "done", "s9": "implementable"},
    )
    assert doccheck._TODO_WITHOUT_SECTION in _codes(gapped.read_text(encoding="utf-8"))


def test_inserting_a_heading_with_its_declaration_is_accepted(tmp_path: Path) -> None:
    checkout, path = _write_plan(
        tmp_path, [("s1", "One"), ("s2", "Two")], {"s1": "done", "s2": "implementable"}
    )

    result = _edit(
        checkout,
        path,
        {
            "op": "insert_section",
            "id": "s3",
            "title": "Three",
            "body": "<p>Third body.</p>",
            "effort_hours": 1.25,
            "capability": deepcopy(CAPABILITY),
            "links": [],
        },
    )

    assert result["ok"] is True, result
    after = _plan_html.read_state(path.read_text(encoding="utf-8"))
    assert after["section_declarations"]["s3"] == "implementable"
    assert "s3" in {
        identity
        for identity, _ in _plan_html.authored_section_headings(path.read_text())
    }


def test_text_edit_removing_a_declared_heading_is_refused(tmp_path: Path) -> None:
    checkout, path = _write_plan(
        tmp_path,
        [("s1", "One"), ("s2", "Two")],
        {"s1": "done", "s2": "done"},
        records_for={"s1"},
    )
    before = path.read_text(encoding="utf-8")
    version = _plan_html.read_state(before)["version"]

    result = mcp_module._edit_plan_tool(
        "sample",
        "coverage",
        expected_version=version,
        checkout_path=str(checkout),
        doc_type="plan",
        mode="text",
        old_html='<h2 id="s2">Two</h2><p>Two body.</p>',
        new_html="<p>Two prose without a heading.</p>",
    )

    assert result["ok"] is False, result
    assert "s2" in result["detail"], result
    assert "todo-without-section" in result["detail"], result
    assert path.read_text(encoding="utf-8") == before


def test_a_plan_already_carrying_a_gap_accepts_an_unrelated_write(
    tmp_path: Path,
) -> None:
    # ``s2`` carries an authored heading with no declaration. The gap predates
    # the write, so the write is not refused over it.
    checkout, path = _write_plan(
        tmp_path,
        [("s1", "One"), ("s2", "Two"), ("s3", "Three")],
        {"s1": "implementable", "s3": "implementable"},
    )
    before = path.read_text(encoding="utf-8")
    assert doccheck._SECTION_WITHOUT_TODO in _codes(before)

    accepted = _edit(
        checkout, path, {"op": "set", "path": "title", "value": "Revised coverage"}
    )

    assert accepted["ok"] is True, accepted
    after = path.read_text(encoding="utf-8")
    assert doccheck._SECTION_WITHOUT_TODO in _codes(after)

    # A second undeclared heading is a gap this write adds, so it is refused,
    # and only the id the write added is named.
    refused = _edit(checkout, path, _set_declarations({"s1": "implementable"}))

    assert refused["ok"] is False, refused
    assert "s3" in refused["detail"], refused
    assert "section-without-todo" in refused["detail"], refused


def test_a_landing_with_an_undeclared_heading_is_refused(tmp_path: Path) -> None:
    """A plan cannot land while an authored heading carries no declaration.

    Reading the declaration map alone, a heading nothing declares has no open
    entry and the plan reaches ``done`` with a section nobody ever answered for.
    The open followup keeps the continuation rule satisfied, so the write is
    refused by the section guard rather than by the continuation rule.
    """
    checkout, path = _write_plan(
        tmp_path,
        [("s1", "One"), ("s2", "Two")],
        {"s1": "done"},
        records_for={"s1"},
        followups=[
            {
                "id": "f-next",
                "status": "open",
                "prompt": "/reckon-build coverage §2",
            }
        ],
    )
    before = path.read_text(encoding="utf-8")

    result = _edit(checkout, path, {"op": "set", "path": "status", "value": "done"})

    assert result["ok"] is False, result
    assert "s2" in result["detail"], result
    assert "declaration" in result["detail"], result
    assert path.read_text(encoding="utf-8") == before


def test_the_server_patch_path_refuses_a_write_that_drops_a_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP patch path renders its own HTML and must checks it too.

    ``POST /plan/<project>/<slug>`` patches state and renders the file with
    ``write_state`` without reaching either store write path, so a patch that
    drops a declaration would otherwise walk past the coverage guard.
    """
    checkout, path = _write_plan(tmp_path, [("s1", "One")], {"s1": "done"})
    docs = checkout / "docs"
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    serve_module._DISC_CACHE.clear()

    before = path.read_text(encoding="utf-8")
    state, _ = _plan_html.read_state_and_text_file(path)
    version = state["version"]

    server = serve_module.ThreadingHTTPServer(("127.0.0.1", 0), serve_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(
            "POST",
            "/plan/sample/coverage",
            body=json.dumps({"section_declarations": {}}),
            headers={"Content-Type": "application/json", "If-Match": str(version)},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
        server.shutdown()

    assert response.status == 400, payload
    assert payload["error"] == "section_coverage", payload
    assert "s1" in payload["sections_without_declaration"], payload
    assert path.read_text(encoding="utf-8") == before, "the refusal must not write"
