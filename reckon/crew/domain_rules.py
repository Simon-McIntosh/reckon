"""Which of a repository's declared domain rules apply to one node.

A repository declares its domain rules once, in the resolved flight config's
``domain`` block (``docs/state/<project>/flight.yaml``). This module is the one
place that answers which of those rules a node touches, so the brief a node is
dispatched with, the review it receives and the checks run at promotion all
read the same match rather than composing their own.

The block is read through the generated model, so a renamed field is a loud
failure here rather than a silent empty match. An absent block changes nothing:
a project that declares no ``domain`` gets an empty result and keeps today's
behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

from reckon import flight
from reckon._flight_schema import DomainConfig, DomainInvariant, DomainReviewCheck

__all__ = ["DomainRules", "rules_for"]


@dataclass(frozen=True)
class DomainRules:
    """The declared domain rules that apply to one node.

    ``invariants`` are the rules a result must satisfy; ``review_checks`` are
    the questions a review of this node must answer; ``model_paths`` holds the
    declared model-location globs the node's write paths matched, so a caller
    can tell a node that touches the modelling surface.
    """

    invariants: tuple[DomainInvariant, ...] = ()
    review_checks: tuple[DomainReviewCheck, ...] = ()
    model_paths: tuple[str, ...] = ()

    @property
    def touches_models(self) -> bool:
        """Whether any declared model path matched the node's write paths."""
        return bool(self.model_paths)

    def __bool__(self) -> bool:
        return bool(self.invariants or self.review_checks or self.model_paths)


def _has_wildcard(pattern: str) -> bool:
    return any(char in pattern for char in "*?[")


def _normalize(raw: str) -> str:
    """Return a write path or pattern with a leading ``./`` removed."""
    text = str(raw).strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _path_matches_write_path(write_path: str, pattern: str) -> bool:
    """Whether one declared glob governs one of a node's write paths.

    A glob matches on the whole path (``reckon/crew/*.py`` reaches
    ``reckon/crew/domain_rules.py``), and a bare pattern naming no separator
    matches any write path whose basename it names. A pattern carrying no
    wildcard also governs everything beneath it, so ``reckon/crew`` reaches a
    file it contains.
    """
    path = _normalize(write_path)
    pattern = _normalize(pattern)
    if not path or not pattern:
        return False
    if fnmatchcase(path, pattern):
        return True
    if "/" not in pattern and fnmatchcase(PurePosixPath(path).name, pattern):
        return True
    if not _has_wildcard(pattern):
        prefix = pattern.rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return False


def _matches(globs: Sequence[str], write_paths: Sequence[str]) -> bool:
    """Whether any of ``globs`` governs any of ``write_paths``."""
    return any(
        _path_matches_write_path(path, glob) for glob in globs for path in write_paths
    )


def _write_paths(node: Any) -> tuple[str, ...]:
    """Read a node's declared write paths from a record or an object."""
    if isinstance(node, Mapping):
        raw = node.get("write_paths") or ()
    else:
        raw = getattr(node, "write_paths", ()) or ()
    return tuple(str(path) for path in raw if str(path).strip())


def rules_for(
    project: str,
    node: Any,
    *,
    project_path: str | None = None,
    host_path: str | None = None,
    shipped_path: str | None = None,
    checkout_path: str | None = None,
) -> DomainRules:
    """Return the domain rules that apply to ``node`` in ``project``.

    The block is read through the generated model, so a field the schema renames
    fails here rather than silently matching nothing. The node is matched by its
    declared write paths. A project that declares no ``domain`` block, or a node
    that matches none of its globs, yields an empty result. ``project_path`` and
    ``host_path`` select the flight layers, so a caller reading a fixture config
    does not have to install one.
    """
    resolved = flight.resolve(
        project=project,
        project_path=project_path,
        host_path=host_path,
        shipped_path=shipped_path,
        checkout_path=checkout_path,
    )
    block = resolved.config.get("domain")
    if not isinstance(block, Mapping):
        return DomainRules()
    config = DomainConfig.model_validate(dict(block))
    write_paths = _write_paths(node)
    invariants = tuple(
        invariant
        for invariant in (config.invariants or ())
        if _matches(invariant.applies_to, write_paths)
    )
    checks = tuple(
        check
        for check in (config.review_checks or ())
        if _matches(check.applies_to, write_paths)
    )
    model_paths = tuple(
        glob for glob in (config.model_paths or ()) if _matches([glob], write_paths)
    )
    return DomainRules(
        invariants=invariants,
        review_checks=checks,
        model_paths=model_paths,
    )
