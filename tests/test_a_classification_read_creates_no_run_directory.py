"""Classifying a pointer is a read and leaves no run's home behind.

A run's directory is its durable home: it is made when the run is dispatched,
holds the run's own records while it lives, and is removed when the run is
discarded. Classifying a pointer reads that home — its exit, worker and attempt
records, and the memo the classification keeps. A classification that created
the directory would give a pointer that never had a home one out of nothing,
and one that filled an empty directory would make one out of the memo it wrote
there. Either way a discard then finds a directory and writes its marker into
it, so a pointer that merely vanished reads as a deliberate discard, and a run
whose home was removed is resurrected on the next sweep.

The three cases below hold the rule from both sides. A pointer with no run
directory is classified and left with none; a run directory that exists but
holds nothing is left untouched; and the positive control — a directory that
already holds the run's records still receives the memo — shows the guard
skips an empty home rather than switching the memo off for every run.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon import crew
from reckon.crew import recovery

# A moment every classification here is taken at, so a reading derived from the
# wall clock cannot make the comparison a test of how long the test took.
MOMENT = 1_800_000_000.0

# A host that is not this one, so liveness comes from the record rather than
# from this machine's process table.
FOREIGN_HOST = "a-login-node-that-is-not-this-one"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _pointer(run_id: str, tmp_path: Path) -> dict:
    """A pointer whose manifest and stream live outside any run directory.

    Both files sit in the caller's scratch tree rather than under the run's own
    directory, so placing the pointer creates no run directory for the case to
    find — the run home is either made by the test or not made at all.
    """
    manifest = _write(
        tmp_path / f"{run_id}.manifest.md",
        "---\nnode: the-node\nstatus: running\n---\n\nbody\n",
    )
    stream = _write(tmp_path / f"{run_id}.stream.jsonl", '{"type":"turn.started"}\n')
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": f"node-{run_id}", "plan": "plan-a"},
        "phase": "working",
        "launcher_host": FOREIGN_HOST,
        "process_alive": False,
        "manifest_path": str(manifest),
        "log_path": str(stream),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def test_a_classification_read_creates_no_run_directory(tmp_path: Path) -> None:
    """A pointer with no run directory is classified without one appearing."""
    record = _pointer("r-nodir", tmp_path)
    assert not crew.run_dir("r-nodir").exists(), "the pointer brought no home"

    row = recovery.classify_pointer(record, now_seconds=MOMENT)

    assert row["run_id"] == "r-nodir"
    assert not crew.run_dir("r-nodir").exists(), "a read left no run directory"


def test_a_classification_read_leaves_no_memo_in_an_empty_home(
    tmp_path: Path,
) -> None:
    """A run directory that holds nothing is not filled by a classification.

    The memo is the classification's cache of the run's own records, so an empty
    directory has nothing for it to cache: writing one there would turn the
    directory into the run's home and hand a discard a place to leave its marker.
    """
    record = _pointer("r-empty", tmp_path)
    home = crew.run_dir("r-empty")
    home.mkdir(parents=True)

    recovery.classify_pointer(record, now_seconds=MOMENT)

    assert list(home.iterdir()) == [], "a read left the empty home empty"


def test_a_classification_memo_still_reaches_a_run_that_has_records(
    tmp_path: Path,
) -> None:
    """A run directory holding the run's records still receives the memo.

    The positive control for the two cases above: the memo is still written
    when the run has a home with state in it, so the absent memo there is the
    empty directory and not a memo write switched off for every run.
    """
    record = _pointer("r-homed", tmp_path)
    home = crew.run_dir("r-homed")
    home.mkdir(parents=True)
    _write(home / "worker.json", json.dumps({"pid": 4321}))

    recovery.classify_pointer(record, now_seconds=MOMENT)

    memo = recovery._classification_memo_path(record)
    assert memo is not None
    assert memo.is_file(), "a run directory with records keeps the memo"
    assert json.loads(memo.read_text(encoding="utf-8"))["version"] == (
        recovery.CLASSIFICATION_MEMO_VERSION
    )
