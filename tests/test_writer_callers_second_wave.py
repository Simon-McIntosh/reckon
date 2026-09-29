"""The second wave of callers reproduces what it wrote before the migration.

Every literal in this file was recorded against the pre-migration writers by
driving the base caller and reading the file it produced, under the umask this
module sets and restores (``0o022``), for one fixed payload. The migration routes those callers
through the shared atomic JSON writer, which is only a correct refactor if each
caller's serialisation, permission bits, fsync behaviour and locking are
unchanged -- so the recorded bytes and modes are asserted here rather than
trusted to the new call.

One recorded difference is deliberate and stated where it appears: the shared
writer terminates every document with a newline, which the compact checkpoint
record did not carry. The document itself is byte-identical.

The delegation tests patch the shared writer on the caller's own module and
assert the caller asks for it with the parameters its recorded output implies.
Patching a name a module does not carry is an AttributeError, so these also
state the landing itself -- a caller that still owns its own temporary-and-rename
cannot pass them.

Two callers are retained exceptions, asserted here so a later "cleanup" that
moves them cannot pass silently:

* ``follow_checkpoint._replace_atomically`` writes the pane's history, a log of
  JSON *lines* rewritten as a whole file. The shared writer emits exactly one
  JSON value plus a newline, so it cannot express that file.
* ``follow_checkpoint.append_history`` appends one line per row and only
  rewrites through that helper once the log has outgrown its cap.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

UMASK = 0o022

CANONICAL_PAYLOAD = {"z": 1, "a": {"n": [1, 2]}, "m": "text", "b": None}

# Recorded from the base writers under umask 0o022: json.dumps with indent=2 and
# sorted keys, terminated by one newline.
CANONICAL_INDENT_SORTED = (
    b"{\n"
    b'  "a": {\n'
    b'    "n": [\n'
    b"      1,\n"
    b"      2\n"
    b"    ]\n"
    b"  },\n"
    b'  "b": null,\n'
    b'  "m": "text",\n'
    b'  "z": 1\n'
    b"}\n"
)

# The same payload with insertion order preserved (indent=2, no sort_keys).
CANONICAL_INDENT_INSERTION = (
    b"{\n"
    b'  "z": 1,\n'
    b'  "a": {\n'
    b'    "n": [\n'
    b"      1,\n"
    b"      2\n"
    b"    ]\n"
    b"  },\n"
    b'  "m": "text",\n'
    b'  "b": null\n'
    b"}\n"
)

# Recorded compact checkpoint document: json.dumps(record, sort_keys=True) with
# no trailing newline.
FC_WRITE_BASE = (
    b'{"offset": 5, "project": "proj", "reported": {"run": "s1"}, '
    b'"session": "sess", "stream_identity": {"dev": 1, "ino": 2}, '
    b'"stream_path": "/nonexistent/stream", "version": 1}'
)

SUPERSEDED_NOTE = (
    "This aggregate is no longer authoritative. Sprints, milestones, blockers "
    "and the timeline are independently versioned resources; read those. It is "
    "retained as a record of the state at migration, and its contents are frozen "
    "at that moment rather than maintained."
)


@pytest.fixture
def recording_umask():
    previous = os.umask(UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def _written(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _fsync_kinds(monkeypatch) -> list[int | None]:
    """Record the file type behind each ``os.fsync`` call, then delegate."""
    kinds: list[int | None] = []
    real = os.fsync

    def recording(fd):
        try:
            kinds.append(stat.S_IFMT(os.fstat(fd).st_mode))
        except OSError:
            kinds.append(None)
        return real(fd)

    monkeypatch.setattr(os, "fsync", recording)
    return kinds


def _capture(monkeypatch, module) -> list[dict]:
    calls: list[dict] = []

    def record(path, payload, **kwargs):
        calls.append({"path": Path(path), "payload": payload, "kwargs": kwargs})
        return Path(path)

    monkeypatch.setattr(module, "write_json_atomically", record)
    return calls


def _drive_checkpoint_write(monkeypatch, target: Path) -> None:
    from reckon.crew import follow_checkpoint

    monkeypatch.setattr(follow_checkpoint, "checkpoint_path", lambda p, s: target)
    follow_checkpoint.write(
        "proj",
        "sess",
        stream_path="/nonexistent/stream",
        offset=5,
        reported={"run": "s1"},
        identity={"dev": 1, "ino": 2},
    )


def _drive_review(monkeypatch, target: Path) -> None:
    from reckon.crew import review

    review._write_record(target, CANONICAL_PAYLOAD)


def _drive_paid_lanes(monkeypatch, target: Path) -> None:
    from reckon.crew import paid_lanes

    paid_lanes.write_document_atomically(CANONICAL_PAYLOAD, target)


def _drive_resumption(monkeypatch, target: Path) -> None:
    from reckon.crew import resumption

    monkeypatch.setattr(
        resumption, "lane_probe_cache_path", lambda project, backend: target
    )
    resumption._write_lane_probe_cache("proj", "be", CANONICAL_PAYLOAD)


def _drive_hooks(monkeypatch, target: Path, original: bytes | None = None) -> None:
    from reckon.hooks import install

    install._write_settings(target, CANONICAL_PAYLOAD, original)


def _drive_stamp(monkeypatch, target: Path) -> None:
    from reckon import project_state

    docs = target.parents[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(CANONICAL_PAYLOAD), encoding="utf-8")
    monkeypatch.setattr(project_state, "datetime", _FrozenDatetime)
    project_state.stamp_legacy_index(docs, "proj", marker={"format": "distributed"})


class _FrozenDatetime(__import__("datetime").datetime):
    """A datetime whose ``now`` is fixed, so a stamped payload is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz)


