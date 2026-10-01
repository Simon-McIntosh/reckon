"""A raise rule that fails to resolve is visible where a rule that did not move is not.

A dispatch reads its lane from the plan section's own typed record, and the
read is allowed to fail: no repository, no mount, a rule whose raised class
routes to a lane no layer defines. Falling back to role routing in that case is
deliberate — a broken rule must not stop a node dispatching — but the fallback
used to be silent, so the dispatch record's ``section_routing`` read exactly as
it does for a node carrying no raise at all. The two are different facts about
a run, and the second is the one a reader acts on: "this section has not earned
a raise" is a decision, "the section's rule could not be read" is a defect to
repair.

The failure is therefore recorded, on the dispatch payload and the run pointer,
naming the exception class and the section, while the dispatch still resolves
role routing. A rule that resolves — with or without a raise — carries no
failure, so the three states stay distinguishable.

The fixture builds its plan, its flight config and its crew home under the
test's own temporary path, so no workstation config home, mount or plan is read
or written.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon._plan_html import write_state
from reckon.crew import runs
from reckon.crew.node import CrewError
from reckon.crew.routing import resolve_role, resolve_section_routing

PROJECT = "sample"
SLUG = "raise-failure"
SECTION = "§5"
LANE = "section-lane"
RAISED_LANE = "raised-lane"
ABSENT_LANE = "absent-lane"

AUTHORED = (
    '<h2 id="s5">§5 — The section under test</h2>\n'
    "<p>Its record carries the attempts that earn a raise.</p>\n"
)

RULE = {
    "attempts_threshold": 3,
    "raised_class": "orchestrator",
    "raised_reasoning": "deep",
    "raised_verification": "strict",
}

BASE = {
    "default_backend": LANE,
    "backends": {
        LANE: {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        },
        RAISED_LANE: {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        },
    },
    "roles": {
        "implement": {
            "backend": LANE,
            "by_capability_class": {
                "orchestrator": {"backend": RAISED_LANE, "effort": "raised-effort"}
            },
        }
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

# A rule whose raised class routes to a lane no layer defines: reading it
# raises, while the role routing the dispatch falls back to resolves cleanly.
MALFORMED = copy.deepcopy(BASE)
MALFORMED["capability_raise"] = dict(RULE)
MALFORMED["roles"]["implement"]["by_capability_class"]["orchestrator"] = {
    "backend": ABSENT_LANE,
    "effort": "raised-effort",
}

VALID = copy.deepcopy(BASE)
VALID["capability_raise"] = dict(RULE)

NO_RULE = copy.deepcopy(BASE)


def _capability() -> dict:
    return {
        "version": "1.0",
        "class": "general",
        "requirements": {
            "reasoning": "standard",
            "verification": "standard",
            "risk": "low",
        },
    }


def _write_plan(directory: Path) -> Path:
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
        "title": "A raise failure is visible",
        "version": 1,
        "type": "plan",
        "status": "active",
        "impl": 0.5,
        "section_declarations": {"s5": "implementable"},
        "sections": [
            {
                "id": "s5",
                "effort_hours": 1.0,
                "capability": _capability(),
                "attempts": 3,
                "status": "implementable",
                "links": [],
            }
        ],
    }
    path = directory / f"{SLUG}.html"
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A mounted project whose committed plan carries the section under test."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "sample-repository"
    (repo / "docs" / "plans").mkdir(parents=True)
    delivery = repo / "delivery" / "result.txt"
    delivery.parent.mkdir(parents=True)
    delivery.write_text("pending\n", encoding="utf-8")
    _write_plan(repo / "docs" / "plans")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "delivery"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


def _node(repo: Path, config_home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="raise-failure-check",
        goal="record whether the section's raise resolved",
        plan=SLUG,
        section=SECTION,
        role="implement",
        spec_level="guided",
        done_when="tests/test_a_raise_failure_is_visible.py reports the cases passed",
        write_paths=[str(repo / "delivery" / "result.txt")],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / "raise-failure-check.md"),
    )


def _resolve(repo: Path, config_home: Path, config: dict):
    return crew.plan_dispatch(
        node=_node(repo, config_home),
        project=PROJECT,
        repo=repo,
        config=config,
    )


def _failure(payload: dict) -> dict | None:
    """The routing failure a dispatch record carries, or None when it carries none."""
    routing = payload.get("section_routing")
    if not isinstance(routing, dict):
        return None
    return routing.get("failure")


def test_a_malformed_rule_records_its_failure_and_still_dispatches(
    tree: tuple[Path, Path],
) -> None:
    config_home, repo = tree
    expected_backend, expected_settings = resolve_role(MALFORMED, "implement", "guided")

    resolution = _resolve(repo, config_home, MALFORMED)

    assert resolution.validation.ok is True, resolution.validation.findings
    # The rule raised; the dispatch proceeded on role routing regardless.
    assert resolution.backend == expected_backend == LANE
    assert resolution.backend_settings == expected_settings
    failure = _failure(resolution.as_dict())
    assert failure is not None
    assert failure["section"] == SECTION

    # The class and the message are the ones the resolution actually raised,
    # re-derived here rather than assumed from the fixture's intent.
    plan_path = repo / "docs" / "plans" / f"{SLUG}.html"
    with pytest.raises(CrewError) as raised:
        resolve_section_routing(
            MALFORMED, node=_node(repo, config_home), plan_path=plan_path
        )
    assert failure["exception"] == type(raised.value).__name__
    assert failure["exception"] == "CrewError"
    assert ABSENT_LANE in failure["detail"]
    assert ABSENT_LANE in str(raised.value)


def test_a_malformed_rule_is_not_reported_as_a_section_with_no_rule(
    tree: tuple[Path, Path],
) -> None:
    """The control's target: dropping the failure collapses these two together."""
    config_home, repo = tree

    malformed = _failure(_resolve(repo, config_home, MALFORMED).as_dict())
    clean = _failure(_resolve(repo, config_home, NO_RULE).as_dict())

    assert malformed is not None
    assert clean is None


def test_a_section_with_no_rule_carries_no_failure(tree: tuple[Path, Path]) -> None:
    config_home, repo = tree

    resolution = _resolve(repo, config_home, NO_RULE)

    routing = resolution.as_dict()["section_routing"]
    assert routing["raise"] is None
    assert routing["failure"] is None
    assert resolution.backend == LANE


def test_a_valid_rule_carries_its_resolved_raise(tree: tuple[Path, Path]) -> None:
    config_home, repo = tree

    resolution = _resolve(repo, config_home, VALID)

    routing = resolution.as_dict()["section_routing"]
    assert routing["failure"] is None
    assert routing["raise"]["changes"] == [
        {"field": "class", "from": "general", "to": "orchestrator"},
        {"field": "reasoning", "from": "standard", "to": "deep"},
        {"field": "verification", "from": "standard", "to": "strict"},
    ]
    assert resolution.backend == RAISED_LANE


def test_the_pointer_carries_the_failure_too(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure reaches the record a reader holds without the dispatching process."""
    config_home, repo = tree
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = repo.parent / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)

    record = crew.dispatch(
        node=_node(repo, config_home),
        project=PROJECT,
        repo=repo,
        config=MALFORMED,
        session="session-raise-failure",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
    )

    assert record["backend"] == LANE
    pointer = json.loads(runs.pointer_path(record["run_id"]).read_text())
    for carrier in (record, pointer):
        failure = _failure(carrier)
        assert failure is not None
        assert failure["section"] == SECTION
        assert failure["exception"] == "CrewError"
        assert ABSENT_LANE in failure["detail"]
