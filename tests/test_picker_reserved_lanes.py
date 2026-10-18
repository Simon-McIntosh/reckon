"""A lane a flight policy reserves is offered on its terms, and never by default.

The lane the orchestrators share is what this guards: a pick reaches it only
where the node is hard enough and the lane's own window still has room, and only
with a reason recorded on the answer. The policy is a rule about what may be
offered rather than a pressure signal, so a lane it withholds is removed in code
before the model sees the offering, and it is never a fallback when a pick fails
or holds. A coordinator that names the lane by hand is not filtered by it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import budget, cli, crew_dispatch_commands
from reckon.crew import dispatch_picker
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, client, snapshot
from tests.test_picker_in_dispatch import repo  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)

ROUTINE: dict = {}
HARDEST = {
    "class": "general",
    "requirements": {
        "reasoning": "deep",
        "verification": "strict",
        "risk": "critical",
    },
}
#: The lane-and-model-and-backend pair the reserved lane is offered under.
CLAUDE_PAIR = "claude:opus:claude-opus"
CODEX_PAIR = "codex:astra:codex-astra"

CONFIG = {
    "default_backend": "codex-astra",
    "budget": {"pace_multiple": 1.1},
    "roles": {"implement": {}, "review": {}},
    "picker_reserved_lanes": {
        "claude": {
            "floor": {
                "reasoning": "deep",
                "verification": "strict",
                "risk": ["elevated", "critical"],
            },
            "or_after_attempts": 2,
            "window_ceiling": 0.6,
        }
    },
    "backends": {
        "claude-opus": {
            "launch": "cli",
            "command": "claude",
            "model": "opus",
            "effort": "high",
            "budget_group": "claude",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "codex-astra": {
            "launch": "cli",
            "command": "codex",
            "model": "astra",
            "effort": "high",
            "budget_group": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _node(role: str = "implement") -> TaskNode:
    return TaskNode(
        id="n",
        goal="g",
        plan="",
        role=role,
        spec_level="guided",
        done_when="d",
        time_budget="45m",
    )


def _request(*, capability=None, attempts=None, assessment=None, role="implement"):
    return PickRequest(
        "example",
        _node(role),
        capability=ROUTINE if capability is None else capability,
        estimated_context=8000,
        attempts=attempts,
        orchestrator_assessment=assessment or {},
    )


def _snapshot_view(
    *, claude_pct: float = 42.0, known: bool = True, codex_pct: float | None = None
) -> dict:
    """One composed budget snapshot: the reserved lane's window at one reading.

    ``claude_pct`` is the share of the lane's own account window already spent,
    which is the figure the lanes block publishes as the lane's utilisation.
    ``known`` false is the absent reading: no window figure at all.
    ``codex_pct`` gives the codex lane the same kind of fresh reading, which a
    config that also reserves that lane needs before its policy will offer it.
    """

    def _fresh(pct: float) -> dict:
        return {
            "headroom": "known",
            "utilisation_pct": pct,
            "burn_multiple": 1.0,
            "resets_at": None,
            "expired": False,
            "observed_at": NOW.isoformat(),
            "source": "account-surface",
        }

    allowance = {
        "state": budget.OBSERVED,
        "utilisation": claude_pct / 100,
        "burn_multiple": 1.0,
        "effective_limit": 0.3,
        "resets_at": None,
        "reset_available": False,
    }
    backends = []
    for name in CONFIG["backends"]:
        if name == "claude-opus":
            state = _fresh(claude_pct) if known else {
                "headroom": "unknown",
                "detail": "no reading",
            }
        elif codex_pct is not None:
            state = _fresh(codex_pct)
        else:
            state = {}
        backends.append({"backend": name, "held": False, "state": state})
    return {
        "groups": [
            {"group": "claude", "members": ["claude-opus"], "allowance": allowance}
        ],
        "backends": backends,
    }


@pytest.fixture
def live_facts(monkeypatch, tmp_path):
    """Freeze every fleet read so a pick touches only the code under test."""

    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(tmp_path / "lane.json"))
    monkeypatch.setattr(snapshot.routing, "shared_verdict_inputs", lambda *a: {})
    monkeypatch.setattr(
        snapshot.budget,
        "latest_recorded",
        lambda *a, **k: SimpleNamespace(for_backend=lambda name: None, unattributed=[]),
    )
    monkeypatch.setattr(snapshot.budget, "recorded_windows", lambda *a, **k: {})
    monkeypatch.setattr(snapshot.budget, "group_pace", lambda *a, **k: [])
    monkeypatch.setattr(
        snapshot.budget,
        "state_for",
        lambda name, *a, **k: budget.BudgetState(name),
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        snapshot.routing,
        "_context_fit_verdict",
        lambda **k: {"allowed": True, "window_tokens": 100000},
    )
    monkeypatch.setattr(
        snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )
    return tmp_path


def _candidates(repo, request, *, claude_pct=42.0, known=True, config=CONFIG):
    return snapshot.candidates(
        request,
        config,
        repo,
        records=[],
        verdict_inputs={},
        budget_snapshot=_snapshot_view(claude_pct=claude_pct, known=known),
    )


def _offered(options):
    return {candidate.backend for candidate in options if not candidate.reasons}


def _reasons(options, backend):
    return next(c.reasons for c in options if c.backend == backend)


def _reserved_reason(options, backend):
    return next(
        (
            reason
            for reason in _reasons(options, backend)
            if reason.startswith("reserved-lane:")
        ),
        "",
    )


# --- the lane is offered only on the policy's terms ---------------------------


def test_the_policy_is_keyed_by_the_lane_the_state_reads(live_facts):
    """A policy names a lane; the lane a backend belongs to is the one resolved."""

    assert dispatch_picker.lane_family(CONFIG, "claude-opus") == "claude"
    assert dispatch_picker.lane_family(CONFIG, "codex-astra") != "claude"


def test_a_routine_node_never_sees_the_reserved_lane(live_facts):
    """A routine node at full headroom is never offered the reserved lane."""

    options = _candidates(live_facts, _request(), claude_pct=1.0)
    assert "claude-opus" not in _offered(options)
    assert _reserved_reason(options, "claude-opus")


def test_the_hardest_tier_with_headroom_sees_the_lane(live_facts):
    """The declared hardest tier is offered the lane while its window has room."""

    options = _candidates(live_facts, _request(capability=HARDEST), claude_pct=42.0)
    assert "claude-opus" in _offered(options)


def test_the_assessment_can_declare_the_same_tier(live_facts):
    """The coordinator's own reading admits the lane where the plan declares none."""

    options = _candidates(
        live_facts,
        _request(capability=ROUTINE, assessment=HARDEST["requirements"]),
        claude_pct=42.0,
    )
    assert "claude-opus" in _offered(options)


