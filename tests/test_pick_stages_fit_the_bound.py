"""A pick resolves the plan a node names once, and the resolution is unchanged.

The capability stage resolved the plan file a node names through a docs-tree
scan on every pick, while the estimate stage had already resolved the same plan
for the same node. The scan is a pure function of the files it reads, so the
capability stage now takes it through the pick-input cache: a warm entry serves
the file the scan would have found without walking the tree.

These tests pin both halves of that cut. The selection a pick reaches is
identical whether the resolution is served from the cache or paid for again per
pick, and the cache is what removes the repeated scan. A later test guards the
cache's correctness, since a resolution stored under a file stamp that never
moves would serve a stale plan.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from reckon import resources
from reckon.crew.node import TaskNode

picker = importlib.import_module("reckon.crew.picker")

#: A minimal plan whose section declares a capability and its own effort, so a
#: pick that reads it reaches a capability and an estimate it would not have.
PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    '<meta name="docs-project" content="proj">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="probe">\n'
    '<meta name="plan-effort-hours" content="48">\n'
    "</head><body>\n"
    '<h2 id="s2">&sect;2 &mdash; A deep section</h2>\n'
    '<section data-reckon="section" data-id="s2" data-effort-hours="10"'
    ' data-status="implementable"'
    ' data-capability-version="1.0" data-capability-class="orchestrator"'
    ' data-capability-reasoning="deep" data-capability-verification="strict"'
    ' data-capability-risk="critical"></section>\n'
    "</body></html>\n"
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the run home and the pick-input cache at the test's temp tree.

    The pick writes its timing line under the crew home and the cut stores its
    resolution under the pick-input cache; both are redirected so no test
    reads or writes the operator's live state.
    """

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "pick-cache"))
    return tmp_path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    plans = tmp_path / "repo" / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "probe.html").write_text(PLAN_HTML)
    return tmp_path / "repo"


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
                "effort": "high",
            }
        },
    }


def _node() -> TaskNode:
    return TaskNode(
        id="work",
        goal="Implement the parser",
        plan="probe",
        section="s2",
        role="implement",
        spec_level="guided",
        done_when="Parser tests pass",
        write_paths=["src/parser.py"],
        estimated_hours=None,
    )


def _candidate() -> picker.Candidate:
    return picker.Candidate(
        backend="local",
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


def _answer(candidate: picker.Candidate) -> dict:
    key = picker.prompts.option_key(candidate)
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


def _run_pick(repo: Path):
    """Run one pick over the recorded input, returning (selection, jev state).

    The state handed to Jev and the selection returned are the two observables
    the cut must leave untouched. A caller that wants the read the cut removed
    restored monkeypatches ``picker._resolved_plan_path`` to return None, which
    sends the capability stage back through the per-pick docs-tree scan.
    """

    seen: list[dict] = []

    def caller(state, questions, **kwargs):
        seen.append(state)
        return _answer(_candidate())

    request = picker.PickRequest("proj", _node(), estimated_context=1000)
    selection = picker.pick(
        request,
        _config(),
        repo=repo,
        records=[],
        budget_snapshot={},
        snapshotter=lambda *a, **k: [_candidate()],
        caller=caller,
    )
    assert seen, "the picker handed Jev no state"
    return selection, seen[0]


def test_the_cached_resolution_keeps_the_selection_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut changes timing only: the selection and Jev's state are identical."""

    cached_selection, cached_state = _run_pick(repo)

    # Restore the read the cut removed for the second arm.
    monkeypatch.setattr(picker, "_resolved_plan_path", lambda *a, **k: None)
    restored_selection, restored_state = _run_pick(repo)

    assert cached_state["node"]["capability"] == restored_state["node"]["capability"]
    assert cached_state["node"]["capability"]["class"] == "orchestrator"
    assert (
        cached_state["node"]["estimated_hours"]
        == restored_state["node"]["estimated_hours"]
        == 10.0
    )
    assert (
        cached_state["node"]["estimated_hours_source"]
        == restored_state["node"]["estimated_hours_source"]
        == "section"
    )
    assert (
        cached_selection.action,
        cached_selection.backend,
        cached_selection.model,
    ) == (
        restored_selection.action,
        restored_selection.backend,
        restored_selection.model,
    )


def test_the_cut_stops_the_repeated_docs_scan(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second pick in the same process resolves the plan without a rescan."""

    calls = {"n": 0}
    real = resources.resolve_resource

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(resources, "resolve_resource", counting)

    _run_pick(repo)  # first pick fills the cache
    assert calls["n"] >= 1, "the first pick resolved the plan"

    calls["n"] = 0
    _run_pick(repo)  # served from the cache
    assert calls["n"] == 0, "a warm cache serves the plan without a rescan"


def test_the_removed_read_is_what_the_cut_eliminated(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the per-pick read restored, the plan is resolved on every pick."""

    calls = {"n": 0}
    real = resources.resolve_resource

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(resources, "resolve_resource", counting)
    monkeypatch.setattr(picker, "_resolved_plan_path", lambda *a, **k: None)

    _run_pick(repo)
    _run_pick(repo)
    assert calls["n"] >= 2, "the restored read is paid on each pick"


def test_a_moved_plan_file_is_not_served_stale(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached resolution is stamped by the files it read, so an edit misses."""

    _run_pick(repo)
    plan = repo / "docs" / "plans" / "probe.html"
    plan.write_text(
        PLAN_HTML.replace('data-effort-hours="10"', 'data-effort-hours="7"')
    )

    _, state = _run_pick(repo)
    assert state["node"]["estimated_hours"] == 7.0
    assert state["node"]["estimated_hours_source"] == "section"


def test_a_renamed_plan_file_is_not_served_stale(repo: Path) -> None:
    """A resolution whose file moved is re-resolved, not served from the cache.

    The in-place rewrite above cannot show that the stamp moved: the resolved
    path is unchanged, so the hours are re-read from the same file whether or
    not the stamp is checked. A rename is the case the stamp exists for — the
    cached resolution names a file that no longer holds the plan, so a cache
    that ignored its stamp would serve that dead path and the section's own
    effort would no longer be found.
    """

    _run_pick(repo)
    plans = repo / "docs" / "plans"
    (plans / "probe.html").rename(plans / "relocated-probe.html")

    _, state = _run_pick(repo)
    assert state["node"]["estimated_hours"] == 10.0
    assert state["node"]["estimated_hours_source"] == "section"
