"""Fail-closed GitHub Pages detection and publication strategy selection."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


_GITHUB_API = "https://api.github.com"
_REMOTE_PATTERN = re.compile(
    r"^(?:git@github\.com:|https?://github\.com/)(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)


class PagesError(RuntimeError):
    """Base class for Pages states that cannot be used safely."""


class PagesAuthenticationError(PagesError):
    """The Pages authority could not be queried with valid credentials."""


class PagesUndeterminedError(PagesError):
    """The Pages authority returned a state that cannot be classified safely."""


class PagesConflictError(PagesError):
    """An existing publisher requires repository-owner coordination."""


@dataclass(frozen=True)
class RepositoryCoordinates:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class PagesConfiguration:
    build_type: str
    branch: str | None = None
    source_path: str | None = None


@dataclass(frozen=True)
class PublicationStrategy:
    name: str
    write_workflow: bool
    branch: str | None
    repository_path: PurePosixPath
    site_subpath: PurePosixPath

    def describe(self) -> str:
        branch = f", branch={self.branch}" if self.branch else ""
        return (
            f"{self.name}{branch}, repository-path={self.repository_path.as_posix()}, "
            f"site-subpath=/{self.site_subpath.as_posix()}"
        )


def pages_configuration_from_response(
    status: int, payload: Mapping[str, Any]
) -> PagesConfiguration | None:
    """Classify one recorded or live response from the Pages endpoint."""
    if status in (401, 403):
        raise PagesAuthenticationError(
            "GitHub Pages configuration was not authenticated; refusing to choose "
            "a publication strategy"
        )
    if status == 404:
        return None
    if status != 200:
        raise PagesUndeterminedError(
            f"GitHub Pages configuration returned HTTP {status}; refusing to guess"
        )

    build_type = payload.get("build_type")
    if build_type == "workflow":
        return PagesConfiguration(build_type="workflow")
    if build_type != "legacy":
        raise PagesUndeterminedError(
            "GitHub Pages configuration has an unknown build type; refusing to guess"
        )

    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise PagesUndeterminedError(
            "legacy GitHub Pages configuration has no authoritative source; "
            "refusing to guess"
        )
    branch = source.get("branch")
    source_path = source.get("path")
    if not isinstance(branch, str) or not branch.strip():
        raise PagesUndeterminedError(
            "legacy GitHub Pages configuration has no branch; refusing to guess"
        )
    if source_path not in ("/", "/docs"):
        raise PagesUndeterminedError(
            "legacy GitHub Pages configuration uses an unsupported source path; "
            "refusing to guess"
        )
    return PagesConfiguration(
        build_type="legacy", branch=branch, source_path=source_path
    )


def select_publication_strategy(
    configuration: PagesConfiguration | None,
    *,
    docs_path: str | PurePosixPath = "docs",
) -> PublicationStrategy:
    """Choose an additive publication location without replacing an existing site."""
    repository_docs = _relative_repository_path(docs_path)
    if configuration is None:
        return PublicationStrategy(
            name="deploying-workflow",
            write_workflow=True,
            branch=None,
            repository_path=repository_docs,
            site_subpath=repository_docs,
        )

    if configuration.build_type == "workflow":
        raise PagesConflictError(
            "Actions-based Pages already publishes this repository; refusing to "
            "replace its artifact. The existing publisher must absorb reckon's output."
        )
    if configuration.build_type != "legacy":
        raise PagesUndeterminedError(
            "GitHub Pages configuration cannot be mapped safely; refusing to guess"
        )

    if configuration.source_path == "/docs":
        publish_path = PurePosixPath("docs") / "reckon"
        return PublicationStrategy(
            name="legacy-docs-subdirectory",
            write_workflow=False,
            branch=configuration.branch,
            repository_path=publish_path,
            site_subpath=PurePosixPath("reckon"),
        )

    if configuration.source_path == "/" and configuration.branch == "gh-pages":
        publish_path = PurePosixPath("reckon")
        return PublicationStrategy(
            name="legacy-pages-branch-subpath",
            write_workflow=False,
            branch=configuration.branch,
            repository_path=publish_path,
            site_subpath=publish_path,
        )

    if configuration.source_path == "/":
        return PublicationStrategy(
            name="legacy-branch-root-subpath",
            write_workflow=False,
            branch=configuration.branch,
            repository_path=repository_docs,
            site_subpath=repository_docs,
        )

    raise PagesUndeterminedError(
        "GitHub Pages configuration cannot be mapped safely; refusing to guess"
    )


def detect_publication_strategy(
    docs_dir: Path,
    *,
    token: str | None = None,
    api_url: str = _GITHUB_API,
) -> PublicationStrategy:
    """Read the repository's live Pages authority and choose a safe strategy."""
    resolved_docs = docs_dir.expanduser().resolve()
    repo_root = _repository_root(resolved_docs)
    repository = _repository_coordinates(repo_root)
    credential = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not credential:
        raise PagesAuthenticationError(
            "GitHub Pages configuration requires GH_TOKEN or GITHUB_TOKEN; "
            "refusing to assume the repository has no site"
        )

    base = api_url.rstrip("/")
    encoded_name = "/".join(
        quote(part, safe="") for part in repository.full_name.split("/")
    )
    repository_status, repository_payload = _request_json(
        f"{base}/repos/{encoded_name}", credential
    )
    _verify_repository_response(repository, repository_status, repository_payload)

    pages_status, pages_payload = _request_json(
        f"{base}/repos/{encoded_name}/pages", credential
    )
    configuration = pages_configuration_from_response(pages_status, pages_payload)
    docs_relative = PurePosixPath(resolved_docs.relative_to(repo_root).as_posix())
    return select_publication_strategy(configuration, docs_path=docs_relative)


