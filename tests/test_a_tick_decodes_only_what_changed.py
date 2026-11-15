"""A tick decodes only what changed.

A station folds the fleet's streams and needs three scalars from each pointer to
do it: the stream it names and the backend and dialect to read it in. Those are
columns written by the pointer's own ingest, which runs only when the pointer
moved, so a tick over a fleet where nothing moved decodes nothing at all -- no
pointer document, no stream byte, no index. One byte appended to one stream is
read once, and the fleet's other documents stay untouched.

The counter wraps ``json.loads`` and ``json.load`` and keeps the text of every
document decoded during the measured window, so an assertion is against the
bytes that were actually decoded rather than against a call count that could be
satisfied by a cheaper decode somewhere else.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import station_store


def _init(session: str) -> dict:
    return {"type": "system", "subtype": "init", "session_id": session}


def _turn(text: str, prompt: int, output: int) -> dict:
    return {
        "type": "assistant",
        "session_id": "s",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": prompt, "output_tokens": output},
        },
    }


def _result(seconds: float, output: int) -> dict:
    return {
        "type": "result",
        "session_id": "s",
        "subtype": "success",
        "is_error": False,
        "result": "delivered",
        "duration_ms": int(seconds * 1000),
        "usage": {"input_tokens": 1000, "output_tokens": output},
    }


RECORDED: dict[str, list[dict]] = {
    "r-a": [_init("s-a"), _turn("reading", 41000, 120), _result(31.5, 1400)],
    "r-b": [_init("s-b"), _turn("planning", 12000, 80), _result(84.0, 260)],
    "r-c": [_init("s-c"), _turn("dispatch", 30000, 150), _result(144.25, 700)],
    "r-d": [_init("s-d"), _turn("verifying", 38000, 480)],
}

BACKENDS = {"r-a": ("claude", "claude"), "r-b": ("codex", "codex")}


def _payload(lines: list[dict]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")


def _fixture_home(root: Path) -> tuple[Path, dict[str, Path]]:
    """A crew home whose pointers each name one recorded stream."""
    home = root / "crew-home"
    (home / "live").mkdir(parents=True)
    streams: dict[str, Path] = {}
    for run_id, lines in RECORDED.items():
        stream = home / f"{run_id}.jsonl"
        stream.write_bytes(_payload(lines))
        streams[run_id] = stream
        backend, dialect = BACKENDS.get(run_id, ("claude", "claude"))
        record = {
            "run_id": run_id,
            "project": "reckon",
            "session": "sess-replay",
            "phase": "working",
            "dialect": dialect,
            "backend": backend,
            "log_path": str(stream),
        }
        (home / "live" / f"{run_id}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    return home, streams


def _pointer_texts(home: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in (home / "live").glob("*.json")
    }


def _stored_documents(home: Path, loads) -> set[str]:
    """The fields documents the store keeps for this home's pointers.

    ``_write_pointer_row`` stores ``json.dumps(record, sort_keys=True)``, so the
    document the store decodes back is this canonical form of each pointer file.
    ``loads`` is the undecorated decoder: building this set must not be recorded
    as a decode of the tick under test.
    """
    return {
        json.dumps(loads(text), sort_keys=True)
        for text in _pointer_texts(home).values()
    }


class _Decodes:
    """Every document decoded while this ledger is installed."""

    def __init__(self, monkeypatch) -> None:
        self.texts: list[str] = []
        self.loads = json.loads
        original_load = json.load

        def counting_loads(data, *args, **kwargs):
            if isinstance(data, str):
                self.texts.append(data)
            elif isinstance(data, bytes):
                self.texts.append(data.decode("utf-8", "replace"))
            return self.loads(data, *args, **kwargs)

        def counting_load(handle, *args, **kwargs):
            position = handle.tell()
            self.texts.append(handle.read())
            handle.seek(position)
            return original_load(handle, *args, **kwargs)

        monkeypatch.setattr(json, "loads", counting_loads)
        monkeypatch.setattr(json, "load", counting_load)

    def clear(self) -> None:
        self.texts.clear()

    def pointer_decodes(self, home: Path) -> list[str]:
        """The decoded documents that are this fleet's stored pointer fields."""
        documents = _stored_documents(home, self.loads)
        return [text for text in self.texts if text in documents]


