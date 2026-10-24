"""The coordinator's difficulty reading reaches Jev beside the declared capability.

A coordinator's reading of a node's difficulty travelled only inside the free
prose of ``--comment``. This covers the structured carrier that replaces it:
``reckon crew dispatch --assess KEY=VALUE`` renders ``node.orchestrator_assessment``
in the state Jev weighs, beside the section's declared ``node.capability`` and
never in place of it, and the run records the requirement the section routing
resolved. The negative control named in the node's manifest removes the
``node_definition.capability`` write below.
"""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.crew.picker as picker_package
from reckon import cli as cli_module
from reckon import crew_dispatch_commands, ledger
from reckon.crew.node import TaskNode
from reckon.crew.picker.types import Candidate

PROJECT = "proj"
PLAN = "plan-a"
SECTION = "s1"

# The capability the plan section declares, as the routing resolver normalises
# it. The declaration is what a run must record; the coordinator's assessment is
# a separate reading and never rewrites this.
DECLARED_CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {"reasoning": "deep", "verification": "strict", "risk": "moderate"},
}

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "routing": {"picker": "route"},
    "backends": {
        "clive": {
            "launch": "in-harness",
            "model": "local-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "beta": {
            "launch": "in-harness",
            "model": "requested-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _plan_html() -> str:
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        "</head><body>"
        '<h2 id="s1">A section declaring a capability</h2>'
        '<section data-reckon="section" data-id="s1" data-status="implementable" '
        'data-effort-hours="2" '
        'data-capability-version="1.0" data-capability-class="general" '
        'data-capability-reasoning="deep" data-capability-verification="strict" '
        'data-capability-risk="moderate"></section>'
        "</body></html>"
    )


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(tmp_path / "lane.json"))

    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    fleet_scripts = root / "skills" / "reckon-build" / "scripts"
    plans.mkdir(parents=True)
    fleet_scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (fleet_scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / f"{PLAN}.html").write_text(_plan_html(), encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _candidate() -> Candidate:
    return Candidate(
        backend="clive",
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
        outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    )


def _stub_picker(monkeypatch: pytest.MonkeyPatch, seen: dict) -> None:
    """Let the picker's own state render run, with Jev and the scan stubbed.

    The rendered state is the artifact under test, so ``pick`` itself is left
    real: only the client answer and the candidate scan are replaced, the two
    boundaries a test cannot reach without the operator's credentials or the
    live lane documents.
    """

    real_pick = picker_package.pick
    key = "local:local-model:clive"

    def snapshotter(request, config, repo, **kwargs):
        return [_candidate()]

    def caller(state, questions, **kwargs):
        seen["state"] = state
        return {
            "model": "stub",
            "usage": {},
            "answers": {
                "route": {
                    "choice": key,
                    "confidence": 0.9,
                    "probabilities": {key: 1.0},
                }
            },
        }

    # The dispatcher hands the pick its own caller, which records the answer on
    # the result, so the pick never calls a caller supplied here. The client is
    # the boundary both paths read, and it is the one that sees the state Jev
    # weighs.
    monkeypatch.setattr(picker_package.client, "ask", caller)

    def stubbed_pick(request, config, **kwargs):
        kwargs.setdefault("snapshotter", snapshotter)
        kwargs.setdefault("caller", caller)
        return real_pick(request, config, **kwargs)

    monkeypatch.setattr(picker_package, "pick", stubbed_pick)


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    extra: list[str] | None = None,
    dry_run: bool = True,
    stub_picker: bool = True,
):
    config = deepcopy(CONFIG)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    seen: dict = {}
    if stub_picker:
        _stub_picker(monkeypatch, seen)
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        SECTION,
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        node,
        "--goal",
        "record the requirement Jev weighed",
        "--done-when",
        "pytest reports the assessment reached the picker",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--no-watch",
        "--repo",
        str(repo),
    ]
    if dry_run:
        arguments.append("--dry-run")
    result = CliRunner().invoke(cli_module.main, [*arguments, *(extra or [])])
    return config, result, seen


def _payload(result) -> dict:
    return json.loads(result.output.splitlines()[0])


