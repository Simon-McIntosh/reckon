"""A reflex-composed run review's budget fits the fence its dispatch resolves.

The reflex builds a run review's dispatch from the reviewed node's own record.
Copying that node's ``time_budget`` verbatim is what a coordinator can defeat
without noticing: a long implement node dispatched with a per-dispatch fence
override of ``fences.time_budget=240m`` composes a review whose command carries
``--time-budget 240m`` and no override of its own. Node validation measures that
command against the fence the review dispatch resolves for itself — the shipped
60m — and refuses it. The refusal reaches nobody: the brief is written, no
review run launches, and the obligations view keeps promising the review will be
composed. The answer is to clamp the composed budget to that same fence, so a
node budget at or under it is carried through unchanged and a node naming none
keeps the default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import recovery_review_subject as review_subject
from reckon.crew.node import TaskNode, parse_duration, validate_node

# The fence a review dispatch resolves for this project, and the budget the
# composition falls back to when the reviewed node names none.
REVIEW_FENCE = "60m"
DEFAULT_BUDGET = "20m"

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": REVIEW_FENCE,
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": REVIEW_FENCE, "needs_help_after_failures": 2},
}


@pytest.fixture
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated crew home, so a run directory resolves under ``tmp_path``."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=CONFIG)
    )
    return home


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A committed tree a run's repository and worktree can point at."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _record(repository: Path, *, budget: str | None) -> dict:
    """A landed implement run at ``repository`` whose node names ``budget``."""
    node: dict = {"id": "long-implement-node", "plan": "fixture", "section": "s2"}
    if budget is not None:
        node["time_budget"] = budget
    return {
        "run_id": "r-long-implement-run",
        "project": "sample",
        "repo": str(repository),
        "worktree": str(repository),
        "role": "implement",
        "node": node,
    }


def test_a_long_node_budget_is_clamped_to_the_review_fence(
    crew_home: Path, repository: Path
) -> None:
    fields = review_subject._review_dispatch_fields(_record(repository, budget="240m"))

    assert fields["time_budget"] == REVIEW_FENCE
    assert parse_duration(fields["time_budget"]) <= parse_duration(REVIEW_FENCE)


def test_a_budget_under_the_fence_is_carried_through(
    crew_home: Path, repository: Path
) -> None:
    fields = review_subject._review_dispatch_fields(_record(repository, budget="30m"))

    assert fields["time_budget"] == "30m"


def test_a_node_naming_no_budget_keeps_the_default(
    crew_home: Path, repository: Path
) -> None:
    fields = review_subject._review_dispatch_fields(_record(repository, budget=None))

    assert fields["time_budget"] == DEFAULT_BUDGET


def test_the_composed_budget_passes_the_bounded_check(
    crew_home: Path, repository: Path, tmp_path: Path
) -> None:
    """The composed command is the one validation sees, measured at its fence."""
    fields = review_subject._review_dispatch_fields(_record(repository, budget="240m"))
    node = TaskNode(
        id=fields["node_id"],
        goal=fields["goal"],
        plan=fields["plan"],
        section=fields["section"],
        brief=fields["brief"],
        role="review",
        spec_level="exact",
        done_when=fields["done_when"],
        write_paths=list(fields["write_paths"]),
        time_budget=fields["time_budget"],
        manifest_path=str(tmp_path / "manifest.md"),
    )

    verdict = validate_node(node, budget_ceiling=REVIEW_FENCE)

    assert "bounded" not in verdict.failed_properties, verdict.findings
