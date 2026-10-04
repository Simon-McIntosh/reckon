"""check-manifest reads its commitless-role set from promotion's own list.

The audit a worker runs against its delivered manifest and the gate promotion
applies hours later must agree about which roles promote with no commit.
Promotion lists both review and investigate as commitless and promotes a
complete, clean, out-of-repository investigation whose manifest declares
``commits: none``; the audit asked every role but review for a commit, so a
read-only investigation was refused by the very check it is told to run.

Each acceptance here is paired with a refusal the same rule must still apply,
so the waiver is shown to rest on the role set rather than on the manifest
shape: the same manifest is refused for a role whose work a commit records,
and a spelling that is not a citation is refused where a citation is owed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, crew_home, pointer_path

RUN_ID = "r-20261004T122758429270-investigate-out-of-repository"
PROJECT = "nova"
COMMIT_FINDING = "status is complete but no commit is recorded"

MANIFEST = """\
node: verify-{run}
status: complete
commits: {commits}
changed_paths:
  - {deliverable}
tests: focused check passed
"""


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture()
def run_fixture(isolated_reckon_home: Path, tmp_path: Path) -> dict[str, object]:
    """A clean repository and an investigation whose only deliverable is outside it.

    The fixture's precondition is the shape promotion accepts: the worktree is
    clean — asserted here rather than assumed — and the manifest's sole changed
    path lies outside the repository, in the declaration the dispatch granted.
    """
    assert crew_home().is_relative_to(isolated_reckon_home)
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repository, "add", "seed.txt")
    _git(
        repository,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=test",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    head = _git(repository, "rev-parse", "HEAD")
    assert _git(repository, "status", "--porcelain") == ""
    deliverable = tmp_path / "findings" / f"{RUN_ID}.json"
    deliverable.parent.mkdir()
    deliverable.write_text("{}\n", encoding="utf-8")
    return {
        "repository": repository,
        "head": head,
        "deliverable": deliverable,
        "manifest": tmp_path / "manifest.md",
    }


def _check(
    run_fixture: dict[str, object], role: str, commits: str
) -> tuple[int, list[str]]:
    """Dispatch one run with the given role, write its manifest, and check it."""
    repository = run_fixture["repository"]
    deliverable = run_fixture["deliverable"]
    manifest = run_fixture["manifest"]
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": run_fixture["head"],
            "launch": "cli",
            "role": role,
            "manifest_path": str(manifest),
            "node": {
                "id": f"verify-{RUN_ID}",
                "plan": "fixture",
                "section": "s38",
                "role": role,
                "write_paths": [str(deliverable)],
                "manifest_path": str(manifest),
            },
        },
    )
    manifest.write_text(
        MANIFEST.format(run=RUN_ID, commits=commits, deliverable=deliverable)
    )
    result = CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", RUN_ID])
    return result.exit_code, json.loads(result.output)["findings"]


@pytest.mark.parametrize("commits", ["none", "[]"])
def test_an_investigate_manifest_without_commits_reads_clean(
    run_fixture: dict[str, object], commits: str
) -> None:
    exit_code, findings = _check(run_fixture, "investigate", commits)

    assert exit_code == 0, findings
    assert findings == []


def test_an_implement_manifest_without_commits_is_still_refused(
    run_fixture: dict[str, object],
) -> None:
    exit_code, findings = _check(run_fixture, "implement", "none")

    assert exit_code != 0
    assert findings == [COMMIT_FINDING]


def test_a_zero_is_not_a_commit_citation_for_a_role_that_owes_one(
    run_fixture: dict[str, object],
) -> None:
    exit_code, findings = _check(run_fixture, "implement", "0")

    assert exit_code != 0
    assert findings == [COMMIT_FINDING]


def test_an_implement_manifest_citing_a_commit_still_passes(
    run_fixture: dict[str, object],
) -> None:
    """The positive control: a real citation satisfies the role that owes one."""
    exit_code, findings = _check(run_fixture, "implement", str(run_fixture["head"]))

    assert exit_code == 0, findings
    assert findings == []
