"""The closure drain and a promotion's fleet reading count through one composition.

``runs.drain`` derives each live pointer's closure row with ``runs._drain_row``:
the host-gated liveness, the classification over it, and the recorded
disposition's validity. A promotion's fleet reading once recomputed that same
step in its own words, so a change to one would not reach the other. These tests
hold the two readings to the same answer over every pointer shape, and pin both
call sites to the one shared function.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import promotion, recovery, runs
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "one-composition-project"


def _pointer(
    run_id: str,
    repository: Path,
    *,
    backend: str,
    disposition: dict[str, Any] | None = None,
    manifest_path: str = "",
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
        "manifest_path": manifest_path,
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
        [("r-handoff", "beta", {"kind": "handed-off"})],
        [
            ("r-working", "alpha", None),
            ("r-handoff", "beta", {"kind": "handed-off"}),
            ("r-still", "gamma", {"kind": "still-working"}),
            ("r-blocked", "alpha", None),
        ],
    ],
)
def test_the_two_readings_agree_on_each_pointer_shape(
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

    expected = runs.drain(PROJECT)["unreconciled_runs"]
    reading = promotion._fleet_state_reading(PROJECT)

    assert reading["fleet_state"] == "measured"
    assert reading["unreconciled_runs"] == expected
    assert reading["unreconciled_runs"] == promotion._unreconciled_live_runs(
        runs.list_live(project=PROJECT)
    )


def test_a_still_working_disposition_over_a_terminal_manifest_counts_unreconciled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``still-working`` excuses a pointer only while its current classification
    # is still ``running``. Over a terminal manifest the classification has
    # excused nothing, and both readings must count the pointer unreconciled.
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = repository / "manifest.md"
    manifest.write_text("node: node-r-terminal\nstatus: complete\n")

    # The pointer carries a stored ``process_alive: True`` — an answer some
    # other host recorded. The host-gated reading does not stand behind it, so
    # it is not carried into the classification and the terminal manifest's
    # verdict holds. A counter that read the stored answer instead would call
    # the run running, excuse the disposition, and disagree with the drain.
    record = _pointer(
        "r-terminal",
        repository,
        backend="alpha",
        disposition={"kind": "still-working"},
        manifest_path=str(manifest),
    )
    record["process_alive"] = True
    _install(monkeypatch, tmp_path, [record])

    pointers = runs.list_live(project=PROJECT)
    assert len(pointers) == 1
    assert recovery.local_liveness(pointers[0]) == (True, False)

    expected = runs.drain(PROJECT)["unreconciled_runs"]
    reading = promotion._fleet_state_reading(PROJECT)["unreconciled_runs"]

    assert expected == 1
    assert reading == expected == 1


def test_both_call_sites_name_one_shared_composition() -> None:
    # The drain and the promotion counter must reach the same composition, not
    # two copies that merely agree today: the pointer's liveness is host-gated,
    # and a copy that dropped that gate would still match the equivalence cases
    # above while disagreeing on a pointer whose classification turns on it.
    assert promotion._drain_row is runs._drain_row
    assert "_drain_row" in inspect.getsource(runs.drain)
    assert "_drain_row" in inspect.getsource(promotion._unreconciled_live_runs)
