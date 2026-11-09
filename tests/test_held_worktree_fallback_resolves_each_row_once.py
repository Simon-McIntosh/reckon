"""The held-tree fallback resolves each row's retained path a bounded number of times.

A registered tree that no screened row names cannot be answered from the index
built from the records' own naming keys, so the derivation falls back to the
rows the screen left out. This module builds a fleet large enough that the old
per-tree pass is the product of records and unresolved trees, then shows that
one :func:`reckon.crew.obligations._held_worktree_records` call resolves each
row's retained path at most twice over both of its bindings however many trees
fall back, and that the map it answers is the one
:func:`reckon.crew.routing.record_names_worktree` gives when applied to every
row and tree.

The fixture holds trees named by a retained path, by a session and node id, by
two records where the later one holds the tree, a tree named only by a row
carrying no run id, and twenty trees no record names. Every record is built by
the producer's own constructor and published through the producer's own writer,
so no case asserts against keys this test invented.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import _store, file_memo, ledger
from reckon.crew import routing
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "held-tree-fallback-fixture"
SESSION = "fallback-once-fixture"
NAMED_TREE_COUNT = 20
UNNAMED_TREE_COUNT = 20
GHOST_RECORD_COUNT = 460
RECORD_COUNT = 2 * NAMED_TREE_COUNT + GHOST_RECORD_COUNT
ORPHAN_TREE_NAME = "node-orphan-named"
BASE_COMPLETED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC) - timedelta(seconds=5000)


def _tree_name(index: int) -> str:
    return f"node-{index:02d}"


def _worktree(root: Path, node: str) -> Path:
    """Register a tree the way a dispatched run leaves one behind."""
    path = root.parent / "managed-worktrees" / SESSION / node
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _orphan_row() -> dict[str, Any]:
    """A run row carrying no run id, naming one tree by its session and node.

    The merged reader can return such a row: the aggregate is a list of rows and
    nothing in it requires a run id, while a per-run document must hold the run
    it is named for. The screen has no namings entry for this row, so the tree it
    names is the one a fallback over the left-out rows must still find.
    """
    return {
        "node": ORPHAN_TREE_NAME,
        "session": SESSION,
        "completed_at": (BASE_COMPLETED + timedelta(seconds=3000)).isoformat(),
    }


@pytest.fixture(scope="module")
def _fleet_build(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, Path, list[Path], list[Path], list[dict[str, Any]]]]:
    """The costly half of the fleet: a synthesised checkout and its records.

    Built once for the module and read by every case without writing. The
    environment is named only where the build needs it and put back before this
    fixture returns, and what it puts back is asserted when the fixture ends: a
    module-scoped fixture outlives the tests that use it, and a worker running
    several test modules on one process would otherwise carry this module's
    redirected home into whatever ran next. Returns the temporary root the
    per-test home is made under, the repository root, the named trees, the trees
    no record names, and the records as the producer wrote them.
    """
    tmp_path = tmp_path_factory.mktemp("held-tree-fallback")
    config_home = tmp_path / "config"
    config_home.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the held-tree fallback fixture"),
    ):
        _git(root, *arguments)
    mounts_path = config_home / "mounts.json"
    mounts_path.write_text(json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8")

    # Publishing a run resolves the checkout that owns the project through the
    # mount registry, and this module-scoped build runs before the per-test home
    # fixture exists. The keys are therefore named for the length of the build
    # and put back before it returns, so nothing here outlives the setup that
    # needed it.
    previous = {
        key: os.environ.get(key)
        for key in ("RECKON_HOME", "RECKON_STATE_ROOT", "RECKON_MOUNTS_PATH")
    }
    os.environ["RECKON_HOME"] = str(config_home)
    os.environ["RECKON_STATE_ROOT"] = str(state_root)
    os.environ["RECKON_MOUNTS_PATH"] = str(mounts_path)
    try:
        named = [
            _worktree(root, _tree_name(index)) for index in range(NAMED_TREE_COUNT)
        ]
        unnamed = [
            _worktree(root, f"unheld-{index:02d}")
            for index in range(UNNAMED_TREE_COUNT)
        ]
        _worktree(root, ORPHAN_TREE_NAME)
        records: list[dict[str, Any]] = []
        for index in range(RECORD_COUNT):
            completed = BASE_COMPLETED + timedelta(seconds=index)
            run_id = f"r-20261016T{100000 + index:06d}-r{index:03d}"
            # The first block names a tree by the path its retention carries. The
            # second names the same trees by session and node id and completes
            # later, so the tree two records name goes to the later one. The
            # remainder name no tree at all.
            retained: Path | None
            if index < NAMED_TREE_COUNT:
                node = _tree_name(index)
                retained = named[index]
            elif index < 2 * NAMED_TREE_COUNT:
                node = _tree_name(index - NAMED_TREE_COUNT)
                retained = None
            else:
                node = f"ghost-{index:03d}"
                retained = None
            record = ledger.build_record(
                run_id=run_id,
                plan="fixture-plan",
                gate="passed",
                node=node,
                session=SESSION,
                completed_at=completed.isoformat(),
            )
            record["worktree_retention"] = (
                {"worktree": str(retained), "retained_at": completed.isoformat()}
                if retained is not None
                else None
            )
            ledger.append_run(PROJECT, record, root=root, allow_create=True)
            records.append(record)
        # The row carrying no run id lives in the aggregate, the one source a row
        # without a run id can come from.
        aggregate_path = ledger.ledger_path(PROJECT, root)
        data, version = _store._load_json_envelope(aggregate_path)
        data.setdefault("members", [])
        data.setdefault("holds", [])
        data.setdefault("runs", [])
        data["runs"].append(_orphan_row())
        _store._write_json_envelope(
            aggregate_path, PROJECT, "ledger", data, expected_version=version
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    yield tmp_path, root, named, unnamed, records
    # Checked when this fixture finalises, which is after every case that used
    # ``fleet`` has ended and restored what it named. A home left pointed here
    # would answer a peer module's lookup on the same worker with a project its
    # mounts file does not carry, and that is a failure of this module however
    # green its own cases were — so it is asserted rather than assumed.
    leaked = {
        key: os.environ.get(key)
        for key in ("RECKON_HOME", "RECKON_STATE_ROOT", "RECKON_MOUNTS_PATH")
        if os.environ.get(key) is not None
        and os.environ.get(key) in {str(config_home), str(state_root), str(mounts_path)}
    }
    assert not leaked, (
        "the fixture left the environment naming its own configuration home, "
        f"which outlives this module: {leaked}"
    )


@pytest.fixture
def fleet(
    _fleet_build: tuple[Path, Path, list[Path], list[Path], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[Path], list[Path], list[dict[str, Any]]]:
    """Point this test, and only this test, at the fixture's checkout.

    The project is resolved through the configuration home, so the derivation
    reads the fixture's checkout only while this test runs. The memo is dropped
    first, so a case that patches a reader measures its own call rather than a
    value a previous case left behind.
    """
    tmp_path, root, named, unnamed, records = _fleet_build
    config_home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    file_memo.clear()
    return root, named, unnamed, records


def _registered_trees(root: Path) -> list[Path]:
    """Every registered tree of the fixture checkout, in git's own order."""
    return [
        Path(line.removeprefix("worktree ")).resolve()
        for line in _git(root, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def _reference_held(rows: Sequence[Mapping[str, Any]], root: Path) -> dict[str, str]:
    """The held tree -> run mapping the naming rule itself produces.

    The reference is computed here rather than taken from the derivation: for
    every row and every registered tree, in the reader's order, the rule decides
    whether the row names the tree, and the last row that names it holds it. A
    tree whose holding row reports it released is dropped.
    """
    registered = [path for path in _registered_trees(root) if path != root.resolve()]
    holders: dict[str, Mapping[str, Any]] = {}
    for record in rows:
        for tree in registered:
            if routing.record_names_worktree(record, tree):
                holders[str(tree)] = record
    held: dict[str, str] = {}
    for tree, record in holders.items():
        release = record.get("release")
        if isinstance(release, Mapping) and release.get("worktree_released") is True:
            continue
        held[tree] = str(record.get("run_id") or "")
    return held


def _head_held() -> dict[str, str]:
    """The derivation's answer, keyed the way the reference keys it."""
    return {
        str(path.resolve()): str(record.get("run_id") or "")
        for path, record in obligations_module._held_worktree_records(PROJECT).items()
    }


def test_the_fixture_holds_trees_no_record_names(
    fleet: tuple[Path, list[Path], list[Path], list[dict[str, Any]]],
) -> None:
    """The fixture is large enough that a per-tree pass is the product."""
    root, named, unnamed, records = fleet
    assert len(records) >= 500
    assert len(unnamed) >= 20
    assert len(named) >= 20
    reference = _reference_held(ledger.runs(PROJECT, root=root), root)
    named_paths = {str(path.resolve()) for path in named}
    assert named_paths <= set(reference), (
        "the fixture's named trees are not all held by a record that names them"
    )
    unheld = {str(path.resolve()) for path in unnamed}
    assert not (unheld & set(reference)), (
        "a tree the fixture builds to be unnamed is named by a record"
    )
    assert len(records) * len(unnamed) > len(records) + len(unnamed)


def test_the_held_map_is_the_naming_rule_applied_to_every_row(
    fleet: tuple[Path, list[Path], list[Path], list[dict[str, Any]]],
) -> None:
    """Every tree the rule holds is held, by the same run, and no other is."""
    root, named, unnamed, _records = fleet
    reference = _reference_held(ledger.runs(PROJECT, root=root), root)
    assert _head_held() == reference
    assert {str(path.resolve()) for path in named} <= set(reference)
    assert not ({str(path.resolve()) for path in unnamed} & set(reference))


def test_the_tree_only_a_run_less_row_names_is_held(
    fleet: tuple[Path, list[Path], list[Path], list[dict[str, Any]]],
) -> None:
    """The row the screen cannot read still holds its tree."""
    root, _named, _unnamed, _records = fleet
    orphan_tree = str(
        (root.parent / "managed-worktrees" / SESSION / ORPHAN_TREE_NAME).resolve()
    )
    reference = _reference_held(ledger.runs(PROJECT, root=root), root)
    assert reference.get(orphan_tree) == "", (
        "the fixture's run-less row does not hold the tree it names"
    )
    assert _head_held().get(orphan_tree) == ""


def test_the_fallback_resolves_each_row_a_bounded_number_of_times(
    fleet: tuple[Path, list[Path], list[Path], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call resolves a record's retained path at most twice, over both bindings.

    The path resolution is wrapped through both names it is reachable by — the
    module that defines it and the name this module imports — so a resolution
    made by the index, the screen or the fallback is counted wherever it
    happens. The instrument is shown to record a call it is asked to make, so a
    later small count is an absence of resolutions rather than of measurement.
    Twenty registered trees that no record names fall back, which is where the
    old per-tree pass resolved every row's path once per tree; the answer is
    still the reference's.
    """
    root, _named, _unnamed, records = fleet
    rows = ledger.runs(PROJECT, root=root)
    calls = {"n": 0}
    real_routing = routing._retained_worktree
    real_imported = obligations_module._retained_worktree

    def counting_routing(record: Mapping[str, Any]) -> Path | None:
        calls["n"] += 1
        return real_routing(record)

    def counting_imported(record: Mapping[str, Any]) -> Path | None:
        calls["n"] += 1
        return real_imported(record)

    monkeypatch.setattr(routing, "_retained_worktree", counting_routing)
    monkeypatch.setattr(obligations_module, "_retained_worktree", counting_imported)

    # Positive control: each binding is shown to record the call it is asked to
    # make, so a later count is a measurement rather than a dead instrument.
    counting_routing(rows[0])
    counting_imported(rows[0])
    assert calls["n"] == 2

    calls["n"] = 0
    file_memo.clear()
    held = _head_held()
    # Read the count before the reference is computed: the reference applies the
    # naming rule to every row and tree, so its own resolutions would otherwise
    # be counted as the derivation's.
    resolution_count = calls["n"]

    assert len(held) == NAMED_TREE_COUNT + 1
    print(
        f"held-worktree fallback: {resolution_count} path resolutions over "
        f"{len(rows)} records and {len(_unnamed)} trees no record names"
    )
    assert resolution_count <= 2 * len(rows), (
        f"{resolution_count} path resolutions over {len(rows)} records; the "
        "fallback is resolving each row's path once per unresolved tree"
    )
    assert held == _reference_held(rows, root)
    assert len(records) == RECORD_COUNT


def test_a_refused_namings_reader_keeps_the_same_answer(
    fleet: tuple[Path, list[Path], list[Path], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the screen dropped, every run is a candidate and the answer is the same.

    ``_run_namings`` returning ``None`` is how the reader that supplies the
    namings reports a refusal, so the screen cannot be built. Every row carrying
    a run id is then a candidate, the run documents are read for themselves, and
    the tree named only by the run-less row must still be held. The refusal is
    shown to be real before it is relied on, so an answer equal to the reference
    is a measurement of this path rather than of a patch that never took.
    """
    root, named, unnamed, _records = fleet
    orphan_tree = str(
        (root.parent / "managed-worktrees" / SESSION / ORPHAN_TREE_NAME).resolve()
    )

    monkeypatch.setattr(obligations_module, "_run_namings", lambda *a, **k: None)
    assert obligations_module._run_namings(PROJECT, root) is None

    file_memo.clear()
    held = _head_held()

    assert held == _reference_held(ledger.run_rows_from_documents(PROJECT, root), root)
    assert {str(path.resolve()) for path in named} <= set(held)
    assert not ({str(path.resolve()) for path in unnamed} & set(held))
    assert held.get(orphan_tree) == ""
