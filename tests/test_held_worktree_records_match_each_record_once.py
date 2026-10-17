"""Each held-tree record is matched once, through an index of the trees it names.

The whole-ledger read is the authority: a retained tree belongs to the last run
that names it in the merged ledger's order. This module builds a fleet large
enough that a per-tree walk over every record is the product, then shows the
index matches each record once — the calls of the naming rule during one
:func:`reckon.crew.obligations._held_worktree_records` are bounded by the records
plus the trees rather than their product — and that its answer for any pair is
the rule's own answer, so nothing is dropped by the shortcut.

Every record is built by the producer's own constructor and published through
the producer's own writer, so no case asserts against keys this test invented.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import routing
from reckon.worker_git_shim import RUN_ID_ENV

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "held-tree-index-fixture"
SESSION = "match-once-fixture"
TREE_COUNT = 20
RECORD_COUNT = 500
BASE_COMPLETED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC) - timedelta(seconds=5000)


def _git(repository: Path, *arguments: str) -> str:
    """Run git in ``repository`` without this process's run identity.

    A crew worker exports the run it serves, and the git shim refuses a
    mutating verb whose run's live pointer it cannot read — which is precisely
    the state a test is in, because its configuration home is a temporary one.
    The fixture's repository is a throwaway, so its git calls run with that
    identity removed and the shim transparent; they change nothing the shim
    exists to protect. Reads need no such care, but run through here too so a
    fixture never depends on which verbs the shim leaves alone.
    """
    environment = {key: value for key, value in os.environ.items() if key != RUN_ID_ENV}
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _tree_name(index: int) -> str:
    return f"node-{index:02d}"


def _worktree(root: Path, node: str) -> Path:
    """Register a tree the way a dispatched run leaves one behind."""
    path = root.parent / "managed-worktrees" / SESSION / node
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _build_case(index: int, trees: list[Path]) -> dict[str, Any]:
    """One fixture record: how it names a tree, its node, session and stamp.

    The first ``TREE_COUNT`` records name a tree by retained path; the next
    ``TREE_COUNT`` name the same trees by session and node id, and complete
    later, so the tree two records name goes to the node-named one. The
    remainder name no tree at all. Every stamp is distinct, so the merged
    order is unambiguous.
    """
    if index < TREE_COUNT:
        node = _tree_name(index)
        return {"node": node, "worktree": trees[index], "session": SESSION}
    if index < 2 * TREE_COUNT:
        return {
            "node": _tree_name(index - TREE_COUNT),
            "worktree": None,
            "session": SESSION,
        }
    return {"node": f"ghost-{index:03d}", "worktree": None, "session": SESSION}


@pytest.fixture(scope="module")
def _fleet_build(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, list[Path], list[dict[str, Any]]]:
    """The costly half of the fleet: a synthesised checkout and its records.

    Built once for the module and read by every case without writing. No
    environment is touched here: a module-scoped fixture outlives the tests
    that use it, and a worker running several test modules on one process would
    carry its redirected home into whatever ran next. Returns the temporary
    root the per-test home is made under, the repository root, the inspected
    tree paths, and the records as the producer wrote them.
    """
    tmp_path = tmp_path_factory.mktemp("held-tree-index")
    config_home = tmp_path / "config"
    config_home.mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the held-tree index fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )

    # Publishing a run resolves the checkout that owns the project through the
    # mount registry, and this module-scoped build runs before the per-test home
    # fixture exists. The two keys are therefore named for the length of the
    # build and put back before it returns, so nothing here outlives the setup
    # that needed it.
    previous = {
        key: os.environ.get(key) for key in ("RECKON_HOME", "RECKON_MOUNTS_PATH")
    }
    os.environ["RECKON_HOME"] = str(config_home)
    os.environ["RECKON_MOUNTS_PATH"] = str(config_home / "mounts.json")
    try:
        trees = [_worktree(root, _tree_name(index)) for index in range(TREE_COUNT)]
        records: list[dict[str, Any]] = []
        for index in range(RECORD_COUNT):
            case = _build_case(index, trees)
            completed = BASE_COMPLETED + timedelta(seconds=index)
            run_id = f"r-20261016T{100000 + index:06d}-r{index:03d}"
            record = ledger.build_record(
                run_id=run_id,
                plan="fixture-plan",
                gate="passed",
                node=case["node"],
                session=case["session"],
                completed_at=completed.isoformat(),
            )
            worktree = case["worktree"]
            record["worktree_retention"] = (
                {"worktree": str(worktree), "retained_at": completed.isoformat()}
                if worktree is not None
                else None
            )
            ledger.append_run(PROJECT, record, root=root, allow_create=True)
            records.append(record)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return tmp_path, root, trees, records


@pytest.fixture
def fleet(
    _fleet_build: tuple[Path, Path, list[Path], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[Path], list[dict[str, Any]]]:
    """Point this test, and only this test, at the fixture's checkout.

    The project is resolved through the configuration home, so the derivation
    reads the fixture's checkout only while this test runs. The home is set for
    one test and restored when it ends, because a worker runs several modules
    in one process and a home left pointed here would answer a peer module's
    lookup with a project its mounts file does not carry.
    """
    tmp_path, root, trees, records = _fleet_build
    config_home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    return root, trees, records


def _whole_ledger_held(root: Path) -> dict[str, str]:
    """The held tree -> run mapping the whole-ledger scan produces.

    This is the parity expectation: every recorded run is read from both
    sources in the merged ledger's order, and a tree a run names is attributed
    to the last run that names it. It is computed from the same authority the
    derivation claims to equal, without the index.
    """
    registered = {
        path
        for path in (
            Path(line.removeprefix("worktree ")).resolve()
            for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        )
        if path != root.resolve()
    }
    held: dict[str, str] = {}
    for record in ledger.runs(PROJECT, root=root):
        retention = record.get("worktree_retention")
        if isinstance(retention, dict) and retention.get("worktree"):
            retained = Path(str(retention["worktree"])).expanduser().resolve()
            if retained in registered:
                held[str(retained)] = str(record.get("run_id") or "")
        node = record.get("node")
        node_id = (
            str(node.get("id") or "") if isinstance(node, dict) else str(node or "")
        )
        session = str(record.get("session") or "").strip()
        for path in registered:
            if not node_id or path.name != node_id:
                continue
            if session and path.parent.name != session:
                continue
            held[str(path)] = str(record.get("run_id") or "")
    return held


def test_the_fixture_is_large_enough_to_expose_a_product(
    fleet: tuple[Path, list[Path], list[dict[str, Any]]],
) -> None:
    """The fixture holds many trees and far more records than trees."""
    _root, trees, records = fleet
    assert len(trees) >= 20
    assert len(records) >= 500
    assert len(records) * len(trees) > len(records) + len(trees)


def test_each_record_is_filed_once_not_compared_per_tree(
    fleet: tuple[Path, list[Path], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call compares a record and a tree at most records-plus-trees times.

    The naming rule is wrapped so every call it receives is counted, and the
    whole-ledger fallback is made to raise so a tree left to it cannot hide a
    per-tree walk. That the wrapper counts at all is shown before it is relied
    on, so a zero reading means no call rather than a dead instrument.
    """
    _root, trees, records = fleet
    calls = {"n": 0}
    real = routing.record_names_worktree

    def counting(record: Any, worktree: Any) -> bool:
        calls["n"] += 1
        return real(record, worktree)

    monkeypatch.setattr(routing, "record_names_worktree", counting)
    monkeypatch.setattr(obligations_module, "record_names_worktree", counting)

    # Positive control: the instrument records a call it is asked to make, so
    # a later zero is an absence of comparisons rather than of measurement.
    counting(records[0], trees[0])
    assert calls["n"] == 1
    calls["n"] = 0

    def _refuse(*_arguments: Any, **_keywords: Any) -> Any:
        raise AssertionError("the whole-ledger fallback ran for a named tree")

    monkeypatch.setattr(ledger, "runs", _refuse)

    held = obligations_module._held_worktree_records(PROJECT)

    assert set(held) == {path.resolve() for path in trees}
    assert calls["n"] <= len(records) + len(trees)


