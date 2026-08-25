from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.cli as cli
import reckon.pages as pages
import reckon.serve as serve
from reckon import __version__


REPO_ROOT = Path(__file__).parents[1]


def test_cli_version_matches_installed_distribution():
    expected = version("reckon-plans")

    result = CliRunner().invoke(cli.main, ["--version"])

    assert expected != "dev"
    assert __version__ == expected
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"reckon, version {expected}"


def _write_plan(
    docs_dir: Path,
    *,
    project: str,
    slug: str,
    sprint: str,
    north_star: str | None = None,
) -> None:
    direction_meta = (
        f'<meta name="plan-north-star" content="{north_star}">' if north_star else ""
    )
    (docs_dir / f"{slug}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug.title()}">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-sprint" content="{sprint}">'
        f"{direction_meta}"
        '<meta name="plan-impl" content="0.5">'
        f"<title>{slug.title()}</title>"
        "</head><body><main></main></body></html>"
    )


def _write_discovery_pages(docs_dir: Path) -> None:
    sprint_dir = docs_dir / "sprints"
    sprint_dir.mkdir()
    (sprint_dir / "delivery.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="sprint-id" content="S8">'
        '<meta name="sprint-theme" content="Static delivery">'
        '<meta name="sprint-status" content="active">'
        '<meta name="sprint-starts" content="2026-07-01">'
        '<meta name="sprint-ends" content="2026-07-31">'
        "</head><body></body></html>"
    )
    milestone_dir = docs_dir / "milestones"
    milestone_dir.mkdir()
    (milestone_dir / "portable.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="milestone-id" content="M8">'
        '<meta name="milestone-name" content="Portable build">'
        '<meta name="milestone-status" content="active">'
        '<meta name="milestone-pct" content="60">'
        "</head><body></body></html>"
    )


def _seed_project_index(docs_dir: Path, project: str) -> Path:
    state_dir = docs_dir / "state" / project
    state_dir.mkdir(parents=True)
    index_path = state_dir / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "updated": "2026-07-01T00:00:00",
                "project": project,
                "doc": "index",
                "data": {
                    "_version": 4,
                    "active_sprint_id": "S7",
                    "sprints": [
                        {
                            "id": "S7",
                            "theme": "Authored sprint",
                            "status": "active",
                            "items": [{"slug": "kept-item"}],
                        }
                    ],
                    "milestones": [
                        {
                            "id": "M7",
                            "name": "Authored milestone",
                            "status": "active",
                            "evidence": ["kept-evidence"],
                        }
                    ],
                    "timeline": [{"date": "2026-07-01", "label": "Keep this"}],
                    "blockers": [{"id": "network", "owner": "ops"}],
                },
            },
            indent=2,
        )
        + "\n"
    )
    return index_path


def _invoke_build(docs_dir: Path, project: str = "fixture"):
    return CliRunner().invoke(cli.main, ["build", str(docs_dir), "--project", project])


def _loaded_jsx(html: str) -> set[str]:
    return {
        reference.lstrip("/")
        for reference in re.findall(r'<script[^>]+src="([^"]+\.jsx)"', html)
    }


def _loaded_stylesheets(html: str) -> tuple[str, ...]:
    return tuple(
        reference.lstrip("/")
        for reference in re.findall(r'<link[^>]+href="([^"]+\.css)"', html)
        if not reference.startswith(("https://", "http://"))
    )


@pytest.fixture()
def built_source_site(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, project="fixture", slug="alpha", sprint="S8")
    _write_discovery_pages(docs_dir)
    index_path = _seed_project_index(docs_dir, "fixture")

    result = _invoke_build(docs_dir)
    assert result.exit_code == 0, result.output
    return docs_dir, index_path, result


