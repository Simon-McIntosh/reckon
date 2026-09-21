"""A promotion creates a ledger only where git says none ever existed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "proj"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _history(root: Path, path: Path) -> str:
    """Return the commits git records for the ledger path, empty when none."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--all",
            "--format=%H",
            "--",
            path.relative_to(root).as_posix(),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A real checkout whose ledger path git has never recorded."""
    root = tmp_path / "repository"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "crew@example.invalid")
    _git(root, "config", "user.name", "crew")
    _git(root, "add", "docs/state/proj/index.json")
    _git(root, "commit", "-qm", "record project index")
    return root


def _record(run_id: str = "r-first") -> dict[str, str]:
    return {"run_id": run_id, "gate": "passed"}


def _commit_then_remove_ledger(root: Path) -> Path:
    """Reproduce the incident shape: the ledger is committed, then unlinked."""
    path = ledger.ledger_path(PROJECT, root)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": ledger.LEDGER_SLUG,
                "data": {
                    "_version": 1,
                    "members": [],
                    "runs": [{"run_id": "r-prior"}],
                    "holds": [],
                },
            }
        )
    )
    _git(root, "add", path.relative_to(root).as_posix())
    _git(root, "commit", "-qm", "record promotion")
    path.unlink()
    return path


def test_a_promotion_refuses_a_ledger_deleted_from_the_tree(repository: Path) -> None:
    """A ledger git once tracked is a recovery condition, not an empty project."""
    path = _commit_then_remove_ledger(repository)
    assert _history(repository, path), "fixture must carry history for the path"

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.append_run(PROJECT, _record(), root=repository)

    message = str(excinfo.value)
    assert str(path) in message
    assert "independent authority that holds every promoted run" in message
    assert not path.exists()


def test_a_promotion_initialises_a_ledger_git_never_tracked(repository: Path) -> None:
    """A path git has never recorded is a new project, not a deleted ledger."""
    path = ledger.ledger_path(PROJECT, repository)
    assert _history(repository, path) == "", "fixture must have no history"

    result = ledger.append_run(PROJECT, _record(), root=repository)

    stored, version = ledger.load(PROJECT, repository)
    assert result["version"] == version == 1
    assert [row["run_id"] for row in stored["runs"]] == ["r-first"]
    assert path.exists()


def test_a_ledger_outside_a_checkout_still_requires_explicit_initialisation(
    tmp_path: Path,
) -> None:
    """Where git cannot answer, the refusal stands and ``allow_create`` is the way."""
    root = tmp_path / "nowhere"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )

    with pytest.raises(ledger.LedgerError):
        ledger.append_run(PROJECT, _record(), root=root)

    result = ledger.append_run(PROJECT, _record(), root=root, allow_create=True)
    stored, version = ledger.load(PROJECT, root)
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