# --------------------------------------------------------------------------
# Byte-for-byte and mode-for-mode reproduction of the recorded base output.
# --------------------------------------------------------------------------


def test_checkpoint_write_document_unchanged(tmp_path, monkeypatch, recording_umask):
    target = tmp_path / "cp.json"
    _drive_checkpoint_write(monkeypatch, target)

    written, mode = _written(target)
    # The document is byte-identical to the recorded base; the shared writer
    # terminates it with one newline, which the pre-migration writer did not.
    assert written == FC_WRITE_BASE + b"\n"
    assert written.rstrip(b"\n") == FC_WRITE_BASE
    assert mode == 0o644


def test_checkpoint_write_fsyncs_the_directory(tmp_path, monkeypatch, recording_umask):
    kinds = _fsync_kinds(monkeypatch)
    _drive_checkpoint_write(monkeypatch, tmp_path / "cp.json")

    assert stat.S_IFDIR in kinds


def test_replace_atomically_retained_output_unchanged(tmp_path, recording_umask):
    from reckon.crew import follow_checkpoint

    target = tmp_path / "raw.json"
    follow_checkpoint._replace_atomically(target, '{"a": 1}')

    written, mode = _written(target)
    assert written == b'{"a": 1}'
    assert mode == 0o644


def test_append_history_retained_rewrite_unchanged(
    tmp_path, monkeypatch, recording_umask
):
    from reckon.crew import follow_checkpoint

    history = tmp_path / "hist.json"
    monkeypatch.setattr(follow_checkpoint, "history_path", lambda p, s: history)
    for index in range(4):
        follow_checkpoint.append_history(
            "proj",
            "sess",
            text=f"row{index}",
            at=1000.0 + index,
            max_rows=1,
            max_seconds=10_000,
            now=1000.0 + index,
        )

    written, mode = _written(history)
    assert written == (
        b'{"at": 1002.0, "kind": "row", "run_id": "", "state": "", "text": "row2"}\n'
        b'{"at": 1003.0, "kind": "row", "run_id": "", "state": "", "text": "row3"}\n'
    )
    assert mode == 0o644


def test_review_record_output_unchanged(tmp_path, monkeypatch, recording_umask):
    _drive_review(monkeypatch, tmp_path / "rev.json")

    written, mode = _written(tmp_path / "rev.json")
    assert written == CANONICAL_INDENT_SORTED
    assert mode == 0o644


def test_paid_lanes_document_output_unchanged(tmp_path, monkeypatch, recording_umask):
    _drive_paid_lanes(monkeypatch, tmp_path / "doc.json")

    written, mode = _written(tmp_path / "doc.json")
    assert written == CANONICAL_INDENT_INSERTION
    assert mode == 0o600


def test_resumption_probe_cache_output_unchanged(
    tmp_path, monkeypatch, recording_umask
):
    _drive_resumption(monkeypatch, tmp_path / "probe.json")

    written, mode = _written(tmp_path / "probe.json")
    assert written == CANONICAL_INDENT_SORTED
    assert mode == 0o644


