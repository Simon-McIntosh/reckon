"""One holder scan, one lock-age function, and every clearing named with its age.

A landing may clear an ``index.lock`` it has proven stale, and the check that
proves it was written beside the landing rather than on top of what the package
already had: a second process-holder scan with different matching, and a second
copy of one lock's age. One lock has one age and one holder scan, and neither is
the landing's to own.

The other half is the record. The wait that clears a lock is shared by several
callers, and only the landing passed the callback that names the removal, so a
lock in a shared checkout could vanish under a promotion, a rollback or a state
write with nothing written down. Each caller is driven through a proven-stale
lock here, and each must leave one line naming the lock and the age it stood for
in the record its own caller reads.

Whether a lock is stale at all is not re-tested here:
``tests/test_a_lander_clears_a_stale_index_lock.py`` owns the four cases that
decide it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import gc, merge, promotion_scope

REPO_ROOT = Path(__file__).resolve().parents[1]

# The lock's age is past the ten-minute bound the judgement applies, so a case
# that means to reach the stale branch does not depend on where the bound is.
ELEVEN_MINUTES = 660.0
AGE_NAMED = re.compile(r"after 6[0-9]\ds")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "author@example.invalid")
    _git(repo, "config", "user.name", "Author")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "-M", "main")
    return repo


def _init_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout with a bare origin its landing can push to."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = _init_repo(tmp_path)
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def _reviewed_head(work: Path) -> str:
    """Commit one file on a side branch, leave main checked out, return the head."""
    _git(work, "checkout", "-q", "-b", "reviewed-feature")
    (work / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work, "add", "feature.txt")
    _git(work, "commit", "-q", "-m", "add feature")
    _git(work, "checkout", "-q", "main")
    return _git(work, "rev-parse", "reviewed-feature")


def _aged_lock(repo: Path, age_seconds: float = ELEVEN_MINUTES) -> Path:
    """Write an empty ``.git/index.lock`` and set its mtime into the past."""
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(lock, (stamp, stamp))
    return lock


def _landing_record_entries(run_id: str) -> list[dict]:
    path = promotion_scope._landing_record_path(run_id)
    assert path.is_file(), f"the run's landing record was not written: {path}"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, list), loaded
    return loaded


def _removal_entries(run_id: str) -> list[dict]:
    entries = [
        entry
        for entry in _landing_record_entries(run_id)
        if entry.get("step") == "stale_index_lock_removed"
    ]
    assert len(entries) == 1, entries
    return entries


def _assert_names_lock_and_age(line: str, lock: Path) -> None:
    assert str(lock) in line, line
    assert AGE_NAMED.search(line), f"the line names the age it stood for: {line}"


@pytest.fixture
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every run-scoped record at a temporary home, never the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture
def fast_lock_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend a few attempts, not a landing's whole bound, to reach the judgement."""
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(promotion_scope, "_INDEX_LOCK_RETRY_INTERVAL_SECONDS", 0.01)


