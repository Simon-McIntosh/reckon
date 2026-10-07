"""Every production pick records where its time went, in one JSON line.

A timed-out pick records only ``fallback_reason timeout``, so the stage that
spent the five-second dispatch budget cannot be attributed. ``pick`` therefore
appends its per-stage times under the crew home, best-effort, when it completes
-- including a pick that finishes after dispatch has already given up, whose
daemon thread goes on running. These tests pin the line's shape, the late-pick
completion, the never-fail write and the isolation of the log from the real
crew home.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from pathlib import Path

import pytest

from reckon.crew.node import TaskNode

picker = importlib.import_module("reckon.crew.picker")
dispatch = importlib.import_module("reckon.crew.dispatch")

STAGE_KEYS = (
    "snapshot_ms",
    "estimate_ms",
    "state_render_ms",
    "questions_render_ms",
    "jev_ms",
)


def _node() -> TaskNode:
    return TaskNode(
        id="parser",
        plan="one-typesafe-model-picker",
        role="implement",
        spec_level="guided",
        goal="Implement a parser",
        done_when="Parser tests pass",
        time_budget="60m",
    )


def _request(session: str = "s-1") -> picker.PickRequest:
    return picker.PickRequest(
        "example",
        _node(),
        capability={"class": "general"},
        estimated_context=1000,
        comment="keep the API stable",
        session=session,
    )


def _candidate(backend: str = "local") -> picker.Candidate:
    return picker.Candidate(
        backend=backend,
        family="local",
        model="local-model",
        effort="high",
        local=True,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={},
        reasons=[],
    )


def _answer(candidate: picker.Candidate | None = None) -> dict[str, object]:
    """Jev's reply offering the pair it means to choose, plus hold.

    The choice and the distribution are keyed by the lane-and-model pair the
    candidate is offered under, computed from the candidate itself: the picker
    rejects an answer by backend name, since that names no offered option.
    """

    key = picker.prompts.option_key(candidate or _candidate())
    return {
        "model": "jev-snapshot",
        "answers": {
            "route": {
                "choice": key,
                "confidence": 0.9,
                "probabilities": {key: 0.9, "hold": 0.1},
            }
        },
    }


def _snapshotter(candidates: list[picker.Candidate]) -> object:
    def snapshot(request, config, repo, **kwargs):
        return list(candidates)

    return snapshot


def _stub_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cheap render for both templates, so no lane read sits in the stage."""

    def render(name: str, **context: object) -> str:
        if name == "state.jinja":
            return json.dumps({"node": {}, "candidates": []})
        return json.dumps({"route": {"criteria": []}})

    monkeypatch.setattr(picker.prompts, "render", render)


def _log_path(home: Path) -> Path:
    return home / "crew" / picker.PICK_TIMINGS_LOG


def _lines(home: Path) -> list[dict[str, object]]:
    path = _log_path(home)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _pick(home: Path, monkeypatch, *, caller, snapshotter=None):
    monkeypatch.setenv("RECKON_HOME", str(home))
    _stub_render(monkeypatch)
    return picker.pick(
        _request(),
        {"default_backend": "local"},
        repo=home,
        records=[],
        budget_snapshot={},
        snapshotter=snapshotter or _snapshotter([_candidate()]),
        caller=caller,
    )


def test_a_pick_records_every_stage(tmp_path, monkeypatch):
    """A routed pick writes one line with each stage's milliseconds present."""
    home = tmp_path / "home"
    selection = _pick(home, monkeypatch, caller=lambda *a, **k: _answer())

    lines = _lines(home)
    assert len(lines) == 1
    line = lines[0]
    assert selection.action == "route" and line["outcome"] == "route"
    assert line["node"] == "parser"
    assert line["session"] == "s-1"
    assert isinstance(line["started_at"], str) and line["started_at"]
    assert line["latency_ms"] > 0
    for key in STAGE_KEYS:
        assert line[key] is not None, key
        assert line[key] >= 0, key
    assert line["within_bound"] is True


def test_a_pick_past_the_bound_records_when_it_completes(tmp_path, monkeypatch):
    """A pick slower than the bound still writes its line, marked past-bound.

    The pick runs on its own thread exactly as dispatch starts it: a waiter
    gives up at the bound while the pick is still inside its slow stage, and the
    pick records its line only once that stage returns.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(dispatch, "PICKER_DISPATCH_TIMEOUT_SECONDS", 0.05)

    def slow_caller(*args, **kwargs):
        time.sleep(0.25)
        return _answer()

    result: dict[str, object] = {}

    def run() -> None:
        result["selection"] = _pick(home, monkeypatch, caller=slow_caller)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(0.05)
    # Dispatch has given up by now, and the pick has written nothing yet.
    assert "selection" not in result
    assert _lines(home) == []

    worker.join(5)
    assert "selection" in result
    lines = _lines(home)
    assert len(lines) == 1
    line = lines[0]
    assert line["within_bound"] is False
    assert line["latency_ms"] > 50
    assert line["jev_ms"] >= 200


def test_an_unwritable_log_leaves_the_pick_unchanged(tmp_path, monkeypatch):
    """A log that cannot be written changes neither the result nor the call."""
    home = tmp_path / "home"
    home.mkdir()
    # A file where the crew directory must be: every append under it fails.
    (home / "crew").write_text("not a directory")

    selection = _pick(home, monkeypatch, caller=lambda *a, **k: _answer())
    assert selection.action == "route"
    assert selection.backend == "local"


def test_the_real_crew_home_is_untouched(tmp_path, monkeypatch):
    """The log resolves through RECKON_HOME, never a candidate real home."""
    fake_home = tmp_path / "fake-home"
    real_candidate = fake_home / ".config" / "reckon"
    real_candidate.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("RECKON_HOME", raising=False)

    home = tmp_path / "isolated"
    _pick(home, monkeypatch, caller=lambda *a, **k: _answer())

    assert _log_path(home).exists()
    assert len(_lines(home)) == 1
    assert not (real_candidate / "crew").exists()
