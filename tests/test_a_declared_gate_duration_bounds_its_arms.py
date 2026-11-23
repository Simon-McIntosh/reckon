"""A run's declared gate duration bounds its landing arms.

A project arms one measurement floor for every run it lands. A gate that
legitimately takes far longer is ended mid-run by that floor, so its landing is
refused and the work can land only by raising the bound for every landing in the
project or by a hand merge.

A run therefore declares how long its gate takes. Every landing arm for that run
is submitted with a limit no shorter than the declared duration, on a partition
that admits it, while every other run in the same project keeps the project's
bound. A duration longer than any configured partition admits is refused at
declaration, naming the longest limit available.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import flight
from reckon.cli import main as cli_main
from reckon.crew import landing_measure, queue_landing, runs

PROJECT = "fixture"
SUITE = "sh -c true"
RUN_A = "r-20261123T000000000000-gate-a"
RUN_B = "r-20261123T000100000000-gate-b"

PROJECT_FLOOR_SECONDS = 600.0
DECLARED_SECONDS = 4500.0

PROJECT_CONFIG = {"landing": {"measure_partition": "all_debug"}}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo: Path, name: str) -> str:
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", f"test: prepare {name}"],
        check=True,
        capture_output=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _time_limit_seconds(argv: list[str]) -> float:
    limit = ""
    for token in argv:
        if token.startswith("--time="):
            limit = token.split("=", 1)[1]
    if not limit:
        return _time_limit_seconds(shlex.split(argv[argv.index("--wrap") + 1]))
    hours, minutes, seconds = limit.split(":")
    return float(hours) * 3600.0 + float(minutes) * 60.0 + float(seconds)


def _partition_of(argv: list[str]) -> str:
    for token in argv:
        if token.startswith("--partition="):
            return token.split("=", 1)[1]
    return ""


@pytest.fixture
def landing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch))

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "subject.py").write_text("VALUE = 'base'\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_subject.py").write_text("import subject\n")
    base = _commit(repo, "base")
    (repo / "subject.py").write_text("VALUE = 'head'\n")
    head = _commit(repo, "head")
    (repo / "README").write_text("later\n")
    _commit(repo, "primary")

    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_subject.py").write_text("import subject\n")
    (worktree / "subject.py").write_text("VALUE = 'head'\n")

    monkeypatch.setattr(
        flight, "resolve", lambda *a, **k: SimpleNamespace(config=PROJECT_CONFIG)
    )
    pointer = {
        "run_id": RUN_A,
        "project": PROJECT,
        "repo": str(repo),
        "base_sha": base,
        "suite_command": SUITE,
        "worktree": str(worktree),
        "node": {"id": "implementation", "write_paths": ["tests/test_subject.py"]},
        "gate_declaration": {"duration": str(int(DECLARED_SECONDS))},
    }
    runs._write_json(
        runs.pointer_path(RUN_A),
        {**pointer, "manifest_path": str(tmp_path / "m-a.md")},
    )
    runs._write_json(
        runs.pointer_path(RUN_B),
        {
            **pointer,
            "run_id": RUN_B,
            "gate_declaration": None,
            "manifest_path": str(tmp_path / "m-b.md"),
        },
    )
    return SimpleNamespace(
        home=home,
        repo=repo,
        base=base,
        head=head,
        worktree=worktree,
        pointer=pointer,
    )


def _recording_runner(real, seen: list[list[str]]):
    def runner(argv, **options):
        if Path(argv[0]).name != "sbatch":
            return real(argv, **options)
        seen.append(argv)
        script = argv[argv.index("--wrap") + 1]
        log = Path(shlex.split(script)[-2])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    return runner


def test_a_declared_duration_bounds_both_arms_on_a_partition_that_admits_it(
    landing, monkeypatch
):
    seen: list[list[str]] = []
    runner = _recording_runner(landing_measure._RUN, seen)
    monkeypatch.setattr(landing_measure, "_RUN", runner, raising=False)
    queue_landing._measure_delivery(
        {**landing.pointer, "run_id": RUN_A},
        RUN_A,
        landing.head,
        str(landing.worktree),
        (),
        request=None,
    )
    assert len(seen) >= 2
    for argv in seen:
        limit = _time_limit_seconds(argv)
        assert limit >= DECLARED_SECONDS, argv
        partition = _partition_of(argv)
        maximum = landing_measure.partition_max_seconds(partition, PROJECT_CONFIG)
        assert maximum is None or limit <= maximum, (argv, maximum)


def test_an_undeclared_run_keeps_the_project_bound(landing, monkeypatch):
    seen: list[list[str]] = []
    runner = _recording_runner(landing_measure._RUN, seen)
    monkeypatch.setattr(landing_measure, "_RUN", runner, raising=False)
    queue_landing._measure_delivery(
        {**landing.pointer, "run_id": RUN_B, "gate_declaration": None},
        RUN_B,
        landing.head,
        str(landing.worktree),
        (),
        request=None,
    )
    assert seen
    for argv in seen:
        assert _partition_of(argv) == "all_debug", argv
        assert _time_limit_seconds(argv) == PROJECT_FLOOR_SECONDS, argv


def test_a_duration_no_partition_admits_is_refused_at_declaration(landing, monkeypatch):
    config = {
        "landing": {
            "measure_partition": "all_debug",
            "measure_long_partition": "long_pool",
            "partition_max_seconds": {"all_debug": 7200, "long_pool": 14400},
        }
    }
    monkeypatch.setattr(
        flight, "resolve", lambda *a, **k: SimpleNamespace(config=config)
    )
    result = CliRunner().invoke(
        cli_main,
        ["crew", "declare-gate", "--run", RUN_A, "--duration", "20000"],
    )
    assert result.exit_code != 0, result.output
    assert "04:00:00" in result.output
    assert "long_pool" in result.output


def test_a_duration_a_partition_admits_is_kept_on_the_run(landing):
    result = CliRunner().invoke(
        cli_main,
        ["crew", "declare-gate", "--run", RUN_B, "--duration", "4500"],
    )
    assert result.exit_code == 0, result.output
    declaration = json.loads(result.output)["gate_declaration"]
    assert declaration["duration"] == "4500"
