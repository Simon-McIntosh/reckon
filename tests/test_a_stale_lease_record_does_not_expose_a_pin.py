"""A stale record must not expose a tree a pin is still taking.

A tree's lease record names the hosts that have taken its lease, and a pruner
keeps a tree whose record names another host renewed inside the lease window.
But the record is not the only thing a pin writes: ``pin_materialized_tree``
opens and stamps the ``.in-use`` mark before it takes the flock, and rewrites
the record after. In that gap the record can still name only a host that
stopped renewing longer ago than the lease window, and a pruner on a third
node secures its own exclusive lock -- ``flock`` does not cross nodes -- and
would remove the tree being pinned.

The tests below hold the fix to the mark's recency: a tree whose record is
stale but whose ``.in-use`` was stamped inside the publish grace counts as in
use. The pin stamps it as it takes the lease; the opening of an existing mark
rewrites nothing, so an explicit stamp is what makes the pin visible across
that gap.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew_follow_commands as follow
from reckon.crew import runs

_GIT_HOME = os.environ.get("RECKON_HOME")


def _git(root: Path, *args: str) -> str:
    environment = dict(os.environ)
    if _GIT_HOME is None:
        environment.pop("RECKON_HOME", None)
    else:
        environment["RECKON_HOME"] = _GIT_HOME
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _commit(root: Path, value: str) -> str:
    (root / "reckon" / "example.py").write_text(f"VALUE = {value!r}\n")
    _git(root, "add", "reckon/example.py")
    _git(root, "commit", "-m", f"Set example to {value}")
    return _git(root, "rev-parse", "HEAD")


def _publish(root: Path, commit: str) -> Path:
    image, _, _, pin = follow.checkout_trust.materialize_committed_tree(root, commit)
    os.close(pin)
    return image


def _stub_node(monkeypatch, name: str = "this-node") -> dict[str, str]:
    """Reroute the node name leases are recorded under, for one test's tree."""
    node = {"name": name}
    monkeypatch.setattr(
        follow.checkout_trust, "_lock_node_name", lambda: node["name"], raising=False
    )
    return node


@pytest.fixture()
def source(tmp_path: Path, monkeypatch):
    for carried in (
        follow._FOLLOWER_SOURCE_ROOT_ENV,
        follow._FOLLOWER_IMAGE_COMMIT_ENV,
        "RECKON_FOLLOWER_OWNER",
    ):
        monkeypatch.delenv(carried, raising=False)
    _stub_node(monkeypatch)
    checkout = follow.checkout_trust
    monkeypatch.setattr(
        checkout,
        "_committed_tree_cache",
        lambda: tmp_path / "home" / "crew" / "reload-trees",
    )
    root = tmp_path / "source"
    (root / "reckon").mkdir(parents=True)
    (root / "reckon" / "__init__.py").write_text("")
    _git(root, "init")
    _git(root, "config", "user.name", "Reload Test")
    _git(root, "config", "user.email", "reload@example.invalid")
    _git(root, "add", "reckon/__init__.py")
    first = _commit(root, "first")
    monkeypatch.setattr(follow, "_import_root", lambda module: root)
    monkeypatch.setattr(
        runs, "follower_code_stamp", lambda: _git(root, "rev-parse", "HEAD")
    )
    return root, first


def _seed_stale_lease(
    checkout, subject: Path, monkeypatch, *, host: str = "stale-host"
) -> None:
    """Leave a tree whose lease record names a host renewed past the window.

    The record is written as a lease taken long ago and never renewed, and both
    the record and the tree's existing in-use mark carry that stale host's old
    mtime. The mark is not created here in a fresh state: opening an existing
    mark rewrites nothing, so a stale mark beside a stale record is exactly
    what a pruner reads when a pin is between its two steps.
    """
    node = _stub_node(monkeypatch, host)
    (subject / checkout.COMMITTED_TREE_LEASE_MARKER).write_text(host + "\n")
    past = time.time() - 3 * checkout.COMMITTED_TREE_LEASE_WINDOW_SECONDS
    for path in (
        subject,
        subject / checkout.COMMITTED_TREE_LEASE_MARKER,
        subject / ".in-use",
    ):
        os.utime(path, (past, past))
    node["name"] = "this-node"


def test_a_stale_lease_record_does_not_expose_a_pin(source, monkeypatch):
    """A pin pauses before its flock; a prune must still keep its tree."""
    root, _ = source
    checkout = follow.checkout_trust
    subject_commit = _commit(root, "subject")
    subject = _publish(root, subject_commit)
    _seed_stale_lease(checkout, subject, monkeypatch)

    real_flock = fcntl.flock
    pruned_once: list[str] = []

    def pause_before_the_flock(descriptor: int, operation: int) -> None:
        # The pin's two steps are: stamp the in-use mark, then take the flock
        # and rewrite the record. Pause in between -- no lock is held yet, so a
        # pruner on another node (whose flock this node cannot see) proceeds as
        # if the tree were free. A second materialiser publishes newer images
        # here, ranking the subject past the retention so the prune considers
        # it.
        if operation == fcntl.LOCK_SH and not pruned_once:
            pruned_once.append("pruned")
            for index in range(checkout.COMMITTED_TREE_RETENTION + 1):
                _, _, _, pin = checkout.materialize_committed_tree(
                    root, _commit(root, f"newer-{index}")
                )
                os.close(pin)
        real_flock(descriptor, operation)

    third = _stub_node(monkeypatch, "third-node")
    monkeypatch.setattr(checkout.fcntl, "flock", pause_before_the_flock)
    pin = checkout.pin_materialized_tree(subject)
    try:
        assert pruned_once, "the paused pin never ran the prune, so nothing raced"
        assert subject.is_dir(), "the paused pin lost its tree"
        assert (subject / "reckon" / "example.py").read_text() == "VALUE = 'subject'\n"
        assert (subject / checkout.COMMITTED_TREE_LEASE_MARKER).read_text().split() == [
            "stale-host",
            "third-node",
        ]
    finally:
        os.close(pin)
        third["name"] = "this-node"


def test_a_stale_lease_with_no_pin_is_pruned(source, monkeypatch):
    """The mark's recency bounds the spare; a stale tree with no pin goes."""
    root, _ = source
    checkout = follow.checkout_trust
    subject_commit = _commit(root, "subject")
    stale = _publish(root, subject_commit)
    _seed_stale_lease(checkout, stale, monkeypatch)

    for index in range(checkout.COMMITTED_TREE_RETENTION + 1):
        _publish(root, _commit(root, f"newer-{index}"))

    assert not stale.exists(), "a stale lease with no pin in progress was spared"