@pytest.mark.parametrize(
    "claude_pct,known",
    [(60.0, True), (90.0, True), (42.0, False)],
)
def test_no_headroom_withholds_the_lane(live_facts, claude_pct, known):
    """At or above the ceiling, and on an unknown reading, the lane is withheld."""

    options = _candidates(
        live_facts, _request(capability=HARDEST), claude_pct=claude_pct, known=known
    )
    assert "claude-opus" not in _offered(options)
    assert _reserved_reason(options, "claude-opus")


def test_a_node_that_failed_elsewhere_sees_the_lane(live_facts):
    """Attempts at or above the policy's count admit the lane on a routine node."""

    options = _candidates(live_facts, _request(attempts=2), claude_pct=42.0)
    assert "claude-opus" in _offered(options)


def test_one_attempt_below_the_count_does_not(live_facts):
    """One attempt below the policy's count leaves the lane withheld."""

    options = _candidates(live_facts, _request(attempts=1), claude_pct=42.0)
    assert "claude-opus" not in _offered(options)


def test_a_review_node_is_not_governed_by_the_policy(live_facts):
    """The policy governs non-review work; review routing keeps its own rule."""

    options = _candidates(
        live_facts, _request(capability=ROUTINE, role="review"), claude_pct=42.0
    )
    assert "claude-opus" in _offered(options)


def test_the_policy_removed_is_the_negative_control(live_facts):
    """With the policy removed, the routine node sees the lane on headroom alone.

    This is the behaviour the policy exists to stop, and the arm the negative
    control for this test file is taken from.
    """

    config = {
        key: value for key, value in CONFIG.items() if key != "picker_reserved_lanes"
    }
    options = _candidates(live_facts, _request(), claude_pct=42.0, config=config)
    assert "claude-opus" in _offered(options)


