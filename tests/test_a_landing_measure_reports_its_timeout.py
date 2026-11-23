"""A landing arm's job follows its population, and a timeout is recorded as one.

An arm the scheduler stops at its time limit leaves no reading at all: the
script's EXIT trap is killed before it can print the arm's exit receipt. Such
an arm must be recorded as timed out, with the job the scheduler cancelled and
the limit it was given, and the landing's refusal must name the timeout rather
than claim the arm did not run.

A population must also name each test file once, and a project that declares
its measurement limit must get that limit on every arm, on a partition whose
maximum admits it.

The scheduler is stubbed only below the submission, so every fact below is read
from the argv, the script and the log the measurement itself produces.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import landing_measure

COMMAND = "python -m pytest {node_test_files}"
BASE = "b" * 40
HEAD = "a" * 40
JOB = "1319168"
TEN_MINUTES = "00:10:00"
GREEN = "24 passed in 0.10s\nEXIT=0\n"

CANCEL_LINES = [
    "# revision: " + BASE,
    "# host: 98dci4-clu-2048",
    "# tree: /tmp/landing/tree",
    "# command: env python -m pytest tests/test_subject.py",
    "........................................ 56%",
    "slurmstepd: error: *** STEP " + JOB + ".0 ON 98dci4-clu-2048 CANCELLED AT "
    "2026-11-22T20:09:12 DUE TO TIME LIMIT ***",
    "slurmstepd: error: *** JOB "
    + JOB
    + " ON 98dci4-clu-2048 CANCELLED AT 2026-11-22T20:09:12 DUE TO TIME LIMIT ***",
    "",
]
TIME_LIMIT_CANCEL = "\n".join(CANCEL_LINES)


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], text=True
    ).strip()


@pytest.fixture(name="project")
def project_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A repository whose armed test also imports the module the node changed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "subject.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_subject.py").write_text(
        "import subject\n\ndef test_subject():\n    assert subject.VALUE\n",
        encoding="utf-8",
    )
    _git(repo, "add", "subject.py", "tests/test_subject.py")
    _git(repo, "commit", "-qm", "test: prepare base\n\nRecord fixture inputs.")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "subject.py").write_text("VALUE = 'head'\n", encoding="utf-8")
    _git(repo, "add", "subject.py")
    _git(repo, "commit", "-qm", "test: prepare head\n\nRecord delivered input.")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    return repo, base, head


def _scheduler(monkeypatch: pytest.MonkeyPatch, answers: list[tuple[str, int, str]]):
    """Stub the scheduler below the submission reader.

    The real submission builder runs, so the argv and the script under test are
    the ones a landing would submit. Only the scheduler call is answered, from
    one ``(log text, job exit, sbatch stdout)`` per arm, in the order the arms
    run.
    """
    state: dict[str, Any] = {"submissions": [], "logs": [], "answers": list(answers)}
    real_submission = landing_measure.partition_submission
    real_run = subprocess.run

    def submission(**options):
        selected, argv, env = real_submission(**options)
        state["submissions"].append(
            {
                "partition": selected,
                "argv": argv,
                "script": options["script"],
                "log": Path(options["log_path"]),
            }
        )
        state["logs"].append(Path(options["log_path"]))
        return selected, argv, env

    def run(argv, **options):
        if not argv or argv[0] != "sbatch":
            return real_run(argv, **options)
        text, status, stdout = state["answers"].pop(0)
        log = state["logs"].pop(0)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(argv, status, stdout, "")

    monkeypatch.setattr(landing_measure, "partition_submission", submission)
    monkeypatch.setattr(landing_measure, "_RUN", run)
    return state


def _measure(
    project,
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    test_files: tuple[str, ...] = ("tests/test_subject.py",),
    changed: list[str] | None = None,
):
    repo, base, head = project
    return landing_measure.measure_run(
        command=COMMAND,
        tree=repo,
        base_sha=base,
        head_sha=head,
        base_tree=repo,
        logs_dir=tmp_path / "run" / "landing-measure",
        test_files=test_files,
        project="fixture",
        changed=[] if changed is None else changed,
        config=config or {"landing": {"measure_partition": "all_debug"}},
    )


def _limits(submissions: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each submission's partition and the ``--time`` limit it carries."""
    limits: list[tuple[str, str]] = []
    for submission in submissions:
        text = next(
            token.split("=", 1)[1]
            for token in submission["argv"]
            if token.startswith("--time=")
        )
        limits.append((submission["partition"], text))
    return limits


