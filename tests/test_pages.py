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
    *,
    readme_text: str | None = None,
):
    root, docs = _repository(tmp_path)
    if readme_text is not None:
        (root / "README.md").write_text(readme_text)
    pages_status, pages_payload = _recorded_response(response_name)
    if pages_status == 200:
        pages_payload = {
            **pages_payload,
            "html_url": "https://example.github.io/consumer/",
        }
    pages_response = pages_status, pages_payload
    monkeypatch.setenv("GH_TOKEN", "recorded-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def recorded_request(url: str, token: str):
        assert token == "recorded-token"
        if url.endswith("/pages"):
            return pages_response
        return 200, {"full_name": "example/consumer"}

    monkeypatch.setattr(pages, "_request_json", recorded_request)
    result = _invoke_sync(tmp_path, docs, publish=True)
    return root, result


def _invoke_sync(tmp_path: Path, docs: Path, *, publish: bool):
    args = [
        "sync",
        str(docs),
        "--project",
        "consumer",
        "--mounts",
        str(tmp_path / "mounts.json"),
        "--state-root",
        str(tmp_path / "state"),
    ]
    if publish:
        args.append("--generate-ci")
    return CliRunner().invoke(
        cli.main,
        args,
    )


def test_no_pages_selects_deploying_workflow_from_recorded_response(
    tmp_path, monkeypatch
):
    root, result = _invoke_recorded_sync(tmp_path, monkeypatch, "no-pages")

    assert result.exit_code == 0, result.output
    assert (root / ".github/workflows/reckon-pages.yml").is_file()


def test_onboarding_inserts_one_local_badge_and_second_run_is_byte_identical(
    tmp_path, monkeypatch
):
    root, result = _invoke_recorded_sync(
        tmp_path, monkeypatch, "no-pages", readme_text="# Consumer\n"
    )

    assert result.exit_code == 0, result.output
    readme = root / "README.md"
    first = readme.read_bytes()
    text = first.decode()
    assert text.count("<!-- reckon-plans-badge -->") == 1
    assert text.count("<!-- /reckon-plans-badge -->") == 1
    assert "[![Plans](docs/_shared/badge.svg)]" in text
    assert "https://example.github.io/consumer/docs/" in text
    assert "shields.io" not in text
    assert (root / "docs/_shared/badge.svg").read_bytes() == (
        Path(__file__).parents[1] / "docs/_shared/badge.svg"
    ).read_bytes()

    second = _invoke_sync(tmp_path, root / "docs", publish=True)

    assert second.exit_code == 0, second.output
    assert readme.read_bytes() == first


def test_sync_without_publication_opt_in_does_not_add_badge(tmp_path):
    root, docs = _repository(tmp_path)

    result = _invoke_sync(tmp_path, docs, publish=False)

    assert result.exit_code == 0, result.output
    assert not (root / "README.md").exists()
    assert (root / "docs/_shared/badge.svg").is_file()


def test_badge_link_uses_detected_pages_subpath(tmp_path, monkeypatch):
    root, result = _invoke_recorded_sync(
        tmp_path, monkeypatch, "legacy-docs", readme_text="# Consumer\n"
    )

    assert result.exit_code == 0, result.output
    readme = (root / "README.md").read_text()
    assert "](https://example.github.io/consumer/reckon/)" in readme
    assert "](https://example.github.io/consumer/)" not in readme


def test_existing_badge_is_updated_in_place_when_pages_target_changes(
    tmp_path, monkeypatch
):
    root, first = _invoke_recorded_sync(
        tmp_path, monkeypatch, "legacy-root", readme_text="# Consumer\n"
    )
    assert first.exit_code == 0, first.output

    moved_status, moved_payload = _recorded_response("legacy-docs")
    moved_pages = moved_status, {
        **moved_payload,
        "html_url": "https://example.github.io/consumer/",
    }

    def moved_request(url: str, token: str):
        assert token == "recorded-token"
        if url.endswith("/pages"):
            return moved_pages
        return 200, {"full_name": "example/consumer"}

    monkeypatch.setattr(pages, "_request_json", moved_request)
    second = _invoke_sync(tmp_path, root / "docs", publish=True)

    assert second.exit_code == 0, second.output
    readme = (root / "README.md").read_text()
    assert readme.count("<!-- reckon-plans-badge -->") == 1
    assert readme.count("<!-- /reckon-plans-badge -->") == 1
    assert "https://example.github.io/consumer/reckon/" in readme
    assert "https://example.github.io/consumer/docs/" not in readme


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
