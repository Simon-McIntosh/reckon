"""Jev routes with the session's own admission, and with a lift, in view.

The state rendered for Jev carries, for every lane whose router publishes a
document, the workers that router would grant this coordinator session, read
through the one allowance helper the dispatch path reads. A route that lands on
a lane granting the session nothing is re-asked once with that lane excluded
for well-defined work, and a lift in force on a lane is named and offered; a
hold stays Jev's own answer.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import (
    budget_lift,
    dispatch_picker,
    dispatch_plan,
    recovery_review_dispatch,
)
from reckon.crew.node import BudgetHold, TaskNode
from reckon.crew.picker import prompts
from reckon.crew.picker import snapshot as snapshot_module
from reckon.crew.picker.types import PickRequest

LOCAL = "clive"
CODEX = "codex"
PROJECT = "proj"


def _lane_document(path: Path, *, session_slots: float, lane_wide: float) -> Path:
    """A lane document that is open lane-wide but grants this session nothing."""
    path.write_text(
        json.dumps(
            {
                "headroom": lane_wide,
                "admission": {
                    "worker_slots": lane_wide,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": session_slots}},
                    "verdict": "open",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _config(
    root: Path,
    *,
    local_slots: float = 0,
    codex_slots: float = 4,
    codex_lift: dict | None = None,
    codex_review_excluded: bool = False,
) -> dict:
    local_doc = _lane_document(
        root / "lane-local.json", session_slots=local_slots, lane_wide=6
    )
    codex_doc = _lane_document(
        root / "lane-codex.json", session_slots=codex_slots, lane_wide=8
    )
    config = {
        "default_backend": CODEX,
        "local_backend": LOCAL,
        "backends": {
            LOCAL: {
                "launch": "cli",
                "command": "clive",
                "model": "deepseek",
                "lane_document": str(local_doc),
                "budget_group": "local-wallet",
                "time_budget": "25m",
                "sandbox": "worktree-full",
            },
            CODEX: {
                "launch": "cli",
                "command": "codex",
                "model": "gpt-5",
                "lane_document": str(codex_doc),
                "budget_group": "codex-wallet",
                "time_budget": "25m",
                "sandbox": "worktree-full",
            },
        },
        "roles": {"implement": {}, "test": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_fixes": 2},
    }
    if codex_review_excluded:
        config["review_excluded_backends"] = [CODEX]
    if codex_lift is not None:
        config["_codex_lift"] = codex_lift
    return config


def _budget_snapshot(config: dict) -> dict:
    lift = config.pop("_codex_lift", None)
    groups = [
        {
            "group": "codex-wallet",
            "members": [CODEX],
            "allowance": {"utilisation": 0.2, **({"lift": lift} if lift else {})},
        }
    ]
    return {
        "backends": [
            {"backend": name, "held": False, "state": {"headroom": "unknown"}}
            for name in config["backends"]
        ],
        "groups": groups,
    }


def _node(role: str = "implement", spec_level: str = "guided") -> TaskNode:
    return TaskNode(
        id="n",
        plan="",
        role=role,
        spec_level=spec_level,
        goal="g",
        done_when="d",
        time_budget="60m",
    )


@pytest.fixture()
def state_readers(monkeypatch: pytest.MonkeyPatch):
    """Every expensive collaborator the snapshot loop reaches, replaced."""
    monkeypatch.setattr(snapshot_module, "_fit", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        snapshot_module, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot_module.routing, "_estimated_hours", lambda *a, **k: (None, None)
    )
    from reckon.crew import resumption

    monkeypatch.setattr(
        resumption, "_read_lane_probe_cache", lambda *a, **k: None, raising=False
    )


def _candidates(
    config: dict, node: TaskNode, *, monkeypatch: pytest.MonkeyPatch, root: Path
):
    """The candidates one pick would be offered, from the real snapshot loop."""
    return snapshot_module.candidates(
        PickRequest(PROJECT, node, estimated_context=1000, session="session"),
        config,
        root,
        records=[],
        verdict_inputs={},
        budget_snapshot=_budget_snapshot(config),
        cached_only=True,
        availability_cache={},
        authority={},
    )


def test_the_state_carries_the_sessions_slots_for_a_metered_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_readers
) -> None:
    """A lane open lane-wide that grants the session no slot reads as zero.

    The local lane is open at six workers but grants this session none, and the
    figure is read through the one allowance helper rather than a second reader.
    """
    calls: list[str] = []
    original = dispatch_plan._lane_worker_allowance
    assert snapshot_module._lane_worker_allowance is original

    def recording(document, *, session):
        calls.append(str(session))
        return original(document, session=session)

    monkeypatch.setattr(snapshot_module, "_lane_worker_allowance", recording)
    config = _config(tmp_path, local_slots=0, codex_slots=4)

    found = {
        candidate.backend: candidate
        for candidate in _candidates(
            config, _node(), monkeypatch=monkeypatch, root=tmp_path
        )
    }
    assert found[LOCAL].worker_slots == 0
    assert calls, "the allowance helper was not consulted"
    assert set(calls) == {"session"}


def test_the_state_renders_the_session_figure_it_read(
    tmp_path: Path, state_readers
) -> None:
    """The rendered state carries the figure the allowance helper returned."""
    config = _config(tmp_path, local_slots=0, codex_slots=6)
    candidates = _candidates(config, _node(), monkeypatch=None, root=tmp_path)
    state = json.loads(
        prompts.render(
            "state.jinja",
            node=_node(),
            capability=None,
            estimated_context=1000,
            comment="",
            candidates=candidates,
            project=PROJECT,
            records=[],
            budget_snapshot=_budget_snapshot(config),
            config=config,
            attempts=0,
        )
    )
    assert [c["worker_slots"] for c in state["candidates"].values()] == [0, 6]


# ── the re-ask ───────────────────────────────────────────────────────────────


def _offered(backend: str, lane: str, *, slots: float | None) -> dict:
    return {
        "backend": backend,
        "family": lane,
        "model": f"{backend}-model",
        "local": lane == "local",
        "worker_slots": slots,
    }


def _selection(*, action: str, backend: str | None, offered: list[dict]) -> dict:
    return {
        "action": action,
        "backend": backend,
        "family": None,
        "model": None,
        "effort": None,
        "probabilities": {},
        "confidence": 0.5,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "offered": offered,
        "excluded": [],
        "rendered_token_estimate": 0,
        "latency_ms": 1.0,
        "decision_source": "jev",
        "comment": "",
        "rendered_state": "{}",
        "questions": {},
        "template_digest": "t",
    }


def _stub_pick(monkeypatch: pytest.MonkeyPatch, answers: list[dict]) -> list[dict]:
    seen: list[dict] = []

    class _S:
        def __init__(self, fields: dict) -> None:
            self._fields = fields

        def as_dict(self) -> dict:
            return dict(self._fields)

    def fake_pick(request, config, **kwargs):
        seen.append(dict(config))
        answer = answers[min(len(seen) - 1, len(answers) - 1)]
        return _S(answer)

    import reckon.crew.picker as picker_module

    monkeypatch.setattr(picker_module, "pick", fake_pick)
    return seen


def _answer(backend: str, lane: str, *, action: str = "route") -> dict:
    return _selection(
        action=action,
        backend=backend,
        offered=[
            _offered(LOCAL, "local", slots=0),
            _offered(CODEX, "codex", slots=4),
        ],
    ) | {"family": lane}


def test_a_guided_implement_node_on_a_zero_slot_lane_is_re_asked_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second ask leaves that lane out, and both asks are recorded."""
    answers = [
        _answer(LOCAL, "local"),
        _answer(CODEX, "codex"),
    ]
    seen = _stub_pick(monkeypatch, answers)
    config = _config(tmp_path)
    selection = dispatch_picker.dispatch_picker_selection(
        node=_node("implement", "guided"),
        config=config,
        project=PROJECT,
        repo=tmp_path,
        session="session",
        comment="",
    )
    assert len(seen) == 2, "the node was not re-asked exactly once"
    assert LOCAL in seen[0]["backends"]
    assert LOCAL not in seen[1]["backends"], "the zero-slot lane was not excluded"
    assert CODEX in seen[1]["backends"]
    assert selection["backend"] == CODEX and selection["action"] == "route"
    assert selection["excluded_zero_slot_lane"] == "local"
    assert [ask["backend"] for ask in selection["asks"]] == [LOCAL, CODEX]


