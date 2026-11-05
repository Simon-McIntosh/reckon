"""A reader builds no index for a ledger that holds nothing.

An index is a cache of a ledger, published as ``ledger-<sha256>-v<schema>.sqlite``
under ``_store.cache_root("pick-input")`` and keyed to the ledger path it
indexes. When the ledger holds nothing there is no cache to keep: the read is
the empty roster ``_load_aggregate`` answers at version 0, and the file a
reader would publish names a checkout that may never exist, so a sweep
classifies it ``recorded-checkout-gone`` and nothing removes it until someone
runs a sweeping ``gc --apply``.

The four cases pin both halves of the rule. A ledger with neither an aggregate
file nor a run record is not indexed, and neither is one whose ``runs``
directory exists but holds nothing: a directory is a shape, not content. A
ledger that holds run records without an aggregate is indexed — that is a
project whose runs are split into their own documents — and so is one that
carries an aggregate.

Every case reads through its own temporary cache root, because a test that
resolved the operator's real cache would be reporting on the machine rather
than on the code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "reader-index-fixture"


def _checkout(tmp_path: Path) -> tuple[str, Path]:
    root = tmp_path / "checkout"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return PROJECT, root


def _state_dir(root: Path) -> Path:
    return root / "docs" / "state" / PROJECT


def _write_aggregate(root: Path) -> None:
    (_state_dir(root) / "crew.json").write_text(
        json.dumps({"data": {"_version": 3, "members": [], "runs": [], "holds": []}}),
        encoding="utf-8",
    )


def _write_run(root: Path, run_id: str = "run-00") -> None:
    runs = _state_dir(root) / "runs"
    runs.mkdir(exist_ok=True)
    (runs / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": "fixture-session",
                "node": {"id": run_id, "plan": "fixture-plan"},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("RECKON_PICK_CACHE", str(root))
    return root


def _indexes(cache_root: Path) -> list[Path]:
    if not cache_root.exists():
        return []
    return sorted(cache_root.glob("ledger-*.sqlite"))


def _assert_no_index(cache_root: Path) -> None:
    built = _indexes(cache_root)
    assert built == [], f"a read of a ledger holding nothing wrote an index: {built}"


def test_a_project_with_no_ledger_and_no_runs_directory_builds_no_index(
    tmp_path: Path, cache_root: Path
) -> None:
    project, root = _checkout(tmp_path)
    data, version = ledger.load(project, root)
    assert data == {"members": [], "runs": [], "holds": []}
    assert version == 0
    _assert_no_index(cache_root)


def test_a_project_with_an_empty_runs_directory_builds_no_index(
    tmp_path: Path, cache_root: Path
) -> None:
    project, root = _checkout(tmp_path)
    (_state_dir(root) / "runs").mkdir()
    data, version = ledger.load(project, root)
    assert data == {"members": [], "runs": [], "holds": []}
    assert version == 0
    _assert_no_index(cache_root)


def test_a_project_holding_run_records_and_no_aggregate_builds_its_index(
    tmp_path: Path, cache_root: Path
) -> None:
    project, root = _checkout(tmp_path)
    _write_run(root)
    data, _version = ledger.load(project, root)
    assert [row["run_id"] for row in data["runs"]] == ["run-00"]
    built = _indexes(cache_root)
    assert len(built) == 1, f"expected one index, found {built}"
    assert built[0].stat().st_size > 0


def test_a_project_with_an_aggregate_builds_its_index(
    tmp_path: Path, cache_root: Path
) -> None:
    project, root = _checkout(tmp_path)
    _write_aggregate(root)
    ledger.load(project, root)
    built = _indexes(cache_root)
    assert len(built) == 1, f"expected one index, found {built}"
    assert built[0].stat().st_size > 0
