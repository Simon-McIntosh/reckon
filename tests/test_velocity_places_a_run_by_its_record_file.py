"""A long window's ledger search stays inside its git bound.

The velocity view places a run that has no promote commit by searching the
committed ledger for the run id's first appearance. That search is a
patch-generating ``git log -p -G`` over the ledger path, so a window reaching
back months asks git for the patch of every commit that touched the ledger in
one call and overruns the runner's 180-second bound. Two mechanisms keep it
inside the bound:

* a run whose own record file, ``docs/state/<project>/runs/<run_id>.json``, is
  added in the range is placed at the adding commit by a path-limited listing
  that generates no patch, so it never reaches the search;
* the remaining ids are searched a slice of ledger-touching commits at a time,
  the cache stored after each slice so a long window is walked across slices
  and an interrupted read resumes where it stopped.

This module synthesises a repository under ``tmp_path`` with several ledger
commits, records every argv the module's git runner receives, and asserts the
three properties §2 names. It points ``RECKON_VELOCITY_CACHE`` and
``RECKON_CLONE_CACHE`` into the temporary directory and asserts the real cache
root is left untouched.
"""

from __future__ import annotations

import datetime as dt
import hashlib
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
RUNS_PATH = "docs/state/sample/runs"

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a profile command outside the
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


