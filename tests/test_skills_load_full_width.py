"""The coordinator loads declared section waits into the queue in one beat."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
BUILD = ROOT / "skills/reckon-build/SKILL.md"
REFERENCE = ROOT / "skills/reckon-build/references/sprint-orchestration.md"
ROADMAP = ROOT / "skills/reckon-roadmap/SKILL.md"
GUIDANCE = ROOT / "AGENTS.md"


def _words(path: Path) -> str:
    return " ".join(path.read_text().split())


def test_dispatch_set_includes_sections_waiting_for_upstream() -> None:
    build = _words(BUILD)
    reference = _words(REFERENCE)

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


def test_readiness_reports_do_not_narrow_the_dispatch_set() -> None:
    for path in (BUILD, REFERENCE, ROADMAP, GUIDANCE):
        words = _words(path)
        assert not re.search(
            r"(?:dispatch set|dispatch every|load only|dispatch only)"
            r".{0,100}(?:dependency-ready|ready-idle)",
            words,
            flags=re.IGNORECASE,
        ), path

    assert "does not limit the build skill's one-beat load" in _words(ROADMAP)
    assert "including those whose upstream has not landed" in _words(GUIDANCE)


def test_waits_are_authored_and_queue_steering_is_discoverable() -> None:
    for skill in ("reckon-create", "reckon-edit"):
        words = _words(ROOT / "skills" / skill / "SKILL.md").lower()
        assert "whenever one section consumes another" in words
        assert "plan-section-depends-on" in words

    build = BUILD.read_text()
    for verb in ("edit", "reprioritise", "drop"):
        assert build.count(f"reckon crew queue {verb}") == 1
    assert "metered request still `awaiting-upstream` or `queued`" in build
    assert "must be dropped and dispatched again" in build
