"""A repository's declared domain rules reach the brief, and their checks run.

The rules a repository declares once are matched against a node's write paths by
one module, and this file holds the two consequences that matter: the brief a
node is composed with carries the rules it touches under one fixed heading, and
each of those rules that carries a check command is replayed at promotion, where
a failure refuses the promotion naming the invariant rather than the command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion_checks
from reckon.crew import prompts as prompts_module
from reckon.crew.node import CrewError, TaskNode
from reckon.crew.prompts import (
    DOMAIN_RULES_HEADING,
    compose_prompt,
    domain_rules_block,
)

FLIGHT = """\
domain:
  invariants:
    - id: convergence
      rule: the section count must match the converged count
      applies_to: ["models/**"]
      check: "{check}"
  review_checks:
    - id: dc-limit
      question: what is the limit behaviour at DC?
      applies_to: ["models/**"]
"""

NO_DOMAIN_FLIGHT = """\
gates:
  dimension_floors: {}
"""


def _flight_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "flight.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _checkout(tmp_path: Path, text: str) -> Path:
    """A git checkout carrying a project's flight config.

    The replay resolves the tree it measures from a commit, so the fixture is a
    real repository rather than a directory: a check is only replayed against a
    tree whose revision can be named.
    """
    repo = tmp_path / "checkout"
    (repo / "docs" / "state" / "fixture").mkdir(parents=True)
    (repo / "docs" / "state" / "fixture" / "flight.yaml").write_text(
        text, encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def _node(write_paths: list[str]) -> TaskNode:
    return TaskNode(id="n1", goal="g", plan="a-plan", write_paths=write_paths)


def test_a_matching_node_carries_the_rule_under_the_heading(tmp_path: Path) -> None:
    flight = _flight_file(tmp_path, FLIGHT.format(check="true"))
    block = domain_rules_block(
        project="fixture", node=_node(["models/tape.py"]), project_path=str(flight)
    )
    assert block.startswith(DOMAIN_RULES_HEADING)
    assert "convergence" in block
    assert "the section count must match the converged count" in block
    assert "dc-limit" in block


def test_a_node_writing_elsewhere_carries_no_rule(tmp_path: Path) -> None:
    flight = _flight_file(tmp_path, FLIGHT.format(check="true"))
    block = domain_rules_block(
        project="fixture", node=_node(["docs/notes.py"]), project_path=str(flight)
    )
    assert block == ""


def test_the_composed_prompt_carries_the_rule_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block reaches the prompt the worker reads, and is not doubled.

    The composer is handed the brief text verbatim, so a caller that composed
    the block into its brief is not given a second copy of it.
    """
    monkeypatch.setattr(
        prompts_module,
        "domain_rules_block",
        lambda **_kwargs: f"{DOMAIN_RULES_HEADING}\n  convergence: a rule",
    )
    node = _node(["models/tape.py"])
    prompt = compose_prompt(
        node=node,
        project="fixture",
        worktree=str(tmp_path / "wt"),
        working_directory=str(tmp_path / "wt"),
        manifest_path=str(tmp_path / "wt" / "manifest.md"),
        time_budget="60m",
        needs_help_after_failures=2,
    )
    assert prompt.count(DOMAIN_RULES_HEADING) == 1
    doubled = compose_prompt(
        node=node,
        project="fixture",
        worktree=str(tmp_path / "wt"),
        working_directory=str(tmp_path / "wt"),
        manifest_path=str(tmp_path / "wt" / "manifest.md"),
        time_budget="60m",
        needs_help_after_failures=2,
        brief=f"{DOMAIN_RULES_HEADING}\n  convergence: a rule",
    )
    assert doubled.count(DOMAIN_RULES_HEADING) == 1