# --- the reason a reserved-lane pick must carry -------------------------------


def _dispatch_pick(
    repo,
    monkeypatch,
    request,
    choices,
    assessment=None,
    *,
    config=CONFIG,
    snapshot=None,
):
    """Run the dispatcher's own picker path with a stubbed Jev client.

    ``choices`` is one ``(option_key, reason)`` pair per asking, in order. The
    stub answers over exactly the option keys the rendered question offers, the
    way a real answer must, and keeps each asking's criteria so a test can read
    what the model was shown. The dispatcher builds its own request from the
    node, so a node's difficulty reaches this path the way it does in the field:
    through the coordinator's assessment, not through a request field.
    """

    calls = []

    def ask(state, questions, *, env_path=None):
        criteria = questions["route"]["criteria"]
        calls.append(criteria)
        choice, reason = choices[len(calls) - 1]
        if choice not in criteria:
            raise AssertionError(
                f"{choice!r} was not offered; shown {sorted(criteria)}"
            )
        probabilities = dict.fromkeys(criteria, 0.0)
        probabilities[choice] = 1.0
        route = {"choice": choice, "confidence": 0.9, "probabilities": probabilities}
        if reason is not None:
            route["reason"] = reason
        return {"answers": {"route": route}}

    monkeypatch.setattr(client, "ask", ask)
    selection = dispatch_picker.dispatch_picker_selection(
        node=request.node,
        config=config,
        project="example",
        repo=repo,
        comment="lead note",
        records=[],
        verdict_inputs={},
        orchestrator_assessment=assessment,
        budget_snapshot=snapshot or _snapshot_view(claude_pct=42.0),
    )
    return selection, calls


def test_a_reserved_pick_carries_and_records_its_reason(
    live_facts, monkeypatch, tmp_path
):
    """A pick of the reserved lane answers why, and the reason is recorded."""

    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    reason = "declared deep reasoning at critical risk; claude window 42% of 60%"
    selection, calls = _dispatch_pick(
        live_facts,
        monkeypatch,
        _request(capability=HARDEST),
        [(CLAUDE_PAIR, reason)],
        assessment=HARDEST["requirements"],
    )
    # The lane was offered at all only because this node meets the policy's
    # floor, and the reason travels with the choice onto the run record.
    assert len(calls) == 1
    assert CLAUDE_PAIR in calls[0]
    assert selection["action"] == "route"
    assert selection["backend"] == "claude-opus"
    assert selection["reserved_lane"] == "claude"
    assert selection["reason"] == reason
    row = json.loads(
        (home / "crew" / dispatch_picker.RESERVED_LANE_PICKS_LOG)
        .read_text()
        .splitlines()[0]
    )
    assert row["lane"] == "claude"
    assert row["backend"] == "claude-opus"
    assert row["reason"] == reason
    assert row["refused_unreasoned"] is False


def test_an_unreasoned_reserved_pick_is_refused_and_re_picked(
    live_facts, monkeypatch, tmp_path
):
    """A reserved-lane choice with no reason is refused and the lane left out."""

    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    selection, calls = _dispatch_pick(
        live_facts,
        monkeypatch,
        _request(capability=HARDEST),
        [(CLAUDE_PAIR, None), (CODEX_PAIR, None)],
        assessment=HARDEST["requirements"],
    )
    assert len(calls) == 2
    # The second asking is shown the lane left out: the refusal is in code.
    assert CLAUDE_PAIR in calls[0]
    assert CLAUDE_PAIR not in calls[1]
    assert selection["reserved_lane_unreasoned"] == "claude"
    assert selection["reissued"] is True
    assert selection["backend"] == "codex-astra"
    rows = (
        (home / "crew" / dispatch_picker.RESERVED_LANE_PICKS_LOG)
        .read_text()
        .splitlines()
    )
    assert len(rows) == 1
    refusal = json.loads(rows[0])
    assert refusal["lane"] == "claude"
    assert refusal["reason"] is None
    assert refusal["refused_unreasoned"] is True


#: A config that reserves the codex lane as well as the claude lane, so the
#: reserved-lane re-ask's second asking can land on a reserved lane again.
TWO_RESERVED = {
    **CONFIG,
    "picker_reserved_lanes": {
        "claude": CONFIG["picker_reserved_lanes"]["claude"],
        "codex": CONFIG["picker_reserved_lanes"]["claude"],
    },
}


