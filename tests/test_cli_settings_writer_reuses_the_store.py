"""The harness-settings writer delegates to the shared atomic JSON writer.

``reckon/cli.py:_write_json_atomically`` keeps its unchanged-bytes early return
and both concurrent-edit refusals, then hands the payload to
``reckon/_store.py:write_json_atomically``. The behaviour a reader depends on
must not move: identical bytes (non-ASCII left unescaped, key order preserved),
the written file's mode, the two refusals, and no sibling temporary left behind.
Each expectation is produced by a faithful replica of the pre-migration writer,
so the assertion is against the base revision's output rather than a value this
change chose.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import click
import pytest

from reckon import cli

PLAIN: dict[str, Any] = {"run_id": "r-1", "attempt": 1, "phase": "running"}
NON_ASCII_UNSORTED: dict[str, Any] = {"zeta": "café-Ω", "alpha": "café", "beta": 2}

WriteFn = Callable[[Path, dict, Any], None]


def _base_revision_write(path: Path, payload: dict, original: bytes | None) -> None:
    """``cli._write_json_atomically`` exactly as it stood before the migration.

    The serialisation, the early return, both refusals, the pid-and-time named
    sibling temporary, the chmod of that temporary to the revision's mode, and
    the rename over the destination — unchanged from the base revision.
    """
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()
    if encoded == original:
        return
    if original is None:
        if path.exists():
            raise click.ClickException(
                f"harness settings changed while being updated: {path}"
            )
        mode = None
    else:
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise click.ClickException(
                f"cannot re-read harness settings {path}: {exc}"
            ) from exc
        if current != original:
            raise click.ClickException(
                f"harness settings changed while being updated: {path}"
            )
        mode = path.stat().st_mode

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.reckon-{os.getpid()}-{time.time_ns()}")
    try:
        temporary.write_bytes(encoded)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise click.ClickException(
            f"cannot write harness settings {path}: {exc}"
        ) from exc


@pytest.fixture(autouse=True)
def _umask() -> Iterator[None]:
    """Pin the umask so a recorded mode is a fact about the writer, not the host."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _contents(directory: Path) -> list[str]:
    """Every entry in the directory, so a stray temporary is visible."""
    return sorted(entry.name for entry in directory.iterdir())


def _encoded(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()


def _refusal_message(
    fn: WriteFn, path: Path, payload: dict, original: bytes | None
) -> str:
    with pytest.raises(click.ClickException) as excinfo:
        fn(path, payload, original)
    return excinfo.value.format_message()


@pytest.mark.parametrize(
    "payload",
    [PLAIN, NON_ASCII_UNSORTED],
    ids=["plain", "non-ascii-unsorted"],
)
def test_written_bytes_match_the_base_revision_writer(
    tmp_path: Path, payload: dict
) -> None:
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"

    _base_revision_write(base, payload, None)
    cli._write_json_atomically(head, payload, None)

    assert head.read_bytes() == base.read_bytes()
    assert json.loads(head.read_bytes()) == payload
    pairs = json.loads(head.read_text(), object_pairs_hook=list)
    assert [key for key, _ in pairs] == list(payload)
    assert _contents(tmp_path) == ["base.json", "head.json"]


def test_non_ascii_is_written_literally_as_the_base_writer_wrote_it(
    tmp_path: Path,
) -> None:
    """The base revision passed ``ensure_ascii=False``: raw UTF-8, not escapes."""
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"

    _base_revision_write(base, NON_ASCII_UNSORTED, None)
    cli._write_json_atomically(head, NON_ASCII_UNSORTED, None)

    data = head.read_bytes()
    assert data == base.read_bytes()
    assert "café-Ω".encode() in data
    assert b"\\u00e9" not in data
    assert b"\\u03a9" not in data


def test_new_file_takes_the_process_default_mode(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"

    _base_revision_write(base, PLAIN, None)
    cli._write_json_atomically(head, PLAIN, None)

    assert stat.S_IMODE(base.stat().st_mode) == 0o644  # 0o666 & ~umask(0o022)
    assert stat.S_IMODE(head.stat().st_mode) == stat.S_IMODE(base.stat().st_mode)


def test_existing_file_keeps_its_mode(tmp_path: Path) -> None:
    """An existing 0o640 file is rewritten at 0o640, as the base revision left it."""
    base = tmp_path / "base.json"
    head = tmp_path / "head.json"
    previous = b'{"stale": true}\n'
    for target in (base, head):
        target.write_bytes(previous)
        target.chmod(0o640)

    _base_revision_write(base, PLAIN, previous)
    cli._write_json_atomically(head, PLAIN, previous)

    assert stat.S_IMODE(base.stat().st_mode) == 0o640
    assert stat.S_IMODE(head.stat().st_mode) == 0o640
    assert head.read_bytes() == base.read_bytes()


def test_unchanged_payload_is_left_alone(tmp_path: Path) -> None:
    """The early return keeps the file's inode, mode and bytes untouched."""
    target = tmp_path / "head.json"
    encoded = _encoded(PLAIN)
    target.write_bytes(encoded)
    target.chmod(0o640)
    before = target.stat()

    cli._write_json_atomically(target, PLAIN, encoded)

    after = target.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert target.read_bytes() == encoded
    assert _contents(tmp_path) == ["head.json"]


def test_refuses_when_the_file_changed_since_it_was_read(tmp_path: Path) -> None:
    recorded = b'{"a": 1}\n'
    changed = b'{"a": 2}\n'
    target = tmp_path / "settings.json"
    target.write_bytes(changed)

    base_msg = _refusal_message(_base_revision_write, target, PLAIN, recorded)
    head_msg = _refusal_message(cli._write_json_atomically, target, PLAIN, recorded)

    assert head_msg == base_msg
    assert "changed while being updated" in head_msg
    assert target.read_bytes() == changed
    assert _contents(tmp_path) == ["settings.json"]


def test_refuses_when_a_file_appeared_that_was_absent_when_read(
    tmp_path: Path,
) -> None:
    target = tmp_path / "settings.json"
    target.write_bytes(b'{"a": 1}\n')

    base_msg = _refusal_message(_base_revision_write, target, PLAIN, None)
    head_msg = _refusal_message(cli._write_json_atomically, target, PLAIN, None)

    assert head_msg == base_msg
    assert "changed while being updated" in head_msg
    assert _contents(tmp_path) == ["settings.json"]


def test_a_refused_write_leaves_no_sibling_temporary(tmp_path: Path) -> None:
    target = tmp_path / "head.json"
    original = b'{"a": 1}\n'
    target.write_bytes(b'{"a": 2}\n')

    with pytest.raises(click.ClickException):
        cli._write_json_atomically(target, PLAIN, original)

    assert _contents(tmp_path) == ["head.json"]
