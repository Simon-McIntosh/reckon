"""A node names exactly one authority: a brief, or a plan and section.

The CLI refuses ``--brief`` beside ``--plan``/``--section`` at argv, but the
brief branch of :func:`plan_dispatch` skips the committed-section and plan-review
gates around their call sites. A programmatic caller that builds a ``TaskNode``
directly therefore reached that branch with a plan section named, and the section
was silently never checked. The exclusivity is enforced on the node itself so the
skip cannot be reached with a second authority present.

The four node cases pin each direction of the pair; the fifth drives the whole
resolution step and asserts the refusal lands before the skipped gate would have
run, so closing the argv-only hole is proven on the path the argv check does not
cover.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from reckon import crew

# ``reckon.crew`` re-exports the ``dispatch`` callable, which shadows the module
# of the same name; import the module explicitly so the gate spy patches the one
# ``plan_dispatch`` reads.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

_MANIFEST = str(Path(tempfile.gettempdir()) / "brief-plan-exclusivity-manifest.md")

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


def _node(**overrides: Any) -> crew.TaskNode:
    """A well-formed node; each case spoils only the authority it studies."""
    fields: dict[str, Any] = {
        "id": "brief-plan-exclusivity",
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
    path.write_text("record the launch matrix for one backend\n", encoding="utf-8")
    return str(path)


def _authority_findings(node: crew.TaskNode) -> list[dict[str, str]]:
    verdict = crew.validate_node(node)
    return [
        finding
        for finding in verdict.findings
        if finding["property"] == "fully-specified"
    ]


def test_a_brief_with_a_plan_is_refused(brief_file: str) -> None:
    """A brief beside a plan is two authorities; the node must be refused."""
    node = _node(plan="reckon:a-worker-can-take-a-brief", brief=brief_file)

    findings = _authority_findings(node)

    assert findings, "a brief beside a plan must be refused"
    detail = " ".join(finding["detail"] for finding in findings)
    # Both carriers are named, so the refusal says which pair is ambiguous.
    assert "brief" in detail
    assert "reckon:a-worker-can-take-a-brief" in detail


def test_a_brief_with_a_section_is_refused(brief_file: str) -> None:
    """A section names a plan's part, so a brief with a section is two carriers."""
    node = _node(section="s2", brief=brief_file)

    findings = _authority_findings(node)

    assert findings, "a brief beside a section must be refused"
    detail = " ".join(finding["detail"] for finding in findings)
    assert "brief" in detail
    assert "s2" in detail


def test_a_brief_alone_validates(brief_file: str) -> None:
    """A brief with no plan section is the one-authority case and must pass."""
    node = _node(brief=brief_file)

    verdict = crew.validate_node(node)

    assert verdict.ok, verdict.findings


def test_a_plan_and_section_alone_validate() -> None:
    """A plan with a section and no brief is the other one-authority case."""
    node = _node(plan="reckon:a-worker-can-take-a-brief", section="s2")

    verdict = crew.validate_node(node)

    assert verdict.ok, verdict.findings


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


def test_plan_dispatch_refuses_before_the_plan_section_gate_is_skipped(
    dispatch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must land on the resolution step, not rely on the argv check.

    A direct caller reaches ``plan_dispatch`` with no CLI argv to refuse. Without
    the node-level check the brief branch would take the digest and skip the
    committed-section gate entirely, leaving the named section unread. The spy
    proves the gate is not reached, so the refusal is the new node check rather
    than a later symptom of the skipped check. The brief file is real, so under
    the declared mutation the resolution completes and the pair *validates* —
    the failure is the missing refusal, not a missing file.
    """
    calls: list[str] = []

    def _spy(*args: Any, **kwargs: Any) -> str:
        calls.append("require_plan_section_visible")
        return "unreachable"

    monkeypatch.setattr(
        dispatch_module, "require_plan_section_visible", _spy, raising=True
    )

    brief = tmp_path / "brief.md"
    brief.write_text("record the launch matrix\n", encoding="utf-8")

    node = _node(
        plan="reckon:a-worker-can-take-a-brief",
        section="s2",
        brief=str(brief),
    )

    resolution = dispatch_module.plan_dispatch(
        node=node, config=CONFIG, project="proj", repo=dispatch_repo
    )

    assert not resolution.validation.ok
    assert calls == [], "the plan-section gate must not be reached for the pair"
    detail = " ".join(finding["detail"] for finding in resolution.validation.findings)
    assert "brief" in detail and "reckon:a-worker-can-take-a-brief" in detail
