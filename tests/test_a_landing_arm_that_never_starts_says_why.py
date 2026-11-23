"""A landing arm that never reaches the scheduler says why.

An arm whose submission is refused before a job exists ends in hundredths of a
second with no job id and an empty log. Reading only the job's absence gives
"the base arm did not run", which tells a reader nothing about what refused the
submission. The submission's own command, status and output are what answer
that, and they are recorded on the arm as well as quoted in the refusal.

An arm whose job does start is unaffected: its receipt is still what reports its
status and its failing ids.

The refusal is produced the same way whether the landing is attempted by the
lander's pass or by ``crew complete --land``, because both read the same stored
measurement's own reason.

The scheduler is stubbed below the submission reader, so every fact asserted
here is read from what the measurement itself records.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import landing_measure, queue_landing, runs

COMMAND = "python -m pytest {node_test_files}"
SUBJECT = "tests/test_subject.py"
SUBJECT_ID = "tests/test_subject.py::test_subject"

JOB = "1319168"

REFUSAL_OUTPUT = (
    "sbatch: error: Batch job submission failed: Invalid partition name specified\n"
)
GREEN = "1 passed in 0.10s\nEXIT=0\n"
RED = f"FAILED {SUBJECT_ID}\n1 failed in 0.10s\nEXIT=1\n"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], text=True
    ).strip()


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A repository with a base commit, a head commit and one test file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "subject.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / SUBJECT).write_text(
        "def test_subject():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "subject.py", SUBJECT)
    _git(repo, "commit", "-qm", "test: prepare base\n\nRecord fixture inputs.")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "subject.py").write_text("VALUE = 'head'\n", encoding="utf-8")
    _git(repo, "add", "subject.py")
    _git(repo, "commit", "-qm", "test: prepare head\n\nRecord delivered input.")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    return repo, base, head


@pytest.fixture(name="crew_home")
def crew_home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _scheduler(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[tuple[str, int, str, str]],
):
    """Answer each sbatch submission from ``(log, status, stdout, stderr)``.

    The real submission builder runs, so the argv under test is the one the
    landing would submit. Only the scheduler call is answered.
    """
    state: dict[str, Any] = {"logs": [], "answers": list(answers)}
    real_submission = landing_measure.partition_submission
    real_run = subprocess.run

    def submission(**options):
        selected, argv, env = real_submission(**options)
        state["logs"].append(Path(options["log_path"]))
        return selected, argv, env

    def run(argv, **options):
        if not argv or argv[0] != "sbatch":
            return real_run(argv, **options)
        text, status, stdout, stderr = state["answers"].pop(0)
        log = state["logs"].pop(0)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(argv, status, stdout, stderr)

    monkeypatch.setattr(landing_measure, "partition_submission", submission)
    monkeypatch.setattr(landing_measure, "_RUN", run)
    return state


def _measure(project, tmp_path: Path) -> landing_measure.Measurement:
    repo, base, head = project
    return landing_measure.measure_run(
        command=COMMAND,
        tree=repo,
        base_sha=base,
        head_sha=head,
        base_tree=repo,
        logs_dir=tmp_path / "run" / "landing-measure",
        test_files=(SUBJECT,),
        project="fixture",
        changed=[],
        config={"landing": {"measure_partition": "all_debug"}},
    )


def test_a_submission_that_starts_no_job_records_what_refused_it(
    project, tmp_path, monkeypatch
):
    """The arm carries the submission, its status and its output; the refusal quotes them."""
    _scheduler(
        monkeypatch,
        [("", 1, "", REFUSAL_OUTPUT), (GREEN, 0, JOB + "\n", "")],
    )

    measured = _measure(project, tmp_path)

    arm = measured.suite["base"]
    assert arm["exit_status"] == 3
    assert arm["job_id"] == ""
    assert arm["submission_failed"] is True
    reason = arm["unreadable_reason"]
    assert "never started a scheduler job" in reason
    assert "sbatch --wait" in reason
    assert "exited 1 with" in reason
    assert "Invalid partition name specified" in reason

    refusal = measured.unreadable
    assert "did not run" not in refusal
    assert "never reached the scheduler" in refusal
    assert "the base arm" in refusal
    assert "Invalid partition name specified" in refusal


def test_an_arm_whose_job_starts_and_fails_is_recorded_as_before(
    project, tmp_path, monkeypatch
):
    """A submitted job's receipt still reports its status and its failing ids."""
    _scheduler(
        monkeypatch,
        [(RED, 1, JOB + "\n", ""), (GREEN, 0, JOB + "\n", "")],
    )

    measured = _measure(project, tmp_path)

    assert measured.unreadable == ""
    base = measured.suite["base"]
    assert base["exit_status"] == 1
    assert tuple(base["failure_ids"]) == (SUBJECT_ID,)
    assert base["submission_failed"] is False
    assert measured.suite["head"]["exit_status"] == 0


def test_the_refusal_reads_the_same_through_the_pass_and_through_complete_land(
    project, tmp_path, monkeypatch, crew_home
):
    """The pass's guard and complete --land's stored guard state one refusal."""
    _scheduler(
        monkeypatch,
        [("", 1, "", REFUSAL_OUTPUT), (GREEN, 0, JOB + "\n", "")],
    )
    measured = _measure(project, tmp_path)
    run_id = "r-test-arm-never-started"
    queue_landing._record_suite_evidence(run_id, measured, promoted=True)

    # The lander's pass judges the measurement it just took.
    from_the_pass = queue_landing._measure_guard(measured)
    # `crew complete --land` reads the run's stored measurement and judges it.
    assert "landing measure unreadable: " in from_the_pass
    with pytest.raises(runs.CrewError) as raised:
        queue_landing._verified_measurement(run_id)

    assert str(raised.value) == from_the_pass
    assert "Invalid partition name specified" in str(raised.value)
    stored = json.loads(
        (runs.run_dir(run_id) / "landing-measure" / "measurement.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["suite"]["base"]["submission_failed"] is True
