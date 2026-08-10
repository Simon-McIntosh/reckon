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


def test_ship_routing_is_prompt_owned_and_worktree_first() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text().lower()
    reference = normalized(
        (ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md")
        .read_text()
        .lower()
    )
    assert "one-below" not in ship
    assert "isolated worktrees by default" in ship
    assert "runtime routing is prompt-owned" in reference
    assert "state the concrete model and effort" in reference
    assert "one-below" not in reference
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


def test_roadmap_skill_owns_dependency_analysis() -> None:
    skill_root = ROOT / "skills"
    roadmap = normalized((skill_root / "reckon-roadmap" / "SKILL.md").read_text())
    status = normalized((skill_root / "reckon-status" / "SKILL.md").read_text())
    sprint = normalized((skill_root / "reckon-sprint" / "SKILL.md").read_text())
    ship = normalized((skill_root / "reckon-ship" / "SKILL.md").read_text())

    assert 'roadmap(project="*")' in roadmap
    assert "Lifecycle completion" in roadmap
    assert "stored implementation" in roadmap
    assert "non-executable-hard-dependency" in roadmap
    assert "Do not reproduce its graph traversal" in status
    assert "Call `roadmap(project)` before and after" in sprint
    assert "canonical plan-level graph" in ship


def test_create_and_edit_skills_guard_repository_allocation() -> None:
    skill_root = ROOT / "skills"
    create = (skill_root / "reckon-create" / "SKILL.md").read_text()
    edit = normalized((skill_root / "reckon-edit" / "SKILL.md").read_text())

    assert "Repository ownership precedes path selection" in create
    assert "link actionable work to a sprint in the same session" in create
    assert "cross-project relocation" in edit
    assert "Never leave two canonical live plans" in edit


def test_edit_skill_uses_version_safe_prose_tool() -> None:
    edit = (ROOT / "skills" / "reckon-edit" / "SKILL.md").read_text()

    assert 'mode="text"' in edit
    assert "old_html must occur exactly once" not in edit
    assert "requires exactly one match" in edit
