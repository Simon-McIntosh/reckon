"""A producer poll over an unchanged run costs a stat, not a reread.

The producer reduces every live pointer to a snapshot on every poll. Each run's
pointer, manifest, stream and exit record are files, and when nothing has moved
between two polls the snapshot they produced cannot have changed either, so a
poll should open none of them and read only what a stat reports. The reuse key
is the classification's own input composition — the review store's candidates
and the worktree's git head included — so a stored review landing on a watched
run, or a run's head moving, drops the entry as surely as a rewrite does.

The cases hold that contract: an unchanged run's poll opens none of the four
and stats each; a manifest rewrite, a stream append, an exit record, a stored
review and a moved head each recompute; and the whole snapshot a reusing
producer yields over a file sequence is the one a full recompute yields.

The negative control recomputes every snapshot on every poll again, never
reusing one; the unchanged-run case then opens the four files and fails.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery_watch
from reckon.crew import recovery, runs
from reckon.crew import review as review_module

PROJECT = "snapshot-stat"
FOREIGN_HOST = "a-login-node-that-is-not-this-one"
MOMENT = 1_800_000_000.0
STALL_SECONDS = 3600
NINE = 9


def _manifest(run_id: str, status: str) -> str:
    return f"---\nnode: {run_id}\nstatus: {status}\n---\n\nbody\n"


def _record(run_id: str, *, chars: int, tag: int) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "session_id": run_id,
                "message": {"content": [{"type": "text", "text": "x" * chars}]},
                "tag": tag,
            }
        )
        + "\n"
    )


def _base_pointer(run_id: str, directory: Path, *, status: str | None) -> dict:
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
        "status": status,
    }


def _seed(record: dict) -> None:
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


def _append(record: dict, *, chars: int, tag: int) -> None:
    with Path(record["log_path"]).open("a", encoding="utf-8") as handle:
        handle.write(_record(record["run_id"], chars=chars, tag=tag))


def _exit_path(record: dict) -> Path:
    return Path(record["manifest_path"]).parent / recovery.EXIT_RECORD_NAME


def _git(tree: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(tree), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def _git_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "git-tree"
    tree.mkdir()
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "fixture@example.invalid")
    _git(tree, "config", "user.name", "fixture")
    (tree / "delivery.txt").write_text("base\n", encoding="utf-8")
    _git(tree, "add", "delivery.txt")
    _git(tree, "commit", "-q", "-m", "base")
    return tree


def _watched(record: dict) -> set[str]:
    """The four files the section names, at their resolved paths."""
    return {
        str(runs.pointer_path(record["run_id"])),
        str(record["manifest_path"]),
        str(record["log_path"]),
        str(_exit_path(record)),
    }


class _IoCounter:
    """Counts opens and stats of the watched files during one call."""

    def __init__(self, watched: set[str]) -> None:
        self.watched = watched
        self.opens = 0
        self.stats = 0


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    records = []
    for index in range(NINE):
        directory = runs.run_dir(f"r-s{index}")
        directory.mkdir(parents=True, exist_ok=True)
        records.append(
            _base_pointer(
                f"r-s{index}", directory, status=None if index == 0 else "running"
            )
        )
    for record in records:
        _seed(record)
    recovery._SNAPSHOT_CACHE.clear()
    return records


def _counting(monkeypatch: pytest.MonkeyPatch, watched: set[str]) -> _IoCounter:
    """Patch the primitives that open the four files, counting only those paths."""
    counter = _IoCounter(watched)
    real_open = pathlib.Path.open
    real_stat = pathlib.Path.stat

    def counting_open(path, *args, **kwargs):
        if str(path) in counter.watched:
            counter.opens += 1
        return real_open(path, *args, **kwargs)

    def counting_stat(path, *args, **kwargs):
        if str(path) in counter.watched:
            counter.stats += 1
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counting_open)
    monkeypatch.setattr(pathlib.Path, "stat", counting_stat)
    return counter


def _snapshot(record: dict, *, moment: float) -> dict:
    return recovery._watch_snapshot(record, moment=moment, stall_seconds=STALL_SECONDS)


def test_an_unchanged_poll_opens_no_file_and_stats_each(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second poll over untouched files reads none of the four, and stats each."""
    record = fleet[0]
    # The first call misses the cache and classifies, warming the entry.
    _snapshot(record, moment=MOMENT)
    counter = _counting(monkeypatch, _watched(record))
    snapshot = _snapshot(record, moment=MOMENT + 1)

    assert counter.opens == 0, "an unchanged run's poll must open no file"
    assert counter.stats >= 4, "each of the four files must be statted"
    assert snapshot["run_id"] == record["run_id"]
    # The silence is recomputed from the stat: it grows with the elapsed second
    # even though no file moved.
    assert snapshot["quiet_seconds"] is not None