def test_one_slot_routes_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane granting the session a worker is answered on the first ask."""
    first = _answer(LOCAL, "local")
    first["offered"][0]["worker_slots"] = 1
    seen = _stub_pick(monkeypatch, [first, _answer(CODEX, "codex")])
    config = _config(tmp_path)
    selection = dispatch_picker.dispatch_picker_selection(
        node=_node("implement", "guided"),
        config=config,
        project=PROJECT,
        repo=tmp_path,
        session="session",
        comment="",
    )
    assert len(seen) == 1
    assert selection["backend"] == LOCAL
    assert "asks" not in selection


def test_an_open_node_may_still_hold_on_a_zero_slot_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node that must choose its own design keeps its hold."""
    seen = _stub_pick(monkeypatch, [_answer(LOCAL, "local", action="hold")])
    config = _config(tmp_path)
    selection = dispatch_picker.dispatch_picker_selection(
        node=_node("implement", "open"),
        config=config,
        project=PROJECT,
        repo=tmp_path,
        session="session",
        comment="",
    )
    assert len(seen) == 1
    assert selection["action"] == "hold"
    assert "asks" not in selection


def test_a_routine_hold_is_recorded_as_jevs_own_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold for a routine node is kept as Jev's answer, never overridden."""
    seen = _stub_pick(monkeypatch, [_answer(LOCAL, "local", action="hold")])
    config = _config(tmp_path)
    selection = dispatch_picker.dispatch_picker_selection(
        node=_node("review", "guided"),
        config=config,
        project=PROJECT,
        repo=tmp_path,
        session="session",
        comment="",
    )
    assert len(seen) == 1
    assert selection["action"] == "hold"
    assert dispatch_picker._picker_refusal_reasons(selection).startswith(
        "picker selected hold"
    )
    with pytest.raises(BudgetHold, match="hold") as raised:
        dispatch_plan.plan_dispatch(
            node=_node("review", "guided"),
            config=config,
            project=PROJECT,
            repo=tmp_path,
            route="picker",
            picker_selection=selection,
        )
    assert "hold" in str(raised.value)


