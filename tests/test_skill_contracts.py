from pathlib import Path


ROOT = Path(__file__).parents[1]


def normalized(text: str) -> str:
    return " ".join(text.split())


def test_ship_skill_supports_plan_and_sprint_targets() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    assert "/reckon-ship S1" in ship
    assert "/reckon-ship <project>:S1" in ship
    assert "plan:<slug>" in ship
    assert "sprint:<id>" in ship


def test_ship_routing_is_model_neutral_and_worktree_first() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text().lower()
    reference = normalized(
        (ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md")
        .read_text()
        .lower()
    )
    assert "one-below" in ship
    assert "isolated worktrees by default" in ship
    assert "do not encode provider or model names" in reference
    assert "haiku" not in ship
    assert "sonnet" not in ship
    assert "opus" not in ship


def test_sprint_skill_hands_execution_to_ship() -> None:
    sprint = normalized((ROOT / "skills" / "reckon-sprint" / "SKILL.md").read_text())
    assert "This skill never dispatches workers" in sprint
    assert "/reckon-ship S1" in sprint


def test_skills_use_progressive_reads_by_intent() -> None:
    skill_root = ROOT / "skills"
    status = (skill_root / "reckon-status" / "SKILL.md").read_text()
    edit = (skill_root / "reckon-edit" / "SKILL.md").read_text()
    sprint = (skill_root / "reckon-sprint" / "SKILL.md").read_text()
    ship = (skill_root / "reckon-ship" / "SKILL.md").read_text()
    orchestration = (
        skill_root / "reckon-ship" / "references" / "sprint-orchestration.md"
    ).read_text()

    assert 'view="summary"' in status
    assert 'view="summary"' in sprint
    assert 'view="summary"' in orchestration
    assert 'view="raw"' in edit
    assert 'view="raw"' in sprint
    assert 'view="raw"' in ship
    assert 'view="schema"' in ship
    assert "resource={" in status
    assert "resource={" in edit
    assert "resource={" in ship
