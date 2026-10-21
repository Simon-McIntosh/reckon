"""A repository's domain rules, declared once and matched to one node.

The plan declares the rule block on the resolved flight config and one module
that answers which rules a node touches, so a brief, a review and a promotion
check read the same match. These tests cover the block's validation, the
refusal of a malformed entry, and the match itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import flight
from reckon._flight_schema import DomainConfig
from reckon.crew.domain_rules import rules_for
from reckon.flight import FlightConfigError

DOMAIN_BOUNDARY = """
domain:
  invariants:
    - id: converged-sections
      rule: A tape model is built only at the repository's converged section count.
      applies_to:
        - models/**
      check: python -m tools.check_convergence
    - id: dc-limit
      rule: A divider's limit behaviour is stated at DC and at high frequency.
      applies_to:
        - lib/converters/**.py
  review_checks:
    - id: same-operator
      question: Do both sides of the comparison pass through the same measurement operator?
      applies_to:
        - models/**
    - id: perturbation-grows
      question: Does a small perturbation grow in a lossy system?
      applies_to:
        - lib/**
  model_paths:
    - models/**
"""


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep the match off this workstation's real host layer."""
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


def write_project_layer(tmp_path: Path, text: str) -> Path:
    """Write a project flight layer and return its path."""
    path = tmp_path / "project" / "flight.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_fixture_domain_block_validates(tmp_path):
    """A block with two invariants, two checks and model paths validates."""
    project = write_project_layer(tmp_path, DOMAIN_BOUNDARY)
    resolved = flight.resolve(project="fixture", project_path=project)
    block = resolved.config.get("domain")
    assert block is not None
    config = DomainConfig.model_validate(block)
    assert [invariant.id for invariant in config.invariants] == [
        "converged-sections",
        "dc-limit",
    ]
    assert [check.id for check in config.review_checks] == [
        "same-operator",
        "perturbation-grows",
    ]
    assert config.model_paths == ["models/**"]


def test_missing_rule_is_refused_naming_the_field(tmp_path):
    """An invariant with no rule is refused, and the refusal names `rule`."""
    project = write_project_layer(
        tmp_path,
        """
domain:
  invariants:
    - id: broken
      applies_to:
        - models/**
""",
    )
    with pytest.raises(FlightConfigError) as raised:
        flight.resolve(project="fixture", project_path=project)
    assert raised.value.key_path.endswith("rule")
    assert str(project) == raised.value.source


def test_applies_to_that_is_not_a_list_is_refused_naming_the_field(tmp_path):
    """A scalar applies_to is refused, and the refusal names `applies_to`."""
    project = write_project_layer(
        tmp_path,
        """
domain:
  review_checks:
    - id: broken
      question: Does it hold?
      applies_to: models/**
""",
    )
    with pytest.raises(FlightConfigError) as raised:
        flight.resolve(project="fixture", project_path=project)
    assert raised.value.key_path.endswith("applies_to")


def test_rules_for_returns_exactly_the_matching_rules(tmp_path):
    """A node carries the invariant and check whose globs reach its writes, and
    carries neither the invariant nor the check that reaches elsewhere."""
    project = write_project_layer(tmp_path, DOMAIN_BOUNDARY)
    node = {"write_paths": ["models/tape.py"]}
    rules = rules_for("fixture", node, project_path=project)
    assert [invariant.id for invariant in rules.invariants] == ["converged-sections"]
    assert [check.id for check in rules.review_checks] == ["same-operator"]
    assert rules.model_paths == ("models/**",)
    assert rules.touches_models


def test_rules_for_is_empty_for_a_project_without_the_block(tmp_path):
    """A project that declares no domain block gets an empty result."""
    project = write_project_layer(
        tmp_path, "review:\n  tiers:\n    light_changed_lines: 50\n"
    )
    node = {"write_paths": ["models/tape.py"]}
    rules = rules_for("fixture", node, project_path=project)
    assert rules.invariants == ()
    assert rules.review_checks == ()
    assert rules.model_paths == ()
    assert not rules


def test_rules_for_accepts_an_object_node(tmp_path):
    """The node's write paths are read from an object as well as a mapping."""

    class Node:
        write_paths = ("lib/converters/divider.py",)

    project = write_project_layer(tmp_path, DOMAIN_BOUNDARY)
    rules = rules_for("fixture", Node(), project_path=project)
    assert [invariant.id for invariant in rules.invariants] == ["dc-limit"]
    assert [check.id for check in rules.review_checks] == ["perturbation-grows"]
    assert rules.model_paths == ()


def test_rules_for_matches_no_write_path_without_globs(tmp_path):
    """A node that writes outside every declared glob carries nothing."""
    project = write_project_layer(tmp_path, DOMAIN_BOUNDARY)
    rules = rules_for(
        "fixture", {"write_paths": ["docs/readme.md"]}, project_path=project
    )
    assert not rules