def test_build_copies_every_canonical_asset(built_source_site):
    docs_dir, _, _ = built_source_site
    expected_ui = {path.name for path in (REPO_ROOT / "docs" / "ui").iterdir()}
    expected_shared = {path.name for path in (REPO_ROOT / "docs" / "_shared").iterdir()}

    assert {path.name for path in (docs_dir / "_ui").iterdir()} == expected_ui
    assert {path.name for path in (docs_dir / "_shared").iterdir()} == expected_shared
    assert (docs_dir / "_shared" / "state.js").read_bytes() == (
        REPO_ROOT / "docs" / "_shared" / "state.js"
    ).read_bytes()


def test_build_writes_relative_index_and_nojekyll(built_source_site):
    docs_dir, _, _ = built_source_site
    index_html = (docs_dir / "index.html").read_text()
    local_references = [
        value
        for value in re.findall(r'(?:href|src)="([^"]+)"', index_html)
        if not value.startswith(("https://", "http://"))
    ]

    assert local_references
    assert all(not value.startswith("/") for value in local_references)
    assert "_ui/glyphs.jsx" in local_references
    assert "_ui/_shared.jsx" in local_references
    assert "_ui/prompts.js" in local_references
    assert "_ui/crew.jsx" in local_references
    assert (docs_dir / ".nojekyll").is_file()


def test_spa_entry_points_load_the_same_jsx_files(built_source_site):
    docs_dir, _, _ = built_source_site
    built = _loaded_jsx((docs_dir / "index.html").read_text())
    checked_in = _loaded_jsx((REPO_ROOT / "docs" / "index.html").read_text())
    live = _loaded_jsx(serve._render_spa_html("fixture"))

    assert "_ui/crew.jsx" in built
    assert live == checked_in
    assert built == checked_in


def test_spa_entry_points_load_the_same_surface_stylesheets(
    built_source_site, tmp_path
):
    docs_dir, _, _ = built_source_site
    synced_docs = tmp_path / "synced" / "docs"
    synced_docs.mkdir(parents=True)
    sync_result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(synced_docs),
            "--project",
            "synced",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
        ],
    )
    assert sync_result.exit_code == 0, sync_result.output

    expected_surface_files = (
        "topbar.css",
        "plans.css",
        "reader.css",
        "overview.css",
        "sprints.css",
        "crew.css",
        "graph.css",
    )
    expected_surface_assets = {f"_ui/{name}" for name in expected_surface_files}
    entry_points = {
        "checked-in": (REPO_ROOT / "docs" / "index.html").read_text(),
        "served": serve._render_spa_html("fixture"),
        "synced": (synced_docs / "index.html").read_text(),
        "built": (docs_dir / "index.html").read_text(),
    }
    loaded = {name: _loaded_stylesheets(html) for name, html in entry_points.items()}

    assert all(assets == loaded["checked-in"] for assets in loaded.values())
    for name in expected_surface_files:
        asset = f"_ui/{name}"
        assert all(asset in references for references in loaded.values())
        assert (docs_dir / "_ui" / name).is_file()
        assert (REPO_ROOT / "docs" / "ui" / name).is_file()


def test_plan_and_sprint_views_surface_only_matching_live_work():
    plan = (REPO_ROOT / "docs" / "ui" / "plan.jsx").read_text()
    sprint = (REPO_ROOT / "docs" / "ui" / "sprint.jsx").read_text()

    band_anchor = plan.find('className="r-inflight-band"')
    assert band_anchor != -1
    assert "run.member" in plan[band_anchor:]
    assert "run.section" in plan[band_anchor:]
    assert 'aria-label="Work in flight"' in plan

    live_summary_anchor = sprint.find("liveSummary =")
    assert live_summary_anchor != -1
    assert "run.member" in sprint[live_summary_anchor:]
    assert "run.section" in sprint[live_summary_anchor:]

    badge_anchor = sprint.find('className="r-inflight-badge"')
    assert badge_anchor != -1
    assert live_summary_anchor < badge_anchor


