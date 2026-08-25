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


def test_every_configured_tool_namespace_has_declared_distribution() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    configured_namespaces = set(config.get("tool", {}))
    declared_distributions = _declared_distributions(config)

    unknown_namespaces = configured_namespaces - NAMESPACE_DISTRIBUTIONS.keys()
    unresolved_namespaces = {
        namespace: sorted(NAMESPACE_DISTRIBUTIONS[namespace])
        for namespace in configured_namespaces - unknown_namespaces
        if NAMESPACE_DISTRIBUTIONS[namespace].isdisjoint(declared_distributions)
    }

    assert not unknown_namespaces, (
        "Configured tool namespaces need an explicit distribution mapping: "
        f"{sorted(unknown_namespaces)}"
    )
    assert not unresolved_namespaces, (
        "Configured tool namespaces resolve to no declared distribution: "
        f"{unresolved_namespaces}"
    )
