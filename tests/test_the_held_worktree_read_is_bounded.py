"""One derivation reads only the per-run documents that can change its answer.

The held-worktree derivation matches registered trees against run records. Over
a crew home of :data:`RECORD_COUNT` per-run documents of which only
:data:`TREE_COUNT` can name a tree, this module counts, with a profile hook over
one :func:`reckon.crew.obligation_snapshot.fleet_state`, how many documents are
decoded, how often a memoised value is copied, and how many stats the read
costs, and shows the held-worktree answer equals the whole-ledger answer.

Against a base tree that decodes every per-run document, the same call decodes
the whole corpus, so the same test fails on the decode assertion. The deep-copy
bound is asserted with it: the read-only path shares the value it is handed, so
a partial revert of that sharing fails there too. The
stat count is recorded rather than bounded: a screen that never acts on a
record older than its run file must stat every per-run document once and every
held tree once, and the reader that supplies the namings charges a few more, so
no single bound both passes a fresh screen and fails the base, which already
stats each document once.

The screen is also exercised refused: the reader that supplies the namings is
made to raise, and the derivation must still answer from the per-run documents,
because a screen that cannot be built costs a read rather than a duty.
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

from reckon import _store, ledger
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
        # Every per-run document is decoded through here, whether the reader
        # decodes each file for itself or only the ones that have moved since
        # the published index, so one hook counts both.
        real_document = ledger.read_document

        def document(source: Any) -> Any:
            self.record += 1
            return real_document(source)

        monkeypatch.setattr(ledger, "read_document", document)
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
    decoded = counts.record

    expected = _whole_ledger_held(root)
    duties = {
        str(duty.get("run_id") or "") for duty in state.held_worktrees.get(SESSION, [])
    }
    assert duties == set(expected.values())
    assert len(expected) == TREE_COUNT
    # The decode bound: one derivation decodes at most the per-run documents
    # that can name an inspected tree, and this corpus holds every other one
    # under a node id no tree is named for. Both routes a document can be read
    # through decode by ``ledger.read_document``, so a decode the namings
    # reader makes is counted here beside one the namings fallback makes.
    assert decoded <= TREE_COUNT, counts.__dict__
    # The read-only path shares the derivation's value rather than copying it,
    # so no document on it is deep-copied however many readers it has.
    assert counts.deepcopy == 0, counts.__dict__
    # The stat count is reported, not bounded. A screen that never acts on a
    # record older than its run file must stat every per-run document once and
    # every held tree once, and the reader that supplies the namings charges a
    # few more, so a bound tight enough to be meaningful would sit below the
    # base's own cost. The recorded count travels in this run's gate log.
    print(
        f"held-worktree derivation: posix.stat {counts.stat}, "
        f"decoded {decoded}, copy.deepcopy 0 "
        f"over {RECORD_COUNT} documents and {TREE_COUNT} held trees"
    )


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
        "r-20261016T100003-node-03",
        names,
        trees,
        {"r-20261016T100003-node-03": ("node-03", None)},
    )
    assert obligations_module._may_name_a_tree(
        "r-20261016T100005-other-node",
        names,
        trees,
        {"r-20261016T100005-other-node": ("other-node", tree)},
    )
    assert obligations_module._may_name_a_tree(
        "r-20261016T100099-unlisted",
        names,
        trees,
        {"r-20261016T100003-node-03": ("node-03", None)},
    )
    assert not obligations_module._may_name_a_tree(
        "r-20261016T100004-other",
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


def test_a_refused_namings_reader_leaves_every_run_a_candidate(
    home: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused screen costs a read, never a duty.

    The screen's namings come from the merged ledger's reader. A run is still on
    disk in the document its own writer put there whatever that reader does, so
    a refused reader must cost the derivation a full read rather than its
    answer: every run stays a candidate and the per-run documents are read for
    themselves. The refusal is shown to be real before it is relied on, so an
    answer equal to the whole-ledger one is a measurement of the fallback rather
    than of a patch that never took.
    """
    root, _trees = home
    expected = _whole_ledger_held(root)

    def refuse(*_arguments: Any, **_keywords: Any) -> Any:
        raise AssertionError("the merged ledger was read for a screen")

    monkeypatch.setattr(ledger, "runs", refuse)
    with pytest.raises(AssertionError):
        ledger.runs(PROJECT, root=root)

    got = {
        str(path.resolve()): str(record.get("run_id") or "")
        for path, record in obligations_module._held_worktree_records(PROJECT).items()
    }
    assert got == expected
    assert len(expected) == TREE_COUNT


def test_a_damaged_aggregate_leaves_the_run_documents_readable(
    home: tuple[Path, list[Path]],
) -> None:
    """A damaged aggregate costs its own rows, never the per-run documents.

    The aggregate is a copy of the rows each run's own writer already put in a
    file under ``runs/``, so an envelope a truncated write or a conflict marker
    has damaged is no reason to answer with no duties at all. The corruption is
    shown to be real before the fallback read is relied on — the refusing read
    raises on the same file — so rows returned here measure the fallback rather
    than a patch that never took. The bytes are restored whatever happens,
    because the fixture is shared with the other cases.
    """
    root, _trees = home
    path = ledger.ledger_path(PROJECT, root)
    original = path.read_bytes() if path.exists() else None
    try:
        path.write_bytes(b'{"data": {"_version": 3, "runs": [')
        # A damaged envelope is a damaged envelope: the ordinary read refuses it
        # rather than folding it into an empty history.
        with pytest.raises(_store.CorruptEnvelopeError):
            ledger.load(PROJECT, root=root, use_index=False)

        rows = ledger.run_rows_from_documents(PROJECT, root)
        assert {str(row["run_id"]) for row in rows} == {
            source.stem for source in (path.parent / "runs").glob("*.json")
        }
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


def _retain_after_publication(root: Path, run_id: str, tree: Path) -> None:
    """Give one published run's file a retention, in place, as a later write does.

    The rewrite is in place rather than an atomic replacement, which is the
    shape a reader keyed on its directory's identity cannot see.
    """
    path = ledger.ledger_path(PROJECT, root).parent / "runs" / f"{run_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["worktree_retention"] = {
        "worktree": str(tree),
        "retained_at": str(record.get("completed_at") or ""),
    }
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def test_a_run_retaining_a_tree_after_the_index_is_published_is_read(
    home: tuple[Path, list[Path]],
) -> None:
    """A run file changed after publication is answered from, not the snapshot.

    The ledger publishes an index of every run, and a later reader is served it
    while the aggregate it recorded is unchanged. A per-run file can change long
    after that snapshot was written, though: this gives one run's file a
    retention of an inspected tree, under a node id that names no tree, and
    requires the bounded read to answer exactly as the whole-ledger scan does.
    The later record holds the tree, so a screen that served the published
    snapshot would answer with the earlier run that names the tree by its node
    id. This runs last, because the fixture is shared and the rewrite persists.
    """
    root, trees = home
    tree = trees[3]
    # Publish, as the fleet keeps it, then move one run's file afterwards.
    ledger.runs(PROJECT, root=root)
    run_id = "r-20261016T100499-ghost-499"
    _retain_after_publication(root, run_id, tree)

    got = {
        str(path.resolve()): str(record.get("run_id") or "")
        for path, record in obligations_module._held_worktree_records(PROJECT).items()
    }
    expected = _whole_ledger_held(root)
    assert got == expected
    # The later record holds the tree, so the answer is not the earlier run that
    # names it by its node id.
    assert expected[str(tree.resolve())] == run_id
