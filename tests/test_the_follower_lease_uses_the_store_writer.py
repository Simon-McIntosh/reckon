"""The follower lease is published through the repository's atomic JSON writer.

The lease crosses a shared home to a reader on another node, so it must be
published whole: a reader must never observe it empty or half-written. That
guarantee lives in ``reckon._store.write_json_atomically``, the one place a file
is published through a sibling temporary. This module pins the lease onto that
writer -- the bytes and the file mode are what the private writer produced, and a
reader polling through a renewing writer never sees a torn record.
"""

from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import follower_registration, run_paths

PROJECT = "lease-store-writer"
SESSION = "session-on-another-node"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised configuration home so the real one is never written."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def test_run_paths_keeps_no_private_temporary_and_rename_writer() -> None:
    """The private writer is gone; the store owns the publish-by-rename step.

    A module that keeps its own temporary-and-rename beside the store's writer
    is the duplication this node removes, so its absence is asserted directly
    rather than inferred from the lease's behaviour.
    """
    assert not hasattr(run_paths, "_write_json_atomically")


def test_the_lease_bytes_and_mode_are_exactly_what_the_writer_produces(
    home: Path,
) -> None:
    """The lease holds compact sorted JSON plus a newline, at mode 0o644.

    ``indent=None`` keeps the record compact and ``mode=0o644`` keeps it
    readable by a reader on another node, both of which differ from the store
    writer's own defaults (indent 2, mode 0o600). The exact bytes are compared
    so an indented or reordered record cannot pass unnoticed.
    """
    follower_registration.renew_follower_lease(PROJECT, SESSION, now=1000.0)
    lease = follower_registration.follower_lease_path(PROJECT, SESSION)
    record = {"project": PROJECT, "session": SESSION, "renewed_at": 1000.0}
    expected = (json.dumps(record, sort_keys=True) + "\n").encode()
    assert lease.read_bytes() == expected
    assert stat.S_IMODE(lease.stat().st_mode) == 0o644


def test_the_lease_write_is_routed_through_the_store_writer(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease is written by the store's writer with the stated arguments.

    Patching the name the module calls proves the call reaches the store's
    writer rather than a private sibling, and pins the arguments that keep the
    bytes compact and the file readable.
    """
    calls: list[dict[str, object]] = []

    def spy(path, payload, **kwargs):
        calls.append({"path": path, "payload": payload, **kwargs})
        return Path(path)

    monkeypatch.setattr(follower_registration, "write_json_atomically", spy)
    follower_registration.renew_follower_lease(PROJECT, SESSION, now=1000.0)

    assert len(calls) == 1
    assert calls[0]["path"] == follower_registration.follower_lease_path(
        PROJECT, SESSION
    )
    assert calls[0]["indent"] is None
    assert calls[0]["mode"] == 0o644


def test_a_renewal_is_never_read_empty_or_torn_while_a_reader_polls(
    home: Path,
) -> None:
    """A reader on another node never sees an empty or partial lease.

    A writer renewing in a loop and a reader polling the lease's bytes from a
    second thread must never observe an empty or unparseable record. This is
    the case the whole-record publish exists for, and the one a truncate-then-
    write in place would break.
    """
    follower_registration.renew_follower_lease(PROJECT, SESSION, now=1000.0)
    lease = follower_registration.follower_lease_path(PROJECT, SESSION)

    stop = threading.Event()
    errors: list[str] = []

    def renew_loop() -> None:
        tick = 1000.0
        while not stop.is_set():
            tick += 1.0
            follower_registration.renew_follower_lease(PROJECT, SESSION, now=tick)

    writer = threading.Thread(target=renew_loop, daemon=True)
    writer.start()
    deadline = time.monotonic() + 1.5
    checks = 0
    try:
        while time.monotonic() < deadline:
            checks += 1
            raw = lease.read_bytes()
            if not raw:
                errors.append("the lease read empty")
                break
            try:
                record = json.loads(raw)
            except ValueError:
                errors.append("the lease read unparseable")
                break
            if not isinstance(record, dict) or "renewed_at" not in record:
                errors.append("the lease read an incomplete record")
                break
    finally:
        stop.set()
        writer.join(timeout=2.0)

    assert checks > 0
    assert errors == []
