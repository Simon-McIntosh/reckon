"""A live dispatch resolves the lane its plan section's record earns.

The plan section's typed record is the authority for both the capability the
work declares and the attempts it has cost, so the production dispatch path
must read it: a section attempted at or above the threshold resolves on the
raised class's lane and the dispatch summary names the count that caused it,
one below the threshold resolves exactly as role routing resolved it, and one
section reference has one identity -- the dotted, hyphenated and printed
spellings all address the record whose attempts decide the lane.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon._plan_html import write_state
from reckon.crew import promotion as promotion_module
from reckon.crew.routing import resolve_role, resolve_section_routing

PROJECT = "sample"
SLUG = "section-routing"
SPELLINGS = ("s5.1", "s5-1", "§5.1")

CONFIG = {
    "default_backend": "section-lane",
    "capability_raise": {
        "attempts_threshold": 3,
        "raised_class": "orchestrator",
        "raised_reasoning": "deep",
        "raised_verification": "strict",
    },
    "backends": {
        "section-lane": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        },
        "raised-lane": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        },
    },
    "roles": {
        "implement": {
            "backend": "section-lane",
            "by_capability_class": {
                "orchestrator": {"backend": "raised-lane", "effort": "raised-effort"}
            },
        }
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

AUTHORED = (
    '<h2 id="s5">§5 — A section at or above the raise threshold</h2>\n'
    "<p>The record under test carries three attempts.</p>\n"
    '<h2 id="s6">§6 — A section below the raise threshold</h2>\n'
    "<p>This record carries two.</p>\n"
    '<h2 id="s5-1">§5.1 — A subsection authored under its anchor</h2>\n'
    "<p>Addressed as s5.1 and §5.1, this is the s5-1 record.</p>\n"
)


def _commit_repository(root: Path, *paths: str) -> None:
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", *paths],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _capability(
    capability_class: str = "general",
    reasoning: str = "standard",
    verification: str = "standard",
    risk: str = "low",
) -> dict:
    return {
        "version": "1.0",
        "class": capability_class,
        "requirements": {
            "reasoning": reasoning,
            "verification": verification,
            "risk": risk,
        },
    }


def _write_plan(tmp_path: Path) -> Path:
    """Write a plan whose section records carry the attempts under test."""
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="document">'
        f'<meta name="plan-slug" content="{SLUG}">'
        f"<title>{SLUG}</title></head>"
        f'<body><main class="plan-doc">{AUTHORED}</main></body></html>'
    )
    state = {
        "slug": SLUG,
        "title": "Section routing reaches dispatch",
        "version": 1,
        "type": "plan",
        "status": "active",
        "impl": 0.5,
        "section_declarations": {"s5": "implementable", "s6": "implementable"},
        "sections": [
            {
                "id": "s5",
                "effort_hours": 1.0,
                "capability": _capability(),
                "attempts": 3,
                "status": "implementable",
                "links": [],
            },
            {
                "id": "s6",
                "effort_hours": 1.0,
                "capability": _capability(),
                "attempts": 2,
                "status": "implementable",
                "links": [],
            },
            {
                "id": "s5-1",
                "effort_hours": 1.0,
                "capability": _capability(risk="elevated"),
                "attempts": 3,
                "status": "implementable",
                "links": [],
            },
        ],
    }
    path = tmp_path / f"{SLUG}.html"
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A mounted project whose committed plan carries the records under test."""
    repo = tmp_path / "sample-repository"
    (repo / "docs" / "plans").mkdir(parents=True)
    delivery = repo / "delivery" / "result.txt"
    delivery.parent.mkdir(parents=True)
    delivery.write_text("pending\n", encoding="utf-8")
    _write_plan(repo / "docs" / "plans")
    _commit_repository(repo, "docs", "delivery")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _node(repo: Path, section: str) -> crew.TaskNode:
    return crew.TaskNode(
        id="section-routing-check",
        goal="resolve the lane the section's own record earns",
        plan=SLUG,
        section=section,
        role="implement",
        spec_level="guided",
        done_when=(
            "tests/test_section_routing_reaches_dispatch.py reports the cases passed"
        ),
        write_paths=[str(repo / "delivery" / "result.txt")],
        time_budget="20m",
    )


