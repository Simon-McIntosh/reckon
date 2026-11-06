"""The build and roadmap skills read the board's views, not a hand-joined report.

The coordination question is section-level: which sections are ready with no run
on them, whose are they, and what does the critical path do at its head. Each
fact now has one view that answers it. The skills name those views, so a
coordinator does not reconstruct the answer from a lossless report and an inline
join.
"""

import re
from pathlib import Path

from tests.test_skill_contracts import normalized

ROOT = Path(__file__).parents[1]
BUILD = ROOT / "skills/reckon-build/references/sprint-orchestration.md"
ROADMAP = ROOT / "skills/reckon-roadmap/SKILL.md"

# The stand-in both skills carried while the views held no section rows: read
# the lossless report and subtract the live runs inline. The views answer it
# now, so a surviving copy is the regression this file exists to catch.
RECIPE = re.compile(r"--json-output|python3 -c\s*'")

# One entry per view the two skills must name, and the field that makes the
# name a use rather than a mention.
VIEWS = (
    ("ready-set filter", "ready_idle"),
    ("critical-path fields", "primary_state"),
    ("critical-path head", "head_note"),
    ("directory view", 'crew(view="directory"'),
    ("lanes view", 'crew(view="lanes"'),
    ("lane share", "session_share"),
)


def test_build_skill_names_the_board_views() -> None:
    text = normalized(BUILD.read_text())

    for name, token in VIEWS:
        assert token in text, f"the build skill does not name the {name}"


def test_roadmap_skill_names_the_board_views() -> None:
    text = normalized(ROADMAP.read_text())

    for name, token in VIEWS:
        assert token in text, f"the roadmap skill does not name the {name}"


def test_neither_skill_carries_the_lossless_report_recipe() -> None:
    for path in (BUILD, ROADMAP):
        text = normalized(path.read_text())
        assert not RECIPE.search(text), (
            f"{path} still hands the coordinator the retired recipe"
        )


def test_the_ready_idle_set_is_a_filter_on_the_ready_set_view() -> None:
    """The set is the view filtered, not a second projection of the same rows."""

    roadmap = normalized(ROADMAP.read_text())

    assert "ready-set view, filtered to rows with no live run" in roadmap
    assert "pages with the same cursor" in roadmap
