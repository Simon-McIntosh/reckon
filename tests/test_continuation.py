"""Continuation closes at three altitudes: worker, plan landing, sprint close.

A chain that closes only at plan level leaves the other two ends dangling — a
worker's out-of-scope discovery has nowhere to go, and a closing sprint cannot
say what it lets us start. Each altitude is tested here, and the last test walks
the whole chain from a worker manifest to a sprint rollup.
"""

from __future__ import annotations

import http.client
import importlib
import json
import threading
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from reckon import crew
from reckon.roadmap import build_roadmap


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
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

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root
    importlib.reload(_store_module)
    importlib.reload(mcp_module)
    return docs_dir, state_root, project


def _followup(ident: str, *, status: str = "open", outcome: str = "") -> dict:
    item = {
        "id": ident,
        "title": f"work {ident}",
        "body": "why",
        "written_by": "reckon-ship",
        "written_at": "2026-08-12",
        "prompt": "/reckon-ship plan-a §2",
    }
    if status == "resolved":
        item.update(
            {
                "resolved_at": "2026-08-12T00:00:00Z",
                "resolved_by": "smc",
                "outcome": outcome,
            }
        )
    return item


def _plan_html(
    docs_dir: Path, slug: str, state: dict, artifact_type: str = "plan"
) -> Path:
    from reckon._plan_html import write_state

    base = dict(state)
    base.setdefault("slug", slug)
    base.setdefault("title", slug.title())
    base.setdefault("status", "active")
    base.setdefault("type", artifact_type)
    bare = (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    path = docs_dir / f"{slug}.html"
    path.write_text(write_state(bare, base), encoding="utf-8")
    return path


# ── Plan altitude ───────────────────────────────────────────────────────────


def test_a_landing_with_no_continuation_is_refused(setup) -> None:
    """The rule existed but was enforced by discipline alone."""
    docs_dir, _, project = setup
    _plan_html(docs_dir, "plan-a", {"version": 0, "followups": [_followup("f1")]})

    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {"op": "set", "path": "impl", "value": 1.0},
            {"op": "set", "path": "status", "value": "shipped"},
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "reckon-ship",
                "outcome": "§2 landed — commit 1a2b3c4",
            },
        ],
        0,
    )

    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert "/reckon-ship <slug> [§N]" in result["detail"]
    # Refused writes nothing: the plan is untouched at its old version.
    data, version = _store_module.read_plan(project, "plan-a")
    assert version == 0
    assert data["status"] == "active"


def test_a_landing_that_appends_the_next_invocation_is_accepted(setup) -> None:
    docs_dir, _, project = setup
    _plan_html(docs_dir, "plan-a", {"version": 0, "followups": [_followup("f1")]})

    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "reckon-ship",
                "outcome": "§2 landed — commit 1a2b3c4; 28 tests green",
            },
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": "f2",
                    "status": "open",
                    "title": "land §3",
                    "body": "next",
                    "written_by": "reckon-ship",
                    "written_at": "2026-08-12",
                    "prompt": "/reckon-ship plan-a §3",
                },
            },
        ],
        0,
    )

    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert [f["id"] for f in data["followups"]] == ["f1", "f2"]


def test_a_landing_may_instead_record_that_the_chain_closes(setup) -> None:
    docs_dir, _, project = setup
    _plan_html(docs_dir, "plan-a", {"version": 0, "followups": [_followup("f1")]})
    evidence_dir = docs_dir / "evidence" / "archive"
    evidence_dir.mkdir(parents=True)
    _plan_html(
        evidence_dir,
        "plan-a-landed",
        {"version": 0, "evidence_for": ["plan-a"]},
        artifact_type="evidence",
    )

    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {"op": "set", "path": "status", "value": "shipped"},
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "reckon-ship",
                "outcome": "all sections landed — done, no followup",
            },
        ],
        0,
    )

    assert result["ok"] is True
    data, _ = _store_module.read_plan(project, "plan-a")
    assert data["status"] == "shipped"


