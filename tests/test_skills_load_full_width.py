"""The coordinator loads declared section waits into the queue, then steers it."""

import re
from pathlib import Path

from tests.test_skill_contracts import normalized

ROOT = Path(__file__).parents[1]
BUILD = ROOT / "skills/reckon-build/SKILL.md"
REFERENCE = ROOT / "skills/reckon-build/references/sprint-orchestration.md"
EDIT = ROOT / "skills/reckon-edit/SKILL.md"
ROADMAP = ROOT / "skills/reckon-roadmap/SKILL.md"
GUIDANCE = ROOT / "AGENTS.md"


def test_dispatch_set_includes_sections_waiting_for_upstream() -> None:
    build = normalized(BUILD.read_text())
    reference = normalized(REFERENCE.read_text())

    assert "queue all of them in one beat" in build
    assert "naming no lane so the picker routes each request" in build
    assert "The set may include sections whose upstream has not landed" in build
    assert "Never load a plan wholesale: choose each section" in build
    assert "parks its node `awaiting-upstream`" in build
    assert "queue all of them in one beat" in reference
    assert "naming no lane" in reference
    assert "The set may include sections whose upstream has not landed" in reference
    assert "parks `awaiting-upstream`" in reference


def test_build_skill_names_project_queue_bound() -> None:
    build = normalized(BUILD.read_text()).lower()

    assert "queue.max_requests" in build
    assert "its share" not in build
    assert "minimum of 2" not in build


def test_readiness_reports_do_not_narrow_the_dispatch_set() -> None:
    for path in (BUILD, REFERENCE, ROADMAP, GUIDANCE):
        words = normalized(path.read_text())
        assert not re.search(
            r"(?:dispatch set|dispatch every|load only|dispatch only)"
            r".{0,100}(?:dependency-ready|ready-idle)",
            words,
            flags=re.IGNORECASE,
        ), path

    assert "does not limit the build skill's one-beat load" in normalized(
        ROADMAP.read_text()
    )
    assert "including those whose upstream has not landed" in normalized(
        GUIDANCE.read_text()
    )


def test_waits_are_authored_and_queue_steering_is_discoverable() -> None:
    for skill in ("reckon-create", "reckon-edit"):
        words = normalized((ROOT / "skills" / skill / "SKILL.md").read_text()).lower()
        assert "whenever one section consumes another" in words
        assert "plan-section-depends-on" in words

    build = normalized(BUILD.read_text())
    assert "steer the queue rather than feeding it" in build
    for verb in ("edit", "hold", "release", "reprioritise", "drop"):
        assert f"reckon crew queue {verb} --run" in build
    assert "plan-review-owed" in build
    assert "crew dispatch --batch" in build


def test_reference_names_every_steering_verb() -> None:
    reference = normalized(REFERENCE.read_text())
    assert "crew dispatch --batch" in reference
    for verb in ("edit", "hold", "release", "reprioritise", "drop"):
        assert f"reckon crew queue {verb} --run" in reference
    assert "plan-review-owed" in reference


def test_edit_skill_states_the_queued_request_hold_rule() -> None:
    edit = normalized(EDIT.read_text())
    assert "An edit to a queued section holds only that section's queue request" in edit
    assert "plan-review-owed" in edit


def test_agents_md_names_the_batch_call() -> None:
    assert "crew dispatch --batch" in normalized(GUIDANCE.read_text())


def test_no_interim_metered_lane_caveat_remains() -> None:
    for path in sorted((ROOT / "skills").rglob("*.md")):
        words = normalized(path.read_text())
        assert "Until metered admission" not in words, path
        assert "must be dropped and dispatched again" not in words, path


def test_edit_skill_states_the_queued_request_hold_rule_uses_the_marked_hold() -> None:
    edit = normalized(EDIT.read_text())
    assert "`plan-review-owed` hold" in edit
    assert "releases the hold as soon as the review is stored" in edit
