"""Keep configured tool namespaces backed by declared distributions."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
NAMESPACE_DISTRIBUTIONS = {
    "hatch": {"hatchling", "hatch-vcs"},
    "pytest": {"pytest"},
    "ruff": {"ruff"},
}


def _distribution_name(requirement: str) -> str:
    """Return a normalized distribution name from a PEP 508 requirement."""

    name = re.split(r"[<>=!~;\[\s]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_distributions(config: dict[str, object]) -> set[str]:
    project = config.get("project", {})
    build_system = config.get("build-system", {})
    dependency_groups = config.get("dependency-groups", {})

    requirements = [
        *project.get("dependencies", []),
        *build_system.get("requires", []),
    ]
    for group_requirements in dependency_groups.values():
        requirements.extend(group_requirements)

    return {_distribution_name(requirement) for requirement in requirements}


def _tooling_failures(
    config: dict[str, object],
) -> tuple[set[str], dict[str, list[str]]]:
    """Configured namespaces with no distribution mapping, split by failure kind."""

    configured_namespaces = set(config.get("tool", {}))
    declared_distributions = _declared_distributions(config)

    unknown = configured_namespaces - NAMESPACE_DISTRIBUTIONS.keys()
    unresolved = {
        namespace: sorted(NAMESPACE_DISTRIBUTIONS[namespace])
        for namespace in configured_namespaces - unknown
        if NAMESPACE_DISTRIBUTIONS[namespace].isdisjoint(declared_distributions)
    }
    return unknown, unresolved


def test_every_configured_tool_namespace_has_declared_distribution() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    unknown, unresolved = _tooling_failures(config)

    assert not unknown, (
        "Configured tool namespaces need an explicit distribution mapping: "
        f"{sorted(unknown)}"
    )
    assert not unresolved, (
        f"Configured tool namespaces resolve to no declared distribution: {unresolved}"
    )


def test_configured_but_undeclared_tool_fails_until_declared() -> None:
    """Synthesise the tree before a declaration: [tool.ruff] still configured, nothing declares it."""

    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    assert "ruff" in config["tool"]

    pre_declaration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    pre_declaration["dependency-groups"]["dev"] = [
        requirement
        for requirement in pre_declaration["dependency-groups"]["dev"]
        if _distribution_name(requirement) != "ruff"
    ]

    unknown, unresolved = _tooling_failures(pre_declaration)
    assert unknown == set()
    assert unresolved == {"ruff": ["ruff"]}
    assert len(unresolved) == 1  # one failing namespace while the tool is undeclared

    unknown, unresolved = _tooling_failures(config)
    assert unknown == set()
    assert unresolved == {}
    assert len(unresolved) == 0  # zero failing namespaces once the tool is declared
