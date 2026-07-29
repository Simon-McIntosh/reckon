"""Typed resource identity, mixed-layout reads, migration, and parity."""

from __future__ import annotations

import importlib
import http.client
import json
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon._store as store_module
import reckon.mcp as mcp_module
import reckon.resources as resources_module
from reckon import _plan_html
from reckon.cli import main
from reckon.resources import (
    ResourceCollision,
    build_migration_manifest,
    canonical_href,
    canonical_relative_path,
    iter_resources,
    migrate_typed_layout,
    resolve_resource,
    resolve_route,
)
from reckon.serve import discover_plans


def _artifact(
    path: Path,
    project: str,
    artifact_type: str,
    slug: str,
    *,
    body: str = "",
    status: str = "active",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    status_meta = (
        f'<meta name="plan-status" content="{status}">'
        if artifact_type == "plan"
        else ""
    )
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        f'<meta name="reckon-type" content="{artifact_type}">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        f"{status_meta}"
        f"<title>{slug}</title></head><body><main>{body}</main></body></html>"
    )
    return path


def _mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docs: Path, project: str
) -> None:
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state))
    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state
    importlib.reload(store_module)
    importlib.reload(mcp_module)


def test_typed_identity_distinguishes_equal_leaf_slugs(tmp_path):
    docs = tmp_path / "docs"
    _artifact(docs / "plans" / "shared.html", "sample", "plan", "shared")
    _artifact(docs / "research" / "shared.html", "sample", "research", "shared")
    _artifact(docs / "evidence" / "shared.html", "sample", "evidence", "shared")

    found = iter_resources(docs, "sample")
    assert {resource.identity.key for resource in found} == {
        "sample:plan:shared",
        "sample:research:shared",
        "sample:evidence:shared",
    }
    assert (
        resolve_resource(docs, "sample", "shared", "plan").path.parent.name == "plans"
    )
    assert (
        resolve_resource(docs, "sample", "shared", "research").path.parent.name
        == "research"
    )
    assert resolve_resource(docs, "sample", "shared").type == "plan"


@pytest.mark.parametrize(
    ("project", "slug"),
    [
        ("../outside", "safe"),
        ("/absolute", "safe"),
        ("sample", "../outside"),
        ("sample", "/absolute"),
        ("sample", "nested/path"),
        ("sample", "."),
        ("sample", ".."),
    ],
)
def test_resource_identity_rejects_unsafe_path_segments(project, slug):
    if slug != "safe":
        with pytest.raises(ValueError, match="single safe path segment"):
            canonical_relative_path("plan", slug)
    with pytest.raises(ValueError, match="single safe path segment"):
        canonical_href(project, "plan", slug)


def test_typed_discovery_rejects_arbitrary_nested_paths(tmp_path):
    docs = tmp_path / "docs"
    nested = _artifact(
        docs / "plans" / "nested" / "work.html",
        "sample",
        "plan",
        "work",
    )
    with pytest.raises(ResourceCollision, match="typed resource path"):
        iter_resources(docs, "sample")
    assert nested.is_file()


def test_migration_flattens_nested_typed_resources_and_rewrites_links(tmp_path):
    docs = tmp_path / "docs"
    first = _artifact(
        docs / "research" / "topic" / "first.html",
        "sample",
        "research",
        "first",
        body='<a href="second.html#result">second</a>',
    )
    second = _artifact(
        docs / "research" / "topic" / "second.html",
        "sample",
        "research",
        "second",
        body='<a href="first.html">first</a>',
    )

    with pytest.raises(ResourceCollision, match="typed resource path"):
        iter_resources(docs, "sample")

    manifest = build_migration_manifest(docs, "sample")
    assert [(item["from"], item["to"]) for item in manifest["moves"]] == [
        ("research/topic/first.html", "research/first.html"),
        ("research/topic/second.html", "research/second.html"),
    ]

    migrated = migrate_typed_layout(docs, "sample")

    assert migrated["moves"] == manifest["moves"]
    assert not first.exists()
    assert not second.exists()
    assert (docs / "research" / "first.html").read_text().count(
        'href="second.html#result"'
    ) == 1
    assert (docs / "research" / "second.html").read_text().count(
        'href="first.html"'
    ) == 1
    assert {resource.identity.key for resource in iter_resources(docs, "sample")} == {
        "sample:research:first",
        "sample:research:second",
    }


