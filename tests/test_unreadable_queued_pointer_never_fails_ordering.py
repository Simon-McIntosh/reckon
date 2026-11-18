"""One unreadable queued pointer never takes the ordering, or a compose, down.

A queued request is placed by when it arrived, so a pointer whose ``queued_at``
cannot be read has no place. The ordering sets it aside rather than failing the
pass, ranks every pointer it can, and names the set-aside run so its record can
be repaired. A queued pointer is also never stored without an arrival every
reader can parse: the writers stamp one where it is missing.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_order, runs
from reckon.crew.admission import launching

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SESSION = "coordinator-a"
LANE = "clive"
GOOD_OLDER = "r-queued-older"
GOOD_NEWER = "r-queued-newer"
UNREADABLE = "r-unplaceable"
REVIEW = "r-composed-review"

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private crew home, so no queued pointer is written anywhere shared."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _pointer(
    run_id: str,
    *,
    minutes_ago: float | None,
    role: str = "implement",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": "proj",
        "plan": "fixture",
        "section": "s1",
        "repo": "/repo",
        "session": SESSION,
        "backend": LANE,
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "node": {
            "id": run_id,
            "goal": "read the queued pointer's arrival from its own record",
            "plan": "fixture",
            "section": "s1",
            "role": role,
        },
    }
    if minutes_ago is not None:
        record["queued_at"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return record


# ── the ordering ranks what it can read and names what it cannot


def test_the_ordering_ranks_the_readable_pointers_and_names_the_set_aside_one() -> None:
    queued = [
        _pointer(GOOD_OLDER, minutes_ago=9),
        _pointer(UNREADABLE, minutes_ago=None),
        _pointer(GOOD_NEWER, minutes_ago=1),
    ]

    order = queue_order.admission_order(queued, [], now=NOW)

    assert [pointer["run_id"] for pointer in order] == [GOOD_OLDER, GOOD_NEWER]
    set_aside = queue_order.partition_placeable(queued)[1]
    assert [pointer["run_id"] for pointer in set_aside] == [UNREADABLE]


def test_the_ordering_keeps_its_order_when_no_pointer_is_set_aside() -> None:
    queued = [
        _pointer(GOOD_OLDER, minutes_ago=9),
        _pointer(GOOD_NEWER, minutes_ago=1),
    ]

    order = queue_order.admission_order(queued, [], now=NOW)

    assert [pointer["run_id"] for pointer in order] == [GOOD_OLDER, GOOD_NEWER]
    assert queue_order.partition_placeable(queued)[1] == []


def test_a_malformed_stamp_is_set_aside_like_a_missing_one() -> None:
    queued = [_pointer(GOOD_NEWER, minutes_ago=1)]
    queued.insert(0, {**_pointer(UNREADABLE, minutes_ago=None), "queued_at": "sundial"})

    order = queue_order.admission_order(queued, [], now=NOW)

    assert [pointer["run_id"] for pointer in order] == [GOOD_NEWER]
    assert [p["run_id"] for p in queue_order.partition_placeable(queued)[1]] == [
        UNREADABLE
    ]


# ── a review is still composed while such a pointer waits in the queue


def test_a_queued_review_is_placed_while_an_unreadable_pointer_waits(
    crew_home: Path,
) -> None:
    for record in (
        _pointer(GOOD_OLDER, minutes_ago=9),
        _pointer(UNREADABLE, minutes_ago=None),
    ):
        runs._write_json(runs.pointer_path(str(record["run_id"])), record)

    review = _pointer(REVIEW, minutes_ago=None, role="review")
    stored, position = runs.queue_dispatch(review)

    assert stored["run_id"] == REVIEW
    assert isinstance(position, int) and position >= 1


# ── no writer ever stores a queued pointer it cannot place


def test_the_requeue_writers_stamp_an_arrival_they_cannot_read() -> None:
    restored = launching._requeued_request({"run_id": UNREADABLE}, UNREADABLE)

    assert restored["phase"] == "queued"
    assert queue_order.parse_utc(restored["queued_at"]) is not None

    kept = launching._requeued_request(
        {"run_id": GOOD_OLDER, "queued_at": (NOW - timedelta(minutes=9)).isoformat()},
        GOOD_OLDER,
    )
    assert kept["queued_at"] == (NOW - timedelta(minutes=9)).isoformat()


def test_a_stored_stamp_is_kept_and_a_broken_one_is_replaced() -> None:
    assert (
        queue_order.readable_queued_at({"queued_at": "2026-11-14T10:00:00Z"}, now=NOW)
        == "2026-11-14T10:00:00Z"
    )
    for stamp in (None, "", "yesterday", 0.5j):
        fresh = queue_order.readable_queued_at({"queued_at": stamp}, now=NOW)
        assert queue_order.parse_utc(fresh) is not None, stamp


def test_no_writer_copies_an_arrival_it_has_not_read() -> None:
    """A pointer's ``queued_at`` is never assigned from another record's field.

    That copy is the defect's shape: the re-queue path carried a predecessor's
    stamp forward unread, so one pointer missing its arrival made the next one
    missing it too. Every writer now stamps through :func:`readable_queued_at`
    or derives the copy through a parsed moment.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "reckon").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                if not (
                    isinstance(target.slice, ast.Constant)
                    and target.slice.value == "queued_at"
                ):
                    continue
                copied = any(
                    isinstance(sub, ast.Subscript)
                    and isinstance(sub.slice, ast.Constant)
                    and sub.slice.value == "queued_at"
                    for sub in ast.walk(node.value)
                )
                if copied:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert offenders == [], f"an unread arrival is copied forward: {offenders}"
