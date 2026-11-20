"""A stored review's two arm logs are read once per state, however often it is read.

Every read of a stored review annotates it with the failures the reviewed run
added, derived from the two gate logs its manifest names. The logs never change
after the run delivers, yet each read re-read both of them and rescanned them in
full: the station's classification tick held 6.7% of its samples in those scans
to recount a number that had not moved.

These tests store one review over a synthesised run and stand in for the one
function that reads an arm log's bytes, counting the reads across repeated reads
of the review. An unchanged pair reads each log once. A head log rewritten to a
new identity is read again and recounted, reporting the failure it gained. A
log in neither recognised format still reads as unmeasured rather than as a
measured zero.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from reckon import file_memo
from reckon.crew import review as review_module
from reckon.crew.runs import run_dir

PROJECT = "proj"
RUN = "r-20261120T000000000000-unchanged-pair"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
SCORE = 18
TOTAL = SCORE * len(review_module.REVIEW_DIMENSIONS)
BASE_LOG_NAME = "base.log"
HEAD_LOG_NAME = "head.log"


def _log(revision: str, failures: list[str]) -> str:
    """Return a gate log in the shape the fleet's gate runner writes.

    The first line names the revision and the literal command, as a real arm
    log does; the summary line is what tells a green pytest log from a log that
    recorded nothing.
    """
    command = (
        "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q "
        "tests/test_gate.py"
    )
    lines = [f"# revision: {revision}; tree: /tmp/scratch; command: {command}"]
    lines.extend(f"FAILED {test_id} - AssertionError: boom" for test_id in failures)
    lines.append(f"{len(failures)} failed, 1 passed" if failures else "1 passed")
    lines.append("EXIT=1" if failures else "EXIT=0")
    return "\n".join(lines) + "\n"


def _suite(log_name: str) -> str:
    observation = {
        "revision": HEAD_SHA,
        "exit_status": 1,
        "log_path": log_name,
        "failure_ids": [],
        "completed": True,
    }
    return json.dumps(observation)


@pytest.fixture()
def stored_review(tmp_path: Path) -> dict:
    """Store one review whose run carries a green base log and a failing head.

    The two logs sit under the reviewed run's own directory, which is where a
    real run writes them, and the manifest names them by relative path exactly
    as an arm records them.
    """
    directory = run_dir(RUN)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / BASE_LOG_NAME).write_text(_log(BASE_SHA, []), encoding="utf-8")
    (directory / HEAD_LOG_NAME).write_text(
        _log(HEAD_SHA, ["tests/test_gate.py::test_beta"]), encoding="utf-8"
    )
    (directory / "manifest.md").write_text(
        f"node: {RUN}\n"
        "status: complete\n"
        f"baseline_suite: {_suite(BASE_LOG_NAME)}\n"
        f"after_suite: {_suite(HEAD_LOG_NAME)}\n",
        encoding="utf-8",
    )
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, SCORE),
            "absent": [],
            "timestamp": "2026-11-20T00:00:00+00:00",
            review_module.REVIEWED_BASE_KEY: BASE_SHA,
            review_module.REVIEWED_HEAD_KEY: HEAD_SHA,
        }
    )
    return {"run_directory": directory}


@pytest.fixture()
def counted_reads(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Stand in for the file read, counting only the two arm logs' reads.

    The stand-in is the read itself rather than one of this module's helpers,
    so the count holds whatever route the review takes to the log — the same
    count a change that skipped the read entirely would show, and the count a
    reader of the defect sees on a tree that still re-reads. Every read is
    recorded under the log's own name, so a read of one arm is told from a read
    of the other. The memo is dropped either side of the test, so the count is
    this test's reads and nothing a previous test cached.
    """
    reads: Counter[str] = Counter()
    watched = {BASE_LOG_NAME, HEAD_LOG_NAME}
    read_text = Path.read_text

    def counting(path: Path, *arguments: Any, **keywords: Any) -> str:
        if path.name in watched:
            reads[path.name] += 1
        return read_text(path, *arguments, **keywords)

    file_memo.clear()
    monkeypatch.setattr(Path, "read_text", counting)
    yield reads
    file_memo.clear()