def test_migration_renames_nested_reserved_resource_identity(tmp_path):
    docs = tmp_path / "docs"
    nested = _artifact(
        docs / "research" / "disagreements" / "index.html",
        "sample",
        "research",
        "index",
    )

    manifest = build_migration_manifest(docs, "sample")
    assert manifest["moves"] == [
        {
            "archived": False,
            "from": "research/disagreements/index.html",
            "resource": "sample:research:disagreements-index",
            "sha256": manifest["moves"][0]["sha256"],
            "slug": "disagreements-index",
            "to": "research/disagreements-index.html",
            "type": "research",
        }
    ]

    migrate_typed_layout(docs, "sample")

    destination = docs / "research" / "disagreements-index.html"
    assert not nested.exists()
    assert _plan_html.parse_meta(destination).get("slug") == "disagreements-index"
    resource = resolve_resource(
        docs,
        "sample",
        "disagreements-index",
        "research",
    )
    assert resource.path == destination


def test_nested_migration_rejects_destination_collision(tmp_path):
    docs = tmp_path / "docs"
    first = _artifact(
        docs / "research" / "first" / "shared.html",
        "sample",
        "research",
        "shared",
    )
    second = _artifact(
        docs / "research" / "second" / "shared.html",
        "sample",
        "research",
        "shared",
    )

    with pytest.raises(ResourceCollision, match="destination collision"):
        build_migration_manifest(docs, "sample")

    assert first.is_file()
    assert second.is_file()
    assert not (docs / ".reckon").exists()


def test_migration_uses_unique_filenames_for_archived_slug_collisions(tmp_path):
    docs = tmp_path / "docs"
    first = _artifact(
        docs / "archive" / "delivery-analysis-landed.html",
        "sample",
        "plan",
        "delivery",
    )
    second = _artifact(
        docs / "archive" / "delivery-verification-landed.html",
        "sample",
        "plan",
        "delivery",
    )

    manifest = build_migration_manifest(docs, "sample")
    assert [(item["slug"], item["to"]) for item in manifest["moves"]] == [
        (
            "delivery-analysis-landed",
            "plans/archive/delivery-analysis-landed.html",
        ),
        (
            "delivery-verification-landed",
            "plans/archive/delivery-verification-landed.html",
        ),
    ]

    migrate_typed_layout(docs, "sample")

    assert not first.exists()
    assert not second.exists()
    migrated = iter_resources(docs, "sample")
    assert {item.slug for item in migrated} == {
        "delivery-analysis-landed",
        "delivery-verification-landed",
    }
    assert all(item.archived for item in migrated)
    assert {_plan_html.parse_meta(item.path).get("slug") for item in migrated} == {
        "delivery-analysis-landed",
        "delivery-verification-landed",
    }


def test_migration_infers_type_for_nested_untyped_document(tmp_path):
    docs = tmp_path / "docs"
    source = docs / "research" / "topic" / "study.html"
    source.parent.mkdir(parents=True)
    source.write_text(
        "<!doctype html><html><head><title>Study</title></head>"
        "<body><main><h1>Study</h1></main></body></html>"
    )

    manifest = build_migration_manifest(docs, "sample")
    assert [(item["type"], item["to"]) for item in manifest["moves"]] == [
        ("research", "research/study.html")
    ]

    migrate_typed_layout(docs, "sample")

    assert not source.exists()
    destination = docs / "research" / "study.html"
    assert destination.is_file()
    assert _plan_html.parse_meta(destination).get("type") == "research"


def test_migration_repairs_canonical_typed_document_without_type_meta(tmp_path):
    docs = tmp_path / "docs"
    source = docs / "research" / "study.html"
    source.parent.mkdir(parents=True)
    source.write_text(
        "<!doctype html><html><head><title>Study</title></head>"
        "<body><main><h1>Study</h1></main></body></html>"
    )

    with pytest.raises(ResourceCollision, match="location type"):
        iter_resources(docs, "sample")

    manifest = build_migration_manifest(docs, "sample")
    assert [(item["from"], item["to"]) for item in manifest["moves"]] == [
        ("research/study.html", "research/study.html")
    ]

    migrate_typed_layout(docs, "sample")

    assert source.is_file()
    assert _plan_html.parse_meta(source).get("type") == "research"
    assert iter_resources(docs, "sample")[0].identity.key == "sample:research:study"


@pytest.mark.parametrize("route", ["plans/..", "plans/nested/work", "plans//work"])
def test_typed_route_rejects_unsafe_or_nested_identity(tmp_path, route):
    docs = tmp_path / "docs"
    docs.mkdir()
    with pytest.raises(ResourceCollision, match="invalid typed resource route"):
        resolve_route(docs, "sample", route)


