"""A review launched by a sweep reaches its attempt record, or says where it stopped.

The follower's review sweep hands its dispatch no launcher, so the launch takes
the supervisor path: it publishes a claim, composes a worktree and a prompt over
several seconds, and writes the attempt record just before starting the
supervisor. A review found afterwards holding its brief and its claim and
nothing else therefore stopped inside that window, and every instance of it read
afterwards as a run that was never launched at all.

These cases drive the sweep the way the follower does and assert the launch gets
past its claim, including when the launcher ends inside the window — the shape a
process that is stopped rather than refused leaves behind.
"""

# ruff: noqa: F811 - the imported fixture is also a test parameter.

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from reckon.crew import queue_admission, resumption, runs
from tests.test_a_queued_launch_ignores_the_coordinator_backlog import (
    _config_for,
    _queue,
    admit_repo,  # noqa: F401 - the imported fixture is also a parameter
)
from tests.test_scoring_dispatches_its_review import (
    CONFIG,
    _scoring_pointer,
    isolated_project,  # noqa: F401 - the imported fixture is also a parameter
)

PROJECT = "sample"


class LauncherEnded(BaseException):
    """A launcher that stopped wanting what the process that ran it wanted.

    Deliberately not an ``Exception``: a process that ends — a killed follower,
    a re-armed session host, a service cutting a bounded slice — runs no handler
    at all, and modelling that with an exception the refusal guard catches would
    release the claim and prove nothing about the shape a stopped launch leaves.
    """


def _dispatch_module():
    return importlib.import_module("reckon.crew.dispatch")


def _sweep() -> dict:
    """Sweep exactly as the follower's review reflex does, with no launcher."""
    return resumption.sweep(PROJECT, config=CONFIG)


def _review_runs() -> list[str]:
    return [
        str(row.get("run_id") or "")
        for row in runs.list_live(project=PROJECT)
        if "review-of-" in str(row.get("run_id") or "")
    ]


def test_a_sweep_launched_review_reaches_its_attempt_record(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive half: the sweep's launcher reaches the supervisor's gate."""
    config_home, repo = isolated_project
    _scoring_pointer(config_home, repo, "r-awaiting-review")
    dispatch_module = _dispatch_module()
    started: list[str] = []

    def start_supervisor(spec_path: Path, run_directory: Path, run_id: str) -> int:
        started.append(run_id)
        return os.getpid()

    monkeypatch.setattr(dispatch_module, "_start_supervisor", start_supervisor)

    report = _sweep()

    dispatched = report["reviews"]["dispatched"]
    assert dispatched, report["reviews"]
    review_id = dispatched[0]
    directory = runs.run_dir(review_id)
    assert (directory / "claim.json").exists()
    assert (directory / "attempt.json").exists()
    assert started == [review_id]


def test_a_refused_launch_leaves_no_claim_behind(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discriminating half: a refusal unwinds, so a kept claim is not one.

    A launch that refuses inside its guarded window gives its claim back and
    takes the run directory with it. The field's stranding keeps that directory
    and its claim, so the cause is a launcher that ended in the window rather
    than one that was refused by it — this case is what makes that reading
    usable.
    """
    config_home, repo = isolated_project
    _scoring_pointer(config_home, repo, "r-awaiting-review")
    dispatch_module = _dispatch_module()
    runs_before = sorted(path.name for path in runs.runs_dir().iterdir())

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("the launcher refused this run")

    monkeypatch.setattr(dispatch_module, "_create_worktree", refuse)

    report = _sweep()

    assert not report["reviews"].get("dispatched"), report["reviews"]
    assert _review_runs() == [], _review_runs()
    assert sorted(path.name for path in runs.runs_dir().iterdir()) == runs_before, (
        "the refused launch left a run directory behind, which reads afterwards "
        "as a launch that stopped rather than one that was refused"
    )


def test_a_launcher_that_ends_stops_after_its_attempt_record(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape the field shows, with the attempt record the repair adds.

    The window this node investigated runs from the claim's publication to the
    attempt record, and the worktree is cut inside it. A launcher that ends
    there writes nothing further, so the run keeps whatever the window made
    durable. The attempt marker is published with the claim precisely so that
    this run names the attempt it stopped on instead of reading as a launch
    that never happened.
    """
    config_home, repo = isolated_project
    _scoring_pointer(config_home, repo, "r-awaiting-review")
    dispatch_module = _dispatch_module()

    def end(*args: object, **kwargs: object) -> None:
        raise LauncherEnded("the sweeping process ended mid-launch")

    monkeypatch.setattr(dispatch_module, "_create_worktree", end)

    with pytest.raises(LauncherEnded):
        _sweep()

    reviews = _review_runs()
    assert len(reviews) == 1, reviews
    directory = runs.run_dir(reviews[0])
    assert (directory / "claim.json").exists()
    assert (directory / "attempt.json").exists(), (
        "the run kept its claim and no attempt record, which is the shape that "
        "reads afterwards as a launch that never started"
    )
    assert not (directory / "worker.json").exists()


def test_a_refused_queue_launch_keeps_no_attempt_record(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused queue launch goes back to waiting, not to a stopped attempt.

    A queued request is launched by admission rather than discarded, so its
    refusal restores the pointer to the phase it waited in instead of removing
    the run directory. The attempt record published with the starting claim
    describes an attempt that never ran; kept, the restored request reads
    afterwards as a launch that stopped after its claim.
    """
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="queued", run_id="r-queued"))
    attempt = runs.run_dir("r-queued") / "attempt.json"
    dispatch_module = _dispatch_module()

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("the launcher refused this run")

    monkeypatch.setattr(dispatch_module, "_create_worktree", refuse)

    queue_admission.admit_once(
        config_for=resolve,
        launcher=lambda *args, **kwargs: pytest.fail(
            "a refused launch must not start a worker"
        ),
    )

    restored = runs.read_pointer("r-queued")
    assert restored["phase"] == "queued", restored.get("phase")
    assert not attempt.exists(), (
        "the restored request kept the attempt record of the launch that was "
        "refused, which reads afterwards as a stranded launch"
    )
