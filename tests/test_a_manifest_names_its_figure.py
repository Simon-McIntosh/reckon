"""A manifest that names a figure is held to the figure the run delivers.

A node whose result is a measurement, a comparison or a rendered surface keeps
a figure under the plan's figure directory and names it on a ``figures:`` line;
a node with nothing to show names none. The name alone establishes nothing, so
the manifest check reads it against three facts: the path lies inside
``docs/figures/``, it lies under the figure directory of the plan this run
builds, and the run's own commits carry it.

Without the third fact a manifest could name a figure a worker rendered to look
at and never staged, which is exactly the copy the figure rule already bans, so
the refusal is what makes the line an account of the delivery rather than of an
intention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from tests.helpers.git import git as _git

PLAN = "plan-a"
NODE = "node-a"
FIGURE = f"docs/figures/{PLAN}/{NODE}/convergence.svg"
OTHER_PLAN_FIGURE = "docs/figures/plan-b/other-node/convergence.svg"
OUTSIDE_FIGURE = "docs/research/convergence.svg"


def _node(**overrides: object) -> TaskNode:
    fields: dict[str, object] = {
        "id": NODE,
        "goal": "hold a named figure to the figure the run delivers",
        "plan": PLAN,
        "write_paths": [
            f"docs/evidence/fragments/{PLAN}/{NODE}.html",
            f"docs/figures/{PLAN}/{NODE}",
        ],
    }
    fields.update(overrides)
    return TaskNode(**fields)  # type: ignore[arg-type]


def _manifest(*, base: str, figures: list[str] | None = None) -> str:
    payload: dict[str, object] = {
        "node": NODE,
        "status": "complete",
        "commits": [base],
        "tests": "pytest -q tests/test_example.py -> 1 passed",
        "orientation_base_sha": base,
    }
    if figures is not None:
        payload["figures"] = figures
    return json.dumps(payload, indent=2)


def _findings(
    text: str,
    node: TaskNode | None = None,
    *,
    worktree: Path | None = None,
    repository: Path | None = None,
) -> list[str]:
    from reckon.crew.reports import audit_manifest

    return audit_manifest(
        text, node or _node(), worktree=worktree, repository=repository
    )["findings"]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "reckon").mkdir(parents=True)
    (root / "reckon" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "reckon/mod.py"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    return root


def _base(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def _commit_figure(root: Path, relative: str) -> None:
    figure = root / relative
    figure.parent.mkdir(parents=True, exist_ok=True)
    figure.write_text("<svg></svg>\n", encoding="utf-8")
    _git(root, "add", relative)
    _git(root, "commit", "-q", "-m", f"feat: add {Path(relative).stem}")


def test_a_figure_the_commits_carry_under_the_plan_passes(repo: Path) -> None:
    base = _base(repo)
    _commit_figure(repo, FIGURE)

    assert (
        _findings(
            _manifest(base=base, figures=[FIGURE]),
            worktree=repo,
            repository=repo,
        )
        == []
    )


def test_a_figure_the_commits_do_not_carry_is_refused_naming_the_path(
    repo: Path,
) -> None:
    base = _base(repo)

    findings = _findings(
        _manifest(base=base, figures=[FIGURE]),
        worktree=repo,
        repository=repo,
    )

    assert len(findings) == 1
    assert FIGURE in findings[0]
    assert "not carried by this run's commits" in findings[0]


def test_a_figure_redrawn_after_the_base_passes(repo: Path) -> None:
    """A node that redraws a figure already present at its base delivers it.

    The path exists at the base, so a check that reads only added paths refuses
    a figure the run's own commit carried. The figure here is committed before
    the base and rewritten after it, which is the shape every redraw has.
    """
    _commit_figure(repo, FIGURE)
    base = _base(repo)
    figure = repo / FIGURE
    figure.write_text("<svg><!-- redrawn --></svg>\n", encoding="utf-8")
    _git(repo, "add", FIGURE)
    _git(repo, "commit", "-q", "-m", "docs: redraw the figure")

    assert (
        _findings(
            _manifest(base=base, figures=[FIGURE]),
            worktree=repo,
            repository=repo,
        )
        == []
    )


def test_a_figure_outside_the_figure_directory_is_refused(repo: Path) -> None:
    base = _base(repo)
    _commit_figure(repo, OUTSIDE_FIGURE)

    findings = _findings(
        _manifest(base=base, figures=[OUTSIDE_FIGURE]),
        worktree=repo,
        repository=repo,
    )

    assert len(findings) == 1
    assert OUTSIDE_FIGURE in findings[0]
    assert "outside docs/figures/" in findings[0]


def test_a_figure_under_another_plans_directory_is_refused(repo: Path) -> None:
    base = _base(repo)
    _commit_figure(repo, OTHER_PLAN_FIGURE)

    findings = _findings(
        _manifest(base=base, figures=[OTHER_PLAN_FIGURE]),
        worktree=repo,
        repository=repo,
    )

    assert len(findings) == 1
    assert OTHER_PLAN_FIGURE in findings[0]
    assert "another plan's figure directory" in findings[0]
    assert PLAN in findings[0]


def test_a_manifest_naming_no_figure_passes(repo: Path) -> None:
    base = _base(repo)

    assert _findings(_manifest(base=base), worktree=repo, repository=repo) == []


def test_the_composed_implementation_prompt_states_the_rule() -> None:
    prompt = compose_prompt(
        node=_node(),
        project="proj",
        worktree="/repo/worktrees/" + NODE,
        working_directory="/repo/worktrees/" + NODE,
        manifest_path="/state/runs/" + NODE + "/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )

    flat = " ".join(prompt.split())
    assert "`figures:` line in your manifest" in flat
    assert "docs/figures/<plan>/" in flat
    assert (
        "the manifest check holds a named figure to a file the run's commits "
        "carry there" in flat
    )
