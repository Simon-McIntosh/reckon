"""The live view honours the fields it was asked for.

A ``crew`` read accepts a ``fields`` list. This module asserts the three
behaviours that list has to deliver: a ``live`` read narrows each row to the
requested fields plus the run id, a ``runs`` read accepts the three fields a
coordinator checks first and draws them from the same classification the live
view computes, and an unknown field is still refused with the accepted set
named. The live projection is load-bearing: with it removed, the first test
below returns whole classifications and fails.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from reckon import crew, mcp

PROJECT = "alpha"

# The three fields the compact read model now accepts and the live view already
# computes, so a coordinator can read them from either surface.
COORDINATOR_FIELDS = (
    "log_age_seconds",
    "commits_beyond_base",
    "manifest_reported_status",
)

PLAN_SLUG = "the-mcp-answers-within-its-ceiling"


def _git(args: list[str], cwd: Path) -> str:
    """Run one git command in ``cwd`` and return its stdout."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _git_worktree(path: Path) -> str:
    """Create a one-commit worktree ahead of its recorded base.

    Returns the base commit, which deliberately predates HEAD so the run's
    ``base_sha`` makes ``commits_beyond_base`` a real count rather than a
    default zero.
    """
    path.mkdir(parents=True)
    _git(["init", "-q"], path)
    (path / "work.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "work.txt"], path)
    _git(["commit", "-q", "-m", "base"], path)
    base = _git(["rev-parse", "HEAD"], path)
    (path / "more.txt").write_text("beyond\n", encoding="utf-8")
    _git(["add", "more.txt"], path)
    _git(["commit", "-q", "-m", "beyond base"], path)
    return base


def _write_run(repository: Path, *, index: int, worktree: Path, base_sha: str) -> dict:
    """Materialise one live run: pointer, manifest, and a current log file."""
    run_id = f"run-{index}"
    log = repository / f"{run_id}.log"
    log.write_text("working\n", encoding="utf-8")
    manifest = repository / f"{run_id}-manifest.md"
    manifest.write_text(
        f"node: node-{index}\nstatus: in-progress\n\nbody\n", encoding="utf-8"
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "node": {"id": f"node-{index}", "plan": PLAN_SLUG},
        "phase": "working",
        "process_alive": False,
        "worktree": str(worktree),
        "base_sha": base_sha,
        "session": f"session-{run_id}",
        "log_path": str(log),
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _two_live_runs(tmp_path: Path) -> tuple[Path, list[dict]]:
    """Write two live runs into the isolated config home, returning them."""
    repository = tmp_path / "alpha-repository"
    repository.mkdir()
    records = []
    for index in (1, 2):
        worktree = tmp_path / f"worktree-{index}"
        base_sha = _git_worktree(worktree)
        records.append(
            _write_run(repository, index=index, worktree=worktree, base_sha=base_sha)
        )
    return repository, records


def test_live_view_returns_exactly_the_requested_fields(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """A narrowed live read carries the requested fields plus the run id."""
    repository, _records = _two_live_runs(tmp_path)

    result = mcp._crew(
        PROJECT,
        view="live",
        checkout_path=str(repository),
        fields=["classification", "node"],
    )

    assert result["ok"]
    assert len(result["runs"]) == 2
    for row in result["runs"]:
        assert set(row) == {"run_id", "classification", "node"}

    # A live row carries fields the compact runs vocabulary has no name for,
    # and those are requestable too.
    classifier_fields = mcp._crew(
        PROJECT,
        view="live",
        checkout_path=str(repository),
        fields=["phase", "process_alive"],
    )
    for row in classifier_fields["runs"]:
        assert set(row) == {"run_id", "phase", "process_alive"}

    # With no fields requested the whole classification is still served, so a
    # caller that never narrows keeps what it always received.
    whole = mcp._crew(PROJECT, view="live", checkout_path=str(repository))
    for row in whole["runs"]:
        assert "log_age_seconds" in row and "manifest_reported_status" in row


def test_live_view_keeps_mine_when_a_session_is_given(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """A session-scoped narrowed read still says which rows are the caller's."""
    repository, _records = _two_live_runs(tmp_path)

    result = mcp._crew(
        PROJECT,
        view="live",
        checkout_path=str(repository),
        session="session-run-1",
        fields=["node"],
    )

    assert result["ok"]
    rows = {row["run_id"]: row for row in result["runs"]}
    assert set(rows) == {"run-1", "run-2"}
    for row in rows.values():
        assert set(row) == {"run_id", "node", "mine"}
    assert rows["run-1"]["mine"] is True
    assert rows["run-2"]["mine"] is False


def test_live_view_refuses_an_unknown_field_naming_the_live_set(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    repository, _records = _two_live_runs(tmp_path)

    result = mcp._crew(
        PROJECT, view="live", checkout_path=str(repository), fields=["not_a_field"]
    )

    assert result["ok"] is False
    detail = str(result["detail"])
    assert "unknown live fields" in detail
    assert "not_a_field" in detail
    # The refusal names the live set, so a caller can correct the request.
    for accepted in ("phase", "process_alive", "next_action", "classification"):
        assert accepted in detail


def test_runs_view_passes_through_the_coordinator_fields(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """The three fields are populated from the live classification."""
    repository, records = _two_live_runs(tmp_path)
    expected = {
        str(record["run_id"]): crew.classify_pointer(record) for record in records
    }

    result = mcp._crew(
        PROJECT,
        view="runs",
        source="live",
        checkout_path=str(repository),
        fields=list(COORDINATOR_FIELDS),
    )

    assert result["ok"]
    rows = {row["run_id"]: row for row in result["rows"]}
    assert set(rows) == set(expected)
    for run_id, row in rows.items():
        source = expected[run_id]
        for field in COORDINATOR_FIELDS:
            assert row[field] == source[field], (run_id, field)
        assert row["manifest_reported_status"] == "in-progress"
        assert row["commits_beyond_base"] == 1
        assert isinstance(row["log_age_seconds"], int)
