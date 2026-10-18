"""A follower's prune pass sweeps the claim directories of hosts that have ended.

Every session-host lifetime files its lift claims under one directory keyed on
the host, ``host-<node>-<pid>-<start>`` beneath ``crew_home/watch/lifts``. The
per-lift prune removes only the files inside the pane's own directory, so a host
that ends leaves its directory standing for every pane it served. A follower's
prune pass also removes the directory of a host that ran on this node and no
longer does, judged by pid *and* kernel start tick so a reused pid does not keep
it. A directory naming another node is never removed here, and a node-less key
from the layout before the node entered the key is removed only once it has gone
untouched for a week.

The pass runs under a temporary crew home, and the fixture proves the real
crew home is left untouched.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon.crew.obligation_snapshot import process_start_time
from reckon.crew.runs import crew_home
from reckon.crew_follow_commands import (
    LIFT_CLAIM_DIR_MAX_AGE_SECONDS,
    _lift_claim_token,
    _prune_abandoned_lift_claim_dirs,
    _prune_lift_claims,
    lift_claim_dir,
)

NODE = _lift_claim_token(socket.gethostname())
OTHER_NODE = "some-other-node"
# A pid far past any kernel's maximum, so no process holds it here.
ABSENT_PID = 999999999
DAY_SECONDS = 24 * 60 * 60.0


def _listing(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(str(path) for path in directory.rglob("*"))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the crew home at a temp dir; prove the real home is untouched."""
    real_claims = lift_claim_dir("")
    before = _listing(real_claims)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    yield tmp_path
    assert _listing(real_claims) == before, "a test wrote the real crew home"


def _lifts_root() -> Path:
    return crew_home() / "watch" / "lifts"


def _claim_dir(name: str, *, touched: float | None = None) -> Path:
    """A claim directory in the producer's own layout, holding one claim file."""
    directory = _lifts_root() / name
    directory.mkdir(parents=True)
    (directory / "codex-sub-1792002586-64d825__in-force").write_text("")
    if touched is not None:
        os.utime(directory, (touched, touched))
    return directory


def _dead_owner_pid() -> tuple[int, str]:
    """A pid this node does not hold, with the start tick it had while alive."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    start = process_start_time(pid) or "0"
    proc.wait()
    return pid, start


# ── the five-directory population: two swept, three kept ─────────────────────


def test_one_prune_pass_sweeps_the_abandoned_and_keeps_the_rest():
    now = time.time()
    live_pid = os.getpid()
    live_start = process_start_time(live_pid) or "0"
    dead_pid, dead_start = _dead_owner_pid()

    live_local = _claim_dir(f"host-{NODE}-{live_pid}-{live_start}")
    dead_local = _claim_dir(f"host-{NODE}-{dead_pid}-{dead_start}")
    other_node = _claim_dir(f"host-{OTHER_NODE}-{ABSENT_PID}-12345")
    stale_nodeless = _claim_dir(
        f"host-{ABSENT_PID}-12345", touched=now - 8 * DAY_SECONDS
    )
    fresh_nodeless = _claim_dir(f"host-{ABSENT_PID}-54321", touched=now)

    removed = _prune_abandoned_lift_claim_dirs(now=now)

    assert set(removed) == {dead_local, stale_nodeless}, (
        f"the pass removed {sorted(str(path) for path in removed)}"
    )
    assert not dead_local.exists(), "a dead local host's directory survived"
    assert not stale_nodeless.exists(), "an old node-less directory survived"
    assert live_local.is_dir(), "a live local host's directory was removed"
    assert other_node.is_dir(), "another node's directory was removed"
    assert fresh_nodeless.is_dir(), "a recent node-less directory was removed"


def test_a_per_session_directory_is_left_alone():
    """A hand-armed follower's claim is a live pane, not an abandoned host."""
    session_dir = _lifts_root() / "s-power"
    session_dir.mkdir(parents=True)
    assert _prune_abandoned_lift_claim_dirs() == []
    assert session_dir.is_dir(), "a per-session claim directory was removed"


# ── the pass is the follower's own: a lift end runs it ───────────────────────


def test_a_lift_end_sweeps_an_abandoned_host_directory():
    """The prune pass that fires when a lift ends also sweeps host directories."""
    dead_pid, dead_start = _dead_owner_pid()
    dead_local = _claim_dir(f"host-{NODE}-{dead_pid}-{dead_start}")

    _prune_lift_claims("s-power", "codex-sub-1792002586-64d825")

    assert not dead_local.exists(), "the per-lift prune left an abandoned directory"


def test_the_age_bound_is_seven_days():
    assert LIFT_CLAIM_DIR_MAX_AGE_SECONDS == 7 * DAY_SECONDS
