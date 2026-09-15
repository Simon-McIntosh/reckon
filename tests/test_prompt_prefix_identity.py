"""The reusable opening of a worker prompt must not contain node data."""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import (
    INVARIANT_PROMPT_BOUNDARY,
    INVARIANT_PROMPT_PREFIX,
    compose_prompt,
)


def _render_prompt(*, node_id: str, goal: str, project: str, write_path: str) -> str:
    return compose_prompt(
        node=TaskNode(
            id=node_id,
            goal=goal,
            plan="shared-contract",
            section="s3",
            role="implement",
            done_when="the shared opening is byte-identical",
            write_paths=[write_path],
            time_budget="20m",
        ),
        project=project,
        worktree="/worktree",
        working_directory="/worktree",
        manifest_path="/state/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        can_write_worktree=True,
    )


def test_prompts_share_the_declared_invariant_prefix_before_node_fields() -> None:
    prompts = [
        _render_prompt(
            node_id="alpha", goal="first goal", project="reckon", write_path="src/a.py"
        ),
        _render_prompt(
            node_id="beta",
            goal="second goal",
            project="imas-codex",
            write_path="src/b.py",
        ),
        _render_prompt(
            node_id="gamma", goal="third goal", project="nova", write_path="src/c.py"
        ),
    ]

    assert len(INVARIANT_PROMPT_PREFIX) == INVARIANT_PROMPT_BOUNDARY
    assert INVARIANT_PROMPT_BOUNDARY > 1000
    assert {prompt[:INVARIANT_PROMPT_BOUNDARY] for prompt in prompts} == {
        INVARIANT_PROMPT_PREFIX
    }
    assert [
        prompt[INVARIANT_PROMPT_BOUNDARY:].splitlines()[0] for prompt in prompts
    ] == ["alpha", "beta", "gamma"]
    assert len({prompt[INVARIANT_PROMPT_BOUNDARY:] for prompt in prompts}) == 3
