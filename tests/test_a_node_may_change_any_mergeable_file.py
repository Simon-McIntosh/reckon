"""A dispatched node may change any mergeable file in its repository.

A declared write path says where a node's change is *expected*, and it is what a
review compares the diff against. It is not a boundary: the fence leaves the
whole worktree writable and seals only the classes git cannot reconcile, so a
node declared with nothing but its evidence fragment delivers source and tests
without a temporary widening or an accepted path, and a dispatch that declares
nothing at all is accepted with the repository as its writable scope.

Promotion still refuses a changed path a merge cannot reconcile when the node
did not declare it, naming the file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion_scope, recovery_review_subject
from reckon.crew.dispatch_admission import _resolved_write_paths
from reckon.crew.node import CrewError, TaskNode, validate_node
from reckon.crew.prompts import compose_prompt

PROJECT = "proj"
PLAN = "plan-a"
FRAGMENT = f"docs/evidence/fragments/{PLAN}/node.html"
SOURCE = "reckon/crew/demo.py"
TEST = "tests/test_demo.py"
UNDECLARED_PLAN = "docs/plans/other.html"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    ).stdout


def _repository(tmp_path: Path) -> Path:
    """A synthesised checkout holding a plan, a source module and its test."""
    repository = tmp_path / "checkout"
    repository.mkdir()
    seeds = (
        (SOURCE, "value = 1\n"),
        (TEST, "def test_demo():\n    assert True\n"),
        (f"docs/plans/{PLAN}.html", "<!doctype html>\n"),
    )
    for relative, text in seeds:
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    return repository


def _commit(repository: Path, relative: str, text: str) -> str:
    """Commit one changed file and return the new head."""
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repository, "add", "--", relative)
    _git(repository, "commit", "-q", "-m", f"feat: change {relative}")
    return _git(repository, "rev-parse", "HEAD").strip()


def _record(repository: Path, write_paths: list[str], base: str) -> dict:
    return {
        "run_id": "r-scope",
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": base,
        "node": {"id": "scope-node", "write_paths": list(write_paths)},
    }


def _prompt_brief(write_paths: list[str]) -> str:
    node = TaskNode(
        id="scope-node",
        goal="change a thing",
        plan=PLAN,
        section="s1",
        done_when="pytest reports the check passing",
        write_paths=list(write_paths),
        role="implement",
        spec_level="guided",
    )
    text = compose_prompt(
        node=node,
        project=PROJECT,
        worktree="/checkout",
        working_directory="/checkout",
        manifest_path="/state/runs/r-scope/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        run_id="r-scope",
    )
    start = text.index("FENCE — SCOPE")
    return text[start : text.index("CONCURRENT NODES", start)]


def _needs_acceptance(changed: list[str], declared: list[str], record: dict) -> tuple:
    """The changed paths a landing cannot proceed without accepting."""
    outside = promotion_scope._outside_declared_scope(
        changed,
        declared,
        record=record,  # type: ignore[arg-type]
        tree=Path(str(record["worktree"])),
    )
    return promotion_scope._changed_artifacts_needing_acceptance(
        outside,
        record=record,
        tree=Path(str(record["worktree"])),  # type: ignore[arg-type]
    )


def test_a_fragment_scope_node_lands_source_and_a_test_with_no_accepted_path(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD").strip()
    declared = [FRAGMENT]
    record = _record(repository, declared, base)

    _commit(repository, SOURCE, "value = 2\n")
    head = _commit(repository, TEST, "def test_demo():\n    assert 2 == 2\n")
    _commit(repository, FRAGMENT, "<article>evidence</article>\n")

    changed = _git(
        repository, "diff", "--name-only", "--no-renames", f"{base}..{head}"
    ).split()
    assert SOURCE in changed and TEST in changed, changed

    # A mergeable source and test change needs no accepted path, so a landing
    # of this run proceeds with the fragment as its only declaration.
    assert _needs_acceptance(changed, declared, record) == ()


def test_an_undeclared_plan_file_is_refused_at_promotion_naming_the_file(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD").strip()
    record = _record(repository, [FRAGMENT], base)
    head = _commit(repository, UNDECLARED_PLAN, "<!doctype html><p>other</p>\n")
    changed = [UNDECLARED_PLAN]

    assert _needs_acceptance(changed, [FRAGMENT], record) == (UNDECLARED_PLAN,)

    with pytest.raises(CrewError) as refused:
        promotion_scope._accepted_scope_exceptions(
            "r-scope",
            [UNDECLARED_PLAN],
            None,
            record=record,
            tree=repository,
            commits=[head],
        )
    assert UNDECLARED_PLAN in str(refused.value)


def test_a_fragment_scope_brief_does_not_present_the_fragment_as_the_whole_scope() -> (
    None
):
    brief = _prompt_brief([FRAGMENT])

    assert "nothing outside them" not in brief, brief

    assert "Writable: the whole worktree" in brief, brief
    assert "Expected:" in brief and FRAGMENT in brief, brief
    # The classes a merge cannot reconcile are named, so the worker reads the
    # rule from its brief rather than from a refusal at promotion.
    assert "docs/plans/*.html" in brief, brief


def test_a_dispatch_declaring_no_write_path_states_the_repository_as_its_scope() -> (
    None
):
    brief = _prompt_brief([])

    assert "Writable: the whole worktree" in brief, brief
    assert "Expected: the repository itself" in brief, brief
    assert "docs/plans/*.html" in brief, brief

    node = TaskNode(
        id="scope-node",
        goal="change a thing",
        plan=PLAN,
        section="s1",
        done_when="pytest reports the check passing",
        write_paths=[],
        role="implement",
        spec_level="guided",
        time_budget="20m",
        manifest_path="/state/runs/r-scope/manifest.md",
    )
    verdict = validate_node(node)
    assert verdict.ok, verdict.findings

    # The dispatch route never presents an empty declaration to the launch: it
    # fills the role's default delivery scope first, so the state a worker
    # receives is those absolute paths. That is the state asserted here: the
    # brief still states the repository as the writable scope, and the
    # defaults are named for what they are — delivery grants, not expected
    # edits.
    defaults = _resolved_write_paths(
        {"write_paths": ["."], "sandbox": "worktree-full"},
        run_directory=Path("/state/runs/r-scope"),
    )
    assert defaults and all(Path(path).is_absolute() for path in defaults), defaults
    filled = _prompt_brief(defaults)
    assert "Expected: the repository itself" in filled, filled
    assert "Delivery grants outside the repository" in filled, filled


def test_a_project_declared_pattern_is_named_in_the_scope_block(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "shared-write-paths.json").write_text(
        '{"paths": [], "exclusive_patterns": ["deploy/*.lock"]}\n', encoding="utf-8"
    )
    node = TaskNode(
        id="scope-node",
        goal="change a thing",
        plan=PLAN,
        section="s1",
        done_when="pytest reports the check passing",
        write_paths=[],
        role="implement",
        spec_level="guided",
    )
    prompt = compose_prompt(
        node=node,
        project=PROJECT,
        worktree=str(repository),
        working_directory=str(repository),
        manifest_path=str(repository / "manifest.md"),
        time_budget="20m",
        needs_help_after_failures=2,
        run_id="r-scope",
    )
    start = prompt.index("FENCE — SCOPE")
    brief = prompt[start : prompt.index("CONCURRENT NODES", start)]

    # The pattern the project declares is the one its fence seals, so a brief
    # naming only the shipped classes would describe a narrower fence.
    assert "deploy/*.lock" in brief, brief


def test_a_review_brief_names_the_declared_paths_and_the_delivered_diff(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD").strip()
    head = _commit(repository, SOURCE, "value = 3\n")
    record = _record(repository, [FRAGMENT], base)

    text = recovery_review_subject._review_brief_text(record, head)

    assert recovery_review_subject.REVIEWED_SCOPE_HEADING in text, text
    assert f"  {SOURCE}" in text, text
    assert f"Delivered diff {base}..{head}" in text, text
    assert f"{SOURCE} (+1 -1)" in text, text
    # The declared scope is named, and the path the node left it for is called
    # out, so a review can score scope discipline from the brief alone.
    assert "Changed outside the declared paths" in text, text