def test_hooks_settings_new_file_output_unchanged(
    tmp_path, monkeypatch, recording_umask
):
    _drive_hooks(monkeypatch, tmp_path / "settings.json")

    written, mode = _written(tmp_path / "settings.json")
    assert written == CANONICAL_INDENT_INSERTION
    assert mode == 0o644


def test_hooks_settings_existing_file_keeps_its_mode(
    tmp_path, monkeypatch, recording_umask
):
    target = tmp_path / "settings.json"
    target.write_bytes(CANONICAL_INDENT_INSERTION)
    target.chmod(0o640)

    _drive_hooks(monkeypatch, target, original=target.read_bytes())

    written, mode = _written(target)
    assert written == CANONICAL_INDENT_INSERTION
    assert mode == 0o640


def test_hooks_settings_refuses_a_concurrent_edit(
    tmp_path, monkeypatch, recording_umask
):
    from reckon.hooks import install

    target = tmp_path / "settings.json"
    target.write_bytes(b"original\n")

    with pytest.raises(install.HookInstallError):
        install._write_settings(target, CANONICAL_PAYLOAD, b"a different original")

    assert target.read_bytes() == b"original\n"


def test_project_state_stamp_output_unchanged(tmp_path, monkeypatch, recording_umask):
    target = tmp_path / "docs" / "state" / "proj" / "index.json"
    _drive_stamp(monkeypatch, target)

    written, mode = _written(target)
    assert mode == 0o600
    parsed = json.loads(written)
    assert {key: parsed[key] for key in CANONICAL_PAYLOAD} == CANONICAL_PAYLOAD
    assert parsed["superseded"] == {
        "by": "distributed",
        "at": "2026-01-02T03:04:05+00:00",
        "marker": ".reckon/project-state-migration.json",
        "canonical": ["sprints/", "milestones/", "blockers/", "state/*/timeline.html"],
        "note": SUPERSEDED_NOTE,
    }
    # Recorded serialisation: indent=2, insertion order, one trailing newline.
    assert written == (json.dumps(parsed, indent=2) + "\n").encode()


def test_create_project_state_output_unchanged(tmp_path, monkeypatch, recording_umask):
    from reckon import project_state

    monkeypatch.setattr(project_state, "datetime", _FrozenDatetime)
    docs = tmp_path / "docs"
    docs.mkdir()
    project_state.create_project_state(docs, "proj")

    project_json = docs / "state" / "proj" / "project.json"
    project_bytes, project_mode = _written(project_json)
    assert project_mode == 0o644
    envelope = json.loads(project_bytes)
    assert envelope["updated"] == "2026-01-02T03:04:05+00:00"
    assert envelope["project"] == "proj"
    assert envelope["doc"] == "project"
    assert envelope["data"]["project"] == "proj"
    assert project_bytes == (json.dumps(envelope, indent=2) + "\n").encode()

    marker = docs / ".reckon" / "project-state-migration.json"
    marker_bytes, marker_mode = _written(marker)
    assert marker_mode == 0o600
    parsed = json.loads(marker_bytes)
    assert parsed["format"] == "distributed"
    assert parsed["completed_at"] == "2026-01-02T03:04:05+00:00"
    assert [row["type"] for row in parsed["resources"]] == ["project", "timeline"]
    for row in parsed["resources"]:
        assert row["sha256"] == project_state._sha256_path(docs / row["path"])
    assert marker_bytes == (json.dumps(parsed, indent=2) + "\n").encode()


# --------------------------------------------------------------------------
# The numeric decoders: one shared decoder, the private helpers gone.
# --------------------------------------------------------------------------

DECODER_CASES = [
    (True, None),
    (False, None),
    (0, 0.0),
    (1.5, 1.5),
    ("1", None),
    (None, None),
    ([1], None),
]


@pytest.mark.parametrize("value, expected", DECODER_CASES)
def test_optional_number_matches_recorded_expectations(value, expected):
    from reckon._observations import optional_number

    assert optional_number(value) == expected


@pytest.mark.parametrize("value, expected", DECODER_CASES)
def test_hold_decodes_through_the_shared_decoder(value, expected):
    from reckon.crew import hold

    assert hold._as_reading({"used_percent": value}).used_percent == expected


