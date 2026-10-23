"""An exited attempt's harness caches leave with that attempt.

A codex run's directory carries the bulk of a run's bytes as caches its harness
built for one process: the plugin catalog and download cache under its
run-owned home, and the per-run tool cache the worktree's library stack fills.
Nothing reads either after the process ends, so they are removed as the exit
record is written, and the paths and sizes that went are carried on the record.

Two things must not happen. The client's session tree a resume reads has to
survive, or every resume of the run breaks. And the removal is bound to the
writing of an attempt's exit record, never to the presence of one, so a later
attempt of the same run filling the same paths is never touched by a read of an
earlier attempt's record.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import dispatch_launch
from reckon.crew.dispatch_launch import (
    ATTEMPT_HARNESS_CACHES,
    EXIT_RECORD_NAME,
    _remove_attempt_harness_caches,
    _supervisor_exit_detail,
    _supervisor_exit_record,
)

# The relative paths the three caches and the session tree occupy under a run
# directory, each seeded with a file of a known size so a removal can be shown
# to have taken the right bytes.
_CACHE_FILES = (
    ("codex-home/cache", 10),
    ("codex-home/plugins", 200),
    ("cache", 30),
)
_SESSION_FILE = ("codex-home/sessions", 50)


def _seed_run_directory(run_directory: Path) -> dict[str, int]:
    """Plant the three caches and the session tree, returning their byte sizes."""
    sizes: dict[str, int] = {}
    for relative, size in (*_CACHE_FILES, _SESSION_FILE):
        leaf = run_directory / relative / "payload.bin"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(b"x" * size)
        sizes[relative] = size
    return sizes


def test_an_exited_attempt_keeps_its_session_tree_and_loses_its_caches(
    tmp_path: Path,
) -> None:
    """The exit record's write removes each cache, keeps sessions, and lists them."""
    run_directory = tmp_path / "r-harness-cache"
    sizes = _seed_run_directory(run_directory)

    record = _supervisor_exit_record(
        run_id=run_directory.name,
        attempt=1,
        worker_pid=None,
        launched_at="2026-10-23T00:00:00Z",
        status=None,
        run_directory=run_directory,
    )

    # The resume fixture: the session tree a resume reads is kept.
    sessions = run_directory / _SESSION_FILE[0]
    assert sessions.is_dir(), "the session tree was removed with the caches"
    assert (sessions / "payload.bin").read_bytes() == b"x" * sizes[_SESSION_FILE[0]]

    # Each of the three caches is gone, and the record names it with its size.
    reported = {
        item["path"]: item["bytes"] for item in record["removed_harness_caches"]
    }
    for relative, _ in _CACHE_FILES:
        target = run_directory / relative
        assert not target.exists(), f"the cache {relative} survived the exit"
        assert reported.get(str(target)) == sizes[relative], (
            f"the exit record did not name {relative} with its size"
        )
    assert len(record["removed_harness_caches"]) == len(_CACHE_FILES)


def test_a_run_that_has_not_exited_keeps_every_cache(tmp_path: Path) -> None:
    """Nothing removes a cache until an attempt of the run exits."""
    run_directory = tmp_path / "r-still-running"
    _seed_run_directory(run_directory)

    for relative, _ in (*_CACHE_FILES, _SESSION_FILE):
        assert (run_directory / relative / "payload.bin").is_file(), (
            f"{relative} was removed before the run exited"
        )


def test_a_later_attempts_caches_survive_a_read_of_an_earlier_exit_record(
    tmp_path: Path,
) -> None:
    """A read of the first attempt's exit record never removes the second's caches."""
    run_directory = tmp_path / "r-second-attempt"
    _seed_run_directory(run_directory)

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
    # The first attempt's exit record is left where a later reader finds it.
    dispatch_launch._supervisor_write(run_directory / EXIT_RECORD_NAME, first)

    # A second attempt of the same run now fills the same cache paths.
    _seed_run_directory(run_directory)

    # A later read of the first attempt's exit record touches none of them.
    _supervisor_exit_detail(run_directory)

    for relative, _ in (*_CACHE_FILES, _SESSION_FILE):
        assert (run_directory / relative / "payload.bin").is_file(), (
            f"a read of the earlier exit record removed the later attempt's {relative}"
        )


def test_a_named_cache_and_a_removal_agree_on_the_paths(tmp_path: Path) -> None:
    """The removal reports exactly the caches it names, and never the sessions."""
    run_directory = tmp_path / "r-path-agreement"
    _seed_run_directory(run_directory)

    removed = _remove_attempt_harness_caches(run_directory)
    assert {item["path"] for item in removed} == {
        str(run_directory / relative) for relative in ATTEMPT_HARNESS_CACHES
    }
    assert (run_directory / _SESSION_FILE[0]).is_dir()
    assert sum(item["bytes"] for item in removed) == sum(
        size for _, size in _CACHE_FILES
    )
