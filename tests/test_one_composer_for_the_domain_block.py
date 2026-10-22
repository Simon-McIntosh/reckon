"""One composer renders the repository's domain block, and nothing renders empty.

The domain block a worker reads and the per-check line a review reads are one
capability, so they have one spelling: ``domain_rules_block`` matches and
renders, and both the brief and the review render a single check through the
same helper rather than restating its shape. A project that declares only where
its models live matches the model-path globs while declaring no rule, so the
renderer composes nothing for it — an empty heading names no rule and would
mislead a reader into looking for one.
"""

from __future__ import annotations

import re
from pathlib import Path

from reckon._flight_schema import DomainReviewCheck
from reckon.crew import dispatch_admission
from reckon.crew import prompts as prompts_module
from reckon.crew.domain_rules import DomainRules
from reckon.crew.node import TaskNode
from reckon.crew.prompts import (
    DOMAIN_RULES_HEADING,
    domain_rules_block,
    render_domain_rules,
)

MODEL_PATHS_ONLY_FLIGHT = """\
domain:
  model_paths: ["models/**"]
"""

DECLARED_RULE_FLIGHT = """\
domain:
  model_paths: ["models/**"]
  invariants:
    - id: convergence
      rule: the section count must match the converged count
      applies_to: ["models/**"]
      check: "true"
  review_checks:
    - id: dc-limit
      question: what is the limit behaviour at DC?
      applies_to: ["models/**"]
"""

DECLARED_RULE_BLOCK = (
    f"{DOMAIN_RULES_HEADING}\n"
    "  convergence: the section count must match the converged count\n"
    "    check: true\n"
    "  dc-limit: what is the limit behaviour at DC?"
)

# The literal the check renderer spells, searched for across the package so a
# second module restating it fails a test rather than drifting silently.
CHECK_LINE_SHAPE = re.compile(r"\{check\.id\}:\s*\{check\.question\}")

CREW_DIR = Path(prompts_module.__file__).resolve().parent


def _flight_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "flight.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _node(write_paths: list[str]) -> TaskNode:
    return TaskNode(id="n1", goal="g", plan="a-plan", write_paths=write_paths)


def _node_with_brief(tmp_path: Path, write_paths: list[str]) -> TaskNode:
    source = tmp_path / "brief.md"
    source.write_text("Build the tape model.\n", encoding="utf-8")
    node = _node(write_paths)
    node.brief = str(source)
    node.brief_sha256 = "f" * 64
    return node


def test_a_model_paths_only_project_composes_no_domain_heading(tmp_path: Path) -> None:
    """A project declaring only where its models live declares no rule.

    The match is truthy — the node writes the modelling surface — so the
    renderer, not the matcher, must decide there is nothing to head.
    """
    flight = _flight_file(tmp_path, MODEL_PATHS_ONLY_FLIGHT)
    block = domain_rules_block(
        project="fixture", node=_node(["models/tape.py"]), project_path=str(flight)
    )
    assert block == ""
    assert DOMAIN_RULES_HEADING not in block


def test_a_declared_invariant_still_carries_the_heading(tmp_path: Path) -> None:
    """The suppression is empty-only: a declared rule still renders."""
    flight = _flight_file(tmp_path, DECLARED_RULE_FLIGHT)
    block = domain_rules_block(
        project="fixture", node=_node(["models/tape.py"]), project_path=str(flight)
    )
    assert block == DECLARED_RULE_BLOCK


def test_the_brief_of_a_model_paths_only_project_carries_no_heading(
    tmp_path: Path,
) -> None:
    """The text a worker reads is the brief's own bytes when no rule matched."""
    flight = _flight_file(tmp_path, MODEL_PATHS_ONLY_FLIGHT)
    node = _node_with_brief(tmp_path, ["models/tape.py"])
    text = dispatch_admission._brief_text(node, "fixture", project_path=str(flight))
    assert text == Path(node.brief).read_text(encoding="utf-8")
    assert DOMAIN_RULES_HEADING not in text


