"""A promotion cannot create a ledger over an absent or malformed history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "proj"


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    return root


def _record(run_id: str = "r-first") -> dict[str, str]:
    return {"run_id": run_id, "gate": "passed"}


def test_a_promotion_refuses_to_create_an_absent_ledger(repository: Path) -> None:
    """An absent ledger is a recovery condition, not an empty project."""
    path = ledger.ledger_path(PROJECT, repository)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.append_run(PROJECT, _record(), root=repository)

    message = str(excinfo.value)
    assert str(path) in message
    assert "independent authority that holds every promoted run" in message
    assert not path.exists()


def test_a_new_ledger_requires_an_explicit_initialisation(repository: Path) -> None:
    """A caller may initialise a new project only by naming that intent."""
    result = ledger.append_run(PROJECT, _record(), root=repository, allow_create=True)

    stored, version = ledger.load(PROJECT, repository)
    assert result["version"] == version == 1
    assert [row["run_id"] for row in stored["runs"]] == ["r-first"]


@pytest.mark.parametrize("key", ["members", "runs"])
def test_a_malformed_ledger_collection_is_not_an_empty_project(
    repository: Path, key: str
) -> None:
    """A malformed collection cannot collapse into a fresh-start promotion."""
    path = ledger.ledger_path(PROJECT, repository)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": ledger.LEDGER_SLUG,
                "data": {
                    "_version": 4,
                    "members": [] if key != "members" else {"bad": "shape"},
                    "runs": [] if key != "runs" else {"bad": "shape"},
                    "holds": [],
                },
            }
        )
    )

    with pytest.raises(ledger.LedgerError, match=rf"{key} must be list-valued"):
        ledger.append_run(PROJECT, _record(), root=repository)
