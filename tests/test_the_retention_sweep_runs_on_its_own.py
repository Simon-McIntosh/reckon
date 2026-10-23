"""The retention pass runs on its own, hourly, across every mounted project.

The pass is the run-directory retention alone: it judges each run directory
against the ledger of the project that dispatched it, so a run another project
dispatched is judged rather than skipped, and it starts no git process, because
gc's worktree checks are where an unscheduled pass spent its time. Each pass
removes at most one pass's worth of directories, writes its removals to one log
under the crew home, and writes nothing when it removed nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import ledger
from reckon.crew import gc as gc_module

PROJECT_A = "alpha"
PROJECT_B = "beta"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def runs_root(home: Path) -> Path:
    root = home / "crew" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def checkout(tmp_path: Path, name: str) -> Path:
    """One project's checkout, carrying its own state directory."""
    root = tmp_path / f"checkout-{name}"
    (root / "docs" / "state" / name).mkdir(parents=True, exist_ok=True)
    (root / "docs" / "state" / name / "index.json").write_text("{}\n")
    return root


def write_mounts(home: Path, projects: dict[str, Path]) -> None:
    """Register every project's docs directory with the mount registry."""
    home.mkdir(parents=True, exist_ok=True)
    entries = {name: str(root / "docs") for name, root in projects.items()}
    (home / "mounts.json").write_text(json.dumps({"mounts": entries}))


def plant_run(
    home: Path,
    root: Path,
    project: str,
    run_id: str,
    *,
    now: datetime,
    age: timedelta = timedelta(days=60),
    figures: bool = True,
) -> Path:
    """Plant one finished run: its directory, its mtime, and its ledger row."""
    run_dir = runs_root(home) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stream.jsonl").write_text("{}\n")
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="passed",
        project=project,
        node="node-a",
        manifest_path=str(run_dir / "stream.jsonl"),
    )
    if figures:
        record["orientation_input_tokens"] = 128_000.0
        record["tool_steps"] = 7.0
    ledger.append_run(project, record, root=root, allow_create=True)
    stamp = (now - age).timestamp()
    os.utime(run_dir, (stamp, stamp))
    return run_dir


def plant_many(
    home: Path, root: Path, project: str, count: int, *, now: datetime
) -> list[Path]:
    """Plant many finished runs in one ledger write, for the bulk cases.

    The per-run promotion path commits once per run, which is the right cost for
    a handful of fixtures and far too slow for a thousand. The aggregate the
    indexed reader folds the rows with is written once here instead, so the case
    measures the pass rather than the fixture.
    """
    run_dirs: list[Path] = []
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
            project=project,
            node="node-a",
            manifest_path=str(run_dir / "stream.jsonl"),
        )
        record["orientation_input_tokens"] = 128_000.0
        record["tool_steps"] = 7.0
        runs.append(record)
        os.utime(run_dir, (stamp, stamp))
        run_dirs.append(run_dir)
    path = root / "docs" / "state" / project / "crew.json"
    path.write_text(
        json.dumps(
            {"data": {"members": [], "runs": runs, "holds": [], "_version": count}}
        )
    )
    return run_dirs


def test_a_pass_retires_the_finished_run_of_every_mounted_project(
    tmp_path: Path, monkeypatch
) -> None:
    """A run another project dispatched is judged, not skipped.

    Reading one project's ledger leaves the other project's finished run in
    place, which is how directories past their window accumulated unretired.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    now = datetime(2026, 9, 1, tzinfo=UTC)
    roots = {
        PROJECT_A: checkout(tmp_path, PROJECT_A),
        PROJECT_B: checkout(tmp_path, PROJECT_B),
    }
    write_mounts(home, roots)
    dir_a = plant_run(home, roots[PROJECT_A], PROJECT_A, "run-a", now=now)
    dir_b = plant_run(home, roots[PROJECT_B], PROJECT_B, "run-b", now=now)

    report = gc_module.retention_sweep(apply=True, now=now)

    assert sorted(report["removed"]) == ["run-a", "run-b"], report
    assert report["removed_count"] == 2
    assert not dir_a.exists()
    assert not dir_b.exists()
    # The removals are written to one log under the crew home.
    log = gc_module.retention_log_path()
    assert log.is_file()
    body = log.read_text()
    assert str(dir_a) in body and str(dir_b) in body


def test_a_second_pass_with_nothing_left_writes_no_log_line(
    tmp_path: Path, monkeypatch
) -> None:
    """A pass that removed nothing writes nothing.

    The log is a record of removals rather than a heartbeat, so a sweep that has
    nothing to take says nothing at all.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    now = datetime(2026, 9, 1, tzinfo=UTC)
    root = checkout(tmp_path, PROJECT_A)
    write_mounts(home, {PROJECT_A: root})
    plant_run(home, root, PROJECT_A, "run-a", now=now)

    first = gc_module.retention_sweep(apply=True, now=now)
    assert first["removed_count"] == 1
    log = gc_module.retention_log_path()
    before = log.read_bytes()

    second = gc_module.retention_sweep(apply=True, now=now)

    assert second["removed_count"] == 0
    assert log.read_bytes() == before


