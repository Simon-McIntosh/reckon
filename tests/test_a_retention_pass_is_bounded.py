"""A retention pass finishes inside its slot.

The pass is bounded in the work it does rather than only in what it offers for
removal: ``--limit`` counts every directory it examines, so a head of
permanently withheld directories cannot consume the budget without advancing.
It resumes from a persisted cursor, so that withheld head is walked once per
cycle rather than re-judged on every pass.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import gc as gc_module

PROJECT = "alpha"
DIRECTORY_COUNT = 2000


def runs_root(home: Path) -> Path:
    root = home / "crew" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def checkout(tmp_path: Path) -> Path:
    root = tmp_path / f"checkout-{PROJECT}"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text("{}\n")
    return root


def write_mounts(home: Path, root: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    entries = {PROJECT: str(root / "docs")}
    (home / "mounts.json").write_text(json.dumps({"mounts": entries}))


def plant_many(
    home: Path,
    root: Path,
    count: int,
    *,
    now: datetime,
    figures_from: int = 0,
) -> list[Path]:
    """Plant ``count`` finished runs in one ledger write.

    A run at or above ``figures_from`` carries its stream-derived figures, so it
    is a prune candidate once past its grace; a run below it has no figures
    recorded, so the retention rule withholds it. The split is by sorted index,
    which is where a permanently withheld head sits in a real store.
    """
    paths: list[Path] = []
    runs: list[dict] = []
    stamp = (now - timedelta(days=60)).timestamp()
    for index in range(count):
        run_id = f"run-{index:04d}"
        run_dir = runs_root(home) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stream.jsonl").write_text("{}\n")
        record = ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            project=PROJECT,
            node="node-a",
            manifest_path=str(run_dir / "stream.jsonl"),
        )
        if index >= figures_from:
            record["orientation_input_tokens"] = 128_000.0
            record["tool_steps"] = 7.0
        runs.append(record)
        os.utime(run_dir, (stamp, stamp))
        paths.append(run_dir)
    path = root / "docs" / "state" / PROJECT / "crew.json"
    path.write_text(
        json.dumps(
            {"data": {"members": [], "runs": runs, "holds": [], "_version": count}}
        )
    )
    return paths


def setup_home(tmp_path: Path, monkeypatch) -> datetime:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return datetime(2026, 9, 1, tzinfo=UTC)


def test_a_pass_examines_at_most_its_limit_however_many_are_withheld(
    tmp_path: Path, monkeypatch
) -> None:
    """``--limit`` bounds examined directories, not only offered ones.

    With every directory withheld, a limit that counted only offered rows would
    walk the whole home and offer nothing, which is how one pass outran its
    slot. The spy counts the judgments the pass actually made.
    """
    now = setup_home(tmp_path, monkeypatch)
    root = checkout(tmp_path)
    write_mounts(tmp_path / "config", root)
    plant_many(
        tmp_path / "config",
        root,
        DIRECTORY_COUNT,
        now=now,
        figures_from=DIRECTORY_COUNT,
    )

    judged: list[str] = []
    real = gc_module._run_directory_retention_report

    def spy(*args, **kwargs):
        judged.append(args[0].name)
        return real(*args, **kwargs)

    monkeypatch.setattr(gc_module, "_run_directory_retention_report", spy)

    report = gc_module.retention_sweep(now=now, limit=100)

    assert len(judged) == 100, len(judged)
    assert report["examined"] == 100
    assert report["judged"] == 100
    assert not report["offered"]


def test_consecutive_passes_reach_a_prune_candidate_behind_a_withheld_head(
    tmp_path: Path, monkeypatch
) -> None:
    """A withheld head is passed over once per cycle, not re-judged every pass.

    The first 300 sorted directories are permanently withheld and everything
    after them is prunable. A pass that reset its cursor would spend every
    budget on the head and never reach a candidate; resuming past the cursor
    reaches one on the fourth pass.
    """
    now = setup_home(tmp_path, monkeypatch)
    root = checkout(tmp_path)
    write_mounts(tmp_path / "config", root)
    plant_many(tmp_path / "config", root, DIRECTORY_COUNT, now=now, figures_from=300)

    offered_by_pass: list[int] = []
    for _ in range(4):
        report = gc_module.retention_sweep(now=now, limit=100)
        offered_by_pass.append(len(report["offered"]))

    assert offered_by_pass[:3] == [0, 0, 0], offered_by_pass
    assert offered_by_pass[3] > 0, offered_by_pass


def test_an_interrupted_pass_has_already_printed_its_progress(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A pass killed part-way leaves a progress record on stderr."""
    now = setup_home(tmp_path, monkeypatch)
    root = checkout(tmp_path)
    write_mounts(tmp_path / "config", root)
    plant_many(tmp_path / "config", root, 200, now=now, figures_from=0)

    real = gc_module._run_directory_retention_report
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 12:
            raise RuntimeError("interrupted")
        return real(*args, **kwargs)

    monkeypatch.setattr(gc_module, "_run_directory_retention_report", flaky)

    with pytest.raises(RuntimeError):
        gc_module.retention_sweep(now=now, limit=50)

    err = capsys.readouterr().err
    assert "retention progress" in err, err


def test_every_pass_prints_one_timing_line_per_phase_and_stdout_is_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Per-phase timings go to stderr; stdout stays one parseable report."""
    now = setup_home(tmp_path, monkeypatch)
    root = checkout(tmp_path)
    write_mounts(tmp_path / "config", root)
    plant_many(tmp_path / "config", root, 50, now=now, figures_from=25)

    exit_code = gc_module.main(["--limit", "100"])

    captured = capsys.readouterr()
    assert exit_code == 0
    report = json.loads(captured.out)
    assert report["limit"] == 100
    for phase in gc_module.RETENTION_PHASES:
        assert f"retention phase {phase} " in captured.err, captured.err
        assert report["phase_seconds"][phase] >= 0.0
    # One timing line per phase, no more.
    timing_lines = [
        line
        for line in captured.err.splitlines()
        if line.startswith("retention phase ")
    ]
    assert len(timing_lines) == len(gc_module.RETENTION_PHASES), timing_lines


def test_a_pass_resolves_each_projects_grace_window_once(
    tmp_path: Path, monkeypatch
) -> None:
    """One config read per project, not one per judged directory.

    ``_run_directory_grace_seconds`` read the flight config once for every
    directory, so a pass over one project resolved the same config 2,000 times.
    The count through the patch is the measurement.
    """
    from reckon import flight

    now = setup_home(tmp_path, monkeypatch)
    root = checkout(tmp_path)
    write_mounts(tmp_path / "config", root)
    plant_many(
        tmp_path / "config",
        root,
        DIRECTORY_COUNT,
        now=now,
        figures_from=DIRECTORY_COUNT,
    )

    resolved: list[object] = []
    real_resolve = flight.resolve

    def spy(*args, **kwargs):
        resolved.append(args)
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(flight, "resolve", spy)

    report = gc_module.retention_sweep(now=now, limit=DIRECTORY_COUNT)

    assert report["judged"] == DIRECTORY_COUNT
    assert len(resolved) == 1, len(resolved)
