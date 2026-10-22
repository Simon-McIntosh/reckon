"""A live pointer is opened again only when its file has changed.

Every follower and producer lists the live pointers on every pass, and on a
shared filesystem re-reading each one every second was nearly all of a
follower's I/O. A pointer whose stat identity is unchanged is served from the
text last read; a replaced, rewritten or removed pointer is read afresh, and
every caller still receives a dict of its own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon._store import write_json_atomically
from reckon.crew import runs


@pytest.fixture()
def live(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runs, "_POINTER_TEXT", {}, raising=False)
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write(directory: Path, run_id: str, **fields) -> Path:
    path = directory / f"{run_id}.json"
    write_json_atomically(path, {"run_id": run_id, "project": "p", **fields})
    return path


@pytest.fixture()
def opened(monkeypatch) -> list[str]:
    """Record every pointer whose text is actually read from disk."""
    names: list[str] = []
    real = Path.read_text

    def counting(self, *args, **kwargs):
        names.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return names


def test_an_unchanged_pointer_is_not_read_again(live, opened):
    _write(live, "r-a", phase="working")
    _write(live, "r-b", phase="working")

    first = runs._list_live_records(project="p")
    for _ in range(5):
        again = runs._list_live_records(project="p")
    runs.read_pointer("r-a")

    assert sorted(opened) == ["r-a.json", "r-b.json"], opened
    assert again == first


def test_a_replaced_pointer_is_read_afresh(live, opened):
    _write(live, "r-a", phase="working")
    runs._list_live_records(project="p")

    _write(live, "r-a", phase="complete")

    assert [r["phase"] for r in runs._list_live_records(project="p")] == ["complete"]
    assert runs.read_pointer("r-a")["phase"] == "complete"
    assert opened.count("r-a.json") == 2, opened


def test_a_pointer_rewritten_in_place_is_read_afresh(live, opened):
    path = _write(live, "r-a", phase="working")
    runs._list_live_records(project="p")
    before = path.stat()

    path.write_text(json.dumps({"run_id": "r-a", "project": "p", "phase": "x"}))
    later = before.st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(later, later))

    assert runs.read_pointer("r-a")["phase"] == "x"


def test_a_removed_pointer_is_forgotten(live):
    path = _write(live, "r-a", phase="working")
    runs._list_live_records(project="p")
    path.unlink()

    assert runs._list_live_records(project="p") == []
    with pytest.raises(runs.CrewError, match="no live run"):
        runs.read_pointer("r-a")
    assert str(path) not in runs._POINTER_TEXT


def test_each_caller_receives_its_own_record(live):
    _write(live, "r-a", phase="working")
    first = runs._list_live_records(project="p")
    first[0]["phase"] = "mutated by a caller"

    assert runs._list_live_records(project="p")[0]["phase"] == "working"
    assert runs.read_pointer("r-a")["phase"] == "working"
