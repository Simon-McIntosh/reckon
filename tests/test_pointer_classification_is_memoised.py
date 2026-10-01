"""A pointer's classification is memoised beside it, and the key is every input.

A classification reads four sources — the pointer, the assertion its manifest
makes, the worker's stream and the review stored against it — and a fleet sweep
reads them again for every live run on every tick. The stream is the expensive
one: a worker's log is megabytes by the end of a turn, and re-parsing it per
reader is what the memo exists to stop.

What these tests hold is the contract rather than the speed. A memo is served
only while the stat identity of every file the classification read still
matches, so a changed manifest, review record, pointer or tree head recomputes
instead of answering from the earlier read; a stream that has only grown is
re-read from the byte offset the last observation reached, not from its first
record; and a memoised classification agrees with one taken with no memo at
all. The last is the case that catches a memo which is fast because it is
wrong, and it is asserted against the same inputs on both arms.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.crew import recovery, review

# A moment every classification in this module is taken at, so the two arms of
# a parity case differ by the memo and by nothing else. The wall clock moves
# between calls, and a reading derived from it would make the comparison a test
# of how long the test took.
MOMENT = 1_800_000_000.0

# A host that is not this one, so liveness comes from the record rather than
# from this machine's process table and every case here is deterministic.
FOREIGN_HOST = "a-login-node-that-is-not-this-one"


def _manifest_text(status: str) -> str:
    return f"---\nnode: the-node\nstatus: {status}\n---\n\nbody\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _stream_record(session: str, index: int) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "session_id": session,
                "timestamp": f"2026-01-01T00:0{index}:00.000Z",
                "message": {"content": [{"type": "text", "text": f"turn {index}"}]},
            }
        )
        + "\n"
    )


def _pointer(run_id: str, *, worktree: Path) -> dict:
    """A record shaped like a live pointer for one run of the synthesised fleet."""
    directory = crew.run_dir(run_id)
    return {
        "run_id": run_id,
        "project": "proj",
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
    }


def _place(record: dict, *, status: str = "running", turns: int = 2) -> dict:
    """Write a pointer and its run's files, and return the record as its file holds it."""
    _write(Path(record["manifest_path"]), _manifest_text(status))
    stream = "".join(
        _stream_record(record["run_id"], index) for index in range(1, turns + 1)
    )
    _write(Path(record["log_path"]), stream)
    pointer = crew.pointer_path(record["run_id"])
    _write(pointer, json.dumps(record))
    return record


def _classify(record: dict) -> dict:
    return recovery.classify_pointer(record, now_seconds=MOMENT)


def _uncached(record: dict) -> dict:
    """Classify with no memo to serve, so the answer is the files' own.

    The memo beside the pointer is removed first, which is the state a cold
    process finds: nothing to serve, everything to read.
    """
    memo = recovery._classification_memo_path(record)
    if memo is not None and memo.exists():
        memo.unlink()
    return _classify(record)


def _counting_parse(monkeypatch) -> list[int]:
    """Count the lines each stream parse was handed, in call order."""
    calls: list[int] = []
    original = _backends.parse_events

    def counting(lines):
        materialised = list(lines)
        calls.append(len(materialised))
        return original(materialised)

    monkeypatch.setattr(_backends, "parse_events", counting)
    return calls


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A checkout of the run's own, outside the configuration home."""
    place = tmp_path / "worktree"
    place.mkdir(parents=True, exist_ok=True)
    return place


@pytest.fixture
def fleet(isolated_reckon_home: Path, worktree: Path) -> dict[str, dict]:
    """Two placed runs, so a case can show one moving and the other not."""
    first = _place(_pointer("r-one", worktree=worktree), status="running")
    second = _place(_pointer("r-two", worktree=worktree), status="running")
    return {"one": first, "two": second}


# ── the memo is served ──────────────────────────────────────────────────────


def test_a_second_classification_does_not_parse_the_stream_again(
    fleet, monkeypatch
) -> None:
    """An unchanged run answers from the memo, so the stream is parsed once."""
    calls = _counting_parse(monkeypatch)
    record = fleet["one"]

    _classify(record)
    assert calls == [2], "the first classification reads the run's stream"

    _classify(record)

    assert calls == [2], "the second reads no stream at all"


def test_appending_to_one_stream_re_observes_only_the_appended_bytes(
    fleet, monkeypatch
) -> None:
    """A grown stream costs the records appended to it, not the whole file."""
    calls = _counting_parse(monkeypatch)
    first, second = fleet["one"], fleet["two"]
    _classify(first)
    _classify(second)
    calls.clear()

    with Path(first["log_path"]).open("a", encoding="utf-8") as handle:
        handle.write(_stream_record(first["run_id"], 3))

    _classify(first)
    assert calls == [1], "the grown stream costs only the record appended to it"

    _classify(second)
    assert calls == [1], "the run whose stream did not move parses nothing at all"


