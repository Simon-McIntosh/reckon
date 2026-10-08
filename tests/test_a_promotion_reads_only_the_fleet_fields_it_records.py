"""A promotion's fleet reading computes only the fields the ledger row records.

The reading stamps live_runs, unreconciled_runs, actionable runs and occupied
lanes onto the promotion result. Every one of those is derived from the live
pointers alone; the reading once ran the full closure drain, whose plan
inventory it never consumed. These tests hold the bounded reading to the answer
the drain would have written, and show the drain is not reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon.crew import promotion, promotion_release, recovery, runs
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "fleet-read-project"


def _pointer(
    run_id: str,
    repository: Path,
    *,
    backend: str,
    disposition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": "0" * 40,
        "launch": "in-harness",
        "role": "implement",
        "backend": backend,
        "created_at": "2030-01-02T03:00:00Z",
        "manifest_path": "",
        "closure_disposition": disposition,
        "node": {
            "id": f"node-{run_id}",
            "plan": "p",
            "section": "s",
            "time_budget": "20m",
            "write_paths": [],
        },
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pointers: list[dict[str, Any]],
) -> None:
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    for record in pointers:
        _write_json(pointer_path(record["run_id"]), record)


@pytest.mark.parametrize(
    "fleet",
    [
        [],
        [("r-one", "alpha", None)],
        [
            ("r-working", "alpha", None),
            ("r-handoff", "beta", {"kind": "handed-off"}),
            ("r-still", "gamma", {"kind": "still-working"}),
            ("r-blocked", "alpha", None),
        ],
    ],
)
def test_the_bounded_reading_agrees_with_the_full_drain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fleet: list[tuple[str, str, dict[str, Any] | None]],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _install(
        monkeypatch,
        tmp_path,
        [
            _pointer(run_id, repository, backend=backend, disposition=disposition)
            for run_id, backend, disposition in fleet
        ],
    )

    expected = runs.drain(PROJECT)
    reading = promotion._fleet_state_reading(PROJECT)

    assert reading["fleet_state"] == "measured"
    assert reading["unreconciled_runs"] == expected["unreconciled_runs"]
    assert reading["live_runs"] == expected["live_pointers"]
    assert reading["live_runs"] == len(runs.list_live(project=PROJECT))


def test_the_bounded_reading_does_not_run_the_closure_drain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    fleet = [
        _pointer("r-working", repository, backend="alpha"),
        _pointer(
            "r-handoff",
            repository,
            backend="beta",
            disposition={"kind": "handed-off"},
        ),
    ]
    _install(monkeypatch, tmp_path, fleet)

    invoked: list[str] = []

    def spy(project: str) -> dict[str, Any]:
        invoked.append(project)
        return runs.drain(project)

    monkeypatch.setattr(promotion_release, "drain", spy)

    reading = promotion._fleet_state_reading(PROJECT)

    assert reading["fleet_state"] == "measured"
    assert reading["unreconciled_runs"] == 1
    assert reading["live_runs"] == len(fleet)
    assert invoked == []


def test_a_still_working_disposition_reads_against_its_current_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``closure_disposition_valid`` returns True for ``still-working`` only when
    # the pointer's current classification is still ``running`` — the one branch
    # where the excusing verdict turns on a re-derived classification. An
    # in-harness pointer with no manifest yet classifies as running, so this
    # fleet exercises its True side; a bounded reading that dropped the
    # classification would leave the bare equivalence test green, so the case is
    # asserted directly here.
    repository = tmp_path / "repository"
    repository.mkdir()
    _install(
        monkeypatch,
        tmp_path,
        [
            _pointer(
                "r-still",
                repository,
                backend="alpha",
                disposition={"kind": "still-working"},
            )
        ],
    )

    pointers = runs.list_live(project=PROJECT)
    assert len(pointers) == 1
    assert recovery.classify_pointer(pointers[0])["classification"] == "running"

    expected = runs.drain(PROJECT)["unreconciled_runs"]
    reading = promotion._fleet_state_reading(PROJECT)

    assert expected == 0
    assert reading["unreconciled_runs"] == expected == 0
    assert reading["live_runs"] == 1
