import re
from pathlib import Path


ROOT = Path(__file__).parents[1]


def normalized(text: str) -> str:
    return " ".join(text.split())


# A concrete model identifier or a provider name. Harness command names are
# matched only where a version-like suffix makes them a model rather than a tool.
ROUTING_IDENTIFIERS = re.compile(
    r"(gpt|claude|llama|mistral|gemini)[-_ ]?[0-9]|anthropic|openai"
    r"|\b(sonnet|opus|haiku)\b",
    re.IGNORECASE,
)

# Translation must name a harness to speak its flags, and the legacy tier map
# reads identifiers out of existing plan state without ever selecting a worker.
LEAKAGE_EXEMPT = {
    Path("reckon/_backends.py"),
    Path("reckon/capability.py"),
}


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


def test_followup_handoffs_are_single_line_plan_invocations() -> None:
    edit = (ROOT / "skills" / "reckon-edit" / "SKILL.md").read_text().lower()
    create = (ROOT / "skills" / "reckon-create" / "SKILL.md").read_text().lower()
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text().lower()
    for text in (edit, create, ship):
        assert "/reckon-ship <slug> [§n]" in text
    assert "is exactly one line" in edit
    assert "contains exactly one line" in create
    assert "is one line" in ship
    assert "the live plan owns all semantic guidance" in ship


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


def test_no_routing_identifier_leaks_into_skills_or_source() -> None:
    """Routing is data, so no model or provider may be named in what agents read.

    The two exemptions are the translation module, which has to speak a harness's
    flags, and the legacy tier map, which reads identifiers out of plan state
    written before capability requests existed and never selects a worker with
    them.
    """
    offenders: list[str] = []
    for directory in ("skills", "reckon"):
        for path in sorted((ROOT / directory).rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".md", ".yaml", ".json"):
                continue
            relative = path.relative_to(ROOT)
            if relative in LEAKAGE_EXEMPT:
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if ROUTING_IDENTIFIERS.search(line):
                    offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == [], "routing identifiers leaked:\n" + "\n".join(offenders)


def test_ship_skill_carries_the_uniform_dispatch_instruction() -> None:
    """One instruction for every backend, and one branch on launch kind."""
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    assert "reckon crew dispatch" in ship
    assert "reckon crew attach" in ship
    assert "reckon crew observe" in ship
    assert "in-harness" in ship
    assert "Branch once, on the returned `launch` kind" in ship


def test_ship_skill_owns_the_pre_dispatch_checklist() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    for prop in (
        "Single goal",
        "Fully specified",
        "Demonstrable",
        "Closed",
        "Scoped",
        "Bounded",
        "Independently verifiable",
    ):
        assert prop in ship
    assert "--dry-run" in ship


def test_ship_skill_authors_the_gate_fence_rule_alone() -> None:
    """Two plans claiming one rule is how a worker rewrites a peer's text."""
    skill_root = ROOT / "skills"
    ship = normalized((skill_root / "reckon-ship" / "SKILL.md").read_text())
    assert "Authored here and nowhere else" in ship
    assert "refuse to open the next wave until the gate's measure has produced" in ship
    others = [
        path
        for path in skill_root.rglob("SKILL.md")
        if path.parent.name != "reckon-ship"
    ]
    for path in others:
        assert "gate fence" not in path.read_text().lower(), path


def test_ship_skill_carries_the_four_axis_summary_reflex() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    for axis in ("WHAT", "WHY", "HOW", "WHEN"):
        assert f"\n{axis}   " in ship
    assert "at completion it carries the gate evidence" in ship


def test_worker_protocol_owns_the_backend_independent_contract() -> None:
    protocol = (
        ROOT / "skills" / "reckon-ship" / "references" / "worker-protocol.md"
    ).read_text()
    assert "NEEDS-HELP:" in protocol
    for field in ("tried:", "options:", "leaning:", "cost-if-wrong:"):
        assert field in protocol
    for fence in ("Scope", "Time", "Evidence", "Delivery"):
        assert f"| {fence} |" in protocol
    assert "follow_ons" in protocol
    assert "reckon crew resume" in protocol


def test_worker_backends_reference_stays_about_mechanics() -> None:
    """The ownership test for the maintainer note, asserted rather than trusted."""
    backends = (
        ROOT / "skills" / "reckon-ship" / "references" / "worker-backends.md"
    ).read_text()
    assert "Not agent-facing" in backends
    assert "Ownership test" in backends
    for topic in ("session", "observation", "budget", "launch"):
        assert topic in backends.lower()


def test_continuation_closes_at_three_altitudes_in_the_skills() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    orchestration = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )
    assert "THREE altitudes" in ship
    assert "feeds_sprints" in ship
    assert "report the sprints this one feeds" in orchestration.lower()


def test_edit_skill_uses_version_safe_prose_tool() -> None:
    edit = (ROOT / "skills" / "reckon-edit" / "SKILL.md").read_text()

    assert 'mode="text"' in edit
    assert "old_html must occur exactly once" not in edit
    assert "requires exactly one match" in edit