def _read() -> dict:
    record = review_module.read_review(PROJECT, RUN)
    assert record is not None, "the stored review must be readable"
    return record


def test_five_reads_of_an_unchanged_pair_read_each_log_once(
    stored_review: dict, counted_reads: Counter[str]
) -> None:
    """An unchanged pair is read and counted once, not once per review read.

    Five reads of the same stored review all report the added failure, and the
    logs behind it are read once each: the count is derived per pair of log
    identities rather than per review read.
    """
    for _ in range(5):
        record = _read()
        assert record["added_failure_count"] == 1
        assert record["added_failure_ids"] == ["tests/test_gate.py::test_beta"]

    assert counted_reads[BASE_LOG_NAME] == 1, (
        "the base log is unchanged across all five reads, so it must be read "
        f"once; it was read {counted_reads[BASE_LOG_NAME]} times"
    )
    assert counted_reads[HEAD_LOG_NAME] == 1, (
        "the head log is unchanged across all five reads, so it must be read "
        f"once; it was read {counted_reads[HEAD_LOG_NAME]} times"
    )


def test_a_rewritten_head_log_is_read_again_and_recounted(
    stored_review: dict, counted_reads: Counter[str]
) -> None:
    """A head log at a new identity misses the pair's key and is recounted.

    The pair's key holds both logs' identities, so rewriting the head log with
    one more failing test misses the stored count. Nothing but the result is
    held — no log bytes are kept — so the derivation reads both arms again: the
    head to find the failure it gained, and the base to difference against. Once
    derived, the new pair is answered from the memo like any other.
    """
    for _ in range(5):
        _read()
    assert counted_reads[BASE_LOG_NAME] == 1
    assert counted_reads[HEAD_LOG_NAME] == 1

    (stored_review["run_directory"] / HEAD_LOG_NAME).write_text(
        _log(
            HEAD_SHA,
            ["tests/test_gate.py::test_beta", "tests/test_gate.py::test_gamma"],
        ),
        encoding="utf-8",
    )

    record = _read()

    assert record["added_failure_count"] == 2, (
        "the rewritten head log names one more failing test, so the review must "
        f"report one more added failure; it reported {record['added_failure_count']}"
    )
    assert record["added_failure_ids"] == [
        "tests/test_gate.py::test_beta",
        "tests/test_gate.py::test_gamma",
    ]
    assert counted_reads[HEAD_LOG_NAME] == 2, (
        "the rewritten head log has a new identity, so it must be read again; "
        f"it was read {counted_reads[HEAD_LOG_NAME]} times"
    )
    assert counted_reads[BASE_LOG_NAME] == 2, (
        "the count is a difference over both arms and no log text is retained, "
        "so a changed pair derives from both logs again; the base log was read "
        f"{counted_reads[BASE_LOG_NAME]} times"
    )

    _read()
    assert counted_reads[HEAD_LOG_NAME] == 2
    assert counted_reads[BASE_LOG_NAME] == 2, (
        "the rewritten pair is now the unchanged pair, so a further read must "
        "come from the memo; the base log was read "
        f"{counted_reads[BASE_LOG_NAME]} times"
    )


def test_a_log_in_neither_format_reads_as_unmeasured(
    stored_review: dict, counted_reads: Counter[str]
) -> None:
    """A log the reader cannot place leaves the count unmeasured, not zero.

    A head log carrying no recognised summary says nothing about which tests
    failed, so the pair has no count to state. It reads as unmeasured, and a
    second read of the same unmeasured pair still reads the log once: an
    unmeasured pair is a derived answer like any other.
    """
    (stored_review["run_directory"] / HEAD_LOG_NAME).write_text(
        "collected no output\n", encoding="utf-8"
    )

    first = _read()
    second = _read()

    assert first["added_failure_count"] is None, (
        "a log in neither format cannot say what failed, so it must read as "
        f"unmeasured; it read as {first['added_failure_count']}"
    )
    assert second["added_failure_count"] is None
    assert counted_reads[HEAD_LOG_NAME] == 1, (
        "the unreadable log did not change between the two reads, so it must be "
        f"read once; it was read {counted_reads[HEAD_LOG_NAME]} times"
    )