def _brief(tmp_path: Path, text: str = "Build the tape model.\n") -> Path:
    path = tmp_path / "brief.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_stored_brief_carries_the_rule_and_its_digest_covers_it(
    tmp_path: Path,
) -> None:
    """The digest a promotion cites covers the rules the worker actually read.

    The heading is composed into the stored copy, and the digest is taken over
    those same bytes — so a rule declared after a brief was written re-keys the
    brief rather than leaving a promotion citing text that never carried it.
    """
    from reckon.crew import dispatch_admission

    flight = _flight_file(tmp_path, FLIGHT.format(check="true"))
    node = _node(["models/tape.py"])
    source = _brief(tmp_path)
    block = domain_rules_block(project="fixture", node=node, project_path=str(flight))
    assert block.startswith(DOMAIN_RULES_HEADING)
    stored = dispatch_admission._store_brief(tmp_path / "run", source, block=block)
    assert DOMAIN_RULES_HEADING in stored.read_text(encoding="utf-8")
    with_block = dispatch_admission._brief_digest(stored, block=block)
    # Read back with the same block the digest is not re-appended: the stored
    # copy already carries it once.
    assert with_block == dispatch_admission._brief_digest(stored)
    assert stored.read_text(encoding="utf-8").count(DOMAIN_RULES_HEADING) == 1
    assert with_block != dispatch_admission._brief_digest(source)


def test_a_brief_that_already_carries_the_heading_is_not_doubled(
    tmp_path: Path,
) -> None:
    from reckon.crew import dispatch_admission

    flight = _flight_file(tmp_path, FLIGHT.format(check="true"))
    node = _node(["models/tape.py"])
    source = _brief(tmp_path, f"Build the tape model.\n\n{DOMAIN_RULES_HEADING}\n  x\n")
    block = domain_rules_block(project="fixture", node=node, project_path=str(flight))
    stored = dispatch_admission._store_brief(tmp_path / "run", source, block=block)
    assert stored.read_text(encoding="utf-8").count(DOMAIN_RULES_HEADING) == 1


def test_the_brief_text_a_worker_reads_carries_the_rule(tmp_path: Path) -> None:
    from reckon.crew import dispatch_admission

    flight = _flight_file(tmp_path, FLIGHT.format(check="true"))
    node = _node(["models/tape.py"])
    node.brief = str(_brief(tmp_path))
    text = dispatch_admission._brief_text(
        node,
        "fixture",
        project_path=str(flight),
    )
    assert DOMAIN_RULES_HEADING in text
    assert "convergence" in text


def test_the_brief_record_names_the_rules_the_brief_carried(tmp_path: Path) -> None:
    from reckon.crew import dispatch_admission

    flight = _flight_file(tmp_path, FLIGHT.format(check="true"))
    node = _node(["models/tape.py"])
    node.brief = str(_brief(tmp_path))
    node.brief_sha256 = "f" * 64
    record = dispatch_admission._brief_record(node, "fixture", project_path=str(flight))
    assert record is not None
    assert record["domain_rules"]["invariants"] == ["convergence"]
    assert record["domain_rules"]["review_checks"] == ["dc-limit"]
    assert record["domain_rules"]["heading"] == DOMAIN_RULES_HEADING


def test_a_project_declaring_nothing_carries_no_rule_heading(tmp_path: Path) -> None:
    """The negative control: drop the declaration and the brief is unchanged."""
    from reckon.crew import dispatch_admission

    flight = _flight_file(tmp_path, NO_DOMAIN_FLIGHT)
    node = _node(["models/tape.py"])
    node.brief = str(_brief(tmp_path))
    original = Path(node.brief).read_text(encoding="utf-8")
    assert (
        domain_rules_block(project="fixture", node=node, project_path=str(flight)) == ""
    )
    assert (
        dispatch_admission._brief_text(node, "fixture", project_path=str(flight))
        == original
    )
    assert dispatch_admission._brief_record(
        node, "fixture", project_path=str(flight)
    ) == {"sha256": node.brief_sha256, "path": node.brief, "source_path": node.brief}


