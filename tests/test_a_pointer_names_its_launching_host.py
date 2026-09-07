"""A live pointer records the machine that launched its run.

The crew configuration home is on shared storage and the live-pointer
directory is global across projects and machines, so a pointer carries a pid
that is only meaningful on the machine that issued it. The classifier reads
liveness at the moment it uses it and gates the process-table lookup on the
pointer's ``launcher_host`` matching the reading host, spelled with
``socket.gethostname()`` on the reading side. Until a pointer names its
launching host the gate cannot fire: the field is absent from every pointer
currently on disk and no module writes it, so the host-gated read cannot be
enabled at all. This locks in that a freshly created pointer carries the host
of the machine that created it, written once, and that pointers without the
field keep behaving exactly as before.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

HOST = socket.gethostname()
OTHER_HOST = f"{HOST}.foreign-host.invalid"

_REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"
# Every pointer this module writes names its run with this prefix, so a scan
# of the real live-pointer directory can tell this module's writes apart from
# the fleet's — the assertion is that none of them landed there.
_RUN_PREFIX = "r-pointer-"


@pytest.fixture(scope="module", autouse=True)
def _real_home_gains_no_pointer() -> None:
    """The temporary configuration home holds every write this module makes."""
    before = (
        set(_REAL_LIVE.glob(f"{_RUN_PREFIX}*.json")) if _REAL_LIVE.is_dir() else set()
    )
    yield
    after = (
        set(_REAL_LIVE.glob(f"{_RUN_PREFIX}*.json")) if _REAL_LIVE.is_dir() else set()
    )
    assert not after - before, "these tests wrote a live pointer into the real home"


@pytest.fixture()
def live_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every pointer and lock inside the test directory."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _launch_record(
    run_id: str,
    *,
    pid: int | None = os.getpid(),
    stored_alive: bool | None = None,
    launcher_host: str | None = None,
) -> dict:
    """One record shaped as dispatch writes it for a freshly launched run.

    ``launcher_host`` is absent unless the caller supplies one, matching the
    launch record as dispatch constructs it today. Delivery paths are under a
    directory that does not exist, so a classifier read treats them as absent.
    """
    record = {
        "run_id": run_id,
        "project": "sample",
        "node": {"id": run_id, "plan": "delivery", "time_budget": "20m"},
        "phase": "working",
        "created_at": "2026-09-07T00:00:00Z",
        "manifest_path": f"/crew-nonexistent/{run_id}/manifest.md",
        "log_path": f"/crew-nonexistent/{run_id}/stream.jsonl",
        "stderr_path": f"/crew-nonexistent/{run_id}/stderr.log",
        "attempt": 1,
        "process_alive": stored_alive,
        "pid": pid,
        "pid_start_time": None,
    }
    if launcher_host is not None:
        record["launcher_host"] = launcher_host
    return record


def _write_predating_pointer(run_id: str, *, pid: int | None) -> None:
    """A pointer file as it would exist had this change never landed.

    Written directly rather than through ``_write_json`` so the first-write
    stamping is not involved: a pointer created before the change carried no
    launching host because no module wrote the field.
    """
    path = runs.pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_launch_record(run_id, pid=pid)))


def test_a_fresh_pointer_carries_the_launching_host(
    live_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pointer first written for a launched run names the machine that wrote
    # it, spelled with socket.gethostname() so the field and the classifier's
    # reading host can never disagree — measured in the writing process, so a
    # foreign gethostname() makes the assertion fail rather than pass.
    monkeypatch.setattr(socket, "gethostname", lambda: HOST)
    run_id = f"{_RUN_PREFIX}launch-host"
    record = _launch_record(run_id)
    runs._write_json(runs.pointer_path(run_id), record)
    pointer = runs.read_pointer(run_id)
    assert pointer["launcher_host"] == HOST
    # The caller's own record carries the field too: dispatch keeps rewriting
    # the same record across a launch, so the first write must not be the only
    # one that knows the host.
    assert record["launcher_host"] == HOST


def test_a_rewritten_pointer_keeps_its_original_launching_host(
    live_home: Path,
) -> None:
    # A pointer created on another login node — the shared home means rewrites
    # arrive from machines other than the launcher — is mutated here without
    # acquiring this machine's host.
    run_id = f"{_RUN_PREFIX}foreign-origin"
    original = OTHER_HOST
    runs._write_json(
        runs.pointer_path(run_id), _launch_record(run_id, launcher_host=original)
    )
    assert runs.read_pointer(run_id)["launcher_host"] == original
    runs._mutate_pointer(run_id, lambda record: {**record, "phase": "waiting"})
    assert runs.read_pointer(run_id)["launcher_host"] == original


def test_the_host_survives_the_write_and_read_cycle(live_home: Path) -> None:
    # The classifier reads the pointer after each of dispatch's launch passes;
    # the field stamped at creation must still answer at every later read.
    # Dispatch re-writes the same evolving record across its launch, so the
    # first write must leave the host on that record for the passes after it.
    run_id = f"{_RUN_PREFIX}cycle"
    record = _launch_record(run_id)
    for _ in range(3):
        record["phase"] = "working"
        runs._write_json(runs.pointer_path(run_id), record)
        assert runs.read_pointer(run_id)["launcher_host"] == HOST


def test_a_predating_pointer_reads_and_classifies_as_it_does_today(
    live_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pointer that predates this change carries no launching host, and
    # nothing invents one for it: it reads without error, classifies from its
    # stored field as unproven without a process lookup, and stays hostless
    # when rewritten — the lookups are forbidden, so any probe is a failure.
    pid = os.getpid()
    run_id = f"{_RUN_PREFIX}predating"
    _write_predating_pointer(run_id, pid=pid)
    pointer = runs.read_pointer(run_id)
    assert "launcher_host" not in pointer

    def _forbidden_lookup(candidate):
        raise AssertionError(
            f"process lookup performed without a recorded host {candidate!r}"
        )

    monkeypatch.setattr(recovery, "process_alive", _forbidden_lookup)
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["process_alive"] is None
    assert row["liveness_proven"] is False

    # The write path does not backfill the field onto a pointer already on
    # disk: a rewrite (file exists) leaves it exactly as it found it.
    runs._write_json(runs.pointer_path(run_id), pointer)
    assert "launcher_host" not in runs.read_pointer(run_id)


def test_the_lookup_happens_only_when_the_host_matches(
    live_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The host gate decides whether the process table is consulted at all.
    # Observing the lookup rather than only its result: a pointer naming this
    # host is probed, a pointer naming any other host is not.
    pid = os.getpid()
    looked_up: list[int] = []
    monkeypatch.setattr(
        recovery,
        "process_alive",
        lambda candidate: looked_up.append(candidate) or False,
    )

    matched = f"{_RUN_PREFIX}matched"
    runs._write_json(
        runs.pointer_path(matched),
        _launch_record(matched, pid=pid, launcher_host=HOST),
    )
    row = recovery.classify_pointer(runs.read_pointer(matched), now_seconds=time.time())
    assert looked_up == [pid]
    assert row["process_alive"] is False
    assert row["liveness_proven"] is True

    looked_up.clear()
    foreign = f"{_RUN_PREFIX}foreign"
    runs._write_json(
        runs.pointer_path(foreign),
        _launch_record(foreign, pid=pid, launcher_host=OTHER_HOST, stored_alive=True),
    )
    row = recovery.classify_pointer(runs.read_pointer(foreign), now_seconds=time.time())
    assert looked_up == []
    assert row["process_alive"] is True
    assert row["liveness_proven"] is False
