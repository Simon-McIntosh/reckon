"""An unchanged live listing decodes no pointer.

Every monitor pass lists the live directory, and each listing used to decode
every pointer it returned even though ``_pointer_text`` already re-read only
the pointers whose stat identity had moved. The record decoded from that text
is now memoised by ``reckon.file_memo`` — the repository's one owner of
parse-per-stat-identity — so a listing whose population has not moved costs no
decode at all, while a pointer that changed, appeared or vanished is decoded or
dropped once.

The counters come from a stand-in installed as the listing's own ``json``
module, so they count the module's decodes rather than the test's own reads of
the same files. Every listing's content is compared with the files decoded
directly, which is what the base revision's listing returns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import file_memo
from reckon._store import write_json_atomically
from reckon.crew import runs

POINTERS = 300


class _Decodes:
    """Every document the listing module decodes while this stand-in is in it."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.texts: list[str] = []

    def loads(self, text: str, *args, **kwargs):
        self.texts.append(text)
        return self._real.loads(text, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _write(directory: Path, run_id: str, **fields) -> Path:
    path = directory / f"{run_id}.json"
    write_json_atomically(
        path,
        {
            "run_id": run_id,
            "project": "p",
            "phase": "working",
            "node_id": f"node-{run_id}",
            "dispatch_options": {"overrides": ["--set", "model=default"]},
            **fields,
        },
    )
    return path


def _listed_by_the_files(directory: Path) -> list[dict]:
    """The listing decoded straight off the files, as the base revision returns it."""
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


@pytest.fixture()
def live(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runs, "_POINTER_TEXT", {}, raising=False)
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(POINTERS):
        _write(directory, f"r-{index:03d}", phase="working")
    return directory


@pytest.fixture()
def decodes(monkeypatch) -> _Decodes:
    stand_in = _Decodes(json)
    monkeypatch.setattr(runs, "json", stand_in)
    return stand_in


def test_an_unchanged_listing_decodes_nothing_and_keeps_its_records_apart(
    live, decodes
):
    first = runs._list_live_records(project="p")
    assert len(first) == POINTERS
    assert len(decodes.texts) == POINTERS, "the stand-in saw no decodes to count"
    assert first == _listed_by_the_files(live)

    decodes.texts.clear()
    second = runs._list_live_records(project="p")
    assert not decodes.texts, (
        f"an unchanged listing decoded {len(decodes.texts)} pointers"
    )
    assert second == _listed_by_the_files(live)

    # A caller's change to a record it was handed reaches neither the kept
    # record nor the next listing.
    second[0]["dispatch_options"]["overrides"].append("--set model=mutated")
    second[0]["phase"] = "mutated by a caller"

    decodes.texts.clear()
    changed = _write(live, "r-010", phase="complete")
    assert changed.exists()
    _write(live, "r-300", phase="queued")
    (live / "r-299.json").unlink()

    third = runs._list_live_records(project="p")
    assert len(decodes.texts) <= 2, (
        f"a listing of one rewrite, one addition and one removal decoded "
        f"{len(decodes.texts)} pointers"
    )
    assert third == _listed_by_the_files(live)

    kept = next(row for row in third if row["run_id"] == "r-000")
    assert kept["phase"] == "working"
    assert kept["dispatch_options"] == {"overrides": ["--set", "model=default"]}


def test_the_pointer_records_are_kept_by_the_one_shared_memo(live, decodes):
    """The record store is ``file_memo``'s, not a second cache beside it."""
    runs._list_live_records(project="p")
    decodes.texts.clear()

    file_memo.clear()

    assert len(runs._list_live_records(project="p")) == POINTERS
    assert len(decodes.texts) == POINTERS, (
        "the records survived a clear of the shared memo, so the listing keeps "
        "a parse cache of its own"
    )
