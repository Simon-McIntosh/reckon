from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from reckon import cli
from reckon.pages import (
    PagesUndeterminedError,
    publication_is_declared,
    require_publication_declaration,
)

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reckon-pages.yml"


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_publication_requires_the_repository_local_declaration() -> None:
    public_fork_metadata = {
        "isPrivate": False,
        "isFork": True,
        "visibility": "PUBLIC",
    }

    assert not publication_is_declared(
        "Simon-McIntosh/imas-codex",
        public_fork_metadata.get("publication_repository"),
    )
    with pytest.raises(PagesUndeterminedError, match="not declared"):
        require_publication_declaration(
            "Simon-McIntosh/imas-codex",
            public_fork_metadata.get("publication_repository"),
        )

    declared_repository = _load_workflow()["env"]["RECKON_PAGES_REPOSITORY"]
    assert publication_is_declared("Simon-McIntosh/reckon", declared_repository)
    require_publication_declaration("Simon-McIntosh/reckon", declared_repository)


def test_deploy_is_main_only_and_holds_its_own_write_permissions() -> None:
    workflow = _load_workflow()
    validate = workflow["jobs"]["validate"]
    deploy = workflow["jobs"]["deploy"]

    assert validate == {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v4"},
            {"uses": "astral-sh/setup-uv@v6"},
            {"run": "uv run --frozen reckon build docs"},
        ],
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert deploy["needs"] == "validate"
    assert deploy["if"] == "${{ github.ref == 'refs/heads/main' }}"
    assert deploy["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert any(
        "require_publication_declaration" in step.get("run", "")
        for step in deploy["steps"]
    )


def test_deployed_build_uses_the_canonical_ui_and_shared_assets(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "sample.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="plan-slug" content="sample">'
        '<meta name="plan-title" content="Sample">'
        '<meta name="plan-status" content="active">'
        "<title>Sample</title></head><body><main></main></body></html>"
    )

    result = CliRunner().invoke(cli.main, ["build", str(docs), "--project", "sample"])

    assert result.exit_code == 0, result.output
    source_ui = {path.name for path in (ROOT / "docs" / "ui").iterdir()}
    compiled_ui = {f"{path.stem}.js" for path in (ROOT / "docs" / "ui").glob("*.jsx")}
    source_shared = {path.name for path in (ROOT / "docs" / "_shared").iterdir()}
    built_ui = {path.name for path in (docs / "_ui").iterdir()}
    built_shared = {path.name for path in (docs / "_shared").iterdir()}

    assert built_ui ^ (source_ui | compiled_ui) == set()
    assert built_shared ^ source_shared == set()
    assert all(
        (docs / "_ui" / path.name).read_bytes() == path.read_bytes()
        for path in (ROOT / "docs" / "ui").iterdir()
    )
    assert all(
        (docs / "_shared" / path.name).read_bytes() == path.read_bytes()
        for path in (ROOT / "docs" / "_shared").iterdir()
    )
