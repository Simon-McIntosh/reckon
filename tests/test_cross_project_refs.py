"""Cross-project plan references — grammar, scope split, resolution, audit.

One grammar serves every link-list field: ``[project:]slug[#stage]``. A bare
slug is local to the owning project; a ``project:`` qualifier makes the ref
external. The MCP layer resolves both scopes (local via the caller's checkout,
external via mounts.json) and the audit reports external refs that do not
resolve. These tests pin the grammar, the write-boundary rejection of
malformed refs, and both resolution paths.
"""

import importlib
import json

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
import reckon.serve as serve_module
from reckon._schema import PlanState, parse_plan_ref, split_refs


# ── Grammar ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ref", "project", "slug", "stage"),
    [
        ("alpha", None, "alpha", None),
        ("alpha#s2", None, "alpha", "s2"),
        ("nova:spine", "nova", "spine", None),
        ("nova:spine#s11", "nova", "spine", "s11"),
        ("L2G:some.plan-v", "L2G", "some.plan-v", None),
        ("STRATEGY", None, "STRATEGY", None),
    ],
)
def test_parse_plan_ref_accepts_the_grammar(ref, project, slug, stage):
    parsed = parse_plan_ref(ref)
    assert parsed is not None
    assert (parsed.project, parsed.slug, parsed.stage) == (project, slug, stage)


@pytest.mark.parametrize(
    "ref",
    ["", ":", "a:", ":b", "a:b:c", "a b", "-leading", "#stage", "a#", 42, None],
)
def test_parse_plan_ref_rejects_malformed(ref):
    assert parse_plan_ref(ref) is None


def test_split_refs_partitions_by_owning_project():
    refs = ["local-one", "nova:remote", "self:qualified-local", "bad ref"]
    local, external = split_refs(refs, "self")
    assert local == ["local-one", "self:qualified-local"]
    assert external == ["nova:remote"]


def test_planstate_scope_views():
    state = PlanState(
        project="norma",
        slug="pkg",
        title="t",
        status="active",
        depends_on=["nova:nova-spine-refactor", "local-thing"],
    )
    assert state.local_depends_on() == ["local-thing"]
    assert state.external_depends_on() == ["nova:nova-spine-refactor"]


def test_write_boundary_rejects_malformed_link_ref():
    state = PlanState(
        project="p",
        slug="s",
        title="t",
        status="active",
        depends_on=["good", "very bad ref"],
    )
    with pytest.raises(ValueError, match="malformed plan ref"):
        state.validate_for_write()


def test_write_boundary_accepts_external_refs():
    PlanState(
        project="p",
        slug="s",
        title="t",
        status="active",
        depends_on=["nova:spine#s11"],
        blocks=["other:thing"],
    ).validate_for_write()


# ── Resolution (two mounted projects) ────────────────────────────────────────


@pytest.fixture()
def two_projects(tmp_path, monkeypatch):
    dirs = {}
    mounts = {}
    for project in ("alpha", "beta"):
        docs = tmp_path / project / "docs"
        docs.mkdir(parents=True)
        dirs[project] = docs
        mounts[project] = str(docs)
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps(mounts))
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    importlib.reload(_store_module)
    importlib.reload(mcp_module)
    yield dirs
    importlib.reload(_store_module)
    importlib.reload(mcp_module)


def _plan_html(project: str, slug: str, status: str, depends_on: str = "") -> str:
    dep_meta = (
        f'<meta name="plan-depends-on" content="{depends_on}">' if depends_on else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{project}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="{status}">
{dep_meta}
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def test_read_plan_resolves_local_and_external_deps(two_projects):
    dirs = two_projects
    (dirs["alpha"] / "upstream.html").write_text(
        _plan_html("alpha", "upstream", "shipped")
    )
    (dirs["beta"] / "provider.html").write_text(
        _plan_html("beta", "provider", "active")
    )
    (dirs["alpha"] / "consumer.html").write_text(
        _plan_html("alpha", "consumer", "active", "upstream,beta:provider,beta:gone")
    )

    result = mcp_module._read_plan("alpha", "consumer")
    deps = {row["ref"]: row for row in result["deps"]}

    assert deps["upstream"]["scope"] == "local"
    assert deps["upstream"]["found"] is True
    assert deps["upstream"]["status"] == "shipped"

    assert deps["beta:provider"]["scope"] == "external"
    assert deps["beta:provider"]["project"] == "beta"
    assert deps["beta:provider"]["found"] is True
    assert deps["beta:provider"]["status"] == "active"

    assert deps["beta:gone"]["found"] is False


def test_audit_flags_unresolved_external_refs(two_projects):
    dirs = two_projects
    (dirs["alpha"] / "consumer.html").write_text(
        _plan_html("alpha", "consumer", "active", "beta:gone,ghost:anything")
    )

    result = mcp_module._audit("alpha")
    codes = {
        (f["code"], f["slug"])
        for f in result["findings"]
        if f["category"] == "references"
    }
    assert ("dangling-external-ref", "consumer") in codes
    assert ("unmounted-external-project", "consumer") in codes


def test_audit_quiet_when_external_ref_resolves(two_projects):
    dirs = two_projects
    (dirs["beta"] / "provider.html").write_text(
        _plan_html("beta", "provider", "shipped")
    )
    (dirs["alpha"] / "consumer.html").write_text(
        _plan_html("alpha", "consumer", "active", "beta:provider")
    )

    result = mcp_module._audit("alpha")
    external = [
        f
        for f in result["findings"]
        if f["code"] in ("dangling-external-ref", "unmounted-external-project")
    ]
    assert external == []


def test_roadmap_resolves_external_dependency_with_lazy_mount_initialization(
    two_projects,
):
    dirs = two_projects
    (dirs["beta"] / "provider.html").write_text(
        _plan_html("beta", "provider", "shipped")
    )
    (dirs["alpha"] / "consumer.html").write_text(
        _plan_html("alpha", "consumer", "active", "beta:provider")
    )
    serve_module._MOUNTS_FILE = None
    serve_module._STATE_ROOT = None
    serve_module._DISC_CACHE.clear()

    result = mcp_module._roadmap("alpha")

    assert [item["slug"] for item in result["ready_now"]] == ["consumer"]
    assert not any(
        item["code"] == "unresolved-external-dependency"
        for item in result["wiring_findings"]
    )