def _store(tmp_path: Path) -> station_store.StationStore:
    store = station_store.StationStore(tmp_path / "station")
    store.connect()
    return store


class _Reads:
    """Every byte the store reads from a stream file, per path and offset."""

    def __init__(self, monkeypatch) -> None:
        self._real = station_store._read_range
        self.reads: dict[str, list[tuple[int, int]]] = {}

        def counting_read(path, offset, limit=None):
            data = self._real(path, offset, limit)
            self.reads.setdefault(str(path), []).append((offset, len(data)))
            return data

        monkeypatch.setattr(station_store, "_read_range", counting_read)

    def clear(self) -> None:
        self.reads.clear()


def _settled(tmp_path: Path, monkeypatch):
    """A store warm over the fixture's whole fleet, with its ledgers cleared."""
    home, streams = _fixture_home(tmp_path)
    store = _store(tmp_path)
    store.tick(home=home)
    decodes = _Decodes(monkeypatch)
    reads = _Reads(monkeypatch)
    return home, streams, store, decodes, reads


# ── an unchanged fleet costs no decode ───────────────────────────────────


def test_a_tick_over_an_unchanged_fleet_decodes_nothing(tmp_path, monkeypatch):
    home, _streams, store, decodes, reads = _settled(tmp_path, monkeypatch)
    reads.clear()

    store.tick(home=home)

    assert reads.reads == {}, "an unchanged stream was read again"
    assert decodes.texts == [], "the tick decoded documents nothing had changed"


def test_a_tick_over_an_unchanged_fleet_reads_no_pointer_document(
    tmp_path, monkeypatch
):
    """The guard the peak needed: the fleet's fields are not re-decoded per tick."""
    home, _streams, store, decodes, reads = _settled(tmp_path, monkeypatch)
    assert len(_stored_documents(home, decodes.loads)) == len(RECORDED)

    # The instrument is shown to see a decode known to happen: a pointer that
    # moves is decoded by its own ingest, once, and no other document is.
    decodes.clear()
    moved = home / "live" / "r-a.json"
    moved.write_text(
        json.dumps(
            {
                "run_id": "r-a",
                "project": "reckon",
                "session": "sess-replay",
                "phase": "delivered",
                "dialect": "claude",
                "backend": "claude",
                "log_path": str(home / "r-a.jsonl"),
            }
        ),
        encoding="utf-8",
    )
    store.tick(home=home)
    assert decodes.texts == [moved.read_text(encoding="utf-8")], (
        "a moved pointer was not decoded exactly once by its own ingest"
    )

    # And nothing it did not move decodes on the tick after.
    decodes.clear()
    reads.clear()
    store.tick(home=home)
    assert decodes.texts == []


# ── one appended byte, read once ────────────────────────────────────────


def test_one_byte_appended_to_one_stream_is_decoded_once(tmp_path, monkeypatch):
    home, streams, store, decodes, reads = _settled(tmp_path, monkeypatch)
    decodes.clear()
    reads.clear()
    stream = streams["r-a"]
    before = stream.stat().st_size
    with stream.open("ab") as handle:
        handle.write(b"{")

    store.tick(home=home)

    assert set(reads.reads) == {str(stream)}, "another stream was read"
    offsets = reads.reads[str(stream)]
    assert offsets[-1] == (before, 1), "the appended byte was not read at its offset"
    assert all(
        reads_at >= before - station_store.ANCHOR_BYTES for reads_at, _ in offsets
    ), "the stream was folded from its start instead of from its stored offset"
    assert (
        sum(length for _offset, length in offsets) <= station_store.ANCHOR_BYTES + 1
    ), "the tick read more than the anchor check and the appended byte"
    assert decodes.pointer_decodes(home) == [], (
        "the tick decoded the fleet's pointer fields for one appended byte"
    )
    stored = {str(row["path"]): str(row["fold_state"]) for row in store.stream_rows()}
    assert decodes.texts == [stored[str(stream)]], (
        "the tick decoded a document other than the grown stream's own fold state"
    )
