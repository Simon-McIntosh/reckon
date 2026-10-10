"""The estimate stage times the context estimate, and a cached view is not shared.

Two defects of the estimate and budget view are repaired here. ``estimate_ms``
read 0 on every production pick: the stage timed a trivial ``math.ceil(len(rendered) / 4)``
division while the context estimate that costs time -- ``estimated_context_tokens``
-- ran inside ``candidates()``, counted only within ``snapshot_ms``, and again,
untimed, in ``dispatch_picker_selection`` before ``pick()`` began. A test that
injects a 50 ms estimator proves the pick records an ``estimate_ms`` of at least
50; the pre-change code fails it because it timed only the division.

The second: ``budget_view`` hands a fresh-cache reader the same report object the
cache stored when the clock has not advanced, so a caller that mutates the view
it was given rewrites the composition and every later read serves the mutation.
These tests pin the timing, the reuse of a pre-pick figure and its duration, and
the isolation of a cached view from its reader.
"""

from __future__ import annotations

import importlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew.node import TaskNode

picker = importlib.import_module("reckon.crew.picker")
snapshot = picker.snapshot
dispatch = importlib.import_module("reckon.crew.dispatch")


def _node(**overrides) -> TaskNode:
    values = {
        "id": "parser",
        "goal": "Implement a parser",
        "plan": "",
        "role": "implement",
        "spec_level": "guided",
        "done_when": "Parser tests pass",
    }
    values.update(overrides)
    return TaskNode(**values)


def _config() -> dict:
    return {
        "default_backend": "local",
        "local_backend": "local",
        "roles": {"implement": {}},
        "backends": {
            "local": {
                "launch": "cli",
                "command": "clive",
                "model": "local-model",
                "usable_input_window": 1_000_000,
            }
        },
    }


def _answer() -> dict:
    return {
        "model": "jev-snapshot",
        "answers": {
            "route": {
                "choice": "local:local-model:local",
                "confidence": 0.9,
                "probabilities": {"local:local-model:local": 0.9, "hold": 0.1},
            }
        },
    }


def _isolate_candidate_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the candidate scan's live reads, leaving the estimate real."""

    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot.routing,
        "_competence_verdict",
        lambda **k: {
            "allowed": True,
            "reason": None,
            "context": {"allowed": True, "window_tokens": 1_000_000},
        },
    )
    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", lambda **k: None)


def _stub_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def render(name: str, **context: object) -> str:
        if name == "state.jinja":
            return json.dumps({"node": {}, "candidates": []})
        return json.dumps({"route": {"criteria": []}})

    monkeypatch.setattr(picker.prompts, "render", render)


def _lines(home: Path) -> list[dict]:
    path = home / "crew" / picker.PICK_TIMINGS_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _pick(home: Path, monkeypatch: pytest.MonkeyPatch, request, **kwargs):
    monkeypatch.setenv("RECKON_HOME", str(home))
    _stub_render(monkeypatch)
    return picker.pick(
        request,
        _config(),
        repo=home,
        records=[],
        budget_snapshot={"backends": [], "groups": []},
        verdict_inputs={},
        caller=lambda *a, **k: _answer(),
        **kwargs,
    )


def test_estimate_ms_times_the_context_estimate(tmp_path, monkeypatch):
    """A 50 ms estimator makes the pick record an estimate_ms of at least 50."""

    home = tmp_path / "home"
    _isolate_candidate_scan(monkeypatch)
    monkeypatch.setattr(
        snapshot,
        "estimated_context_tokens",
        lambda node, repo, **k: (time.sleep(0.05), 50_000)[1],
    )

    # No figure on the request, so the pick must measure the estimate itself.
    _pick(home, monkeypatch, picker.PickRequest("proj", _node(), estimated_context=0))

    line = _lines(home)[-1]
    assert line["estimate_ms"] >= 50, line["estimate_ms"]


def test_a_pre_pick_estimate_is_reused_and_its_duration_recorded(tmp_path, monkeypatch):
    """A figure handed in before the pick is reused, and its duration recorded.

    Dispatch measures the estimate itself, before the pick's own bound, so the
    figure reaches the pick already known. The scan must not measure it a second
    time, and the stage must record the pre-pick duration rather than a second
    measurement's.
    """

    home = tmp_path / "home"
    _isolate_candidate_scan(monkeypatch)
    calls: list[int] = []

    def estimator(node, repo, **k):
        calls.append(1)
        return 12_345

    monkeypatch.setattr(snapshot, "estimated_context_tokens", estimator)

    _pick(
        home,
        monkeypatch,
        picker.PickRequest("proj", _node(), estimated_context=50_000),
        estimated_context_ms=42.5,
    )

    line = _lines(home)[-1]
    assert line["estimate_ms"] == 42.5, line["estimate_ms"]
    assert calls == [], "the pre-pick figure was measured a second time"


def test_dispatch_times_the_estimate_it_hands_the_pick(monkeypatch, tmp_path):
    """The production dispatch path passes the figure and its own duration."""

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "target.py").write_text("x" * 2000)
    seen: dict[str, object] = {}

    def fake_pick(request, _config, *, repo, cached_only, **kwargs):
        seen["estimated_context"] = request.estimated_context
        seen["estimated_context_ms"] = kwargs.get("estimated_context_ms")
        return SimpleNamespace(as_dict=lambda: {"backend": "local"})

    monkeypatch.setattr(picker, "pick", fake_pick)
    monkeypatch.setattr(
        snapshot, "estimated_context_tokens", lambda *a, **k: 9000
    )

    dispatch.dispatch_picker_selection(
        node=_node(write_paths=["src/target.py"]),
        config=_config(),
        project="proj",
        repo=repo,
    )

    assert seen["estimated_context"] == 9000
    assert seen["estimated_context_ms"] is not None
    assert seen["estimated_context_ms"] >= 0


def test_a_reused_view_is_not_shared_with_its_reader():
    """A zero-delta read must not hand back the object the cache holds.

    A view reused at the moment it was built returns ``value["report"]`` by
    reference unless the zero-delta path copies it, so a caller that mutates the
    view it was given rewrites the cached composition and the next read of the
    same value serves the mutation. The check mutates the first read and asserts
    the second read still carries the built figure.
    """

    moment = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    value = {
        "built_at": stamp,
        "report": {"checked_at": stamp, "backends": [], "groups": [], "summary": {}},
    }

    first = snapshot._reage_budget_report(value, moment, {})
    first["checked_at"] = "MUTATED"

    second = snapshot._reage_budget_report(value, moment, {})
    assert second["checked_at"] == stamp, "a cached view was shared with its reader"


def test_a_cached_view_equals_the_uncached_composition(tmp_path, monkeypatch):
    """A fresh-cache read and an uncached read agree on every figure."""

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("RECKON_PICK_CACHE", str(cache_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    config = {
        "backends": {"local": {"model": "m", "launch": "cli", "lane_document": None}},
        "local_backend": "local",
    }
    moment = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)

    cached = snapshot.budget_view(
        "sample", config, repo, [], cached_only=True, now=moment, cache_root=cache_root
    )
    uncached = snapshot.budget_view(
        "sample",
        config,
        repo,
        [],
        cached_only=True,
        now=moment,
        cache_root=cache_root / "other",
    )
    assert cached == uncached
