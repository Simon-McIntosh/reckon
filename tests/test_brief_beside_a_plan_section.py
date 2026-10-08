"""A brief beside a plan section is the coordinator's instructions, not a rival.

A node names its authority in one of three shapes: a plan section, a brief, or
a brief beside a plan section. The third is how a coordinator says what it
learned between nodes — which module to extend, which interface not to widen,
what a sibling found — without routing every sentence through a plan edit. The
plan stays the authority: ``plan_dispatch`` keys the committed-section and
plan-review gates on the plan being named, so the pair reaches both gates, and
the composed prompt carries the plan pointer first and the brief after it.

Negative control: restoring the pair refusal in ``validate_node`` turns the
acceptance cases red, and the gate spies here fail if either gate is skipped.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_plan as dispatch_plan_module
from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew.prompts import (
    BRIEF_LANDING_CONTRACT,
    PLAN_LANDING_CONTRACT,
    compose_prompt,
)

# ``reckon.crew`` re-exports the ``dispatch`` callable, which shadows the module
# of the same name; import the module explicitly so the gate spies patch the one
# ``plan_dispatch`` reads.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

_MANIFEST = str(Path(tempfile.gettempdir()) / "brief-beside-plan-manifest.md")

CONFIG: dict[str, Any] = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

BRIEF_TEXT = (
    "Extend reckon/crew/node.py rather than adding a sibling module; the reuse "
    "map names validate_node as the owner.\n"
)


def _node(**overrides: Any) -> crew.TaskNode:
    fields: dict[str, Any] = {
        "id": "brief-beside-plan",
        "goal": "record the launch matrix for one backend",
        "plan": "",
        "section": "",
        "brief": "",
        "done_when": "uv run pytest tests/test_backends.py reports 34 passed",
        "write_paths": ["reckon/crew/node.py"],
        "time_budget": "20m",
        "manifest_path": _MANIFEST,
        "spec_level": "guided",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


@pytest.fixture()
def brief_file(tmp_path: Path) -> str:
    path = tmp_path / "brief.md"
    path.write_text(BRIEF_TEXT, encoding="utf-8")
    return str(path)


def _authority_findings(node: crew.TaskNode) -> list[dict[str, str]]:
    verdict = crew.validate_node(node)
    return [f for f in verdict.findings if f["property"] == "fully-specified"]


def test_a_brief_beside_a_plan_section_validates(brief_file: str) -> None:
    node = _node(plan="a-worker-can-take-a-brief", section="s2", brief=brief_file)

    verdict = crew.validate_node(node)

    assert verdict.ok, verdict.findings


def test_a_brief_alone_validates(brief_file: str) -> None:
    verdict = crew.validate_node(_node(brief=brief_file))

    assert verdict.ok, verdict.findings


def test_a_section_with_a_brief_and_no_plan_is_refused_naming_the_plan(
    brief_file: str,
) -> None:
    """A section is a plan's part; with no plan it names nothing."""
    findings = _authority_findings(_node(section="s2", brief=brief_file))

    assert findings, "a section with no plan must be refused"
    detail = " ".join(f["detail"] for f in findings)
    assert "s2" in detail and "no plan" in detail


def test_a_brief_with_a_plan_and_no_section_is_refused_naming_the_section(
    brief_file: str,
) -> None:
    """A brief beside a plan is the instructions for a section; name it.

    The plan scopes the pair to a section, so a briefed plan with no section
    is the mirror of a section with no plan and is refused the same way.
    """
    findings = _authority_findings(
        _node(plan="a-worker-can-take-a-brief", brief=brief_file)
    )

    assert findings, "a brief with a plan and no section must be refused"
    detail = " ".join(f["detail"] for f in findings)
    assert "a-worker-can-take-a-brief" in detail and "no section" in detail


def test_a_node_naming_neither_is_refused() -> None:
    findings = _authority_findings(_node())

    assert findings
    assert "no plan or brief" in " ".join(f["detail"] for f in findings)


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