def test_build_bakes_discovery_and_preserves_authored_project_state(
    built_source_site,
):
    _, index_path, _ = built_source_site
    data = json.loads(index_path.read_text())["data"]

    assert "alpha" in {item["slug"] for item in data["inventory"]}
    assert {item["id"] for item in data["sprints"]} == {"S7", "S8"}
    # Markerless typed project-state destinations are migration staging, not
    # canonical state. The authored legacy index remains the only source.
    assert {item["id"] for item in data["milestones"]} == {"M7"}
    assert data["sprints"][0]["items"] == [{"slug": "kept-item"}]
    assert data["milestones"][0]["evidence"] == ["kept-evidence"]
    assert data["timeline"] == [{"date": "2026-07-01", "label": "Keep this"}]
    assert data["blockers"] == [{"id": "network", "owner": "ops"}]
    assert data["active_sprint_id"] == "S7"
    assert data["_version"] == 5


def test_build_carries_directions_into_the_project_surfaces(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    directions = [
        {
            "id": "reliable-delivery",
            "name": "Reliable delivery",
            "statement": "Every release remains reproducible and observable.",
        },
        {
            "id": "clear-work",
            "name": "Clear work",
            "statement": "Every active plan states what it advances.",
        },
    ]
    _write_plan(
        docs_dir,
        project="fixture",
        slug="alpha",
        sprint="S8",
        north_star="reliable-delivery",
    )
    _write_discovery_pages(docs_dir)
    index_path = _seed_project_index(docs_dir, "fixture")
    envelope = json.loads(index_path.read_text())
    envelope["data"]["north_stars"] = directions
    index_path.write_text(json.dumps(envelope, indent=2) + "\n")

    result = _invoke_build(docs_dir)

    assert result.exit_code == 0, result.output
    data = json.loads(index_path.read_text())["data"]
    alpha = next(item for item in data["inventory"] if item["slug"] == "alpha")
    shell = (docs_dir / "_ui" / "shell.jsx").read_text()
    loader = (docs_dir / "_ui" / "state-loader.js").read_text()
    assert data["north_stars"] == directions
    assert alpha["north_star"] == "reliable-delivery"
    assert 'aria-label="North stars"' in shell
    assert "northStars.map(direction =>" in shell
    assert "{direction.statement}" in shell
    assert "{liveCount} live" in shell
    assert "r-north-star-badge" in shell
    assert 'toggle("north_star", direction.id)' in shell
    assert "filters.north_star.includes(p.north_star)" in shell
    assert "north_stars:       northStars" in loader


def test_build_without_directions_preserves_the_unlabelled_shape(built_source_site):
    _, index_path, _ = built_source_site
    data = json.loads(index_path.read_text())["data"]
    alpha = next(item for item in data["inventory"] if item["slug"] == "alpha")
    shell = (REPO_ROOT / "docs" / "ui" / "shell.jsx").read_text()

    assert "north_stars" not in data
    assert "north_star" not in alpha
    assert "{northStars.length > 0 && (" in shell
    assert "{p.north_star && <>" in shell
    assert "(M.north_stars || []).length && filters.north_star?.length" in shell


@pytest.mark.parametrize("destination", ["_ui", "_shared"])
def test_build_rejects_non_directory_asset_destination(tmp_path, destination):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / destination).write_text("not a directory")

    result = _invoke_build(docs_dir)

    assert result.exit_code == 1
    assert f"{destination} exists but is not a directory" in result.output


def test_build_fails_loudly_when_packaged_and_source_assets_are_missing(
    tmp_path, monkeypatch
):
    fake_package = tmp_path / "installed" / "reckon"
    fake_package.mkdir(parents=True)
    monkeypatch.setattr(cli, "__file__", str(fake_package / "cli.py"))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    result = _invoke_build(docs_dir)

    assert result.exit_code == 1
    assert "frontend assets are missing or incomplete" in result.output


