"""A landing arm's tree does not outlive the job that built it.

A partitioned arm unpacks its tree under the node's own tmp and releases it on
every exit its own script reaches. A job the scheduler ends at its time limit,
or kills outright, never reaches that release, so its tree sits there until
something else removes it. This sweep removes such a tree once its last
activity is older than the partition's time limit and no process holds it, and
it reports each removal with the node, the path and the bytes reclaimed.

The cases drive the sweep directly against a synthetic node root, so every
tree under test is under the test's own control: one abandoned tree older than
the limit, one held by a live process, one younger than the limit, and two
abandoned trees whose removal records are read back.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import landing_measure

#: The longest a job on the partition under test may run.
LIMIT_SECONDS = 3600.0

#: The node the sweep reports against.
NODE = "98dci4-clu-3141"


def _arm_tree(root: Path, name: str, *, age_seconds: float, now: float) -> Path:
    """A tree named the way a partitioned arm names its own, aged as stated."""
    tree = root / f"{landing_measure.arm_tree_prefix()}{name}-abcd1234"
    payload = tree / "tree" / "tests"
    payload.mkdir(parents=True)
    (payload / "test_payload.py").write_bytes(b"x" * 4096)
    stamp = now - age_seconds
    os.utime(tree, (stamp, stamp))
    return tree


@pytest.fixture(name="node_root")
def node_root_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "node-tmp"
    root.mkdir()
    return root


def test_a_tree_older_than_the_limit_with_no_holder_is_removed(node_root: Path):
    """The tree a job killed at its limit left is gone on the next sweep."""
    now = time.time()
    tree = _arm_tree(node_root, "killed", age_seconds=LIMIT_SECONDS + 600, now=now)

    removed = landing_measure.sweep_stale_arm_trees(
        node_root, limit_seconds=LIMIT_SECONDS, node=NODE, now=now
    )

    assert not tree.exists()
    assert len(removed) == 1


def test_a_tree_older_than_the_limit_that_a_process_holds_is_kept(node_root: Path):
    """A concurrent arm's tree survives however long it has been in place."""
    now = time.time()
    tree = _arm_tree(node_root, "held", age_seconds=LIMIT_SECONDS + 600, now=now)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=str(tree),
    )
    try:
        removed = landing_measure.sweep_stale_arm_trees(
            node_root, limit_seconds=LIMIT_SECONDS, node=NODE, now=now
        )
    finally:
        holder.kill()
        holder.wait(timeout=30)

    assert tree.exists()
    assert removed == []


def test_a_tree_younger_than_the_limit_is_kept(node_root: Path):
    """A tree a job may still be building is not removed."""
    now = time.time()
    tree = _arm_tree(node_root, "young", age_seconds=LIMIT_SECONDS - 60, now=now)

    removed = landing_measure.sweep_stale_arm_trees(
        node_root, limit_seconds=LIMIT_SECONDS, node=NODE, now=now
    )

    assert tree.exists()
    assert removed == []


def test_each_removal_is_recorded_with_its_node_path_and_size(node_root: Path):
    """A reader can account for the space the sweep reclaimed."""
    now = time.time()
    first = _arm_tree(node_root, "first", age_seconds=LIMIT_SECONDS + 60, now=now)
    second = _arm_tree(node_root, "second", age_seconds=LIMIT_SECONDS + 120, now=now)

    removed = landing_measure.sweep_stale_arm_trees(
        node_root, limit_seconds=LIMIT_SECONDS, node=NODE, now=now
    )

    assert not first.exists()
    assert not second.exists()
    records = {Path(record["path"]): record for record in removed}
    assert set(records) == {first, second}
    for path, record in records.items():
        assert record["node"] == NODE
        assert record["bytes"] >= 4096
        assert record["age_seconds"] >= LIMIT_SECONDS
        assert path.name.startswith(landing_measure.arm_tree_prefix())


def test_a_tree_under_another_accounts_name_is_left_alone(node_root: Path):
    """The sweep reclaims only this account's trees on a shared node."""
    now = time.time()
    foreign = node_root / "someone-else-landing-1234-abcd"
    foreign.mkdir(parents=True)

    removed = landing_measure.sweep_stale_arm_trees(
        node_root,
        limit_seconds=LIMIT_SECONDS,
        node=NODE,
        now=now,
        user="someone-else-but-not-me",
    )

    assert foreign.exists()
    assert removed == []


def test_an_unbounded_partition_licenses_no_removal(node_root: Path):
    """A partition with no declared ceiling cannot date a tree as abandoned."""
    now = time.time()
    tree = _arm_tree(node_root, "unbounded", age_seconds=10**6, now=now)

    removed = landing_measure.sweep_stale_arm_trees(
        node_root, limit_seconds=0, node=NODE, now=now
    )

    assert tree.exists()
    assert removed == []


def test_the_sweep_runs_from_its_job_entry_point(
    node_root: Path, monkeypatch: pytest.MonkeyPatch
):
    """The entry point a landing job's shell command calls is the sweep itself."""
    now = time.time()
    tree = _arm_tree(node_root, "entry", age_seconds=LIMIT_SECONDS + 60, now=now)
    monkeypatch.setattr(landing_measure, "arm_tree_root", lambda: node_root)

    landing_measure.sweep_arm_trees_entry(f"{LIMIT_SECONDS:g}", NODE)

    assert not tree.exists()


def test_the_sweep_command_names_the_entry_point_and_the_bound():
    """The shell command a job runs carries the program, the sweep and the limit."""
    command = landing_measure.arm_sweep_command(
        LIMIT_SECONDS, Path("/home/example/reckon")
    )

    assert "PYTHONPATH=/home/example/reckon" in command
    assert "sweep_arm_trees_entry" in command
    assert f"{LIMIT_SECONDS:g}" in command
    assert '"$(hostname)"' in command
