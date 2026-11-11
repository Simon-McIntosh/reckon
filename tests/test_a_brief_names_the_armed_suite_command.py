"""A brief and a resume advice name the revisions each suite arm must record.

Promotion compares each arm's command with the armed template resolved over
that arm's own revision, so a worker that reconstructs both commands from one
statement of the template records an arm at a revision nobody asked for. On
10 November two power_convertor implement runs were refused for exactly that:
their arms ran the right test files in a different order and at mid-run
revisions, and every landing that evening hit the same refusal for a third
spelling, the absolute interpreter path in place of the venv's.

The implement brief is composed at dispatch, when the worktree's head *is* the
base revision, so it can name that revision and the command resolved there. It
cannot name the head's command, whose tree does not exist yet, so it states the
rule instead: record the after arm at the run's head, with the command resolved
over that head.

A resume advice is composed after the tree has moved, so both revisions exist
and both commands can be resolved. It names the base revision with the command
resolved over the base tree and the run's current head with the command
resolved over that head, which differ as soon as the run added or deleted a
test file.

The declared negative control drops the revisions from the brief, which makes
the first case fail, and drops them from the resume advice, which makes the
second case fail. Run with the control's environment variable set, both cases
are red.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import resumption
from reckon.crew.dispatch import _compose_dispatch_prompt
from reckon.crew.node import TaskNode
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    CONFIG,
    _stopped_pointer,
    crew_home,
    operator_home,
    repo,
)

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The declared mutation, verbatim: the string a reader compares the red log
# against. Two wrong states, one switch, because the string names both.
DECLARED_MUTATION = (
    "drop the revisions from the brief, which makes the first case fail; a "
    "second drops them from the resume advice, which makes the second case fail"
)

MUTATION_ENV = "RECKON_BRIEF_REVISIONS_NEGATIVE_CONTROL"


class _PlanCapturedError(Exception):
    """The test's seam: the advice is captured before a launch is composed."""


SCOPED = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
BASE_COMMAND = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_x.py"
)
HEAD_COMMAND = BASE_COMMAND + " tests/test_y.py"

RUN_ID = "r-brief-names-its-revisions"


@pytest.fixture(autouse=True)
def _declared_negative_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation when its environment variable is set."""
    if os.environ.get(MUTATION_ENV) != "1":
        return
    real_body = dispatch_module._suite_gate_prompt_body

    def body_without_revisions(declared, resolved, *, base_revision=""):
        return real_body(declared, resolved, base_revision="")

    monkeypatch.setattr(
        dispatch_module, "_suite_gate_prompt_body", body_without_revisions
    )
    monkeypatch.setattr(resumption, "_revision_suite_block", lambda record: "")


def _node() -> TaskNode:
    return TaskNode(
        id=RUN_ID,
        goal="name the revisions the suite arms must record",
        plan="plan-a",
        role="implement",
        done_when="the arms record the revisions their commands were resolved at",
        write_paths=["tests/test_x.py", "tests/test_y.py"],
        time_budget="60m",
        manifest_path="/state/runs/node/manifest.md",
    )


def _composed_brief(tmp_path: Path, base_revision: str) -> str:
    """The implement brief a dispatch composes, with its suite fence."""
    suite_body = dispatch_module._suite_gate_prompt_body(
        SCOPED, BASE_COMMAND, base_revision=base_revision
    )
    return _compose_dispatch_prompt(
        node=_node(),
        project="proj",
        authority={},
        backend={"sandbox": "worktree-full"},
        repo_root=tmp_path,
        run_directory=tmp_path / "run",
        worktree=str(tmp_path),
        working_directory=str(tmp_path),
        suite_command=suite_body,
        launch_instant="2026-11-11T08:00:00Z",
        needs_help_after_failures=2,
    )


def test_the_implement_brief_names_the_base_revision_and_the_after_arm_rule(
    tmp_path: Path,
) -> None:
    """Case 1: the brief names the base revision, its command, and the head rule."""
    base_revision = "0123456789abcdef0123456789abcdef01234567"

    brief = _composed_brief(tmp_path, base_revision)

    assert f"Base revision: {base_revision}" in brief
    assert BASE_COMMAND in brief
    assert "Record the after arm at the run's head" in brief
    assert "resolved over that head" in brief


def _git_tree(tmp_path: Path) -> tuple[Path, str, str]:
    """A tree whose head adds a test file its base revision does not hold."""
    tree = tmp_path / "tree"
    for name in ("tests/test_x.py", "reckon/thing.py"):
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tree), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "worker@example.invalid")
    git("config", "user.name", "Worker")
    git("add", "tests", "reckon")
    git("commit", "-q", "-m", "test: seed")
    base = git("rev-parse", "HEAD")
    (tree / "tests" / "test_y.py").write_text("", encoding="utf-8")
    git("add", "tests/test_y.py")
    git("commit", "-q", "-m", "test: add a check")
    head = git("rev-parse", "HEAD")
    return tree, base, head


def _advice_a_resume_carries(pointer: dict) -> str:
    """The advice text a resume hands its worker, captured before the launch."""
    captured: dict[str, str] = {}

    def capture(run_id: str, advice: str, *, config=None):
        captured["advice"] = advice
        raise _PlanCapturedError

    original = resumption.resume_plan
    resumption.resume_plan = capture
    try:
        with pytest.raises(_PlanCapturedError):
            resumption._resume(
                RUN_ID, pointer, config=CONFIG, advice="the catalog is stale; continue"
            )
    finally:
        resumption.resume_plan = original
    return captured["advice"]


def test_the_resume_advice_names_both_revisions_with_their_own_commands(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Case 2: base and head are named, with two different resolved commands."""
    crew_home_path = request.getfixturevalue("crew_home")
    repo_path = request.getfixturevalue("repo")
    tree, base, head = _git_tree(tmp_path)
    pointer = _stopped_pointer(tmp_path, repo_path, RUN_ID, backend="alpha")
    pointer["worktree"] = str(tree)
    pointer["base_sha"] = base
    pointer["suite_command"] = SCOPED
    pointer["node"] = {
        **dict(pointer["node"]),
        "write_paths": ["tests/test_x.py", "tests/test_y.py"],
    }
    crew_home_path.mkdir(parents=True, exist_ok=True)

    advice = _advice_a_resume_carries(pointer)

    assert f"Base revision: {base}" in advice
    assert f"Run head now: {head}" in advice
    assert BASE_COMMAND in advice
    assert HEAD_COMMAND in advice
    assert BASE_COMMAND != HEAD_COMMAND