def test_a_section_at_the_threshold_resolves_the_raised_lane(repository: Path) -> None:
    resolution = crew.plan_dispatch(
        node=_node(repository, "§5"),
        project=PROJECT,
        repo=repository,
        config=CONFIG,
    )

    assert resolution.validation.ok is True, resolution.validation.findings
    assert resolution.backend == "raised-lane"
    assert resolution.backend_settings["effort"] == "raised-effort"
    routed = resolution.section_routing
    assert routed is not None
    assert routed["attempts"] == 3
    assert routed["capability"]["class"] == "orchestrator"
    assert routed["capability"]["requirements"]["verification"] == "strict"
    assert routed["raise"]["changes"] == [
        {"field": "class", "from": "general", "to": "orchestrator"},
        {"field": "reasoning", "from": "standard", "to": "deep"},
        {"field": "verification", "from": "standard", "to": "strict"},
    ]
    summary = routed["summary"]
    assert "attempt 3" in summary
    assert "raised-lane" in summary
    assert resolution.as_dict()["section_routing"]["summary"] == summary


def test_a_section_below_the_threshold_resolves_as_role_routing_did(
    repository: Path,
) -> None:
    node = _node(repository, "§6")
    expected_backend, expected_settings = resolve_role(
        CONFIG, node.role, node.spec_level
    )

    resolution = crew.plan_dispatch(
        node=node, project=PROJECT, repo=repository, config=CONFIG
    )

    assert resolution.validation.ok is True, resolution.validation.findings
    assert resolution.backend == "section-lane"
    assert resolution.backend == expected_backend
    assert resolution.backend_settings == expected_settings
    routed = resolution.section_routing
    assert routed is not None
    assert routed["attempts"] == 2
    assert routed["raise"] is None
    assert "rais" not in routed["summary"].lower()


def test_a_node_without_a_section_resolves_as_role_routing_did(
    repository: Path,
) -> None:
    node = _node(repository, "")
    expected_backend, expected_settings = resolve_role(
        CONFIG, node.role, node.spec_level
    )

    resolution = crew.plan_dispatch(
        node=node, project=PROJECT, repo=repository, config=CONFIG
    )

    assert resolution.backend == expected_backend == "section-lane"
    assert resolution.backend_settings == expected_settings
    assert resolution.section_routing is None


def test_one_derivation_serves_every_spelling_across_the_callers(
    repository: Path,
) -> None:
    plan_path = repository / "docs" / "plans" / f"{SLUG}.html"

    # Imported where it is exercised: a revision that carries no single
    # derivation fails inside the case that needs one, not at collection.
    from reckon.crew.routing import section_anchor, section_record_id

    # One derivation: every spelling of the reference addresses one identity.
    assert {section_record_id(spelling) for spelling in SPELLINGS} == {"s5-1"}
    assert {section_anchor(spelling) for spelling in SPELLINGS} == {"s5-1"}

    # The dispatch caller resolves the hyphenated record for every spelling.
    for spelling in SPELLINGS:
        resolution = crew.plan_dispatch(
            node=_node(repository, spelling),
            project=PROJECT,
            repo=repository,
            config=CONFIG,
        )
        assert resolution.section_routing["attempts"] == 3
        assert resolution.backend == "raised-lane"

    # The routing caller resolves the hyphenated record for every spelling.
    for spelling in SPELLINGS:
        payload = resolve_section_routing(
            CONFIG, node=_node(repository, spelling), plan_path=plan_path
        )
        assert payload["attempts"] == 3
        assert payload["backend"] == "raised-lane"

    # The promotion caller reads the same identity off the same record.
    for spelling in SPELLINGS:
        record = {
            "project": PROJECT,
            "node": {"plan": SLUG, "section": spelling},
        }
        assert (
            promotion_module._run_capability_risk(record, root=repository) == "elevated"
        )
