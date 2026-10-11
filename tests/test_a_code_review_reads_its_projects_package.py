"""A code review reads the reviewed repository's own import package.

The code-subject builder reads production modules from the package snapshot at
the reviewed revision. The snapshot resolves that package from the repository
under review — its ``pyproject.toml`` project name, or its single top-level
package — rather than a literal ``reckon`` prefix, so a review of a repository
whose package is named something else finds its modules. A repository declaring
two candidate packages and no matching project name is refused by name.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon import served_code
from reckon.crew import plan_review
from reckon.served_code import (
    PackageNotFoundError,
    SourceSnapshot,
    neighbourhood,
    package_modules,
)
from reckon.velocity import read_sources

# The declared mutation: restore the literal ``reckon`` prefix in the package
# snapshot, so a repository whose package is ``acme_tools`` reads no modules and
# the acme cases below fail. Set the environment variable to run the red arm.
NEGATIVE_CONTROL_MUTATION = (
    "restore the literal reckon prefix in the package snapshot; "
    "the acme_tools cases must fail"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_PACKAGE_PREFIX_NEGATIVE") == "1"

_ACME_MODULES = frozenset(
    {
        "acme_tools",
        "acme_tools.a",
        "acme_tools.b",
        "acme_tools.sub",
        "acme_tools.sub.c",
        "acme_tools.sub.d",
    }
)


def _build_repo(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``root`` and commit them at ``HEAD``."""

    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    identity = ["-c", "user.email=test@example.invalid", "-c", "user.name=Test"]
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", *identity, "commit", "-qm", "chore: seed fixture"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


def _acme_files() -> dict[str, str]:
    return {
        "pyproject.toml": '[project]\nname = "acme-tools"\nversion = "0"\n',
        "acme_tools/__init__.py": "",
        "acme_tools/a.py": "VALUE = 1\n",
        "acme_tools/b.py": "import acme_tools.a\n\n\nB = acme_tools.a.VALUE\n",
        "acme_tools/sub/__init__.py": "",
        "acme_tools/sub/c.py": "C = 3\n",
        "acme_tools/sub/d.py": "D = 4\n",
    }


def _literal_prefix_snapshot(repo, revision) -> SourceSnapshot:
    """The mutated snapshot: the package prefix is the literal ``reckon``."""

    sources: dict[str, str] = {}
    files: dict[str, tuple[tuple[int, int, int, int, int], str]] = {}
    for path, text in read_sources(repo, revision, prefix="reckon").items():
        relative = path.removeprefix("reckon/")
        sources[relative] = text.decode("utf-8-sig")
        files[relative] = ((0, 0, 0, 0, 0), "")
    return SourceSnapshot(
        root=Path("reckon"), files=files, sources=sources, revision=revision or None
    )


@pytest.fixture(autouse=True)
def _package_prefix(monkeypatch):
    if NEGATIVE_CONTROL:
        monkeypatch.setattr(served_code, "_package_snapshot", _literal_prefix_snapshot)


@pytest.fixture
def acme(tmp_path):
    return _build_repo(tmp_path / "acme", _acme_files())


def test_package_modules_lists_the_declared_package(acme):
    assert package_modules(str(acme), "HEAD") == _ACME_MODULES


def test_neighbourhood_of_a_named_file_holds_its_importer(acme):
    assert neighbourhood(str(acme), "HEAD", ["acme_tools/a.py"]) == frozenset(
        {"acme_tools.b"}
    )


def test_a_directory_path_names_the_modules_beneath_it(acme):
    subject = plan_review.code_review_subject(
        "sample", "coordinator", paths=["acme_tools/sub"], repo=str(acme)
    )
    assert subject["named_paths"] == ["acme_tools.sub.c", "acme_tools.sub.d"]
    brief = plan_review.code_review_brief(subject)
    assert "acme_tools.sub.c" in brief
    assert "acme_tools.sub.d" in brief


def test_two_candidate_packages_are_refused_by_name(tmp_path):
    files = {
        "pyproject.toml": '[project]\nname = "unrelated-name"\nversion = "0"\n',
        "alpha/__init__.py": "",
        "alpha/one.py": "ONE = 1\n",
        "beta/__init__.py": "",
        "beta/two.py": "TWO = 2\n",
    }
    repo = _build_repo(tmp_path / "two", files)

    with pytest.raises(PackageNotFoundError) as raised:
        package_modules(str(repo), "HEAD")

    message = str(raised.value)
    assert "alpha" in message
    assert "beta" in message


def test_this_repository_reads_the_same_modules_as_the_reckon_listing():
    repo = str(Path(__file__).resolve().parents[1])
    expected = {
        _module_name("reckon", path.removeprefix("reckon/"))
        for path in read_sources(repo, "HEAD", prefix="reckon")
    }

    assert set(package_modules(repo, "HEAD")) == expected


def _module_name(package: str, relative: str) -> str:
    parts = relative.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([package, *parts]) if parts else package
