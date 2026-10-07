"""Two reviews of one plan version keep their own staging file.

A review run is the identity a delivered plan review is keyed by, so two reviews
of one plan version whose bytes are identical must not share a staging file:
the version-and-blob name hashes both to one blob, and the plain path the first
review took would be overwritten by the second. These cases store two such
reviews — identical ``reviewed_blob_sha``, distinct ``review_run_id`` — read
each back by the review run id it carries, find both through the version-keyed
candidate walk, and hold a re-store of one run to rewriting only its own file.
The store is a temporary review directory under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from reckon.crew import plan_review as module

PROJECT = "rounds-fixture"
PLAN = "rounds-plan"
VERSION = 2
# One blob for both reviews: identical plan bytes at one version is exactly the
# case the version-and-blob name cannot separate.
BLOB = "c" * 40
RUN_ONE = "r-20261004T164308123456-review-rounds-one"
RUN_TWO = "r-20261005T014515123456-review-rounds-two"
FINDING_ONE = "first-round-finding"
FINDING_TWO = "second-round-finding"


def _record(run_id: str, finding_id: str, *, extra: str | None = None) -> dict:
    record = {
        "project": PROJECT,
        "plan_slug": PLAN,
        "plan_version": VERSION,
        "rubric": "plan_review",
        "reviewed_blob_sha": BLOB,
        "plan_fingerprint": "fp",
        "findings": [{"id": finding_id, "type": "reuse", "text": "name the owner"}],
        "responses": {},
        "status": "ready",
        "review_run_id": run_id,
    }
    if extra is not None:
        record["note"] = extra
    return record


def _read_by_run(base_dir: Path, run_id: str) -> tuple[Path | None, dict | None]:
    """Return the record the named review run carries, from the candidate walk.

    The reader goes through ``_candidate_paths`` — the version-keyed walk every
    plan-review reader is built on — so a stored round is found without a reader
    change, and the record is selected by the review run id it carries rather
    than by the filename it happens to sit at.
    """
    for path in module._candidate_paths(PROJECT, PLAN, VERSION, base_dir):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("review_run_id") == run_id:
            return path, record
    return None, None


def test_two_review_runs_of_one_version_keep_their_own_file(tmp_path: Path) -> None:
    first = module.store_plan_review(_record(RUN_ONE, FINDING_ONE), base_dir=tmp_path)
    second = module.store_plan_review(_record(RUN_TWO, FINDING_TWO), base_dir=tmp_path)

    # The defect this holds: two runs of identical bytes must not share a file.
    assert first != second
    assert first.is_file() and second.is_file()

    # Both rounds are reachable through the version-keyed candidate walk, and
    # the second stayed inside the ``.at-*.json`` family those readers glob.
    candidates = module._candidate_paths(PROJECT, PLAN, VERSION, tmp_path)
    assert set(candidates) == {first, second}
    assert second.name.startswith(f"plan-{PLAN}.v{VERSION}.at-")
    assert second.name.endswith(".json")

    # Each round reads back by its own review run id.
    path_one, record_one = _read_by_run(tmp_path, RUN_ONE)
    path_two, record_two = _read_by_run(tmp_path, RUN_TWO)
    assert path_one == first and record_one is not None
    assert path_two == second and record_two is not None
    assert record_one["findings"][0]["id"] == FINDING_ONE
    assert record_two["findings"][0]["id"] == FINDING_TWO


def test_re_storing_one_run_rewrites_only_its_own_file(tmp_path: Path) -> None:
    first = module.store_plan_review(_record(RUN_ONE, FINDING_ONE), base_dir=tmp_path)
    second = module.store_plan_review(_record(RUN_TWO, FINDING_TWO), base_dir=tmp_path)
    first_bytes = first.read_bytes()

    # A re-store of one run resolves to the file it already owns and leaves the
    # other run's file byte-for-byte untouched.
    again_two = module.store_plan_review(
        _record(RUN_TWO, FINDING_TWO, extra="re-stored"), base_dir=tmp_path
    )
    assert again_two == second
    assert first.read_bytes() == first_bytes
    assert json.loads(second.read_text(encoding="utf-8"))["note"] == "re-stored"

    # Re-storing the first run is idempotent on its own file too.
    again_one = module.store_plan_review(
        _record(RUN_ONE, FINDING_ONE), base_dir=tmp_path
    )
    assert again_one == first
    assert module._candidate_paths(PROJECT, PLAN, VERSION, tmp_path) == sorted(
        {first, second}
    )


def test_the_newest_round_reads_through_the_version_keyed_reader(
    tmp_path: Path,
) -> None:
    first = module.store_plan_review(_record(RUN_ONE, FINDING_ONE), base_dir=tmp_path)
    second = module.store_plan_review(_record(RUN_TWO, FINDING_TWO), base_dir=tmp_path)
    # Pin the mtimes so the ordering the reader resolves is unambiguous: two
    # writes this close would otherwise be ordered by the filesystem alone.
    now = time.time()
    os.utime(first, (now - 10, now - 10))
    os.utime(second, (now, now))

    # "Newest" keeps its meaning: the reader returns the most recently written
    # round among the candidates for this version, not the plain-path one.
    newest = module.read_plan_review(PROJECT, PLAN, VERSION, base_dir=tmp_path)
    assert newest is not None and newest["review_run_id"] == RUN_TWO
    assert _read_by_run(tmp_path, RUN_TWO)[0] == second != first
