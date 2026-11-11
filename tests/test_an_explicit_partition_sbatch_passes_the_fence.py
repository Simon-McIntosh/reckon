"""An explicit partition submission reaches the scheduler through the shim."""

from pathlib import Path

import pytest

from reckon.nested_launch import _explicit_partition_submission
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
        ["-p", "all_debug"],
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
        ("sbatch", ["--output", "--partition=all_debug", "--wrap=true"]),
        ("sbatch", ["--job-name", "-prigel", "--wrap=true"]),
        ("sbatch", ["job.sh", "--partition=all_debug"]),
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


# Required-value spellings from Slurm's sbatch option table, independently
# retained here so dropping a role from the parser cannot shrink the cases.
@pytest.mark.parametrize(
    "option",
    [
        "--account",
        "--acctg-freq",
        "--array",
        "--autocomplete",
        "--batch",
        "--bb",
        "--bbf",
        "--begin",
        "--chdir",
        "--cluster",
        "--cluster-constraint",
        "--clusters",
        "--comment",
        "--constraint",
        "--container",
        "--container-id",
        "--context",
        "--core-spec",
        "--cores-per-socket",
        "--cpu-freq",
        "--cpus-per-gpu",
        "--cpus-per-task",
        "--deadline",
        "--delay-boot",
        "--dependency",
        "--distribution",
        "--error",
        "--exclude",
        "--export",
        "--extra",
        "--extra-node-info",
        "--gid",
        "--gpu-bind",
        "--gpu-freq",
        "--gpus",
        "--gpus-per-node",
        "--gpus-per-socket",
        "--gpus-per-task",
        "--gres",
        "--gres-flags",
        "--hint",
        "--input",
        "--job-name",
        "--kill-on-invalid-dep",
        "--licenses",
        "--mail-type",
        "--mail-user",
        "--mcs-label",
        "--mem",
        "--mem-bind",
        "--mem-per-cpu",
        "--mem-per-gpu",
        "--mincpus",
        "--network",
        "--nodefile",
        "--nodelist",
        "--nodes",
        "--ntasks",
        "--ntasks-per-core",
        "--ntasks-per-gpu",
        "--ntasks-per-node",
        "--ntasks-per-socket",
        "--ntasks-per-tres",
        "--open-mode",
        "--output",
        "--power",
        "--prefer",
        "--priority",
        "--profile",
        "--qos",
        "--reservation",
        "--signal",
        "--sockets-per-node",
        "--switches",
        "--tasks-per-node",
        "--thread-spec",
        "--threads-per-core",
        "--time",
        "--time-min",
        "--tmp",
        "--tres-bind",
        "--tres-per-task",
        "--uid",
        "--wait-all-nodes",
        "--wckey",
        "--wrap",
        "-A",
        "-B",
        "-C",
        "-D",
        "-F",
        "-G",
        "-J",
        "-L",
        "-M",
        "-N",
        "-S",
        "-a",
        "-b",
        "-c",
        "-d",
        "-e",
        "-i",
        "-m",
        "-n",
        "-o",
        "-q",
        "-t",
        "-w",
        "-x",
    ],
)
def test_required_option_values_cannot_select_a_partition(option):
    assert not _explicit_partition_submission(
        "sbatch", [option, "--partition=all_debug", "--wrap=true"]
    )
    assert not _explicit_partition_submission(
        "sbatch", [option, "-prigel", "--wrap=true"]
    )
    assert _explicit_partition_submission(
        "sbatch", [option, "value", "--partition", "all_debug", "--wrap=true"]
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--nice", "--partition", "all_debug"],
        ["--exclusive=user", "-pall_debug"],
        ["-k", "-vvpall_debug"],
        ["--output=--partition=rigel", "-p", "all_debug"],
        ["-o-prigel", "--partition=all_debug"],
        ["--error", "log", "-Hvpall_debug"],
    ],
)
def test_attached_values_optional_values_and_flag_clusters_preserve_options(options):
    assert _explicit_partition_submission("sbatch", options)


@pytest.mark.parametrize(
    "options",
    [
        ["--unknown", "--partition=all_debug"],
        ["--partition=all_debug", "--unknown"],
        ["--partition=all_debug", "--partition="],
        ["--wait=--partition=all_debug"],
        ["-kpall_debug"],
        ["-o", "log", "job.sh", "-prigel"],
    ],
)
def test_unknown_roles_and_script_arguments_cannot_grant_an_exemption(options):
    assert not _explicit_partition_submission("sbatch", options)
