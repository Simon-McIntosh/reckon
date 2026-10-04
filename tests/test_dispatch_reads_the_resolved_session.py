"""Session decisions read the run's stream when its pointer has not caught up."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import promotion
from reckon.crew.runs import capture_run_session, run_dir

dispatch = import_module("reckon.crew.dispatch")

THREAD_ID = "01a0635f-62a3-7283-a81b-61cd39bedb60"


@pytest.fixture
def run_with_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    run_id = "r-20261004T000000000000-node-a"
    stream = run_dir(run_id) / "stream.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        f'{{"type":"thread.started","thread_id":"{THREAD_ID}"}}\n',
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "project": "proj",
        "repo": str(tmp_path / "repo"),
        "node": {"id": "node-a", "plan": "plan-a"},
        "role": "implement",
        "launch": "cli",
        "backend": "codex",
        "log_path": str(stream),
        "session_id": None,
        "session_harness": "codex",
    }


def test_same_task_dispatch_reuses_session_named_only_by_prior_stream(
    run_with_stream: dict,
) -> None:
    node = SimpleNamespace(id="node-a", plan="plan-a", role="implement")

    answer = dispatch._task_session_resolution(
        node, "proj", live_pointers=[run_with_stream], harness="codex"
    )

    assert answer == {"session_id": THREAD_ID, "withheld": None}


def test_capture_run_session_reads_stream_when_pointer_lags(
    run_with_stream: dict,
) -> None:
    answer = capture_run_session(run_with_stream)

    assert answer is not None
    assert answer["session_id"] == THREAD_ID
    assert run_with_stream["session_harness"] == "codex"


def test_promotion_guard_reads_the_same_resolution(run_with_stream: dict) -> None:
    answer = promotion._recoverable_session(run_with_stream)

    assert answer == {"session_id": THREAD_ID, "source": "stream"}
