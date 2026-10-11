"""A ``--path`` naming a directory expands to the production modules beneath it.

The code-subject builder resolves each named path to production modules in the
package snapshot at the reviewed head. A directory names every module beneath
it, so naming a directory reviews its modules rather than matching nothing; a
directory carrying no module is refused by name rather than composing a brief
with no neighbourhood; a single file composes as it did before.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from reckon.crew import plan_review
from reckon.crew.node import CrewError

# The declared mutation: remove the directory expansion so a directory path is
# passed through unchanged and the subpackage case names no module. Set the
# environment variable to run the red arm.
NEGATIVE_CONTROL_MUTATION = (
    "remove the directory expansion from the code-subject builder; "
    "the subpackage case must fail with no named modules"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_DIRECTORY_PATH_NEGATIVE") == "1"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    root = tmp_path / "repo"
    package = root / "reckon"
    sub = package / "sub"
    assets = package / "assets"
    sub.mkdir(parents=True)
    assets.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "top.py").write_text("VALUE = 0\n", encoding="utf-8")
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "mod_a.py").write_text(
        "import reckon.sub.mod_b\n\n\nA = reckon.sub.mod_b.B\n", encoding="utf-8"
    )
    (sub / "mod_b.py").write_text("B = 1\n", encoding="utf-8")
    # A directory that carries no ``.py`` module, so its name resolves to none.
    (assets / "notes.md").write_text("not a module\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "reckon"],
        ["commit", "-q", "-m", "chore: seed package", "-m", "Supply a package."],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _subject(repo, *paths):
    return plan_review.code_review_subject(
        "sample", "coordinator", paths=list(paths), repo=str(repo)
    )


def _line(brief: str, prefix: str) -> str:
    for line in brief.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"brief carries no {prefix!r} line:\n{brief}")


def test_a_subpackage_directory_expands_to_its_modules(repo, monkeypatch):
    if NEGATIVE_CONTROL:
        # The declared mutation: the paths are kept verbatim, so the directory
        # ``reckon/sub`` names no module and the assertion below fails.
        monkeypatch.setattr(
            plan_review,
            "_expand_code_review_paths",
            lambda repository, head, paths: list(paths),
        )
    subject = _subject(repo, "reckon/sub")
    assert subject["named_paths"] == ["reckon.sub.mod_a", "reckon.sub.mod_b"]
    brief = plan_review.code_review_brief(subject)
    named = _line(brief, "Named paths:")
    assert "reckon.sub.mod_a" in named
    assert "reckon.sub.mod_b" in named


def test_a_directory_with_no_module_is_refused_by_name(repo):
    with pytest.raises(CrewError) as raised:
        _subject(repo, "reckon/assets")
    assert "reckon/assets" in str(raised.value)


def test_a_single_file_path_composes_as_at_base(repo):
    subject = _subject(repo, "reckon/sub/mod_a.py")
    assert subject["named_paths"] == ["reckon/sub/mod_a.py"]
    assert "reckon.sub.mod_b" in subject["modules"]
    brief = plan_review.code_review_brief(subject)
    assert "reckon/sub/mod_a.py" in _line(brief, "Named paths:")
