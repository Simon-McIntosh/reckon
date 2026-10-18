"""The attempts memo compares the ledger stamp members it means, by name.

A memoised section-attempts read is keyed on the ledger's index state. When
that state is read by position, a member added to or reordered within the
ledger's stamp shifts which member the memo compares, and the miss is silent:
no test fails and a stale value is served. This checks the memo against a
ledger stamp that has gained an extra leading member, and shows it still
notices a change to the runs directory's identity while holding through a
change to the index file's identity alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from reckon import ledger, mcp_views
from reckon.crew import runs

_INSERTED_MEMBER = "leading_member_the_memo_must_ignore"


def _stamp_function_name() -> str:
    """The ledger function whose return the memo's state is taken from.

    A ledger that exposes its index state as a member-named record is read
    through that; otherwise the positional index stamp is the stamp the memo
    slices, and the extra member is inserted there.
    """
    return (
        "index_state_stamp" if hasattr(ledger, "index_state_stamp") else "index_stamp"
    )


def _with_leading_member(stamp):
    """Insert one extra member at the front of the stamp the ledger built."""
    if isinstance(stamp, Mapping):
        return {_INSERTED_MEMBER: "inserted", **stamp}
    return [_INSERTED_MEMBER, *stamp]


def test_attempt_memo_compares_stamp_members_by_name(
    tmp_path: Path, monkeypatch
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(runs, "live_dir", lambda: live)
    monkeypatch.setattr(ledger, "load", lambda *_args: ({"runs": []}, 0))
    monkeypatch.setattr(
        ledger, "_run_index_path", lambda *_args: tmp_path / "ledger.sqlite"
    )

    runs_dir = ledger.ledger_path("sample", tmp_path).parent / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "zzz.json").write_text(json.dumps({"run_id": "zzz"}))
    index_file = tmp_path / "ledger.sqlite"
    index_file.write_text("index-v1")

    # The memo reads the stamp the ledger builds. Insert an extra member at the
    # front of that stamp, so a positional read compares a shifted set.
    name = _stamp_function_name()
    original_stamp = getattr(ledger, name)
    monkeypatch.setattr(
        ledger,
        name,
        lambda *args, **kwargs: _with_leading_member(original_stamp(*args, **kwargs)),
    )

    mcp_views._ATTEMPT_CACHE.clear()
    original_group = mcp_views._group_section_attempts
    aggregations = [0]

    def counted(*args, **kwargs):
        aggregations[0] += 1
        return original_group(*args, **kwargs)

    monkeypatch.setattr(mcp_views, "_group_section_attempts", counted)

    def read() -> dict:
        return mcp_views.section_attempts_by_plan("sample", tmp_path)

    assert read() == {}
    assert aggregations[0] == 1
    assert read() == {}
    assert aggregations[0] == 1, "an unchanged stamp must serve from the memo"

    # Move the runs directory's identity without moving the newest run file:
    # the new file sorts before the newest, so the newest-run member is
    # unchanged and only the runs directory's identity moves.
    (runs_dir / "0000-early.json").write_text(json.dumps({"run_id": "early"}))
    read()
    assert aggregations[0] == 2, (
        "the runs directory's identity change must invalidate the memo"
    )
    assert read() == {}
    assert aggregations[0] == 2

    # Move only the index file's identity. The memo names the members it
    # compares, and the index file's identity is not among them, so the memo
    # holds.
    index_file.write_text("index-v2")
    read()
    assert aggregations[0] == 2, (
        "an index-file-only change must not invalidate the memo"
    )


def test_attempt_memo_notices_a_member_added_to_the_stamp(
    tmp_path: Path, monkeypatch
) -> None:
    """A member added to the stamp the ledger builds reaches the memo.

    The ledger owns one member set. A member added to it must reach the record
    the memo compares, so a change it carries invalidates the memo rather than
    being invisible to it.
    """
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(runs, "live_dir", lambda: live)
    monkeypatch.setattr(ledger, "load", lambda *_args: ({"runs": []}, 0))
    ledger.ledger_path("sample", tmp_path).parent.mkdir(parents=True, exist_ok=True)

    held = ["first"]
    if hasattr(ledger, "_index_stamp_members"):
        original_members = ledger._index_stamp_members

        def with_added_member(project, root=None):
            members = original_members(project, root)
            members["added_member"] = held[0]
            return members

        monkeypatch.setattr(ledger, "_index_stamp_members", with_added_member)
    else:
        original_stamp = ledger.index_stamp

        def with_added_member(*args, **kwargs):
            return [held[0], *original_stamp(*args, **kwargs)]

        monkeypatch.setattr(ledger, "index_stamp", with_added_member)

    mcp_views._ATTEMPT_CACHE.clear()
    original_group = mcp_views._group_section_attempts
    aggregations = [0]

    def counted(*args, **kwargs):
        aggregations[0] += 1
        return original_group(*args, **kwargs)

    monkeypatch.setattr(mcp_views, "_group_section_attempts", counted)

    def read() -> dict:
        return mcp_views.section_attempts_by_plan("sample", tmp_path)

    assert read() == {}
    assert aggregations[0] == 1
    assert read() == {}
    assert aggregations[0] == 1, "an unchanged stamp must serve from the memo"

    held[0] = "second"
    read()
    assert aggregations[0] == 2, (
        "a member added to the stamp must invalidate the memo when it changes"
    )
