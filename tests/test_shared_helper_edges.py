"""Edge cases the shared helpers must keep, found by review of their callers.

Two behaviours that a review of the first migration wave found broken:

* the atomic JSON writer leaked its sibling temporary when the ``chmod`` that
  applies a requested mode raised, even though the writer promises a failure
  leaves neither a partial destination nor a stray sibling;
* the zone test in the watch-stream and rollout callers matched only a trailing
  ``Z``/``z`` or an offset ending in minutes, so an offset that carries seconds
  was read as naming no zone.

Both are asserted against the behaviour the pre-migration base produced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from reckon import _store
from reckon._store import write_json_atomically
from reckon.crew.query import _normalize_stamp
from reckon.crew.rollout import _parse_timestamp

# An offset that carries seconds: local 01:02:03 at +00:00:30 is 01:01:33 UTC.
SECONDS_OFFSET = "2026-09-29T01:02:03+00:00:30"
SECONDS_OFFSET_UTC = datetime(2026, 9, 29, 1, 1, 33, tzinfo=UTC)


def test_chmod_failure_leaves_no_temporary_beside_the_destination(
    tmp_path: Path,
) -> None:
    """A failing chmod removes the sibling temporary it created.

    The writer creates a unique sibling, chmods it to the requested mode, then
    renames it over the destination. When the chmod raises, the destination
    must still be the old content and the directory must hold nothing but the
    destination — no half-created sibling left behind.
    """
    destination = tmp_path / "dest.json"
    destination.write_text("OLD\n")

    def refuse_chmod(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated chmod failure")

    with mock.patch.object(_store.os, "chmod", refuse_chmod):
        try:
            write_json_atomically(destination, {"a": 1})
        except OSError:
            pass
        else:  # pragma: no cover - the refusal must propagate
            raise AssertionError("the chmod failure did not propagate")

    assert destination.read_text() == "OLD\n"
    leftover = sorted(p.name for p in tmp_path.iterdir() if p.name != "dest.json")
    assert leftover == []


def test_query_reads_a_seconds_offset_as_a_zone() -> None:
    """The watch-stream caller reports an offset carrying seconds as UTC."""
    assert _normalize_stamp(SECONDS_OFFSET) == ("2026-09-29T01:01:33+00:00", "utc")


def test_rollout_reads_a_seconds_offset_as_a_zone() -> None:
    """The rollout caller returns the same aware instant as the base."""
    moment = _parse_timestamp(SECONDS_OFFSET)
    assert moment is not None
    assert moment.tzinfo is not None
    assert moment == SECONDS_OFFSET_UTC
