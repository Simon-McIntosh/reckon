"""A recorded row that cites the wrong commit is corrected from its merge.

A landing pass once recorded a delivery the primary branch already carried
against the branch tip at the time of the pass rather than against the run's
head, so a ledger row can name an unrelated promotion. That row cannot be
appended again and no verb replaced it, so the wrong citation stayed wrong and
every reader of the run's reviewed head read the wrong revision.

These cases build one throwaway repository whose primary branch carries a
``--no-ff`` merge of the run's head, seed a ledger row citing an unrelated
commit, and run ``crew record-merge`` over it. They assert the four behaviours
the correction owes: a wrong citation is replaced by the head the merge carried
with the merge recorded and the original kept as superseded; the same works for
a run with no live pointer and no run directory; a row already citing the head
is left alone and writes no superseded record; and a commit that carries no
head is refused with the row untouched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout

RUN_ID = "r-20261121T201951227166-anchored-module-s18"
REASON = "the recorded citation names another run's promotion, not this run's head"


def _write_row(repo: Path, cited: str, **overrides) -> dict:
    row = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "node": "anchored-module-s18",
        "plan": "plan-a",
        "section": "section-four",
        "gate": "passed",
        "completed_at": "2026-11-23T17:23:33Z",
        "commits": [cited],
        "promoted_revision": cited,
    }
    row.update(overrides)
    path = ledger.run_path(PROJECT, RUN_ID, root=repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2) + "\n")
    return row


def _row_path(repo: Path) -> Path:
    return ledger.run_path(PROJECT, RUN_ID, root=repo)


def _commit_count(repo: Path) -> int:
    done = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return int(done.stdout.strip())


def _record_merge(repo: Path, merge_sha: str, *, project: str | None = PROJECT):
    arguments = [
        "crew",
        "record-merge",
        "--run",
        RUN_ID,
        "--merge",
        merge_sha,
        "--reason",
        REASON,
        "--checkout-path",
        str(repo),
    ]
    if project:
        arguments.extend(["--project", project])
    return CliRunner().invoke(cli_main, arguments)


@pytest.fixture()
def cited_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A checkout whose primary branch merged a run head over a wrong citation.

    The primary branch carries an unrelated promotion first, then a ``--no-ff``
    merge of the run's own commit, so the merge's first parent is the branch
    tip the row wrongly cites and its second is the head the run delivered.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    _origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    (repository / "promotion.txt").write_text("an unrelated plan review\n")
    _git(repository, "add", "promotion.txt")
    _git(repository, "commit", "-q", "-m", "promote an unrelated plan review")
    cited = _git(repository, "rev-parse", "HEAD")

    _git(repository, "checkout", "-q", "-b", "run-branch")
    (repository / "reviewed.txt").write_text("the reviewed change\n")
    _git(repository, "add", "reviewed.txt")
    _git(repository, "commit", "-q", "-m", "test(anchored-edit): the reviewed change")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "main")
    _git(
        repository,
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        f"land({RUN_ID}): merge the reviewed head onto the primary branch",
        "run-branch",
    )
    merge_sha = _git(repository, "rev-parse", "HEAD")
    return {
        "repository": repository,
        "cited": cited,
        "head": head,
        "merge": merge_sha,
        "config_home": config_home,
    }


def test_a_row_citing_another_commit_is_corrected_to_the_head_its_merge_carried(
    cited_repo: dict,
) -> None:
    """The corrected row names the head; the citation it replaced survives."""
    run = cited_repo
    repository = run["repository"]
    _write_row(repository, run["cited"])
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "created_at": "2026-11-21T20:19:51Z",
        "node": {"id": "anchored-module-s18", "plan": "plan-a"},
    }
    _write_json(pointer_path(RUN_ID), pointer)
    commits_before = _commit_count(repository)

    result = _record_merge(repository, run["merge"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["corrected"] is True, payload
    assert payload["head"] == run["head"]
    assert payload["recorded_merge"] == run["merge"]
    row = json.loads(_row_path(repository).read_text(encoding="utf-8"))
    assert row["commits"] == [run["head"]]
    assert row["promoted_revision"] == run["head"]
    assert row["recorded_merge"] == run["merge"]
    superseded = row["citation_superseded"]
    assert superseded["commits"] == [run["cited"]]
    assert superseded["promoted_revision"] == run["cited"]
    assert superseded["corrected_head"] == run["head"]
    assert superseded["recorded_merge"] == run["merge"]
    assert superseded["reason"] == REASON
    assert superseded["time"]
    assert _commit_count(repository) == commits_before + 1
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout.decode()
    assert f"record-merge({RUN_ID})" in subject


def test_a_row_with_no_pointer_and_no_run_directory_is_resolved_from_its_row(
    cited_repo: dict,
    tmp_path: Path,
) -> None:
    """The stored row alone reaches the correction, with no --project named."""
    run = cited_repo
    repository = run["repository"]
    _write_row(repository, run["cited"])
    assert not pointer_path(RUN_ID).exists()
    run_directory = run["config_home"] / "crew" / "runs" / RUN_ID
    assert not run_directory.exists()

    result = _record_merge(repository, run["merge"], project=None)

    assert result.exit_code == 0, result.output
    row = json.loads(_row_path(repository).read_text(encoding="utf-8"))
    assert row["promoted_revision"] == run["head"]
    assert row["recorded_merge"] == run["merge"]


def test_a_row_already_citing_the_head_is_left_unchanged(cited_repo: dict) -> None:
    """A correct row writes no superseded record and lands no commit."""
    run = cited_repo
    repository = run["repository"]
    _write_row(repository, run["head"])
    before = _row_path(repository).read_bytes()
    commits_before = _commit_count(repository)

    result = _record_merge(repository, run["merge"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["corrected"] is False, payload
    assert _row_path(repository).read_bytes() == before
    assert "citation_superseded" not in json.loads(before)
    assert _commit_count(repository) == commits_before


def test_a_commit_that_carries_no_head_is_refused_with_the_row_untouched(
    cited_repo: dict,
) -> None:
    """A commit with no second parent names no head, so nothing is corrected."""
    run = cited_repo
    repository = run["repository"]
    _write_row(repository, run["cited"])
    before = _row_path(repository).read_bytes()
    commits_before = _commit_count(repository)

    result = _record_merge(repository, run["cited"])

    assert result.exit_code != 0, result.output
    assert "second parent" in result.output
    assert _row_path(repository).read_bytes() == before
    assert _commit_count(repository) == commits_before
