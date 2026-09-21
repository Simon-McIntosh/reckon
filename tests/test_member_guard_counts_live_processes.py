"""The member guard releases a member on a proven-dead worker, not on a phase.

Measured 2026-09-21 across every project on this workstation: 44 live run
pointers held members, 16 of their workers were still alive, and 28 pointed at
a worker that had already exited. The guard refused on the ``phase`` field
alone, which lags until something folds the run's stream, so twelve pointers
should have read ``working`` and sixteen read ``starting`` while their own
manifests said otherwise. Nine working workers sat behind a declared ceiling of
sixty-four, and the binding constraint was neither the member count nor the
ceiling.

The replacement predicate is process liveness, and its trap is the answer that
cannot be established. A live pointer records the pid of a worker spawned on
the host that wrote it, and the crew config home is shared across login nodes,
so a pid read on the wrong machine answers about a process it never issued.
Liveness is therefore three-valued and only one answer may release a member:

* a worker running on this host blocks — a worker is running;
* a terminal phase releases without asking — the answer the guard already gave;
* a worker proven gone on this host releases — this is the case that recovers
  the lane;
* a liveness that cannot be established here blocks and is reported as
  ``unknown`` — releasing would double-dispatch a member whose worker may be
  alive on another node.

What the guard never reads is the manifest. *Process gone* is a true statement
standing in for *work abandoned*, and a delivered run has also left the process
table; told apart by the manifest, the stale reading was false for
twenty-seven of twenty-eight pointers.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from reckon.crew import node
from reckon.crew.node import (
    MemberInFlight,
    member_in_flight_verdict,
    refuse_member_in_flight,
)

HOST = socket.gethostname()
OTHER_HOST = "another-login-node"


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    return ceiling + 4096


def _pointer(
    run_id: str = "r-20260921T090441654915",
    *,
    phase: object = "working",
    pid: int | None = None,
    launcher_host: str | None = HOST,
    **extra: object,
) -> dict[str, object]:
    """A synthesised live pointer, carrying only what the guard may consult."""
    pointer: dict[str, object] = {"run_id": run_id, "phase": phase}
    if pid is not None:
        pointer["pid"] = pid
    if launcher_host is not None:
        pointer["launcher_host"] = launcher_host
    pointer.update(extra)
    return pointer


def _delivered_manifest(tmp_path: Path) -> Path:
    """A manifest on disk reporting the run as delivered — a terminal story."""
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "status: complete\n"
        "final_summary: the work landed and the node closed\n"
        "changed_paths: reckon/crew/node.py\n"
    )
    return manifest


def test_a_running_worker_on_this_host_blocks_its_member():
    verdict = member_in_flight_verdict(_pointer(pid=os.getpid()))
    assert verdict.blocks is True
    assert verdict.liveness == "alive"


def test_a_worker_gone_on_this_host_releases_its_member():
    verdict = member_in_flight_verdict(_pointer(pid=_absent_pid()))
    assert verdict.blocks is False
    assert verdict.liveness == "gone"


@pytest.mark.parametrize("phase", ["working", "starting"])
def test_the_lagging_phases_release_a_member_once_the_worker_is_gone(phase):
    # The two phases the measurement found lagging: neither may hold a member
    # whose process has exited, which is what left the lane at nine of sixty-four.
    verdict = member_in_flight_verdict(_pointer(phase=phase, pid=_absent_pid()))
    assert verdict.blocks is False
    assert verdict.liveness == "gone"


def test_a_pointer_with_no_recorded_pid_blocks_and_is_unknown():
    verdict = member_in_flight_verdict(_pointer(pid=None))
    assert verdict.blocks is True
    assert verdict.liveness == "unknown"


def test_a_pointer_recorded_on_another_host_blocks_and_is_unknown():
    # A foreign pid is not this host's process to judge in either direction.
    verdict = member_in_flight_verdict(_pointer(pid=1, launcher_host=OTHER_HOST))
    assert verdict.blocks is True
    assert verdict.liveness == "unknown"


def test_unknown_is_distinct_from_gone_for_both_unprovable_shapes():
    no_pid = member_in_flight_verdict(_pointer(pid=None))
    foreign = member_in_flight_verdict(_pointer(pid=1, launcher_host=OTHER_HOST))
    assert {no_pid.liveness, foreign.liveness} == {"unknown"}
    assert no_pid.liveness != "gone"
    assert foreign.liveness != "gone"
    assert no_pid.reason and foreign.reason


def test_a_terminal_phase_releases_a_live_worker_without_asking():
    verdict = member_in_flight_verdict(_pointer(phase="complete", pid=os.getpid()))
    assert verdict.blocks is False
    assert verdict.liveness == "terminal"


def test_a_terminal_phase_releases_a_pointer_with_no_pid_at_all():
    verdict = member_in_flight_verdict(_pointer(phase="failed", pid=None))
    assert verdict.blocks is False
    assert verdict.liveness == "terminal"


def test_the_refusal_names_the_observed_liveness_for_each_blocking_state():
    for pointer, state in (
        (_pointer(pid=os.getpid()), "alive"),
        (_pointer(pid=None), "unknown"),
        (_pointer(pid=1, launcher_host=OTHER_HOST), "unknown"),
    ):
        with pytest.raises(MemberInFlight) as raised:
            refuse_member_in_flight("member-a", pointer)
        assert state in str(raised.value)
        assert raised.value.verdict is not None
        assert raised.value.verdict.liveness == state


def test_a_proven_dead_worker_raises_nothing():
    # The recovery direction: no refusal is the whole point of the predicate.
    assert refuse_member_in_flight("member-a", _pointer(pid=_absent_pid())) is None


def test_the_guard_never_consults_the_manifest(tmp_path, monkeypatch):
    manifest = _delivered_manifest(tmp_path)
    # The manifest on disk says the run delivered; the worker is still running,
    # so the guard consults none of it and the member stays held.
    verdict = member_in_flight_verdict(
        _pointer(pid=os.getpid(), manifest_path=str(manifest))
    )
    assert verdict.blocks is True
    assert verdict.liveness == "alive"
    # And no manifest read is attempted at all: the freshness probe a
    # manifest-consulting guard would reach through is armed to explode.
    from reckon.crew import runs

    def _explode(*args, **kwargs):
        raise AssertionError("the member guard read the manifest")

    monkeypatch.setattr(runs, "_manifest_mtime_ns", _explode)
    for pointer in (
        _pointer(pid=os.getpid(), manifest_path=str(manifest)),
        _pointer(pid=_absent_pid(), manifest_path=str(manifest)),
        _pointer(phase="failed", pid=os.getpid(), manifest_path=str(manifest)),
        _pointer(pid=None, manifest_path=str(manifest)),
    ):
        node.member_in_flight_verdict(pointer)


def test_every_pointer_above_carries_a_non_terminal_phase():
    # No release above may pass because a phase happened to read terminal.
    for pid, host in ((_absent_pid(), HOST), (None, HOST), (1, OTHER_HOST)):
        pointer = _pointer(pid=pid, launcher_host=host)
        assert str(pointer["phase"]) not in node._TERMINAL_RUN_PHASES
