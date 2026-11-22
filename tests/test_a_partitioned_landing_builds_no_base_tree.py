"""A landing measured on a partition builds no base tree on the fleet node.

Each partitioned arm builds its own tree from the revision on the node that runs
it, and the runner tells the base arm from the head arm by the directory it is
handed. A full checkout on the fleet node for the base arm is read by nothing,
and two concurrent landings of a large project held 6 GB of node-local disk
that way, under the launch floor every dispatch checks.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from reckon.crew import landing_measure


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "scratch"
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))
    return root


@pytest.fixture
def checkouts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    built: list[str] = []

    @contextlib.contextmanager
    def fake_checkout(repo, base_sha, timeout):
        built.append(base_sha)
        tree = tmp_path / f"checkout-{base_sha}"
        tree.mkdir()
        yield tree

    monkeypatch.setattr(landing_measure, "base_checkout", fake_checkout)
    return built


def test_a_partitioned_measure_gets_an_empty_placeholder(
    scratch: Path, checkouts: list[str]
) -> None:
    with landing_measure.base_tree_for_measure(
        Path("/repo"), "abc123", 30.0, partitioned=True
    ) as tree:
        assert tree.is_dir()
        assert list(tree.iterdir()) == []
        assert tree.parent == scratch
    assert checkouts == []
    assert not tree.exists()


def test_an_in_place_measure_still_gets_the_checkout(
    scratch: Path, checkouts: list[str]
) -> None:
    with landing_measure.base_tree_for_measure(
        Path("/repo"), "abc123", 30.0, partitioned=False
    ) as tree:
        assert tree.name == "checkout-abc123"
    assert checkouts == ["abc123"]


def test_the_partition_setting_decides_the_mode() -> None:
    assert landing_measure.measures_on_partition(
        {"landing": {"measure_partition": "all_debug"}}
    )
    assert not landing_measure.measures_on_partition({"landing": {}})
    assert not landing_measure.measures_on_partition(None)
