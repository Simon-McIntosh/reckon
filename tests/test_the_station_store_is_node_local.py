"""The station's node-local store: rebuild, retention, root refusal, reuse.

The store is one SQLite database under the node-local runtime root, keyed by
the same stat identity every in-process memo uses. These cases pin the four
things that make it the node's rather than a session's or a shared
filesystem's: a full rebuild equals an incremental one, a day file older than
the retention window leaves with its rows, a root that is not proved node-local
is refused naming why, and an unchanged pointer is never re-parsed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import host
from reckon.crew import station_store


class _Click:
    """A replacement for a named step that counts the calls it receives."""

    def __init__(self, real):
        self._real = real
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return self._real(path)


def _event_line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _fixture_home(root: Path) -> Path:
    """A crew home with three live pointers, two of which name a stream."""
    home = root / "crew-home"
    (home / "live").mkdir(parents=True)
    (home / "runs").mkdir(parents=True)
    for index in (1, 2, 3):
        record = {
            "run_id": f"r{index}",
            "project": "reckon",
            "session": "sess-a",
            "phase": "working",
            "dialect": "claude",
            "backend": "claude",
            "log_path": "",
        }
        if index <= 2:
            stream = home / "runs" / f"r{index}.jsonl"
            stream.write_text(_event_line(f"hello {index}") + "\n", encoding="utf-8")
            record["log_path"] = str(stream)
        (home / "live" / f"r{index}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    return home


def _runtime_root(root: Path) -> Path:
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    return runtime


# ── a rebuild equals an incremental build ────────────────────────────────


def test_a_rebuild_equals_an_incremental_build(tmp_path):
    home = _fixture_home(tmp_path)

    rebuilt = station_store.StationStore(tmp_path / "rebuilt")
    rebuilt.rebuild(home=home)

    incremental = station_store.StationStore(tmp_path / "incremental")
    for _ in (1, 2, 3):
        incremental.tick(home=home)

    assert rebuilt.pointer_rows() == incremental.pointer_rows()
    assert rebuilt.observation_rows() == incremental.observation_rows()
    assert {Path(row["path"]).name for row in rebuilt.stream_rows()} == {
        "r1.jsonl",
        "r2.jsonl",
    }


def test_every_pointer_and_stream_is_ingested(tmp_path):
    home = _fixture_home(tmp_path)
    store = station_store.StationStore(tmp_path / "store")
    store.connect()
    store.tick(home=home)

    assert [row["run_id"] for row in store.pointer_rows()] == ["r1", "r2", "r3"]
    assert all(row["phase"] == "working" for row in store.pointer_rows())
    assert len(store.stream_rows()) == 2
    assert all(row["consumed"] > 0 for row in store.stream_rows())


# ── retention ────────────────────────────────────────────────────────────


def test_a_day_file_older_than_the_window_leaves_with_its_rows(tmp_path):
    store = station_store.StationStore(tmp_path / "store", retention_days=7)
    store.connect()
    now = datetime(2026, 10, 23, tzinfo=UTC)
    old = now - timedelta(days=9)

    store.write_event(audience="sess-a", at=old, run="r1", kind="new")
    store.write_event(audience="sess-a", at=now, run="r2", kind="new")
    assert len(store.day_files()) == 2
    assert [event["run"] for event in store.events(audience="sess-a")] == ["r1", "r2"]

    removed = store.sweep_retention(now=now)

    assert [path.name for path in removed] == [station_store._day_file_name(old.date())]
    remaining = {path.name for path in store.day_files()}
    assert station_store._day_file_name(old.date()) not in remaining
    assert [event["run"] for event in store.events(audience="sess-a")] == ["r2"]
    # Every file that survives belongs to a day inside the window.
    for path in store.day_files():
        day = station_store._day_from_name(path.name)
        assert day is not None and day >= now.date() - timedelta(days=7)


# ── the root must be proved node-local ───────────────────────────────────


@pytest.mark.parametrize("kind", sorted(host._SHARED_FILESYSTEMS))
def test_a_shared_filesystem_root_is_refused_naming_its_type(
    tmp_path, monkeypatch, kind
):
    runtime = _runtime_root(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(host, "_filesystem_type", lambda target, mounts: kind)

    with pytest.raises(station_store.StationStoreError) as refusal:
        station_store.StationStore.at_runtime_root()
    assert kind in str(refusal.value)


def test_an_undeterminable_root_is_refused(tmp_path, monkeypatch):
    runtime = _runtime_root(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(host, "_filesystem_type", lambda target, mounts: None)

    with pytest.raises(
        station_store.StationStoreError, match="could not be determined"
    ):
        station_store.StationStore.at_runtime_root()


def test_a_tmpfs_root_is_accepted(tmp_path, monkeypatch):
    runtime = _runtime_root(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(host, "_filesystem_type", lambda target, mounts: "tmpfs")

    store = station_store.StationStore.at_runtime_root()
    store.connect()
    assert store.database.parent == runtime


def test_a_root_reached_only_through_tmpdir_is_refused(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(host, "_filesystem_type", lambda target, mounts: "tmpfs")

    with pytest.raises(station_store.StationStoreError, match="TMPDIR"):
        station_store.StationStore.at_runtime_root(
            environ={"TMPDIR": str(scratch)},
            run_user_root=tmp_path / "no-such-run-user",
        )


# ── an unchanged pointer is never re-parsed ──────────────────────────────


def test_an_unchanged_pointer_is_not_reparsed(tmp_path, monkeypatch):
    home = _fixture_home(tmp_path)
    store = station_store.StationStore(tmp_path / "store")
    store.connect()

    click = _Click(station_store._read_pointer_record)
    monkeypatch.setattr(station_store, "_read_pointer_record", click)

    store.tick(home=home)
    assert click.calls == 3

    store.tick(home=home)
    assert click.calls == 3

    touched = home / "live" / "r2.json"
    record = json.loads(touched.read_text(encoding="utf-8"))
    record["phase"] = "landed"
    touched.write_text(json.dumps(record), encoding="utf-8")

    store.tick(home=home)
    assert click.calls == 4
    phases = {row["run_id"]: row["phase"] for row in store.pointer_rows()}
    assert phases["r2"] == "landed"
