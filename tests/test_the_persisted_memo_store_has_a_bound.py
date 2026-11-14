"""The persisted memo store is bounded by the sweep.

Every plan edit whose review persists a snapshot adds a memo entry holding that
snapshot's whole prose, and nothing else removes them, so the store grows with
the number of reviewed edits. The sweep judges an entry by its last write: an
entry is cache, re-derived from the code that produced it, and a read never
refreshes one, so an entry still in use is rewritten on its next miss.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import gc

KIND = "plan-review-snapshot-evidence"
CUTOFF_DAYS = gc.PERSISTED_MEMO_MAX_AGE_DAYS


def _memo(home, kind: str, name: str, *, age_days: float, now: datetime):
    """Write one persisted-memo entry as the store's own path names it."""

    root = home / "cache" / kind
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    path = root / f"{digest}-v1.json"
    path.write_text('{"schema": "reckon.persisted-memo"}', encoding="utf-8")
    stamp = (now - timedelta(days=age_days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def bounded_home(tmp_path, monkeypatch):
    """Point the config home at a temporary directory the sweep resolves through."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    # The sweep's other stores are not this test's subject; it judges run
    # directories and scratch, which a temporary repo has none of.
    monkeypatch.setattr(
        gc, "_retention_ledger_records", lambda *args: (tmp_path / "absent", {}, {}, {})
    )
    monkeypatch.setattr(gc, "list_live", list)
    monkeypatch.setattr(gc, "_live_review_targets", set)
    monkeypatch.setattr(gc, "settle_run_scratch", lambda **kwargs: {"removed": []})
    return home


def test_a_gc_pass_removes_aged_memos_and_keeps_recent_ones(
    bounded_home, monkeypatch, capsys
):
    now = datetime.now(tz=UTC)
    stale = _memo(bounded_home, KIND, "stale", age_days=CUTOFF_DAYS + 1, now=now)
    fresh = _memo(bounded_home, KIND, "fresh", age_days=CUTOFF_DAYS - 1, now=now)

    dry = gc.retention_sweep(apply=False, now=now)
    assert dry["dry_run"] is True
    assert stale.is_file() and fresh.is_file()

    report = gc.retention_sweep(apply=True, now=now)

    assert not stale.exists()
    assert fresh.is_file()
    removed = [row for row in report["persisted_memos"] if row["removed"]]
    assert [row["path"] for row in removed] == [str(stale)]
    captured = capsys.readouterr()
    assert str(stale) in captured.err
    # stdout carries the pass's JSON report, so a removal must not land there.
    assert captured.out == ""


def test_skipping_the_memo_store_leaves_aged_entries_in_place(
    bounded_home, monkeypatch
):
    """Negative control: without the memo roots the sweep cannot reach them."""

    now = datetime.now(tz=UTC)
    stale = _memo(bounded_home, KIND, "stale", age_days=CUTOFF_DAYS + 1, now=now)
    monkeypatch.setattr(gc, "_persisted_memo_roots", list)

    report = gc.retention_sweep(apply=True, now=now)

    assert stale.is_file()
    assert report["persisted_memos"] == []