# ── the lift ────────────────────────────────────────────────────────────────


LIFT = {
    "id": "lift-1",
    "group": "codex-wallet",
    "form": "uncapped",
    "scope": "global",
    "pace_multiple": 1.0,
    "ends_at": None,
    "ends_on": "unknown",
    "remaining_seconds": None,
}


def _review_excluded(config: dict, *, home: Path, monkeypatch, lift: bool) -> set[str]:
    monkeypatch.setenv("RECKON_HOME", str(home))
    document = home / budget_lift.LIFTS_LEAF
    if lift:
        now = datetime.now(UTC)
        document.write_text(
            json.dumps(
                {
                    "version": 1,
                    "lifts": [
                        {
                            "id": "lift-1",
                            "group": "codex-wallet",
                            "form": "uncapped",
                            "scope": "global",
                            "pace_multiple": 1.0,
                            "granted_at": now.isoformat(),
                            "cleared_at": None,
                            "ends": {
                                "kind": "at",
                                "at": (now + timedelta(hours=6)).isoformat(),
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    else:
        document.write_text(json.dumps({"version": 1, "lifts": []}), encoding="utf-8")
    return recovery_review_dispatch._review_excluded_backends(config)


def test_a_lift_keeps_its_lane_off_the_exclusion_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lifted lane is offered to a review; without the lift it is not."""
    home = tmp_path / "config"
    home.mkdir()
    config = _config(tmp_path, codex_review_excluded=True)
    assert _review_excluded(config, home=home, monkeypatch=monkeypatch, lift=False) == {
        CODEX
    }
    assert (
        _review_excluded(config, home=home, monkeypatch=monkeypatch, lift=True) == set()
    )


def test_the_rendered_state_names_the_lift_and_the_guidance_carries_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_readers
) -> None:
    """The state names the lift on the lane, and the wording is carried."""
    config = _config(tmp_path, codex_slots=0, codex_lift=LIFT)
    candidates = _candidates(
        config, _node("review", "guided"), monkeypatch=monkeypatch, root=tmp_path
    )
    by_backend = {candidate.backend: candidate for candidate in candidates}
    assert by_backend[CODEX].congestion["lift"]["form"] == "uncapped"
    assert by_backend[CODEX].congestion["lift"]["id"] == "lift-1"
    questions = prompts.render("questions.jinja", candidates=candidates)
    instructions = json.loads(questions)["route"]["instructions"]
    assert "meant to be spent" in instructions
    assert "cheaper models" in instructions
    assert "grants this session no worker now" in instructions
    assert re.findall(r"<([a-z_]+)>", instructions) == ["lane", "model", "backend"]


def test_a_review_without_the_lift_is_offered_no_lifted_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_readers
) -> None:
    """Without the lift the excluded lane is offered no option at all."""
    config = _config(tmp_path, codex_review_excluded=True)
    candidates = _candidates(
        config, _node("review", "guided"), monkeypatch=monkeypatch, root=tmp_path
    )
    offered = [c.backend for c in candidates if not c.reasons]
    assert CODEX not in offered
    excluded = [c for c in candidates if c.reasons]
    assert any("review-excluded-backend" in c.reasons for c in excluded)
