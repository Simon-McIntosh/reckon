from __future__ import annotations

import importlib
import json
import os
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon.cli import main


def _write_html_resource(
    docs_dir: Path,
    project: str,
    resource_type: str,
    slug: str,
    tags: list[str],
) -> None:
    path = (
        docs_dir
        / {
            "plan": "plans",
            "research": "research",
            "evidence": "evidence",
        }[resource_type]
        / f"{slug}.html"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {
        "type": resource_type,
        "slug": slug,
        "title": slug,
        "tags": tags,
    }
    if resource_type == "plan":
        state["status"] = "active"
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def tagged_project(tmp_path: Path) -> tuple[Path, str, Path]:
    project = "tagged-project"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    _write_html_resource(
        docs_dir,
        project,
        "plan",
        "tagged-plan",
        ["standrd-names", "shared-topic"],
    )
    _write_html_resource(
        docs_dir,
        project,
        "research",
        "tagged-research",
        ["standard-names", "shared-topic"],
    )
    _write_html_resource(
        docs_dir,
        project,
        "evidence",
        "tagged-evidence",
        ["standard-names"],
    )

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    return docs_dir, project, mounts_file


def _cli_env(mounts_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["RECKON_MOUNTS_PATH"] = str(mounts_file)
    return env


def test_tag_rename_command_invokes_backend_with_expected_args(
    tagged_project: tuple[Path, str, Path], monkeypatch
) -> None:
    docs_dir, project, mounts_file = tagged_project
    runner = CliRunner()
    called: dict[str, object] = {}

    def fake_rename_project_tag(
        resolved_docs, resolved_project, source, target, *, dry_run: bool = False
    ):
        called["docs"] = resolved_docs
        called["project"] = resolved_project
        called["source"] = source
        called["target"] = target
        called["dry_run"] = dry_run
        return {
            "project": resolved_project,
            "source": source,
            "target": target,
            "dry_run": dry_run,
            "changed": 0,
            "resources": [],
        }

    monkeypatch.setattr("reckon.tags.rename_project_tag", fake_rename_project_tag)

    result = runner.invoke(
        main,
        [
            "tag",
            "rename",
            "--project",
            project,
            "standrd-names",
            "standard-names",
        ],
        env=_cli_env(mounts_file),
    )

    assert result.exit_code == 0, result.output
    assert called == {
        "docs": docs_dir,
        "project": project,
        "source": "standrd-names",
        "target": "standard-names",
        "dry_run": False,
    }


def test_tag_rename_command_dry_run_returns_report_and_leaves_resources_unchanged(
    tagged_project: tuple[Path, str, Path]
) -> None:
    docs_dir, project, mounts_file = tagged_project
    tracked = {
        path: path.read_bytes()
        for path in (
            list((docs_dir / "plans").glob("*.html"))
            + list((docs_dir / "research").glob("*.html"))
            + list((docs_dir / "evidence").glob("*.html"))
            + list((docs_dir / "sprints").glob("*.html"))
        )
    }

    result = CliRunner().invoke(
        main,
        [
            "tag",
            "rename",
            "--project",
            project,
            "--dry-run",
            "standrd-names",
            "standard-names",
        ],
        env=_cli_env(mounts_file),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["changed"] == 0

    current = {
        path: path.read_bytes()
        for path in (
            list((docs_dir / "plans").glob("*.html"))
            + list((docs_dir / "research").glob("*.html"))
            + list((docs_dir / "evidence").glob("*.html"))
            + list((docs_dir / "sprints").glob("*.html"))
        )
    }
    assert current == tracked


def test_audit_rename_invocation_is_executable(
    tagged_project: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_dir, project, mounts_file = tagged_project
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))

    importlib.invalidate_caches()
    import reckon._store as store_module
    import reckon.mcp as mcp_module

    importlib.reload(store_module)
    importlib.reload(mcp_module)

    report = mcp_module._audit(project)
    assert "findings" in report, report
    invocation = next(
        item["extra"]["rename_invocation"]
        for item in report["findings"]
        if item["code"] == "tag-near-duplicate"
    )

    result = CliRunner().invoke(
        main,
        shlex.split(invocation)[1:],
        env=_cli_env(mounts_file),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project"] == project
    assert payload["source"] == "standrd-names"
    assert payload["target"] == "standard-names"
    assert payload["changed"] > 0
    assert payload["dry_run"] is False
