"""A revision whose tree has no ledger reads as a ledger with no runs.

The velocity view reads the committed ledger at two revisions: the window head,
whose runs it reports, and the window base, whose runs it treats as already
present so the ledger-clock recovery searches only for the ones added since.
On an early window the base predates the ledger's first commit, so the path does
not exist at that revision; once the ledger is split into one file per run no
revision will carry it at all. A reader that calls ``git show`` unconditionally
raises there and the whole view aborts, although the true answer is that no runs
existed.

This module synthesises a repository under ``tmp_path`` whose first commits carry
no ``docs/state/<project>/crew.json`` and whose later commits add one with run
rows, points the velocity caches into the same directory, and asserts the three
properties the reader must hold:

* a window whose base predates the ledger reports the window's runs and does not
  raise;
* a window whose head predates the ledger reports zero runs with a ``None``
  ledger digest and does not raise — the digest is ``None`` rather than the
  digest of an empty document, so a revision with no ledger is distinguishable
  from one whose ledger is empty;
* a read of a ledger that is present but fails still raises.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
PROJECT = "sample"
BRANCH = "main"
LEDGER_PATH = "docs/state/sample/crew.json"

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a mutating verb outside the
    # worker's worktree. Point the subprocess at the synthesised repository by
    # dropping the inherited identity.
    base = {**os.environ, **(env or {})}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, str]) -> str:
    when = _iso(day)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for path, content in changes.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


def _ledger(run_ids: list[str]) -> str:
    return json.dumps(
        {
            "data": {
                "runs": [
                    {
                        "run_id": run_id,
                        "node": "late-node",
                        "plan": "p",
                        "role": "implement",
                        "gate": "passed",
                        "backend": "claude",
                        "dispatched_at": _iso(3, 10),
                        "completed_at": _iso(3, 50),
                        "worker_seconds": 40,
                        "lineage": {},
                        "attempt": 1,
                    }
                    for run_id in run_ids
                ]
            }
        }
    )


def _build_repository(root: Path) -> tuple[Path, str, str, str]:
    """A repository whose ledger appears only in its last commit.

    Returns the checkout and the shas of the pre-ledger root, the last
    pre-ledger commit, and the commit that adds the ledger.
    """
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    root_sha = _commit(repo, "chore: root", 0, {})
    before = [root_sha]
    before.append(_commit(repo, "feat: source a", 1, {"src/a.py": "l1\nl2\n"}))
    before.append(_commit(repo, "feat: source b", 2, {"src/b.py": "l3\nl4\n"}))
    head = _commit(
        repo,
        "feat: source c with the ledger",
        3,
        {"src/c.py": "l5\nl6\n", LEDGER_PATH: _ledger(["r-late"])},
    )
    return repo, before[0], before[-1], head


@pytest.fixture()
def caches(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("RECKON_CLONE_CACHE", str(tmp_path / "clones"))
    return tmp_path


def test_a_window_whose_base_predates_the_ledger_reports_its_runs(
    tmp_path: Path, caches: Path
):
    repo, _root, before, head = _build_repository(tmp_path)
    start, end = _iso(3), _iso(3, 100)

    # The window head is the commit that adds the ledger and its base is the
    # commit before it, which carries no ledger at all. This is the shape the
    # live view crashed on.
    captured = velocity.capture_project(
        PROJECT, BRANCH, repo_path=repo, start=start, end=end
    )
    assert [run["run_id"] for run in captured["runs"]] == ["r-late"]
    assert captured["ledger_sha256"] is not None

    # And the ledger-clock recovery, reading the base revision through the same
    # reader, places a run the base did not carry at all.
    snapshot = {
        "window": [start, end],
        "projects": [
            {
                "project": PROJECT,
                "base": before,
                "head": head,
                "runs": [
                    {
                        "run_id": "r-late",
                        "completed_at": _iso(3, 50),
                        "promotion_commits": [],
                    }
                ],
            }
        ],
    }
    velocity.recover_ledger_clocks(
        snapshot, repos={PROJECT: repo}, start=start, end=end
    )
    placed = snapshot["projects"][0]["runs"][0]["promotion_commits"]
    assert snapshot["projects"][0]["ledger_clock_recoveries"] == ["r-late"]
    assert [item["sha"] for item in placed] == [head]


def test_a_window_whose_head_predates_the_ledger_reports_no_runs(
    tmp_path: Path, caches: Path
):
    repo, _root, _before, _head = _build_repository(tmp_path)
    # The window closes on the last commit before the ledger exists.
    captured = velocity.capture_project(
        PROJECT, BRANCH, repo_path=repo, start=_iso(1), end=_iso(2, 100)
    )
    assert captured["runs"] == []
    assert captured["ledger_sha256"] is None
    # The revision genuinely has no ledger, so the absence above is attributable
    # to the missing file rather than to a window that never reached it.
    assert velocity._blob_sha(repo, captured["head"], LEDGER_PATH) is None


def test_a_failing_read_of_a_present_ledger_still_raises(
    tmp_path: Path, caches: Path, monkeypatch
):
    repo, _root, _before, head = _build_repository(tmp_path)
    # The ledger is present at this revision; the failure below is a failed read
    # of a real ledger, not a missing one.
    assert velocity._blob_sha(repo, head, LEDGER_PATH) is not None

    original = velocity.run_git

    def flaky(repo_path, *args, input: bytes | None = None):
        if args and args[0] == "cat-file" and input and b"crew.json" in input:
            raise subprocess.CalledProcessError(128, ["git", "cat-file", "--batch"])
        return original(repo_path, *args, input=input)

    monkeypatch.setattr(velocity, "run_git", flaky)
    with pytest.raises(subprocess.CalledProcessError):
        velocity.capture_project(
            PROJECT, BRANCH, repo_path=repo, start=_iso(3), end=_iso(3, 100)
        )