def _verify_repository_response(
    expected: RepositoryCoordinates, status: int, payload: Mapping[str, Any]
) -> None:
    if status in (401, 403):
        raise PagesAuthenticationError(
            "GitHub repository lookup was not authenticated; refusing to choose a "
            "Pages publication strategy"
        )
    if status != 200:
        raise PagesUndeterminedError(
            f"GitHub repository lookup returned HTTP {status}; a Pages 404 would be "
            "ambiguous, so publication is refused"
        )
    full_name = payload.get("full_name")
    if (
        not isinstance(full_name, str)
        or full_name.casefold() != expected.full_name.casefold()
    ):
        raise PagesUndeterminedError(
            "GitHub repository identity did not match the configured origin; "
            "refusing to query Pages for a different repository"
        )


def _repository_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PagesUndeterminedError(
            "the docs path is not inside a Git repository; Pages configuration "
            "cannot be determined"
        )
    return Path(result.stdout.strip()).resolve()


def _repository_coordinates(repo_root: Path) -> RepositoryCoordinates:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PagesUndeterminedError(
            "the Git repository has no origin remote; Pages configuration cannot "
            "be determined"
        )
    remote = result.stdout.strip()
    match = _REMOTE_PATTERN.fullmatch(remote)
    if match is None and remote.startswith("ssh://"):
        parsed = urlsplit(remote)
        if parsed.hostname == "github.com":
            match = _REMOTE_PATTERN.fullmatch(
                f"https://github.com/{parsed.path.lstrip('/')}"
            )
    if match is None:
        raise PagesUndeterminedError(
            "the origin remote is not an unambiguous github.com repository; "
            "Pages configuration cannot be determined"
        )
    return RepositoryCoordinates(owner=match.group("owner"), name=match.group("name"))


def _request_json(url: str, token: str) -> tuple[int, Mapping[str, Any]]:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "reckon-pages-onboarding",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise PagesUndeterminedError(
            f"GitHub Pages configuration could not be read: {exc}; refusing to guess"
        ) from exc

    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PagesUndeterminedError(
            "GitHub returned a non-JSON response for Pages configuration; "
            "refusing to guess"
        ) from exc
    if not isinstance(payload, Mapping):
        raise PagesUndeterminedError(
            "GitHub returned an unexpected Pages response shape; refusing to guess"
        )
    return status, payload


def _relative_repository_path(path: str | PurePosixPath) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise PagesUndeterminedError(
            "the docs path is not a safe repository-relative publication path"
        )
    return candidate
