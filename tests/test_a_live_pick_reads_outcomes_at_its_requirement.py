"""A live pick reads its candidates' history at the capability it resolved.

``pick()`` built its candidates before it resolved the node's declared
``capability``, so a live dispatch -- whose ``PickRequest`` carries no capability
-- handed the scan a requirement-less request and the per-candidate
``outcomes_at_requirement`` block was inert: only a replay, which carries the
capability on its request, ever showed it. The pick now resolves the declared
capability first and hands the scan a request that carries it, so the block
filters on the same capability the state renders as ``node.capability``. A
request that already carries one is kept. Both stages keep their own timing
keys, and a deadline that expires in either still names the step it was inside.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import routing
from reckon.crew.node import TaskNode
from reckon.crew.picker import prompts, snapshot

picker = importlib.import_module("reckon.crew.picker")

#: A minimal live plan whose section s2 declares reasoning deep and risk
#: critical -- the capability a dispatch for a deep, critical node resolves.
PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    '<meta name="docs-project" content="reckon">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="jev-learns-from-its-routes">\n'
    "</head><body>\n"
    '<h2 id="s2">&sect;2 &mdash; A deep, critical section</h2>\n'
    '<section data-reckon="section" data-id="s2" data-effort-hours="2"'
    ' data-status="implementable"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="deep" data-capability-verification="strict"'
    ' data-capability-risk="critical"></section>\n'
    "</body></html>\n"
)

CONFIG: dict[str, Any] = {
    "default_backend": "laneb",
    "local_backend": "laneb",
    "roles": {"implement": {}, "review": {}},
    "backends": {
        "lanea": {"model": "m-a"},
        "laneb": {"model": "m-b"},
    },
}

#: The requirement the resolved plan section carries, in the capability schema's
#: shape, and the one a request states when it already carries a capability.
_DEEP_CRITICAL = {"reasoning": "deep", "risk": "critical"}
_STANDARD = {"reasoning": "standard", "risk": "low"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    plans = tmp_path / "repo" / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "jev-learns-from-its-routes.html").write_text(PLAN_HTML)
    return tmp_path / "repo"


@pytest.fixture
def picker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the real candidate scan from the ledger, repo and network."""

    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *a, **k: 0)
    monkeypatch.setattr(snapshot, "_fit", lambda request, name, backend, repo, **k: [])
    monkeypatch.setattr(
        snapshot,
        "_serving_observation",
        lambda backend: {"status": "served", "detail": ""},
    )
    monkeypatch.setattr(
        routing, "_estimated_hours", lambda *a, **k: (None, "unavailable")
    )
    return None


def _request(
    capability: dict[str, Any] | None = None,
    plan: str = "jev-learns-from-its-routes",
) -> Any:
    return picker.PickRequest(
        project="reckon",
        node=TaskNode(
            id="live-pick-node",
            goal="read outcomes at the requirement a live pick resolves",
            plan=plan,
            section="s2",
            role="implement",
            spec_level="exact",
            done_when="the block reads the matched rows",
            write_paths=[],
        ),
        session="s",
        capability=capability or {},
    )


def _row(
    *,
    backend: str,
    model: str,
    gate: str,
    requirement: dict[str, str] | None,
    days_ago: float = 1.0,
    review_total: float | None = None,
    wall_seconds: float | None = None,
) -> dict[str, Any]:
    stamp = datetime.now(UTC) - timedelta(days=days_ago)
    row: dict[str, Any] = {
        "backend": backend,
        "agent": {"model": model},
        "gate": gate,
        "role": "implement",
        "spec_level": "exact",
        "completed_at": stamp.isoformat(),
    }
    if requirement is not None:
        row["node_definition"] = {"capability": {"requirements": dict(requirement)}}
    if review_total is not None:
        row["review"] = {"total": review_total}
    if wall_seconds is not None:
        row["wall_seconds"] = wall_seconds
    return row


def _records() -> list[dict[str, Any]]:
    """Two lanes: A's wins are at the wrong difficulty, B's are at the right one.

    Lane A passes four standard rows and fails two deep-critical rows; lane B
    fails one standard row and passes three deep-critical rows. Pooled, A looks
    strong while its record at the requirement is two failures, and B the
    reverse.
    """

    rows: list[dict[str, Any]] = []
    rows.extend(
        _row(backend="lanea", model="m-a", gate="passed", requirement=_STANDARD)
        for _ in range(4)
    )
    rows.extend(
        _row(backend="lanea", model="m-a", gate="failed", requirement=_DEEP_CRITICAL)
        for _ in range(2)
    )
    rows.append(
        _row(backend="laneb", model="m-b", gate="failed", requirement=_STANDARD)
    )
    for review, wall in ((88.0, 100.0), (92.0, 200.0), (80.0, 300.0)):
        rows.append(
            _row(
                backend="laneb",
                model="m-b",
                gate="passed",
                requirement=_DEEP_CRITICAL,
                review_total=review,
                wall_seconds=wall,
            )
        )
    return rows