def test_migration_rejects_destination_symlink_escape(tmp_path):
    docs = tmp_path / "docs"
    outside = tmp_path / "outside"
    docs.mkdir()
    outside.mkdir()
    _artifact(docs / "work.html", "sample", "plan", "work")
    (docs / "plans").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ResourceCollision, match="escapes the docs directory"):
        migrate_typed_layout(docs, "sample")

    assert not (outside / "work.html").exists()
    assert (docs / "work.html").is_file()


def test_legacy_discovery_ignores_unactivated_project_state_resources(tmp_path):
    docs = tmp_path / "docs"
    _artifact(docs / "plans" / "shared.html", "sample", "plan", "shared")
    _artifact(docs / "research" / "shared.html", "sample", "research", "shared")
    sprint = docs / "sprints" / "iteration.html"
    sprint.parent.mkdir(parents=True)
    sprint.write_text(
        "<html><head>"
        '<meta name="sprint-id" content="iteration">'
        '<meta name="sprint-theme" content="Typed state">'
        "</head><body></body></html>"
    )

    discovered = discover_plans(docs, "sample", None)
    assert {(item["type"], item["slug"]) for item in discovered["inventory"]} == {
        ("plan", "shared"),
        ("research", "shared"),
    }
    assert {item["resource_id"] for item in discovered["inventory"]} == {
        "sample:plan:shared",
        "sample:research:shared",
    }
    assert discovered["sprints"] == []


def test_mixed_layout_routes_typed_and_flat_resources(tmp_path):
    docs = tmp_path / "docs"
    _artifact(docs / "flat.html", "sample", "plan", "flat")
    _artifact(docs / "research" / "study.html", "sample", "research", "study")

    canonical, alias = resolve_route(docs, "sample", "plans/flat")
    assert canonical is not None and canonical.path == docs / "flat.html"
    assert alias is False
    legacy, alias = resolve_route(docs, "sample", "flat.html")
    assert legacy == canonical
    assert alias is True
    typed, alias = resolve_route(docs, "sample", "research/study")
    assert typed is not None and typed.path == docs / "research" / "study.html"
    assert alias is False


def test_mcp_typed_read_selects_duplicate_leaf(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "plans" / "shared.html", "sample", "plan", "shared")
    _artifact(docs / "research" / "shared.html", "sample", "research", "shared")
    _mount(tmp_path, monkeypatch, docs, "sample")

    plan = mcp_module._read_plan("sample", "shared", doc_type="plan")
    research = mcp_module._read_plan("sample", "shared", doc_type="research")

    assert plan["data"]["type"] == "plan"
    assert research["data"]["type"] == "research"


def test_audit_reports_duplicate_stable_identity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "work.html", "sample", "plan", "work")
    _artifact(docs / "plans" / "work.html", "sample", "plan", "work")
    _mount(tmp_path, monkeypatch, docs, "sample")

    result = mcp_module._audit("sample")

    assert any(
        finding["code"] == "duplicate-resource-identity"
        for finding in result["findings"]
    )


def test_audit_reports_invalid_resource_path_without_hiding_valid_plans(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "plans" / "work.html", "sample", "plan", "work")
    _artifact(
        docs / "research" / "topic" / "study.html",
        "sample",
        "research",
        "study",
    )
    _mount(tmp_path, monkeypatch, docs, "sample")

    result = mcp_module._audit("sample")

    assert result["checked"] == 1
    assert any(
        finding["code"] == "invalid-resource-path"
        and finding["path"] == "research/topic/study.html"
        for finding in result["findings"]
    )
    assert discover_plans(docs, "sample", None)["inventory"][0]["slug"] == "work"


def test_audit_tolerates_duplicate_legacy_archives(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "plans" / "work.html", "sample", "plan", "work")
    _artifact(docs / "archive" / "work-outcome-a.html", "sample", "plan", "work")
    _artifact(docs / "archive" / "work-outcome-b.html", "sample", "plan", "work")
    _mount(tmp_path, monkeypatch, docs, "sample")

    result = mcp_module._audit("sample")

    assert result["checked"] == 1
    assert result["rollups_recomputed"] is True


