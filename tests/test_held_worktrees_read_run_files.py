"""A held tree is resolved from the ledger file of the run that named it.

The whole-ledger read is the authority, so every parity case derives its
expectation from that read and compares it with the per-run derivation. The
per-run files are what the fast path opens for a tree, and the aggregate the
derivation also reads is what carries a row the split has written no file for —
the two sources are separated in the never-called case by making the merged
whole-ledger readers raise.

Every record here is built by the producer's own constructor and published
through the producer's own writers, so no case asserts against keys this test
invented.
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
DEFAULT_COMPLETED = OBSERVED_AT - timedelta(seconds=600)


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


def _produced(
    run_id: str,
    node: str,
    *,
    worktree: Path | None = None,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    """One record as the producer builds it, with its retention attached.

    ``build_record`` assembles the row; the retention is attached afterwards
    because that is where the promoter attaches it too.
    """
    completed = completed_at or DEFAULT_COMPLETED
    record = ledger.build_record(
        run_id=run_id,
        plan="fixture-plan",
        gate="passed",
        node=node,
        completed_at=completed.isoformat(),
    )
    if worktree is not None:
        record["worktree_retention"] = {
            "worktree": str(worktree),
            "retained_at": completed.isoformat(),
        }
    else:
        record["worktree_retention"] = None
    return record


def _append(root: Path, record: dict[str, Any]) -> None:
    """Publish one run as the split writes it: its own file, nothing else."""
    ledger.append_run(PROJECT, record, root=root, allow_create=True)


def _aggregate_version(root: Path) -> int:
    return ledger._load_aggregate(PROJECT, root)[1]


def _publish_aggregate_only(root: Path, records: list[dict[str, Any]]) -> None:
    """Publish rows the aggregate carries and the split has written no file for.

    The aggregate writer is the producer's; removing the run file it also
    writes leaves the state of a row the split has not reached, which is how a
    run recorded before the ledger was split is still read today.
    """
    data, _version = ledger._load_aggregate(PROJECT, root)
    wanted = {str(record["run_id"]): record for record in records}
    rows = [row for row in data["runs"] if str(row.get("run_id")) not in wanted]
    rows += [dict(record) for record in records]
    ledger.write(
        PROJECT,
        {"members": data["members"], "runs": rows, "holds": data["holds"]},
        _aggregate_version(root),
        root=root,
    )
    for record in records:
        ledger.run_path(PROJECT, str(record["run_id"]), root).unlink(missing_ok=True)


def _whole_ledger_held(root: Path, session: str) -> set[str]:
    """Derive the held set the way the whole-ledger scan does.

    This is the parity expectation: every recorded run is read from both
    sources, merged in the ledger's own order, and a retained tree the scan
    reaches is attributed to the last record that names it under the session's
    directory.
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

    _publish_aggregate_only(
        fleet,
        [
            _produced(
                "r-20261001T090200000000-gamma-aggregate-only",
                "gamma-aggregate-only",
                worktree=gamma,
            )
        ],
    )
    _append(
        fleet,
        _produced("r-20261001T090000000000-alpha-holds", "alpha-holds", worktree=alpha),
    )
    _append(
        fleet,
        _produced("r-20261001T090100000000-beta-holds", "beta-holds", worktree=beta),
    )

    held = _held(fleet)

    assert set(held) == _whole_ledger_held(fleet, SESSION)
    assert set(held) == {
        "r-20261001T090000000000-alpha-holds",
        "r-20261001T090100000000-beta-holds",
        "r-20261001T090200000000-gamma-aggregate-only",
    }
    assert held["r-20261001T090000000000-alpha-holds"]["kind"] == "worktree-held"
    assert held["r-20261001T090000000000-alpha-holds"]["age_seconds"] == 600


# The mixed states a held tree can be recorded in. Two runs of one node whose
# completion order reverses their dispatch order, and a tie on the completion
# stamp, are the orderings the two sources disagree about.
MIXED_STATES = (
    "aggregate-only",
    "per-run-only",
    "file-and-aggregate-row",
    "completion-reverses-dispatch",
    "completed-at-tie",
)


def _build_case(root: Path, case: str) -> str:
    """Publish one mixed state and return the run id it holds the tree for."""
    tree = _worktree(root, "alpha-holds")
    early = "r-20261001T090000000000-alpha-holds"
    late = "r-20261001T100000000000-alpha-holds"
    if case == "aggregate-only":
        _publish_aggregate_only(root, [_produced(early, "alpha-holds", worktree=tree)])
        return early
    if case == "per-run-only":
        _append(root, _produced(early, "alpha-holds", worktree=tree))
        return early
    if case == "file-and-aggregate-row":
        # The newer run has no file, the aggregate carries it, and the ledger
        # attributes the tree to it.
        _append(root, _produced(early, "alpha-holds", worktree=tree))
        _publish_aggregate_only(root, [_produced(late, "alpha-holds", worktree=tree)])
        return late
    if case == "completion-reverses-dispatch":
        _append(
            root,
            _produced(
                early,
                "alpha-holds",
                worktree=tree,
                completed_at=OBSERVED_AT - timedelta(minutes=10),
            ),
        )
        _publish_aggregate_only(
            root,
            [
                _produced(
                    late,
                    "alpha-holds",
                    worktree=tree,
                    completed_at=OBSERVED_AT - timedelta(minutes=60),
                )
            ],
        )
        return early
    if case == "completed-at-tie":
        for run_id in (early, late):
            _append(
                root,
                _produced(
                    run_id,
                    "alpha-holds",
                    worktree=tree,
                    completed_at=OBSERVED_AT - timedelta(minutes=30),
                ),
            )
        return late
    raise AssertionError(f"unknown mixed state {case!r}")


@pytest.mark.parametrize("case", MIXED_STATES)
def test_a_mixed_state_goes_to_the_run(fleet: Path, case: str) -> None:
    """Every mixed state names the same run as the whole-ledger scan."""
    expected = _build_case(fleet, case)

    held = _held(fleet)

    assert set(held) == _whole_ledger_held(fleet, SESSION), case
    assert set(held) == {expected}, case


def test_inspected_runs_are_read_from_their_own_files(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With each tree named by one run's file, the merged ledger is not read."""
    expected = set()
    for index, node in enumerate(("alpha-holds", "beta-holds", "gamma-held")):
        path = _worktree(fleet, node)
        run_id = f"r-20261001T0900{index:02d}000000-{node}"
        _append(fleet, _produced(run_id, node, worktree=path))
        expected.add(run_id)

    def _refuse(*_arguments: Any, **_keywords: Any) -> Any:
        raise AssertionError("the merged ledger was read for a tree a file names")

    monkeypatch.setattr(ledger, "runs", _refuse)
    monkeypatch.setattr(ledger, "load", _refuse)

    # The refusal is real: the patched readers raise when they are reached.
    with pytest.raises(AssertionError):
        ledger.runs(PROJECT, root=fleet)
    with pytest.raises(AssertionError):
        ledger.load(PROJECT, root=fleet)

    assert set(_held(fleet)) == expected


def test_a_tree_a_live_run_occupies_is_not_held(fleet: Path) -> None:
    """A tree a live pointer names is skipped without reading any record."""
    held_tree = _worktree(fleet, "alpha-holds")
    live_tree = _worktree(fleet, "beta-live")
    _append(
        fleet,
        _produced(
            "r-20261001T090000000000-alpha-holds", "alpha-holds", worktree=held_tree
        ),
    )
    live_run = "r-20261001T090100000000-beta-live"
    _append(fleet, _produced(live_run, "beta-live", worktree=live_tree))
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
