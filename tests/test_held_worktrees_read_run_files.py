"""A held tree is resolved from the ledger file of the run that named it.

The whole-ledger read is the authority, so the parity case derives its
expectation from that read and compares it with the per-run derivation. The
per-run files are what the derivation is allowed to open for a tree it
inspects, and the whole ledger is what it may fall back to for a tree no
per-run file names — the two reads are separated here by making the whole-ledger
readers raise.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "held-tree-fixture"
SESSION = "s21-fixture"
OBSERVED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the held-tree fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _worktree(root: Path, node: str) -> Path:
    """Register a tree the way a dispatched run leaves one behind."""
    path = root.parent / "managed-worktrees" / SESSION / node
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _record(
    run_id: str,
    node: str,
    *,
    worktree: Path | None = None,
    retained_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    completed = completed_at or (retained_at or OBSERVED_AT) - timedelta(seconds=600)
    record: dict[str, Any] = {
        "run_id": run_id,
        "plan": "fixture-plan",
        "node": node,
        "completed_at": completed.isoformat(),
        "worktree_retention": (
            {
                "worktree": str(worktree),
                "retained_at": (retained_at or OBSERVED_AT).isoformat(),
            }
            if worktree is not None
            else None
        ),
    }
    return record


def _write_run_file(root: Path, record: dict[str, Any]) -> Path:
    path = ledger.run_path(PROJECT, str(record["run_id"]), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ledger.serialize_run(record), encoding="utf-8")
    return path


def _write_aggregate(root: Path, records: list[dict[str, Any]]) -> None:
    ledger.ledger_path(PROJECT, root).write_text(
        json.dumps(
            {
                "updated": OBSERVED_AT.isoformat(),
                "project": PROJECT,
                "doc": "crew",
                "data": {
                    "members": [],
                    "runs": records,
                    "holds": [],
                    "_version": 1,
                },
            }
        ),
        encoding="utf-8",
    )


def _whole_ledger_held(root: Path, session: str) -> set[str]:
    """Derive the held set the way the whole-ledger scan does.

    This is the parity expectation: every recorded run is read, and a retained
    tree the scan reaches is attributed to the last record that names it under
    the session's directory.
    """
    registered = {
        path
        for path in (
            Path(line.removeprefix("worktree ")).resolve()
            for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        )
        if path != root.resolve() and path.parent.name == session
    }
    occupied = {
        Path(str(pointer.get("worktree") or "")).expanduser().resolve()
        for pointer in runs.list_live(project=PROJECT)
        if str(pointer.get("worktree") or "").strip()
    }
    matched: dict[Path, dict[str, Any]] = {}
    for record in ledger.runs(PROJECT, root=root):
        retention = record.get("worktree_retention")
        if isinstance(retention, dict):
            value = str(retention.get("worktree") or "").strip()
            if value:
                retained = Path(value).expanduser().resolve()
                if retained in registered and retained not in occupied:
                    matched[retained] = record
        node = record.get("node")
        node_id = (
            str(node.get("id") or "") if isinstance(node, dict) else str(node or "")
        )
        for path in registered:
            if node_id and path.name == node_id and path not in occupied:
                matched[path] = record
    return {str(record.get("run_id") or "") for record in matched.values()}


def _held(root: Path, session: str = SESSION) -> dict[str, dict[str, Any]]:
    return {
        str(item["run_id"]): item
        for item in obligations_module._held_worktrees(
            PROJECT, session, now=OBSERVED_AT
        )
    }


def test_the_held_set_equals_the_whole_ledger_derivation(fleet: Path) -> None:
    """Parity: the per-run derivation agrees with the whole-ledger one."""
    alpha = _worktree(fleet, "alpha-holds")
    beta = _worktree(fleet, "beta-holds")
    gamma = _worktree(fleet, "gamma-aggregate-only")
    _worktree(fleet, "delta-named-by-nothing")

    _write_run_file(
        fleet,
        _record("r-20261001T090000000000-alpha-holds", "alpha-holds", worktree=alpha),
    )
    _write_run_file(
        fleet,
        _record("r-20261001T090100000000-beta-holds", "beta-holds", worktree=beta),
    )
    # Recorded before the ledger was split: no file of its own.
    _write_aggregate(
        fleet,
        [
            _record(
                "r-20261001T090200000000-gamma-aggregate-only",
                "gamma-aggregate-only",
                worktree=gamma,
            )
        ],
    )

    held = _held(fleet)

    assert set(held) == _whole_ledger_held(fleet, SESSION)
    assert set(held) == {
        "r-20261001T090000000000-alpha-holds",
        "r-20261001T090100000000-beta-holds",
        "r-20261001T090200000000-gamma-aggregate-only",
    }
    assert held["r-20261001T090000000000-alpha-holds"]["kind"] == "worktree-held"
    assert held["r-20261001T090000000000-alpha-holds"]["age_seconds"] == 0


def test_a_tree_named_by_two_runs_follows_the_held_ledger_order(fleet: Path) -> None:
    """A node run twice is held for the run the whole ledger lists last.

    The ledger orders the runs it merges by completion, not by dispatch, so a
    node whose later dispatch finished first is held for the run that
    dispatched first — an order the per-run files are not listed in and cannot
    decide between them.
    """
    tree = _worktree(fleet, "alpha-holds")
    dispatched_first = "r-20261001T090000000000-alpha-holds"
    dispatched_later = "r-20261001T100000000000-alpha-holds"
    _write_run_file(
        fleet,
        _record(
            dispatched_first,
            "alpha-holds",
            worktree=tree,
            completed_at=datetime(2026, 10, 1, 11, 50, tzinfo=UTC),
        ),
    )
    _write_run_file(
        fleet,
        _record(
            dispatched_later,
            "alpha-holds",
            worktree=tree,
            completed_at=datetime(2026, 10, 1, 11, 0, tzinfo=UTC),
        ),
    )

    held = _held(fleet)

    assert set(held) == _whole_ledger_held(fleet, SESSION)
    assert set(held) == {dispatched_first}


def test_inspected_runs_are_read_from_their_own_files(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With each inspected tree named by one run's file, the ledger is not read."""
    expected = set()
    for index, node in enumerate(("alpha-holds", "beta-holds", "gamma-held")):
        path = _worktree(fleet, node)
        run_id = f"r-20261001T0900{index:02d}000000-{node}"
        _write_run_file(fleet, _record(run_id, node, worktree=path))
        expected.add(run_id)

    def _refuse(*_arguments: Any, **_keywords: Any) -> Any:
        raise AssertionError("the whole ledger was loaded for a tree it named")

    monkeypatch.setattr(ledger, "runs", _refuse)
    monkeypatch.setattr(ledger, "load", _refuse)

    # The refusal is real: the patched readers raise when they are reached.
    with pytest.raises(AssertionError):
        ledger.runs(PROJECT, root=fleet)
    with pytest.raises(AssertionError):
        ledger.load(PROJECT, root=fleet)

    assert set(_held(fleet)) == expected


