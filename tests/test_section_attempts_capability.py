"""A section's attempt count raises the capability a node resolves at.

The plan's typed section record owns both the capability the work declares and
the attempts it has cost, so the record alone decides which lane serves the
node. This checks that a record at or above the shipped threshold resolves on
the raised class's lane and says which count caused it, that one below the
threshold resolves unchanged and names nothing, and that resolution leaves the
workstation's own config home alone.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from reckon import flight
from reckon._plan_html import write_state
from reckon.crew.node import TaskNode

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_DEFAULTS = ROOT / "reckon" / "schema" / "flight-defaults.yaml"
SHIPPED_LINKML = ROOT / "reckon" / "schema" / "flight.yaml"

HOST_LAYER = """
version: 1
backends:
  section-lane:
    launch: in-harness
    sandbox: worktree-full
  raised-lane:
    launch: in-harness
    sandbox: worktree-full
roles:
  implement:
    backend: section-lane
    by_capability_class:
      orchestrator:
        backend: raised-lane
        effort: raised-effort
"""


def _resolve_temporary_layers(tmp_path: Path):
    """Resolve shipped defaults plus a temporary host layer.

    The raise rule itself is deliberately absent from the host layer: the
    threshold under test is the one the shipped defaults declare.
    """
    host = tmp_path / "host" / "flight.yaml"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(HOST_LAYER, encoding="utf-8")
    return flight.resolve(
        host_path=host, project_path=tmp_path / "project" / "flight.yaml"
    )


def _plan(
    tmp_path: Path,
    attempts: int,
    capability_class: str = "general",
    reasoning: str = "standard",
    verification: str = "standard",
) -> Path:
    """Write a plan whose section record carries a seeded attempt count."""
    slug = "section-attempts"
    authored = (
        '<h2 id="s5">§5 — Capability raises with section attempts</h2>\n'
        "<p>The raise under test reads this section's own record.</p>"
    )
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{authored}</main></body></html>'
    )
    state = {
        "slug": slug,
        "title": "Section attempts raise the capability",
        "version": 1,
        "type": "plan",
        "status": "active",
        "impl": 0.5,
        "section_declarations": {"s5": "implementable"},
        "sections": [
            {
                "id": "s5",
                "effort_hours": 1.0,
                "capability": {
                    "version": "1.0",
                    "class": capability_class,
                    "requirements": {
                        "reasoning": reasoning,
                        "verification": verification,
                        "risk": "low",
                    },
                },
                "attempts": attempts,
                "status": "implementable",
                "links": [],
            }
        ],
    }
    path = tmp_path / f"{slug}.html"
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def _node() -> TaskNode:
    return TaskNode(
        id="raise-check",
        goal="resolve at the raised class once the section has been tried",
        plan="section-contract-and-computed-impl",
        section="§5",
    )


def _resolve_section_routing(
    resolved: flight.ResolvedFlight, *, node: TaskNode, plan_path: Path
) -> dict:
    """Resolve through the section entry point, imported where it is exercised.

    A revision without the raise resolves nothing here, and the import fails
    inside the test that needs the behaviour rather than at collection time.
    """
    from reckon.crew.routing import resolve_section_routing

    return resolve_section_routing(resolved.config, node=node, plan_path=plan_path)


def _config_home_flight_identity() -> tuple | None:
    home = Path(flight.host_config_path())
    try:
        stat = home.stat()
    except OSError:
        return None
    return (home, stat.st_mtime_ns, stat.st_size)


def test_shipped_defaults_declare_the_raise_threshold_and_its_slot():
    defaults = yaml.safe_load(SHIPPED_DEFAULTS.read_text(encoding="utf-8"))
    rule = defaults.get("capability_raise") or {}
    assert rule.get("attempts_threshold") == 3
    assert rule.get("raised_class") == "orchestrator"
    assert rule.get("raised_reasoning") == "deep"
    assert rule.get("raised_verification") == "strict"
    linkml = yaml.safe_load(SHIPPED_LINKML.read_text(encoding="utf-8"))
    assert "attempts_threshold" in linkml["classes"]["CapabilityRaise"]["slots"]
    assert "capability_raise" in linkml["classes"]["FlightConfig"]["slots"]
    assert "by_capability_class" in linkml["classes"]["RoleConfig"]["slots"]


def test_section_at_the_threshold_resolves_at_the_raised_class_and_names_it(tmp_path):
    before = _config_home_flight_identity()
    config = _resolve_temporary_layers(tmp_path)

    payload = _resolve_section_routing(
        config, node=_node(), plan_path=_plan(tmp_path, attempts=3)
    )

    assert payload["attempts"] == 3
    assert payload["backend"] == "raised-lane"
    assert payload["backend_settings"]["effort"] == "raised-effort"
    assert payload["capability"]["class"] == "orchestrator"
    assert payload["capability"]["requirements"]["reasoning"] == "deep"
    assert payload["capability"]["requirements"]["verification"] == "strict"

    raise_record = payload["raise"]
    assert raise_record is not None
    assert raise_record["attempts"] == 3
    assert raise_record["threshold"] == 3
    assert raise_record["before"]["class"] == "general"
    assert raise_record["changes"] == [
        {"field": "class", "from": "general", "to": "orchestrator"},
        {"field": "reasoning", "from": "standard", "to": "deep"},
        {"field": "verification", "from": "standard", "to": "strict"},
    ]
    assert "raise" in payload["summary"].lower()
    assert "3" in payload["summary"]
    assert "raised-lane" in payload["summary"]
    assert _config_home_flight_identity() == before


def test_section_below_the_threshold_resolves_unraised_and_names_nothing(tmp_path):
    config = _resolve_temporary_layers(tmp_path)

    payload = _resolve_section_routing(
        config, node=_node(), plan_path=_plan(tmp_path, attempts=2)
    )

    assert payload["attempts"] == 2
    assert payload["raise"] is None
    assert payload["backend"] == "section-lane"
    assert "effort" not in payload["backend_settings"]
    assert payload["capability"]["class"] == "general"
    assert payload["capability"]["requirements"]["verification"] == "standard"
    summary = payload["summary"].lower()
    assert "rais" not in summary
    assert "orchestrator" not in summary
    assert "3" not in summary


def test_a_section_already_above_the_rule_records_no_move(tmp_path):
    config = _resolve_temporary_layers(tmp_path)

    payload = _resolve_section_routing(
        config,
        node=_node(),
        plan_path=_plan(
            tmp_path,
            attempts=4,
            capability_class="orchestrator",
            reasoning="deep",
            verification="strict",
        ),
    )

    assert payload["capability"]["class"] == "orchestrator"
    assert payload["raise"]["attempts"] == 4
    assert payload["raise"]["changes"] == []
    assert "already at orchestrator" in payload["summary"]
    assert "4" in payload["summary"]


def test_a_level_below_the_rule_moves_without_lowering_a_higher_one(tmp_path):
    config = _resolve_temporary_layers(tmp_path)

    payload = _resolve_section_routing(
        config,
        node=_node(),
        plan_path=_plan(tmp_path, attempts=3, capability_class="orchestrator"),
    )

    assert payload["capability"]["class"] == "orchestrator"
    assert payload["raise"]["changes"] == [
        {"field": "reasoning", "from": "standard", "to": "deep"},
        {"field": "verification", "from": "standard", "to": "strict"},
    ]
    assert payload["backend"] == "raised-lane"


def test_the_raised_rule_comes_from_the_shipped_default_layer(tmp_path):
    config = _resolve_temporary_layers(tmp_path)

    assert config.provenance["capability_raise.attempts_threshold"] == "shipped"
    assert config.provenance["backends.section-lane.launch"] == "host"
    assert config.provenance[
        "roles.implement.by_capability_class.orchestrator.backend"
    ] == ("host")
