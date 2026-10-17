"""A release changes only the promoted run's durable record."""

from __future__ import annotations

import json
from pathlib import Path

from reckon import ledger
from reckon.crew.promotion_release import _record_release_on_ledger


def _project_with_split_runs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repository"
    aggregate = ledger.ledger_path("sample", repo)
    aggregate.parent.mkdir(parents=True)
    rows = [{"run_id": f"aggregate-{index}"} for index in range(10)]
    ledger.write("sample", {"members": [], "runs": rows, "holds": []}, 0, root=repo)
    for index in range(50):
        path = ledger.run_path("sample", f"split-{index}", repo)
        path.parent.mkdir(exist_ok=True)
        path.write_text(ledger.serialize_run({"run_id": f"split-{index}"}))
    return repo, aggregate


def test_release_of_aggregate_run_does_not_fold_split_runs(tmp_path, monkeypatch):
    repo, aggregate = _project_with_split_runs(tmp_path, monkeypatch)
    before = {
        path: path.read_bytes()
        for path in aggregate.parent.joinpath("runs").glob("*.json")
    }
    assert len(before) == 50
    assert len(ledger._load_aggregate("sample", repo)[0]["runs"]) == 10

    receipt = {"worktree_released": True}
    recorded, row = _record_release_on_ledger(
        project="sample",
        root=repo,
        run_id="aggregate-0",
        release=receipt,
        checkout=None,
    )

    rows = ledger._load_aggregate("sample", repo)[0]["runs"]
    assert recorded == receipt
    assert row is not None and row["release"] == receipt
    assert len(rows) <= 11, (
        f"release folded {len(rows) - 10} split runs into the aggregate"
    )
    assert len(rows) == 10
    assert (
        next(item for item in rows if item["run_id"] == "aggregate-0")["release"]
        == receipt
    )
    assert {path: path.read_bytes() for path in before} == before


def test_release_of_split_run_changes_only_its_file(tmp_path, monkeypatch):
    repo, aggregate = _project_with_split_runs(tmp_path, monkeypatch)
    aggregate_before = aggregate.read_bytes()
    paths = list(aggregate.parent.joinpath("runs").glob("*.json"))
    before = {path: path.read_bytes() for path in paths}
    target = ledger.run_path("sample", "split-0", repo)

    receipt = {"worktree_released": False}
    recorded, row = _record_release_on_ledger(
        project="sample",
        root=repo,
        run_id="split-0",
        release=receipt,
        checkout=None,
    )

    assert recorded == receipt
    assert row is not None and row["release"] == receipt
    assert json.loads(target.read_text())["release"] == receipt
    assert aggregate.read_bytes() == aggregate_before
    assert {path: path.read_bytes() for path in paths if path != target} == {
        path: contents for path, contents in before.items() if path != target
    }