def _answer_for(candidate: Any) -> dict[str, Any]:
    key = prompts.option_key(candidate)
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


def _drive_pick(
    repo: Path,
    request: Any,
    *,
    records: list[dict[str, Any]] | None = None,
    running: dict[str, Any] | None = None,
) -> Any:
    """Run a real pick; only the Jev answer is stubbed.

    The real candidate scan runs, so the block the pick builds is the block it
    would build live; only the answer comes back without a model.
    """

    scan_seen: list[list[Any]] = []

    def snapshotter(*args: Any, **kwargs: Any) -> list[Any]:
        candidates = snapshot.candidates(*args, **kwargs)
        scan_seen.append(candidates)
        return candidates

    def caller(state: Any, questions: Any, **kwargs: Any) -> dict[str, Any]:
        offered = [c for c in scan_seen[-1] if not c.reasons]
        return _answer_for(offered[0])

    return picker.pick(
        request,
        CONFIG,
        repo=repo,
        records=records if records is not None else _records(),
        budget_snapshot={"backends": [], "groups": []},
        snapshotter=snapshotter,
        caller=caller,
        running=running,
    )


def _state(selection: Any) -> dict[str, Any]:
    return json.loads(selection.rendered_state)


def _candidate_row(candidates: dict[str, Any], backend: str) -> dict[str, Any]:
    """A rendered candidate row, found under whatever option key it holds."""

    return next(row for row in candidates.values() if row["backend"] == backend)


def test_a_live_pick_reads_outcomes_at_the_capability_it_resolved(
    repo: Path, picker_env
) -> None:
    """The request carries no capability; the section's deep, critical
    requirement reaches the scan, so each candidate's block reads the matched
    rows while its pooled record keeps every row."""

    selection = _drive_pick(repo, _request())
    candidates = _state(selection)["candidates"]

    a_block = _candidate_row(candidates, "lanea")["outcomes_at_requirement"]
    assert a_block["passed"] == 0
    assert a_block["failed"] == 2
    # Lane A's matched rows recorded no review total or wall time.
    assert a_block["review_total_median"] is None
    assert a_block["wall_seconds_median"] is None

    b_block = _candidate_row(candidates, "laneb")["outcomes_at_requirement"]
    assert b_block["passed"] == 3
    assert b_block["failed"] == 0
    assert b_block["review_total_median"] == 88.0
    assert b_block["wall_seconds_median"] == 200.0

    # The pooled record keeps every row, whatever difficulty it recorded.
    assert _candidate_row(candidates, "lanea")["outcomes"]["passed"] == 4
    assert _candidate_row(candidates, "lanea")["outcomes"]["failed"] == 2


def test_a_request_that_carries_a_capability_keeps_it(repo: Path, picker_env) -> None:
    """A request that already states a capability is not re-resolved, and the
    scan filters on the capability it carries."""

    selection = _drive_pick(
        repo, _request(capability={"requirements": dict(_STANDARD)})
    )
    state = _state(selection)
    assert state["node"]["capability"]["requirements"]["reasoning"] == "standard"
    assert (
        _candidate_row(state["candidates"], "lanea")["outcomes_at_requirement"][
            "passed"
        ]
        == 4
    )


def test_a_node_with_no_declared_capability_shows_no_block(
    repo: Path, picker_env
) -> None:
    """A node that names no plan resolves no capability, so no block is built."""

    selection = _drive_pick(repo, _request(plan=""))
    candidates = _state(selection)["candidates"]
    # The template drops a null block, so the absence of the key is the block
    # being absent.
    assert _candidate_row(candidates, "lanea").get("outcomes_at_requirement") is None


def test_both_stages_are_timed_and_name_their_step(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability resolve and the candidate scan keep their own timing keys,
    and a deadline that expires in either names the step it was inside."""

    steps: dict[str, str | None] = {}
    running: dict[str, Any] = {}

    def capability(*_args: Any, **_kwargs: Any) -> tuple[Any, Any, Any]:
        steps["capability"] = running.get("step")
        return ({"requirements": dict(_DEEP_CRITICAL)}, None, None)

    candidate = picker.Candidate(
        backend="laneb",
        family="laneb",
        model="m-b",
        effort=None,
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

    def snapshotter(*_args: Any, **_kwargs: Any) -> list[Any]:
        steps["snapshot"] = running.get("step")
        return [candidate]

    def caller(state: Any, questions: Any, **kwargs: Any) -> dict[str, Any]:
        return _answer_for(candidate)

    monkeypatch.setattr(picker, "_declared_difficulty", capability)
    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))

    picker.pick(
        _request(),
        CONFIG,
        repo=repo,
        records=_records(),
        budget_snapshot={"backends": [], "groups": []},
        snapshotter=snapshotter,
        caller=caller,
        running=running,
    )

    assert steps["capability"] == "capability"
    assert steps["snapshot"] == "snapshot"
    log = (home / "crew" / picker.PICK_TIMINGS_LOG).read_text().splitlines()[0]
    line = json.loads(log)
    assert line["capability_ms"] is not None
    assert line["snapshot_ms"] is not None