def test_the_package_holds_one_holder_scan_and_one_lock_age_function(
    tmp_path: Path,
) -> None:
    """``git grep`` finds one definition of each, and the landing asks that one.

    A second scan was the defect: two answers to "which processes hold this
    path", from matching rules that could disagree while both looked right.
    """
    defs = {
        "^def _processes_holding": "reckon/crew/gc.py",
        "^def _index_lock_age_seconds": "reckon/crew/merge.py",
    }
    for pattern, owner in defs.items():
        result = subprocess.run(
            ["git", "grep", "-n", "-E", pattern, "--", "reckon"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        hits = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(hits) == 1, f"one definition of {pattern}, found {hits}"
        assert hits[0].startswith(f"{owner}:"), hits[0]

    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    assert merge._holders_of(lock) == []
    assert merge._processes_holding is gc._processes_holding, (
        "the stale-lock judgement must ask the one holder scan, not a copy"
    )


def test_a_landing_names_the_lock_it_cleared_in_its_report(tmp_path: Path) -> None:
    """The landing's own report carries one line naming the lock and its age."""
    origin, work = _init_checkout(tmp_path)
    head = _reviewed_head(work)
    lock = _aged_lock(work)

    report = merge.land_merge(
        work,
        head=head,
        message="Land the reviewed feature",
        body="the feature is reviewed and its suite passes",
        attempts=3,
        retry_seconds=0.01,
    )

    assert not lock.exists(), "a provably stale lock is cleared"
    assert _git(origin, "rev-parse", "main") == report["commit"]
    removals = report["stale_index_lock_removed"]
    assert len(removals) == 1, removals
    _assert_names_lock_and_age(removals[0], lock)


def test_restoring_a_staged_copy_names_the_lock_it_cleared(tmp_path: Path) -> None:
    """A landing that unstages a peer's copy hands the removal to its own record."""
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("peer's staged copy\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    lock = _aged_lock(repo)
    removed: list[str] = []

    restored = merge.clear_stale_landed_copies(
        repo,
        ["tracked.txt"],
        on_stale_removed=lambda lock, age: removed.append(
            merge.stale_index_lock_removal_line(lock, age)
        ),
    )

    assert restored == ["tracked.txt"]
    assert not lock.exists()
    assert len(removed) == 1, removed
    _assert_names_lock_and_age(removed[0], lock)


def test_staging_a_landing_names_the_lock_it_cleared(
    tmp_path: Path, crew_home: Path, fast_lock_wait: None
) -> None:
    """The staging wait clears a stale lock and appends one line to the run's record."""
    repo = _init_repo(tmp_path)
    target = repo / "landed.txt"
    target.write_text("landed\n", encoding="utf-8")
    lock = _aged_lock(repo)

    staged = promotion_scope._stage_landing_writes("run-staging", repo, [target])

    assert staged.returncode == 0, staged.stderr
    assert not lock.exists()
    entry = _removal_entries("run-staging")[0]
    assert entry["lock"] == str(lock)
    assert 600 <= entry["age_seconds"] < 700, entry
    _assert_names_lock_and_age(entry["message"], lock)


def test_a_landing_commit_names_the_lock_it_cleared(
    tmp_path: Path, crew_home: Path, fast_lock_wait: None
) -> None:
    """The commit wait clears a stale lock and appends one line to the run's record."""
    repo = _init_repo(tmp_path)
    target = repo / "landed.txt"
    target.write_text("landed\n", encoding="utf-8")
    lock = _aged_lock(repo)

    result = promotion_scope._commit_landing_writes(
        run_id="run-commit",
        verdict="delivered",
        checkout=repo,
        paths=[target],
    )

    assert result["committed"] is True, result
    assert not lock.exists()
    entry = _removal_entries("run-commit")[0]
    assert entry["lock"] == str(lock)
    _assert_names_lock_and_age(entry["message"], lock)


def test_a_landing_commit_names_the_lock_its_rollback_cleared(
    tmp_path: Path, crew_home: Path, fast_lock_wait: None
) -> None:
    """A refused commit's rollback clears the lock, and the clearing is recorded."""
    repo = _init_repo(tmp_path)
    target = repo / "tracked.txt"
    target.write_text("staged by a refused landing\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    lock = _aged_lock(repo)

    report = promotion_scope._restore_landing_writes(
        repo,
        [target],
        on_stale_removed=promotion_scope._landing_record_stale_removal("run-rollback"),
    )

    assert report[str(target)] is True, report
    assert not lock.exists()
    entry = _removal_entries("run-rollback")[0]
    assert entry["lock"] == str(lock)
    _assert_names_lock_and_age(entry["message"], lock)


def test_the_plan_text_commit_names_the_lock_it_cleared(
    tmp_path: Path,
    crew_home: Path,
    fast_lock_wait: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private-projection commit writes the shared index, so its wait records too."""
    repo = _init_repo(tmp_path)
    plan = repo / "plan.html"
    plan.write_text("committed plan\n", encoding="utf-8")
    _git(repo, "add", "plan.html")
    _git(repo, "commit", "-q", "-m", "plan")
    plan.write_text(
        "committed plan\n\nauthored prose still pending\n", encoding="utf-8"
    )
    lock = _aged_lock(repo)
    # The projection keeps the plan's bookkeeping and drops the authored prose,
    # so a landing of it differs from HEAD's plan text and commits.
    projection = 'committed plan\n<meta name="plan-version" content="2">\n'
    monkeypatch.setattr(
        promotion_scope,
        "_verified_plan_text_projection",
        lambda _checkout, _path: projection,
    )

    result = promotion_scope._commit_preserving_plan_text(
        run_id="run-text",
        verdict="delivered",
        checkout=repo,
        targets=[plan.resolve()],
        text_edits={plan.resolve(): projection},
        subject=None,
        body=None,
        store_row_written=False,
    )

    assert result["committed"] is True, result
    assert not lock.exists()
    assert "authored prose" in plan.read_text(encoding="utf-8"), (
        "the pending prose must survive the projection"
    )
    entry = _removal_entries("run-text")[0]
    _assert_names_lock_and_age(entry["message"], lock)


def test_a_state_write_states_the_lock_it_cleared(
    tmp_path: Path,
    crew_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A state write has no run record, so the clearing is stated where it is read."""
    repo = _init_repo(tmp_path)
    target = repo / "tracked.txt"
    target.write_text("state write\n", encoding="utf-8")
    lock = _aged_lock(repo)
    monkeypatch.setattr(
        ledger,
        "_state_checkout_paths",
        lambda _project, paths, _what: (repo, [Path(p).name for p in paths]),
    )

    retries = ledger._commit_state_write(
        "project",
        "subject",
        "body",
        target,
        what="state",
        sleep=lambda _seconds: None,
    )

    assert retries >= 1
    assert not lock.exists()
    assert _git(repo, "log", "-1", "--format=%s") == "subject"
    lines = [
        line for line in capsys.readouterr().err.splitlines() if "index lock:" in line
    ]
    assert len(lines) == 1, lines
    _assert_names_lock_and_age(lines[0], lock)
