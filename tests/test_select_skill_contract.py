"""The present-the-decisions skill's contract: discovered, cited, and named.

These are the checks a reader meets the skill by. They import nothing from the
presentation module on purpose: this file must be runnable against a tree that
carries no skill and no presentation at all, so the negative control — the base
revision, where the skill does not exist — fails as a test rather than as a
collection error.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_cited_commands_exist import _citation_problems, citations
from tests.test_skill_contracts import normalized

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / "skills" / "reckon-select"
SKILL = SKILL_DIR / "SKILL.md"
SHIP = ROOT / "skills" / "reckon-build" / "SKILL.md"


def test_the_select_skill_is_discovered_beside_the_other_reckon_skills() -> None:
    """The installer's own enumeration reaches it: this is the listing a skill
    is shipped by, not a stray directory beside them."""
    from reckon.project_maintenance_commands import _skills_source

    names = sorted(
        path.name
        for path in _skills_source().iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    assert "reckon-select" in names, names
    assert SKILL.is_file()


def test_every_command_the_select_skill_cites_resolves() -> None:
    """Each cited verb chain and flag resolves against the CLI's own --help."""
    assert SKILL.is_file(), f"{SKILL} does not exist, so it cites no command"
    relative = str(SKILL.relative_to(ROOT))
    cited = [cite for cite in citations(ROOT) if cite.path == relative]
    assert cited, "the skill cites no reckon command, so the check is vacuous"
    assert _citation_problems(cited) == []


def test_the_decisions_surface_carries_the_presenting_verbs() -> None:
    """The ship skill's inventory carries the verbs, so the crew-verb census
    finds them documented rather than exempt, and a reader at the decisions
    read is told which verb presents them."""
    ship = normalized(SHIP.read_text(encoding="utf-8"))
    assert "reckon crew decision present --session" in ship
    assert "reckon crew decision choose" in ship
    assert "reckon crew decision release" in ship


def test_the_select_skill_names_the_presenting_session_and_the_choice_fields() -> None:
    """The body teaches the three things a reader cannot guess: the presenting
    session answers, a claim is what keeps two presenters apart, and a
    plan-block decision reports its fields as absent rather than empty."""
    body = normalized(SKILL.read_text(encoding="utf-8"))
    assert "presenting session" in body
    assert "claim" in body.lower()
    assert "absent" in body
    assert "unreached" in body
