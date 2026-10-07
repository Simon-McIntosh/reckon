"""One owner per picker helper, and an unchanged rendered picker state.

Two design-review findings asked that duplicated picker helpers and the
run-time-profile cache have a single owner. The helpers live once in
:mod:`reckon.crew.run_time_profile` and are imported everywhere else; the
profile cache goes through :func:`reckon.capabilities.cached_pick_input`. One
owner must not mean one behaviour: merging three copies of ``_number`` also
merged their return types, and the picker state renders the float cast as
``300.0`` where the base rendered ``300``. So the rendered state is pinned
against the base revision's own bytes, not against a second render of the new
code -- a check that renders the code twice moves with the code it is meant to
hold still.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context, prompts
from reckon.crew.picker.types import Candidate

NOW = datetime(2026, 10, 2, 2, 1, 0, tzinfo=UTC)

#: The picker state rendered by the reviewed run's base revision (``4e2f6823c``)
#: for the fixture built below, committed verbatim as a test fixture. The head
#: must reproduce these bytes exactly, so a single owner whose ``_number``
#: changed an integer figure into ``300.0`` is caught rather than followed. The
#: bytes were produced by running the base tree's own code over the same frozen
#: fixture (ledger rows, profile-cache directory, lane document and live-worker
#: list all fixed), never transcribed by hand.
GOLDEN_STATE = r"""{"node":{"role":"implement","spec_level":"guided","capability":{},"goal":"g","done_when":"d","estimated_context":0,"estimated_hours":null,"attempts":0,"write_path_count":0,"negative_control_declared":false},"orchestrator_comment":"one owner","candidates":{"clive":{"backend":"clive","lane":"f","model":"m","availability":"served","utilisation_pct":null,"burn_multiple":null,"pace_allowance":null,"days_to_reset":null,"resets_at":null,"worker_slots":null,"congestion":null,"outcomes":{"passed":0,"failed":0,"not-run":0,"unknown":0},"context":null,"budget_source":null,"budget_age_s":null,"stale":null,"reset_available":null},"amine":{"backend":"amine","lane":"f","model":"m","availability":"served","utilisation_pct":null,"burn_multiple":null,"pace_allowance":null,"days_to_reset":null,"resets_at":null,"worker_slots":null,"congestion":null,"outcomes":{"passed":0,"failed":0,"not-run":0,"unknown":0},"context":null,"budget_source":null,"budget_age_s":null,"stale":null,"reset_available":null}},"return_times":{"clive":{"p50_s":300,"p90_s":500,"runs":3,"size_key":"time_budget","size_bucket":"30m_to_60m","budget_source":null,"budget_age_s":null,"stale":false},"amine":{"p50_s":null,"p90_s":null,"runs":null,"size_key":"time_budget","size_bucket":"30m_to_60m","budget_source":null,"budget_age_s":null,"stale":false}},"local_lane":{"admission":"admitting","expected_wait_s":null,"running":2,"waiting":0,"headroom":14,"worker_slots":53,"tokens_per_second":89.425,"read_at":"2026-10-02T02:01:00+00:00"}}"""

#: Helper names that must not be re-defined inside the picker package, each
#: mapped to the one function in run_time_profile that owns its behaviour.
OWNER_OF = {
    "_bucket": "_size_class",
    "_number": "_number",
    "_percentile": "_percentile",
}

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PICKER_DIR = _REPO_ROOT / "reckon" / "crew" / "picker"
_RUN_TIME_PROFILE = _REPO_ROOT / "reckon" / "crew" / "run_time_profile.py"

#: The lane document the fixture freezes, so the local-lane block is identical
#: on every run rather than reading whatever the serving lane last published.
_LANE_DOCUMENT = {
    "state": "measured",
    "running": 2,
    "waiting": 0,
    "headroom": 14,
    "concurrent_requests": 6,
    "mean_context": 12345,
    "observed_at": NOW.isoformat(),
    "suggested_shelf_life_seconds": 300,
    "router_generation_gate": {"width": 16, "in_flight": 2, "waiting": 0},
}

_FIXED_LOAD = {
    "read_at": NOW.isoformat(),
    "document": _LANE_DOCUMENT,
    "observed_at": NOW.isoformat(),
    "state": "measured",
    "running": 2,
    "waiting": 0,
    "headroom": 14,
    "worker_slots": 53,
    "mean_tokens_per_second": 89.425,
    "detail": "",
}


def _defined_names(path: Path) -> list[str]:
    """Every top-level or nested function name defined in one module."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_the_owned_helpers_live_once_in_run_time_profile():
    """Positive control: each behaviour has one owner where it is meant to live.

    Without this, a scan that finds nothing would read the same whether the
    helpers were merged or simply renamed away entirely.
    """

    names = _defined_names(_RUN_TIME_PROFILE)
    for owned, owner in OWNER_OF.items():
        assert names.count(owner) == 1, (
            f"the owner {owner} of {owned} is not defined once in {_RUN_TIME_PROFILE}"
        )