def test_a_failing_check_refuses_naming_the_invariant(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, FLIGHT.format(check="exit 3"))
    node = _node(["models/tape.py"]).as_dict()
    with pytest.raises(CrewError) as refusal:
        promotion_checks._require_domain_invariant_checks(
            "r-fixture",
            "fixture",
            node,
            repository=repo,
            replay_directory=tmp_path / "logs",
        )
    assert "convergence" in str(refusal.value)
    # The check ran and its output was kept, so the refusal points at the
    # evidence rather than only at the rule.
    assert (tmp_path / "logs" / "domain-check-convergence.log").is_file()


def test_a_passing_check_promotes(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, FLIGHT.format(check="exit 0"))
    node = _node(["models/tape.py"]).as_dict()
    assert (
        promotion_checks._require_domain_invariant_checks(
            "r-fixture",
            "fixture",
            node,
            repository=repo,
            replay_directory=tmp_path / "logs",
        )
        is None
    )


def test_a_check_that_overruns_its_bound_is_refused_not_hung(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, FLIGHT.format(check="sleep 30"))
    node = _node(["models/tape.py"]).as_dict()
    log = tmp_path / "logs" / "domain-check-convergence.log"
    with pytest.raises(CrewError) as refusal:
        promotion_checks._require_domain_invariant_checks(
            "r-fixture",
            "fixture",
            node,
            repository=repo,
            timeout_seconds=1.0,
            replay_directory=tmp_path / "logs",
        )
    assert "convergence" in str(refusal.value)
    assert log.is_file()
    assert "cut short" in log.read_text(encoding="utf-8")


def test_a_check_without_a_bound_lands_beside_the_suite_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unbounded caller's log goes where the suite replay writes its own."""
    seen: dict[str, object] = {}

    def _capture(**kwargs: object) -> tuple[()]:
        seen.update(kwargs)
        return ()

    monkeypatch.setattr(
        "reckon.crew.promotion_gate.replay_domain_invariant_checks", _capture
    )
    repo = _checkout(tmp_path, FLIGHT.format(check="exit 0"))
    from reckon.crew.routing import run_directory_of

    promotion_checks._require_domain_invariant_checks(
        "r-fixture", "fixture", _node(["models/tape.py"]).as_dict(), repository=repo
    )
    assert Path(seen["replay_directory"]) == run_directory_of({"run_id": "r-fixture"})


def test_a_project_without_the_block_runs_nothing(tmp_path: Path) -> None:
    """The negative control: no declaration, no guard."""
    repo = _checkout(tmp_path, NO_DOMAIN_FLIGHT)
    node = _node(["models/tape.py"]).as_dict()
    assert (
        promotion_checks._require_domain_invariant_checks(
            "r-fixture",
            "fixture",
            node,
            repository=repo,
            replay_directory=tmp_path / "logs",
        )
        is None
    )
    assert not (tmp_path / "logs").exists()


def test_a_node_matching_no_rule_runs_nothing(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, FLIGHT.format(check="exit 3"))
    node = _node(["docs/notes.py"]).as_dict()
    assert (
        promotion_checks._require_domain_invariant_checks(
            "r-fixture",
            "fixture",
            node,
            repository=repo,
            replay_directory=tmp_path / "logs",
        )
        is None
    )


def test_a_stated_reason_waives_the_failing_check(tmp_path: Path) -> None:
    repo = _checkout(tmp_path, FLIGHT.format(check="exit 3"))
    node = _node(["models/tape.py"]).as_dict()
    waived = promotion_checks._require_domain_invariant_checks(
        "r-fixture",
        "fixture",
        node,
        repository=repo,
        waiver_reason="the fixture result predates the convergence fix",
        replay_directory=tmp_path / "logs",
    )
    assert waived is not None
    assert waived["waived_invariants"] == ["convergence"]
    assert waived["reason"] == "the fixture result predates the convergence fix"