def test_a_later_write_after_an_explicit_closure_still_passes(setup) -> None:
    """Closing the chain once must not block every subsequent edit."""
    docs_dir, _, project = setup
    _plan_html(
        docs_dir,
        "plan-a",
        {
            "version": 0,
            "status": "shipped",
            "followups": [
                _followup("f1", status="resolved", outcome="done — no followup")
            ],
        },
    )
    result = mcp_module._edit_plan(
        project, "plan-a", [{"op": "set", "path": "status", "value": "done"}], 0
    )
    assert result["ok"] is True


def test_an_open_followup_elsewhere_carries_the_chain(setup) -> None:
    docs_dir, _, project = setup
    _plan_html(
        docs_dir,
        "plan-a",
        {"version": 0, "followups": [_followup("f1"), _followup("f2")]},
    )
    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "reckon-ship",
                "outcome": "§2 landed — 12 tests green",
            }
        ],
        0,
    )
    assert result["ok"] is True


def test_an_ordinary_edit_is_not_a_landing(setup) -> None:
    """The rule fires on a landing, not on every write."""
    docs_dir, _, project = setup
    _plan_html(docs_dir, "plan-a", {"version": 0, "followups": []})
    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "summary", "value": "a new summary"}],
        0,
    )
    assert result["ok"] is True


def test_only_plans_owe_a_continuation() -> None:
    """Research and evidence carry no execution chain, so they owe no next step.

    Checked against the op applier directly: the schema separately forbids a
    non-plan artifact from holding followups or a terminal status at all, so the
    guard cannot be reached through a write and would otherwise go unverified.
    """
    landing = [{"op": "set", "path": "status", "value": "shipped"}]
    _store_module.apply_ops({"type": "evidence", "followups": []}, landing, False)

    with pytest.raises(_store_module.OpError):
        _store_module.apply_ops({"type": "plan", "followups": []}, landing, False)


def test_the_http_patch_path_enforces_the_same_rule() -> None:
    """The rule must hold on every write path, or it is a courtesy not a rule.

    Checked against the shared validator the server calls, because a landing
    marked through the HTTP patch surface would otherwise bypass the ops writer
    entirely.
    """
    landed = {"type": "plan", "status": "shipped", "followups": []}
    with pytest.raises(_store_module.OpError) as excinfo:
        _store_module.validate_landing_patch(landed, {"status": "shipped"})
    assert "/reckon-ship <slug> [§N]" in str(excinfo.value)

    carried = {"type": "plan", "status": "shipped", "followups": [_followup("f1")]}
    _store_module.validate_landing_patch(carried, {"status": "shipped"})

    closed = {
        "type": "plan",
        "status": "shipped",
        "followups": [_followup("f1", status="resolved", outcome="done — no followup")],
    }
    _store_module.validate_landing_patch(closed, {"status": "done"})


def test_a_plan_already_landed_without_a_continuation_stays_editable() -> None:
    """History is not retroactively locked — only a new landing owes an answer.

    Measured before choosing the rule's scope: 155 of 202 terminal plans across
    the mounted projects carry no continuation. A state-level invariant would
    have made all of them unwritable.
    """
    legacy = {"type": "plan", "status": "shipped", "followups": []}
    _store_module.validate_landing_patch(legacy, {"summary": "a corrected summary"})
    _store_module.validate_landing_patch(legacy, {"impl": 1.0})


def test_an_index_write_is_never_a_plan_landing() -> None:
    """Sprint state is not a plan, and its own status verb is not a landing."""
    working = {"sprints": [{"id": "S1", "status": "active", "items": []}]}
    _store_module.apply_ops(
        working, [{"op": "set", "path": "sprints.S1.status", "value": "done"}], True
    )
    assert working["sprints"][0]["status"] == "done"


