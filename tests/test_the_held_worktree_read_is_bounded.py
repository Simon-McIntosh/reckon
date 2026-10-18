"""One derivation reads only the per-run documents that can change its answer.

The held-worktree derivation matches registered trees against run records. Over
a crew home of :data:`RECORD_COUNT` per-run documents of which only
:data:`TREE_COUNT` can name a tree, this module counts, with a profile hook over
one :func:`reckon.crew.obligation_snapshot.fleet_state`, how many documents are
decoded, how often a memoised value is copied, and how many stats the read
costs, and shows the held-worktree answer equals the whole-ledger answer.

Against a base tree that decodes every per-run document, the decode count grows
with the corpus and every document is stat'ed a second time, so the same test
fails on both the copy and the stat bound.
"""

from __future__ import annotations

import copy as copy_module
import importlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import obligation_snapshot
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "held-read-bound-fixture"
SESSION = "held-read-bound"
TREE_COUNT = 15
RECORD_COUNT = 500
BASE_COMPLETED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC) - timedelta(seconds=5000)


def _tree_name(index: int) -> str:
    return f"node-{index:02d}"


def _worktree(root: Path, node: str) -> Path:
    """Register a tree the way a dispatched run leaves one behind."""
    path = root.parent / "managed-worktrees" / SESSION / node
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


@pytest.fixture(scope="module")
def _build(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, Path, list[Path]]]:
    """A synthesised checkout, its trees, and one record-bearing home.

    The environment is named only for the build and put back before the fixture
    returns, because a module-scoped fixture outlives the cases that use it.
    """
    tmp_path = tmp_path_factory.mktemp("held-read-bound")
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
        ("commit", "-q", "-m", "test: seed the held-read-bound fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    previous = {
        key: os.environ.get(key) for key in ("RECKON_HOME", "RECKON_MOUNTS_PATH")
    }
    os.environ["RECKON_HOME"] = str(config_home)
    os.environ["RECKON_MOUNTS_PATH"] = str(config_home / "mounts.json")
    try:
        trees = [_worktree(root, _tree_name(index)) for index in range(TREE_COUNT)]
        for index in range(RECORD_COUNT):
            completed = BASE_COMPLETED + timedelta(seconds=index)
            if index < TREE_COUNT:
                node = _tree_name(index)
                worktree: Path | None = trees[index]
            else:
                node = f"ghost-{index:03d}"
                worktree = None
            run_id = f"r-20261016T{100000 + index:06d}-{node}"
            record = ledger.build_record(
                run_id=run_id,
                plan="fixture-plan",
                gate="passed",
                node=node,
                session=SESSION,
                completed_at=completed.isoformat(),
            )
            record["worktree_retention"] = (
                {"worktree": str(worktree), "retained_at": completed.isoformat()}
                if worktree is not None
                else None
            )
            ledger.append_run(PROJECT, record, root=root, allow_create=True)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return tmp_path, root, trees


