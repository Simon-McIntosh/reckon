from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.cli as cli
import reckon.pages as pages


FIXTURES = Path(__file__).parent / "fixtures" / "pages"


def _recorded_response(name: str) -> tuple[int, dict]:
    recorded = json.loads((FIXTURES / f"{name}.json").read_text())
    return recorded["status"], recorded["body"]


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "consumer"
    docs = root / "docs"
    docs.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "git@github.com:example/consumer.git",
        ],
        check=True,
    )
    return root, docs


def _invoke_recorded_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_name: str,
):
    root, docs = _repository(tmp_path)
    pages_response = _recorded_response(response_name)
    monkeypatch.setenv("GH_TOKEN", "recorded-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def recorded_request(url: str, token: str):
        assert token == "recorded-token"
        if url.endswith("/pages"):
            return pages_response
        return 200, {"full_name": "example/consumer"}

    monkeypatch.setattr(pages, "_request_json", recorded_request)
    result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(docs),
            "--project",
            "consumer",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
            "--generate-ci",
        ],
    )
    return root, result


def test_no_pages_selects_deploying_workflow_from_recorded_response(
    tmp_path, monkeypatch
):
    root, result = _invoke_recorded_sync(tmp_path, monkeypatch, "no-pages")

    assert result.exit_code == 0, result.output
    assert (root / ".github/workflows/reckon-pages.yml").is_file()


@pytest.mark.parametrize(
    ("response_name", "strategy", "repository_path", "site_subpath"),
    [
        ("legacy-root", "legacy-branch-root-subpath", "docs", "/docs"),
        ("legacy-docs", "legacy-docs-subdirectory", "docs/reckon", "/reckon"),
        (
            "legacy-pages-branch",
            "legacy-pages-branch-subpath",
            "reckon",
            "/reckon",
        ),
    ],
)
def test_legacy_pages_selects_additive_subpath_without_writing_workflow(
    tmp_path,
    monkeypatch,
    response_name,
    strategy,
    repository_path,
    site_subpath,
):
    root, result = _invoke_recorded_sync(tmp_path, monkeypatch, response_name)

    assert result.exit_code == 0, result.output
    assert not (root / ".github/workflows/reckon-pages.yml").exists()
    assert strategy in result.output
    assert f"repository-path={repository_path}" in result.output
    assert f"site-subpath={site_subpath}" in result.output
    assert "no workflow written" in result.output


def test_actions_pages_refuses_and_names_existing_publisher(tmp_path, monkeypatch):
    root, result = _invoke_recorded_sync(tmp_path, monkeypatch, "actions")

    assert result.exit_code == 1
    assert "Actions-based Pages already publishes this repository" in result.output
    assert "existing publisher must absorb reckon's output" in result.output
    assert not (root / ".github/workflows/reckon-pages.yml").exists()
    assert not (tmp_path / "mounts.json").exists()


def test_unauthenticated_pages_response_refuses_instead_of_defaulting(
    tmp_path, monkeypatch
):
    root, result = _invoke_recorded_sync(tmp_path, monkeypatch, "unauthenticated")

    assert result.exit_code == 1
    assert "not authenticated" in result.output
    assert "refusing" in result.output
    assert not (root / ".github/workflows/reckon-pages.yml").exists()
    assert not (tmp_path / "mounts.json").exists()


def test_undetermined_pages_response_refuses_instead_of_defaulting(
    tmp_path, monkeypatch
):
    root, result = _invoke_recorded_sync(tmp_path, monkeypatch, "undetermined")

    assert result.exit_code == 1
    assert "unsupported source path" in result.output
    assert "refusing to guess" in result.output
    assert not (root / ".github/workflows/reckon-pages.yml").exists()
    assert not (tmp_path / "mounts.json").exists()


def test_missing_token_refuses_before_querying_github(tmp_path, monkeypatch):
    root, docs = _repository(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        pages,
        "_request_json",
        lambda *_: pytest.fail("an unauthenticated request must not be sent"),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "sync",
            str(docs),
            "--generate-ci",
            "--mounts",
            str(tmp_path / "mounts.json"),
            "--state-root",
            str(tmp_path / "state"),
        ],
    )

    assert result.exit_code == 1
    assert "requires GH_TOKEN or GITHUB_TOKEN" in result.output
    assert "refusing to assume" in result.output
    assert not (root / ".github/workflows/reckon-pages.yml").exists()
    assert not (tmp_path / "mounts.json").exists()
