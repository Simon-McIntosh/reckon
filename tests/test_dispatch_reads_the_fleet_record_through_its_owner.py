"""The fleet record is read at the path its owner resolves.

The fleet's supervisor owns where the record lives, choosing the directory
from ``FLEET_STATE_DIR`` or the fleet's own name. A dispatcher that computed
that path itself would disagree with the owner for a named fleet and read a
path holding no record — deciding a spawn on an absence it had created.
"""

import json
from pathlib import (
    Path,
)

import pytest

from reckon.crew import (
    fleet_supervisor,
)
from reckon.crew.dispatch_launch import (
    _read_fleet_record,
)

RECORD_PAYLOAD = {"job_id": "4242", "node": "alpha-node", "runtime_dir": "/run/x"}


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home the owner resolves into the test tree, with no fleet named yet."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv(fleet_supervisor.FLEET_NAME_ENV, raising=False)
    monkeypatch.delenv(fleet_supervisor.STATE_DIR_ENV, raising=False)
    monkeypatch.delenv("RECKON_FLEET_RECORD", raising=False)
    return home


def _write_record(directory: Path, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / "record.json"
    record.write_text(json.dumps(payload), encoding="utf-8")
    return record


def test_a_named_fleet_record_is_read_where_the_owner_resolves_it(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(fleet_supervisor.FLEET_NAME_ENV, "alpha")

    directory = fleet_supervisor.state_directory()
    assert directory == (
        isolated_home / ".local" / "state" / "fleet" / "fleets" / "alpha"
    ), "the owner did not resolve the named fleet's own state directory"
    written = _write_record(directory, RECORD_PAYLOAD)

    assert written == directory / "record.json"
    assert _read_fleet_record() == RECORD_PAYLOAD


def test_the_override_still_takes_precedence_over_the_owner(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(fleet_supervisor.FLEET_NAME_ENV, "alpha")
    _write_record(fleet_supervisor.state_directory(), {"source": "owner"})

    elsewhere = tmp_path / "elsewhere" / "record.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps({"source": "override"}), encoding="utf-8")
    monkeypatch.setenv("RECKON_FLEET_RECORD", str(elsewhere))

    assert _read_fleet_record() == {"source": "override"}


def test_a_named_fleet_with_no_record_reads_none(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(fleet_supervisor.FLEET_NAME_ENV, "alpha")

    assert not fleet_supervisor.state_directory().exists()
    assert _read_fleet_record() is None
