"""A run whose stream its backend refuses does not end the project's sweep.

A pointer moved from one worker harness to another keeps the stream its first
harness wrote, and the second harness's dialect refuses to fold a stream that
carries none of its own events. The recovery sweep re-observes every live
pointer of a project in one pass, and that refusal used to leave the pass:
every other run of the project went unclassified, and no review any of them
owed was composed for as long as the one pointer stood.
"""

from __future__ import annotations

from typing import Any

import pytest

from reckon._worker_fence import BackendError
from reckon.crew import recovery_watch

PROJECT = "proj"


def _pointer(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": "coordinator-a",
        "phase": "working",
        "backend": "harness-b",
        "launch": "cli",
        "node": {"id": run_id, "role": "implement"},
    }


@pytest.fixture()
def two_runs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[dict[str, Any]]:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    pointers = [_pointer("r-foreign-stream"), _pointer("r-readable")]
    monkeypatch.setattr(recovery_watch, "list_live", lambda: list(pointers))

    def observe(run_id: str, *, config: Any = None) -> dict[str, Any]:
        if run_id == "r-foreign-stream":
            raise BackendError("the dialect refused a stream in another vocabulary")
        return next(p for p in pointers if p["run_id"] == run_id)

    import sys

    import reckon.crew.dispatch  # noqa: F401 - the module, not the re-exported function

    monkeypatch.setattr(sys.modules["reckon.crew.dispatch"], "observe", observe)
    return pointers


def test_the_sweep_classifies_every_run_beside_the_refused_one(two_runs) -> None:
    report = recovery_watch.recover(project=PROJECT)

    listed = {str(item.get("run_id")) for item in report["runs"]}
    assert listed == {"r-foreign-stream", "r-readable"}, report


def test_the_refused_run_names_its_stream_fault(two_runs) -> None:
    report = recovery_watch.recover(project=PROJECT)

    refused = next(
        item for item in report["runs"] if item.get("run_id") == "r-foreign-stream"
    )
    assert "stream unreadable" in str(refused.get("detail")), refused
    assert "another vocabulary" in str(refused.get("detail")), refused


def test_a_refused_stream_has_no_completion_stamp(monkeypatch) -> None:
    """The classification's own stream read does not raise the refusal either.

    Classifying a run dates its finish from its stream. For a pointer that
    names one harness over a stream another wrote, that read is refused, and it
    is reached after the observation above: the sweep went on failing for the
    whole project until this read answered with no stamp.
    """
    from reckon.crew import promotion, recovery_stream

    def refuse(record):
        raise BackendError("the dialect refused a stream in another vocabulary")

    monkeypatch.setattr(promotion, "_terminal_stream_data", refuse)
    record = {"run_id": "r-foreign-stream", "launch": "cli", "backend": "harness-b"}

    assert recovery_stream._stream_completion_stamp(record) is None
    # Asked again, the stream is read again: a refusal is not remembered.
    assert recovery_stream._stream_completion_stamp(record) is None
