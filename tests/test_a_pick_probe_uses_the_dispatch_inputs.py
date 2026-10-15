"""A pick probe renders the same node state a dispatch would from the same fields.

``crew pick`` reads one node's live routing state without dispatching a worker,
so a coordinator can see an answer Jev would give a real node. A pick handed
fewer fields than a dispatch weighs a thinner node and can answer differently,
so the probe accepts the node fields dispatch already hands the picker — write
paths, negative control, estimated hours and section — and names any it left out
in its own answer. With every field supplied, a pick and a dry-run dispatch must
render the same node state for Jev.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import picker
from reckon.crew.picker import Candidate, prompts

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        name: {
            "launch": "in-harness",
            "model": f"{name}-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
        for name in ("alpha", "beta")
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

#: A fixed candidate so both arms weigh the same offering; only the node differs.
LANE = {"return_times": None, "lanes": {}, "local_lane": None}

PROBE_FIELDS = {
    "goal": "record a backend",
    "done_when": "pytest checks the selected backend",
    "section": "dispatch",
    "write_paths": ("result.json", "tests/test_probe.py"),
    "negative_control": "a pick given fewer fields must render a different state",
    "estimated_hours": "1.5",
}


def _candidate() -> Candidate:
    return Candidate(
        backend="beta",
        family="test",
        model="beta-model",
        effort="medium",
        local=False,
        availability="served",
        utilisation_pct=20,
        burn_multiple=1,
        pace_allowance=50,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={},
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "example.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="example"></head>'
        '<body><h2 id="dispatch">Dispatch</h2></body></html>'
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/example.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return root


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture the node each pick is handed, returning a hold so no worker runs.

    Both ``crew pick`` and the picker route of ``crew dispatch`` reach the picker
    through the same module attribute, so one recorder sees the node both build.
    """
    nodes: list = []

    def recording_pick(request, config, *, repo, **kwargs):
        # Snapshot the node as the picker is handed it: a later dispatch stage
        # may extend the same object's write paths, and the probe measures the
        # node the pick itself saw.
        nodes.append(copy.deepcopy(request.node))
        return SimpleNamespace(
            as_dict=lambda: {
                "action": "route",
                "backend": "beta",
                "family": "test",
                "model": "beta-model",
                "effort": "medium",
                "probabilities": {"beta": 1.0},
                "confidence": 0.9,
                "jev_model": "jev-test",
                "fallback_reason": None,
                "latency_ms": 1.0,
                "excluded": [],
                "comment": "",
            }
        )

    monkeypatch.setattr(picker, "pick", recording_pick)
    return nodes


def node_state(node) -> dict:
    """The node block Jev is shown, rendered by the picker's own state builder."""
    return prompts.build_state(
        node=node,
        capability=None,
        estimated_context=0,
        comment="",
        candidates=[_candidate()],
        lane=LANE,
        attempts=0,
    )["node"]


def invoke_pick(repo: Path, *, fields: dict | None = PROBE_FIELDS):
    args = [
        "crew",
        "pick",
        "--project",
        "proj",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--goal",
        "record a backend",
        "--done-when",
        "pytest checks the selected backend",
        "--checkout-path",
        str(repo),
        "--session",
        "session",
    ]
    for key, value in (fields or {}).items():
        if key == "write_paths":
            for path in value:
                args += ["--write-path", path]
            continue
        args += [f"--{key.replace('_', '-')}", value]
    result = CliRunner().invoke(cli.main, args)
    payload = json.loads(result.output.splitlines()[0])
    return result, payload


def invoke_dispatch(repo: Path):
    """A dry-run dispatch on the picker route, so the pick runs before any gate."""
    args = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "example",
        "--section",
        PROBE_FIELDS["section"],
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "probe",
        "--goal",
        PROBE_FIELDS["goal"],
        "--done-when",
        PROBE_FIELDS["done_when"],
        "--negative-control",
        PROBE_FIELDS["negative_control"],
        "--estimated-hours",
        PROBE_FIELDS["estimated_hours"],
        "--route",
        "picker",
        "--dry-run",
        "--session",
        "session",
        "--repo",
        str(repo),
        "--no-watch",
    ]
    for path in PROBE_FIELDS["write_paths"]:
        args += ["--write-path", path]
    return CliRunner().invoke(cli.main, args)


def test_a_pick_and_a_dry_run_dispatch_render_the_same_node_state(repo, captured):
    result, _ = invoke_pick(repo)
    assert result.exit_code == 0, result.output
    invoke_dispatch(repo)
    assert len(captured) == 2, "both the pick and the dispatch must reach the picker"

    pick_node, dispatch_node = captured
    assert node_state(pick_node) == node_state(dispatch_node)


def test_the_probe_carries_every_field_a_dispatch_will(repo, captured):
    result, _ = invoke_pick(repo)
    assert result.exit_code == 0, result.output
    state = node_state(captured[0])
    assert state["write_path_count"] == len(PROBE_FIELDS["write_paths"])
    assert state["negative_control_declared"] is True
    assert state["estimated_hours"] == float(PROBE_FIELDS["estimated_hours"])


def test_a_probe_that_omits_write_paths_names_them(repo, captured):
    result, payload = invoke_pick(repo, fields=None)
    assert result.exit_code == 0, result.output
    assert "write_paths" in payload["omitted_inputs"]
    assert payload["omitted_inputs"] == [
        "write_paths",
        "negative_control",
        "estimated_hours",
        "section",
    ]


def test_a_full_probe_omits_nothing(repo, captured):
    result, payload = invoke_pick(repo)
    assert result.exit_code == 0, result.output
    assert payload["omitted_inputs"] == []


def test_a_thinner_probe_renders_a_different_state(repo, captured):
    """The negative control: fewer fields, a state a dispatch would not render."""
    invoke_dispatch(repo)
    dispatch_node = captured[0]
    invoke_pick(repo, fields=None)
    thin_node = captured[1]
    assert node_state(thin_node) != node_state(dispatch_node)


def test_the_pick_options_mirror_dispatch_and_retire_nothing():
    pick_params = {
        param.name for param in cli.main.commands["crew"].commands["pick"].params
    }
    dispatch_params = {
        param.name for param in cli.main.commands["crew"].commands["dispatch"].params
    }
    mirrored = {"section", "write_paths", "negative_control", "estimated_hours"}
    assert mirrored <= pick_params
    assert mirrored <= dispatch_params
    # The pick's pre-existing inputs remain, so the options add rather than retire.
    assert {"goal", "done_when", "spec_level", "session"} <= pick_params