@pytest.fixture
def home(
    _build: tuple[Path, Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[Path]]:
    tmp_path, root, trees = _build
    config_home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    return root, trees


class _Counts:
    """A profile hook counting the derivation's decodes, copies and stats.

    Copies and stats are counted only while the held-worktree derivation runs,
    so they measure that read rather than every reader inside a fleet state.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.record = 0
        self.deepcopy = 0
        self.stat = 0
        self.active = False
        real_read = obligations_module._held_worktree_records

        def read(project: str) -> Any:
            self.active = True
            try:
                return real_read(project)
            finally:
                self.active = False

        monkeypatch.setattr(obligations_module, "_held_worktree_records", read)
        real_record = obligations_module._run_file_record

        def record(source: Any) -> Any:
            self.record += 1
            return real_record(source)

        monkeypatch.setattr(obligations_module, "_run_file_record", record)
        real_deepcopy = copy_module.deepcopy

        def deepcopy(value: Any, memo: Any = None) -> Any:
            if self.active:
                self.deepcopy += 1
            return real_deepcopy(value) if memo is None else real_deepcopy(value, memo)

        monkeypatch.setattr(copy_module, "deepcopy", deepcopy)
        real_stat = os.stat

        def stat(*arguments: Any, **keywords: Any) -> Any:
            if self.active:
                self.stat += 1
            return real_stat(*arguments, **keywords)

        monkeypatch.setattr(os, "stat", stat)


def _whole_ledger_held(root: Path) -> dict[str, str]:
    """The held tree -> run mapping the whole-ledger scan produces."""
    registered = {
        path.resolve()
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
    return held


def test_one_derivation_reads_only_the_documents_that_can_change_it(
    home: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet_state decodes and stats each per-run document at most once.

    The ledger index is warmed first, as the production fleet keeps it, so the
    measurement is of the derivation rather than of a cold index build. The
    profile hook counts decodes, deep copies and stats across the call.
    """
    root, _trees = home
    indexes = importlib.import_module("reckon.ledger")
    # Warm the index under the key the derivation's own reads use, so the
    # measurement is of the derivation rather than of a cold index build.
    indexes.runs(PROJECT, root=root)

    counts = _Counts(monkeypatch)
    state = obligation_snapshot.fleet_state(PROJECT)

    expected = _whole_ledger_held(root)
    duties = {
        str(duty.get("run_id") or "") for duty in state.held_worktrees.get(SESSION, [])
    }
    assert duties == set(expected.values())
    assert len(expected) == TREE_COUNT
    # The read-only path shares the memo's value rather than copying it, so no
    # document on it is deep-copied however many readers the derivation has.
    # Collect every bound's failure before asserting, so a run that misses both
    # names both rather than stopping at the first.
    problems: list[str] = []
    if counts.deepcopy != 0:
        problems.append(f"deepcopy on the read-only path is {counts.deepcopy}, not 0")
    # At most one stat per per-run document plus one per held tree.
    stat_bound = RECORD_COUNT + TREE_COUNT
    if counts.stat > stat_bound:
        problems.append(f"stat is {counts.stat}, above the {stat_bound} bound")
    assert not problems, (problems, counts.__dict__)
    # The only documents that can change the answer are the TREE_COUNT that
    # name a tree; the rest name no tree and must not be decoded.
    assert counts.record <= TREE_COUNT, counts.__dict__


def test_a_run_retaining_another_nodes_tree_stays_a_candidate() -> None:
    """The screen never drops a tree the naming rule could accept.

    A run names a tree by either route ``record_names_worktree`` applies: the
    node id it carries, or the path its retention carries. A run dispatched for
    one node can retain a tree named for another, so screening on the node id
    alone would leave the later record undecoded and answer with the earlier
    one. Both routes are keyed here, and a run the index does not carry stays a
    candidate, so a lagging index costs a read rather than a dropped duty.
    """
    tree = Path("/managed-worktrees/session/node-03")
    names = {tree.name}
    trees = {tree}
    assert obligations_module._may_name_a_tree(
        Path("r-20261016T100003-node-03.json"),
        names,
        trees,
        {"r-20261016T100003-node-03": ("node-03", None)},
    )
    assert obligations_module._may_name_a_tree(
        Path("r-20261016T100005-other-node.json"),
        names,
        trees,
        {"r-20261016T100005-other-node": ("other-node", tree)},
    )
    assert obligations_module._may_name_a_tree(
        Path("r-20261016T100099-unlisted.json"),
        names,
        trees,
        {"r-20261016T100003-node-03": ("node-03", None)},
    )
    assert not obligations_module._may_name_a_tree(
        Path("r-20261016T100004-other.json"),
        names,
        trees,
        {"r-20261016T100004-other": ("other-node", None)},
    )


def test_the_head_derivation_equals_the_whole_ledger(
    home: tuple[Path, list[Path]],
) -> None:
    """The bounded derivation holds the same trees as the whole-ledger scan."""
    root, _trees = home
    got = {
        str(path.resolve()): str(record.get("run_id") or "")
        for path, record in obligations_module._held_worktree_records(PROJECT).items()
    }
    assert got == _whole_ledger_held(root)
