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

# The declared mutation: resolve the package without the named paths, so a
# repository declaring several candidates and no matching project name is
# refused by name and the first acme case below fails. Set the environment
# variable to run the red arm.
PATHS_MUTATION = (
    "resolve the package without the named paths - the first case must fail "
    "with the refusal naming 'acme_tools', 'apps' and 'benchmarks'"
)
PATHS_NEGATIVE = os.environ.get("RECKON_TEST_PACKAGE_IGNORES_PATHS") == "1"

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
    if PATHS_NEGATIVE:
        monkeypatch.setattr(
            served_code, "package_directory", _package_directory_without_paths
        )


def _package_directory_without_paths(repo, revision, paths=()):
    """The mutated resolution: the named paths are ignored."""

    return _package_directory_ignoring_paths(repo, revision)


_package_directory_ignoring_paths = served_code.package_directory


@pytest.fixture
def acme(tmp_path):
    return _build_repo(tmp_path / "acme", _acme_files())


def _three_package_files() -> dict[str, str]:
    """A repository declaring three candidates and a name naming no directory."""

    return {
        "pyproject.toml": '[project]\nname = "unrelated-name"\nversion = "0"\n',
        "acme_tools/__init__.py": "",
        "acme_tools/core.py": (
            "import acme_tools.helper\n\n\nVALUE = acme_tools.helper.BASE\n"
        ),
        "acme_tools/helper.py": "BASE = 1\n",
        "acme_tools/sub/__init__.py": "",
        "acme_tools/sub/c.py": "C = 3\n",
        "acme_tools/sub/d.py": "D = 4\n",
        "apps/__init__.py": "import acme_tools.helper\n",
        "apps/main.py": "import apps.util\n\n\nMAIN = apps.util.U\n",
        "apps/util.py": "U = 1\n",
        "benchmarks/__init__.py": "",
        "benchmarks/run.py": "RUN = 1\n",
    }


@pytest.fixture
def three(tmp_path):
    return _build_repo(tmp_path / "three", _three_package_files())


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


def _subject(three, *paths):
    return plan_review.code_review_subject(
        "sample", "coordinator", paths=list(paths), repo=str(three)
    )


def test_a_named_path_settles_the_package(three):
    subject = _subject(three, "acme_tools/core.py")

    assert all(path.startswith("acme_tools") for path in subject["named_paths"])
    assert subject["modules"], subject["modules"]
    assert all(module.startswith("acme_tools.") for module in subject["modules"])
    # A module one import away, inside acme_tools, is in the neighbourhood.
    assert "acme_tools.helper" in subject["modules"]


def test_a_directory_path_under_the_package_expands_to_its_modules(three):
    subject = _subject(three, "acme_tools/sub")

    assert subject["named_paths"] == ["acme_tools.sub.c", "acme_tools.sub.d"]


def test_a_path_under_another_package_composes_with_that_package(three):
    subject = _subject(three, "apps/main.py")

    assert all(path.startswith("apps") for path in subject["named_paths"])
    assert all(module.startswith("apps.") for module in subject["modules"])
    assert "apps.util" in subject["modules"]


def test_paths_under_two_packages_are_refused_naming_both(three):
    with pytest.raises(PackageNotFoundError) as raised:
        _subject(three, "acme_tools/core.py", "apps/main.py")

    message = str(raised.value)
    assert "acme_tools" in message
    assert "apps" in message


def _module_name(package: str, relative: str) -> str:
    parts = relative.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([package, *parts]) if parts else package
