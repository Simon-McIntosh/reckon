"""Which of a repository's declared domain rules apply to one node.

A repository declares its domain rules once, in the resolved flight config's
``domain`` block (``docs/state/<project>/flight.yaml``). This module is the one
place that answers which of those rules a node touches, so the feed of a brief,
the review a node receives and the checks run at promotion all read the same
match rather than composing their own.

An absent block changes nothing: a project that declares no ``domain`` gets an
empty result and keeps today's behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

from reckon import flight

__all__ = ["DomainRules", "Invariant", "ReviewCheck", "rules_for"]


@dataclass(frozen=True)
class Invariant:
    """One rule a result must satisfy, as the caller reads it."""

    id: str
    rule: str
    applies_to: tuple[str, ...]
    check: str = ""


@dataclass(frozen=True)
class ReviewCheck:
    """One question a review of a node must answer, as the caller reads it."""

    id: str
    question: str
    applies_to: tuple[str, ...]


@dataclass(frozen=True)
class DomainRules:
    """The declared domain rules that apply to one node.

    ``invariants`` are the rules a result must satisfy; ``review_checks`` are
    the questions a review of this node must answer; ``model_paths`` holds the
    declared model-location globs the node's write paths matched, so a caller
    can tell a node that touches the modelling surface.
    """

    invariants: tuple[Invariant, ...] = ()
    review_checks: tuple[ReviewCheck, ...] = ()
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


def _strings(value: Any) -> tuple[str, ...]:
    """Read a declared string list, tolerating an absent entry."""
    return tuple(str(item) for item in value or ())


def _invariant(entry: Mapping[str, Any]) -> Invariant:
    return Invariant(
        id=str(entry.get("id") or ""),
        rule=str(entry.get("rule") or ""),
        applies_to=_strings(entry.get("applies_to")),
        check=str(entry.get("check") or ""),
    )


def _review_check(entry: Mapping[str, Any]) -> ReviewCheck:
    return ReviewCheck(
        id=str(entry.get("id") or ""),
        question=str(entry.get("question") or ""),
        applies_to=_strings(entry.get("applies_to")),
    )


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

    The block is read from the resolved flight config, which the resolver has
    already validated against the schema, so this maps what it finds rather
    than re-checking its shape. The node is matched by its declared write
    paths. A project that declares no ``domain`` block, or a node that matches
    none of its globs, yields an empty result. ``project_path`` and
    ``host_path`` select the flight layers, so a caller reading a fixture
    config does not have to install one.
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
    write_paths = _write_paths(node)
    invariants: list[Invariant] = []
    for entry in block.get("invariants") or ():
        if not isinstance(entry, Mapping):
            continue
        invariant = _invariant(entry)
        if _matches(invariant.applies_to, write_paths):
            invariants.append(invariant)
    review_checks: list[ReviewCheck] = []
    for entry in block.get("review_checks") or ():
        if not isinstance(entry, Mapping):
            continue
        check = _review_check(entry)
        if _matches(check.applies_to, write_paths):
            review_checks.append(check)
    model_paths = tuple(
        str(glob)
        for glob in block.get("model_paths") or ()
        if _matches([str(glob)], write_paths)
    )
    return DomainRules(
        invariants=tuple(invariants),
        review_checks=tuple(review_checks),
        model_paths=model_paths,
    )