def test_a_manifest_rewrite_recomputes(fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest rewrite drops the snapshot and classifies afresh."""
    record = fleet[1]
    before = _snapshot(record, moment=MOMENT)
    assert before["manifest_status"] == "running"
    Path(record["manifest_path"]).write_text(_manifest(record["run_id"], "complete"))
    counter = _counting(monkeypatch, _watched(record))
    after = _snapshot(record, moment=MOMENT + 1)

    assert counter.opens > 0, "a manifest rewrite must be reread"
    assert after["manifest_status"] == "complete"


def test_a_stream_append_recomputes(fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream append drops the snapshot so the new bytes are observed."""
    record = fleet[2]
    _snapshot(record, moment=MOMENT)
    _append(record, chars=4000, tag=9)
    counter = _counting(monkeypatch, _watched(record))
    snapshot = _snapshot(record, moment=MOMENT + 1)

    assert counter.opens > 0, "a grown stream must be observed again"
    assert snapshot["run_id"] == record["run_id"]


def test_an_exit_record_recomputes(fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's own exit record drops the snapshot: its evidence moved."""
    record = fleet[3]
    _snapshot(record, moment=MOMENT)
    _exit_path(record).write_text(json.dumps({"exit_code": 0, "signal": None}))
    counter = _counting(monkeypatch, _watched(record))
    snapshot = _snapshot(record, moment=MOMENT + 1)

    assert counter.opens > 0, "an exit record must be reread"
    assert snapshot["run_id"] == record["run_id"]


def _store_complete_review(run_id: str, tokens: str) -> None:
    """Store a parsed review for one run, naming the head it read."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": tokens,
            "reviewed_head_sha": tokens,
        }
    )
    review_module.store_review(record)


def _completed_git_record(tmp_path: Path, run_id: str) -> tuple[dict, Path, str]:
    """A run with a completed manifest over a real git worktree."""
    tree = _git_tree(tmp_path)
    head = _git(tree, "rev-parse", "HEAD")
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    record = _base_pointer(run_id, directory, status="complete")
    record["worktree"] = str(tree)
    record["base_sha"] = head
    _seed(record)
    return record, tree, head


def test_a_stored_review_recomputes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review landing with the four files untouched changes the classification.

    The run is complete with no review attached, so it reads ``scoring``; a
    review stored for its head moves it to ``promotable`` on the next poll. If
    the reuse key covered only the four named files the stale ``scoring`` row
    would be served and the review reflex would arm a second review.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    recovery._SNAPSHOT_CACHE.clear()
    record, _tree, head = _completed_git_record(tmp_path, "r-review")
    before = _snapshot(record, moment=MOMENT)
    assert before["classification"] == "scoring", before["classification"]

    _store_complete_review("r-review", head)
    counter = _counting(monkeypatch, _watched(record))
    after = _snapshot(record, moment=MOMENT + 1)

    assert counter.opens > 0, "a stored review must be reread"
    assert after["classification"] == "promotable", after["classification"]


def test_a_moved_worktree_head_recomputes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head that moves with the four files untouched drops the snapshot."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    recovery._SNAPSHOT_CACHE.clear()
    record, tree, _head = _completed_git_record(tmp_path, "r-head")
    _snapshot(record, moment=MOMENT)
    before_tokens = recovery._worktree_head_identity(tree)

    (tree / "delivery.txt").write_text("moved\n", encoding="utf-8")
    _git(tree, "add", "delivery.txt")
    _git(tree, "commit", "-q", "-m", "moved")
    assert recovery._worktree_head_identity(tree) != before_tokens

    counter = _counting(monkeypatch, _watched(record))
    after = _snapshot(record, moment=MOMENT + 1)

    assert counter.opens > 0, "a moved head must reclassify"
    assert after["run_id"] == record["run_id"]


def test_reuse_matches_full_recompute_over_a_file_sequence(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole snapshot a reusing poll yields is the one a full recompute yields.

    Only ``quiet_seconds`` is derived from the poll's own moment by design and
    is excluded from the comparison; everything else, the stall-derived state
    and detail included, must be broken by nothing.
    """
    record = fleet[4]
    moment_derived = {"quiet_seconds"}

    def flip_manifest() -> None:
        record["status"] = "complete" if record["status"] == "running" else "running"
        Path(record["manifest_path"]).write_text(
            _manifest(record["run_id"], record["status"])
        )

    def grow_stream() -> None:
        _append(record, chars=2000, tag=1)

    def write_exit() -> None:
        _exit_path(record).write_text(json.dumps({"exit_code": 0, "signal": None}))

    mutations = [flip_manifest, grow_stream, write_exit, flip_manifest, grow_stream]

    def drive() -> list[dict]:
        recovery._SNAPSHOT_CACHE.clear()
        out = []
        moment = MOMENT
        for mutate in mutations:
            moment += 30
            mutate()
            # A fixed write instant per step, so both arms see the same stream
            # clock and the snapshots are comparable field for field.
            os.utime(record["log_path"], (moment - 45, moment - 45))
            out.append(_snapshot(record, moment=moment))
        return out

    reusing = drive()

    # Rebuild the same start and drive with reuse disabled: every poll
    # classifies afresh from the files — the whole-recompute arm.
    record["status"] = "running"
    _seed(record)
    monkeypatch.setattr(recovery_watch, "_remember_snapshot", lambda *a, **k: None)
    whole = drive()

    assert reusing, "the case must produce snapshots to compare"
    for reusing_row, whole_row in zip(reusing, whole):
        trimmed_reusing = {
            key: value for key, value in reusing_row.items() if key not in moment_derived
        }
        trimmed_whole = {
            key: value for key, value in whole_row.items() if key not in moment_derived
        }
        assert trimmed_reusing == trimmed_whole