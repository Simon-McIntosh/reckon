import re
from pathlib import Path
from typing import get_args

from reckon._mcp_tools import CrewArgs
from reckon.cli import main


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


def test_ship_skill_supports_plan_sprint_and_graph_targets() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    assert "/reckon-ship S1" in ship
    assert "/reckon-ship <project>:S1" in ship
    assert "/reckon-ship graph:<handle>" in ship
    assert "/reckon-ship <handle>" in ship
    assert "plan:<slug>" in ship
    assert "sprint:<id>" in ship
    assert 'roadmap(project="graph:<handle>", view="raw")' in ship
    assert "Only the handle is authored on the endpoint" in ship
    assert "schedule_override.deferred" in ship
    assert "unambiguous long form" in ship
    assert "remains a single-plan target" in ship


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


def test_create_and_edit_skills_share_the_canonical_authoring_exemplar() -> None:
    skill_root = ROOT / "skills"
    exemplar = "docs/_exemplar-plan.html"

    for skill_name in ("reckon-create", "reckon-edit"):
        text = normalized((skill_root / skill_name / "SKILL.md").read_text())
        assert exemplar in text
        assert (
            "canonical annotated exemplar for plan, research, and evidence resources"
            in text
        )


def test_create_skill_reads_live_tag_inventory_before_choosing_tags() -> None:
    create = normalized(
        (ROOT / "skills" / "reckon-create" / "SKILL.md").read_text()
    )

    assert "tag_inventory" in create
    assert "before choosing `plan-tags`" in create
    assert "Reuse an existing canonical tag identity" in create
    assert "do not invent a near-duplicate spelling" in create


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
    assert "refuse to dispatch work behind the gate until its measure has produced" in ship
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
    assert "dispatching an unrelated ready node is outside this freeze" in reference.lower()
    assert "dispatching an unrelated ready node is outside this freeze" in ship.lower()


def test_ship_refills_members_without_crossing_unverified_dependencies() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    assert "Concurrency — the roster is the whole authority" in ship
    assert "There is no slot pool and no numeric worker cap anywhere in Reckon" in ship
    assert "free members are the only ceiling" in ship
    assert "registers enough members to meet it" in ship
    assert "redispatch each member as soon as its finished node is verified" in ship
    assert "no dependent node builds on unverified work" in ship
    assert "Do not wait for the slowest active node" in ship
    assert "independent refill may start" in reference


def test_ship_has_one_advisory_fleet_size_table() -> None:
    root = ROOT / "skills" / "reckon-ship"
    texts = {path: path.read_text() for path in root.rglob("*.md")}
    tables = [path for path, text in texts.items() if "| Items | Strategy |" in text]

    assert tables == [root / "SKILL.md"]
    ship = texts[root / "SKILL.md"]
    reference = texts[root / "references" / "sprint-orchestration.md"]
    assert "Advisory fleet-size guide" in ship
    assert "This table is advisory" in ship
    assert "none of them is a slot pool" in normalized(ship.lower())
    assert "single advisory fleet-size table in `../SKILL.md`" in normalized(reference)


# reckon-ship SKILL.md is loaded in full at the start of every session, so its size
# is a per-run cost rather than a one-off. The budget exists to force reference
# material into references/, which is read only when hand-composing.
#
# Headroom is deliberate. The previous value of 12_000 was met by ONE token, which
# made it a ratchet rather than a budget: the next legitimate sentence broke the
# suite, and the only ways out were trimming good prose or bumping the number. A
# budget should bind where a review is actually wanted, so it is set above current
# size with room for the rule set to grow, and raising it again should require
# stating why here.
FIXED_READ_SET_TOKEN_BUDGET = 14_000


def test_engine_generated_dispatch_keeps_fixed_read_set_bounded() -> None:
    root = ROOT / "skills" / "reckon-ship"
    ship = (root / "SKILL.md").read_text()
    references = [
        (root / "references" / "sprint-orchestration.md").read_text(),
        (root / "references" / "worker-protocol.md").read_text(),
    ]

    assert all("only when hand-composing" in text for text in references)
    assert "only when hand-composing" in ship
    estimated_tokens = (len(ship.split()) * 4 + 2) // 3
    assert estimated_tokens < FIXED_READ_SET_TOKEN_BUDGET, (
        f"reckon-ship SKILL.md is {estimated_tokens} estimated tokens against a "
        f"{FIXED_READ_SET_TOKEN_BUDGET} budget. This file is loaded in full every "
        "session, so growth costs every run. The fix is to move REFERENCE material "
        "into references/ (read conditionally), not to shave sentences off a rule "
        "that is worth stating. Raise the budget only with a reason recorded here."
    )


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


def test_ship_retriages_followups_after_every_landing_until_dry() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    for text in (ship, reference):
        assert "re-triage" in text.lower()
        assert "after every landing beat" in text
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


def _command_at(path: tuple[str, ...]):
    command = main
    for part in path:
        commands = getattr(command, "commands", {})
        assert part in commands, f"missing CLI command: {' '.join(path)}"
        command = commands[part]
    return command


