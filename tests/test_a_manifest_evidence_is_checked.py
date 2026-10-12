"""A manifest's evidence is checked, not taken on the field's word.

A worker runs ``check-manifest`` before it finishes, so a finding reaches the
party who still holds the pen. Two kinds of evidence are judged here, both of
which were read as present because a field named them: the gate logs a manifest
cites, and the production source a change adds.

A test log is the manifest's evidence of the run that passed. A log that carries
no command near its head, or ends with no ``EXIT=`` record, cannot show what ran
or how it ended, and a log whose command is a description rather than a command
cannot be re-executed — so each is a finding. The command is judged by the
reader promotion already applies to a recorded gate command, so the two surfaces
cannot disagree about what an abbreviation is.

Production source that cites a plan section ties code to a plan that can be
renamed or deleted. Only *added* comment or docstring lines are judged, because
a change is not answerable for a file it inherited, and only comment or
docstring text, because a string literal is data. Test source is exempt, since
a fixture legitimately records a section identifier as such.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.reports import audit_manifest
from tests.helpers.git import git as _git

WELL_FORMED_COMMAND = "pytest -q tests/test_example_check.py"
PRODUCTION_PATH = "reckon/crew/reports.py"


def _node(**overrides: object) -> TaskNode:
    fields: dict[str, object] = {
        "id": "evidence-check",
        "goal": "check the manifest's evidence",
        "plan": "fixture",
        "write_paths": [PRODUCTION_PATH],
    }
    fields.update(overrides)
    return TaskNode(**fields)  # type: ignore[arg-type]


def _manifest(
    *,
    test_logs: list[Path] | None = None,
    base: str | None = None,
    commits: str = "1a2b3c4",
    changed: str = PRODUCTION_PATH,
) -> str:
    payload: dict[str, object] = {
        "node": "evidence-check",
        "status": "complete",
        "commits": commits,
        "changed_paths": changed,
        "tests": f"{WELL_FORMED_COMMAND} -> 1 passed",
        "test_logs": [str(path) for path in (test_logs or [])],
    }
    if base is not None:
        payload["orientation_base_sha"] = base
    return json.dumps(payload, indent=2)


def _findings(
    text: str,
    node: TaskNode | None = None,
    *,
    worktree: Path | None = None,
    repository: Path | None = None,
) -> list[str]:
    return audit_manifest(
        text, node or _node(), worktree=worktree, repository=repository
    )["findings"]


def _well_formed_log(tmp_path: Path) -> Path:
    path = tmp_path / "gate.log"
    path.write_text(
        f"# revision=abc1234 tree={tmp_path} command: {WELL_FORMED_COMMAND}\n"
        "1 passed\n"
        "EXIT=0\n",
        encoding="utf-8",
    )
    return path


# ── The gate logs a manifest cites ──────────────────────────────────────────


def test_a_log_with_no_exit_record_is_a_finding(tmp_path: Path) -> None:
    log = tmp_path / "gate.log"
    log.write_text(f"# command: {WELL_FORMED_COMMAND}\n1 passed\n", encoding="utf-8")

    findings = _findings(_manifest(test_logs=[log]))

    assert len(findings) == 1
    assert "EXIT=<n>" in findings[0]


def test_a_log_whose_command_is_an_abbreviation_is_a_finding(tmp_path: Path) -> None:
    log = tmp_path / "gate.log"
    log.write_text(
        "# command: pytest -q <five tests>\n1 passed\nEXIT=0\n", encoding="utf-8"
    )

    findings = _findings(_manifest(test_logs=[log]))

    assert len(findings) == 1
    assert "angle-bracket placeholder" in findings[0]
    assert "<five tests>" in findings[0]


def test_a_log_naming_no_command_near_its_head_is_a_finding(tmp_path: Path) -> None:
    log = tmp_path / "gate.log"
    log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")

    findings = _findings(_manifest(test_logs=[log]))

    assert len(findings) == 1
    assert "no command near its head" in findings[0]


def test_a_well_formed_log_is_accepted(tmp_path: Path) -> None:
    log = _well_formed_log(tmp_path)

    assert _findings(_manifest(test_logs=[log])) == []


def test_a_log_that_cannot_be_read_is_left_to_the_citation_readers(
    tmp_path: Path,
) -> None:
    assert _findings(_manifest(test_logs=[tmp_path / "absent.log"])) == []


# ── Plan sections in added production source ────────────────────────────────


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "reckon" / "crew").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "reckon" / "crew" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text("y = 1\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "reckon/crew/mod.py", "tests/test_mod.py"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    return root


def _base(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def test_an_added_production_comment_citing_a_section_is_a_finding(
    repo: Path,
) -> None:
    base = _base(repo)
    (repo / "reckon" / "crew" / "mod.py").write_text(
        "x = 1\n# §9: keeps the label out of production source\ny = 2\n",
        encoding="utf-8",
    )

    findings = _findings(
        _manifest(base=base, commits=base),
        worktree=repo,
        repository=repo,
    )

    assert len(findings) == 1
    assert "reckon/crew/mod.py:2" in findings[0]
    assert "§9" in findings[0]


def test_an_added_production_docstring_citing_a_section_is_a_finding(
    repo: Path,
) -> None:
    base = _base(repo)
    (repo / "reckon" / "crew" / "mod.py").write_text(
        '"""a module\nciting §2 here\n"""\nx = 1\n', encoding="utf-8"
    )

    findings = _findings(
        _manifest(base=base, commits=base),
        worktree=repo,
        repository=repo,
    )

    assert len(findings) == 1
    assert "reckon/crew/mod.py:2" in findings[0]


def test_the_label_check_reads_the_worktree_not_the_repository(
    repo: Path,
) -> None:
    """The run's added lines live in its worktree, not the main checkout.

    A dispatch records the repository as the main checkout and the worktree as
    the detached tree the run worked in. The main checkout does not hold the
    run's unmerged commit, so a check that diffed the repository would report
    the checkout's own later changes and never the lines the run added, leaving
    this half of the audit blind to every real run.
    """
    base = _base(repo)
    worktree = repo.parent / "run-worktree"
    _git(repo, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "reckon" / "crew" / "mod.py").write_text(
        "x = 1\n# §9 added in the worktree\n", encoding="utf-8"
    )
    assert "§9" not in (repo / "reckon" / "crew" / "mod.py").read_text(encoding="utf-8")

    findings = _findings(
        _manifest(base=base, commits=base),
        worktree=worktree,
        repository=repo,
    )

    assert len(findings) == 1
    assert "reckon/crew/mod.py:2" in findings[0]


def test_an_added_test_fixture_string_citing_a_section_is_not_a_finding(
    repo: Path,
) -> None:
    base = _base(repo)
    (repo / "tests" / "test_mod.py").write_text('SECTION = "§3"\n', encoding="utf-8")

    findings = _findings(
        _manifest(base=base, commits=base, changed="tests/test_mod.py"),
        _node(write_paths=["tests/test_mod.py"]),
        worktree=repo,
        repository=repo,
    )

    assert not any("§" in finding for finding in findings), findings


def test_a_production_string_literal_citing_a_section_is_not_a_finding(
    repo: Path,
) -> None:
    base = _base(repo)
    (repo / "reckon" / "crew" / "mod.py").write_text(
        'MESSAGE = "see §4"\n', encoding="utf-8"
    )

    findings = _findings(
        _manifest(base=base, commits=base),
        worktree=repo,
        repository=repo,
    )

    assert not any("§" in finding for finding in findings), findings


def test_a_manifest_recording_no_base_leaves_the_label_check_unarmed(
    repo: Path,
) -> None:
    base = _base(repo)
    (repo / "reckon" / "crew" / "mod.py").write_text(
        "x = 1\n# §9\ny = 2\n", encoding="utf-8"
    )

    findings = _findings(
        _manifest(commits=base),
        worktree=repo,
        repository=repo,
    )

    assert not any("§" in finding for finding in findings), findings


# ── A well-formed manifest ──────────────────────────────────────────────────


def test_a_well_formed_manifest_has_no_findings(tmp_path: Path) -> None:
    log = _well_formed_log(tmp_path)

    assert _findings(_manifest(test_logs=[log])) == []
