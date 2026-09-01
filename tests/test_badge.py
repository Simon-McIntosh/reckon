from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon.cli import main

ROOT = Path(__file__).parents[1]
REAL_README = ROOT / "README.md"


def _repository(tmp_path: Path, *, declared: bool = True) -> Path:
    root = tmp_path / "imas-codex"
    (root / "docs" / "_shared").mkdir(parents=True)
    (root / "README.md").write_text("# IMAS Codex\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "git@github.com:ITER-Organization/imas-codex.git",
        ],
        check=True,
    )
    if declared:
        workflow = root / ".github" / "workflows" / "reckon-pages.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "name: Publish plans\n"
            "env:\n"
            "  RECKON_PAGES_REPOSITORY: ITER-Organization/imas-codex\n",
            encoding="utf-8",
        )
    return root


def _invoke(root: Path, *extra: str):
    return CliRunner().invoke(
        main,
        [
            "badge",
            "--project",
            "imas-codex",
            "--checkout-path",
            str(root),
            *extra,
        ],
    )


def test_badge_prints_declared_repository_site_and_write_is_idempotent(
    tmp_path: Path,
) -> None:
    real_readme_before = REAL_README.read_bytes()
    root = _repository(tmp_path)
    readme = root / "README.md"
    expected = (
        "[![Plans](docs/_shared/badge.svg)]"
        "(https://iter-organization.github.io/imas-codex/)"
    )

    printed = _invoke(root)

    assert printed.exit_code == 0, printed.output
    assert printed.output == f"{expected}\n"
    assert readme.read_text(encoding="utf-8") == "# IMAS Codex\n"

    first_write = _invoke(root, "--write")
    assert first_write.exit_code == 0, first_write.output
    first_bytes = readme.read_bytes()

    second_write = _invoke(root, "--write")
    assert second_write.exit_code == 0, second_write.output
    second_bytes = readme.read_bytes()
    assert second_bytes == first_bytes

    altered = readme.read_text(encoding="utf-8").replace(expected, "[old badge](old)")
    readme.write_text(altered, encoding="utf-8")
    third_write = _invoke(root, "--write")

    assert third_write.exit_code == 0, third_write.output
    updated = readme.read_text(encoding="utf-8")
    assert updated.count("<!-- reckon-plans-badge -->") == 1
    assert updated.count("<!-- /reckon-plans-badge -->") == 1
    assert updated.count(expected) == 1
    assert "[old badge](old)" not in updated
    assert REAL_README.read_bytes() == real_readme_before


def test_badge_refuses_repository_without_publication_declaration(
    tmp_path: Path,
) -> None:
    real_readme_before = REAL_README.read_bytes()
    root = _repository(tmp_path, declared=False)
    readme_before = (root / "README.md").read_bytes()

    result = _invoke(root, "--write")

    assert result.exit_code == 1
    assert "RECKON_PAGES_REPOSITORY" in result.output
    assert "[![Plans]" not in result.output
    assert (root / "README.md").read_bytes() == readme_before
    assert REAL_README.read_bytes() == real_readme_before