def test_a_memo_read_from_disk_carries_no_in_call_marker(fleet) -> None:
    """The marker that lets one call reuse its own parse does not survive to disk.

    A stream entry written by a call is current for that call, which is what
    lets the second reader of one classification reuse the first reader's parse.
    Read back from the file it means nothing — the call that wrote it has ended
    — and a later reader that took it for a fresh key would serve a stream
    without the key having matched, which is the one thing a memo may not do.
    """
    record = fleet["one"]
    _classify(record)

    reloaded = recovery._read_classification_memo(record)

    assert "current" not in reloaded.get("stream", {})


def test_a_grown_stream_reports_the_records_it_already_folded(fleet) -> None:
    """Resuming from an offset carries the folded records rather than losing them."""
    record = fleet["one"]
    before = _classify(record)

    with Path(record["log_path"]).open("a", encoding="utf-8") as handle:
        handle.write(_stream_record(record["run_id"], 3))

    after = _classify(record)

    assert after["classification"] == before["classification"]
    assert after["manifest_digest"] == before["manifest_digest"]


# ── the key covers every input ──────────────────────────────────────────────


def test_a_changed_manifest_reclassifies(fleet) -> None:
    """A manifest that moved is read again, not answered from the memo."""
    record = fleet["one"]
    before = _classify(record)

    _write(Path(record["manifest_path"]), _manifest_text("complete"))
    after = _classify(record)

    assert after["classification"] != before["classification"]
    assert after["classification"] == _uncached(record)["classification"]


def test_a_changed_review_record_reclassifies(fleet) -> None:
    """A review stored between two calls is seen by the second, not masked."""
    record = fleet["one"]
    _write(Path(record["manifest_path"]), _manifest_text("complete"))
    before = _classify(record)
    assert before["classification"] == "scoring"

    _store_review(record["run_id"])
    after = _classify(record)

    assert after["classification"] == "promotable"
    assert after["classification"] == _uncached(record)["classification"]


def test_a_changed_pointer_reclassifies(fleet) -> None:
    """A pointer rewritten under the memo is classified on what it now says."""
    record = fleet["one"]
    before = _classify(record)

    moved = dict(record, phase="stopped")
    _write(crew.pointer_path(record["run_id"]), json.dumps(moved))
    after = _classify(moved)

    assert after["classification"] == "stopped"
    assert after["classification"] != before["classification"]


def test_a_changed_tree_head_reclassifies(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """A commit under a stored review moves the record that describes the run.

    The review store files a record against the revision it read. A tree that
    gained a commit since is no longer described by that record, so the run
    stops reading promotable — which it cannot do if the head is not part of the
    memo's key, because the memo would go on serving the verdict taken before
    the commit.
    """
    tree = tmp_path / "reviewed-tree"
    tree.mkdir()
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "worker@example.invalid")
    _git(tree, "config", "user.name", "Worker")
    (tree / "file.txt").write_text("first\n", encoding="utf-8")
    _git(tree, "add", "file.txt")
    _git(tree, "commit", "-q", "-m", "first")

    record = _place(_pointer("r-tree", worktree=tree), status="complete")
    _store_review(record["run_id"], head=_head(tree))

    before = _classify(record)
    assert before["classification"] == "promotable"

    (tree / "file.txt").write_text("second\n", encoding="utf-8")
    _git(tree, "add", "file.txt")
    _git(tree, "commit", "-q", "-m", "second")

    after = _classify(record)

    assert after["classification"] == "scoring"
    assert after["classification"] == _uncached(record)["classification"]


# ── parity ──────────────────────────────────────────────────────────────────


def test_memoised_and_uncached_classifications_agree(fleet) -> None:
    """Every pointer classifies the same with a memo as without one.

    The two arms are taken on the same files at the same moment, so a
    difference between them is the memo and nothing else. This is the case that
    fails when a memo is fast because it is stale.
    """
    for record in fleet.values():
        _classify(record)

    for run_id, record in fleet.items():
        memoised = _classify(record)
        uncached = _uncached(record)
        assert memoised == uncached, f"{run_id} classified differently"


def test_a_second_classification_agrees_after_a_stream_grows(fleet) -> None:
    """A resumed observation lands on the answer a whole parse would give."""
    record = fleet["one"]
    _classify(record)
    with Path(record["log_path"]).open("a", encoding="utf-8") as handle:
        handle.write(_stream_record(record["run_id"], 3))

    resumed = _classify(record)
    whole = _uncached(record)

    assert resumed == whole


# ── helpers ─────────────────────────────────────────────────────────────────


def _git(tree: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=tree, check=True, capture_output=True)


def _head(tree: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _store_review(run_id: str, *, head: str | None = None) -> Path:
    """File a review of ``run_id`` at the revision it read."""
    store = review.review_store_root() / "proj"
    name = f"{run_id}.json" if head is None else f"{run_id}.at-{head}.json"
    record = {
        "status": "parsed",
        "review_run_id": f"review-of-{run_id}",
        "reviewed_run_id": run_id,
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 90),
        "absent": [],
        "total": 90,
    }
    if head is not None:
        record["reviewed_head_sha"] = head
        record["reviewed_base_sha"] = head
    return _write(store / name, json.dumps(record))
