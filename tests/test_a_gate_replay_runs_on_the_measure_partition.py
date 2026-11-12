"""A promoted gate replay runs on the project's measure partition.

The replay re-runs a landed node's gate at the integrated revision. When the
flight resolved for the replayed repository declares a measure partition, the
replay is submitted as its own batch job instead: an ``srun`` step inside an
``sbatch --wait`` submission whose bound the scheduler enforces. A bare
``srun`` is deliberately not used, because it stays inside whatever allocation
the caller already holds.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import promotion_gate

COMMAND = "python -m pytest -q tests/test_subject.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def project(tmp_path):
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
    return repo


def _landing(partition="all_debug", long_partition="rigel") -> dict:
    landing = {}
    if partition:
        landing["measure_partition"] = partition
    if long_partition:
        landing["measure_long_partition"] = long_partition
    return {"landing": landing}


def _record(
    monkeypatch,
    config,
    *,
    status=0,
    output="1 passed\n",
    job_status=None,
    job_output=None,
    job_writes=None,
):
    """Record every command the replay runs, and write the job's own log.

    The client-side result and the job's own log are separate inputs here, so a
    case can make them disagree. ``job_writes`` is the text the job left in its
    log; by default it is the job output plus an ``EXIT=`` record, while the
    client's own return code and stdout are set independently by ``status``
    and ``output``. A replay that reads the wrong one is therefore visible.
    """
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *args, **options: SimpleNamespace(config=config),
    )
    if job_writes is None:
        job_status = status if job_status is None else job_status
        job_output = output if job_output is None else job_output
        job_writes = f"{job_output}EXIT={job_status}\n"
    calls: list[tuple[list[str], dict]] = []

    def runner(argv, **options):
        calls.append((list(argv), options))
        if Path(argv[0]).name == "sbatch":
            script = argv[argv.index("--wrap") + 1]
            tokens = shlex.split(script)
            log = Path(tokens[-2])
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as stream:
                stream.write(job_writes)
        return subprocess.CompletedProcess(list(argv), status, output, "")

    return calls, runner


def _replay(repo: Path, runner, *, bound=450.0, log=None):
    return promotion_gate.rerun_gate_at_integrated_revision(
        repository=repo,
        gate_check={"command": COMMAND},
        base_verdict="passed",
        integrated_revision="HEAD",
        timeout_seconds=bound,
        replay_log_path=log or repo.parent / "verify-gate.log",
        run=runner,
    )


def test_the_replay_is_submitted_as_a_batch_job_on_the_measure_partition(
    project, monkeypatch
):
    calls, runner = _record(monkeypatch, _landing())
    _replay(project, runner)

    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[:2] == ["sbatch", "--wait"]
    assert "--partition=all_debug" in argv
    assert f"--chdir={project.resolve()}" in argv
    assert options["cwd"] == str(project.resolve())


def test_the_submitted_bound_is_rendered_as_the_scheduler_reads_it(
    project, monkeypatch
):
    calls, runner = _record(monkeypatch, _landing())
    _replay(project, runner, bound=450.0)

    argv, _ = calls[0]
    assert "--time=00:07:30" in argv


def test_the_submission_carries_no_scheduler_variables_of_the_caller(
    project, monkeypatch
):
    monkeypatch.setenv("SLURM_JOB_ID", "1291586")
    monkeypatch.setenv("SLURM_JOBID", "1291586")
    monkeypatch.setenv("SBATCH_PARTITION", "rigel")
    calls, runner = _record(monkeypatch, _landing())
    _replay(project, runner)

    _, options = calls[0]
    environment = options["env"]
    assert "SLURM_JOB_ID" not in environment
    assert "SLURM_JOBID" not in environment
    assert not [key for key in environment if key.startswith(("SLURM_", "SBATCH_"))]


def test_a_long_bound_takes_the_flight_s_long_partition(project, monkeypatch):
    calls, runner = _record(monkeypatch, _landing())
    _replay(project, runner, bound=7200.0)

    argv, _ = calls[0]
    assert "--partition=rigel" in argv
    assert "--partition=all_debug" not in argv
    assert "--time=02:00:00" in argv


def test_a_flight_declaring_no_partition_keeps_the_replay_in_place(
    project, monkeypatch
):
    calls, runner = _record(monkeypatch, {"landing": {}})
    _replay(project, runner)

    argv, _ = calls[0]
    assert argv[0] == "sh"
    assert argv[1] == "-c"
    assert "srun" not in " ".join(argv)
    assert "sbatch" not in " ".join(argv)


def test_the_replay_log_names_the_partition_it_ran_on(project, monkeypatch):
    _, runner = _record(monkeypatch, _landing())
    log = project.parent / "verify-gate.log"
    _replay(project, runner, log=log)

    text = log.read_text()
    assert "# partition: all_debug" in text
    assert text.rstrip().endswith("EXIT=0")


def test_a_declared_domain_check_is_replayed_on_the_partition(project, monkeypatch):
    calls, runner = _record(monkeypatch, _landing())
    rows = promotion_gate.replay_domain_invariant_checks(
        invariants=[
            SimpleNamespace(
                id="state-bounded", rule="state stays bounded", check="true"
            )
        ],
        repository=project,
        replay_directory=project.parent / "replays",
        integrated_revision="HEAD",
        timeout_seconds=450.0,
        run=runner,
    )

    assert [row["ok"] for row in rows] == [True]
    argv, _ = calls[0]
    assert argv[:2] == ["sbatch", "--wait"]
    assert "--partition=all_debug" in argv


def test_the_job_s_own_log_decides_the_verdict(project, monkeypatch):
    """The client's return code is not the verdict: the job log's receipt is."""
    _, runner = _record(
        monkeypatch,
        _landing(),
        status=1,
        output="FAILED tests/test_subject.py::test_subject\n1 failed\n",
        job_status=0,
        job_output="1 passed\n",
    )
    report = _replay(project, runner)

    assert report["exit_status"] == 0
    assert report["integrated_verdict"] == "passed"
    assert report["ok"] is True


def test_a_job_log_without_a_receipt_is_unreadable(project, monkeypatch):
    """A job that never reached its command has no verdict, even if sbatch exits 0."""
    _, runner = _record(
        monkeypatch,
        _landing(),
        status=0,
        output="1 passed\n",
        job_writes="sbatch: error: Batch job submission failed\n",
    )
    report = _replay(project, runner)

    assert report["exit_status"] == 3
    assert report["integrated_verdict"] == "failed"
    assert report["ok"] is False


def test_only_the_last_receipt_in_the_job_log_counts(project, monkeypatch):
    _, runner = _record(
        monkeypatch,
        _landing(),
        job_writes="1 passed\nEXIT=0\nre-running\nFAILED tests/test_subject.py::test_subject\nEXIT=1\n",
    )
    report = _replay(project, runner)

    assert report["exit_status"] == 1
    assert report["integrated_verdict"] == "failed"


def test_a_stale_job_log_is_not_read_as_this_attempt_s_verdict(project, monkeypatch):
    """An earlier attempt's passing log must not answer for a job that never ran."""
    stale = project.parent / "verify-gate.log.job.log"
    stale.write_text("1 passed\nEXIT=0\n")
    _, runner = _record(
        monkeypatch,
        _landing(),
        status=1,
        output="",
        job_writes="",
    )
    report = _replay(project, runner)

    assert report["exit_status"] == 3
    assert report["integrated_verdict"] == "failed"
    assert report["ok"] is False
    assert "EXIT=" not in stale.read_text()
