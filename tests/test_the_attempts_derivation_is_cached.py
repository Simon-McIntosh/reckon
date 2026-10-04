"""Section attempt reads reuse a stable ledger and live-pointer snapshot."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reckon import ledger, mcp_views
from reckon.crew import runs


def test_attempt_aggregation_reuses_and_invalidates(
    tmp_path: Path, monkeypatch
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(runs, "live_dir", lambda: live)
    history: list[dict] = []
    version = [0]
    monkeypatch.setattr(ledger, "index_stamp", lambda *_args: [version[0]])
    monkeypatch.setattr(
        ledger, "load", lambda *_args: ({"runs": list(history)}, version[0])
    )
    original = mcp_views._group_section_attempts
    aggregations = [0]

    def counted(*args, **kwargs):
        aggregations[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_views, "_group_section_attempts", counted)

    def read() -> dict:
        return mcp_views.section_attempts_by_plan("sample", tmp_path)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _index: read(), range(10)))
    assert results == [{}] * 10
    assert aggregations[0] == 1

    pointer = {
        "project": "sample",
        "role": "implement",
        "run_id": "launched",
        "node": {"plan": "fixture", "section": "work"},
    }
    path = live / "launched.json"
    path.write_text(json.dumps(pointer))
    assert read()["fixture"]["work"]["attempts"] == 1
    assert aggregations[0] == 2

    stamp = path.stat().st_mtime_ns
    pointer["node"]["section"] = "else"
    path.write_text(json.dumps(pointer))
    os.utime(path, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    assert "work" not in read()["fixture"]
    assert read()["fixture"]["else"]["attempts"] == 1
    assert aggregations[0] == 3
    pointer["node"]["section"] = "work"
    path.write_text(json.dumps(pointer))
    os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
    assert read()["fixture"]["work"]["attempts"] == 1
    assert aggregations[0] == 4

    history.append({**pointer, "plan": "fixture", "section": "work", "gate": "passed"})
    version[0] += 1
    path.unlink()
    promoted = read()["fixture"]["work"]
    assert promoted["attempts"] == 1
    assert promoted["attempt_outcomes"][0]["status"] == "promoted"
    assert aggregations[0] == 5

    pointer["run_id"] = "discarded"
    path = live / "discarded.json"
    path.write_text(json.dumps(pointer))
    assert read()["fixture"]["work"]["attempts"] == 2
    path.unlink()
    assert read()["fixture"]["work"]["attempts"] == 1
    assert aggregations[0] == 7


def test_narrowed_count_reuses_indexed_query(tmp_path: Path, monkeypatch) -> None:
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(runs, "live_dir", lambda: live)
    monkeypatch.setattr(ledger, "index_stamp", lambda *_args: [0])
    calls = [0]

    def headers(*_args):
        calls[0] += 1
        return {"runs": []}, 0

    monkeypatch.setattr(ledger, "indexed_headers", headers)
    monkeypatch.setattr(ledger, "_run_index_path", lambda *_args: tmp_path / "absent")
    monkeypatch.setattr(
        mcp_views,
        "section_attempts_by_plan",
        lambda *_args, **_kwargs: {"fixture": {"work": {"attempts": 2}}},
    )
    for _ in range(10):
        assert (
            mcp_views.section_attempt_count("sample", "fixture", "work", tmp_path) == 2
        )
    assert calls[0] == 1


def test_newest_run_replacement_invalidates_cache(tmp_path: Path, monkeypatch) -> None:
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(runs, "live_dir", lambda: live)
    monkeypatch.setattr(ledger, "index_stamp", lambda *_args: [0])
    row = {
        "project": "sample",
        "role": "implement",
        "run_id": "finished",
        "plan": "fixture",
        "section": "work",
        "gate": "passed",
    }
    monkeypatch.setattr(ledger, "load", lambda *_args: ({"runs": [row]}, 0))
    run_file = ledger.ledger_path("sample", tmp_path).parent / "runs" / "finished.json"
    run_file.parent.mkdir(parents=True)
    run_file.write_text(json.dumps(row))
    first = mcp_views.section_attempts_by_plan("sample", tmp_path)
    assert first["fixture"]["work"]["attempt_outcomes"][0]["status"] == "promoted"

    stamp = run_file.stat().st_mtime_ns
    row["gate"] = "failed"
    run_file.write_text(json.dumps(row))
    os.utime(run_file, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    second = mcp_views.section_attempts_by_plan("sample", tmp_path)
    assert second["fixture"]["work"]["attempt_outcomes"][0]["status"] == "failed"