def test_assessment_renders_beside_the_declared_capability(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run dispatch renders the coordinator's reading beside the declaration."""

    _config, result, seen = _invoke(
        dispatch_repo,
        monkeypatch,
        node="assessed-node",
        extra=["--assess", "reasoning=deep", "--assess", "risk=critical"],
    )
    assert result.exit_code == 0, result.output
    payload = _payload(result)
    rendered = json.loads(payload["picker_selection"]["rendered_state"])
    assert rendered["node"]["orchestrator_assessment"] == {
        "reasoning": "deep",
        "risk": "critical",
    }
    assert rendered["node"]["capability"] == DECLARED_CAPABILITY
    # The pick's client saw the same state the payload records, so the assessment
    # reached the facts Jev weighed and not only the report.
    assert seen["state"]["node"]["orchestrator_assessment"] == {
        "reasoning": "deep",
        "risk": "critical",
    }


def test_no_assessment_renders_no_key(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch with no --assess leaves the state's node block unchanged."""

    _config, result, _seen = _invoke(dispatch_repo, monkeypatch, node="unassessed-node")
    assert result.exit_code == 0, result.output
    payload = _payload(result)
    rendered = json.loads(payload["picker_selection"]["rendered_state"])
    assert "orchestrator_assessment" not in rendered["node"]


def test_undeclared_value_is_refused_naming_the_key_and_values(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, result, _seen = _invoke(
        dispatch_repo,
        monkeypatch,
        node="bad-value-node",
        extra=["--assess", "risk=extreme"],
    )
    assert result.exit_code != 0
    detail = _payload(result)["detail"]
    assert "risk" in detail
    assert "critical" in detail and "moderate" in detail


def test_undeclared_key_is_refused_naming_the_key_and_values(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, result, _seen = _invoke(
        dispatch_repo,
        monkeypatch,
        node="bad-key-node",
        extra=["--assess", "colour=red"],
    )
    assert result.exit_code != 0
    detail = _payload(result)["detail"]
    assert "colour" in detail
    assert "reasoning" in detail and "risk" in detail


def _section_capability(repo: Path) -> dict:
    """The capability the section routing resolves for this plan and section."""

    from reckon.crew.routing import resolve_section_routing

    node = TaskNode(
        id="n",
        goal="g",
        plan=PLAN,
        section=SECTION,
        role="implement",
        spec_level="exact",
    )
    resolved = resolve_section_routing(
        deepcopy(CONFIG), node=node, plan_path=repo / "docs" / "plans" / f"{PLAN}.html"
    )
    return resolved["capability"]


def test_record_carries_capability_on_the_explicit_route(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --backend dispatch still records the section's requirement."""

    _config, result, _seen = _invoke(
        dispatch_repo,
        monkeypatch,
        node="explicit-node",
        extra=["--backend", "beta"],
        dry_run=False,
        stub_picker=False,
    )
    assert result.exit_code == 0, result.output
    payload = _payload(result)
    expected = _section_capability(dispatch_repo)
    assert payload["node"]["capability"] == expected
    # The ledger's record builder carries the authored node definition across,
    # so the promoted row keeps the requirement the run was dispatched under.
    row = ledger.build_record(
        run_id="r-explicit",
        project=PROJECT,
        plan=PLAN,
        section=SECTION,
        node="explicit-node",
        gate="passed",
        node_definition=payload["node"],
        dispatched_at="2026-10-16T00:00:00Z",
        completed_at="2026-10-16T00:00:00Z",
    )
    assert row["node_definition"]["capability"] == expected


def test_record_carries_capability_on_the_picker_route(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A picker-routed dispatch records the same requirement and the reading."""

    _config, result, _seen = _invoke(
        dispatch_repo,
        monkeypatch,
        node="picker-node",
        extra=["--assess", "reasoning=deep", "--assess", "risk=critical"],
        dry_run=False,
    )
    assert result.exit_code == 0, result.output
    payload = _payload(result)
    expected = _section_capability(dispatch_repo)
    assert payload["node"]["capability"] == expected
    assert payload["node"]["orchestrator_assessment"] == {
        "reasoning": "deep",
        "risk": "critical",
    }


# The lane blocks are carried straight through into the state, so the render's
# equality with build_state's mapping is what is under test rather than any
# figure inside them. The live lane document and the ledger are read by
# ``lane_context.build``, which the test replaces, so nothing outside the
# repository is read.
_FIXED_LANE = {
    "return_times": {"local": {"p50_s": 900.0, "n": 3}},
    "lanes": {"local": {"models": ["local-model"]}},
    "local_lane": {"admission": "open", "worker_slots": 2},
    "read_at": "2026-10-16T12:00:00+00:00",
}

# The comment carries an apostrophe and an ampersand, which one encoder escapes
# and the other leaves alone, so a state spliced after the render cannot pass
# for one built once even where the two encoders agreed on their separators.
_COMMENT = "weigh the lane's pace & the node's own risk"


def test_rendered_state_is_build_states_mapping_encoded_once(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state Jev is shown is the picker's own mapping through one encoder.

    The comparison is made against the rendered state the pick records rather
    than against the state ``prompts.render`` returns in isolation: a splice
    applied after the render leaves the latter untouched, so only the state that
    actually travels can show whether one encoder built it.
    """

    monkeypatch.setattr(
        picker_package.prompts.lane_context, "build", lambda **_kwargs: _FIXED_LANE
    )
    recorded: dict = {}
    real_build_state = picker_package.prompts.build_state

    def recording_build_state(**kwargs):
        mapping = real_build_state(**kwargs)
        recorded["mapping"] = mapping
        return mapping

    monkeypatch.setattr(picker_package.prompts, "build_state", recording_build_state)
    _config, result, seen = _invoke(
        dispatch_repo,
        monkeypatch,
        node="assessed-node",
        extra=["--assess", "reasoning=deep", "--assess", "risk=critical"],
    )
    assert result.exit_code == 0, result.output
    mapping = recorded["mapping"]
    assert mapping["node"]["orchestrator_assessment"] == {
        "reasoning": "deep",
        "risk": "critical",
    }
    rendered = _payload(result)["picker_selection"]["rendered_state"]
    assert rendered == picker_package.prompts.encode(mapping)
    # The client saw the same bytes the state records, so the encoder under test
    # is the one whose output Jev weighs.
    assert seen["state"] == json.loads(rendered)


def test_no_post_render_encoder_remains_in_the_picker() -> None:
    """The picker no longer re-encodes a rendered state after building it."""

    source = (
        Path(__file__).parents[1] / "reckon" / "crew" / "picker" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert "htmlsafe_json_dumps" not in source