def _population_invocations(script: str) -> list[list[str]]:
    return [
        shlex.split(line)
        for line in script.splitlines()
        if line.startswith("env ") and line.rstrip().endswith("-n 8")
    ]


def test_a_time_limit_cancel_is_recorded_as_a_timeout(project, tmp_path, monkeypatch):
    """The scheduler's cancel line is read as a timeout, then named in the refusal."""
    _scheduler(monkeypatch, [(TIME_LIMIT_CANCEL, 1, ""), (GREEN, 0, JOB + "\n")])

    measured = _measure(project, tmp_path)

    arm = measured.suite["base"]
    assert arm["timed_out"] is True
    assert arm["exit_status"] == 3
    assert arm["job_id"] == JOB
    assert arm["time_limit"] == TEN_MINUTES
    refusal = measured.unreadable
    assert "did not run" not in refusal
    assert "job 1319168" in refusal
    assert f"limit {TEN_MINUTES}" in refusal
    assert "exceeded its 600s bound" in refusal
    log = Path(arm["log_path"])
    assert log.is_relative_to(tmp_path / "run" / "landing-measure")
    assert "DUE TO TIME LIMIT" in log.read_text(encoding="utf-8")


def test_a_population_naming_one_file_twice_runs_it_once(
    project, tmp_path, monkeypatch
):
    """A node's test that is also an importer is named once in every arm."""
    state = _scheduler(monkeypatch, [(GREEN, 0, JOB + "\n")] * 4)

    measured = _measure(project, tmp_path, changed=["subject.py"])

    population_scripts = [
        submission["script"]
        for submission in state["submissions"]
        if _population_invocations(submission["script"])
    ]
    assert len(population_scripts) == 2
    for script in population_scripts:
        (invocation,) = _population_invocations(script)
        assert invocation.count("tests/test_subject.py") == 1
    assert measured.population["files"] == ["tests/test_subject.py"]


def test_a_declared_limit_reaches_every_arm_on_a_partition_that_admits_it(
    project, tmp_path, monkeypatch
):
    """The declared measure limit is the floor every arm's job is submitted under."""
    long_config = {
        "landing": {
            "measure_partition": "all_debug",
            "measure_long_partition": "rigel",
            "measure_timeout_seconds": 1800,
        }
    }
    state = _scheduler(monkeypatch, [(GREEN, 0, JOB + "\n")] * 8)
    _measure(project, tmp_path, config=long_config)
    limits = _limits(state["submissions"])
    assert limits
    assert limits == [("all_debug", "00:30:00")] * len(limits)

    past_an_hour = dict(long_config, landing=dict(long_config["landing"]))
    past_an_hour["landing"]["measure_timeout_seconds"] = 5400
    state = _scheduler(monkeypatch, [(GREEN, 0, JOB + "\n")] * 8)
    _measure(project, tmp_path, config=past_an_hour)
    limits = _limits(state["submissions"])
    assert limits
    assert limits == [("rigel", "01:30:00")] * len(limits)


def test_every_arm_log_names_its_revision_its_command_and_its_exit(
    project, tmp_path, monkeypatch
):
    """Every submitted job prints the header its log needs and its own receipt."""
    state = _scheduler(monkeypatch, [(GREEN, 0, JOB + "\n")] * 4)

    measured = _measure(project, tmp_path)

    assert measured.unreadable == ""
    logs = tmp_path / "run" / "landing-measure"
    assert state["submissions"]
    for submission in state["submissions"]:
        script = submission["script"]
        assert "printf '# revision: %s\n# host: %s\n# tree: %s\n'" in script
        assert "printf '# command: %s\n'" in script
        assert 'printf "EXIT=%s\n" "$status"' in script
        assert submission["log"].is_relative_to(logs)
        assert submission["log"].read_text(encoding="utf-8").endswith("EXIT=0\n")
