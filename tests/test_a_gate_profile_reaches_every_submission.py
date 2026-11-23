"""A project's gate profiles reach every submission its gates make.

A gate that can only be measured on one scheduler — an H200 partition with a
reservation, a gres request and a memory sized to the node's real limit — states
those as a named profile in the project's flight layer. Every submission a gate
makes reads that profile: the arms a landing measure submits, the arms a suite
run submits, and the job a declared gate submits. A name no profile carries is a
bare partition and submits exactly as it always did.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import reckon.crew.runs as runs_module
from reckon import flight
from reckon.crew import landing_measure, standing_suite

PROFILE = {
    "partition": "betelgeuse",
    "reservation": "gpu_0003_grpA",
    "gres": "gpu:1",
    "memory": "96G",
    "cpus": 4,
    "time": "04:00:00",
}

PROFILE_OPTIONS = (
    "--partition=betelgeuse",
    "--reservation=gpu_0003_grpA",
    "--gres=gpu:1",
    "--mem=96G",
    "--cpus-per-task=4",
    "--time=04:00:00",
)

FAILURE = "tests/test_subject.py::test_subject"


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _profile_config():
    return {"landing": {"gate_profiles": {"h200": dict(PROFILE)}}}


def _assert_profile_in(argv):
    for option in PROFILE_OPTIONS:
        assert option in argv, f"{option} missing from {argv!r}"


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
    (repo / "tests/test_subject.py").write_text("import subject\n# head\n")
    _git(repo, "add", "subject.py", "tests/test_subject.py")
    _git(repo, "commit", "-qm", "test: prepare head\n\nRecord delivered input.")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "s"))
    return repo, base, head


def test_a_declared_gate_profile_is_passed_to_its_submission(tmp_path):
    """The job a declared gate submits carries the whole profile."""
    selected, argv, environment = landing_measure.partition_submission(
        script="printf 'EXIT=0\\n'",
        partition="h200",
        long_partition="rigel",
        bound_seconds=600,
        chdir=tmp_path,
        log_path=tmp_path / "job.log",
        config=_profile_config(),
    )
    assert selected == "betelgeuse"
    _assert_profile_in(argv)
    assert "--time=04:00:00" in argv
    step = argv[argv.index("--wrap") + 1]
    _assert_profile_in(shlex.split(step))
    assert environment["TMPDIR"] == landing_measure.NODE_LOCAL_TEMP_ROOT
    assert "uv" not in shlex.split(step)


def test_a_landing_arm_carries_the_profile(project, monkeypatch, tmp_path):
    """Both arms of a landing measure submit with the profile's options."""
    repo, base, head = project
    monkeypatch.setattr(
        flight, "resolve", lambda *a, **k: SimpleNamespace(config=_profile_config())
    )
    calls = []

    def runner(argv, **options):
        calls.append((argv, options))
        if Path(argv[0]).name == "sbatch":
            script = argv[argv.index("--wrap") + 1]
            log = Path(shlex.split(script)[-2])
            is_head = f"archive {base} |" not in script
            output = f"FAILED {FAILURE}\n1 failed\n" if is_head else "1 passed\n"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(output + f"EXIT={int(is_head)}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

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
            declared_partition="h200",
            run=runner,
        )
    assert calls
    assert all(Path(argv[0]).name == "sbatch" for argv, _ in calls)
    for argv, _options in calls:
        _assert_profile_in(argv)
    assert result.as_dict()["execution"] == "slurm"


def test_a_suite_run_carries_the_profile(project, monkeypatch, tmp_path):
    """A suite run submitted on the profile's name carries its options."""
    repo, _base, head = project
    manifest = tmp_path / "manifest.md"
    pointer = {
        "run_id": "fixture-run",
        "project": "fixture",
        "manifest_path": str(manifest),
    }
    monkeypatch.setattr(runs_module, "read_pointer", lambda run_id: dict(pointer))
    monkeypatch.setattr(
        flight, "resolve", lambda *a, **k: SimpleNamespace(config=_profile_config())
    )
    captured = []
    real_partition_runner = landing_measure._partition_runner

    def fake_runner(argv, **options):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, "1 passed\nEXIT=0\n", "")

    def wrapper(runner, **kwargs):
        return real_partition_runner(fake_runner, **kwargs)

    monkeypatch.setattr(landing_measure, "_partition_runner", wrapper)
    observation = standing_suite.run_node_command(
        repository=repo,
        run_id="fixture-run",
        revision=head,
        command="PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_subject.py",
        partition="h200",
        timeout_seconds=600,
        label="scoped-gate",
    )
    assert captured
    _assert_profile_in(captured[0])
    assert observation["partition"] == "betelgeuse"


def test_a_profile_without_memory_is_refused_at_load(tmp_path):
    """A profile whose memory is absent, or zero, is refused naming it."""
    source = tmp_path / "flight.yaml"
    with pytest.raises(flight.FlightConfigError, match="h200"):
        flight.validate_layer(
            {
                "landing": {
                    "gate_profiles": {
                        "h200": {"partition": "betelgeuse", "gres": "gpu:1"}
                    }
                }
            },
            source,
            strict=True,
        )
    with pytest.raises(flight.FlightConfigError, match="h200"):
        flight.validate_layer(
            {
                "landing": {
                    "gate_profiles": {"h200": {"partition": "betelgeuse", "memory": 0}}
                }
            },
            source,
            strict=True,
        )
    assert (
        flight.validate_layer(
            {"landing": {"gate_profiles": {"h200": dict(PROFILE)}}},
            source,
            strict=True,
        )
        == []
    )


def test_a_partition_with_no_profile_submits_exactly_as_today(tmp_path):
    """A bare partition name keeps the shipped defaults and no profile options."""
    for index, config in enumerate(
        ({}, None, {"landing": {"gate_profiles": {"h200": dict(PROFILE)}}})
    ):
        _selected, argv, _environment = landing_measure.partition_submission(
            script="printf 'EXIT=0\\n'",
            partition="all_debug",
            long_partition="rigel",
            bound_seconds=600,
            chdir=tmp_path,
            log_path=tmp_path / f"job-{index}.log",
            config=config,
        )
        assert "--partition=all_debug" in argv
        assert f"--cpus-per-task={landing_measure.POPULATION_WORKERS}" in argv
        assert "--mem=32G" in argv
        assert not any(option.startswith("--reservation=") for option in argv)
        assert not any(option.startswith("--gres=") for option in argv)
        assert "--time=00:10:00" in argv
