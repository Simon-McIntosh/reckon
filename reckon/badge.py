"""Render and install a badge for a repository's declared plans site."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

from reckon.pages import (
    PagesUndeterminedError,
    PublicationStrategy,
    RepositoryCoordinates,
    _pages_site_base,
    _repository_coordinates,
    require_publication_declaration,
    write_readme_badge,
)

DECLARATION_NAME = "RECKON_PAGES_REPOSITORY"
DECLARATION_PATH = Path(".github/workflows/reckon-pages.yml")


def declared_badge(docs_dir: Path) -> tuple[str, PublicationStrategy]:
    """Return badge Markdown and the publication strategy declared by the repo."""
    resolved_docs = docs_dir.expanduser().resolve()
    repo_root = resolved_docs.parent
    declaration_path = repo_root / DECLARATION_PATH
    declared_repository = _read_declaration(declaration_path)
    repository = _repository_coordinates(repo_root)
    try:
        require_publication_declaration(repository.full_name, declared_repository)
    except PagesUndeterminedError as exc:
        raise PagesUndeterminedError(
            f"{exc}; set env.{DECLARATION_NAME} to {repository.full_name} in "
            f"{declaration_path}"
        ) from exc

    declared_owner, declared_name = declared_repository.split("/", maxsplit=1)
    declared_coordinates = RepositoryCoordinates(declared_owner, declared_name)
    site_url = _pages_site_base(declared_coordinates, 404, {})
    docs_relative = PurePosixPath(resolved_docs.relative_to(repo_root).as_posix())
    image_target = (docs_relative / "_shared" / "badge.svg").as_posix()
    markdown = f"[![Plans]({image_target})]({site_url})"
    strategy = PublicationStrategy(
        name="declared-workflow",
        write_workflow=False,
        branch=None,
        repository_path=docs_relative,
        site_subpath=PurePosixPath("."),
        site_url=site_url,
    )
    return markdown, strategy


def install_declared_badge(docs_dir: Path, strategy: PublicationStrategy) -> bool:
    """Insert or update the declared plans badge in the repository README."""
    return write_readme_badge(docs_dir, strategy)


def _read_declaration(path: Path) -> str:
    if not path.is_file():
        raise PagesUndeterminedError(_declaration_requirement(path))
    try:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PagesUndeterminedError(
            f"cannot read the publication declaration from {path}: {exc}; "
            f"{_declaration_requirement(path)}"
        ) from exc
    if not isinstance(workflow, dict):
        raise PagesUndeterminedError(_declaration_requirement(path))
    environment = workflow.get("env")
    declared = (
        environment.get(DECLARATION_NAME) if isinstance(environment, dict) else None
    )
    if not isinstance(declared, str) or not declared.strip():
        raise PagesUndeterminedError(_declaration_requirement(path))
    return declared.strip()


def _declaration_requirement(path: Path) -> str:
    return (
        "plans publication is not declared; add "
        f"`env.{DECLARATION_NAME}: owner/repository` to {path}"
    )
