"""A launch span is dated, and one that stops inside it becomes a recorded refusal.

Nothing inside a blocked launch can observe its own bound, so the admission
pass, a separate process, releases it: the release records the step the span
reached, gives the claim back and removes the run directory. The launch itself
fences on that record rather than on the claim, because the holder may still be
alive and blocked, and reads it twice — before the spawn and after it — so a
release landing between the two is caught and the spawned worker is signalled.
"""

# ruff: noqa: F811 - the imported fixture is also a test parameter.

from __future__ import annotations

import importlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims, dispatch_refusals, queue_admission, runs
from reckon.crew.node import CrewError, TaskNode
from tests.test_scoring_dispatches_its_review import (
    CONFIG,
    isolated_project,  # noqa: F401 - the imported fixture is also a parameter
)

PROJECT = "sample"
RUN_ID = "r-held-launch"


class LaunchEnded(BaseException):
    """A launch whose process ended inside the span, writing nothing further."""


def _dispatch_module():
    return importlib.import_module("reckon.crew.dispatch")


def _node(config_home: Path, name: str) -> TaskNode:
    """A node on the fixture plan's own section, so the plan check has it at base."""
    return TaskNode(
        id=f"node-{name}",
        goal="launch one node past the span bound",
        plan="fixture",
        section="s2",
        spec_level="guided",
        done_when=(
            "pytest tests/test_a_launch_span_is_dated_and_bounded.py reports "
            "0 failures past the 900s span bound"
        ),
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _launch(repo: Path, config_home: Path, name: str, *, launcher, run_id: str) -> dict:
    return _dispatch_module().dispatch(
        node=_node(config_home, name),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=f"session-{name}",
        launcher=launcher,
        run_id=run_id,
    )


def _held_launch(repo: Path, config_home: Path) -> None:
    """Leave a claim behind exactly as a span cut off before its spawn does."""

    def end(*args: object, **kwargs: object) -> None:
        raise LaunchEnded("the process running the launch span ended inside it")

    with pytest.raises(LaunchEnded):
        _launch(repo, config_home, "held", launcher=end, run_id=RUN_ID)

    claim = dispatch_claims.read_launch_claim(RUN_ID)
    # This pid began after the claim, so it cannot be the original holder.
    claim["launched_at"] = "1970-01-01T00:00:00+00:00"
    runs._write_json(runs.run_dir(RUN_ID) / "claim.json", claim)


def _release(run_id: str) -> list[str]:
    live = runs.list_live()
    return queue_admission._release_stranded_launches(
        live, now_seconds=time.time() + 100_000
    )


def test_a_launch_held_past_the_bound_is_released_and_recorded(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    _held_launch(repo, config_home)

    directory = runs.run_dir(RUN_ID)
    assert (directory / "claim.json").exists()
    assert dispatch_claims.read_launch_claim(RUN_ID)["step"] == "claim-published"

    released = _release(RUN_ID)

    assert released == [RUN_ID]
    assert not runs.pointer_path(RUN_ID).exists()
    assert not directory.exists(), "the released launch kept its run directory"
    refusal = dispatch_refusals.launch_refusal(RUN_ID)
    assert refusal is not None
    assert refusal["step"] == "claim-published"
    assert refusal["holder"]["holder_pid"] == os.getpid()
    assert refusal["holder"]["holder_host"]


def test_a_released_launch_resumes_into_a_refusal_rather_than_a_spawn(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    _held_launch(repo, config_home)
    _release(RUN_ID)

    spawned: list[int] = []

    def launcher(*args: object, **kwargs: object) -> int:
        spawned.append(os.getpid())
        return os.getpid()

    with pytest.raises(CrewError) as refused:
        _launch(repo, config_home, "resumed", launcher=launcher, run_id=RUN_ID)

    message = str(refused.value)
    assert "released as a stranded launch" in message
    assert "at step claim-published" in message
    # The remedy the operator is told must fit a released launch, so the
    # plan-review action registered for another refusal must not ride along.
    assert "allow-unreviewed-plan" not in message
    assert "reckon crew review" not in message
    assert "`reckon crew dispatch` the node again" in message
    assert spawned == [], "the released launch spawned a worker anyway"


def test_a_release_pinned_after_the_spawn_read_signals_its_worker(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    worker: list[subprocess.Popen] = []

    def launcher(*args: object, **kwargs: object) -> int:
        # The release lands after the pre-spawn check and before the post-spawn
        # read, which is the window the second read exists to close.
        dispatch_refusals.record_launch_refusal(
            run_id=RUN_ID, step="claim-published", holder={}
        )
        worker.append(subprocess.Popen(["sleep", "60"], start_new_session=True))
        return worker[0].pid

    with pytest.raises(CrewError) as refused:
        _launch(repo, config_home, "pinned", launcher=launcher, run_id=RUN_ID)

    assert "released as a stranded launch" in str(refused.value)
    assert "while its worker was being started" in str(refused.value)
    deadline = time.monotonic() + 10
    while worker[0].poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert worker[0].poll() is not None, "the spawned worker outlived its refusal"


def test_the_claim_names_its_holder_and_the_step_advances(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    seen: list[dict] = []

    def launcher(*args: object, **kwargs: object) -> int:
        seen.append(dispatch_claims.read_launch_claim(RUN_ID))
        return os.getpid()

    record = _launch(repo, config_home, "advancing", launcher=launcher, run_id=RUN_ID)

    assert record["run_id"] == RUN_ID
    first = seen[0]
    assert first["holder_pid"] == os.getpid()
    assert first["holder_host"]
    assert first["launched_at"]
    assert first["step"] == "claim-published"
    final = dispatch_claims.read_launch_claim(RUN_ID)
    assert final["step"] == "identity-published"