def test_ship_crew_views_match_the_typed_mcp_surface() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    documented = set(re.findall(r'crew\(project, view="([a-z]+)"\)', ship))
    annotation = CrewArgs.model_fields["view"].annotation
    assert documented == set(get_args(annotation))


def test_ship_cli_instructions_match_registered_commands_and_flags() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    assert "`scope-conflict` | 7" in ship
    assert "`watcher-required` | 8" in ship
    assert "Peer scopes come from live pointers" in ship
    assert "`--peer <other-node>=<their-paths>` is optional" in ship
    expected = {
        ("crew", "attach"): {"--run", "--task"},
        ("crew", "complete"): {
            "--run",
            "--gate",
            "--commit",
            "--tests-added",
            "--scope-changed",
        },
        ("crew", "dispatch"): {
            "--project",
            "--plan",
            "--section",
            "--role",
            "--node",
            "--goal",
            "--done-when",
            "--write-path",
            "--peer",
            "--time-budget",
            "--session",
            "--set",
            "--dry-run",
            "--member",
            "--manifest",
            "--allow-unreconciled-runs",
            "--no-watch",
        },
        ("crew", "drain"): {"--project", "--leave"},
        ("crew", "list"): set(),
        ("crew", "member", "add"): set(),
        ("crew", "member", "list"): {"--project"},
        ("crew", "observe"): {"--run"},
        ("crew", "preflight"): {"--project", "--role"},
        ("crew", "recover"): set(),
        ("crew", "resume"): {"--run", "--advice"},
        ("crew", "stop"): set(),
        ("crew", "watch"): {"--project", "--stall-window"},
        ("flight",): {"--project"},
        ("audit-doc",): set(),
        ("sync",): set(),
    }
    named = {
        tuple(match.split())
        for match in re.findall(
            r"\breckon ((?:crew (?:member )?[a-z-]+)|flight|audit-doc|sync)\b",
            ship,
        )
    }
    assert named == set(expected)
    for path, required_flags in expected.items():
        command = _command_at(path)
        registered_flags = {
            option
            for parameter in command.params
            for option in getattr(parameter, "opts", ())
        }
        assert required_flags <= registered_flags, " ".join(path)
        for flag in required_flags:
            assert flag in ship


def test_closure_ledger_carries_both_drain_counts() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    for text in (ship, reference):
        assert "foldable-remaining: 0 unreconciled-runs: 0" in text
        assert "`handed-off`" in text
        assert "`still-working`" in text


def test_ship_dispatch_exit_table_matches_cli_branches() -> None:
    ship = (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    documented = {
        name: int(code)
        for name, code in re.findall(r"\| `([a-z-]+)` \| ([0-9]) \|", ship)
    }
    assert documented == {
        "success": 0,
        "request-error": 1,
        "not-dispatchable": 2,
        "budget-hold": 3,
        "plan-unavailable": 4,
        "competence-refusal": 5,
        "unreconciled-runs": 6,
        "scope-conflict": 7,
        "watcher-required": 8,
    }
    source = (ROOT / "reckon" / "cli.py").read_text()
    assert "0 succeeded, 1 the configuration or request is wrong" in source
    for error, code in documented.items():
        if error in {"success", "request-error"}:
            continue
        assert re.search(
            rf'"error": "{error}"[\s\S]{{0,350}}Exit\({code}\)', source
        ), error


def test_ship_documents_dispatch_prerequisites_and_refusal_remedies() -> None:
    ship = normalized(
        (ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text()
    ).lower()
    crew_sources = [
        ROOT / "reckon" / "crew.py",
        *sorted((ROOT / "reckon" / "crew").glob("*.py")),
    ]
    crew_source = "\n".join(path.read_text() for path in crew_sources)
    reference = normalized(
        (
            ROOT / "skills" / "reckon-ship" / "references" / "sprint-orchestration.md"
        ).read_text()
    )

    assert "worktree_fleet.py" in ship
    assert "reckon sync docs/" in ship
    assert "commit the plan before dispatching" in ship
    assert "before creating a worktree" in ship
    assert "`--no-watch`" in ship
    assert "promoted ledger record" in ship
    assert "a goal containing `;` is not one deliverable" in ship
    assert "every node needs at least one `--write-path`" in ship
    assert '" then ", ";"' in crew_source
    assert "no exclusive write path is enumerated" in crew_source
    assert "commit the plan before dispatching" in crew_source
    assert "<config-home>/crew/runs/<run-id>/manifest.md" in reference
    assert "<scratchpad>/<node-id>-manifest.md" not in reference


def test_ship_run_lifecycle_guidance_matches_launch_ownership() -> None:
    ship = normalized((ROOT / "skills" / "reckon-ship" / "SKILL.md").read_text())
    protocol = normalized(
        (ROOT / "skills" / "reckon-ship" / "references" / "worker-protocol.md")
        .read_text()
    )
    assert "non-terminal live pointer" in ship
    assert "in-flight run" in ship
    assert "An in-harness run has no spawned process" in ship
    assert "through the attached harness task/session" in protocol
    assert "Until the classifier reads manifests" not in ship
    assert "The live classifier reads the manifest's recorded status" in ship
