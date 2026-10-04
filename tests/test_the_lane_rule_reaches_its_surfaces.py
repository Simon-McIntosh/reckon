"""Check the lane warning where a coordinator chooses or inspects a route."""

from pathlib import Path

from click.testing import CliRunner

from reckon import cli, mcp
from reckon.crew.dispatch import _dispatch_orchestrator_lane_stop

ROOT = Path(__file__).resolve().parents[1]
CLAUSES = (
    "do not dispatch background work to an orchestrator lane",
    "it runs the orchestrators",
    "background work there costs orchestrator capacity",
    "saturating it stops every session rather than one node",
)


def test_every_lane_choice_surface_states_the_rule_and_reason() -> None:
    help_result = CliRunner().invoke(cli.main, ["crew", "dispatch", "--help"])
    assert help_result.exit_code == 0
    stop = _dispatch_orchestrator_lane_stop(
        backend_name="shared",
        backend={"serves_orchestrators": True},
        config={"backends": {}},
        role="implement",
        spec_level="exact",
    )["detail"]
    surfaces = {
        "dispatch help": help_result.output,
        "orchestrator lane stop": stop,
        "build skill": (ROOT / "skills/reckon-build/SKILL.md").read_text(),
        "sprint skill": (ROOT / "skills/reckon-sprint/SKILL.md").read_text(),
        "lane routing reference": (
            ROOT / "skills/reckon-build/references/lane-routing.md"
        ).read_text(),
        "MCP crew tool": mcp._crew.__doc__ or "",
    }
    for name, surface in surfaces.items():
        normalized = " ".join(surface.lower().split())
        assert all(clause in normalized for clause in CLAUSES), name
    assert "(references/lane-routing.md)" in surfaces["build skill"]
    assert "(../reckon-build/references/lane-routing.md)" in surfaces["sprint skill"]
