"""A queued request admission refused names that refusal in its obligation.

The admission pass re-tests a queued request on every pass, and a request it
refuses on the node contract will never launch however long it waits: the
failure is cured by repairing a field of the request or of the plan section it
was queued from, never by a lane slot. Read from the pointer alone, that
request is indistinguishable from one waiting its turn — ``run-queued``, "wait
for a local lane slot" — so the refusal sits in ``failure`` where no duty
surface reads it, and a coordinator sees a request that is merely waiting until
it goes looking.

These tests drive ``reckon.crew.obligations`` over a synthesised crew home: one
queued pointer carrying the failure the admission pass stamps, and one without
a failure at all, which must keep the waiting reading it has today.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "refused-request-fixture"
SESSION = "coordinator-fixture"
REFUSED_RUN = "r-refused-request"
WAITING_RUN = "r-waiting-request"
OBSERVED_AT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)

#: The failure text the admission pass stamps for a node-contract refusal: the
#: exception class at the head, then every property the node failed.
CONTRACT_REFUSAL = (
    "CrewError: node is not dispatchable — spec-level: no specification level "
    "is declared; pass --spec-level exact, guided or open so the run remains "
    "visible to calibration; negative_control: the node's write paths include a "
    "test file (tests/test_a_refused_request_names_its_refusal.py), so it writes "
    "a check, but its negative_control field declares no mutation that check "
    "must fail against"
)


@pytest.fixture()
def mounted_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and one mounted project, so nothing real is read."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    docs = repo / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed refused-request fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return home


def _write_queued_pointer(run_id: str, *, failure: str = "") -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "queued",
        "process_alive": False,
        "launcher_host": "some-host.invalid",
        "queued_at": "2026-11-07T11:40:00+00:00",
    }
    if failure:
        record["failure"] = failure
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _obligations() -> list[dict[str, object]]:
    return obligations_module.obligations(PROJECT, SESSION)["obligations"]


def test_a_contract_refusal_is_named_in_its_row(mounted_home: Path) -> None:
    """The row carries the refusal's own property names and a repair command."""
    _write_queued_pointer(REFUSED_RUN, failure=CONTRACT_REFUSAL)

    rows = _obligations()

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == "queued-contract-refused"
    reason = str(row["reason"])
    assert "spec-level" in reason
    assert "negative_control" in reason
    # It is not the waiting reading: nothing on the lane clears this refusal.
    assert "wait for a local lane slot" not in str(row["next_command"])
    assert "reckon crew queue drop" in str(row["next_command"])


def test_a_waiting_request_keeps_the_queued_row(mounted_home: Path) -> None:
    """A request with no failure is still waiting its turn, and says so."""
    _write_queued_pointer(WAITING_RUN)

    rows = _obligations()

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["kind"] == "run-queued"
    assert str(row["next_command"]).startswith("waiting for a lane slot")


def test_ignoring_the_failure_leaves_the_plain_waiting_row(
    mounted_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the refusal is what makes the row actionable.

    With the refusal reading switched off the same pointer yields exactly the
    plain waiting row, so the row the test above asserts could not have been
    produced by anything but the failure it carries.
    """
    _write_queued_pointer(REFUSED_RUN, failure=CONTRACT_REFUSAL)
    monkeypatch.setattr(
        obligations_module, "_queued_contract_refusal", lambda pointer, row: None
    )

    rows = _obligations()

    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "run-queued"
    assert str(rows[0]["next_command"]).startswith("waiting for a lane slot")
