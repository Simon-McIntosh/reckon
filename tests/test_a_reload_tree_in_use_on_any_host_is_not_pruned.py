"""Pruning keeps a recent image: one just published, or one still leased."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import time
from importlib import import_module
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


def _executed_root(execs) -> Path:
    launcher = execs[0][1][2]
    literal = launcher.split("sys.path.insert(0, ", 1)[1].split(");", 1)[0]
    return Path(ast.literal_eval(literal))


def _stub_node(monkeypatch, name: str = "this-node") -> dict[str, str]:
    """Reroute the node name leases are recorded under, for one test's tree."""
    node = {"name": name}
    monkeypatch.setattr(
        follow.checkout_trust, "_lock_node_name", lambda: node["name"], raising=False
    )
    return node


def _lease_from_another_host(monkeypatch, root: Path, image: Path) -> None:
    """Hold an image as a follower on another node does, then let it go.

    ``flock`` does not cross nodes, so once the descriptor is closed nothing on
    this node holds the lease; what remains is the node named in the tree's
    lease record, which the holder restamped on each of its polls.
    """
    node = _stub_node(monkeypatch, "another-node")
    monkeypatch.setenv(follow._FOLLOWER_SOURCE_ROOT_ENV, str(root))
    monkeypatch.setenv(
        follow._FOLLOWER_IMAGE_COMMIT_ENV, _git(root, "rev-parse", "HEAD")
    )
    monkeypatch.setattr(follow, "_import_root", lambda module: image)
    holder = follow._FollowerReloader("another-node", None)
    assert holder.import_root == image
    assert holder.image_pin is not None
    holder.poll({})
    os.close(holder.image_pin)
    node["name"] = "this-node"


@pytest.fixture()
def source(tmp_path: Path, monkeypatch):
    # A test run from inside a materialised image carries the image's own
    # source root and commit, and every fixture here builds its own; leaving
    # them set would make these assertions depend on how the suite was
    # launched rather than on what it exercises.
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
    monkeypatch.setattr(runs, "FOLLOWER_FRESHNESS_SECONDS", 0)
    monkeypatch.setattr(
        import_module("reckon.crew.dispatch"),
        "_export_launched_workers_for_reexec",
        lambda: None,
    )
    execs = []
    monkeypatch.setattr(follow.os, "execve", lambda *args: execs.append(args))
    return root, first, execs


def test_a_tree_published_before_its_lease_survives_a_concurrent_prune(
    source, monkeypatch
):
    """The publish-to-lease gap must not be a pruning window for anyone else."""
    root, _, _ = source
    checkout = follow.checkout_trust
    for index in range(checkout.COMMITTED_TREE_RETENTION):
        _publish(root, _commit(root, f"filler-{index}"))
    target_commit = _commit(root, "the-target")
    real_pin = checkout.pin_materialized_tree
    removals: list[Path] = []

    def prune_first_then_lease(pin_root: Path) -> int:
        # A second materialiser publishes newer images while the first has
        # published its tree and not yet taken its lease. Its prune ranks the
        # first's tree old and would remove it.
        if not removals:
            removals.append(pin_root)
            for index in range(checkout.COMMITTED_TREE_RETENTION + 1):
                _, removed, _, pin = checkout.materialize_committed_tree(
                    root, _commit(root, f"concurrent-{index}")
                )
                os.close(pin)
                removals.extend(removed)
        return real_pin(pin_root)

    monkeypatch.setattr(checkout, "pin_materialized_tree", prune_first_then_lease)
    image, _, _, pin = checkout.materialize_committed_tree(root, target_commit)
    try:
        assert image.is_dir(), "the tree was pruned between publication and its lease"
        assert (image / "reckon" / "example.py").read_text() == "VALUE = 'the-target'\n"
    finally:
        os.close(pin)


