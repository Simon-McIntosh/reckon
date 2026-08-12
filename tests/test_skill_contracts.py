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
            if not path.is_file() or path.suffix not in (
                ".py",
                ".md",
                ".yaml",
                ".json",
            ):
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


def test_ship_gate_fence_reads_computed_state() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    assert "computed gate state" in ship
    assert "`read_plan` and `roadmap`" in ship
    assert "returned `blocking` and `gate_blockers`" in ship
    assert '`crew(project, view="flight")`' in ship
    assert "resolved `gates.enforce`" in ship
    assert "strict enforcement refuses" in ship
    assert "advisory enforcement records a warning" in ship
    assert "evidence-gate table" not in ship


def test_ship_skill_carries_the_four_axis_summary_reflex() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    for axis in ("WHAT", "WHY", "HOW", "WHEN"):
        assert f"\n{axis}   " in ship
    assert "`WHY` axis carries that gate's evidence" in ship
    # The same discipline covers a held wave: a hold without a figure is not a
    # report a lead can act on, so WHY is quantitative on both occasions.
    assert "at completion or a hold it carries the figure" in ship


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


# Primitives and commands that belong to one HOST harness the orchestrator runs
# inside. Naming any of them in the skill or a process reference couples the whole
# skill to that host, which is the portability the single-skill design preserves.
HARNESS_LOCAL_PRIMITIVES = re.compile(
    r"run_in_background|ScheduleWakeup|CronCreate|CronList|TaskOutput"
    r"|\bcodex (app-server|exec|resume|fork)\b|\bclaude -p\b|--output-format",
)

# The four capabilities an orchestrator's behaviour actually turns on. Each host
# file states all four, so a capability this host lacks is recorded as absent
# rather than left out — an omission reads as "not investigated".
HOST_CAPABILITIES = (
    "Background dispatch",
    "Wake on completion",
    "Self-scheduling",
    "Budget visibility to itself",
)


def _harness_reference_dir():
    return ROOT / "skills" / "reckon-ship" / "references" / "orchestrator-harness"


def test_harness_local_primitives_appear_only_in_the_host_references() -> None:
    """The quarantine, asserted rather than trusted to reviewer attention."""
    quarantined = _harness_reference_dir()
    offenders: list[str] = []
    for path in sorted((ROOT / "skills").rglob("*.md")):
        if quarantined in path.parents:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if HARNESS_LOCAL_PRIMITIVES.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == [], (
        "harness-local primitives leaked out of orchestrator-harness/:\n"
        + "\n".join(offenders)
    )


def test_every_host_reference_records_all_four_capabilities() -> None:
    """Two hosts minimum, and neither may stay silent about what it cannot do."""
    files = sorted(_harness_reference_dir().glob("*.md"))
    assert len(files) >= 2, "a second host file is what proves the shape generalises"
    for path in files:
        text = path.read_text()
        for capability in HOST_CAPABILITIES:
            assert capability in text, f"{path.name} does not state {capability!r}"
        assert "Ownership test" in text, path.name


def test_ship_skill_authors_the_budget_fence_alone() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    assert "Authored here and nowhere else" in ship
    assert "reckon crew preflight" in ship
    assert "Unknown never holds" in ship
    assert "orchestrator-harness/<harness>.md" in ship
    others = [
        path
        for path in (ROOT / "skills").rglob("SKILL.md")
        if path.parent.name != "reckon-ship"
    ]
    for path in others:
        assert "budget fence" not in path.read_text().lower(), path


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


def test_ship_writes_plan_state_in_the_same_beat_as_run_promotion() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    assert "Immediately after EACH `reckon crew complete`" in ship
    assert "Immediately after each `reckon crew complete`" in reference
    assert "Do not promote another run" in reference


def test_ship_advances_implementation_for_every_node_landing() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())

    assert "count of completed executable nodes" in ship
    assert "count of total executable nodes" in ship
    assert "Set it on EVERY node landing" in ship
    assert "never wait for section closure to record earlier nodes" in ship


def test_ship_landing_state_carries_commit_and_gate_measure_with_impl() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    assert '"path": "impl"' in ship
    assert '"path": "commits"' in ship
    assert '"target": "comments"' in ship
    assert "gate <gate-name> <verdict>" in ship
    assert "quantitative measure" in reference
    assert "one version-safe state write" in reference


def test_ship_keeps_the_orchestrator_as_the_only_plan_state_writer() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    assert "the orchestrator writes this node's commit" in ship
    assert "Workers still only return outcome data" in ship
    assert "Workers return outcome data in their manifests" in reference
    assert "never write shared plan or index state" in reference


def test_ship_turns_same_plan_follow_on_work_into_sections() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    assert "Same-plan follow-on work becomes a section, never a followup" in ship
    assert "Work owned by the plan in hand becomes a new evergreen section" in reference
    assert (
        "Do not set a terminal status while a same-plan section remains open"
        in reference
    )


def test_ship_retriages_followups_after_every_wave_until_dry() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    for text in (ship, reference):
        assert "re-triage" in text.lower()
        assert "after every wave" in text
        assert "complete pass finds nothing foldable" in text
        assert "fixed pass count" in text


def test_ship_enumerates_the_only_open_followup_exemptions() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    for text in (ship, reference):
        for exemption in ("authority-required", "dissent-reopen", "foreign-owner"):
            assert f"`{exemption}`" in text
        assert "spend, an outward-facing effect, or an irreversible" in text
        assert "asks to reopen a locked decision" in text
        assert (
            "different plan or repository" in text
            or "another plan or repository" in text
        )


def test_manifest_follow_ons_enter_the_open_followup_triage_loop() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    sentence = "Manifest `follow_ons` enter the same triage loop as open plan followups"
    assert sentence in ship
    assert sentence in reference


def test_terminal_status_waits_for_the_followup_drain() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    for text in (ship, reference):
        assert "Do not set" in text
        assert "`shipped` or `done` while" in text
        assert "foldable followup is open" in text


def test_exempt_open_followup_records_its_claim() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    for text in (ship, reference):
        assert "exempt open followup" in text.lower()
        assert "record" in text.lower()
        assert "which exemption it claims" in text


def test_edit_skill_uses_version_safe_prose_tool() -> None:
    edit = (ROOT / "skills" / "reckon-edit" / "SKILL.md").read_text()

    assert 'mode="text"' in edit
    assert "old_html must occur exactly once" not in edit
    assert "requires exactly one match" in edit
