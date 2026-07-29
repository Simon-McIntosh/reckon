from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.cli as cli


REPO_ROOT = Path(__file__).parents[1]


def _write_plan(docs_dir: Path, *, project: str, slug: str, sprint: str) -> None:
    (docs_dir / f"{slug}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug.title()}">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-sprint" content="{sprint}">'
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
    assert (docs_dir / ".nojekyll").is_file()


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
    return docs_dir, index_path, result


def test_installed_wheel_build_emits_complete_static_site(installed_static_site):
    docs_dir, _, result = installed_static_site

    assert len(list((docs_dir / "_ui").iterdir())) == len(
        list((REPO_ROOT / "docs" / "ui").iterdir())
    )
    assert len(list((docs_dir / "_shared").iterdir())) == len(
        list((REPO_ROOT / "docs" / "_shared").iterdir())
    )
    assert (docs_dir / "_shared" / "state.js").is_file()
    assert (docs_dir / ".nojekyll").is_file()
    assert "Build complete" in result.stdout


def test_installed_wheel_build_emits_usable_state(installed_static_site):
    docs_dir, index_path, _ = installed_static_site
    index_html = (docs_dir / "index.html").read_text()
    state = json.loads(index_path.read_text())["data"]

    assert 'src="_ui/shell.jsx"' in index_html
    assert 'href="_shared/foundation.css"' in index_html
    assert "portable" in {item["slug"] for item in state["inventory"]}
    assert {item["id"] for item in state["sprints"]} == {"S7", "S8"}
    assert state["timeline"]
    assert state["blockers"]
