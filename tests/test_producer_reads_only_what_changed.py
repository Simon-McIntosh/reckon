"""A live producer's poll parses only the stream bytes appended since the last.

The producer re-reads the whole fleet on every tick. Each run's stream is
megabytes by the end of a turn, so re-parsing every record once a second is the
cost the producer's design pays, and a poll over a fleet nobody moved should pay
none of it. These cases hold the contract: the producer records the bytes it
parsed in its registration, an unchanged poll parses zero bytes and launches no
git subprocess, a stream replaced or truncated under a cursor is read from its
first record rather than resumed into bytes it never held, and the transitions
an incremental producer yields are the ones the whole-file path yields over the
same files.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import recovery_watch
from reckon.crew import recovery, runs

PROJECT = "producer-reads"
FOREIGN_HOST = "a-login-node-that-is-not-this-one"
MOMENT = 1_800_000_000.0

# Roughly two megabytes per run, so nine growing streams reproduce the shape the
# producer's cost was measured on. Every assertion is taken against the bytes
# actually appended rather than this constant.
APPEND_CHARS = 2_000_000
PARITY_CHARS = 50_000
NINE = 9


def _record(run_id: str, *, chars: int, tag: int) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "session_id": run_id,
                "timestamp": f"2026-01-01T00:00:{tag % 60:02d}.000Z",
                "message": {"content": [{"type": "text", "text": "x" * chars}]},
            }
        )
        + "\n"
    )


def _manifest(run_id: str, status: str) -> str:
    return f"---\nnode: {run_id}\nstatus: {status}\n---\n\nbody\n"


def _git(tree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(tree), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def _worktree(tmp_path: Path) -> tuple[Path, str]:
    """A checkout one commit past its base, so the git arm has work to count."""
    tree = tmp_path / "worktree"
    tree.mkdir()
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "fixture@example.invalid")
    _git(tree, "config", "user.name", "fixture")
    (tree / "delivery.txt").write_text("base\n")
    _git(tree, "add", "delivery.txt")
    _git(tree, "commit", "-q", "-m", "base")
    base = _git(tree, "rev-parse", "HEAD")
    (tree / "delivery.txt").write_text("delivered\n")
    _git(tree, "add", "delivery.txt")
    _git(tree, "commit", "-q", "-m", "delivered")
    return tree, base


def _pointer(run_id: str, *, worktree: Path, base_sha: str, status: str | None) -> dict:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": f"node-{run_id}", "plan": "plan-a"},
        "phase": "working",
        "launcher_host": FOREIGN_HOST,
        "process_alive": False,
        "backend": "claude",
        "launch": "cli",
        "command": "claude",
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "worktree": str(worktree),
        "base_sha": base_sha,
        # The status this case mutates, carried so a rebuild restores the start.
        "status": status,
    }


def _seed(record: dict) -> None:
    """Write a run's initial stream, manifest and pointer from its record."""
    Path(record["log_path"]).write_text(_record(record["run_id"], chars=8, tag=0))
    manifest = Path(record["manifest_path"])
    if record["status"] is None:
        if manifest.exists():
            manifest.unlink()
    else:
        manifest.write_text(_manifest(record["run_id"], record["status"]))
    pointer = runs.pointer_path(record["run_id"])
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(record))


def _append(record: dict, *, chars: int, tag: int) -> int:
    text = _record(record["run_id"], chars=chars, tag=tag)
    with Path(record["log_path"]).open("a", encoding="utf-8") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def _uncached(record: dict) -> dict:
    """Classify with no memo to serve, so the answer is the files' own."""
    memo = recovery._classification_memo_path(record)
    if memo is not None and memo.exists():
        memo.unlink()
    return recovery.classify_pointer(record, now_seconds=MOMENT)


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "600")
    tree, base = _worktree(tmp_path)
    records = [
        _pointer(
            f"r-p{index}",
            worktree=tree,
            base_sha=base,
            status=None if index == 0 else "running",
        )
        for index in range(NINE)
    ]
    for record in records:
        _seed(record)
    return {"tree": tree, "base": base, "records": records}


class _StopTickingError(Exception):
    """Ends the producer after the case's steps have all run."""


def _drive(
    project: str, *, steps: list, spawns: list[list[str]]
) -> tuple[list[dict], list[int], list[int]]:
    """Run the producer, invoking ``steps[i]`` at the i-th quiet boundary.

    Each step mutates the fleet for the next poll. Returns the yielded events,
    the ``bytes_parsed_last_poll`` read at each boundary, and the git spawn
    count observed at each boundary.
    """
    events: list[dict] = []
    recorded: list[int] = []
    spawn_marks: list[int] = []
    clock = {"i": 0}

    def sleeper(_interval: float) -> None:
        index = clock["i"]
        clock["i"] += 1
        recorded.append(
            int(
                runs.read_watch_registration(project).get("bytes_parsed_last_poll") or 0
            )
        )
        spawn_marks.append(len(spawns))
        if index >= len(steps):
            raise _StopTickingError
        steps[index]()

    generator = recovery.watch_ticker(project, poll_interval=0.0, sleeper=sleeper)
    with contextlib.suppress(_StopTickingError):
        events.extend(generator)
    return events, recorded, spawn_marks