def test_the_index_agrees_with_the_naming_rule_for_every_pair(
    fleet: tuple[Path, list[Path], list[dict[str, Any]]],
) -> None:
    """For every record and tree, the index answers what the rule answers."""
    index_cls = getattr(routing, "WorktreeRecordIndex", None)
    if index_cls is None:
        pytest.skip("the record index is not present in this tree")
    _root, trees, records = fleet
    index = index_cls()
    for record in records:
        index.add(record)

    for record in records:
        for tree in trees:
            assert index.names(record, tree) == routing.record_names_worktree(
                record, tree
            ), (record.get("run_id"), tree)


def test_the_held_trees_equal_the_whole_ledger_derivation(
    fleet: tuple[Path, list[Path], list[dict[str, Any]]],
) -> None:
    """The index holds the same trees as the whole-ledger scan, and the same runs."""
    root, trees, records = fleet

    held = {
        str(path.resolve()): str(record.get("run_id") or "")
        for path, record in obligations_module._held_worktree_records(PROJECT).items()
    }

    assert held == _whole_ledger_held(root)
    assert set(held.values()) <= {str(record.get("run_id")) for record in records}
    # A tree two records name goes to the later one: the node-named record in
    # the second block completes after the path-named record in the first.
    for index in range(TREE_COUNT):
        tree = trees[index].resolve()
        assert (
            held[str(tree)]
            == f"r-20261016T{100000 + TREE_COUNT + index:06d}-r{TREE_COUNT + index:03d}"
        )


def test_a_record_that_names_no_tree_holds_none(
    fleet: tuple[Path, list[Path], list[dict[str, Any]]],
) -> None:
    """A record naming no tree is never attributed one."""
    _root, trees, _records = fleet
    index_cls = getattr(routing, "WorktreeRecordIndex", None)
    if index_cls is None:
        pytest.skip("the record index is not present in this tree")
    ghost = ledger.build_record(
        run_id="r-20261016T999999-ghost",
        plan="fixture-plan",
        gate="passed",
        node="ghost-unmatched",
        session=SESSION,
        completed_at=BASE_COMPLETED.isoformat(),
    )
    ghost["worktree_retention"] = None
    index = index_cls()
    index.add(ghost)
    assert all(index.records_naming(tree) == [] for tree in trees)
    assert all(not index.names(ghost, tree) for tree in trees)