def test_no_second_definition_of_an_owned_helper_under_picker():
    """A re-added local copy of an owned helper is a second owner, and fails."""

    offenders: dict[str, list[str]] = {}
    for module in sorted(_PICKER_DIR.rglob("*.py")):
        defined = set(_defined_names(module)) & set(OWNER_OF)
        if defined:
            offenders[str(module.relative_to(_REPO_ROOT))] = sorted(defined)
    assert offenders == {}, f"picker package re-defines an owned helper: {offenders}"


def _rows(backend, walls, effort="high", role="implement", spec_level="guided"):
    return [
        {
            "backend": backend,
            "agent": {"effort": effort, "model": "m"},
            "role": role,
            "spec_level": spec_level,
            "wall_seconds": wall,
            "gate": "passed",
            "completed_at": (NOW - timedelta(days=1)).isoformat(),
        }
        for wall in walls
    ]


def _node(time_budget=""):
    return TaskNode(
        id="n",
        goal="g",
        plan="",
        role="implement",
        spec_level="guided",
        done_when="d",
        time_budget=time_budget,
    )


def _candidate(backend):
    return Candidate(
        backend=backend,
        family="f",
        model="m",
        effort="high",
        local=False,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    )


def _freeze(tmp_path, monkeypatch):
    """Fix everything the render reads, so only the code under test can vary."""

    rows_path = tmp_path / "ledger-reckon.json"
    rows_path.write_text(
        json.dumps({"completed": _rows("clive", [100, 300, 500])}), encoding="utf-8"
    )
    lane_path = tmp_path / "lane.json"
    lane_path.write_text(json.dumps(_LANE_DOCUMENT), encoding="utf-8")
    monkeypatch.setattr(
        "reckon.crew.run_time_profile.ledger.runs",
        lambda *a, **k: json.loads(rows_path.read_text(encoding="utf-8"))["completed"],
    )
    monkeypatch.setenv("RECKON_RUN_TIME_PROFILE_CACHE", str(tmp_path / "profile-cache"))
    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _project: ["ledger", 1])
    lane_context._PROFILE_CACHE.clear()
    monkeypatch.setattr(lane_context, "list_live", list)
    monkeypatch.setattr(lane_context, "local_lane_load", lambda: _FIXED_LOAD)
    # The base's reader resolves the lane document through a path; the head
    # reuses the one local_lane_load returned. Freeze both seams to one document.
    monkeypatch.setattr(
        lane_context, "local_lane_path", lambda: lane_path, raising=False
    )


def _render(tmp_path, monkeypatch):
    _freeze(tmp_path, monkeypatch)
    return prompts.render(
        "state.jinja",
        node=_node(time_budget="45m"),
        capability={},
        estimated_context=0,
        comment="one owner",
        candidates=[_candidate("clive"), _candidate("amine")],
        project="reckon",
        records=None,
        attempts=0,
        now=NOW,
    )


def test_rendered_state_matches_the_base_revision_bytes(tmp_path, monkeypatch):
    """The head renders exactly the bytes the base revision rendered.

    The golden bytes were produced by the base tree's own code over this same
    frozen fixture, so the comparison is a real before/after check: it fails if
    any picker figure changes -- here, an integer wall-second figure rendering
    as a float -- rather than only if the new code disagrees with itself.
    """

    rendered = _render(tmp_path, monkeypatch)
    assert rendered == GOLDEN_STATE, (
        "the rendered picker state changed from the base revision's bytes"
    )
    payload = json.loads(rendered)
    block = payload["return_times"]["clive"]
    # The figures the base rendered as integers stay integers.
    assert block["p50_s"] == 300 and isinstance(block["p50_s"], int)
    assert block["p90_s"] == 500 and isinstance(block["p90_s"], int)
    assert block["runs"] == 3
    assert block["size_bucket"] == "30m_to_60m"


def test_an_unsafe_project_name_never_forms_a_cache_path(tmp_path, monkeypatch):
    """A project id that is not a safe file name is never cached.

    A guard must not be dropped when the reader and writer that carried it are
    deleted: the profile cache file name is built from the project string, and an
    unsafe string must never form a path. The profile is then read without being
    cached, exactly as before.
    """

    calls: list[str] = []

    def profile(project, **kwargs):
        calls.append(project)
        return {"groups": []}

    cache = tmp_path / "profile-cache"
    monkeypatch.setenv("RECKON_RUN_TIME_PROFILE_CACHE", str(cache))
    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _project: ["ledger", 1])
    monkeypatch.setattr(lane_context, "run_time_profile", profile)
    lane_context._PROFILE_CACHE.clear()

    for _ in range(2):
        assert lane_context._cached_run_time_profile("../escape", now=NOW) == {
            "groups": []
        }

    assert calls == ["../escape", "../escape"], "an unsafe name was cached"
    assert not list(cache.rglob("*")), "an unsafe name formed a cache path"
