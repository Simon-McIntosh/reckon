"""The supervisor's run-directory writer delegates to the shared atomic writer.

``reckon/crew/dispatch.py:_supervisor_write`` no longer builds its own sibling
temporary, fsyncs it and renames it; it hands the payload to
``reckon/_store.py:write_json_atomically`` with ``create_parents=False``. Two
guarantees a reader depends on must not move:

* identical bytes and the same private mode, and
* the run directory is never materialised — a write whose directory has gone is
  dropped and reported by the ``False`` return rather than by raising.

Each expectation below is produced by a faithful replica of the pre-migration
writer, so the bytes and mode assertions are against the base revision's output
rather than against a value this change chose.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reckon import _store
from reckon.crew.dispatch import _supervisor_write

NESTED: dict[str, Any] = {
    "run_id": "r-1",
    "attempt": 3,
    "z": [1, 2, "café-Ω", {"b": [True, None, 3.5]}],
    "a": {"nested": {"deep": [], "empty": {}}},
}


def _base_revision_write(path: Path, payload: dict[str, Any]) -> bool:
    """The writer as it stood before the migration onto the shared helper."""
    parent = path.parent
    if not parent.is_dir():
        return False
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
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
    except OSError:
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return True


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


def test_bytes_and_mode_match_the_base_revision_writer(tmp_path: Path) -> None:
    """A nested payload lands byte-identically and at the same private mode."""
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"

    assert _base_revision_write(base, NESTED) is True
    assert _supervisor_write(head, NESTED) is True

    assert head.read_bytes() == base.read_bytes()
    assert json.loads(head.read_text(encoding="utf-8")) == NESTED
    assert stat.S_IMODE(base.stat().st_mode) == 0o600
    assert stat.S_IMODE(head.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "base.json",
        "head.json",
    ]
    assert _leftover_temporaries(tmp_path) == []


def test_absent_run_directory_returns_false_and_creates_nothing(
    tmp_path: Path,
) -> None:
    """A write whose directory is missing is dropped, not materialised."""
    run_dir = tmp_path / "run"
    target = run_dir / "worker.json"

    assert _supervisor_write(target, NESTED) is False

    assert not run_dir.exists()
    assert not target.exists()


def test_directory_removed_between_check_and_write_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory vanishing mid-write is reported, never by resurrecting it.

    The wrapper removes the directory at the instant the writer takes over —
    after ``_supervisor_write``'s ``is_dir`` guard and before the sibling
    temporary is opened — so the call exercises exactly the window the guard
    exists for, against the real shared writer.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "worker.json"
    real = _store.write_json_atomically

    def removing_writer(path: str | Path, payload: Any, **kwargs: Any) -> Path:
        shutil.rmtree(run_dir)
        return real(path, payload, **kwargs)

    monkeypatch.setattr(_store, "write_json_atomically", removing_writer)

    assert _supervisor_write(target, NESTED) is False

    assert not run_dir.exists()
    assert _leftover_temporaries(tmp_path) == []


def test_a_successful_write_leaves_no_sibling_temporary(tmp_path: Path) -> None:
    """The shared writer's temporary is renamed away, not left beside the file."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "worker.json"

    assert _supervisor_write(target, NESTED) is True

    assert _leftover_temporaries(run_dir) == []
