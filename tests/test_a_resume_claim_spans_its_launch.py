"""A resume holds its in-process launch claim across the attempt's recording.

The registry that separates two passes of one process says a launch of a run is
under way. It is taken, the in-flight mark is written, the plan is composed and
the worker spawned, and only then is the attempt's pid folded onto the pointer.
Two placements of that entry each defeat it:

* Taken before the guarded block, a mark that raised leaves the entry behind.
  The launch never started, nothing will ever discard the entry, and every later
  resume of that run in this process is refused against a launch that is not
  there — for the life of the process.
* Released on the way out of the guarded block rather than after the attempt is
  written, the entry is gone for the whole window between the launcher returning
  and ``record_resumption`` recording the attempt. A second pass of the same
  process taken inside that window is not refused by the registry and launches a
  worker beside the one already starting.

Both cases drive ``_resume`` directly with a launcher seam, so no worker and no
thread is started and there is nothing left to end. Each asserts the registry is
left as it was found.

The declared mutation is the pre-fix placement: the registry add and the mark
moved back before the guarded block, and the discard moved back before the
attempt is recorded. Applied together, both cases go red.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reckon.crew import resumption, runs
from reckon.crew.node import CrewError

DECLARED_MUTATION = (
    "two controls: move the registry add and mark_resume_launching back before "
    "the try, and the raised-mark case must fail; move the discard back before "
    "record_resumption, and the concurrent-pass case must fail"
)

RUN_ID = "r-resume-claim"


def _launcher(pid: int) -> Callable[..., int]:
    """A launcher seam standing in for the supervisor spawn, returning ``pid``."""

    def launcher(plan: Any, **kwargs: Any) -> int:
        return pid

    return launcher


@pytest.fixture(autouse=True)
def _registry_is_clean() -> Any:
    """No case inherits a registry entry left by another."""
    resumption._LAUNCHES_IN_THIS_PROCESS.clear()
    yield
    resumption._LAUNCHES_IN_THIS_PROCESS.clear()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run whose launch reaches the seam, with no external door consulted.

    The record reaches the launcher with the in-process guards and the
    fleet-gate read only; the plan is a token, the advice prompt is written
    under a temporary crew home, and ``record_resumption`` is replaced per case
    so nothing outside this module is written but the pointer.
    """
    config_home = tmp_path / "config"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: stub-node\nstatus: waiting\n", encoding="utf-8")
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "",
            "repo": str(worktree),
            "worktree": str(worktree),
            "backend": "alpha",
            "launch": "cli",
            "phase": "working",
            "pid": None,
            "manifest_path": str(manifest),
            "log_path": str(directory / "stream.jsonl"),
            "attempt": 1,
            "created_at": "2026-10-02T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        resumption, "resume_plan", lambda *a, **k: SimpleNamespace(stdin_text="")
    )
    monkeypatch.setattr(resumption, "_require_fleet_gate_open", lambda: None)
    monkeypatch.setattr(resumption, "record_resumption", lambda *a, **k: None)


def test_a_raised_mark_leaves_no_registry_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mark that raised must not leave the run refused by a launch that never
    started.

    The mark is made to raise once, on the take. The launch raises, so the entry
    it added must have been discarded with it: the run is absent from the
    registry, and a second resume of the same run in this process is not refused
    by the registry — it launches.
    """
    _fixture(tmp_path, monkeypatch)
    calls = {"count": 0}
    real_mark = resumption.mark_resume_launching

    def mark_seam(run_id: str) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("the in-flight mark could not be written")
        real_mark(run_id)

    monkeypatch.setattr(resumption, "mark_resume_launching", mark_seam)

    with pytest.raises(RuntimeError):
        resumption._resume(
            RUN_ID,
            runs.read_pointer(RUN_ID),
            config=None,
            launcher=_launcher(424242),
            advice="continue",
        )

    assert RUN_ID not in resumption._LAUNCHES_IN_THIS_PROCESS

    # The registry no longer refuses the run: a second resume of it in this
    # process launches rather than raising the in-flight refusal.
    launched = resumption._resume(
        RUN_ID,
        runs.read_pointer(RUN_ID),
        config=None,
        launcher=_launcher(424243),
        advice="continue",
    )
    assert launched["pid"] == 424243, launched
    assert RUN_ID not in resumption._LAUNCHES_IN_THIS_PROCESS


def test_a_pass_between_the_launcher_and_the_recorded_attempt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry is held until the attempt is written.

    The window between the launcher returning and the attempt being recorded is
    entered from inside ``record_resumption`` itself: a second resume of the
    same run, in this process, must meet the registry and be refused rather than
    launch a worker beside the one already starting. The outer launch then
    records its own single attempt.
    """
    _fixture(tmp_path, monkeypatch)
    observed: dict[str, Any] = {"refused": False, "done": False}
    recorded: list[str] = []

    def record_seam(run_id: str, **kwargs: Any) -> None:
        recorded.append(run_id)
        if not observed["done"]:
            observed["done"] = True
            try:
                resumption._resume(
                    run_id,
                    runs.read_pointer(run_id),
                    config=None,
                    launcher=_launcher(999),
                    advice="continue",
                )
            except CrewError as exc:
                observed["refused"] = True
                observed["refusal"] = str(exc)

    monkeypatch.setattr(resumption, "record_resumption", record_seam)

    launched = resumption._resume(
        RUN_ID,
        runs.read_pointer(RUN_ID),
        config=None,
        launcher=_launcher(424242),
        advice="continue",
    )

    assert launched["pid"] == 424242, launched
    assert observed["refused"] is True, observed
    assert "already has a resume launching" in observed["refusal"], observed
    # The refused pass started nothing, so the outer launch recorded the one and
    # only attempt for the run.
    assert recorded == [RUN_ID], recorded
    assert RUN_ID not in resumption._LAUNCHES_IN_THIS_PROCESS
