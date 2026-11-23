"""A landing arm measures only the node's test files its own tree holds.

The base and the head of a landing can carry different sets of a node's test
files: a sibling node may delete one on the primary branch before the landing,
and the node's own change may add one. An arm that named a file its tree does
not hold stopped on pytest's unmatched-pathspec error — exit 4 with no failure
id — which read as an unreadable measurement and refused the landing with no
waiver that applied. These cases run the armed command over two trees and read
what each arm actually ran.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from reckon.crew import landing_measure

COMMAND = f"{sys.executable} -m pytest -p no:cacheprovider -q {{node_test_files}}"
BASE = "b" * 40
HEAD = "a" * 40
KEPT = "tests/test_kept.py"
DELETED = "tests/test_deleted.py"
ADDED = "tests/test_added.py"


@pytest.fixture(autouse=True)
def _no_outfitting_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child pytest that inherits an outfitting option runs another suite."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    """Two trees, each carrying the one test file the node kept."""
    base = tmp_path / "base"
    head = tmp_path / "head"
    for tree in (base, head):
        (tree / "tests").mkdir(parents=True)
        (tree / "tests" / "test_kept.py").write_text(
            "def test_kept():\n    assert True\n", encoding="utf-8"
        )
    return base, head


def _measure(tmp_path: Path, test_files: list[str], base: Path, head: Path) -> dict:
    return landing_measure.armed_observations(
        command=COMMAND,
        tree=head,
        base_tree=base,
        base_sha=BASE,
        head_sha=HEAD,
        logs_dir=tmp_path / "logs",
        test_files=test_files,
        timeout=180,
        config={},
    )


def _named(observation: dict) -> list[str]:
    """The test files one arm's recorded command names."""
    return [
        word
        for word in shlex.split(observation["command"])
        if word.startswith("tests/")
    ]


def _left_out(observation: dict) -> list[str]:
    return list(observation.get("left_out") or ())


def test_a_file_the_head_dropped_runs_in_neither_arm_and_is_named(
    tmp_path: Path,
) -> None:
    base, head = _trees(tmp_path)

    measured = _measure(tmp_path, [KEPT, DELETED], base, head)

    assert "unreadable" not in measured, measured
    baseline = measured["baseline_suite"]
    after = measured["after_suite"]
    assert isinstance(baseline, dict), baseline
    assert baseline["exit_status"] == 0, baseline
    assert after["exit_status"] == 0, after
    assert _named(baseline) == [KEPT], baseline
    assert _named(after) == [KEPT], after
    assert _left_out(baseline) == [DELETED], baseline
    assert _left_out(after) == [DELETED], after
    log = Path(baseline["log_path"]).read_text(encoding="utf-8")
    assert "1 passed" in log, log


def test_a_file_the_head_added_runs_in_the_head_arm_only(tmp_path: Path) -> None:
    base, head = _trees(tmp_path)
    (head / "tests" / "test_added.py").write_text(
        "def test_added():\n    assert True\n", encoding="utf-8"
    )

    measured = _measure(tmp_path, [KEPT, ADDED], base, head)

    assert "unreadable" not in measured, measured
    baseline = measured["baseline_suite"]
    after = measured["after_suite"]
    assert _named(baseline) == [KEPT], baseline
    assert _named(after) == [KEPT, ADDED], after
    assert _left_out(baseline) == [ADDED], baseline
    assert _left_out(after) == [], after


def test_a_pair_whose_files_both_trees_carries_runs_them_all(tmp_path: Path) -> None:
    base, head = _trees(tmp_path)
    for tree in (base, head):
        (tree / "tests" / "test_second.py").write_text(
            "def test_second():\n    assert True\n", encoding="utf-8"
        )

    measured = _measure(tmp_path, [KEPT, "tests/test_second.py"], base, head)

    assert "unreadable" not in measured, measured
    baseline = measured["baseline_suite"]
    after = measured["after_suite"]
    for arm in (baseline, after):
        assert _named(arm) == [KEPT, "tests/test_second.py"], arm
        assert _left_out(arm) == [], arm
