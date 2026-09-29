"""The live-pointer writer delegates to the shared atomic JSON writer.

``reckon/crew/runs.py:_write_json`` no longer builds its own temporary, fsyncs
it and renames it; it stamps the pointer's launch host and hands the payload to
``reckon/_store.py:write_json_atomically``. The behaviour a reader depends on
must not move: identical bytes, the same private mode, and no sibling temporary
left behind. Each expectation below is produced by a faithful replica of the
pre-migration writer, so the assertion is against the base revision's output
rather than against a value this change chose.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import runs

PLAIN: dict[str, Any] = {"run_id": "r-1", "phase": "running", "attempt": 1}
NON_ASCII: dict[str, Any] = {"node": "café-Ω", "note": "naïve"}
NESTED: dict[str, Any] = {
    "z": [1, 2, {"b": [True, None, 3.5]}],
    "a": {"café": [], "empty": {}},
}


def _base_revision_write(path: Path, payload: Any) -> None:
    """The writer as it stood before the migration onto the shared helper.

    A ``NamedTemporaryFile`` sibling, json-dumped with the same serialisation
    arguments, flushed and fsynced, renamed over the destination and removed on
    any failure.
    """
    payload = runs._stamp_pointer_launch_host(path, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _umask_0o022() -> Iterator[None]:
    """Pin the umask so a recorded mode is a fact about the writer, not the host."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _leftover_temporaries(directory: Path) -> list[str]:
    """Every sibling temporary left anywhere in the directory."""
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name.startswith(".") and entry.name.endswith(".tmp")
    )


@pytest.mark.parametrize(
    "payload",
    [PLAIN, NON_ASCII, NESTED],
    ids=["plain", "non-ascii", "nested"],
)
def test_bytes_and_mode_match_the_base_revision_writer(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"

    _base_revision_write(base, payload)
    runs._write_json(head, payload)

    assert head.read_bytes() == base.read_bytes()
    assert stat.S_IMODE(base.stat().st_mode) == 0o600
    assert stat.S_IMODE(head.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "base.json",
        "head.json",
    ]
    assert _leftover_temporaries(tmp_path) == []


def test_non_ascii_is_escaped_exactly_as_the_base_writer_escaped_it(
    tmp_path: Path,
) -> None:
    """The default ``ensure_ascii`` is kept: non-ASCII arrives as escapes.

    The byte assertion below is what a caller dropping ``ensure_ascii`` would
    fail, because the shared writer would then emit the raw UTF-8 characters
    ``b"\\xc3\\xa9"`` and ``b"\\xce\\xa9"`` instead of their ``\\uXXXX`` forms.
    """
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"
    _base_revision_write(base, NON_ASCII)
    runs._write_json(head, NON_ASCII)

    data = head.read_bytes()
    assert json.loads(data) == NON_ASCII
    assert b"\\u00e9" in data and b"\\u03a9" in data
    assert "é".encode() not in data
    assert "Ω".encode() not in data
    assert data == base.read_bytes()


def test_pointer_writer_still_stamps_the_launch_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly created pointer records where its process was launched."""
    monkeypatch.setattr(runs, "live_dir", lambda: tmp_path)
    pointer = tmp_path / "r-1.json"

    runs._write_json(pointer, dict(PLAIN))

    assert json.loads(pointer.read_text())["launcher_host"] == socket.gethostname()


def test_existing_pointer_carries_forward_its_recorded_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rewriting a pointer that already names a host keeps that host."""
    monkeypatch.setattr(runs, "live_dir", lambda: tmp_path)
    pointer = tmp_path / "r-1.json"
    pointer.write_text(json.dumps({"run_id": "r-1", "launcher_host": "elsewhere"}))

    runs._write_json(pointer, {"run_id": "r-1"})

    assert json.loads(pointer.read_text())["launcher_host"] == "elsewhere"