def test_a_lease_renewed_from_another_host_keeps_its_tree(source, monkeypatch):
    """A renewal protects its tree even with no flock held on this host."""
    root, _, _ = source
    checkout = follow.checkout_trust
    subject_commit = _commit(root, "subject")
    subject, _, _, pin = checkout.materialize_committed_tree(root, subject_commit)
    os.close(pin)
    _lease_from_another_host(monkeypatch, root, subject)

    for index in range(checkout.COMMITTED_TREE_RETENTION + 1):
        _publish(root, _commit(root, f"newer-{index}"))

    assert subject.is_dir(), "a lease renewed from another host lost its tree"


def test_a_tree_a_peer_node_pinned_before_renewing_keeps_its_tree(source, monkeypatch):
    """A peer's freshly taken lease holds a tree this node's locks cannot."""
    root, _, _ = source
    checkout = follow.checkout_trust
    node = _stub_node(monkeypatch, "peer-node")
    peer_commit = _commit(root, "peer")
    peer, _, _, pin = checkout.materialize_committed_tree(root, peer_commit)
    os.close(pin)
    # The peer has taken its lease and has not polled since, so no renewal
    # has been recorded: this is the instant the tree was left unprotected in.
    node["name"] = "this-node"

    for index in range(checkout.COMMITTED_TREE_RETENTION + 1):
        _publish(root, _commit(root, f"newer-{index}"))

    assert peer.is_dir(), "a peer node's freshly taken lease did not keep its tree"


def test_a_tree_past_both_windows_with_no_lease_is_pruned(source, monkeypatch):
    """The windows bound how long an in-use tree is spared, as before."""
    root, _, _ = source
    checkout = follow.checkout_trust
    stale_commit = _commit(root, "stale")
    stale, _, _, pin = checkout.materialize_committed_tree(root, stale_commit)
    os.close(pin)
    _lease_from_another_host(monkeypatch, root, stale)
    # Hours behind, so no stated grace or window could still be covering it.
    past = time.time() - 10_800
    for path in (stale, *(stale / name for name in os.listdir(stale))):
        os.utime(path, (past, past))

    for index in range(checkout.COMMITTED_TREE_RETENTION + 1):
        _publish(root, _commit(root, f"newer-{index}"))

    assert not stale.exists(), "a tree past both windows was spared"


def test_a_target_pruned_before_its_lease_is_retried_once(source, monkeypatch, capsys):
    """One vanished target is recovered; the reload does not defer for it."""
    root, first, execs = source
    checkout = follow.checkout_trust
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
    second = _commit(root, "second")
    real_pin = checkout.pin_materialized_tree
    removals: list[Path] = []

    def remove_the_target_once(pin_root: Path) -> int:
        if not removals:
            removals.append(pin_root)
            shutil.rmtree(pin_root)
        return real_pin(pin_root)

    monkeypatch.setattr(checkout, "pin_materialized_tree", remove_the_target_once)
    reloader.poll({})

    assert len(removals) == 1, "the target was never removed, so nothing was raced"
    assert len(execs) == 1, capsys.readouterr().out
    image = _executed_root(execs)
    assert image.is_dir()
    assert (image / "reckon" / "example.py").read_text() == "VALUE = 'second'\n"
    assert second in str(image)


def test_a_target_removed_twice_defers_naming_the_vanished_path(
    source, monkeypatch, capsys
):
    """A prune that is not a race is deferred on, not spun on."""
    root, first, execs = source
    checkout = follow.checkout_trust
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
    second = _commit(root, "second")
    real_pin = checkout.pin_materialized_tree
    removals: list[Path] = []

    def remove_the_target(pin_root: Path) -> int:
        removals.append(pin_root)
        shutil.rmtree(pin_root)
        return real_pin(pin_root)

    monkeypatch.setattr(checkout, "pin_materialized_tree", remove_the_target)
    reloader.poll({})

    assert len(removals) == 2, "the materialiser did not retry exactly once"
    assert execs == []
    line = capsys.readouterr().out
    assert "could not materialize HEAD" in line
    assert second in line, line
