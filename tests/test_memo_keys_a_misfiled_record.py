"""A run's review can sit under another run's id, and the memo must see it move.

A review worker writes its record by hand, so a record can sit at a path keyed
on the reviewer's own run id while its content names the run it reviews. A run
with no record of its own is answered from that file, and the classification
memo keys the run's own candidate paths and the review directory's identity —
neither of which moves when the misfiled record is rewritten in place. A memo
written before the rewrite would go on serving the earlier verdict, so the row
reads promotable on a review that has since said otherwise.

These tests hold that the misfiled record's own identity is part of the key: a
rewrite in place is seen on the next classification, while an unchanged record
leaves the memo in force rather than defeating it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.crew import recovery, review

# A moment every classification in this module is taken at, so the two arms of a
# parity case differ by the memo and by nothing else.
MOMENT = 1_800_000_000.0

# A host that is not this one, so liveness comes from the record rather than
# from this machine's process table.
FOREIGN_HOST = "a-login-node-that-is-not-this-one"

TARGET_RUN = "r-the-reviewed-run"

# The run id the record file is named for: the review worker keyed the file on
# its own identity rather than the run its content reviews.
REVIEWING_RUN = "r-the-reviewers-own-run-id"


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


def _place(record: dict, *, status: str, turns: int = 2) -> dict:
    _write(Path(record["manifest_path"]), _manifest_text(status))
    stream = "".join(
        _stream_record(record["run_id"], index) for index in range(1, turns + 1)
    )
    _write(Path(record["log_path"]), stream)
    _write(crew.pointer_path(record["run_id"]), json.dumps(record))
    return record


def _classify(record: dict) -> dict:
    return recovery.classify_pointer(record, now_seconds=MOMENT)


def _uncached(record: dict) -> dict:
    """Classify with no memo to serve, so the answer is the files' own."""
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


def _misfiled_path() -> Path:
    return review.review_store_root() / "proj" / f"{REVIEWING_RUN}.json"


def _misfiled_record_text(reviewed_run_id: str, *, head: str, verdict: str) -> str:
    """A hand-written review of ``reviewed_run_id`` as its verdict worded it."""
    if verdict == "parsed":
        record = {
            "status": "parsed",
            "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 90),
            "absent": [],
            "total": 90,
        }
    else:
        record = {"status": verdict, "scores": {}, "absent": [], "total": None}
    record.update(
        {
            "review_run_id": REVIEWING_RUN,
            "reviewed_run_id": reviewed_run_id,
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
        }
    )
    return json.dumps(record)


@pytest.fixture
def reviewed(tmp_path: Path):
    """A completed run whose only stored review is filed under another run id."""
    tree = tmp_path / "reviewed-tree"
    tree.mkdir()
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "worker@example.invalid")
    _git(tree, "config", "user.name", "Worker")
    (tree / "file.txt").write_text("first\n", encoding="utf-8")
    _git(tree, "add", "file.txt")
    _git(tree, "commit", "-q", "-m", "first")
    head = _head(tree)

    record = _place(_pointer(TARGET_RUN, worktree=tree), status="complete")
    _write(
        _misfiled_path(),
        _misfiled_record_text(TARGET_RUN, head=head, verdict="parsed"),
    )
    return record, head


def test_a_misfiled_record_rewritten_in_place_reclassifies(
    isolated_reckon_home: Path, reviewed
) -> None:
    """The record is keyed on its own identity, so an in-place rewrite is seen.

    The rewrite moves no directory entry and none of the run's own files, which
    are every input the key held before: without the misfiled file's own
    identity in the key the memo would serve the parsed verdict on a record
    that no longer says it.
    """
    record, head = reviewed

    before = _classify(record)
    assert before["classification"] == "promotable"

    _write(
        _misfiled_path(),
        _misfiled_record_text(TARGET_RUN, head=head, verdict="unparsed"),
    )

    after = _classify(record)
    fresh = _uncached(record)

    assert after["classification"] == "scoring"
    assert after["review_status"] == "unparsed"
    # The whole row is not compared: some of its fields are ages read against
    # the wall clock, which moves between the two calls. The verdict and the
    # classification are what the rewrite moved.
    assert after["classification"] == fresh["classification"]
    assert after["review_status"] == fresh["review_status"]


def test_an_unchanged_misfiled_record_leaves_the_memo_in_force(
    isolated_reckon_home: Path, reviewed, monkeypatch
) -> None:
    """The added identity must not defeat the memo it keys: nothing moved, nothing read."""
    record, _head_sha = reviewed
    calls = _counting_parse(monkeypatch)

    first = _classify(record)
    assert calls, "the first classification reads the run's stream"

    read_again = list(calls)
    second = _classify(record)

    assert second == first
    assert calls == read_again, "an unchanged misfiled record costs no re-read"


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
