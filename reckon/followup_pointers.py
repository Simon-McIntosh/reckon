"""Classify a followup's invocation as a pointer or as work that hides.

A followup carries one invocation line — its ``prompt``, or
``recommends_skill`` when the prompt holds none — in the grammar
``/reckon-VERB TARGET`` with an optional ``§<n>`` section. The roadmap
schedules sections, so a followup only helps a reader when it points at work
somewhere else: another plan, another mount's plan, a sprint, or its own plan's
section the plan declares ``implementable``. Anything else names work that no
surface can dispatch, and this module is the one place that says so.

The reason word is drawn from a fixed set. Hiding work reports ``no-section``,
``section-not-implementable``, ``host-complete`` or ``unparseable``; a pointer
reports ``other-plan``, ``other-mount``, ``sprint`` or
``implementable-section``.

``host-complete`` is judged before the section: a followup on a completed plan
hides work wherever it points inside its own plan, because the roadmap never
dispatches a completed plan and the work has to move to a live one. A
followup naming any other plan is a pointer regardless of this plan's status.

The section token composes the shared section-number core exported by
:mod:`reckon._plan_html` inside this module's own command frame, so a dotted or
hyphenated spelling the plan writer emits —
``§5.1``, ``§5-1`` — is captured whole and normalised by
:func:`reckon._plan_html.section_record_id` to the one identity the plan's
records carry (``s5-1``). An alphabetic suffix is rejected: the token is bounded
so ``§5a`` captures no section at all, because a suffix the plan writer never
emits is not a section.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from reckon._plan_html import SECTION_NUMBER_PATTERN, section_record_id
from reckon._schema import is_implementable_section, parse_plan_ref
from reckon.lifecycle import COMPLETED_STATUSES

#: Reason words this module may return for an open followup that hides work.
HIDING_REASONS = (
    "no-section",
    "section-not-implementable",
    "host-complete",
    "unparseable",
)

#: Reason words this module may return for a followup that is a pointer.
POINTER_REASONS = ("other-plan", "other-mount", "sprint", "implementable-section")

# The section token is the shared number core inside this command's frame. The
# trailing lookahead ends the token at a boundary, so an alphabetic suffix
# (``§5a``) captures no section — a suffix the plan writer never emits is not a
# section.
_INVOCATION_RE = re.compile(
    r"/reckon-[a-z][a-z0-9-]*"
    r"\s+(?P<target>[^\s§]+)"
    rf"(?:\s*§\s*(?P<section>{SECTION_NUMBER_PATTERN})(?![0-9A-Za-z.-]))?"
)

_TRAILING_PUNCTUATION = ".,;"


@dataclass(frozen=True)
class Invocation:
    """One parsed ``/reckon-VERB TARGET §N`` line."""

    target: str
    section: str | None


@dataclass(frozen=True)
class FollowupVerdict:
    """What one open followup's invocation names, and the reason for it."""

    pointer: bool
    reason: str

    @property
    def hides_work(self) -> bool:
        return not self.pointer


def parse_invocation(text: str) -> Invocation | None:
    """Return the first ``/reckon-VERB TARGET [§N]`` line in ``text``.

    ``TARGET`` is one non-space token; the section, when present, is normalised
    to the plan's section identity (``§2`` → ``s2``, ``§5.1`` and ``§5-1`` →
    ``s5-1``). ``None`` means no invocation line is present at all.
    """

    match = _INVOCATION_RE.search(str(text or ""))
    if match is None:
        return None
    target = match.group("target").rstrip(_TRAILING_PUNCTUATION)
    if not target or target.startswith("§"):
        return None
    number = match.group("section")
    return Invocation(
        target=target, section=section_record_id(number) if number else None
    )


def find_invocation(followup: Mapping[str, Any]) -> Invocation | None:
    """Parse ``followup``'s invocation: its prompt, else its recommended skill."""

    for field in ("prompt", "recommends_skill"):
        parsed = parse_invocation(str(followup.get(field) or ""))
        if parsed is not None:
            return parsed
    return None


def _host_complete(plan: Mapping[str, Any]) -> bool:
    status = str(plan.get("workflow_status") or plan.get("status") or "")
    return status.strip().lower() in COMPLETED_STATUSES


def classify_followup(
    plan: Mapping[str, Any],
    followup: Mapping[str, Any],
    *,
    project: str = "",
    declarations: Mapping[str, Any] | None = None,
    sprint_ids: Collection[str] = (),
) -> FollowupVerdict:
    """Answer whether one open followup on ``plan`` is a pointer or hides work.

    ``plan`` is the parsed host plan; ``project`` names its mount (falling back
    to the plan row's own ``project``); ``declarations`` is the plan's
    ``section_declarations`` mapping when the caller already resolved it,
    otherwise the row's own. ``sprint_ids`` are the project's sprint identities,
    bare or qualified, so a qualifier naming a sprint reads as one. The schema
    predicate admits a same-plan section until reclassification; landing
    evidence alone does not retire the work a followup names.
    """

    host_slug = str(plan.get("slug") or "").strip()
    host_project = str(project or plan.get("project") or "").strip()
    invocation = find_invocation(followup)
    if invocation is None:
        return FollowupVerdict(False, "unparseable")

    target = invocation.target
    ref = parse_plan_ref(target)
    if ref is not None and ref.stage:
        return FollowupVerdict(False, "unparseable")

    sprints = {str(identity).strip() for identity in sprint_ids}
    if target in sprints or (ref is not None and ref.slug in sprints):
        return FollowupVerdict(True, "sprint")
    if ref is None:
        return FollowupVerdict(False, "unparseable")
    if ref.project is not None and host_project and ref.project != host_project:
        return FollowupVerdict(True, "other-mount")
    if ref.slug != host_slug:
        return FollowupVerdict(True, "other-plan")

    if _host_complete(plan):
        return FollowupVerdict(False, "host-complete")
    if invocation.section is None:
        return FollowupVerdict(False, "no-section")
    declared = (
        declarations if declarations is not None else plan.get("section_declarations")
    ) or {}
    if is_implementable_section(declared.get(invocation.section)):
        return FollowupVerdict(True, "implementable-section")
    return FollowupVerdict(False, "section-not-implementable")