def test_sync_generates_pinned_uv_workflow(tmp_path, monkeypatch):
    repo_dir = tmp_path / "consumer"
    docs_dir = repo_dir / "docs"
    docs_dir.mkdir(parents=True)
    monkeypatch.chdir(repo_dir)
    monkeypatch.setattr(
        pages,
        "detect_publication_strategy",
        lambda _docs_dir: pages.select_publication_strategy(None),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            "docs",
            "--project",
            "consumer",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
            "--generate-ci",
        ],
    )

    assert result.exit_code == 0, result.output
    workflow = (repo_dir / ".github" / "workflows" / "reckon-pages.yml").read_text()
    assert "astral-sh/setup-uv@v6" in workflow
    assert (
        'uvx --from "git+https://github.com/Simon-McIntosh/reckon@v0.2.0rc25"'
        in workflow
    )
    assert "reckon build docs" in workflow
    assert "pip install" not in workflow


def test_repository_workflow_is_uv_native():
    workflow = (REPO_ROOT / ".github" / "workflows" / "reckon-pages.yml").read_text()

    assert "astral-sh/setup-uv@v6" in workflow
    assert "uv run --frozen reckon build docs" in workflow
    assert "pip install" not in workflow


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory):
    wheel_dir = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(wheel_dir.glob("reckon_plans-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_canonical_frontend_assets(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())

    expected_ui = {
        f"reckon/_assets/ui/{path.name}"
        for path in (REPO_ROOT / "docs" / "ui").iterdir()
        if path.is_file()
    }
    expected_shared = {
        f"reckon/_assets/_shared/{path.name}"
        for path in (REPO_ROOT / "docs" / "_shared").iterdir()
        if path.is_file()
    }
    assert expected_ui <= names
    assert expected_shared <= names
    assert "reckon/_assets/_shared/state.js" in names


@pytest.fixture(scope="session")
def installed_static_site(tmp_path_factory, built_wheel):
    root = tmp_path_factory.mktemp("installed-build")
    site_packages = root / "site-packages"
    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(site_packages),
            str(built_wheel),
        ],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    docs_dir = root / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, project="installed", slug="portable", sprint="S8")
    _write_discovery_pages(docs_dir)
    index_path = _seed_project_index(docs_dir, "installed")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site_packages)
    env["PYTHONNOUSERSITE"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from reckon.cli import main; main()",
            "build",
            str(docs_dir),
            "--project",
            "installed",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    version_result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from reckon.cli import main; main()",
            "--version",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    metadata_result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlib.metadata import version; print(version('reckon-plans'))",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert version_result.returncode == 0, version_result.stdout + version_result.stderr
    assert metadata_result.returncode == 0, (
        metadata_result.stdout + metadata_result.stderr
    )
    return docs_dir, index_path, result, version_result, metadata_result


def test_installed_wheel_build_emits_complete_static_site(installed_static_site):
    docs_dir, _, result, version_result, metadata_result = installed_static_site

    assert len(list((docs_dir / "_ui").iterdir())) == len(
        list((REPO_ROOT / "docs" / "ui").iterdir())
    )
    assert len(list((docs_dir / "_shared").iterdir())) == len(
        list((REPO_ROOT / "docs" / "_shared").iterdir())
    )
    assert (docs_dir / "_shared" / "state.js").is_file()
    assert (docs_dir / ".nojekyll").is_file()
    assert "Build complete" in result.stdout
    installed_version = metadata_result.stdout.strip()
    assert installed_version != "dev"
    assert version_result.stdout.strip() == f"reckon, version {installed_version}"


def test_installed_wheel_build_emits_usable_state(installed_static_site):
    docs_dir, index_path, _, _, _ = installed_static_site
    index_html = (docs_dir / "index.html").read_text()
    state = json.loads(index_path.read_text())["data"]

    assert 'src="_ui/shell.jsx"' in index_html
    assert 'href="_shared/foundation.css"' in index_html
    assert "portable" in {item["slug"] for item in state["inventory"]}
    assert {item["id"] for item in state["sprints"]} == {"S7", "S8"}
    assert state["timeline"]
    assert state["blockers"]
