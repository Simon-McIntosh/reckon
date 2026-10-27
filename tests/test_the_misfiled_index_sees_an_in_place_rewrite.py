"""The misfiled-review index answers from each record file's own identity.

The store index exists so a per-turn reader does not read the whole store for
every pointer with no review. It is held per store directory and per process,
and it is valid only while the facts it read still describe the files on disk.
A record rewritten in place -- a disposition recorded on a stored review, a
record re-filed under another run id -- keeps its name, its inode and the
store's directory entries, so the directory's stat identity does not move while
the record's content does. Keying the index's validity on the directory alone
therefore answers a lookup from facts that are no longer true, and a lookup
naming the new head or the new run selects nothing.

A run can also hold a record of its own at one head while a record of the same
run sits at another reviewer's path at the head a caller asks about. A lookup
that stops at the run's own files misses it.

Every store is built under ``tmp_path``; nothing touches the operator's store,
and every cost the timing case measures is paid against a synthesised store on
node-local disk.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import review as review_module
from reckon.crew import review_records as records_module

PROJECT = "proj"
REVIEWED_RUN = "r-20261001T000000000000-reviewed"
OTHER_RUN = REVIEWED_RUN + "-second"
REVIEWER_RUN = "r-20261001T010000000000-reviewer"
HEAD_ONE = "a1" * 20
HEAD_TWO = "b2" * 20
HEAD_OWN = "c3" * 20
HEAD_ASKED = "d4" * 20
SESSION_RUNS = 200


def _record(
    reviewed_run_id: str, head: str | None, *, review_run_id: str
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": reviewed_run_id,
        "review_run_id": review_run_id,
        "status": "parsed",
        "scores": {},
        "absent": [],
        "total": 80,
        "timestamp": "2026-10-01T00:00:00+00:00",
    }
    if head is not None:
        record["reviewed_head_sha"] = head
    return record


def _write_in_place(path: Path, payload: dict[str, Any]) -> None:
    """Rewrite ``path`` keeping its inode, as a disposition write does."""
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    directory = review_module.review_store_root() / PROJECT
    directory.mkdir(parents=True)
    records_module._STORE_INDEXES.clear()
    return directory


def test_a_record_rewritten_in_place_is_found_at_its_new_head(store: Path) -> None:
    """A head that moved in place is answered on the next lookup."""
    path = store / f"{REVIEWER_RUN}.json"
    _write_in_place(path, _record(REVIEWED_RUN, HEAD_ONE, review_run_id=REVIEWER_RUN))
    first = review_module.stored_record(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_ONE
    )
    assert first[1] is not None

    _write_in_place(path, _record(REVIEWED_RUN, HEAD_TWO, review_run_id=REVIEWER_RUN))
    found_path, found = review_module.stored_record(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_TWO
    )
    assert found is not None, "the rewrite in place is seen on the next lookup"
    assert found_path == path
    assert found["reviewed_head_sha"] == HEAD_TWO
    assert found.get(review_module.MISFILED_KEY) is True


def test_a_record_rewritten_in_place_is_found_at_its_new_run(store: Path) -> None:
    """A record re-filed onto another reviewed run is answered by that run."""
    path = store / f"{REVIEWER_RUN}.json"
    _write_in_place(path, _record(REVIEWED_RUN, HEAD_ONE, review_run_id=REVIEWER_RUN))
    assert review_module.stored_record(PROJECT, REVIEWED_RUN)[1] is not None

    _write_in_place(path, _record(OTHER_RUN, HEAD_ONE, review_run_id=REVIEWER_RUN))
    found_path, found = review_module.stored_record(PROJECT, OTHER_RUN)
    assert found is not None, "a rewrite naming another run is seen on the next lookup"
    assert found_path == path
    assert found["reviewed_run_id"] == OTHER_RUN


def test_a_record_at_the_asked_head_is_found_beside_the_runs_own_file(
    store: Path,
) -> None:
    """A run's own file at another head does not hide a record at this one."""
    _write_in_place(
        store / f"{REVIEWED_RUN}.json",
        _record(REVIEWED_RUN, HEAD_OWN, review_run_id=REVIEWED_RUN),
    )
    misfiled = store / f"{REVIEWER_RUN}.json"
    _write_in_place(
        misfiled, _record(REVIEWED_RUN, HEAD_ASKED, review_run_id=REVIEWER_RUN)
    )

    found_path, found = review_module.stored_record(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_ASKED
    )
    assert found is not None, "the record filed at the asked head is found"
    assert found_path == misfiled
    assert found.get(review_module.MISFILED_KEY) is True

    # The run's own head still answers from the run's own file, unflagged.
    own_path, own = review_module.stored_record(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_OWN
    )
    assert own_path == store / f"{REVIEWED_RUN}.json"
    assert own is not None
    assert own.get(review_module.MISFILED_KEY) is None


