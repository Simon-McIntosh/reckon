"""A landing measured on a partition keeps no base checkout on the fleet node.

Each arm of a partitioned measure is a scheduler job on another node, and that
node cannot read the fleet node's private scratch. A tree built for the arms on
the fleet node is read by nothing and, held under the launch floor's
filesystem, refuses every queued launch on the host for as long as the job
runs.

So the landing holds no tree of its own while its arms run: the base arm's
directory is a name the runner uses to tell the job apart, the job builds and
removes its own tree where it runs, and a landing measured in place holds one
base tree that the node's scratch sweep reclaims if its process is killed.

A killed process leaves its tree where it died, because no exit handler runs on
SIGKILL, so the removal is proven on the next pass rather than at the death.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import gc, landing_measure, queue_landing, runs

PROJECT = "fixture"
SUITE = "sh -c ls"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo: Path, name: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"test: prepare {name}\n\nRecord fixture {name}.")
    return _git(repo, "rev-parse", "HEAD")


def _log_of(script: str) -> Path:
    """The durable arm log the submission script appends to."""
    return Path(shlex.split(script)[-2])


def _host_landing_trees() -> set[str]:
    """The landing trees any arm may still hold in this host's own temp."""
    root = Path(tempfile.gettempdir())
    return {str(path) for path in root.glob(f"{getpass.getuser()}-landing-*")}


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
    # Primary moves on after the delivery, so the landing measures a merge.
    (repo / "README").write_text("later\n")
    _commit(repo, "primary")

    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_subject.py").write_text("import subject\n")
    (worktree / "subject.py").write_text("VALUE = 'head'\n")

    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *a, **k: SimpleNamespace(
            config={"landing": {"measure_partition": "all_debug"}}
        ),
    )
    pointer = {
        "run_id": "r-20261123T000000000000-fixture",
        "project": PROJECT,
        "repo": str(repo),
        "base_sha": base,
        "suite_command": SUITE,
        "worktree": str(worktree),
        "node": {"id": "implementation", "write_paths": ["tests/test_subject.py"]},
    }
    runs._write_json(
        runs.pointer_path(pointer["run_id"]), {"run_id": pointer["run_id"]}
    )
    return SimpleNamespace(
        home=home,
        scratch=scratch,
        repo=repo,
        base=base,
        head=head,
        worktree=worktree,
        pointer=pointer,
    )


def _measure(landing, monkeypatch: pytest.MonkeyPatch, runner):
    monkeypatch.setattr(landing_measure, "_RUN", runner, raising=False)
    return queue_landing._measure_delivery(
        dict(landing.pointer),
        landing.pointer["run_id"],
        landing.head,
        str(landing.worktree),
        (),
        request=None,
    )


def _submitting_runner(landing, seen: list[list[str]], real):
    """Answer each submitted job, recording the scratch root it saw."""

    def runner(argv, **options):
        if Path(argv[0]).name != "sbatch":
            return real(argv, **options)
        script = argv[argv.index("--wrap") + 1]
        seen.append(sorted(path.name for path in landing.scratch.iterdir()))
        log = _log_of(script)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    return runner


def _executing_runner(real):
    """Run each submitted job's script, in the environment the job gets."""

    def runner(argv, **options):
        if Path(argv[0]).name != "sbatch":
            return real(argv, **options)
        step = argv[argv.index("--wrap") + 1]
        tokens = shlex.split(step)
        script = tokens[tokens.index("-c") + 1]
        log = _log_of(step)
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=options.get("cwd"),
            capture_output=True,
            text=True,
            env=options.get("env"),
            timeout=120,
            check=False,
        )
        with log.open("a", encoding="utf-8") as stream:
            stream.write(completed.stdout)
            stream.write(completed.stderr)
        return subprocess.CompletedProcess(argv, 0, completed.stdout, completed.stderr)

    return runner


