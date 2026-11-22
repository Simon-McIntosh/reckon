"""A section's record status and its declaration must state the same thing.

The served record and the dispatch that reads the declaration are two views of
one fact, so a plan that leaves them disagreeing reads landed to one reader and
open to another. Three surfaces accept a plan document and each refuses such a
document by naming the section and both values: ``reckon audit-doc`` reports it
as an authored defect, the server's plan write refuses the file it is handed,
and a promotion refuses to commit it. A plan whose two views agree passes all
three.

Every specimen is synthesised: a live plan is a fuse. The disagreeing document
is an agreeing one whose declaration meta was rewritten, which is the shape a
direct authored edit leaves behind.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, doccheck, ledger
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

_DECLARATIONS_META = re.compile(
    r'<meta name="plan-section-declarations" content="[^"]*">'
)

RUN_ID = "r-20261122T190000000000-a-disagreeing-section"
SECTION = "s1"
DISAGREES = "section_status_disagreement"
PLAN_PATH = f"docs/plans/{PLAN}.html"

_RECORD = (
    '<h2 id="{sid}">Section one</h2>'
    '<section data-reckon="section" data-id="{sid}" data-effort-hours="1.0"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard"'
    ' data-capability-verification="strict" data-capability-risk="low"'
    ' data-status="{status}" data-links=""></section>'
)


def _with_declaration(html_text: str, declarations: Mapping[str, str]) -> str:
    """The document with its ``plan-section-declarations`` meta set.

    An existing meta is replaced rather than a second one added: the head parse
    takes the first occurrence in document order, so a second copy would leave
    the document reading as whichever was written first.
    """
    meta = (
        '<meta name="plan-section-declarations" content="'
        + json.dumps(dict(declarations)).replace('"', "&quot;")
        + '">'
    )
    replaced, count = _DECLARATIONS_META.subn(lambda _match: meta, html_text, count=1)
    if count:
        return replaced
    return html_text.replace("<title>", meta + "<title>", 1)


def _document(declaration: str, *, record_status: str = "done") -> str:
    """A one-section plan whose two views are set independently."""
    body = _RECORD.format(sid=SECTION, status=record_status)
    authored = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<meta name="plan-status" content="active">'
        f"<title>{PLAN}</title></head><body>"
        f'<main class="plan-doc">{body}</main></body></html>\n'
    )
    return _plan_html.write_state(
        _with_declaration(authored, {SECTION: declaration}),
        {
            "type": "plan",
            "slug": PLAN,
            "title": PLAN,
            "status": "active",
            "version": 0,
            "section_declarations": {SECTION: declaration},
            "sections": [
                {
                    "id": SECTION,
                    "effort_hours": 1.0,
                    "capability": {
                        "version": "1.0",
                        "class": "general",
                        "requirements": {
                            "reasoning": "standard",
                            "verification": "strict",
                            "risk": "low",
                        },
                    },
                    "attempts": 0,
                    "status": record_status,
                    "links": [],
                }
            ],
        },
    )


def _disagreeing() -> str:
    """A record reading done beside a declaration reading implementable."""
    text = _document("done")
    return _with_declaration(text, {SECTION: "implementable"})


@pytest.fixture()
def agreeing_plan() -> str:
    return _document("done")


def test_audit_names_the_section_and_both_values(tmp_path: Path) -> None:
    path = tmp_path / "plan.html"
    path.write_text(_disagreeing(), encoding="utf-8")

    findings = doccheck.audit_file(path)

    named = [f for f in findings if f.code == doccheck._SECTION_STATUS_DISAGREES]
    assert named, [f.code for f in findings]
    assert named[0].severity == "error"
    assert SECTION in named[0].message, named[0].message
    assert "'done'" in named[0].message, named[0].message
    assert "'implementable'" in named[0].message, named[0].message
    assert doccheck.run([str(path)]) != 0


def test_audit_passes_a_plan_whose_two_views_agree(
    tmp_path: Path, agreeing_plan: str
) -> None:
    path = tmp_path / "plan.html"
    path.write_text(agreeing_plan, encoding="utf-8")

    findings = doccheck.audit_file(path)

    assert not [f for f in findings if f.code == doccheck._SECTION_STATUS_DISAGREES]
    assert not [f for f in findings if f.severity == "error"], [
        (f.severity, f.code) for f in findings
    ]
    assert doccheck.run([str(path)]) == 0


@pytest.fixture
def mounts_cache():
    import reckon.serve as serve_module

    saved = dict(serve_module._DISC_CACHE)
    serve_module._DISC_CACHE.clear()
    try:
        yield serve_module
    finally:
        serve_module._DISC_CACHE.clear()
        serve_module._DISC_CACHE.update(saved)


def test_the_server_plan_write_refuses_the_disagreeing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mounts_cache
) -> None:
    serve_module = mounts_cache
    checkout = tmp_path / "repo"
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True)
    path.write_text(_disagreeing(), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(checkout / "docs")}), encoding="utf-8")
    monkeypatch.setattr(serve_module, "_MOUNTS_FILE", mounts)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()

    server = serve_module.ThreadingHTTPServer(("127.0.0.1", 0), serve_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(
            "POST",
            f"/plan/{PROJECT}/{PLAN}",
            body=json.dumps({"title": "A new title"}),
            headers={"Content-Type": "application/json", "If-Match": "0"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
        server.shutdown()

    assert response.status == 400, payload
    assert payload["error"] == DISAGREES, payload
    assert SECTION in payload["sections"], payload
    assert "done" in payload["detail"] and "implementable" in payload["detail"]
    assert path.read_text(encoding="utf-8") == before, "the refusal must not write"

@pytest.fixture()
def disagreeing_landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """A finished run whose project's plan was committed with the disagreement."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    plan_path = repository / PLAN_PATH
    plan_path.write_text(_disagreeing(), encoding="utf-8")
    _git(repository, "add", "--", PLAN_PATH)
    _git(repository, "commit", "-q", "-m", "docs: reopen a section's declaration")
    _git(repository, "push", "-q", "origin", "main")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktrees" / "disagreeing"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text("the reviewed change\n", encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "attempt": 1,
            "phase": "delivered",
            "created_at": "2026-11-22T19:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": SECTION,
                "time_began": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": str(worktree),
        "run_id": RUN_ID,
        "head": head,
        "gate_log": gate_log,
        "plan_before": plan_path.read_text(encoding="utf-8"),
    }


def test_a_promotion_refuses_to_commit_the_disagreeing_plan(
    disagreeing_landing: dict[str, Any],
) -> None:
    repository = disagreeing_landing["repository"]

    result = _complete_land(disagreeing_landing)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), (
        "the promotion crashed rather than refusing: "
        f"{result.exception!r}"
    )
    assert SECTION in result.output, result.output
    assert "record status" in result.output, result.output
    assert "'done'" in result.output and "'implementable'" in result.output, (
        result.output
    )
    assert (repository / PLAN_PATH).read_text(
        encoding="utf-8"
    ) == disagreeing_landing["plan_before"], "the refusal must not write the plan"
    row = Path(
        __import__("reckon.ledger", fromlist=["run_path"]).run_path(
            PROJECT, disagreeing_landing["run_id"], repository
        )
    )
    assert not row.exists(), f"a refused landing wrote a ledger row at {row}"
