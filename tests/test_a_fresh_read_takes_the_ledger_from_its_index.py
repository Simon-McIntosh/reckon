"""A fresh read takes the run ledger from its index rather than from every run file.

The index is the ledger's whole point: it carries every run document the
aggregate already decoded, so a reader whose index is current decodes no run
file at all. Two conditions took that away on the live fleet node.

The first is an index that cannot be opened. The pick cache sits inside the
configuration home, and the fleet mounts that home read-only around worker
launches, so the index file could not even be created there. Every fresh
launch therefore paid a full scan of the whole ledger — about 4,150 run files —
with no index for the last-good fallback to serve.

The second is a memo thrown away mid-read. ``_cached_attempts`` published its
aggregate only while the newest run file and the live-pointer set held still
across the read, and on a busy fleet a peer writes one during almost every
``RECKON_ROADMAP``-sized read, so a single read computed the whole aggregate
twice and decoded the ledger twice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from reckon import ledger, mcp_views

RUN_FILES = 200

# Resolve a run index for a project without a checkout, so the fixture owns both
# the ledger and the cache directory the index is published into.
PROJECT = "reckon"


def _fake_run(index: int) -> dict[str, object]:
    run_id = f"r-20261017T000000{index:04d}-node-{index:04d}"
    return {
        "run_id": run_id,
        "project": PROJECT,
        "node": "a-fresh-read-takes-the-ledger-from-its-index",
        "role": "implement",
        "plan": "a-read-answers-inside-its-deadline",
        "section": "s28",
        "gate": "passed",
    }


def _build_ledger(root: Path) -> Path:
    """Write RUN_FILES run documents and the aggregate that indexes them."""
    state = root / "docs" / "state" / PROJECT
    runs = state / "runs"
    runs.mkdir(parents=True)
    rows = []
    for index in range(RUN_FILES):
        record = _fake_run(index)
        (runs / f"{record['run_id']}.json").write_text(json.dumps(record))
        rows.append(record)
    (state / "crew.json").write_text(
        json.dumps({"data": {"runs": rows, "members": [], "holds": [], "_version": 1}})
    )
    return root


def _read_only(directory: Path) -> Path:
    """A directory that exists and cannot be written to."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o500)
    return directory


@pytest.fixture()
def read_only_home(tmp_path, monkeypatch):
    """The live node's condition: a configured pick cache that cannot be written.

    ``RECKON_PICK_CACHE`` names the directory the configuration home resolves to,
    and ``XDG_CACHE_HOME`` names the user cache the read falls back to. Both are
    under the test's own tmp tree, so neither the operator's cache nor the
    configured home is touched.
    """
    locked = _read_only(tmp_path / "configured-cache")
    user_cache = tmp_path / "user-cache"
    user_cache.mkdir()
    monkeypatch.setenv("RECKON_PICK_CACHE", str(locked))
    monkeypatch.setenv("XDG_CACHE_HOME", str(user_cache))
    assert not os.access(locked, os.W_OK)
    return locked, user_cache / "reckon"


def test_the_read_only_condition_still_raises_where_the_index_would_be_built(
    read_only_home,
):
    """The exception the live node raised, reproduced at its own path."""
    import sqlite3

    locked, _fallback = read_only_home
    index = locked / ledger._run_index_path(PROJECT, None).name
    with pytest.raises(sqlite3.OperationalError) as raised:
        sqlite3.connect(index, timeout=0.2)
    assert "unable to open database file" in str(raised.value)


def test_the_index_moves_to_the_user_cache_when_the_configured_one_is_locked(
    read_only_home,
):
    """The index is published where it can be written, not where it cannot."""
    locked, fallback = read_only_home
    assert ledger._index_directory() == fallback
    assert ledger._run_index_path(PROJECT, None).parent == fallback
    assert ledger._index_directory() != locked


def test_a_fresh_process_over_a_current_index_decodes_no_run_file(
    tmp_path, read_only_home, monkeypatch
):
    """The measured claim: no ``_read_run`` call under the live node's condition."""
    root = _build_ledger(tmp_path / "repo")
    _locked, fallback = read_only_home
    # Build the index before the read under test, so it is current when the
    # process under measurement starts.
    ledger.load(PROJECT, root)
    index = ledger._run_index_path(PROJECT, root)
    assert index.parent == fallback and index.is_file()

    driver = tmp_path / "count_read_run.py"
    driver.write_text(
        textwrap.dedent(
            """
            import os, sys
            from pathlib import Path
            from reckon import ledger

            root = Path(sys.argv[1])
            calls = []
            ledger._read_run = lambda source: calls.append(source)
            ledger.load("reckon", root)
            print(f"READ_RUN={len(calls)}")
            """
        )
    )
    result = subprocess.run(
        [sys.executable, str(driver), str(root)],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "READ_RUN=0" in result.stdout


def test_one_read_aggregates_once_when_a_live_pointer_is_written_mid_read(
    tmp_path, home, monkeypatch
):
    """A pointer written during the aggregate is seen next read, not re-aggregated."""
    root = _build_ledger(tmp_path / "repo")
    ledger.load(PROJECT, root)

    from reckon.crew import runs

    live = runs.live_dir()
    live.mkdir(parents=True, exist_ok=True)
    written: list[int] = []
    real_records = runs._list_live_records

    def records(*args, **kwargs):
        rows = real_records(*args, **kwargs)
        if not written:
            written.append(1)
            (live / "r-late.json").write_text(
                json.dumps(
                    {
                        "run_id": "r-late",
                        "project": PROJECT,
                        "node": {
                            "plan": "a-read-answers-inside-its-deadline",
                            "section": "s28",
                        },
                        "role": "implement",
                        "created_at": "2026-10-17T10:00:00Z",
                    }
                )
            )
        return rows

    monkeypatch.setattr(runs, "_list_live_records", records)

    loads: list[int] = []
    real_load = ledger.load

    def counting(*args, **kwargs):
        loads.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(ledger, "load", counting)
    mcp_views._ATTEMPT_CACHE.clear()

    first = mcp_views.section_attempts_by_plan(PROJECT, root)
    second = mcp_views.section_attempts_by_plan(PROJECT, root)

    assert written, "the pointer was never written during the aggregate"
    assert len(loads) == 1, f"ledger.load ran {len(loads)} times in one read"
    assert first == second
