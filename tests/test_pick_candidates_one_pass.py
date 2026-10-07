"""A production pick scans the ledger once and reuses its instruction census.

Three properties the picker's latency work rests on. ``snapshot.candidates``
must read the ledger rows once per pick and still produce, for every candidate,
exactly the outcome counts and peak-utilisation figures its per-backend readers
produce. ``routing.context_census`` must reuse the census of a set of files
while their stamps stand, and recount only when a file's stamp moves.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import routing
from reckon.crew.node import TaskNode
from reckon.crew.picker import snapshot
from reckon.crew.picker.snapshot import (
    _peak_input_utilisation,
    recent_outcomes,
)
from reckon.crew.picker.types import PickRequest

BACKENDS = ("claude", "codex", "clive")
CONFIG = {
    "local_backend": "claude",
    "roles": {"implement": {}, "review": {}},
    "backends": {
        "claude": {"model": "m-alpha"},
        "codex": {"model": "m-beta"},
        "clive": {"model": "m-beta"},
    },
}


def _node(**overrides: Any) -> TaskNode:
    fields: dict[str, Any] = {
        "id": "scan-node",
        "goal": "scan the ledger once",
        "plan": "one-typesafe-model-picker",
        "role": "implement",
        "spec_level": "exact",
        "done_when": "the scan is one pass",
        "write_paths": [],
    }
    fields.update(overrides)
    return TaskNode(**fields)


def _row(
    *,
    backend: str,
    model: str,
    gate: str,
    role: str = "implement",
    spec_level: str = "exact",
    days_ago: float = 1.0,
    utilisation: float | None = None,
) -> dict[str, Any]:
    stamp = datetime.now(UTC) - timedelta(days=days_ago)
    row: dict[str, Any] = {
        "backend": backend,
        "agent": {"model": model},
        "gate": gate,
        "role": role,
        "spec_level": spec_level,
        "completed_at": stamp.isoformat(),
    }
    if utilisation is not None:
        row["throughput"] = {"input_utilisation_pct": utilisation}
    return row


def _records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Outcome counts vary per backend/model and per (role, spec_level).
    rows.append(_row(backend="claude", model="m-alpha", gate="passed"))
    rows.append(_row(backend="claude", model="m-alpha", gate="failed"))
    rows.append(_row(backend="claude", model="m-alpha", gate="passed", utilisation=70.0))
    rows.append(_row(backend="claude", model="m-alpha", gate="passed", utilisation=40.0))
    rows.append(
        _row(backend="claude", model="m-alpha", gate="passed", role="review")
    )  # other role: not counted for this request
    rows.append(_row(backend="codex", model="m-beta", gate="not-run"))
    rows.append(_row(backend="codex", model="m-beta", gate="passed", utilisation=90.0))
    rows.append(_row(backend="clive", model="m-beta", gate="passed", utilisation=10.0))
    rows.append(_row(backend="clive", model="m-beta", gate="failed"))
    # A row outside the 14-day window is read by neither reader.
    rows.append(
        _row(backend="clive", model="m-beta", gate="passed", days_ago=20, utilisation=99.0)
    )
    return rows


class _ScanSpy(list):
    """A row list that counts how many times it is iterated."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        super().__init__(items)
        self.iterations = 0

    def __iter__(self):  # type: ignore[override]
        self.iterations += 1
        return super().__iter__()


@pytest.fixture
def picker_env(monkeypatch, tmp_path):
    """Isolate candidates() from the ledger, the repository and the network."""

    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *a, **k: 0)
    monkeypatch.setattr(snapshot, "_fit", lambda request, name, backend, repo, **k: [])
    monkeypatch.setattr(
        snapshot, "_serving_observation", lambda backend: {"status": "served", "detail": ""}
    )
    monkeypatch.setattr(routing, "_estimated_hours", lambda *a, **k: (None, "unavailable"))
    return tmp_path


def _run_candidates(records, picker_env) -> list[Any]:
    request = PickRequest(project="reckon", node=_node(), session="s")
    return snapshot.candidates(
        request,
        CONFIG,
        picker_env,
        records=records,
        budget_snapshot={"backends": [], "groups": []},
        verdict_inputs={},
        cached_only=True,
    )


def test_one_pass_scan_matches_per_backend_reference(picker_env):
    """Every candidate's outcomes and peaks equal the per-backend readers'."""

    records = _records()
    request = PickRequest(project="reckon", node=_node(), session="s")
    now = datetime.now(UTC)

    candidates = _run_candidates(records, picker_env)
    by_name = {candidate.backend: candidate for candidate in candidates}
    assert set(by_name) == set(BACKENDS)

    for name in BACKENDS:
        model = CONFIG["backends"][name]["model"]
        assert by_name[name].outcomes == recent_outcomes(
            records, request, name, model, now=now
        )
        reference_peaks = _peak_input_utilisation(records, name, now=now)
        assert by_name[name].context is not None
        for key, value in reference_peaks.items():
            assert by_name[name].context[key] == value


def test_candidates_iterates_the_records_once(picker_env):
    """The row list is read in a single pass, whatever the backend count."""

    spy = _ScanSpy(_records())
    _run_candidates(spy, picker_env)
    assert spy.iterations == 1


def test_census_reuses_unchanged_files_and_recounts_a_touched_one(
    tmp_path, monkeypatch
):
    """A second census reads no file; a moved stamp forces the recount."""

    repo = tmp_path / "repo"
    repo.mkdir()
    standing_file = repo / "AGENTS.md"
    standing_file.write_text("a" * 40)
    node_file = repo / "mod.py"
    node_file.write_text("b" * 20)

    calls = {"standing": 0, "files": 0}

    def standing(_repo, _backend):
        calls["standing"] += 1
        return 10, {
            "files": [
                {"path": str(standing_file), "bytes": 40, "estimated_tokens": 10}
            ]
        }

    def files(_repo, _node, _authority):
        calls["files"] += 1
        return 5, {
            "write_paths": [
                {"path": str(node_file), "bytes": 20, "estimated_tokens": 5}
            ],
            "named_files": [],
        }

    monkeypatch.setattr(routing, "_standing_context_input", standing)
    monkeypatch.setattr(routing, "_context_file_inputs", files)

    node = _node(write_paths=[str(node_file)])
    root = tmp_path / "cache"

    first = routing.context_census(node, repo, root=root)
    assert first == 15
    assert calls == {"standing": 1, "files": 1}

    second = routing.context_census(node, repo, root=root)
    assert second == first
    assert calls == {"standing": 1, "files": 1}  # no file read again

    stat = node_file.stat()
    os.utime(node_file, (stat.st_atime, stat.st_mtime + 1000))
    third = routing.context_census(node, repo, root=root)
    assert third == first
    assert calls == {"standing": 2, "files": 2}  # touched file recounted


def test_census_equals_an_uncached_census(tmp_path, monkeypatch):
    """The cached figure is the figure the same files produce uncached."""

    repo = tmp_path / "repo"
    repo.mkdir()
    node_file = repo / "mod.py"
    node_file.write_text("c" * 33)

    monkeypatch.setattr(
        routing, "_standing_context_input", lambda _repo, _backend: (0, {"files": []})
    )
    monkeypatch.setattr(
        routing,
        "_context_file_inputs",
        lambda _repo, _node, _authority: (9, {"write_paths": [], "named_files": []}),
    )
    node = _node()
    root = tmp_path / "cache"

    cached = routing.context_census(node, repo, root=root)
    # A second cache root starts from nothing: the uncached build.
    uncached = routing.context_census(node, repo, root=tmp_path / "cache2")
    assert cached == uncached
    assert Path(root).exists()