def test_a_run_without_its_own_file_resolves_through_the_fallback(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree no per-run file names is resolved against the whole ledger."""
    modern = _worktree(fleet, "alpha-holds")
    legacy = _worktree(fleet, "beta-aggregate-only")
    _write_run_file(
        fleet,
        _record("r-20261001T090000000000-alpha-holds", "alpha-holds", worktree=modern),
    )
    _write_aggregate(
        fleet,
        [
            _record(
                "r-20261001T090100000000-beta-aggregate-only",
                "beta-aggregate-only",
                worktree=legacy,
            )
        ],
    )

    original = ledger.runs
    calls: list[str] = []

    def _spy(project: str, root: str | Path | None = None, **keywords: Any) -> Any:
        calls.append(project)
        return original(project, root=root, **keywords)

    monkeypatch.setattr(ledger, "runs", _spy)
    held = _held(fleet)

    assert set(held) == {
        "r-20261001T090000000000-alpha-holds",
        "r-20261001T090100000000-beta-aggregate-only",
    }
    assert calls == [PROJECT]


def test_a_tree_a_live_run_occupies_is_not_held(fleet: Path) -> None:
    """A tree a live pointer names is skipped without reading any record."""
    held_tree = _worktree(fleet, "alpha-holds")
    live_tree = _worktree(fleet, "beta-live")
    _write_run_file(
        fleet,
        _record(
            "r-20261001T090000000000-alpha-holds", "alpha-holds", worktree=held_tree
        ),
    )
    live_run = "r-20261001T090100000000-beta-live"
    _write_run_file(fleet, _record(live_run, "beta-live", worktree=live_tree))
    runs._write_json(
        runs.pointer_path(live_run),
        {
            "run_id": live_run,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "worktree": str(live_tree),
            "node": {"id": "beta-live", "plan": "fixture-plan"},
        },
    )

    assert set(_held(fleet)) == {"r-20261001T090000000000-alpha-holds"}