def test_no_second_composer_of_the_domain_block_exists() -> None:
    """One module matches and renders the block; the admission layer does not."""
    deleted_name = "_brief_" + "domain_block"
    assert not hasattr(dispatch_admission, deleted_name)
    for module in ("dispatch.py", "dispatch_plan.py"):
        source = (CREW_DIR / module).read_text(encoding="utf-8")
        assert deleted_name not in source, module
        assert "domain_rules_block" in source, module
    tests_dir = CREW_DIR.parent.parent / "tests"
    stale = sorted(
        path.name
        for path in tests_dir.glob("*.py")
        if path.name != Path(__file__).name
        and deleted_name in path.read_text(encoding="utf-8")
    )
    assert stale == [], f"a test still names the deleted composer: {stale}"
    definers = sorted(
        path.name
        for path in CREW_DIR.glob("*.py")
        if "DOMAIN_RULES_HEADING =" in path.read_text(encoding="utf-8")
    )
    assert definers == ["prompts.py"]


def test_the_per_check_line_has_one_spelling() -> None:
    """A check line is spelled once, in the module that owns the block."""
    from reckon.crew.prompts import render_review_check

    check = DomainReviewCheck(
        id="dc-limit",
        question="what is the limit behaviour at DC?",
        applies_to=["models/**"],
    )
    assert (
        render_review_check(check) == "  dc-limit: what is the limit behaviour at DC?"
    )
    spellers = sorted(
        path.name
        for path in CREW_DIR.glob("*.py")
        if CHECK_LINE_SHAPE.search(path.read_text(encoding="utf-8"))
    )
    assert spellers == ["prompts.py"]


def test_the_rendered_check_line_is_the_one_the_block_carries() -> None:
    """The block and a lone per-check render compose from the same helper."""
    from reckon.crew.prompts import render_review_check

    check = DomainReviewCheck(
        id="dc-limit",
        question="what is the limit behaviour at DC?",
        applies_to=["models/**"],
    )
    rendered = render_domain_rules(DomainRules(review_checks=(check,)))
    assert rendered == "\n".join([DOMAIN_RULES_HEADING, render_review_check(check)])


def test_a_model_paths_only_match_renders_nothing_through_the_renderer() -> None:
    """The renderer suppresses the heading on a model-path-only match."""
    assert render_domain_rules(DomainRules(model_paths=("models/**",))) == ""


def test_the_brief_digest_of_an_unchanged_node_is_unchanged(tmp_path: Path) -> None:
    """A node carrying a declared rule keeps the block, its text and its digest.

    The digest covers the brief's bytes with the block appended, so freezing the
    block's own text for a rule-declaring node freezes the digest a promotion
    cites. Recomputing it twice over the same document must agree.
    """
    flight = _flight_file(tmp_path, DECLARED_RULE_FLIGHT)
    node = _node_with_brief(tmp_path, ["models/tape.py"])
    stored = dispatch_admission._store_brief(
        tmp_path / "run",
        Path(node.brief),
        block=domain_rules_block(
            project="fixture", node=node, project_path=str(flight)
        ),
    )
    node.brief_path = str(stored)
    first = dispatch_admission._brief_digest(stored)
    second = dispatch_admission._brief_digest(stored)
    assert first == second
    assert stored.read_text(encoding="utf-8").count(DOMAIN_RULES_HEADING) == 1
    assert DECLARED_RULE_BLOCK in stored.read_text(encoding="utf-8")


def test_a_model_paths_only_project_records_no_domain_rules(tmp_path: Path) -> None:
    """The brief record carries rules only when a rule rendered."""
    flight = _flight_file(tmp_path, MODEL_PATHS_ONLY_FLIGHT)
    node = _node_with_brief(tmp_path, ["models/tape.py"])
    record = dispatch_admission._brief_record(node, "fixture", project_path=str(flight))
    assert record is not None
    assert "domain_rules" not in record