def _row(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "node": "node",
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


def _ledger(run_ids: list[str]) -> str:
    # Each run row is indented onto its own lines so the search sees one
    # ``"run_id"`` per diff line, as a ledger written with indentation would be.
    return json.dumps({"data": {"runs": [_row(rid) for rid in run_ids]}}, indent=2)


def _record_file(run_id: str) -> str:
    return json.dumps({"data": _row(run_id)}, indent=2)


def _build_repository(root: Path) -> dict[str, str]:
    """A repository whose ledger grows commit by commit, then a per-run file.

    The ledger is absent at the root, added empty at ``root_ledger``, gains a
    row at ``alpha`` and another at ``beta``, and a per-run record file is
    added at ``record``.
    """
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    root_sha = _commit(repo, "chore: root", 0, {"src/a.py": "l1\n"})
    added = _commit(repo, "feat: add the ledger", 1, {LEDGER_PATH: _ledger([])})
    alpha = _commit(
        repo, "feat: record r-alpha", 2, {LEDGER_PATH: _ledger(["r-alpha"])}
    )
    beta = _commit(
        repo,
        "feat: record r-beta",
        3,
        {LEDGER_PATH: _ledger(["r-alpha", "r-beta"])},
    )
    record = _commit(
        repo,
        "feat: split out a per-run record",
        4,
        {RUNS_PATH + "/r-record.json": _record_file("r-record")},
    )
    return {
        "repo": repo,
        "root": root_sha,
        "added": added,
        "alpha": alpha,
        "beta": beta,
        "record": record,
    }


def _snapshot(base: str, head: str, run_ids: list[str]) -> dict:
    return {
        "window": [_iso(3), _iso(3, 100)],
        "projects": [
            {
                "project": PROJECT,
                "base": base,
                "head": head,
                "runs": [
                    {
                        "run_id": rid,
                        "completed_at": _iso(3, 50),
                        "promotion_commits": [],
                    }
                    for rid in run_ids
                ],
            }
        ],
    }


def _is_ledger_search(args: tuple) -> bool:
    return "-p" in args and "-G" in args and LEDGER_PATH in args


def _search_range(args: tuple) -> str | None:
    for argument in args:
        if isinstance(argument, str) and ".." in argument:
            return argument
    return None


def _entries(root: Path) -> list[str] | None:
    try:
        return sorted(path.name for path in root.iterdir())
    except OSError:
        return None


def _real_velocity_cache_root() -> Path:
    saved = os.environ.pop("RECKON_VELOCITY_CACHE", None)
    try:
        return velocity.velocity_cache_root()
    finally:
        if saved is not None:
            os.environ["RECKON_VELOCITY_CACHE"] = saved


@pytest.fixture()
def caches(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("RECKON_CLONE_CACHE", str(tmp_path / "clones"))
    return tmp_path


@pytest.fixture()
def argv(monkeypatch) -> list[tuple]:
    """Record every argv the module's git runner receives and pass it through."""
    seen: list[tuple] = []
    original = velocity.run_git

    def recorder(repo, *args, input: bytes | None = None):
        seen.append((str(repo), *args))
        return original(repo, *args, input=input)

    monkeypatch.setattr(velocity, "run_git", recorder)
    return seen


def _assert_real_cache_untouched(tmp_path: Path, repo: Path, before) -> None:
    real_root = _real_velocity_cache_root()
    assert _entries(real_root) == before, "the real velocity cache root was written"
    digest = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
    assert not (real_root / (digest + ".json")).exists()
    assert not (real_root / (digest + ".ledger.json")).exists()
    assert velocity.velocity_cache_root() == tmp_path / "cache"


def test_a_run_with_a_record_file_is_placed_without_the_ledger_search(
    tmp_path: Path, caches: Path, argv: list[tuple]
):
    fixture = _build_repository(tmp_path)
    repo, base, head = fixture["repo"], fixture["root"], fixture["record"]
    real_before = _entries(_real_velocity_cache_root())

    snapshot = _snapshot(base, head, ["r-record"])
    velocity.recover_ledger_clocks(
        snapshot, repos={PROJECT: repo}, start=_iso(3), end=_iso(3, 100)
    )

    placed = snapshot["projects"][0]["runs"][0]["promotion_commits"]
    assert [item["sha"] for item in placed] == [fixture["record"]]
    assert placed[0]["source"] == "first_primary_record_file_addition_upper_bound"
    # The run was placed by the record-file listing alone: no patch-generating
    # search over the ledger ran for it.
    assert not [call for call in argv if _is_ledger_search(call[1:])]
    _assert_real_cache_untouched(tmp_path, repo, real_before)


def test_a_run_without_a_record_file_is_placed_by_the_sliced_search(
    tmp_path: Path, caches: Path, argv: list[tuple], monkeypatch
):
    fixture = _build_repository(tmp_path)
    repo, base, head = fixture["repo"], fixture["root"], fixture["record"]
    monkeypatch.setattr(velocity, "_LEDGER_SEARCH_SLICE_COMMITS", 1)
    real_before = _entries(_real_velocity_cache_root())

    snapshot = _snapshot(base, head, ["r-beta"])
    velocity.recover_ledger_clocks(
        snapshot, repos={PROJECT: repo}, start=_iso(3), end=_iso(3, 100)
    )
    placed = snapshot["projects"][0]["runs"][0]["promotion_commits"]
    assert [item["sha"] for item in placed] == [fixture["beta"]]
    assert placed[0]["source"] == "first_primary_ledger_appearance_upper_bound"
    # With one commit per slice the window takes several slices, so the search
    # plainly ran in slices rather than as one call over the whole range.
    searches = [call for call in argv if _is_ledger_search(call[1:])]
    assert len(searches) >= 3
    _assert_real_cache_untouched(tmp_path, repo, real_before)


def test_an_interrupted_read_resumes_at_the_slice_it_reached(
    tmp_path: Path, caches: Path, monkeypatch
):
    fixture = _build_repository(tmp_path)
    repo, base, head = fixture["repo"], fixture["root"], fixture["record"]
    monkeypatch.setattr(velocity, "_LEDGER_SEARCH_SLICE_COMMITS", 1)

    seen: list[tuple] = []
    original = velocity.run_git
    state = {"armed": True, "count": 0}

    def interrupting(repo_path, *args, input: bytes | None = None):
        seen.append((str(repo_path), *args))
        if _is_ledger_search(args):
            state["count"] += 1
            # Interrupt the second slice of the first read only, so the resumed
            # read runs uninterrupted.
            if state["armed"] and state["count"] == 2:
                state["armed"] = False
                raise subprocess.TimeoutExpired(["git", "log"], 180)
        return original(repo_path, *args, input=input)

    monkeypatch.setattr(velocity, "run_git", interrupting)

    first = _snapshot(base, head, ["r-alpha", "r-beta"])
    with pytest.raises(subprocess.TimeoutExpired):
        velocity.recover_ledger_clocks(
            first, repos={PROJECT: repo}, start=_iso(3), end=_iso(3, 100)
        )
    # The interrupted read stored the cache at the slice it completed, so the
    # first slice's last commit is the slice boundary now on disk.
    assert [call for call in seen if _is_ledger_search(call[1:])]

    seen.clear()
    resumed = _snapshot(base, head, ["r-alpha", "r-beta"])
    velocity.recover_ledger_clocks(
        resumed, repos={PROJECT: repo}, start=_iso(3), end=_iso(3, 100)
    )
    placed = {
        run["run_id"]: run["promotion_commits"]
        for run in resumed["projects"][0]["runs"]
    }
    assert [item["sha"] for item in placed["r-alpha"]] == [fixture["alpha"]]
    assert [item["sha"] for item in placed["r-beta"]] == [fixture["beta"]]

    resumed_ranges = [
        _search_range(call[1:]) for call in seen if _is_ledger_search(call[1:])
    ]
    # The resumed read searched only from the first slice's last commit onward,
    # so its first range begins there and no searched range begins at the window
    # base (which the first slice's search covered before the interruption).
    first_slice_last = fixture["added"]
    assert resumed_ranges and all(rng is not None for rng in resumed_ranges)
    assert resumed_ranges[0].split("..")[0] and resumed_ranges[0].startswith(
        first_slice_last + ".."
    )
    assert all(rng.split("..")[0] != base for rng in resumed_ranges)

    # A third, uninterrupted read of the completed window issues no search.
    seen.clear()
    third = _snapshot(base, head, ["r-alpha", "r-beta"])
    velocity.recover_ledger_clocks(
        third, repos={PROJECT: repo}, start=_iso(3), end=_iso(3, 100)
    )
    assert not [call for call in seen if _is_ledger_search(call[1:])]
    assert sorted(third["projects"][0]["ledger_clock_recoveries"]) == [
        "r-alpha",
        "r-beta",
    ]
