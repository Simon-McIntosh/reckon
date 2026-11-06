"""The build and roadmap skills read the board's views, not a hand-joined report.

The coordination question is section-level: which sections are ready with no run
on them, whose are they, and what does the critical path do at its head. Each
fact has one read that serves it. These cases assert the contract behind that
claim rather than its wording: a view a skill names is one the crew surface
actually serves, and a set a skill names is one a coordinator can reach with a
call it can make.
"""

import inspect
import re
from pathlib import Path

from reckon import mcp_views
from tests.test_skill_contracts import normalized

ROOT = Path(__file__).parents[1]
BUILD = ROOT / "skills/reckon-build/references/sprint-orchestration.md"
ROADMAP = ROOT / "skills/reckon-roadmap/SKILL.md"
SKILLS = (BUILD, ROADMAP)

# The stand-in both skills carried while the views held no section rows: read
# the lossless report and subtract the live runs inline. The views answer it
# now, so a surviving copy is the regression this file exists to catch.
RECIPE = re.compile(r"--json-output|python3 -c\s*'")

# A crew call a skill tells a coordinator to make. The surface refuses any view
# name outside CREW_VIEWS, so a skill naming one leaves the coordinator with a
# call that cannot be made.
CREW_CALL = re.compile(r'crew\(view="([a-z][a-z-]*)"')

# The field the served crew view carries the filtered set under. A statement of
# the set that names neither this field nor a call reaching it asks the
# coordinator to obtain rows from a view no surface serves.
SET_FIELD = "ready_idle_sections"
SET_CALL = 'crew(view="directory"'

# One entry per fact the two skills must name, and the field that makes the
# name a use rather than a mention.
FACTS = (
    ("ready-idle filter", "ready_idle"),
    ("critical-path fields", "primary_state"),
    ("critical-path head", "head_note"),
    ("directory view", SET_CALL),
    ("lanes view", 'crew(view="lanes"'),
    ("lane share", "session_share"),
)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.;])\s+", text) if part.strip()]


def test_build_skill_names_the_board_views() -> None:
    text = normalized(BUILD.read_text())

    for fact, token in FACTS:
        assert token in text, f"the build skill does not name the {fact}"


def test_roadmap_skill_names_the_board_views() -> None:
    text = normalized(ROADMAP.read_text())

    for fact, token in FACTS:
        assert token in text, f"the roadmap skill does not name the {fact}"


def test_neither_skill_carries_the_lossless_report_recipe() -> None:
    for path in SKILLS:
        text = normalized(path.read_text())
        assert not RECIPE.search(text), (
            f"{path} still hands the coordinator the retired recipe"
        )


def test_every_crew_view_a_skill_names_is_served() -> None:
    served = set(mcp_views.crew_view_names())

    for path in SKILLS:
        named = set(CREW_CALL.findall(normalized(path.read_text())))
        assert named, f"{path} names no crew view"
        assert named <= served, (
            f"{path} names crew views the surface refuses: {sorted(named - served)}"
        )


def test_the_ready_idle_rows_reach_a_served_view() -> None:
    """The projection has no view of its own, so the rows must reach one."""

    assert "ready_idle" in inspect.signature(mcp_views.ready_set_view).parameters

    serving = inspect.getsource(mcp_views.serve_crew_view)
    assert re.search(
        r"crew_directory\((?:[^()]|\([^()]*\))*?ready_idle=True", serving
    ), "no served crew view asks the directory for the ready-idle rows"


def test_a_skills_ready_idle_set_names_a_call_that_serves_it() -> None:
    """A statement of the set must reach a served call, not a view name."""

    for path in SKILLS:
        text = normalized(path.read_text())
        claims = [s for s in _sentences(text) if "ready-idle set" in s.lower()]
        assert claims, f"{path} never says what the ready-idle set is"
        for sentence in claims:
            assert SET_CALL in sentence or SET_FIELD in sentence, (
                f"{path} states the ready-idle set with no call that serves it: "
                f"{sentence}"
            )


NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}


def test_the_roadmap_inventory_matches_its_own_count() -> None:
    """A section that states how many views answer must list that many."""

    section = ROADMAP.read_text().split(
        "## Ready-idle sections, owners and the critical path", 1
    )[1]
    section = section.split("\n## ", 1)[0]
    stated = re.search(
        r"\b(two|three|four|five) views answer it\b", normalized(section)
    )
    assert stated, "the section does not state how many views answer it"
    listed = re.findall(r"(?m)^\d+\. \*\*", section)
    assert len(listed) == NUMBER_WORDS[stated.group(1)], (
        f"the section says {stated.group(1)} views and lists {len(listed)}"
    )
