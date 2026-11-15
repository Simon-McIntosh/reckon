"""A code review's brief carries the findings stored beneath its named paths.

A reviewer reads the brief's finding list to decide whether a claim is one
earlier reviews already raised. The store keys a finding by the file it names,
so a named directory holds no row of its own and a brief that compares paths for
equality carries nothing for it — the defect this test fails on: a
directory-named review reported "none" while the findings view returned 44 open
findings for the same path. A store that cannot be read is a second, quieter
absence: those rows are not known to be absent, so the brief says the store
could not be read rather than reporting a count it never measured.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import findings as findings_module
from reckon.crew import plan_review
from reckon.crew import review as review_module

PROJECT = "sample"

# The findings stored beneath the named directory, as
# ``(path, line, text, severity)``.
BENEATH = (
    ("reckon/sub/parallel.py", "12", "unguarded index", "blocking"),
    ("reckon/sub/parallel.py", "40", "naming would read better", "follow-on"),
    ("reckon/sub/other.py", "7", "swallows the error", "follow-on"),
)
# A finding on a file above the named directory, which is not beneath it.
ABOVE = ("reckon/top.py", "3", "unrelated sibling finding", "blocking")


def _line(brief: str, prefix: str) -> str:
    for line in brief.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"brief carries no {prefix!r} line:\n{brief}")


def _finding_bullets(brief: str) -> list[str]:
    """The finding bullets the brief carries, in order."""
    lines = brief.splitlines()
    bullets: list[str] = []
    found = False
    for line in lines:
        if not found:
            if line.startswith("Open findings"):
                found = True
            continue
        if not line.startswith("- "):
            break
        bullets.append(line)
    assert found, brief
    return bullets


def _store(repo: Path, findings: list[tuple[str, str, str, str]]) -> None:
    review_module.store_committed_review(
        {
            "project": PROJECT,
            "reviewed_run_id": "r-reviewed-a",
            "review_run_id": "r-20261008T090000000000-review-a",
            "reviewed_head_sha": "0" * 40,
            "scores": {"evidence": 17},
            "findings": [
                {"file": path, "line": line, "text": text, "severity": severity}
                for path, line, text, severity in findings
            ],
        },
        root=repo,
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout holding a package and a committed findings store."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    root = tmp_path / "repo"
    package = root / "reckon"
    sub = package / "sub"
    sub.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "top.py").write_text("VALUE = 0\n", encoding="utf-8")
    (sub / "__init__.py").write_text("import reckon.sub.other\n", encoding="utf-8")
    (sub / "other.py").write_text("X = 1\n", encoding="utf-8")
    (sub / "parallel.py").write_text(
        "import reckon.sub.other\n\n\nP = reckon.sub.other.X\n", encoding="utf-8"
    )
    (root / "docs").mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "reckon"],
        ["commit", "-q", "-m", "chore: seed package", "-m", "Supply a package."],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _all_findings() -> list[tuple[str, str, str, str]]:
    return [*BENEATH, ABOVE]


def _brief(repo: Path, *paths: str) -> str:
    subject = plan_review.code_review_subject(
        PROJECT, "coordinator", paths=list(paths), repo=str(repo)
    )
    return plan_review.code_review_brief(subject)


def _open_rows_in_the_view(repo: Path, path: str) -> list[dict]:
    payload = findings_module.findings_by_path(PROJECT, path=path, root=repo)
    return [
        row
        for group in payload["paths"]
        for row in group["rows"]
        if row["disposition"] == "open"
    ]


def test_a_named_directory_carries_every_open_finding_beneath_it(repo: Path) -> None:
    _store(repo, _all_findings())

    brief = _brief(repo, "reckon/sub")

    bullets = _finding_bullets(brief)
    view_rows = _open_rows_in_the_view(repo, "reckon/sub")
    assert len(bullets) == len(view_rows) == len(BENEATH), (bullets, view_rows)
    carried = "\n".join(bullets)
    for _, _, text, _ in BENEATH:
        assert text in carried, brief
    # The finding on the file above the directory is not stored beneath it.
    assert ABOVE[2] not in carried


def test_a_named_file_carries_only_that_files_findings(repo: Path) -> None:
    _store(repo, _all_findings())

    brief = _brief(repo, "reckon/sub/other.py")

    bullets = _finding_bullets(brief)
    assert len(bullets) == 1, bullets
    assert "swallows the error" in bullets[0]
    assert "naming would read better" not in brief
    assert ABOVE[2] not in brief


def test_an_unreadable_store_says_so_rather_than_none(repo: Path) -> None:
    # The committed reviews tree is a file where a directory is expected, so the
    # store exists and cannot be read: no row was measured, and "none" would
    # report an absence the reader never saw.
    committed = review_module.committed_review_root(PROJECT, root=repo)
    assert committed is not None
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text("not a store\n", encoding="utf-8")

    brief = _brief(repo, "reckon/sub")

    line = _line(brief, "Open findings")
    assert "could not be read" in line, brief
    assert "none" not in line