def test_a_nested_reask_onto_an_unreasoned_reserved_lane_is_held(
    live_facts, monkeypatch, tmp_path
):
    """A reserved lane reached on a nested re-ask with no reason is held.

    The first asking picks the claude lane with no reason and is refused, the
    lane left out; the second asking, run with re-asking disabled, lands on the
    codex lane -- also reserved -- and gives no reason again. That nested asking
    cannot ask a third time, so the pick must be refused as a hold rather than
    routed while the log records the refusal. On the unfixed code the nested
    asking routes to codex while the log carries the claude refusal, so the log
    and the routed lane disagree.
    """

    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    # The nested asking is run by the re-ask helper, which carries the first
    # asking's selection forward but composes its own budget view. The reserved
    # lane it lands on is offered only if a window reading reaches that asking
    # too, so the view the snapshot composes is frozen for both askings.
    view = _snapshot_view(claude_pct=42.0, codex_pct=10.0)
    monkeypatch.setattr(snapshot, "budget_view", lambda *a, **k: view)
    selection, calls = _dispatch_pick(
        live_facts,
        monkeypatch,
        _request(capability=HARDEST),
        [(CLAUDE_PAIR, None), (CODEX_PAIR, None)],
        assessment=HARDEST["requirements"],
        config=TWO_RESERVED,
        snapshot=view,
    )
    assert len(calls) == 2
    assert CLAUDE_PAIR in calls[0]
    assert CLAUDE_PAIR not in calls[1]
    # The nested asking was refused rather than routed: the node is held.
    assert selection["action"] == "hold"
    assert not selection.get("backend")
    rows = [
        json.loads(line)
        for line in (home / "crew" / dispatch_picker.RESERVED_LANE_PICKS_LOG)
        .read_text()
        .splitlines()
    ]
    # The log and the routed lane agree: the claude refusal was re-asked, and the
    # nested codex pick was held, never routed.
    assert [row["lane"] for row in rows] == ["claude", "codex"]
    assert rows[0]["refused_unreasoned"] is True
    assert rows[0]["disposition"] == "reasked"
    assert rows[1]["refused_unreasoned"] is True
    assert rows[1]["disposition"] == "held"


def test_an_unreserved_pick_is_left_alone(live_facts, monkeypatch, tmp_path):
    """A pick of an ordinary lane demands no reason and records no lane."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    selection, calls = _dispatch_pick(
        live_facts,
        monkeypatch,
        _request(),
        [(CODEX_PAIR, None)],
    )
    assert len(calls) == 1
    assert not selection.get("reserved_lane")
    assert not selection.get("reason")
    assert selection["backend"] == "codex-astra"


# --- a coordinator that names the lane is not filtered by the policy ----------


def _invoke(repo, *extra):
    """Dispatch one node through the CLI, returning its parsed payload."""

    args = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "example",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "picker-test",
        "--goal",
        "record a backend",
        "--done-when",
        "pytest checks the named lane is the one dispatched to",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
        *extra,
    ]
    return CliRunner().invoke(cli.main, args)


def _payload(result):
    return json.loads(result.output.splitlines()[0])


def test_naming_the_reserved_lane_is_not_filtered(repo, monkeypatch):
    """A coordinator naming the reserved lane dispatches there, no pick made.

    The policy governs what the picker may offer, not what a coordinator may
    name: naming a lane routes deterministically, so the candidate scan the
    policy filters is never consulted for this dispatch.
    """

    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )

    def never(*_a, **_k):
        raise AssertionError("a named lane must not reach the picker")

    monkeypatch.setattr(dispatch_picker, "dispatch_picker_selection", never)
    result = _invoke(repo, "--backend", "claude-opus", "--comment", "lead chose it")
    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["route"] == "deterministic"
    assert payload["backend"] == "claude-opus"
    assert payload["requested_backend"] == "claude-opus"


def test_naming_the_reserved_lane_through_the_picker_is_refused(repo, monkeypatch):
    """A named lane is a deterministic route; asking for both is a usage error."""

    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    result = _invoke(repo, "--backend", "claude-opus", "--route", "picker")
    assert result.exit_code != 0
    assert "picker routing cannot be combined" in result.output
