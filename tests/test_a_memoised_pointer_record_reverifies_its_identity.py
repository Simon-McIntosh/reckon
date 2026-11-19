"""A memoised pointer record is stored under the identity of the text it parsed.

A listing fetches a pointer's text — stat, then read — and hands that text to
``file_memo``, which stores the decoded record under the file's stat identity.
Pointer writers replace a file by rename, so a stat taken inside the store can
already describe the replacing file: the record of the old text was then kept
under the new file's identity, and every later listing that found the new file
unchanged answered with the old record until the pointer moved again.

The listing now hands the store the identity its text was read at, and the
store keeps nothing when the file has moved past it, so a rename landing
between the text fetch and the store costs one listing's staleness at most.
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


@pytest.fixture()
def live(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runs, "_POINTER_TEXT", {}, raising=False)
    file_memo.clear()
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture()
def decodes(monkeypatch) -> _Decodes:
    stand_in = _Decodes(json)
    monkeypatch.setattr(runs, "json", stand_in)
    return stand_in


def _write(directory: Path, run_id: str, **fields) -> Path:
    """Write one live pointer the way its writers do: a temporary renamed over it."""
    path = directory / f"{run_id}.json"
    write_json_atomically(path, {"run_id": run_id, "project": "p", **fields})
    return path


def _phases(records) -> dict[str, str]:
    return {record["run_id"]: record["phase"] for record in records}


def test_the_listing_after_a_rename_returns_the_new_record(live, monkeypatch):
    """A rename between the text fetch and the memo store leaves no stale record.

    The stand-in performs the rename where the store is entered, which is the
    one point between the listing's text fetch and the memo's own stat. The
    listing that meets it may answer from either file — it read the old text
    and the new file arrived under it — but the next listing must answer from
    the file the rename left in place.
    """
    target = _write(live, "r-target", phase="working")
    _write(live, "r-other", phase="working")
    before = target.stat()

    first = _phases(runs._list_live_records(project="p"))
    assert first == {"r-other": "working", "r-target": "working"}

    fired: list[str] = []
    real_memoized = file_memo.memoized

    def renaming_memoized(kind, path, compute, **options):
        if Path(path) == target and not fired:
            fired.append("r-target")
            _write(live, "r-target", phase="complete")
        return real_memoized(kind, path, compute, **options)

    monkeypatch.setattr(file_memo, "memoized", renaming_memoized)

    during = _phases(runs._list_live_records(project="p"))
    assert fired == ["r-target"], (
        "the stand-in never reached the target pointer's memo store, so this "
        "listing did not meet the rename it measures"
    )
    replaced = target.stat()
    assert replaced.st_ino != before.st_ino, (
        "the stand-in did not replace the pointer by rename"
    )
    assert json.loads(target.read_text())["phase"] == "complete"
    assert during["r-target"] in {"working", "complete"}, during

    after = _phases(runs._list_live_records(project="p"))
    assert after["r-target"] == "complete", (
        "the listing after the rename still answers from the replaced file's "
        "text, so the record was stored under an identity taken after the read"
    )
    assert after["r-other"] == "working"


def test_an_unchanged_listing_over_three_hundred_pointers_decodes_none(
    live, decodes
):
    """The identity the store is handed keeps an unchanged pass decode-free."""
    for index in range(POINTERS):
        _write(live, f"r-{index:03d}", phase="working")

    first = runs._list_live_records(project="p")
    assert len(first) == POINTERS
    assert len(decodes.texts) == POINTERS, (
        "the stand-in saw no decodes to count, so this run cannot show an "
        "unchanged pass decoding none"
    )

    decodes.texts.clear()
    second = runs._list_live_records(project="p")
    assert decodes.texts == [], (
        f"an unchanged listing decoded {len(decodes.texts)} pointers"
    )
    assert second == [json.loads(path.read_text()) for path in sorted(live.glob("*.json"))]


def test_a_rename_at_the_store_is_the_only_pointer_decoded_twice(
    live, decodes, monkeypatch
):
    """The pointer that met the rename is decoded once more; the rest are not."""
    target = _write(live, "r-target", phase="working")
    for index in range(POINTERS - 1):
        _write(live, f"r-{index:03d}", phase="working")
    runs._list_live_records(project="p")

    real_memoized = file_memo.memoized
    fired: list[str] = []

    def renaming_memoized(kind, path, compute, **options):
        if Path(path) == target and not fired:
            fired.append("r-target")
            _write(live, "r-target", phase="complete")
        return real_memoized(kind, path, compute, **options)

    decodes.texts.clear()
    monkeypatch.setattr(file_memo, "memoized", renaming_memoized)
    runs._list_live_records(project="p")

    assert fired == ["r-target"]
    decodes.texts.clear()
    after = runs._list_live_records(project="p")
    assert len(decodes.texts) == 1, (
        f"the listing after one rename decoded {len(decodes.texts)} pointers"
    )
    assert _phases(after)["r-target"] == "complete"
