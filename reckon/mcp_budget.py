"""Bound serialised MCP read answers before the transport receives them."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

RESPONSE_CEILING_ENV = "RECKON_MCP_RESPONSE_CEILING"
DEFAULT_RESPONSE_CEILING = 80_000
MINIMUM_RESPONSE_CEILING = 128

#: The most characters a *default* call of one read view may return, keyed by
#: ``(tool, view)``. The transport ceiling above is a guard that refuses an
#: oversized answer after the fact; this table is the contract each view is
#: built to, so a call with only its required arguments fits by construction and
#: a view that reaches the ceiling is a defect in that view rather than a
#: caller's mistake. A view answers inside its row by paging and by summarising
#: whole records; the runtime ceiling stays as the backstop for a caller that
#: asks for the lossless form explicitly.
VIEW_BUDGETS = {
    # roadmap: a per-plan table plus a page of graph findings.
    ("roadmap", "summary"): 20_000,
    ("roadmap", "detail"): 30_000,
    ("roadmap", "raw"): 80_000,
    # read_plan: one plan's page of state, never its whole section list.
    ("read_plan", "summary"): 24_000,
    ("read_plan", "detail"): 24_000,
    ("read_plan", "history"): 24_000,
    ("read_plan", "version"): 4_000,
    ("read_plan", "raw"): 60_000,
    ("read_plan", "schema"): 30_000,
    ("read_plan", "section"): 24_000,
    # read_plan view=why: one ref's state, waits, holders and last five events.
    ("read_plan", "why"): 24_000,
    # audit: counts for summary, a page of findings for detail.
    ("audit", "summary"): 24_000,
    ("audit", "detail"): 30_000,
    ("audit", "raw"): 80_000,
    # crew: one page of rows per view.
    ("crew", "summary"): 16_000,
    ("crew", "flight"): 16_000,
    ("crew", "live"): 24_000,
    ("crew", "scopes"): 24_000,
    ("crew", "drain"): 16_000,
    ("crew", "records"): 24_000,
    ("crew", "ledger"): 24_000,
    ("crew", "budget"): 16_000,
    ("crew", "lanes"): 24_000,
    ("crew", "routing"): 24_000,
    ("crew", "directory"): 16_000,
    ("crew", "fleet"): 16_000,
    ("crew", "runs"): 24_000,
    ("crew", "forgotten"): 16_000,
    ("crew", "obligations"): 24_000,
    ("crew", "models"): 16_000,
    ("crew", "velocity"): 24_000,
    ("crew", "scores"): 24_000,
    ("crew", "plan-review"): 24_000,
    ("crew", "findings"): 24_000,
}

#: The view a tool answers with when the call names none.
DEFAULT_VIEWS = {
    "roadmap": "summary",
    "read_plan": "summary",
    "audit": "summary",
    "crew": "summary",
}


def view_budget(tool: str, arguments: Mapping[str, Any]) -> int | None:
    """The character budget the view this call selects is held to, if declared."""

    view = arguments.get("view")
    if not isinstance(view, str) or not view.strip():
        view = DEFAULT_VIEWS.get(tool)
    if not isinstance(view, str):
        return None
    return VIEW_BUDGETS.get((tool, view))


def serialised_characters(value: Any) -> int:
    """Return the deterministic compact JSON character count for ``value``."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def response_ceiling(environment: Mapping[str, str] | None = None) -> int:
    """Read and validate the configured MCP response ceiling."""

    source = os.environ if environment is None else environment
    raw = source.get(RESPONSE_CEILING_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_RESPONSE_CEILING
    try:
        ceiling = int(raw)
    except ValueError as exc:
        raise ValueError(f"{RESPONSE_CEILING_ENV} must be an integer") from exc
    if ceiling < MINIMUM_RESPONSE_CEILING:
        raise ValueError(
            f"{RESPONSE_CEILING_ENV} must be at least {MINIMUM_RESPONSE_CEILING}"
        )
    return ceiling


def _omitted_counts(value: Any) -> dict[str, int]:
    mappings = 0
    fields = 0
    sequences = 0
    items = 0
    scalars = 0
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            mappings += 1
            fields += len(current)
            pending.extend(current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            sequences += 1
            items += len(current)
            pending.extend(current)
        else:
            scalars += 1
    return {
        "mappings": mappings,
        "fields": fields,
        "sequences": sequences,
        "items": items,
        "scalars": scalars,
    }


def _suggestion(argument: str, value: Any) -> dict[str, Any]:
    return {"argument": argument, "value": value}


def _narrow_with(tool: str, arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
    view = arguments.get("view")
    if tool == "roadmap":
        suggestions = [_suggestion("view", "summary")]
        if arguments.get("sprint") is None:
            suggestions.append(_suggestion("sprint", "<sprint-id>"))
        if view == "detail":
            suggestions.append(_suggestion("limit", 10))
        return suggestions
    if tool == "read_plan":
        suggestions = [_suggestion("view", "summary")]
        if arguments.get("slug") is None and arguments.get("resource") is None:
            suggestions.append(_suggestion("slug", "<resource-id>"))
        suggestions.append(_suggestion("limit", 10))
        return suggestions
    if tool == "crew":
        if view == "live":
            return [
                _suggestion("fields", ["classification", "node"]),
                _suggestion("session", "<coordinator-session>"),
            ]
        if view == "scopes":
            return [_suggestion("candidates", "<fewer candidate nodes>")]
        return [
            _suggestion("limit", 10),
            _suggestion("fields", ["classification"]),
        ]
    if tool == "audit":
        return [
            _suggestion("view", "summary"),
            _suggestion("limit", 10),
        ]
    return [_suggestion("limit", 10)]


def _bounded_notice(
    *,
    tool: str,
    original_characters: int,
    ceiling: int,
    omitted: dict[str, int],
    narrow_with: list[dict[str, Any]],
    budget: int | None = None,
) -> dict[str, Any]:
    truncated: dict[str, Any] = {
        "ceiling_characters": ceiling,
        "original_characters": original_characters,
        "omitted": {"characters": original_characters, **omitted},
        "narrow_with": narrow_with,
    }
    if budget is not None:
        truncated["view_budget_characters"] = budget
    full = {
        "ok": False,
        "error": "response_over_budget",
        "tool": tool,
        "truncated": truncated,
    }
    if serialised_characters(full) <= ceiling:
        return full

    compact = {
        "truncated": {
            "omitted": {"characters": original_characters},
            "narrow_with": [narrow_with[0]],
        }
    }
    if serialised_characters(compact) <= ceiling:
        return compact

    argument = str(narrow_with[0]["argument"])
    minimal = {
        "truncated": {
            "omitted": {"characters": original_characters},
            "narrow_with": [argument],
        }
    }
    if serialised_characters(minimal) > ceiling:
        raise ValueError(
            f"{RESPONSE_CEILING_ENV} is too small for a truncation receipt"
        )
    return minimal


def bound_response(
    response: Any,
    *,
    tool: str,
    arguments: Mapping[str, Any],
    ceiling: int | None = None,
) -> Any:
    """Return ``response`` unchanged when it fits, otherwise a bounded receipt."""

    configured_ceiling = response_ceiling() if ceiling is None else ceiling
    original_characters = serialised_characters(response)
    if original_characters <= configured_ceiling:
        return response
    return _bounded_notice(
        tool=tool,
        original_characters=original_characters,
        ceiling=configured_ceiling,
        omitted=_omitted_counts(response),
        narrow_with=_narrow_with(tool, arguments),
        budget=view_budget(tool, arguments),
    )
