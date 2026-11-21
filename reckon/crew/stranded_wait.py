"""What could release a request held on its upstream waits.

A request stored ``awaiting-upstream`` is not one condition. Its upstream may be
being built right now; it may be nobody's work at all; or it may name a section
that can never land, because the plan carrying it is gone or closed. The three
read differently to the session that owns the request: the first needs nothing,
the second is a prompt to the owner after a threshold, and the third is work
already lost and is retired on the pass that finds it.

The class is derived here from the same per-ref state the admission pass already
resolves, so a wait is never judged twice. Nothing in this module writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon import flight

#: The request's upstream is being built or queued, so nothing is owed.
PROGRESSING = "progressing"

#: Every unlanded upstream is still open and nothing is building it.
IDLE = "idle"

#: An upstream names something that can never land, so the wait cannot resolve.
STRANDED = "stranded"

WAIT_CLASSES: tuple[str, ...] = (PROGRESSING, IDLE, STRANDED)

#: How long an idle wait may stand before its owner is told once.
DEFAULT_REPORT_AFTER_HOURS = 72.0

#: The flight block and slot that carry the idle report threshold.
REPORT_AFTER_HOURS_KEY = "stranded_wait"
REPORT_AFTER_HOURS_SLOT = "report_after_hours"


def report_after_hours(config: Mapping[str, Any] | None = None) -> float:
    """The configured idle-report threshold in hours, or the shipped default.

    Zero is honoured: an operator who sets it means that any idle wait is
    reported on the pass that finds it. Only a figure the schema cannot hold -- a
    boolean, a non-number, or a negative -- falls back to the shipped default,
    because such a value is a broken layer rather than a threshold.
    """
    resolved: Mapping[str, Any]
    if config is not None:
        resolved = config
    else:
        try:
            resolved = flight.resolve().config
        except Exception:  # noqa: BLE001 - an unreadable flight layer is the default
            resolved = {}
    block = resolved.get(REPORT_AFTER_HOURS_KEY)
    value = block.get(REPORT_AFTER_HOURS_SLOT) if isinstance(block, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return DEFAULT_REPORT_AFTER_HOURS
    return float(value)


def classify(
    *,
    stranded: Sequence[str],
    working: Sequence[str],
    unlanded: Sequence[str],
) -> tuple[str, str]:
    """The class one waiting request carries, and the account a reader shows.

    ``stranded`` names the upstreams that can never land, ``working`` the runs
    still on their way to landing an upstream, and ``unlanded`` the upstream
    sections that have not landed yet. A stranded upstream outranks the others:
    one that cannot land will not be released by anything still working.
    """
    if stranded:
        return STRANDED, "cannot land: " + "; ".join(stranded)
    if working:
        return PROGRESSING, ""
    if unlanded:
        return IDLE, "nobody is building " + ", ".join(unlanded)
    return PROGRESSING, ""


def section_cannot_land(
    ref: str,
    *,
    project: str,
    docs_dir: Path | None,
    inventory: Mapping[str, Mapping[str, Any]],
) -> str:
    """Why one section ref can never land, or the empty string when it can.

    Three facts make a section unreachable: its plan document is absent, the
    plan is closed, superseded or abandoned with the section not done, or the
    plan carries no such section. An unreadable plan is none of them -- a plan
    whose document exists but does not parse is withheld, because retiring work
    on the strength of a failed read would discard a request a working read
    would have released.
    """
    from reckon._schema import parse_plan_ref
    from reckon.lifecycle import TERMINAL_STATUSES

    from .admission import wait_refs as _wait_refs

    if docs_dir is None:
        return ""
    parsed = parse_plan_ref(str(ref))
    if parsed is None or not parsed.stage:
        return ""
    target_project = parsed.project or project
    target_docs = _wait_refs._docs_for_project(
        target_project, current_project=project, current_docs=docs_dir
    )
    if target_docs is None:
        return f"{ref}: project {target_project!r} is not mounted"
    slug = parsed.slug
    section = parsed.stage
    item = inventory.get(slug)
    if item is None:
        if not (Path(target_docs) / "plans" / f"{slug}.html").is_file():
            return f"{ref}: plan {slug!r} does not exist"
        return ""
    if str(_wait_refs._section_status(item, section)).lower() == "done":
        return ""
    status = str(item.get("workflow_status") or item.get("status") or "")
    if status in TERMINAL_STATUSES:
        return f"{ref}: plan {slug!r} is {status} and {slug}#{section} is not done"
    declarations = item.get("section_declarations") or {}
    known = {str(key) for key in declarations}
    if known and section not in known:
        return f"{ref}: plan {slug!r} carries no section {section!r}"
    return ""
