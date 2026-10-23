"""An exited attempt's harness caches leave with that attempt.

A codex run's directory carries the bulk of a run's bytes as caches its harness
built for one process: the plugin catalog and download cache under its
run-owned home, and the per-run tool cache the worktree's library stack fills.
Nothing reads either after the process ends, so they are removed as the exit
record is written, and the paths and sizes that went are carried on the record.

Two things must not happen. The client's session tree a resume reads has to
survive, or every resume of the run breaks. And the removal belongs to the
attempt that built the caches, never to a later attempt filling the same paths:
it runs only while the attempt being recorded is still the run's current one,
so a record written late leaves a newer attempt's caches alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew.dispatch_launch import (
    ATTEMPT_HARNESS_CACHES,
    ATTEMPT_RECORD_NAME,
    EXIT_RECORD_NAME,
    _remove_attempt_harness_caches,
    _supervisor_exit_detail,
    _supervisor_exit_record,
    _supervisor_launched_worker,
    _supervisor_write,
)

# The three caches, plus the session tree a resume reads, each with a file of a
# known size so a removal can be shown to have taken the right bytes. The cache
# paths are spelled out here rather than read from the module's own constant,
# so this assertion cannot agree with that constant by construction.
_CACHES = (
    ("codex-home/cache", 10),
    ("codex-home/plugins", 200),
    ("cache", 30),
)
_SESSIONS = ("codex-home/sessions", 50)


def _seed_run_directory(run_directory: Path) -> None:
    """Plant the three caches and the session tree under a run directory."""
    for relative, size in (*_CACHES, _SESSIONS):
        leaf = run_directory / relative / "payload.bin"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(b"x" * size)


def _name_current_attempt(run_directory: Path, attempt: int) -> None:
    """Write the marker a live attempt puts down, so it reads as current."""
    (run_directory / ATTEMPT_RECORD_NAME).write_text(
        json.dumps({"run_id": run_directory.name, "attempt": attempt}),
        encoding="utf-8",
    )


def _present(run_directory: Path, relative: str) -> bool:
    return (run_directory / relative / "payload.bin").is_file()


def test_an_exited_attempt_keeps_its_session_tree_and_loses_its_caches(
    tmp_path: Path,
) -> None:
    """The exit record's write removes each cache, keeps sessions, and lists them."""
    run_directory = tmp_path / "r-harness-cache"
    _seed_run_directory(run_directory)
    _name_current_attempt(run_directory, 1)

    record = _supervisor_exit_record(
        run_id=run_directory.name,
        attempt=1,
        worker_pid=None,
        launched_at="2026-10-23T00:00:00Z",
        status=None,
        run_directory=run_directory,
    )

    # The resume fixture: the session tree a resume reads is kept.
    assert _present(run_directory, _SESSIONS[0]), "the session tree was removed"

    # Each of the three caches is gone, and the record names it with its size.
    reported = {
        item["path"]: item["bytes"] for item in record["removed_harness_caches"]
    }
    for relative, size in _CACHES:
        target = run_directory / relative
        assert not target.exists(), f"the cache {relative} survived the exit"
        assert reported.get(str(target)) == size, (
            f"the exit record did not name {relative} with its size"
        )
    assert len(record["removed_harness_caches"]) == len(_CACHES)


def test_a_run_that_has_not_exited_keeps_every_cache(tmp_path: Path) -> None:
    """A removal driven for a run whose live attempt is newer removes nothing."""
    run_directory = tmp_path / "r-still-running"
    _seed_run_directory(run_directory)
    # The second attempt is the one running now; the first is long gone.
    _name_current_attempt(run_directory, 2)

    # The exit readers a run with no exit record is put through see nothing.
    assert _supervisor_exit_detail(run_directory) is None
    assert _supervisor_launched_worker(run_directory) is False

    # A record written late for the superseded first attempt, and the removal
    # driven for it, leave the live attempt's caches alone: a removal that ran
    # regardless of which attempt is current would take everything here.
    record = _supervisor_exit_record(
        run_id=run_directory.name,
        attempt=1,
        worker_pid=1000,
        launched_at="2026-10-23T00:00:00Z",
        status=None,
        run_directory=run_directory,
    )
    assert record["removed_harness_caches"] == []
    assert _remove_attempt_harness_caches(run_directory, attempt=1) == []

    for relative, _ in (*_CACHES, _SESSIONS):
        assert _present(run_directory, relative), (
            f"{relative} was removed from a run whose live attempt is newer"
        )


def test_a_later_attempts_caches_survive_a_read_of_an_earlier_exit_record(
    tmp_path: Path,
) -> None:
    """A read of the first attempt's exit record never removes the second's caches."""
    run_directory = tmp_path / "r-second-attempt"
    _seed_run_directory(run_directory)
    _name_current_attempt(run_directory, 1)

    # The first attempt exits, removing the caches it built.
    first = _supervisor_exit_record(
        run_id=run_directory.name,
        attempt=1,
        worker_pid=1000,
        launched_at="2026-10-23T00:00:00Z",
        status=None,
        run_directory=run_directory,
    )
    assert first["removed_harness_caches"], "the first attempt removed nothing"
    # Its exit record is left where a later reader finds it.
    _supervisor_write(run_directory / EXIT_RECORD_NAME, first)

    # A second attempt of the same run now fills the same cache paths.
    _name_current_attempt(run_directory, 2)
    _seed_run_directory(run_directory)

    # A later read of the first attempt's exit record touches none of them.
    _supervisor_exit_detail(run_directory)
    _supervisor_launched_worker(run_directory)

    for relative, _ in (*_CACHES, _SESSIONS):
        assert _present(run_directory, relative), (
            f"a read of the earlier exit record removed the later attempt's {relative}"
        )


def test_a_named_cache_and_a_removal_agree_on_the_paths(tmp_path: Path) -> None:
    """The removal reports exactly the caches it names, and never the sessions."""
    run_directory = tmp_path / "r-path-agreement"
    _seed_run_directory(run_directory)
    _name_current_attempt(run_directory, 1)

    removed = _remove_attempt_harness_caches(run_directory, attempt=1)
    assert {item["path"] for item in removed} == {
        str(run_directory / relative) for relative, _ in _CACHES
    }
    assert set(ATTEMPT_HARNESS_CACHES) == {relative for relative, _ in _CACHES}
    assert (run_directory / _SESSIONS[0]).is_dir()
    assert sum(item["bytes"] for item in removed) == sum(size for _, size in _CACHES)