def test_a_two_hundred_run_session_pays_one_whole_store_pass(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session's misses stay inside the budget one store pass costs.

    Every run of the session has its review filed under another reviewer's run
    id, which is the shape that reaches the store's index, and the session's
    last lookup is for a record rewritten in place -- the case the index exists
    to keep cheap and the case the directory's stat identity cannot see. The
    stubbed arm removes the fallback entirely, so the difference between the two
    arms is what the fallback costs the session.
    The whole-store revalidation is paid once and named: a second pass per miss
    is the cost the index was built to remove.
    """
    runs = [
        f"r-20261001T0000{index:02d}000000-session-{index:03d}"
        for index in range(SESSION_RUNS)
    ]
    rewritten = store / f"{REVIEWER_RUN}-rewritten.json"
    for index, run_id in enumerate(runs):
        _write_in_place(
            store / f"{REVIEWER_RUN}-{index:03d}.json",
            _record(
                run_id, f"{index:040x}", review_run_id=f"{REVIEWER_RUN}-{index:03d}"
            ),
        )
    _write_in_place(
        rewritten,
        _record(runs[-1], "e" * 40, review_run_id=REVIEWER_RUN),
    )

    def session() -> None:
        for run_id in runs:
            review_module.stored_record(PROJECT, run_id)

    started = time.perf_counter()
    session()
    head_elapsed = time.perf_counter() - started

    # The session's index is warm now, which is the state a per-turn reader is
    # in: a record rewritten in place has to be seen from it.
    _write_in_place(rewritten, _record(runs[-1], "f" * 40, review_run_id=REVIEWER_RUN))
    assert (
        review_module.stored_record(PROJECT, runs[-1], reviewed_head_sha="f" * 40)[1]
        is not None
    )

    real_filed = records_module._record_filed_elsewhere
    monkeypatch.setattr(
        records_module, "_record_filed_elsewhere", lambda *a, **k: (None, None)
    )
    records_module._STORE_INDEXES.clear()
    started = time.perf_counter()
    session()
    stubbed_elapsed = time.perf_counter() - started
    monkeypatch.setattr(records_module, "_record_filed_elsewhere", real_filed)

    assert head_elapsed - stubbed_elapsed < 3.0, (
        f"the fallback costs the session {head_elapsed - stubbed_elapsed:.2f}s, "
        "against a 3s budget over one whole-store pass"
    )

    passes = {"count": 0}
    real_update = records_module._update_store_entries

    def counting_update(*args: Any, **kwargs: Any) -> Any:
        passes["count"] += 1
        return real_update(*args, **kwargs)

    monkeypatch.setattr(records_module, "_update_store_entries", counting_update)
    records_module._STORE_INDEXES.clear()
    for run_id in runs:
        review_module.stored_record(PROJECT, run_id)
    assert passes["count"] <= 1, (
        f"{passes['count']} whole-store passes over a {SESSION_RUNS}-run session"
    )
