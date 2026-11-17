"""Plan write replies share the keyed roadmap and never reuse obsolete verdicts."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from reckon import (
    _plan_html,
    _store,
    ledger,
    mcp,
    mcp_deadlines,
    roadmap,
    roadmap_snapshot,
    run_store,
    serve,
)

PROJECT = "sample"
CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {"reasoning": "standard", "verification": "strict", "risk": "low"},
}


def _plan(docs: Path, slug: str, *, depends_on: list[str] | None = None) -> Path:
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": slug,
        "title": slug,
        "status": "active",
        "version": 0,
        "standalone": "isolated fixture",
        "depends_on": depends_on or [],
        "section_declarations": {"s1": "implementable", "s2": "implementable"},
        "sections": [
            {
                "id": name,
                "status": "implementable",
                "effort_hours": 1.0,
                "capability": CAPABILITY,
                "attempts": 0,
                "links": [],
            }
            for name in ("s1", "s2")
        ],
    }
    text = (
        f'<!doctype html><html><head><meta name="docs-project" content="{PROJECT}">'
        f'<meta name="plan-depends-on" content="{",".join(depends_on or [])}">'
        '</head><body><main class="plan-doc">'
    )
    text += "".join(
        f'<h2 id="{name}">{name}</h2><p>Deliver {name} work.</p>'
        for name in ("s1", "s2")
    )
    path = docs / "plans" / f"{slug}.html"
    path.write_text(_plan_html.write_state(text + "</main></body></html>", state))
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    docs = tmp_path / "repo" / "docs"
    (docs / "plans").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RECKON_ROADMAP_REVALIDATE_S", "3600")
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    serve._resolve_paths(mounts)
    roadmap_snapshot._ROADMAP_SNAPSHOTS.clear()
    _plan(docs, "upstream")
    _plan(docs, "target", depends_on=["upstream"])
    _plan(docs, "independent")
    return docs


@pytest.fixture
def builds(monkeypatch):
    calls = []
    original = roadmap._build_roadmap

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(roadmap, "_build_roadmap", counted)
    return calls


def _edit(slug: str, ops: list[dict], *, checkout_path=None) -> dict:
    _, version = _store.read_plan(PROJECT, slug, checkout_path)
    answer = mcp._edit_plan_tool(
        PROJECT, slug, expected_version=version, ops=ops, checkout_path=checkout_path
    )
    assert answer["ok"], answer
    return answer


def _set(path: str, value: object) -> dict:
    return {"op": "set", "path": path, "value": value}


def _fields(report: dict, slug="target") -> dict:
    row = next(row for row in report["pending_work"] if row["slug"] == slug)
    return {key: row[key] for key in ("readiness", "closure_blockers")}


def _assert_current(answer: dict, slug="target", *, checkout_path=None):
    # This read bypasses the snapshot and every discovery memo. A stale cache
    # must disagree with an independently composed answer from the written tree.
    serve._DISC_CACHE.clear()
    expected = _fields(mcp._roadmap_scoped(PROJECT, checkout_path=checkout_path), slug)
    assert {key: answer[key] for key in expected} == expected
    return expected


def test_metadata_and_text_writes_build_no_roadmap(project, builds):
    answer = _edit("target", [_set("summary", "changed metadata")])
    assert "readiness" not in answer
    assert builds == []
    _, version = _store.read_plan(PROJECT, "target")
    answer = mcp._edit_plan_tool(
        PROJECT,
        "target",
        expected_version=version,
        mode="text",
        old_html="Deliver s1 work.",
        new_html="Ship s1 work.",
    )
    assert answer["ok"], answer
    assert "readiness" not in answer
    assert builds == []


def test_unchanged_declaration_builds_no_roadmap(project, builds):
    answer = _edit("target", [_set("section_declarations.s1", "implementable")])
    assert builds == [], f"unchanged declaration built {len(builds)} roadmaps"
    assert "readiness" not in answer


def test_metadata_beside_unchanged_declaration_builds_no_roadmap(project, builds):
    _edit(
        "target",
        [
            _set("summary", "new summary"),
            _set("section_declarations.s1", "implementable"),
        ],
    )
    assert builds == []


@pytest.mark.parametrize("explicit_checkout", [False, True])
def test_changed_declaration_builds_once_and_returns_current_fields(
    project, builds, explicit_checkout
):
    checkout = str(project.parent) if explicit_checkout else None
    answer = _edit(
        "target",
        [_set("section_declarations", {"s1": "done", "s2": "done"})],
        checkout_path=checkout,
    )
    assert len(builds) <= 1, f"declaration built {len(builds)} roadmaps"
    assert len(builds) == 1, "the counter never saw a known cold composition"
    assert _assert_current(answer, checkout_path=checkout)["readiness"] == "closable"


def test_upstream_write_invalidates_readiness(project, builds):
    before = mcp_deadlines._plan_readiness_fields(PROJECT, "target")
    assert before["readiness"] == "blocked"
    _edit("upstream", [_set("section_declarations", {"s1": "done", "s2": "done"})])
    current = mcp_deadlines._plan_readiness_fields(PROJECT, "target")
    assert _assert_current(current)["readiness"] == "ready"
    answer = _edit("target", [_set("section_declarations.s1", "done")])
    assert _assert_current(answer)["readiness"] == "ready"
    assert answer["readiness"] != before["readiness"]


def test_snapshot_hit_does_not_compose_ahead_of_lookup(project, builds):
    first = mcp_deadlines._plan_readiness_fields(PROJECT, "target")
    assert first["readiness"] == "blocked"
    builds.clear()
    assert mcp_deadlines._plan_readiness_fields(PROJECT, "target") == first
    assert builds == []


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def test_promoted_unlanded_run_invalidates_snapshot(project, builds):
    root = project.parent
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")
    _git(
        root,
        "add",
        "docs/plans/upstream.html",
        "docs/plans/target.html",
        "docs/plans/independent.html",
    )
    _git(
        root,
        "commit",
        "-q",
        "-m",
        "chore: seed fixture",
        "-m",
        "Establish the temporary repository.",
    )
    parent = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    unlanded = subprocess.check_output(
        ["git", "-C", str(root), "commit-tree", tree, "-p", parent],
        input="feat: detached fixture\n\nRepresent unlanded work.\n",
        text=True,
    ).strip()
    answer = _edit(
        "target", [_set("section_declarations", {"s1": "done", "s2": "done"})]
    )
    _assert_current(answer)
    row = ledger.build_record(
        run_id="run-unlanded",
        plan="target",
        section="s1",
        node="work",
        role="implement",
        gate="passed",
        commits=[unlanded],
        completed_at="2026-11-10T10:00:00Z",
    )
    with run_store.RunStore() as store:
        store.append(PROJECT, row)
    builds.clear()
    current = mcp_deadlines._plan_readiness_fields(PROJECT, "target")
    assert builds, "a promoted store write must invalidate the readiness snapshot"
    _assert_current(current)
    report = mcp._roadmap_scoped(PROJECT)
    assert any(
        item.get("code") == "run-not-landed" for item in report["wiring_findings"]
    ), report


def test_fixture_work_outside_landing_audit_is_under_budget(project, monkeypatch):
    import cProfile
    import pstats

    profile = cProfile.Profile()
    start = time.monotonic()
    profile.enable()
    answer = _edit("target", [_set("section_declarations.s1", "done")])
    profile.disable()
    wall = time.monotonic() - start
    audit = sum(
        values[3]
        for key, values in pstats.Stats(profile).stats.items()
        if key[2] == "_done_sections_unlanded_findings"
    )
    assert wall - audit <= 5, f"outside landing audit: {wall - audit:.3f}s"
    print(
        f"READINESS_FIXTURE wall={wall:.6f}s landing_audit={audit:.6f}s outside={wall - audit:.6f}s"
    )
    _assert_current(answer)
