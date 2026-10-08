"""Claim grace follows launch intervals recorded with individual runs."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims, dispatch_launch
from reckon.crew.node import TaskNode


def _record_intervals(root: Path, seconds: list[float]) -> None:
    origin = datetime(2026, 10, 8, tzinfo=UTC)
    for index, duration in enumerate(seconds):
        directory = root / f"r-20261008T{index:014d}-sample"
        directory.mkdir(parents=True)
        registered = origin + timedelta(seconds=index)
        (directory / "worker.json").write_text(
            json.dumps(
                {
                    "claim_registered_at": registered.isoformat(),
                    "launched_at": (
                        registered + timedelta(seconds=duration)
                    ).isoformat(),
                }
            ),
            encoding="utf-8",
        )


def test_two_recorded_launch_distributions_produce_distinct_graces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quick = tmp_path / "quick"
    slow = tmp_path / "slow"
    quick.mkdir()
    slow.mkdir()
    _record_intervals(quick, [8.0] * 20)
    _record_intervals(slow, [24.0] * 20)

    quick_grace = dispatch_claims.recent_claim_grace_seconds(quick)
    slow_grace = dispatch_claims.recent_claim_grace_seconds(slow)

    assert quick_grace == 12.0
    assert slow_grace == 36.0
    assert slow_grace > quick_grace

    claim = dispatch_claims._RepositoryScopeClaim(
        project="fixture",
        repository=tmp_path,
        run_id="earlier",
        node_id="earlier",
        path="src/example.py",
        absolute_path=tmp_path / "src/example.py",
        declared_path="src/example.py",
        registered_at="2026-10-08T12:00:00+00:00",
    )

    def elapsed_wait(root: Path) -> float:
        monkeypatch.setattr(dispatch_claims, "runs_dir", lambda: root)
        elapsed = 0.0

        def pause(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        assert (
            dispatch_claims._settle_racing_winner(
                claim,
                own_run_id="later",
                own_registered_at="2026-10-08T12:00:10+00:00",
                reread=lambda _run_id: claim,
                clock=lambda: elapsed,
                pause=pause,
            )
            == "expired"
        )
        return elapsed

    assert elapsed_wait(quick) == quick_grace
    assert elapsed_wait(slow) == slow_grace


def test_fewer_than_twenty_recorded_intervals_use_the_floor(tmp_path: Path) -> None:
    _record_intervals(tmp_path, [40.0] * 19)

    assert (
        dispatch_claims.recent_claim_grace_seconds(tmp_path)
        == dispatch_claims.CLAIM_GRACE_FLOOR_SECONDS
    )


def test_claim_registration_reaches_the_worker_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-20261008T120000000000-recorded"
    directory = tmp_path / run_id
    pointer = tmp_path / "live" / f"{run_id}.json"
    monkeypatch.setattr(dispatch_claims, "run_dir", lambda _run_id: directory)
    monkeypatch.setattr(dispatch_claims, "pointer_path", lambda _run_id: pointer)
    node = TaskNode(
        id="record-launch",
        goal="record the launch interval",
        plan="",
        section="s1",
        role="implement",
        spec_level="exact",
        done_when="the worker receipt carries both instants",
        write_paths=["src/example.py"],
        time_budget="20m",
        manifest_path="manifest.md",
    )
    dispatch_claims._publish_launch_claim(
        run_id,
        node=node,
        project="fixture",
        repo=tmp_path,
        session="fixture",
        authority={},
        member="fixture",
        backend="fixture",
        launch="cli",
        agent={},
        session_id=None,
        registered_at="2026-10-08T12:00:00+00:00",
    )

    assert json.loads((directory / "claim.json").read_text())[
        "claim_registered_at"
    ] == ("2026-10-08T12:00:00+00:00")
    assert (
        dispatch_launch._claim_registration_for_worker(directory, run_id)
        == "2026-10-08T12:00:00+00:00"
    )


def test_dispatch_reads_the_bounded_receipt_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch_claims, "crew_home", lambda: tmp_path)
    monkeypatch.setattr(dispatch_claims, "runs_dir", lambda: tmp_path / "runs")
    monkeypatch.setattr(dispatch_launch, "crew_home", lambda: tmp_path)
    monkeypatch.setattr(dispatch_launch, "_pointer_lock", lambda _key: nullcontext())
    registered = datetime(2026, 10, 8, tzinfo=UTC)
    for index in range(20):
        dispatch_launch._remember_claim_launch_interval(
            f"r-20261008T{index:014d}-sample",
            registered.isoformat(),
            (registered + timedelta(seconds=18)).isoformat(),
        )

    assert dispatch_claims.recent_claim_grace_seconds() == 27.0
