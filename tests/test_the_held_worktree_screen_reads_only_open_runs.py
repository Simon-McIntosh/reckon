"""The held-worktree screen takes its bounded open-run path on a steady tick.

The screen matches registered trees against run records. Its bounded path asks
the open-run projection for the open rows and for the rows that name a held
tree; its fallback decodes every stored run the project ever promoted. Over a
crew home of :data:`RECORD_COUNT` promoted runs, :data:`OPEN_RUNS` live
pointers and two held worktrees — one named by its run's node id and one named
only by the path its retention carries — this module counts the stored rows a
fleet-state derivation decodes, and shows the second derivation of an unchanged
home decodes at most :data:`DECODE_BOUND` of them.

The rows are counted through a recording stand-in on
``run_store._record_rows``, the one join every stored read goes through. The
instrument is shown to see rows before an absence is reported: a reference
derivation whose bounded path is refused the way the base revision refuses it
decodes the whole population, so a second derivation of five rows measures the
screen and not a counter that never fired.

The process memo and the screen's own incremental index are dropped between
derivations, so each derivation measures its own read rather than the answer
the previous one left in memory. That makes the measurement harder than a
station tick, not easier: a memo that answers hands back a value no read
produced, and the bound still holds when every derivation reads cold.

The held trees are the ones the naming rule holds, which is a tree carrying a
commit its primary branch does not reach, so every fixture tree is left holding
one. A clean tree would be measured against a rule this file is not about.

On a tree without the bounded path the second derivation decodes the whole
population, and this file's decode assertion fails there — which is what the
node's negative control records.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger, run_store
from reckon.crew import obligation_snapshot, open_run_index, runs
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "held-screen-fixture"
SESSION = "held-screen"
RECORD_COUNT = 2000
OPEN_RUNS = 3
DECODE_BOUND = 5
BASE_COMPLETED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NODE_NAMED_RUN = "r-20261016T100000-node-00"
PATH_NAMED_RUN = "r-20261016T100001-some-other-node"


def _worktree(root: Path, name: str) -> Path:
    """Register a tree the way a dispatched run leaves one behind.

    The tree carries a commit its primary branch does not reach, because a
    retained tree is a duty only while it holds work no landing has taken.
    """
    path = root.parent / "managed-worktrees" / SESSION / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    _git(path, "commit", "-q", "--allow-empty", "-m", f"test: work in {name}")
    return path


def _promoted(
    root: Path,
    run_id: str,
    *,
    node: str,
    completed: datetime,
    retained: Path | None = None,
) -> None:
    """Promote one run through the ledger's own writer."""
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


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[Path]]:
    """A synthesised checkout whose store holds the measured population.

    The environment is named only for the build and put back before the fixture
    returns, because a module-scoped fixture outlives the cases that use it.
    The projection is warmed here as a station holds it, so the measurement is
    of a derivation rather than of a cold projection build.
    """
    tmp_path = tmp_path_factory.mktemp("held-screen")
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
        ("commit", "-q", "-m", "test: seed the held-screen fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    previous = {
        key: os.environ.get(key)
        for key in ("RECKON_HOME", "RECKON_STATE_ROOT", "RECKON_MOUNTS_PATH")
    }
    os.environ["RECKON_HOME"] = str(config_home)
    os.environ["RECKON_MOUNTS_PATH"] = str(config_home / "mounts.json")
    os.environ.pop("RECKON_STATE_ROOT", None)
    try:
        trees = [_worktree(root, "node-00"), _worktree(root, "node-01")]
        # One tree is named by the run's own node id, the other only by the
        # path its retention carries, so both naming routes are populated.
        _promoted(
            root,
            NODE_NAMED_RUN,
            node=trees[0].name,
            completed=BASE_COMPLETED,
            retained=trees[0],
        )
        _promoted(
            root,
            PATH_NAMED_RUN,
            node="some-other-node",
            completed=BASE_COMPLETED + timedelta(seconds=1),
            retained=trees[1],
        )
        # The rest of the population is promoted history no tree names, written
        # to the store the screen would otherwise decode in full.
        for index in range(2, RECORD_COUNT):
            completed = BASE_COMPLETED + timedelta(seconds=index)
            record = ledger.build_record(
                run_id=f"r-20261016T{100000 + index:06d}-ghost-{index:03d}",
                plan="fixture-plan",
                gate="passed",
                node=f"ghost-{index:03d}",
                session=SESSION,
                completed_at=completed.isoformat(),
            )
            record["worktree_retention"] = None
            run_store.append(PROJECT, record)
        live = runs.live_dir()
        live.mkdir(parents=True, exist_ok=True)
        for index in range(OPEN_RUNS):
            (live / f"r-20261117T12000{index}-open.json").write_text(
                json.dumps(
                    {
                        "project": PROJECT,
                        "run_id": f"r-20261117T12000{index}-open-node-{index}",
                        "session": SESSION,
                        "node": {"id": f"open-node-{index}"},
                    }
                ),
                encoding="utf-8",
            )
        # The station holds a published projection: build it before anything is
        # counted, so the measurement is of the derivation and not of it.
        ledger.open_runs(PROJECT, root)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return root, trees


