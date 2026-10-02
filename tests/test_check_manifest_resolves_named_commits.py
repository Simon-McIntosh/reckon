"""check-manifest resolves every commit citation in the run's repository.

A manifest's ``commits`` field is the pointer a coordinator follows to the
work, and a cited object id that resolves to nothing reads as evidence while
pointing at none. The audit therefore asks the store the run's own trees name
about each commit-id shaped token, and reports by value what the store cannot
find. The population this must not refuse is the honest one: an entry holding
several revisions, an entry carrying a revision beside its subject, and a field
that opens by declaring there is no commit to cite. A store that cannot answer
— a pointer into a tree that is not a repository — leaves the check unarmed
rather than reporting every citation it was never able to ask about.

The negative control removes the resolution check from ``audit_manifest``: the
unresolvable citation then passes silently and the reporting cases here fail.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.reports import audit_manifest
from reckon.crew.runs import _write_json, pointer_path

RUN_ID = "r-20260921T000000000000-unresolvable-citation"


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _repository(tmp_path: Path) -> Path:
    """A synthesised repository holding one commit."""
    root = tmp_path / "run-repository"
    root.mkdir()
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "citation-check@example.invalid")
    _git(root, "config", "user.name", "Citation Check")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _commit(root: Path, name: str, message: str) -> str:
    (root / name).write_text(message + "\n", encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", message)
    return _head(root)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _resolves(root: Path, revision: str) -> bool:
    """The store's own answer, asked the way the audit asks it."""
    probe = _git(
        root,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{revision}^{{commit}}",
    )
    return probe.returncode == 0


def _manifest(commits: str) -> str:
    return (
        "node: resolve-citations\n"
        "status: complete\n"
        f"commits: {commits}\n"
        "changed_paths: []\n"
        "tests: uv run pytest tests/test_check_manifest_resolves_named_commits.py"
        " -q -> passed\n"
        "test_logs: /tmp/citation-check.log\n"
        "artifacts: none\n"
        "evidence_inputs: none\n"
        "follow_ons: none\n"
        "blockers: none\n"
    )


def test_the_unresolvable_citation_is_reported_and_the_resolving_one_is_not(
    tmp_path: Path,
) -> None:
    """One manifest, one resolvable sha and one 40-character sha that is not."""
    root = _repository(tmp_path)
    resolving = _head(root)
    fabricated = "a" * 40

    # The instrument is shown to see something known present before the absence
    # of the fabricated value is believed: the store resolves the real commit
    # and answers nothing for the fabricated one.
    assert _resolves(root, resolving)
    assert not _resolves(root, fabricated)

    audit = audit_manifest(_manifest(f"[{resolving}, {fabricated}]"), repository=root)

    assert len(audit["findings"]) == 1, audit["findings"]
    finding = audit["findings"][0]
    assert fabricated in finding
    assert resolving not in finding
    assert audit["ok"] is False


def test_an_entry_holding_several_revisions_resolves_when_each_resolves(
    tmp_path: Path,
) -> None:
    """The honest population shape: one entry, several space-separated ids."""
    root = _repository(tmp_path)
    first = _head(root)
    second = _commit(root, "second.txt", "second")
    third = _commit(root, "third.txt", "third")

    audit = audit_manifest(_manifest(f"{first} {second} {third}"), repository=root)

    assert audit["ok"] is True, audit["findings"]


def test_a_revision_beside_its_subject_still_resolves(tmp_path: Path) -> None:
    """A citation followed by the subject it was committed under is one id."""
    root = _repository(tmp_path)
    resolving = _head(root)
    cited = f"{resolving} feat(crew): a cited revision is resolved in the store"

    audit = audit_manifest(_manifest(cited), repository=root)

    assert audit["ok"] is True, audit["findings"]


def test_a_field_that_opens_by_declaring_absence_is_left_alone(
    tmp_path: Path,
) -> None:
    """Prose that opens with an absence word is a declaration, not a citation.

    The same hex-shaped value inside an entry that does not declare absence is
    reported, so the suppression is the declaration being read and not the
    token escaping the reader for some other reason.
    """
    root = _repository(tmp_path)
    unresolvable = "1c20ca680a2b35119f625"
    assert not _resolves(root, unresolvable)

    declared = _manifest(
        "none — the store is outside the repository; this analysis cited the "
        f"revision {unresolvable} it read"
    )
    assert audit_manifest(declared, repository=root)["ok"] is True

    cited = _manifest(
        f"see the analysis this run read, which named the revision {unresolvable}"
    )
    audit = audit_manifest(cited, repository=root)
    assert len(audit["findings"]) == 1, audit["findings"]
    assert unresolvable in audit["findings"][0]


def test_a_store_that_cannot_answer_leaves_the_check_unarmed(tmp_path: Path) -> None:
    """Not a repository, and no repository recorded: nothing can be asked."""
    not_a_repository = tmp_path / "plain-tree"
    not_a_repository.mkdir()
    manifest = _manifest("a" * 40)

    assert _git(not_a_repository, "rev-parse", "--git-dir").returncode != 0
    assert audit_manifest(manifest, repository=not_a_repository)["findings"] == []
    assert audit_manifest(manifest)["findings"] == []


def test_the_command_reports_the_unresolvable_citation(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """check-manifest itself, the surface a worker runs before it ends."""
    root = _repository(tmp_path)
    resolving = _head(root)
    fabricated = "b" * 40
    manifest = tmp_path / "manifest.md"
    manifest.write_text(_manifest(f"[{resolving}, {fabricated}]"), encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "sample",
            "repo": str(root),
            "worktree": str(root),
            "base_sha": resolving,
            "launch": "in-harness",
            "role": "implement",
            "node": {
                "id": "resolve-citations",
                "plan": "fixture",
                "section": "s1",
                "write_paths": [],
            },
            "manifest_path": str(manifest),
        },
    )
    # The pointer write stayed in the home the case substituted.
    assert pointer_path(RUN_ID).is_relative_to(isolated_reckon_home)

    result = CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", RUN_ID])

    assert result.exit_code != 0, result.output
    findings = json.loads(result.output)["findings"]
    assert len(findings) == 1, findings
    assert fabricated in findings[0]
    assert resolving not in findings[0]
