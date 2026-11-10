"""An explicit partition submission reaches the scheduler through the shim."""

from pathlib import Path

import pytest

from tests.test_nested_launch import (
    launch_env,
    make_fixture,
    recorded,
    run_shim,
)


@pytest.mark.parametrize(
    "options",
    [
        ["--partition=all_debug"],
        ["-p", "rigel"],
        ["--partition", "all_debug"],
        ["-prigel"],
    ],
)
def test_an_explicit_partition_sbatch_reaches_the_scheduler(tmp_path: Path, options):
    fixture = make_fixture(tmp_path)
    env = launch_env(fixture, inside=True, extra={"SLURM_JOB_ID": "4242"})
    argv = [*options, "--wait", "job.sh"]
    result = run_shim("sbatch", argv, env)
    assert result.returncode == 0, result.stderr
    assert recorded(fixture.record) == [(str(fixture.fake_bin / "sbatch"), tuple(argv))]


@pytest.mark.parametrize(
    "tool,argv",
    [
        ("sbatch", ["job.sh"]),
        ("sbatch", ["--partition=", "job.sh"]),
        ("sbatch", ["--partition"]),
        ("sbatch", ["-p"]),
        ("sbatch", ["-p", "--wait", "job.sh"]),
        ("sbatch", ["--", "--partition=all_debug"]),
        ("srun", ["true"]),
        ("srun", ["--partition=all_debug", "true"]),
        ("salloc", ["--partition=all_debug", "true"]),
    ],
)
def test_an_implicit_nested_launch_is_refused(tmp_path: Path, tool, argv):
    fixture = make_fixture(tmp_path)
    env = launch_env(fixture, inside=True, extra={"SLURM_JOB_ID": "4242"})
    result = run_shim(tool, argv, env)
    assert result.returncode == 97, result.stderr
    assert "sbatch --partition" in result.stderr
    assert recorded(fixture.record) == []
