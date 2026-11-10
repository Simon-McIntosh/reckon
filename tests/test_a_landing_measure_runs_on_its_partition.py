"""Measurement arms use independent scheduler jobs and node-local revision trees."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import landing_measure

FAILURE = "tests/test_subject.py::test_subject"


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "subject.py").write_text("VALUE = 'base'\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_subject.py").write_text("import subject\n")
    _git(repo, "add", "subject.py", "tests/test_subject.py")
    _git(repo, "commit", "-qm", "test: prepare base\n\nRecord fixture inputs.")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "subject.py").write_text("VALUE = 'head'\n")
    _git(repo, "add", "subject.py")
    _git(repo, "commit", "-qm", "test: prepare head\n\nRecord delivered input.")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    return repo, base, head


def _measure(
    project, monkeypatch, tmp_path, *, partition="all_debug", timeout=600, receipt=True
):
    repo, base, head = project
    config = {"landing": {"measure_partition": partition}} if partition else {}
    monkeypatch.setattr(
        flight, "resolve", lambda *a, **k: SimpleNamespace(config=config)
    )
    calls = []

    def runner(argv, **options):
        calls.append((argv, options))
        if Path(argv[0]).name == "sbatch":
            script = argv[argv.index("--wrap") + 1]
            # The script names a durable log independently of scheduler stdout.
            tokens = shlex.split(script)
            log = Path(tokens[-2])
            is_head = f"archive {base} |" not in script
            output = f"FAILED {FAILURE}\n1 failed\n" if is_head else "1 passed\n"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(output + (f"EXIT={int(is_head)}\n" if receipt else ""))
        else:
            is_head = Path(options["cwd"]) == repo
            output = f"FAILED {FAILURE}\n1 failed\n" if is_head else "1 passed\n"
        return subprocess.CompletedProcess(argv, int(is_head), output, "")

    with landing_measure.base_checkout(repo, base) as base_tree:
        result = landing_measure.measure_run(
            command="PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_subject.py",
            tree=repo,
            base_sha=base,
            head_sha=head,
            base_tree=base_tree,
            logs_dir=tmp_path / "run/landing-measure",
            test_files=["tests/test_subject.py"],
            project="fixture",
            changed=["subject.py"],
            run=runner,
            timeout=timeout,
        )
    return result, calls


def test_every_arm_uses_a_separate_bounded_partition_job(
    project, monkeypatch, tmp_path
):
    result, calls = _measure(project, monkeypatch, tmp_path)
    assert len(calls) == 10  # suite pair, population pair, three isolated pairs
    assert all(Path(argv[0]).name == "sbatch" for argv, _ in calls)
    for argv, options in calls:
        assert "--wait" in argv
        assert "--partition=all_debug" in argv
        assert "--time=00:10:00" in argv
        script = argv[argv.index("--wrap") + 1]
        assert "srun" in script
        assert "--partition=all_debug" in script
        assert "--overlap" not in script
        assert str(tmp_path / "run/landing-measure") in script
        assert options["env"]["RECKON_ALLOW_NESTED_LAUNCH"] == "1"
    assert result.real == (FAILURE,)
    assert result.as_dict()["execution"] == "slurm"
    assert (
        landing_measure.Measurement.from_dict(result.as_dict()).as_dict()
        == result.as_dict()
    )


def test_a_long_arm_uses_the_long_partition(project, monkeypatch, tmp_path):
    _, calls = _measure(project, monkeypatch, tmp_path, timeout=3601)
    assert calls
    assert all("--partition=rigel" in argv for argv, _ in calls)
    assert all("--time=01:00:01" in argv for argv, _ in calls)


def test_an_undeclared_project_records_in_place_execution(
    project, monkeypatch, tmp_path
):
    result, calls = _measure(project, monkeypatch, tmp_path, partition="")
    assert calls
    assert all(Path(argv[0]).name != "sbatch" for argv, _ in calls)
    assert result.as_dict()["execution"] == "in-place"


def test_declared_flight_settings_are_validated(tmp_path):
    source = tmp_path / "flight.yaml"
    assert (
        flight.validate_layer(
            {
                "landing": {
                    "measure_partition": "all_debug",
                    "measure_long_partition": "rigel",
                    "measure_timeout_seconds": 600,
                }
            },
            source,
            strict=True,
        )
        == []
    )
    with pytest.raises(flight.FlightConfigError, match="measure_partition"):
        flight.validate_layer(
            {"landing": {"measure_partition": 42}}, source, strict=True
        )


def test_job_builds_the_named_revision_and_cleans_its_tree(
    project, monkeypatch, tmp_path
):
    repo, base, head = project
    (repo / ".venv").symlink_to(Path(sys.executable).parent.parent)
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *a, **k: SimpleNamespace(
            config={"landing": {"measure_partition": "all_debug"}}
        ),
    )
    scripts = []
    node_tmp = tmp_path / "job-tmp"
    node_tmp.mkdir()
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "srun").write_text(
        '#!/bin/sh\nwhile [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n'
    )
    (shim / "srun").chmod(0o755)

    def run(argv, **options):
        assert Path(argv[0]).name == "sbatch"
        script = argv[argv.index("--wrap") + 1]
        scripts.append(script)
        env = {
            **options["env"],
            "PATH": str(shim) + ":" + options["env"]["PATH"],
            "TMPDIR": str(node_tmp),
        }
        return subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
        )

    log_dir = tmp_path / "run/logs"
    command = "PYTHONPATH=. .venv/bin/python -c " + shlex.quote(
        "import pathlib,socket; print(pathlib.Path('subject.py').read_text().strip()); "
        "print('host=' + socket.gethostname()); print('tree=' + str(pathlib.Path.cwd()))"
    )
    with landing_measure.base_checkout(repo, base) as tree:
        measured = landing_measure.measure_run(
            command=command,
            tree=repo,
            base_sha=base,
            head_sha=head,
            base_tree=tree,
            logs_dir=log_dir,
            project="fixture",
            changed=[],
            run=run,
        )
    assert not measured.unreadable
    assert "VALUE = 'base'" in (log_dir / "suite-base.log").read_text()
    assert "VALUE = 'head'" in (log_dir / "suite-head.log").read_text()
    assert not list(node_tmp.iterdir())
    assert all(base in script or head in script for script in scripts)
    assert json.loads(json.dumps(measured.as_dict()))["execution"] == "slurm"


def test_submission_success_without_an_arm_receipt_refuses_the_measurement(
    project, monkeypatch, tmp_path
):
    result, calls = _measure(project, monkeypatch, tmp_path, receipt=False)
    assert calls
    assert result.unreadable == "the base arm did not run"
    assert result.suite["base"]["exit_status"] == 3


def test_a_timed_out_batch_job_is_cancelled_by_its_reported_id(
    project, monkeypatch, tmp_path
):
    repo, base, head = project
    config = {"landing": {"measure_partition": "all_debug"}}
    monkeypatch.setattr(
        flight, "resolve", lambda *a, **k: SimpleNamespace(config=config)
    )
    calls = []

    def runner(argv, **options):
        calls.append(argv)
        if argv[0] == "scancel":
            return subprocess.CompletedProcess(argv, 0, "cancelled fixture job\n", "")
        raise subprocess.TimeoutExpired(argv, options["timeout"], output="12345\n")

    with landing_measure.base_checkout(repo, base) as tree:
        measured = landing_measure.measure_run(
            command="python -m pytest tests/test_subject.py",
            tree=repo,
            base_sha=base,
            head_sha=head,
            base_tree=tree,
            logs_dir=tmp_path / "run/logs",
            changed=[],
            run=runner,
            project="fixture",
        )
    assert calls.count(["scancel", "12345"]) == 2
    assert measured.suite["base"]["timed_out"]
    assert "exceeded" in measured.unreadable