def _install_spawn_counter(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every git subprocess launched while the producer runs."""
    spawns: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            spawns.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    return spawns


def test_bytes_parsed_equals_bytes_appended_and_unchanged_polls_parse_nothing(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registration reports exactly the producer's read of each poll."""
    records = fleet["records"]
    appended: list[int] = []

    def append_all() -> None:
        grown = sum(_append(record, chars=APPEND_CHARS, tag=1) for record in records)
        appended.append(grown)

    spawns = _install_spawn_counter(monkeypatch)
    _events, recorded, marks = _drive(
        PROJECT, steps=[append_all, lambda: None, lambda: None], spawns=spawns
    )

    assert appended, "the case must have appended stream bytes"
    assert len(recorded) >= 3, recorded
    # The first quiet boundary follows the post-baseline poll, which found
    # nothing appended and so recorded zero bytes.
    assert recorded[0] == 0, "an unchanged poll must parse no bytes"
    assert recorded[1] == appended[0], (
        "the poll parsed the growth it owed, and nothing else"
    )
    assert recorded[2] == 0, "a second unchanged poll parses zero bytes"
    # The first poll counts the worktree's commits; nothing after it may spawn
    # git again while the head holds still.
    assert marks[0] >= 1, "the git arm must have been exercised"
    assert marks[1] == marks[0], "a poll whose head did not move must spawn no git"
    assert marks[2] == marks[0]


def test_a_replaced_or_truncated_stream_is_read_from_zero(fleet) -> None:
    """A cursor into a stream that was replaced resumes nowhere useful."""
    records = fleet["records"]
    first, second = records[0], records[1]
    # Warm the cursors so a stale offset exists for both.
    recovery.classify_pointer(first, now_seconds=MOMENT)
    recovery.classify_pointer(second, now_seconds=MOMENT)

    # Replace the first stream entirely, through a rename so the inode moves.
    replacement = Path(first["log_path"]).with_suffix(".new")
    replacement.write_text(_record(first["run_id"], chars=4, tag=7))
    os.replace(replacement, first["log_path"])
    # Truncate the second in place, keeping its inode.
    Path(second["log_path"]).write_text(_record(second["run_id"], chars=4, tag=8))

    observed_first = recovery.classify_pointer(first, now_seconds=MOMENT)
    observed_second = recovery.classify_pointer(second, now_seconds=MOMENT)

    assert observed_first == _uncached(first)
    assert observed_second == _uncached(second)


def test_incremental_transitions_match_the_whole_file_path(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer yields the transitions a whole-file recompute would."""
    records = fleet["records"]

    def step() -> None:
        for index, record in enumerate(records):
            if record["status"] is None:
                continue
            flipped = "complete" if record["status"] == "running" else "running"
            Path(record["manifest_path"]).write_text(
                _manifest(record["run_id"], flipped)
            )
            record["status"] = flipped
            _append(record, chars=PARITY_CHARS, tag=index + 1)

    def transitions() -> list[tuple]:
        events, _recorded, _marks = _drive(PROJECT, steps=[step, step, step], spawns=[])
        return [
            (event.get("run_id"), event.get("to_state"))
            for event in events
            if event.get("event") == "transition"
        ]

    incremental = transitions()

    # Rebuild identical starting files, drop every memo, and drive again with
    # the resume path disabled so each poll re-reads whole streams: the whole-
    # file arm over the same file sequence.
    for index, record in enumerate(records):
        record["status"] = None if index == 0 else "running"
        _seed(record)
        memo = recovery._classification_memo_path(record)
        if memo is not None and memo.exists():
            memo.unlink()

    real_observe = _backends.observe_log
    monkeypatch.setattr(
        _backends,
        "observe_log",
        lambda **kwargs: real_observe(**{**kwargs, "resume": None}),
    )
    whole_file = transitions()

    assert incremental, "the case must produce transitions to compare"
    assert incremental == whole_file


def test_a_poll_over_unchanged_runs_reads_no_admission_bytes(fleet) -> None:
    """The admission path reads nothing when no stream has moved.

    The admission check reads a run's raw stream records rather than the
    dialect fold, so its reads sit beside the shared stream reads, and a poll
    over a fleet whose streams are unchanged must read nothing there either.
    The counter is exercised on growth first, so a zero on the unchanged poll is
    a reading rather than a meter that never moves.
    """
    records = fleet["records"]
    admission: list[int] = []
    clock = {"i": 0}

    def sleeper(_interval: float) -> None:
        index = clock["i"]
        clock["i"] += 1
        admission.append(recovery.take_admission_stream_bytes())
        # The first boundary is over an unchanged fleet; at the second, grow
        # every stream so the poll after it must rescan the admission path.
        if index == 1:
            for record in records:
                _append(record, chars=PARITY_CHARS, tag=index + 1)
        if index >= 2:
            raise _StopTickingError

    generator = recovery.watch_ticker(PROJECT, poll_interval=0.0, sleeper=sleeper)
    with contextlib.suppress(_StopTickingError):
        list(generator)

    assert len(admission) >= 3, admission
    # Both polls over the unchanged fleet read nothing on the admission path.
    assert admission[0] == 0, admission
    assert admission[1] == 0, admission
    # A grown stream is read again, which also proves the counter can be non-zero.
    assert admission[2] > 0, admission


def _terminal_record(run_id: str, *, pad: int) -> str:
    """A stream record that ends the turn, padded so it outgrows a cursor."""
    return (
        json.dumps(
            {
                "type": "result",
                "session_id": run_id,
                "is_error": False,
                "result": "done-this-turn-" + "y" * pad,
            }
        )
        + "\n"
    )


def _cursor_offset(memo: dict) -> int:
    """The byte offset the cursor in ``memo`` recorded for the stream."""
    return int((memo.get("stream") or {}).get("offset") or 0)


def test_a_stream_replaced_beyond_the_cursor_is_read_from_zero(fleet) -> None:
    """A replacement at a new inode, at least as long as the cursor, re-reads whole.

    The replacement is longer than the inode cursor reached, so the pre-existing
    ``offset <= size`` guard cannot force the whole read: only the inode
    comparison can, and without it the read resumes mid-record and misses the
    terminal event the new first record carries.
    """
    record = fleet["records"][0]
    memo: dict = {}
    before = recovery._observed_stream(record, memo=memo)
    assert before is not None and before["terminal"] is False

    replacement = Path(record["log_path"]).with_suffix(".new")
    replacement.write_text(_terminal_record(record["run_id"], pad=400))
    assert replacement.stat().st_size >= _cursor_offset(memo), (
        "the replacement must outgrow the cursor, or the size guard alone saves it"
    )
    os.replace(replacement, record["log_path"])

    observed = recovery._observed_stream(record, memo=memo)

    assert observed == recovery._observed_stream(record, memo=None)
    assert observed is not None and observed["terminal"] is True


def test_a_stream_rewritten_in_place_is_read_from_zero(fleet) -> None:
    """A rewrite at the same inode, opened with new bytes, re-reads from zero.

    The inode is unchanged and the new file is longer than the cursor, so
    neither the inode comparison nor the size guard can catch it: only the
    fingerprint of the stream's opening distinguishes a rewrite from an append,
    and without it the read resumes from the stale offset and misses the
    terminal event in the new first record.
    """
    record = fleet["records"][1]
    memo: dict = {}
    before = recovery._observed_stream(record, memo=memo)
    assert before is not None and before["terminal"] is False

    path = Path(record["log_path"])
    inode = os.stat(path).st_ino
    path.write_text(_terminal_record(record["run_id"], pad=400))
    assert os.stat(path).st_ino == inode, "the rewrite must keep the inode"
    assert path.stat().st_size >= _cursor_offset(memo)

    observed = recovery._observed_stream(record, memo=memo)

    assert observed == recovery._observed_stream(record, memo=None)
    assert observed is not None and observed["terminal"] is True


def _fake_worktree(root: Path, index: int) -> Path:
    """A checkout whose head identity reads as a revision without a git process."""
    tree = root / f"wt-{index}"
    git_dir = tree / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("f" * 40 + "\n", encoding="utf-8")
    return tree


def test_the_commits_cache_evicts_gone_worktrees_and_stays_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reclaimed worktree's count is dropped and the cache stays bounded."""
    limit = 8
    monkeypatch.setattr(recovery_watch, "_COMMITS_BEYOND_BASE_CACHE_LIMIT", limit)
    cache = recovery._COMMITS_BEYOND_BASE_CACHE
    cache.clear()
    completed = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout=b"2\n", stderr=b""
    )
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **k: completed)

    def count(tree: Path) -> int:
        return recovery._commits_beyond_base(
            {"worktree": str(tree), "base_sha": "base"}
        )

    try:
        trees = [_fake_worktree(tmp_path, index) for index in range(limit)]
        assert [count(tree) for tree in trees] == [2] * limit
        assert len(cache) == limit, sorted(cache)

        # Reclaim half the trees; the next classification must drop their entries.
        gone = trees[: limit // 2]
        for tree in gone:
            shutil.rmtree(tree)
        assert not gone[0].is_dir()
        count(_fake_worktree(tmp_path, limit))
        for tree in gone:
            assert (str(tree), "base") not in cache, sorted(cache)

        # Many more classified runs: the cache never grows past its ceiling.
        for index in range(limit + 1, limit * 6):
            count(_fake_worktree(tmp_path, index))
        assert len(cache) <= limit, sorted(cache)
    finally:
        cache.clear()
