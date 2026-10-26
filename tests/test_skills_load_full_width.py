"""The coordinator loads declared section waits into the queue in one beat."""

import re
from pathlib import Path

from tests.test_skill_contracts import normalized

ROOT = Path(__file__).parents[1]
BUILD = ROOT / "skills/reckon-build/SKILL.md"
REFERENCE = ROOT / "skills/reckon-build/references/sprint-orchestration.md"
ROADMAP = ROOT / "skills/reckon-roadmap/SKILL.md"
GUIDANCE = ROOT / "AGENTS.md"


def test_dispatch_set_includes_sections_waiting_for_upstream() -> None:
    build = normalized(BUILD.read_text())
    reference = normalized(REFERENCE.read_text())

    assert "dispatch all of them in one beat" in build
    assert "naming no lane so the picker routes each request" in build
    assert "The dispatch set includes sections whose upstream has not landed" in build
    assert (
        "leave out only deferred sections, done sections, and sections with a live run"
        in build
    )
    assert "parks its node `awaiting-upstream`" in build
    assert "dispatch all of them in one beat" in reference
    assert (
        "The dispatch set includes sections whose upstream has not landed" in reference
    )
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

    build = BUILD.read_text()
    load_rule = build.split(
        "**Load the plan's full width after its review round is answered.**", 1
    )[1].split("Each implementation dispatch carries", 1)[0]
    assert (
        "Read progress through `crew view live` and steer waiting requests "
        "with the three queue verbs below."
    ) in normalized(load_rule)
    for verb in ("edit", "reprioritise", "drop"):
        assert build.count(f"reckon crew queue {verb}") == 1
    assert "metered request still `awaiting-upstream` or `queued`" in build
    assert "must be dropped and dispatched again" in build