def test_mid_run_resolve_agrees_across_ops_and_http_patch(setup) -> None:
    docs_dir, _, project = setup
    initial = {
        "version": 0,
        "status": "active",
        "impl": 0.5,
        "followups": [_followup("driver")],
    }
    _plan_html(docs_dir, "ops-plan", initial)
    _plan_html(docs_dir, "http-plan", initial)

    resolve = {
        "op": "resolve",
        "target": "followups",
        "id": "driver",
        "by": "reckon-ship",
        "outcome": "the dispatched section landed with 4 tests passing",
    }
    ops_result = mcp_module._edit_plan(project, "ops-plan", [resolve], 0)

    import reckon.serve as serve_module

    serve_module._DISC_CACHE.clear()
    server = serve_module.ThreadingHTTPServer(("127.0.0.1", 0), serve_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    resolved = _followup("driver", status="resolved")
    resolved["outcome"] = resolve["outcome"]
    try:
        connection.request(
            "POST",
            f"/plan/{project}/http-plan",
            body=json.dumps({"followups": [resolved]}),
            headers={"Content-Type": "application/json", "If-Match": "0"},
        )
        response = connection.getresponse()
        response.read()
        http_ok = response.status == 200
    finally:
        connection.close()
        server.shutdown()

    assert [ops_result["ok"], http_ok] == [True, True]
    for slug in ("ops-plan", "http-plan"):
        state, _ = _store_module.read_plan(project, slug)
        assert [item for item in state["followups"] if item["status"] == "open"] == []


def test_active_incomplete_plan_carries_its_own_continuation() -> None:
    state = {"type": "plan", "status": "active", "impl": 0.75, "followups": []}
    assert _store_module.continuation_present(state) is True


def test_full_implementation_does_not_carry_its_own_continuation(setup) -> None:
    docs_dir, _, project = setup
    _plan_html(
        docs_dir,
        "plan-a",
        {"version": 0, "status": "active", "impl": 1.0, "followups": [_followup("f1")]},
    )
    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "reckon-ship",
                "outcome": "the final section landed with 4 tests passing",
            }
        ],
        0,
    )
    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert result["detail"] == _store_module.CONTINUATION_REQUIRED


@pytest.mark.parametrize("status", ["shipped", "done"])
def test_new_terminal_status_without_continuation_is_refused(status: str) -> None:
    state = {"type": "plan", "status": "active", "impl": 0.5, "followups": []}
    with pytest.raises(_store_module.OpError, match="plan landing leaves"):
        _store_module.apply_ops(
            state,
            [{"op": "set", "path": "status", "value": status}],
            False,
        )


def test_historical_terminal_plan_without_followup_stays_writable(setup) -> None:
    docs_dir, _, project = setup
    _plan_html(
        docs_dir,
        "plan-a",
        {"version": 0, "status": "shipped", "impl": 1.0, "followups": []},
    )
    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "summary", "value": "corrected history"}],
        0,
    )
    assert result["ok"] is True


# ── Sprint altitude ─────────────────────────────────────────────────────────


def _plan_row(slug: str, *, depends_on=None, sprint=None, status="active") -> dict:
    return {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": status,
        "impl": 0.0,
        "depends_on": depends_on or [],
        "sprint": sprint,
        "effort": "M",
        "roi": "high",
        "blocking": [],
    }


def test_a_sprint_reports_the_sprints_it_feeds() -> None:
    """Derived from the graph, so it cannot go stale the way a list would."""
    inventory = [
        _plan_row("dispatch", sprint="S1"),
        _plan_row("ledger", depends_on=["dispatch"], sprint="S2"),
        _plan_row("visibility", depends_on=["ledger"], sprint="S3"),
    ]
    sprints = [
        {"id": "S1", "status": "active", "items": ["dispatch"]},
        {"id": "S2", "status": "planned", "items": ["ledger"]},
        {"id": "S3", "status": "planned", "items": ["visibility"]},
    ]

    rows = {row["id"]: row for row in build_roadmap("p", inventory, sprints)["sprints"]}

    assert rows["S1"]["feeds_sprints"] == ["S2"]
    assert rows["S1"]["unblocks"] == [
        {"plan": "ledger", "sprint": "S2", "via": "dispatch"}
    ]
    assert rows["S2"]["feeds_sprints"] == ["S3"]
    assert rows["S3"]["feeds_sprints"] == []


