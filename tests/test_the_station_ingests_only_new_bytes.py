"""The station reads only the bytes a stream gained, and refolds a rewrite.

Each case here replays recorded worker streams the way a live station sees
them: bytes arrive a chunk at a time, a partial record waits for the bytes that
complete it, and a whole fold of the finished file is the answer the
incremental fold must reach. The five streams are real claude stream-json
shapes -- init, assistant turns carrying prompt usage, a rate-limit reading and
a final result -- so the fold's carried state (event count, peak prompt,
malformed count, the result's rate) is genuinely built across ticks rather than
within one.

Four things make a stream refold rather than extend: its inode changed, its
size fell below the consumed offset, its size fell below the size last read, or
the bytes before the consumed offset no longer match the stored anchor. Each is
measured by what the store read -- a refold reads the file from offset zero --
so a guard that is present but never fires is visible here.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from reckon import _backends
from reckon.crew import station_store

ANCHOR = station_store.ANCHOR_BYTES


# ── recorded stream shapes ───────────────────────────────────────────────


def _init(session: str, model: str = "claude-sonnet-5-5") -> dict:
    return {"type": "system", "subtype": "init", "session_id": session, "model": model}


def _turn(text: str, prompt: int, output: int, cached: int = 0) -> dict:
    return {
        "type": "assistant",
        "session_id": "s",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": prompt,
                "output_tokens": output,
                "cache_read_input_tokens": cached,
            },
        },
    }


def _limits(utilisation: float) -> dict:
    return {
        "type": "rate_limit_event",
        "session_id": "s",
        "rate_limit_info": {
            "status": "allowed_warning",
            "rateLimitType": "five_hour",
            "utilization": utilisation,
        },
    }


def _result(seconds: float, output: int, prompt: int) -> dict:
    return {
        "type": "result",
        "session_id": "s",
        "subtype": "success",
        "is_error": False,
        "result": "delivered",
        "duration_ms": int(seconds * 1000),
        "duration_api_ms": int(seconds * 800),
        "usage": {
            "input_tokens": prompt,
            "output_tokens": output,
            "cache_read_input_tokens": 0,
        },
        "modelUsage": {
            "claude-sonnet-5-5": {
                "inputTokens": prompt,
                "outputTokens": output,
                "cacheReadInputTokens": 0,
                "contextWindow": 200000,
            }
        },
        "total_cost_usd": 0.12,
    }


# Five recorded runs. The turn counts and prompt sizes differ, so the carried
# peak prompt and the result's rate are stream-specific rather than shared.
RECORDED_RUNS: dict[str, list[str]] = {
    "r-a": [
        _init("sess-a"),
        _turn("reading the store", 41000, 120),
        _turn("editing the ingest", 52000, 340, cached=9000),
        _limits(0.42),
        _turn("running the gate", 61000, 900),
        _result(310.5, 1400, 61000),
    ],
    "r-b": [
        _init("sess-b", "claude-opus-5-5"),
        _turn("planning", 12000, 80),
        _result(84.0, 260, 12000),
    ],
    "r-c": [
        _init("sess-c"),
        _turn("dispatch", 30000, 150),
        # An interrupted write that left a half record in the stream.
        '{"type": "assistant", "message": {"content": [{"type": "te',
        _turn("resumed", 34000, 210),
        _turn("verifying", 38000, 480),
        _result(144.25, 700, 38000),
    ],
    "r-d": [
        _init("sess-d", "claude-haiku-5-5"),
        _turn("quick check", 8000, 40),
        _turn("answer", 9000, 55),
        _result(12.5, 95, 9000),
    ],
    "r-e": [
        _init("sess-e"),
        _turn("fleet sweep", 70000, 300, cached=25000),
        _limits(0.71),
        _turn("second pass", 76000, 350, cached=30000),
        _limits(0.88),
        _turn("third pass", 82000, 420, cached=40000),
        _turn("final pass", 88000, 510, cached=45000),
        _result(402.0, 1900, 88000),
    ],
}


def _payload(lines: list[str | dict]) -> bytes:
    return (
        "\n".join(
            json.dumps(line) if isinstance(line, dict) else line for line in lines
        )
        + "\n"
    ).encode("utf-8")


def _fixture_home(root: Path) -> tuple[Path, dict[str, Path]]:
    """A crew home whose five live pointers each name one recorded stream."""
    home = root / "crew-home"
    (home / "live").mkdir(parents=True)
    streams: dict[str, Path] = {}
    for run_id in RECORDED_RUNS:
        stream = home / f"{run_id}.jsonl"
        stream.write_bytes(b"")
        streams[run_id] = stream
        record = {
            "run_id": run_id,
            "project": "reckon",
            "session": "sess-replay",
            "phase": "working",
            "dialect": "claude",
            "backend": "claude",
            "log_path": str(stream),
        }
        (home / "live" / f"{run_id}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    return home, streams


def _full_fold(path: Path) -> dict:
    """One fold of every whole record in the file, from no carried state.

    A trailing partial record is not yet a record, so it is outside the fold.
    """
    payload = path.read_bytes()
    end = payload.rfind(b"\n")
    lines = payload[: end + 1].decode("utf-8").splitlines() if end >= 0 else []
    return _backends.observe_stream(
        backend_name="claude",
        backend={"command": "claude", "dialect": "claude"},
        lines=lines,
        state=None,
    ).as_dict()


class _Reader:
    """Counts every byte the store reads, per stream path and offset."""

    def __init__(self) -> None:
        self._real = station_store._read_range
        self.reads: dict[str, list[tuple[int, int]]] = {}

    def __call__(self, path, offset, limit=None):
        data = self._real(path, offset, limit)
        self.reads.setdefault(str(path), []).append((offset, len(data)))
        return data

    def bytes_for(self, path: Path) -> int:
        return sum(length for _offset, length in self.reads.get(str(path), []))

    def offsets_for(self, path: Path) -> list[int]:
        return [offset for offset, _length in self.reads.get(str(path), [])]

    def clear(self) -> None:
        self.reads.clear()


def _store(tmp_path: Path) -> station_store.StationStore:
    store = station_store.StationStore(tmp_path / "station")
    store.connect()
    return store


def _observation(store: station_store.StationStore, path: Path) -> dict:
    return store.observation_rows()[str(path)]


def _record(store: station_store.StationStore, path: Path) -> dict:
    for row in store.stream_rows():
        if str(row["path"]) == str(path):
            return row
    raise AssertionError(f"{path} is not stored")


def _chunks(payload: bytes, sizes: list[int]):
    """Split ``payload`` into deterministic chunks, mid-record boundaries included."""
    position = 0
    index = 0
    while position < len(payload):
        step = sizes[index % len(sizes)]
        yield payload[position : position + step]
        position += step
        index += 1


# ── the replay: only new bytes are read, and the fold is the full fold ───


def test_five_streams_replayed_in_chunks_read_only_their_new_bytes(
    tmp_path, monkeypatch
):
    home, streams = _fixture_home(tmp_path)
    store = _store(tmp_path)
    reader = _Reader()
    monkeypatch.setattr(station_store, "_read_range", reader)

    payloads = {run_id: _payload(lines) for run_id, lines in RECORDED_RUNS.items()}
    schedule: list[tuple[str, bytes]] = []
    chunkers = {
        run_id: list(_chunks(payload, [7, 1, 61, 113, 3, 251, 19]))
        for run_id, payload in payloads.items()
    }
    while any(chunkers.values()):
        schedule.extend(
            (run_id, chunkers[run_id].pop(0))
            for run_id in RECORDED_RUNS
            if chunkers[run_id]
        )

    sizes = dict.fromkeys(streams, 0)
    partial: dict[str, int] = dict.fromkeys(streams, 0)
    appended: dict[str, int] = {}

    store.tick(home=home)
    reader.clear()

    for run_id, chunk in schedule:
        path = streams[run_id]
        before = sizes[run_id]
        with path.open("ab") as handle:
            handle.write(chunk)
        sizes[run_id] = before + len(chunk)
        appended = {run_id: len(chunk)}
        store.tick(home=home)

        for other in streams:
            if other != run_id:
                assert reader.bytes_for(streams[other]) == 0, (
                    f"{other} grew nothing yet {reader.bytes_for(streams[other])} "
                    "bytes were read from it"
                )
        allowed = appended[run_id] + ANCHOR + partial[run_id]
        assert reader.bytes_for(path) <= allowed, (
            f"{run_id} read {reader.bytes_for(path)} bytes; at most {allowed} "
            f"were new ({appended[run_id]} appended, {partial[run_id]} partial "
            f"carried, {ANCHOR} anchor)"
        )
        partial[run_id] = sizes[run_id] - int(_record(store, path)["consumed"])
        reader.clear()

    for run_id, path in streams.items():
        assert _observation(store, path) == _full_fold(path), (
            f"{run_id}'s incremental fold differs from a full fold"
        )
        assert int(_record(store, path)["consumed"]) == sizes[run_id]


def test_a_stream_whose_size_did_not_move_is_not_read(tmp_path, monkeypatch):
    home, streams = _fixture_home(tmp_path)
    path = streams["r-b"]
    # A complete record followed by the first half of the next one.
    whole = _payload(RECORDED_RUNS["r-b"][:2])
    half = _payload(RECORDED_RUNS["r-b"][2:])[:40]
    path.write_bytes(whole + half)

    store = _store(tmp_path)
    reader = _Reader()
    monkeypatch.setattr(station_store, "_read_range", reader)

    store.tick(home=home)
    consumed = int(_record(store, path)["consumed"])
    assert consumed == len(whole)
    reader.clear()

    store.tick(home=home)
    assert reader.bytes_for(path) == 0
    assert int(_record(store, path)["consumed"]) == consumed

    with path.open("ab") as handle:
        handle.write(_payload(RECORDED_RUNS["r-b"][2:])[40:])
    store.tick(home=home)
    assert reader.bytes_for(path) > 0
    assert _observation(store, path) == _full_fold(path)


# ── a stream that changed underneath the store refolds from its start ────


def _seeded(
    tmp_path: Path, monkeypatch
) -> tuple[station_store.StationStore, Path, _Reader, Path]:
    home, streams = _fixture_home(tmp_path)
    path = streams["r-d"]
    path.write_bytes(_payload(RECORDED_RUNS["r-d"]))
    store = _store(tmp_path)
    reader = _Reader()
    monkeypatch.setattr(station_store, "_read_range", reader)
    store.tick(home=home)
    reader.clear()
    return store, path, reader, home


def test_a_changed_inode_is_refolded_from_its_start(tmp_path, monkeypatch):
    store, path, reader, _home = _seeded(tmp_path, monkeypatch)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(
        _payload(
            [_init("sess-z"), _turn("fresh run", 5000, 30), _result(3.0, 30, 5000)]
        )
    )
    os.replace(replacement, path)

    store.ingest_stream(path)

    assert _observation(store, path) == _full_fold(path)
    assert reader.offsets_for(path) == [0]
    assert reader.bytes_for(path) == path.stat().st_size


def test_a_size_below_the_consumed_offset_is_refolded(tmp_path, monkeypatch):
    store, path, reader, _home = _seeded(tmp_path, monkeypatch)
    consumed = int(_record(store, path)["consumed"])
    path.write_bytes(_payload(RECORDED_RUNS["r-d"][:2]))
    assert path.stat().st_size < consumed

    store.ingest_stream(path)

    assert _observation(store, path) == _full_fold(path)
    assert reader.offsets_for(path) == [0]


def test_a_size_below_the_last_read_size_is_refolded(tmp_path, monkeypatch):
    home, streams = _fixture_home(tmp_path)
    path = streams["r-b"]
    # One whole record plus a partial tail: the consumed offset stays put while
    # the last-read size covers the tail.
    whole = _payload(RECORDED_RUNS["r-b"][:2])
    path.write_bytes(whole + b'{"type": "assistant", "message": {}}')
    store = _store(tmp_path)
    reader = _Reader()
    monkeypatch.setattr(station_store, "_read_range", reader)
    store.tick(home=home)
    assert int(_record(store, path)["consumed"]) == len(whole)
    reader.clear()

    # The tail is replaced by a shorter one, so the size falls below the size
    # last read while the bytes before the consumed offset are untouched.
    path.write_bytes(whole + b'{"ty')
    assert path.stat().st_size < len(whole) + len(
        b'{"type": "assistant", "message": {}}'
    )
    store.ingest_stream(path)

    assert _observation(store, path) == _full_fold(path)
    assert reader.offsets_for(path) == [0]
    assert reader.bytes_for(path) == path.stat().st_size


def test_a_moved_tail_anchor_refolds_a_grown_stream(tmp_path, monkeypatch):
    store, path, reader, _home = _seeded(tmp_path, monkeypatch)
    before = int(_record(store, path)["size"])
    consumed = int(_record(store, path)["consumed"])
    # A rewrite that keeps the inode and grows past the stored size, with the
    # prefix replaced: only the anchor can see it.
    rewritten = _payload(
        [
            _init("sess-r"),
            _turn("rewritten run from the top", 21000, 90),
            _turn("rewritten again with more of the record kept", 23000, 140),
            _turn("a third turn after the rewrite replaced the prefix", 25000, 180),
            _turn("and a fourth so the file grows past its old size", 27000, 220),
            _limits(0.63),
            _turn("running the rewritten gate", 29000, 260),
            _turn("verifying the rewritten result", 31000, 300),
            _result(220.0, 640, 31000),
        ]
    )
    assert len(rewritten) > before
    path.write_bytes(rewritten)
    assert path.stat().st_size > consumed

    store.ingest_stream(path)

    assert _observation(store, path) == _full_fold(path)
    # The anchor was compared, then the file was folded from its start.
    assert reader.offsets_for(path)[-1] == 0
    assert reader.bytes_for(path) >= len(rewritten)


def test_a_grown_stream_with_a_matching_anchor_is_not_refolded(tmp_path, monkeypatch):
    home, streams = _fixture_home(tmp_path)
    path = streams["r-e"]
    path.write_bytes(_payload(RECORDED_RUNS["r-e"][:2]))
    store = _store(tmp_path)
    reader = _Reader()
    monkeypatch.setattr(station_store, "_read_range", reader)
    store.tick(home=home)
    consumed = int(_record(store, path)["consumed"])
    reader.clear()

    with path.open("ab") as handle:
        handle.write(_payload(RECORDED_RUNS["r-e"][2:]))
    store.ingest_stream(path)

    offsets = reader.offsets_for(path)
    assert offsets and offsets[-1] == consumed
    assert len(offsets) == 2  # the anchor and the appended tail, nothing else
    assert _observation(store, path) == _full_fold(path)


# ── the store's own bookkeeping ─────────────────────────────────────────


def test_writing_events_leaves_no_connections_open(tmp_path):
    store = _store(tmp_path)
    store.write_event(audience="warm", at=_moment())
    before = len(os.listdir("/proc/self/fd"))
    for index in range(50):
        store.write_event(audience="warm", at=_moment(), run=f"r{index}")
    after = len(os.listdir("/proc/self/fd"))
    assert after == before


def test_the_duplicate_constructors_are_gone():
    root = Path(station_store.__file__).resolve().parent.parent
    hits = [
        f"{source}: {name}"
        for source in sorted(root.rglob("*.py"))
        for name in ("def store_at_runtime_root", "def default_store")
        if name in source.read_text(encoding="utf-8")
    ]
    assert hits == []


def _moment():
    return datetime.now(tz=UTC)