def test_a_pass_removes_one_limit_and_leaves_the_rest(
    tmp_path: Path, monkeypatch
) -> None:
    """One pass stays short on shared storage; the next hour takes the rest."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    now = datetime(2026, 9, 1, tzinfo=UTC)
    root = checkout(tmp_path, PROJECT_A)
    write_mounts(home, {PROJECT_A: root})
    dirs = plant_many(home, root, PROJECT_A, 1200, now=now)

    report = gc_module.retention_sweep(apply=True, now=now)

    assert report["removed_count"] == gc_module.RETENTION_SWEEP_LIMIT
    assert len([path for path in dirs if path.exists()]) == 700


def test_a_pass_starts_no_git_process_even_where_the_checkout_carries_trees(
    tmp_path: Path, monkeypatch
) -> None:
    """The pass runs no git command, so its cost does not depend on the tree.

    The shim records every git invocation made while the pass runs, and the same
    pass removes both directories, so a quiet shim is a measured absence rather
    than a pass that never got far enough to call git.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    now = datetime(2026, 9, 1, tzinfo=UTC)
    root_a = checkout(tmp_path, PROJECT_A)
    root_b = checkout(tmp_path, PROJECT_B)
    # A checkout that carries a registered worktree, which is what an
    # age-and-worktree sweep would have had to inspect.
    git(root_a, "init", "-q", "-b", "main")
    git(root_a, "config", "user.email", "test@example.invalid")
    git(root_a, "config", "user.name", "Test User")
    (root_a / "seed.txt").write_text("seed\n")
    git(root_a, "add", "seed.txt")
    git(root_a, "commit", "-q", "-m", "test: seed")
    git(root_a, "worktree", "add", str(tmp_path / "wt-a"), "-b", "wtbranch")
    assert (tmp_path / "wt-a" / "seed.txt").is_file()
    write_mounts(home, {PROJECT_A: root_a, PROJECT_B: root_b})
    dir_a = plant_run(home, root_a, PROJECT_A, "run-a", now=now)
    dir_b = plant_run(home, root_b, PROJECT_B, "run-b", now=now)

    real_git = shutil.which("git")
    assert real_git
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    calls = tmp_path / "git-calls.log"
    shim = shim_dir / "git"
    shim.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\nexec {real_git} "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    report = gc_module.retention_sweep(apply=True, now=now)

    assert sorted(report["removed"]) == ["run-a", "run-b"]
    assert not dir_a.exists()
    assert not dir_b.exists()
    assert not calls.exists(), calls.read_text()


def test_the_pass_reads_each_ledger_through_its_index(
    tmp_path: Path, monkeypatch
) -> None:
    """Both ledgers are read through the shared index-backed reader.

    The spy sits on the index-backed reader itself rather than on the wrapper
    that carries the flag, because the pass never passes the flag: a spy on the
    wrapper records whatever a caller's default happens to be and passes
    whether or not the index is read at all. A pass that parsed an aggregate
    would not reach this reader for either project, so an empty or partial
    record here is the failure this case exists to raise.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    now = datetime(2026, 9, 1, tzinfo=UTC)
    roots = {
        PROJECT_A: checkout(tmp_path, PROJECT_A),
        PROJECT_B: checkout(tmp_path, PROJECT_B),
    }
    write_mounts(home, roots)
    plant_run(home, roots[PROJECT_A], PROJECT_A, "run-a", now=now)
    plant_run(home, roots[PROJECT_B], PROJECT_B, "run-b", now=now)

    indexed: list[str] = []
    real_indexed_data = ledger._indexed_data

    def spy(project, root=None, **kwargs):
        indexed.append(project)
        return real_indexed_data(project, root, **kwargs)

    monkeypatch.setattr(ledger, "_indexed_data", spy)

    report = gc_module.retention_sweep(apply=True, now=now)

    assert sorted(report["removed"]) == ["run-a", "run-b"]
    assert sorted(set(indexed)) == [PROJECT_A, PROJECT_B], indexed


def test_installing_writes_a_service_and_an_hourly_timer_and_reinstalling_writes_nothing(
    tmp_path: Path,
) -> None:
    """The timer is installed once; an unchanged re-run leaves it untouched."""
    unit_dir = tmp_path / "systemd-user"
    unit_dir.mkdir()
    commands: list[list[str]] = []

    first = gc_module.install_retention_timer(directory=unit_dir, run=commands.append)

    service = unit_dir / gc_module.RETENTION_SERVICE_NAME
    timer = unit_dir / gc_module.RETENTION_TIMER_NAME
    assert service.is_file() and timer.is_file()
    timer_body = timer.read_text()
    assert "OnUnitActiveSec=1h" in timer_body
    assert f"Unit={gc_module.RETENTION_SERVICE_NAME}" in timer_body
    assert "ExecStart=" in service.read_text()
    assert commands == [
        ["daemon-reload"],
        ["enable", "--now", gc_module.RETENTION_TIMER_NAME],
    ]
    assert first["changed"] is True

    stamps = {path: path.stat().st_mtime_ns for path in (service, timer)}
    commands.clear()
    second = gc_module.install_retention_timer(directory=unit_dir, run=commands.append)

    assert second["changed"] is False
    assert commands == []
    assert {path: path.stat().st_mtime_ns for path in (service, timer)} == stamps