def test_live_server_typed_routes_and_legacy_redirect(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    import reckon.serve as serve_module

    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "plans" / "shared.html", "sample", "plan", "shared")
    _artifact(docs / "research" / "shared.html", "sample", "research", "shared")
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(docs)}))
    monkeypatch.setattr(serve_module, "_MOUNTS_FILE", mounts)

    server = serve_module.ThreadingHTTPServer(("127.0.0.1", 0), serve_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", "/sample/plans/shared")
        response = connection.getresponse()
        assert response.status == 200
        assert b'reckon-type" content="plan"' in response.read()

        connection.request("GET", "/sample/shared.html")
        response = connection.getresponse()
        assert response.status == 308
        assert response.getheader("Location") == "/sample/plans/shared"
        response.read()

        connection.request("GET", "/plan/sample/research/shared")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["type"] == "research"

        archived = _artifact(
            docs / "evidence" / "archive" / "shared.html",
            "sample",
            "evidence",
            "shared",
        )
        assert archived.is_file()
        serve_module._DISC_CACHE.clear()
        connection.request("GET", "/sample/evidence/archive/shared")
        response = connection.getresponse()
        assert response.status == 200
        assert b'reckon-type" content="evidence"' in response.read()

        connection.request("GET", "/sample/evidence/archive/shared.html")
        response = connection.getresponse()
        assert response.status == 308
        assert response.getheader("Location") == "/sample/evidence/archive/shared"
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_migration_moves_by_semantic_type_and_rewrites_links(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    plan = _artifact(
        docs / "delivery.html",
        "sample",
        "plan",
        "delivery",
        body=(
            '<a href="study.html#result">study</a>'
            '<a href="/sample/check.html">check</a>'
            '<a href="/sample/assets/guide.pdf?download=1#page=2">guide</a>'
            '<img src="/sample/figures/diagram.svg?theme=dark#shape">'
            '<img src="figures/diagram.svg">'
        ),
    )
    figure = docs / "figures" / "diagram.svg"
    figure.parent.mkdir()
    figure.write_text("<svg></svg>")
    guide = docs / "assets" / "guide.pdf"
    guide.parent.mkdir()
    guide.write_bytes(b"%PDF")
    research = _artifact(docs / "study.html", "sample", "research", "study")
    evidence = _artifact(docs / "check.html", "sample", "evidence", "check")
    archived = _artifact(
        docs / "archive" / "delivery-landed.html",
        "sample",
        "evidence",
        "delivery-landed",
    )
    before = {
        "plan": _plan_html.read_state(plan.read_text()),
        "research": _plan_html.read_state(research.read_text()),
        "evidence": _plan_html.read_state(evidence.read_text()),
        "archived": _plan_html.read_state(archived.read_text()),
    }

    manifest = migrate_typed_layout(docs, "sample")

    assert [item["to"] for item in manifest["moves"]] == [
        "evidence/archive/delivery-landed.html",
        "evidence/check.html",
        "plans/delivery.html",
        "research/study.html",
    ]
    moved_plan = docs / "plans" / "delivery.html"
    text = moved_plan.read_text()
    assert 'href="../research/study.html#result"' in text
    assert 'href="/sample/evidence/check"' in text
    assert 'href="/sample/assets/guide.pdf?download=1#page=2"' in text
    assert 'src="/sample/figures/diagram.svg?theme=dark#shape"' in text
    assert 'src="../figures/diagram.svg"' in text
    assert _plan_html.read_state(text) == before["plan"]
    assert (
        _plan_html.read_state((docs / "research" / "study.html").read_text())
        == before["research"]
    )
    assert (
        _plan_html.read_state((docs / "evidence" / "check.html").read_text())
        == before["evidence"]
    )
    assert (
        _plan_html.read_state(
            (docs / "evidence" / "archive" / "delivery-landed.html").read_text()
        )
        == before["archived"]
    )
    assert not (docs / "delivery.html").exists()
    assert (
        json.loads((docs / ".reckon" / "typed-resource-manifest.json").read_text())
        == manifest
    )


def test_migration_is_idempotent_and_manifest_is_stable(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "work.html", "sample", "plan", "work")

    first = migrate_typed_layout(docs, "sample")
    manifest_bytes = (docs / ".reckon" / "typed-resource-manifest.json").read_bytes()
    second = migrate_typed_layout(docs, "sample")

    assert second == first
    assert (
        docs / ".reckon" / "typed-resource-manifest.json"
    ).read_bytes() == manifest_bytes


def test_incremental_migration_preserves_manifest_provenance(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "first.html", "sample", "plan", "first")
    first = migrate_typed_layout(docs, "sample")

    _artifact(
        docs / "second.html",
        "sample",
        "research",
        "second",
        body='<a href="/sample/first.html">first</a>',
    )
    second = migrate_typed_layout(docs, "sample")

    assert [move["from"] for move in first["moves"]] == ["first.html"]
    assert [move["from"] for move in second["moves"]] == [
        "first.html",
        "second.html",
    ]
    assert second["moves"][0] == first["moves"][0]
    assert second["rewrites"]
    stable = (docs / ".reckon" / "typed-resource-manifest.json").read_bytes()
    assert migrate_typed_layout(docs, "sample") == second
    assert (docs / ".reckon" / "typed-resource-manifest.json").read_bytes() == stable


@pytest.mark.parametrize(
    "manifest",
    [
        {"format": 99, "project": "sample", "moves": []},
        {"format": 1, "project": "other", "moves": []},
        {"format": 1, "project": "sample", "moves": "bad"},
    ],
)
def test_incremental_migration_rejects_invalid_prior_manifest(tmp_path, manifest):
    docs = tmp_path / "docs"
    (docs / ".reckon").mkdir(parents=True)
    (docs / ".reckon" / "typed-resource-manifest.json").write_text(json.dumps(manifest))
    _artifact(docs / "work.html", "sample", "plan", "work")
    before = (docs / "work.html").read_bytes()

    with pytest.raises(ResourceCollision, match="manifest"):
        migrate_typed_layout(docs, "sample")

    assert (docs / "work.html").read_bytes() == before


def test_incremental_migration_rejects_contradictory_prior_move(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "work.html", "sample", "plan", "work")
    migrate_typed_layout(docs, "sample")
    _artifact(docs / "work.html", "sample", "research", "work")

    with pytest.raises(ResourceCollision, match="contradicts prior manifest"):
        migrate_typed_layout(docs, "sample")


def test_migration_collision_preflight_does_not_mutate(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = _artifact(docs / "work.html", "sample", "plan", "work")
    destination = _artifact(docs / "plans" / "work.html", "sample", "plan", "work")
    source_before = source.read_bytes()
    destination_before = destination.read_bytes()

    with pytest.raises(ResourceCollision, match="destination already exists"):
        build_migration_manifest(docs, "sample")

    assert source.read_bytes() == source_before
    assert destination.read_bytes() == destination_before
    assert not (docs / ".reckon").exists()


def test_migration_install_failure_rolls_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    docs = tmp_path / "docs"
    docs.mkdir()
    first = _artifact(docs / "first.html", "sample", "plan", "first")
    second = _artifact(docs / "second.html", "sample", "research", "second")
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    prior_manifest = docs / ".reckon" / "typed-resource-manifest.json"
    prior_manifest.parent.mkdir()
    prior_manifest.write_text(
        '{"format":1,"moves":[],"project":"sample","retained":true,"rewrites":[]}\n'
    )
    real_replace = resources_module.os.replace
    calls = 0

    def fail_during_install(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected install failure")
        return real_replace(source, destination)

    monkeypatch.setattr(resources_module.os, "replace", fail_during_install)
    with pytest.raises(OSError, match="injected"):
        migrate_typed_layout(docs, "sample")

    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not (docs / "plans" / "first.html").exists()
    assert not (docs / "research" / "second.html").exists()
    assert (
        prior_manifest.read_text()
        == '{"format":1,"moves":[],"project":"sample","retained":true,"rewrites":[]}\n'
    )


def test_static_build_inventory_matches_live_discovery(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _artifact(docs / "plans" / "work.html", "sample", "plan", "work")
    _artifact(docs / "research" / "study.html", "sample", "research", "study")
    live = discover_plans(docs, "sample", docs / "state")["inventory"]
    mcp_inventory = mcp_module._read_plan(
        "sample",
        checkout_path=str(tmp_path),
        include_followups=False,
        include_questions=False,
    )["plans"]

    result = CliRunner().invoke(main, ["build", str(docs), "--project", "sample"])
    assert result.exit_code == 0, result.output
    static = json.loads((docs / "state" / "sample" / "index.json").read_text())["data"][
        "inventory"
    ]

    project_fields = ("resource_id", "type", "slug", "href", "canonical_href")
    assert [tuple(item[field] for field in project_fields) for item in static] == [
        tuple(item[field] for field in project_fields) for item in live
    ]
    assert [
        tuple(item[field] for field in project_fields) for item in mcp_inventory
    ] == [tuple(item[field] for field in project_fields) for item in live]


def test_spa_graph_uses_typed_navigation_identity():
    root = Path(__file__).resolve().parents[1]
    graph = (root / "docs/ui/graph.jsx").read_text()
    loader = (root / "docs/ui/state-loader.js").read_text()

    assert "function _artifactKey(artifact)" in graph
    assert "Object.fromEntries(M.inventory.map(p => [_artifactKey(p), p]))" in graph
    assert "pos[_artifactKey(p)]" in graph
    assert "key={_artifactKey(p)}" in graph
    assert 'onNav({ view: "plan", slug: navKey })' in graph
    assert 'isArchivedArtifact(inv) ? "archive:" : ""' in loader
    assert (
        "Object.fromEntries(mergedInventory.map(inv => [inv.nav_key, inv]))" in loader
    )
