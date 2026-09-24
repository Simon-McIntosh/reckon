"""Bound serialised MCP read answers before the transport receives them."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

RESPONSE_CEILING_ENV = "RECKON_MCP_RESPONSE_CEILING"
DEFAULT_RESPONSE_CEILING = 80_000
MINIMUM_RESPONSE_CEILING = 128


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
) -> dict[str, Any]:
    full = {
        "ok": False,
        "error": "response_over_budget",
        "tool": tool,
        "truncated": {
            "ceiling_characters": ceiling,
            "original_characters": original_characters,
            "omitted": {"characters": original_characters, **omitted},
            "narrow_with": narrow_with,
        },
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
    )
