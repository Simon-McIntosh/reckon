"""Log collisions preserve their targets and prevent execution."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import landing_measure, promotion_gate, standing_suite
from reckon.flight import review_suite


@pytest.fixture
def runners(tmp_path, monkeypatch):
    monkeypatch.setattr(standing_suite, "_revision", lambda root: "fixture")
    monkeypatch.setattr(promotion_gate, "_commit_canonical_id", lambda *args: "fixture")
    monkeypatch.setattr(promotion_gate, "_replay_landing", lambda root: {})
    marker = tmp_path / "command-ran"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch(); print('command-marker')",
    ]

    def suite(log):
        declaration = review_suite(
            {"review": {"suite": {"command": command, "budget": "10s"}}}
        )
        return standing_suite.run(tmp_path, declaration, log)

    def replay(log):
        return promotion_gate.rerun_gate_at_integrated_revision(
            repository=tmp_path,
            gate_check={"command": shlex.join(command)},
            base_verdict="passed",
            integrated_revision="fixture",
            replay_log_path=log,
        )

    def arm(log):
        return landing_measure._arm(command, tmp_path, log, subprocess.run)

    return {"suite": suite, "replay": replay, "arm": arm}, marker


@pytest.mark.parametrize("runner", ["suite", "replay", "arm"])
@pytest.mark.parametrize("kind", ["regular", "symlink", "dangling"])
def test_existing_log_is_preserved_before_execution(tmp_path, runners, runner, kind):
    calls, marker = runners
    target = tmp_path / "target"
    log = tmp_path / "gate.log"
    if kind != "dangling":
        target.write_bytes(b"sentinel\n")
        before = (target.read_bytes(), target.stat().st_mtime_ns)
    if kind == "regular":
        log = target
    else:
        log.symlink_to(target)
    refused = False
    try:
        result = calls[runner](log)
    except (OSError, standing_suite.StandingSuiteError):
        refused = True
    else:
        refused = (
            not result["ran"] and not result["ok"]
            if runner == "replay"
            else result.exit_status is None and not result.readable
            if runner == "arm"
            else result["exit_status"] != 0
        )
    if kind == "dangling":
        assert not target.exists(), "the dangling log target was created"
    else:
        assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert refused, "the runner accepted an existing log path"
    assert not marker.exists(), "the command ran before the log was refused"
    if kind != "regular":
        assert log.is_symlink()


@pytest.mark.parametrize("runner", ["suite", "replay", "arm"])
def test_a_fresh_log_keeps_the_marker_and_receipt(tmp_path, runners, runner):
    calls, marker = runners
    log = tmp_path / "fresh.log"
    calls[runner](log)
    assert marker.exists()
    assert "command-marker" in log.read_text()
    assert log.read_text().rstrip().endswith("EXIT=0")


@pytest.mark.parametrize("kind", ["regular", "symlink", "dangling"])
def test_partition_submission_refuses_an_existing_log(tmp_path, kind):
    target = tmp_path / "target"
    log = tmp_path / "job.log"
    if kind != "dangling":
        target.write_bytes(b"sentinel\n")
        before = (target.read_bytes(), target.stat().st_mtime_ns)
    if kind == "regular":
        log = target
    else:
        log.symlink_to(target)
    with pytest.raises(FileExistsError):
        landing_measure.partition_submission(
            script="touch command-ran",
            partition="all_debug",
            long_partition="rigel",
            bound_seconds=10,
            chdir=tmp_path,
            log_path=log,
        )
    if kind == "dangling":
        assert not target.exists()
    else:
        assert (target.read_bytes(), target.stat().st_mtime_ns) == before


@pytest.mark.parametrize("kind", ["regular", "symlink", "dangling"])
def test_a_scheduler_output_collision_refuses_submission(tmp_path, kind):
    target = tmp_path / "target"
    log = tmp_path / "job.log"
    scheduler = tmp_path / "job.log.scheduler"
    if kind != "dangling":
        target.write_bytes(b"sentinel\n")
        before = (target.read_bytes(), target.stat().st_mtime_ns)
    if kind == "regular":
        scheduler.write_bytes(b"sentinel\n")
    else:
        scheduler.symlink_to(target)
    with pytest.raises(FileExistsError):
        landing_measure.partition_submission(
            script="touch command-ran",
            partition="all_debug",
            long_partition="rigel",
            bound_seconds=10,
            chdir=tmp_path,
            log_path=log,
        )
    if kind == "dangling":
        assert not target.exists()
    elif kind == "regular":
        assert scheduler.read_bytes() == b"sentinel\n"
    else:
        assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_a_partition_script_appends_to_its_exclusively_created_log(tmp_path):
    log = tmp_path / "job.log"
    shim = tmp_path / "bin"
    shim.mkdir()
    srun = shim / "srun"
    srun.write_text('#!/bin/sh\nwhile [ "$1" != bash ]; do shift; done\nexec "$@"\n')
    srun.chmod(0o700)
    _, argv, environment = landing_measure.partition_submission(
        script="printf 'command-marker\\nEXIT=7\\n'; exit 7",
        partition="all_debug",
        long_partition="rigel",
        bound_seconds=10,
        chdir=tmp_path,
        log_path=log,
    )
    assert log.is_file() and log.read_bytes() == b""
    assert (tmp_path / "job.log.scheduler").is_file()
    environment["PATH"] = str(shim) + ":" + environment["PATH"]
    finished = subprocess.run(
        ["bash", "-c", argv[-1]], env=environment, capture_output=True, check=False
    )
    assert finished.returncode == 7
    assert log.read_text() == "command-marker\nEXIT=7\n"
    assert "--open-mode=append" in argv


@pytest.mark.parametrize("kind", ["regular", "symlink", "dangling"])
def test_partitioned_arm_preserves_the_unresolved_log_path(tmp_path, monkeypatch, kind):
    log = tmp_path / "arm.log"
    target = tmp_path / "target"
    if kind != "dangling":
        target.write_bytes(b"sentinel\n")
        before = (target.read_bytes(), target.stat().st_mtime_ns)
    if kind == "regular":
        log = target
    else:
        log.symlink_to(target)
    monkeypatch.setattr(
        landing_measure,
        "_git",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, str(tmp_path / ".git")
        ),
    )
    calls = []

    def scheduler(*args, **kwargs):
        calls.append(args)
        raise AssertionError("an existing log must be refused before submission")

    runner = landing_measure._partition_runner(
        scheduler,
        repo=tmp_path,
        base_tree=tmp_path / "base",
        base_sha="base",
        head_sha="head",
        partition="all_debug",
        long_partition="rigel",
        test_files=[],
    )
    runner.partitioned = True
    arm = landing_measure._arm(["true"], tmp_path, log, runner)
    assert not arm.readable
    assert not calls
    if kind == "dangling":
        assert not target.exists()
    else:
        assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_repeated_armed_measurements_keep_each_attempt(tmp_path):
    tree = tmp_path / "head"
    base = tmp_path / "base"
    tree.mkdir()
    base.mkdir()

    def runner(argv, **options):
        return subprocess.CompletedProcess(argv, 0, "command-marker\n", "")

    arguments = {
        "command": "true",
        "tree": tree,
        "base_tree": base,
        "base_sha": "base",
        "head_sha": "head",
        "logs_dir": tmp_path / "logs",
        "run": runner,
        "config": {},
    }
    first = landing_measure.armed_observations(**arguments)
    previous = {
        arm["log_path"]: (
            Path(arm["log_path"]).read_bytes(),
            Path(arm["log_path"]).stat().st_mtime_ns,
        )
        for arm in first.values()
    }
    second = landing_measure.armed_observations(**arguments)
    assert not set(previous) & {arm["log_path"] for arm in second.values()}
    for path, snapshot in previous.items():
        assert (Path(path).read_bytes(), Path(path).stat().st_mtime_ns) == snapshot
    assert all(arm["exit_status"] == 0 for arm in second.values())


def test_a_job_retry_keeps_the_prior_receipt(tmp_path, monkeypatch):
    replay = tmp_path / "verify.log"
    stale = tmp_path / "verify.log.job.log"
    stale.write_text("old-marker\nEXIT=0\n")
    snapshot = (stale.read_bytes(), stale.stat().st_mtime_ns)
    monkeypatch.setattr(promotion_gate, "_commit_canonical_id", lambda *args: "fixture")
    monkeypatch.setattr(
        promotion_gate,
        "_replay_landing",
        lambda root: {"measure_partition": "all_debug"},
    )
    calls = []

    def scheduler(argv, **options):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    report = promotion_gate.rerun_gate_at_integrated_revision(
        repository=tmp_path,
        gate_check={"command": "true"},
        integrated_revision="fixture",
        replay_log_path=replay,
        run=scheduler,
    )
    assert len(calls) == 1
    assert report["exit_status"] == 3
    assert not report["ok"]
    assert (stale.read_bytes(), stale.stat().st_mtime_ns) == snapshot
    assert "old-marker" not in replay.read_text()
