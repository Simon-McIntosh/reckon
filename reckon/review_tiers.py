"""Resolve a finished run to a review tier from what it actually changed.

A review is sized to the risk of the node it examines. The tier is decided at
completion — from the run's changed paths, its changed-line count and its
declared specification level — rather than from the node's role, so a
documentation node that touches no runtime source is not reviewed at all and a
one-line source fix is not read as carefully as a rewrite.

Three tiers partition every run:

``full``   The run changes runtime source, or its plan or section declares
           capability risk ``elevated`` or ``critical``. This is the review
           that has always existed:  a full read of the diff against the brief.
``light``  The run changes runtime source by fewer than the configured
           changed-line ceiling, at a specification level that fixes the
           done-when, with no elevated risk. A light review answers whether the
           diff does what the brief says, and nothing else.
``none``   The run changes no runtime source — tests, plans, evidence, research
           data or figures, alone or together. The merged-head gate re-run
           checks it instead, and the ledger row records this tier as the
           reason no review exists.

Runtime source is the classification :func:`reckon.path_classes.path_class`
already makes for the velocity view, imported here rather than re-implemented:
it counts package source and the SPA the server delivers, and it excludes the
tests, documents and state a reviewer would read for meaning rather than risk.
"""

from __future__ import annotations

from collections.abc import Iterable

from reckon.flight import DEFAULT_LIGHT_CHANGED_LINES
from reckon.path_classes import path_class

FULL = "full"
LIGHT = "light"
NONE = "none"

# Specification levels that fix the done-when tightly enough for a light review.
# An exact brief names the change; a guided brief names the plan section that
# does. An open brief leaves the approach to the worker, and a reviewer cannot
# check a diff against intent the brief never fixed, so open work is reviewed
# at full however small the diff.
LIGHT_SPEC_LEVELS = frozenset({"exact", "guided"})

# Plan-declared capability risk levels that force a full review regardless of
# size, because the work touches a guard, a fence or a security boundary where
# a single light read could not see the failure mode.
ELEVATED_RISKS = frozenset({"elevated", "critical"})

# The SPA the server delivers is runtime source even though it lives under
# ``docs/``; ``path_class`` carries that override, ``file_class`` does not.
RUNTIME_SOURCE_CLASS = "source"


def changes_runtime_source(changed_paths: Iterable[str]) -> bool:
    """Whether any changed path is runtime source rather than a document or test.

    Every path is classified, and one runtime-source path is enough: a node
    that fixes one source file and edits its own test still owes a review of
    the source.
    """
    return any(path_class(str(path)) == RUNTIME_SOURCE_CLASS for path in changed_paths)


def elevated_risk(capability_risk: str | None) -> bool:
    """Whether a declared capability risk forces a full review."""
    return str(capability_risk or "").strip().lower() in ELEVATED_RISKS


def review_tier(
    changed_paths: Iterable[str],
    changed_lines: int,
    spec_level: str | None,
    capability_risk: str | None = None,
    *,
    light_changed_lines: int = DEFAULT_LIGHT_CHANGED_LINES,
) -> str:
    """Return ``full``, ``light`` or ``none`` for one finished run.

    ``changed_paths`` are the repository-relative paths the run changed,
    ``changed_lines`` is their added-plus-deleted count, ``spec_level`` is the
    run's declared specification level and ``capability_risk`` is the risk its
    plan or section declares. ``light_changed_lines`` is the light tier's
    ceiling, read from the resolved flight config rather than fixed here.

    Precedence, highest first: a declared elevated or critical risk is always
    ``full``; a run changing no runtime source is ``none``; a runtime-source run
    earns ``light`` only below the ceiling at a fixing specification level and
    is otherwise ``full``. An unreadable ``changed_lines`` is treated as over
    the ceiling, so a caller that could not measure the diff gets the fuller
    review rather than the lighter one.
    """
    if elevated_risk(capability_risk):
        return FULL
    if not changes_runtime_source(changed_paths):
        return NONE
    try:
        lines = int(changed_lines)
    except (TypeError, ValueError):
        return FULL
    ceiling = int(light_changed_lines)
    if lines < ceiling and str(spec_level or "") in LIGHT_SPEC_LEVELS:
        return LIGHT
    return FULL