def test_a_partitioned_landing_creates_nothing_under_the_scratch_root(
    landing, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    measured = _measure(
        landing, monkeypatch, _submitting_runner(landing, seen, landing_measure._RUN)
    )

    assert seen, "the measurement submitted no arm"
    assert not measured.unreadable, measured.unreadable
    assert all(names == [] for names in seen), seen
    assert sorted(path.name for path in landing.scratch.iterdir()) == []


def test_the_job_builds_and_removes_its_own_base_tree(
    landing, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _host_landing_trees()
    measured = _measure(landing, monkeypatch, _executing_runner(landing_measure._RUN))

    assert not measured.unreadable, measured.unreadable
    log = Path(measured.suite["base"]["log_path"]).read_text(encoding="utf-8")
    tree = re.search(r"^# tree: (.+)$", log, re.MULTILINE)
    assert tree is not None, log
    # The job built the revision in its own scratch and removed it on exit.
    assert "subject.py" in log, log
    assert not Path(tree.group(1)).exists()
    assert re.search(r"^EXIT=0$", log, re.MULTILINE), log
    assert _host_landing_trees() == before


def test_one_base_tree_each_and_a_killed_landing_is_reclaimed(
    landing,
) -> None:
    with (
        landing_measure.base_tree_for_measure(
            landing.repo, landing.base, 30.0, partitioned=False
        ) as first,
        landing_measure.base_tree_for_measure(
            landing.repo, landing.base, 30.0, partitioned=False
        ) as second,
    ):
        held = sorted(path.name for path in landing.scratch.iterdir())
        assert len(held) == 2, held
        assert first != second
    assert sorted(path.name for path in landing.scratch.iterdir()) == []

    # A process killed mid-measure runs no exit handler, so its tree stays.
    orphan = landing.scratch / "landing-base-killed" / "tree"
    orphan.mkdir(parents=True)
    (orphan / "left-behind").write_text("x", encoding="utf-8")
    assert orphan.is_dir()

    report = gc.garbage_collect_orphan_scratch(
        apply=True, grace_seconds=0.0, now=time.time() + 60.0
    )
    assert report["removed"], report
    assert not (landing.scratch / "landing-base-killed").exists()


def _signalled_runner(landing, real, outcome: dict[str, object]):
    """End the first submitted job by signal, as a scheduler does.

    The scheduler terminates a job that overruns its bound. The script installs
    a TERM handler so that the job ends by a deliberate exit, which records
    ``EXIT=143`` in its log, rather than by the shell dying from the signal;
    nothing else exercises that handler.
    """

    first = [True]

    def runner(argv, **options):
        if Path(argv[0]).name != "sbatch":
            return real(argv, **options)
        step = argv[argv.index("--wrap") + 1]
        tokens = shlex.split(step)
        inner = tokens[tokens.index("-c") + 1]
        log = _log_of(step)
        log.parent.mkdir(parents=True, exist_ok=True)
        if not first[0]:
            log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")
        first[0] = False
        scratch_id = hashlib.sha256(str(log.absolute()).encode()).hexdigest()[:12]
        prefix = f"{getpass.getuser()}-landing-{scratch_id}-"
        root = Path(tempfile.gettempdir())
        process = subprocess.Popen(
            ["bash", "-c", inner],
            cwd=options.get("cwd"),
            env=options.get("env"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        held: Path | None = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            matches = [p for p in root.glob(prefix + "*") if (p / "tree").is_dir()]
            if matches:
                held = matches[0]
                break
            time.sleep(0.05)
        try:
            # Positive control: the job was holding a tree when it was signalled.
            assert held is not None, "the job built no tree to remove"
            assert (held / "tree" / "subject.py").is_file(), held
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            output = process.communicate(timeout=60)[0]
        finally:
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.communicate()
        outcome["tree"] = held
        outcome["output"] = output
        log.write_text(output, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, output, "")

    return runner


def test_a_job_ended_by_signal_removes_its_tree(
    landing, monkeypatch: pytest.MonkeyPatch
) -> None:
    landing.pointer["suite_command"] = "sh -c 'sleep 60'"
    outcome: dict[str, object] = {}
    _measure(
        landing,
        monkeypatch,
        _signalled_runner(landing, landing_measure._RUN, outcome),
    )

    held = outcome.get("tree")
    assert isinstance(held, Path), outcome
    output = str(outcome.get("output") or "")
    # Without the handler the shell dies from the signal and records EXIT=0, so
    # this receipt is what shows the handler turned it into a deliberate exit.
    assert re.search(r"^EXIT=143$", output, re.MULTILINE), output
    # The deliberate exit also runs the EXIT trap that removes the tree.
    assert not held.exists(), held
