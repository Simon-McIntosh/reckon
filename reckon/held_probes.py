"""Reviewed probes available to held blocker resources by stable id."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class ProbeFinding:
    arrived: bool
    finding: str


Probe = Callable[[Path, str, str], ProbeFinding]


def _path_exists(docs_dir: Path, project: str, subject: str) -> ProbeFinding:
    """Check a repository path declared relative to the project's docs root."""
    del project
    path = PurePosixPath(subject)
    if (
        not subject
        or path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath(".")
    ):
        raise ValueError("path-exists subject must be a docs-relative path")
    root = docs_dir.resolve()
    target = (root / subject).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path-exists subject escapes the docs root")
    arrived = target.exists()
    return ProbeFinding(arrived, f"{subject} {'exists' if arrived else 'is absent'}")


PROBES: dict[str, Probe] = {"path-exists": _path_exists}