def test_plan_dispatch_on_the_pair_reaches_both_plan_gates(
    dispatch_repo: Path, brief_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief must not skip the gates the plan section is owed.

    Both gates are spied rather than run, because the fixture has no committed
    plan: what this case proves is the branch taken, which is the fact the old
    exclusivity existed to protect. The spies return the values a passing gate
    would, so the resolution continues past them and the node validates.
    """
    calls: list[str] = []

    def _section_gate(*args: Any, **kwargs: Any) -> str:
        calls.append("section")
        return "0" * 40

    def _review_gate(*args: Any, **kwargs: Any) -> None:
        calls.append("review")

    monkeypatch.setattr(
        dispatch_plan_module, "require_plan_section_visible", _section_gate, raising=True
    )
    monkeypatch.setattr(
        dispatch_plan_module, "require_plan_reviewed", _review_gate, raising=True
    )
    monkeypatch.setattr(
        dispatch_plan_module,
        "_done_when_plan_overlap_warning",
        lambda **kwargs: None,
        raising=True,
    )

    node = _node(plan="a-worker-can-take-a-brief", section="s2", brief=brief_file)

    resolution = dispatch_module.plan_dispatch(
        node=node, config=CONFIG, project="proj", repo=dispatch_repo
    )

    assert resolution.validation.ok, resolution.validation.findings
    assert calls == ["section", "review"]
    # The brief's digest is taken as well: both authorities are recorded.
    assert len(node.brief_sha256) == 64


def test_a_brief_alone_still_skips_the_plan_gates(
    dispatch_repo: Path, brief_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        dispatch_module,
        "require_plan_section_visible",
        lambda **kwargs: calls.append("section"),
        raising=True,
    )
    monkeypatch.setattr(
        dispatch_module,
        "require_plan_reviewed",
        lambda **kwargs: calls.append("review"),
        raising=True,
    )

    resolution = dispatch_module.plan_dispatch(
        node=_node(brief=brief_file), config=CONFIG, project="proj", repo=dispatch_repo
    )

    assert resolution.validation.ok, resolution.validation.findings
    assert calls == []


def _compose(node: crew.TaskNode, brief: str = "") -> str:
    return compose_prompt(
        node=node,
        project="proj",
        worktree="/repo/worktree",
        working_directory="/repo/worktree",
        manifest_path=_MANIFEST,
        time_budget="20m",
        needs_help_after_failures=2,
        brief=brief,
    )


def test_the_pair_prompt_carries_the_plan_pointer_then_the_brief() -> None:
    node = _node(plan="a-worker-can-take-a-brief", section="s2", brief="brief.md")

    prompt = _compose(node, brief=BRIEF_TEXT)

    pointer = prompt.index("PLAN     proj:a-worker-can-take-a-brief s2")
    heading = prompt.index("BRIEF    (the coordinator's instructions for this section")
    body = prompt.index(BRIEF_TEXT)
    assert pointer < heading < body
    # The landing stays plan-carried: the record goes to the section.
    assert PLAN_LANDING_CONTRACT in prompt
    assert BRIEF_LANDING_CONTRACT not in prompt
    assert "do not edit the plan" in prompt


def test_a_brief_only_prompt_is_unchanged_by_the_pair_shape() -> None:
    node = _node(brief="brief.md")

    prompt = _compose(node, brief=BRIEF_TEXT)

    assert "PLAN     " not in prompt
    assert prompt.index("BRIEF\n") < prompt.index(BRIEF_TEXT)
    assert BRIEF_LANDING_CONTRACT in prompt
    assert PLAN_LANDING_CONTRACT not in prompt


PLAN_HTML = """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
<meta name="plan-title" content="Fixture">
<meta name="plan-status" content="active">
<meta name="plan-impl" content="0.0">
<meta name="plan-modified" content="2026-09-25">
<meta name="plan-version" content="1">
</head><body><h2 id="delivery">Delivery</h2><p>Ship one measured change.</p></body></html>
"""

IN_HARNESS_CONFIG: dict[str, Any] = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        role: {"backend": "worker", "execution_capable": True}
        for role in ("implement", "investigate", "review", "test")
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def committed_plan_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(PLAN_HTML, encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    brief = tmp_path / "brief.md"
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    return config_home, repo, brief


def test_a_brief_beside_a_committed_plan_section_dispatches_end_to_end(
    committed_plan_repo: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pair succeeds against a committed plan, not merely at argv.

    The committed-section gate and the plan-review gate both key on the plan
    being named, so with a committed plan whose section exists and the shipped
    report-only review gate, the dry run clears every gate and resolves the
    brief's digest onto the node — the end-to-end path the argv-only case does
    not exercise.
    """
    _config_home, repo, brief = committed_plan_repo
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: IN_HARNESS_CONFIG
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--section",
            "delivery",
            "--brief",
            str(brief),
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "briefed-plan-dry",
            "--goal",
            "ship one measured change",
            "--write-path",
            "src/change.py",
            "--done-when",
            "pytest reports one passing plan review gate case",
            "--session",
            "briefed-plan-dry-session",
            "--repo",
            str(repo),
            "--dry-run",
        ],
    )

    payload = json.loads(result.stdout.splitlines()[0])
    assert result.exit_code == 0, result.output
    assert payload["ok"] is True
    assert (
        payload["node"]["brief_sha256"]
        == hashlib.sha256(BRIEF_TEXT.encode("utf-8")).hexdigest()
    )
    assert payload["node"]["plan"] == "fixture"
    assert payload["node"]["section"] == "delivery"