@pytest.fixture
def fixture_home(
    home: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[Path]]:
    """Point this test, and only this test, at the fixture's checkout."""
    root, trees = home
    tmp_config = root.parent / "config"
    monkeypatch.setenv("RECKON_HOME", str(tmp_config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_config / "mounts.json"))
    return root, trees


class _Decodes:
    """A recording stand-in for the one join every stored read goes through.

    The counter is enabled around a derivation, so a read the fixture or the
    projection makes is not attributed to the screen.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rows = 0
        self.active = False
        real = run_store._record_rows

        def record_rows(*arguments: Any, **keywords: Any) -> Any:
            for row in real(*arguments, **keywords):
                if self.active:
                    self.rows += 1
                yield row

        monkeypatch.setattr(run_store, "_record_rows", record_rows)

    def derivation(self) -> tuple[dict[str, list[dict[str, Any]]], int]:
        """One fleet-state derivation and the stored rows it decoded."""
        obligations_module._clear_cached_obligation_inputs()
        self.rows = 0
        self.active = True
        try:
            state = obligation_snapshot.fleet_state(PROJECT)
        finally:
            self.active = False
        return state.held_worktrees, self.rows


def _duties(state: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """The tree -> holding run names a fleet state's held duties carry."""
    return {
        session: sorted(str(duty.get("run_id") or "") for duty in duties)
        for session, duties in state.items()
        if duties
    }


def test_a_second_derivation_reads_only_the_open_rows(
    fixture_home: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steady tick decodes the open rows, not the promoted population."""
    root, _trees = fixture_home
    reference = obligations_module._held_records_pass(PROJECT).trees()
    assert sorted(str(tree) for tree in reference) == sorted(
        str(path) for path in _trees
    )
    # The reference read is the one the base revision takes: the bounded path
    # is refused, so the whole promoted population is decoded and the counter
    # is shown to see rows before an absence is reported.
    counts = _Decodes(monkeypatch)
    real = open_run_index.matches_sources
    monkeypatch.setattr(open_run_index, "matches_sources", lambda *a, **k: False)
    reference_state, reference_rows = counts.derivation()
    assert reference_rows >= RECORD_COUNT, reference_rows
    monkeypatch.setattr(open_run_index, "matches_sources", real)

    first_state, first_rows = counts.derivation()
    second_state, second_rows = counts.derivation()

    print(
        f"held-worktree screen: reference decodes {reference_rows} rows, "
        f"first derivation {first_rows}, second derivation {second_rows} "
        f"over {RECORD_COUNT} promoted runs and {OPEN_RUNS} open runs"
    )
    # The bound is the node's measure, asserted before anything else a base
    # tree would fail: on the base revision this is the whole population.
    assert second_rows <= DECODE_BOUND, second_rows
    assert first_rows <= DECODE_BOUND, first_rows
    # The bounded path was taken, which is a marker this change introduced
    # rather than the absence of the fallback.
    signature = obligations_module._held_records_signature(PROJECT, root)
    assert signature is not None
    assert open_run_index.matches_sources(
        PROJECT, obligations_module._ledger_read_root(PROJECT, root), signature
    )
    # Both derivations answer what the whole-population read answered.
    assert _duties(first_state) == _duties(reference_state)
    assert _duties(second_state) == _duties(reference_state)
    assert _duties(second_state) == {SESSION: sorted([NODE_NAMED_RUN, PATH_NAMED_RUN])}


def test_a_run_retaining_a_new_tree_is_reported_by_the_next_derivation(
    fixture_home: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree a new run retains reaches the screen on the next derivation."""
    root, _trees = fixture_home
    counts = _Decodes(monkeypatch)
    before, _rows = counts.derivation()
    assert SESSION in _duties(before)

    tree = _worktree(root, "node-fresh")
    fresh_run = "r-20261117T130000-node-fresh"
    _promoted(
        root,
        fresh_run,
        node=tree.name,
        completed=BASE_COMPLETED + timedelta(seconds=RECORD_COUNT + 10),
        retained=tree,
    )
    after, _rows = counts.derivation()
    assert fresh_run in _duties(after)[SESSION]
    assert str(tree) in {
        str(path) for path in obligations_module._held_records_pass(PROJECT).trees()
    }