def test_a_dependency_inside_one_sprint_is_not_downstream_work() -> None:
    inventory = [
        _plan_row("dispatch", sprint="S1"),
        _plan_row("ledger", depends_on=["dispatch"], sprint="S1"),
    ]
    sprints = [{"id": "S1", "status": "active", "items": ["dispatch", "ledger"]}]
    rows = build_roadmap("p", inventory, sprints)["sprints"]
    assert rows[0]["feeds_sprints"] == []


def test_a_cross_project_dependency_is_not_claimed_as_a_local_feed() -> None:
    inventory = [
        dict(_plan_row("dispatch", sprint="S1"), project="p"),
        dict(
            _plan_row("elsewhere", depends_on=["other:thing"], sprint="S2"), project="p"
        ),
    ]
    sprints = [
        {"id": "S1", "status": "active", "items": ["dispatch"]},
        {"id": "S2", "status": "planned", "items": ["elsewhere"]},
    ]
    rows = build_roadmap("p", inventory, sprints)["sprints"]
    assert rows[0]["feeds_sprints"] == []


# ── The whole chain ─────────────────────────────────────────────────────────


def test_the_chain_stays_intact_from_worker_to_sprint(setup) -> None:
    """One trace across all three altitudes.

    A worker reports a follow-on it was fenced out of; that becomes a plan
    followup in the same writeback that resolves the driving one; and the sprint
    holding the plan reports the sprint it feeds. No altitude ends silently.
    """
    docs_dir, _, project = setup
    _plan_html(
        docs_dir,
        "plan-a",
        {"version": 0, "sprint": "S1", "followups": [_followup("f1")]},
    )

    manifest = (
        "node: node-a\n"
        "status: complete\n"
        "commits: 1a2b3c4\n"
        "changed_paths: reckon/_backends.py\n"
        "tests: uv run pytest tests/test_backends.py -q -> 28 passed\n"
        "follow_ons: the observe command needs a --wait flag\n"
        "blockers: none\n"
    )

    # Worker altitude: the candidate follow-on becomes an append op.
    ops = crew.followup_ops_from_manifest(manifest, slug="plan-a", section="§3")
    assert len(ops) == 1

    # Plan altitude: the same writeback resolves the driver and appends the next.
    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [
            {"op": "set", "path": "impl", "value": 0.5},
            {
                "op": "resolve",
                "target": "followups",
                "id": "f1",
                "by": "reckon-ship",
                "outcome": "§2 landed — commit 1a2b3c4; 28 tests green",
            },
            *ops,
        ],
        0,
    )
    assert result["ok"] is True

    data, _ = _store_module.read_plan(project, "plan-a")
    carried = [f for f in data["followups"] if f["status"] == "open"]
    assert len(carried) == 1
    assert carried[0]["prompt"] == "/reckon-ship plan-a §3"
    assert "fenced out of its write scope" in carried[0]["body"]

    # Sprint altitude: what closing this sprint would let us start.
    inventory = [
        _plan_row("plan-a", sprint="S1"),
        _plan_row("plan-b", depends_on=["plan-a"], sprint="S2"),
    ]
    sprints = [
        {"id": "S1", "status": "active", "items": ["plan-a"]},
        {"id": "S2", "status": "planned", "items": ["plan-b"]},
    ]
    rows = build_roadmap(project, inventory, sprints)["sprints"]
    assert rows[0]["feeds_sprints"] == ["S2"]