@pytest.mark.parametrize("value, expected", DECODER_CASES)
def test_window_reading_decodes_through_the_shared_decoder(value, expected):
    from reckon.crew import window_reading

    event = {
        "type": "rate_limit_event",
        "timestamp": "2026-01-02T03:04:05Z",
        "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": value}}},
    }
    reading = window_reading.read_windows([event])
    figure = reading.figure("five_hour")
    assert (None if figure is None else figure.utilisation) == expected


def test_private_number_helpers_are_gone():
    from reckon.crew import hold, window_reading

    assert not hasattr(hold, "_as_number")
    assert not hasattr(window_reading, "_numeric")


# --------------------------------------------------------------------------
# Delegation: each migrated caller asks the shared writer for what it needs.
# --------------------------------------------------------------------------


def test_checkpoint_write_delegates(tmp_path, monkeypatch):
    from reckon.crew import follow_checkpoint

    calls = _capture(monkeypatch, follow_checkpoint)
    _drive_checkpoint_write(monkeypatch, tmp_path / "cp.json")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] is None
    assert kwargs["sort_keys"] is True
    assert kwargs["mode"] is None
    assert kwargs["fsync"] is True
    assert kwargs["fsync_directory"] is True


def test_review_record_delegates(tmp_path, monkeypatch):
    from reckon.crew import review

    calls = _capture(monkeypatch, review)
    _drive_review(monkeypatch, tmp_path / "rev.json")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["mode"] is None
    assert kwargs["fsync"] is False
    assert kwargs["create_parents"] is False


def test_paid_lanes_delegates(tmp_path, monkeypatch):
    from reckon.crew import paid_lanes

    calls = _capture(monkeypatch, paid_lanes)
    _drive_paid_lanes(monkeypatch, tmp_path / "doc.json")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is False
    assert kwargs["mode"] == 0o600
    assert kwargs["fsync"] is False


def test_resumption_delegates(tmp_path, monkeypatch):
    from reckon.crew import resumption

    calls = _capture(monkeypatch, resumption)
    _drive_resumption(monkeypatch, tmp_path / "probe.json")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["mode"] is None
    assert kwargs["fsync"] is False


def test_hooks_settings_delegates(tmp_path, monkeypatch):
    from reckon.hooks import install

    calls = _capture(monkeypatch, install)
    _drive_hooks(monkeypatch, tmp_path / "settings.json")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is False
    assert kwargs["mode"] is None
    assert kwargs["fsync"] is False


def test_project_state_stamp_delegates(tmp_path, monkeypatch):
    from reckon import project_state

    calls = _capture(monkeypatch, project_state)
    _drive_stamp(monkeypatch, tmp_path / "docs" / "state" / "proj" / "index.json")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is False
    assert kwargs["mode"] == 0o600
    assert kwargs["fsync"] is True
    assert kwargs["fsync_directory"] is True


# --------------------------------------------------------------------------
# A failure mid-write leaves the target holding its old content, no stray temp.
# --------------------------------------------------------------------------


DRIVERS = [
    ("checkpoint_write", _drive_checkpoint_write),
    ("review_record", _drive_review),
    ("paid_lanes_document", _drive_paid_lanes),
    ("resumption_probe", _drive_resumption),
    ("hooks_settings", _drive_hooks),
    ("project_state_stamp", _drive_stamp),
]


@pytest.mark.parametrize("name, driver", DRIVERS, ids=[name for name, _ in DRIVERS])
def test_a_failure_mid_write_leaves_old_content(tmp_path, monkeypatch, name, driver):
    from reckon import _store

    target = {
        "project_state_stamp": tmp_path / "docs" / "state" / "proj" / "index.json",
    }.get(name, tmp_path / "target.json")

    def exploding_dump(obj, handle, **kwargs):
        handle.write("PARTIAL")
        raise RuntimeError("injected failure")

    monkeypatch.setattr(_store.json, "dump", exploding_dump)
    target.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError):
        driver(monkeypatch, target)

    if name == "project_state_stamp":
        # The stamp rewrites a document the driver placed there first.
        assert target.read_bytes() == json.dumps(CANONICAL_PAYLOAD).encode()
    else:
        # No destination existed, so the failed write leaves none behind.
        assert not target.exists()

    # The temporary the shared writer named is removed on the way out.
    assert list(target.parent.glob(f".{target.name}*")) == []
