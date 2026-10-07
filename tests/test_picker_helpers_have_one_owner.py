"""One owner per picker helper, and an unchanged rendered picker state.

Two design-review findings asked that duplicated picker helpers and the
run-time-profile cache have a single owner. The helpers live once in
:mod:`reckon.crew.run_time_profile` and are imported everywhere else; the
profile cache goes through :func:`reckon.capabilities.cached_pick_input`. These
tests pin both, and pin that the refactor changed no rendered byte.
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


def _render():
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


def test_rendered_state_is_byte_identical_cold_and_warm(tmp_path, monkeypatch):
    """The persisted profile yields exactly the bytes the computed profile did.

    The state is rendered twice from one fixture. The first render is a cache
    miss: the profile is computed from the ledger and persisted. The second
    clears the in-process memo, so it can only come from the persisted copy
    through ``cached_pick_input``. The ledger loader must run once, and the two
    renders must be byte-identical -- the cached and the computed profile cannot
    disagree.
    """

    # A ledger whose rows the profile groups: same backend, two wall times.
    rows_path = tmp_path / "ledger-reckon.json"
    rows_path.write_text(
        json.dumps({"completed": _rows("clive", [100, 300])}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "reckon.crew.run_time_profile.ledger.runs",
        lambda *a, **k: json.loads(rows_path.read_text(encoding="utf-8"))["completed"],
    )
    # Isolate the persisted profile and hold the stamp fixed, so the two renders
    # differ only in whether the profile came from the cache.
    monkeypatch.setenv("RECKON_RUN_TIME_PROFILE_CACHE", str(tmp_path / "profile-cache"))
    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _project: ["ledger", 1])
    lane_context._PROFILE_CACHE.clear()

    # No live local workers, so the expected-wait figure is deterministic.
    monkeypatch.setattr(lane_context, "list_live", list)

    # The lane document is read for real, but its read stamp is frozen: the
    # render embeds it, and an unfrozen stamp would differ between the renders.
    real_load = lane_context.local_lane_load

    def frozen_load():
        load = dict(real_load())
        load["read_at"] = NOW.isoformat()
        return load

    monkeypatch.setattr(lane_context, "local_lane_load", frozen_load)

    calls: list[str] = []
    real_profile = lane_context.run_time_profile

    def counting_profile(project, **kwargs):
        calls.append(project)
        return real_profile(project, **kwargs)

    monkeypatch.setattr(lane_context, "run_time_profile", counting_profile)

    first = _render()
    assert calls == ["reckon"], "the cold render must read the ledger once"

    # Drop the in-process memo, so the warm render can only use the persisted
    # profile: the second read must not touch the ledger.
    lane_context._PROFILE_CACHE.clear()
    second = _render()

    assert calls == ["reckon"], "the warm render must not read the ledger again"
    assert first == second, "the cached profile changed the rendered bytes"

    payload = json.loads(first)
    block = payload["return_times"]["clive"]
    assert block["size_bucket"] == "30m_to_60m"
    assert block["p50_s"] == 200.0
    assert block["p90_s"] == 300.0
    assert block["runs"] == 2
