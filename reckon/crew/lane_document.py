"""Read one published lane document field by field, degrading each on its own.

A serving lane publishes a small JSON document about its own occupancy: what
it is running and waiting on, how many concurrent requests it will carry, the
context it spends per request, its KV occupancy, its remaining headroom, the
state it is in, and the instant it took the lane's reading. The fields do not
arrive together. A lane with nothing resident publishes ``state: measured``
beside a ``null`` mean context and a ``null`` binding, and a lane that declines
one figure still publishes the rest. The reader therefore resolves each field
independently: a missing precondition costs its own field and never the whole
document, and a field that did arrive is never reported as unknown because a
sibling did not.

The engine's headroom does not account for a router admission FIFO. When a
document carries ``router_generation_gate``, this reader carries its width,
in-flight count and waiting count and computes the slots available at the
router. A newer ``admission`` block may publish that calculation directly;
that value is preferred to local arithmetic. The reported headroom is the
minimum of the engine and admission readings, so queued work is visible to the
caller that decides whether to send another request.

Two properties are the point of the module, and each exists because its
opposite was observed on a live document:

* **Per-field degradation in both directions.** A ``null`` or absent field
  resolves to ``unknown`` while its siblings keep their measured values, and a
  field that is present resolves even though a sibling is ``unknown``. The
  reader never invents a figure: a value that is not a number is ``unknown``,
  never zero and never a ceiling, because a zero and a ceiling are both claims
  the document did not make.
* **Freshness travels with the figure.** A reading older than the lane's own
  ``suggested_shelf_life_seconds`` is not dropped and not served as current:
  its age is carried beside it and a ``stale`` marker states plainly that the
  figure no longer describes the present. Dropping it hides a real figure;
  serving it silently presents an old one as new.

The reader never raises. A dispatch consults it outside a ``try`` block, so a
document that is absent, unreadable, not JSON, or not an object returns every
field unknown with a reason rather than an exception the caller must guard.

This module reads the document's point-in-time figures as well as its rolling
ones: this shape reports the rolling value under a bare key and the value taken
at the reading's instant under a ``_instant`` key, and the instant value is the
one that describes the moment the lane observed. The ``withheld`` block is
deliberately not read: a figure the lane withheld is a figure the lane declined
to publish as its own, so it stays ``unknown`` here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

UNKNOWN = "unknown"

# Every field the reader resolves independently. Each maps to the document
# keys that may carry it, most specific first: the bare key names the rolling
# figure and the ``_instant`` key the figure the lane took at ``observed_at``.
# Reading both means a document publishing only the instant figure still
# resolves the field.
_FIELD_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("headroom", ("headroom", "headroom_instant")),
    ("running", ("running",)),
    ("waiting", ("waiting",)),
    (
        "concurrent_requests",
        ("concurrent_requests", "concurrent_requests_instant"),
    ),
    ("mean_context", ("mean_context", "mean_context_instant")),
    ("kv_occupancy", ("kv_occupancy",)),
    ("state", ("state",)),
    ("observed_at", ("observed_at",)),
)

# The fields the document carries as numbers. A value that is not one of these
# resolves to ``unknown`` rather than being coerced.
_NUMERIC_FIELDS = frozenset(
    {
        "headroom",
        "running",
        "waiting",
        "concurrent_requests",
        "mean_context",
        "kv_occupancy",
    }
)

SHELF_LIFE_KEY = "suggested_shelf_life_seconds"
GATE_KEY = "router_generation_gate"
ADMISSION_KEY = "admission"
_GATE_FIELDS = ("width", "in_flight", "waiting")

# What each admission verdict means for a caller deciding whether to send work.
# A lane publishes congested, full, open or paused, and the reason must name
# which one this is: a paused lane whose reason read "the gate is open" would
# licence exactly the dispatch the lane was declining. An unrecognised verdict
# is stated as itself rather than folded into the nearest known one.
_VERDICT_CLAUSE = {
    "open": "the gate is open",
    "congested": "the gate is congested and requests are queued",
    "full": "the gate is full, at or over its width",
    "paused": "admission is paused",
}


def _number(value: object) -> int | float | None:
    """Return ``value`` when it is a real number, else None.

    ``bool`` is a subclass of ``int`` and is rejected, so a JSON ``true`` is
    never read as the number one.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _text(value: object) -> str | None:
    """Return a non-empty string for ``value``, else None."""

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _first_value(document: Mapping[str, Any], keys: tuple[str, ...]) -> object:
    """Return the first present, non-null value among ``keys``, else None."""
    for key in keys:
        value = document.get(key)
        if value is not None:
            return value
    return None


def _parse_stamp(value: object) -> datetime | None:
    """Parse an ISO-8601 stamp into an aware datetime, or None."""
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _unknown_gate() -> dict[str, Any]:
    """Return the carried gate shape when no gate was published."""
    return dict.fromkeys(_GATE_FIELDS, UNKNOWN)


def _gate_reading(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the router gate counters without inventing a missing count."""
    raw = payload.get(GATE_KEY)
    if not isinstance(raw, Mapping):
        return _unknown_gate()
    return {
        name: value if (value := _number(raw.get(name))) is not None else UNKNOWN
        for name in _GATE_FIELDS
    }


def _admission_reading(
    payload: Mapping[str, Any], gate: Mapping[str, Any]
) -> tuple[int | float | str, str, str]:
    """Resolve admission headroom, verdict and reason from the published shape."""
    admission = payload.get(ADMISSION_KEY)
    published_headroom: int | float | None = None
    published_verdict: str | None = None
    published_reason: str | None = None
    if isinstance(admission, Mapping):
        published_headroom = _number(admission.get("headroom"))
        published_verdict = _text(admission.get("verdict"))
        published_reason = _text(admission.get("reason"))

    if published_headroom is not None:
        available: int | float = published_headroom
        source = "published admission headroom"
    else:
        width = _number(gate.get("width"))
        in_flight = _number(gate.get("in_flight"))
        waiting = _number(gate.get("waiting"))
        if width is None or in_flight is None or waiting is None:
            return (
                UNKNOWN,
                UNKNOWN,
                "admission headroom is unknown: gate counts are incomplete",
            )
        available = width - in_flight - waiting
        source = f"width {width:g} - in_flight {in_flight:g} - waiting {waiting:g}"

    verdict = published_verdict
    if verdict is None:
        verdict = "congested" if available <= 0 else "open"
    reason = published_reason
    if reason is None:
        clause = _VERDICT_CLAUSE.get(verdict, f"the verdict is {verdict}")
        reason = f"admission headroom is {available:g} ({source}); {clause}"
    return available, verdict, reason


def _minimum_headroom(engine_headroom: object, admission_headroom: object) -> object:
    """Return the smaller measured headroom, or unknown when either is absent."""
    engine = _number(engine_headroom)
    admission = _number(admission_headroom)
    if engine is None or admission is None:
        return UNKNOWN
    return min(engine, admission)


def blank_report(*, detail: str, malformed: bool = False) -> dict[str, Any]:
    """Return every field unknown, with a reason and the malformed marker."""
    report: dict[str, Any] = {name: UNKNOWN for name, _ in _FIELD_KEYS}
    report["stale"] = False
    report["age_seconds"] = None
    report["shelf_life_seconds"] = None
    report["unknown_fields"] = [name for name, _ in _FIELD_KEYS]
    report["detail"] = detail
    report["malformed"] = malformed
    report["engine_headroom"] = UNKNOWN
    report["router_generation_gate"] = _unknown_gate()
    report["admission_headroom"] = UNKNOWN
    report["admission_verdict"] = UNKNOWN
    report["admission_reason"] = UNKNOWN
    return report


def _as_mapping(
    document: object,
) -> tuple[Mapping[str, Any] | None, dict[str, Any] | None]:
    """Coerce a document into a mapping, or return the blank report to use."""
    if document is None:
        return None, blank_report(detail="no lane document")
    if isinstance(document, Mapping):
        return document, None
    if isinstance(document, (str, bytes, bytearray)):
        raw = bytes(document) if isinstance(document, (bytes, bytearray)) else document
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            return None, blank_report(
                detail=f"lane document is not valid JSON: {exc}", malformed=True
            )
        if not isinstance(decoded, Mapping):
            return None, blank_report(
                detail=f"lane document holds {type(decoded).__name__}, not a JSON object",
                malformed=True,
            )
        return decoded, None
    return None, blank_report(
        detail=f"lane document is {type(document).__name__}, not a JSON object",
        malformed=True,
    )


def read_lane_document(
    document: object, *, now: datetime | None = None
) -> dict[str, Any]:
    """Resolve every field of one published lane document independently.

    ``document`` is the parsed JSON object, or the raw JSON text, or None.
    Every field named in ``_FIELD_KEYS`` comes back with its own value or
    ``UNKNOWN``; ``stale``, ``age_seconds`` and ``shelf_life_seconds`` carry
    the freshness of the reading, ``unknown_fields`` names the fields that did
    not resolve, ``malformed`` marks a document the reader could not parse at
    all, and ``detail`` states the reason in one line. The function never
    raises.
    """
    payload, failure = _as_mapping(document)
    if failure is not None:
        return failure
    assert payload is not None  # narrowed by _as_mapping's contract

    report: dict[str, Any] = {}
    unknown_fields: list[str] = []
    for name, keys in _FIELD_KEYS:
        value = _first_value(payload, keys)
        if name in _NUMERIC_FIELDS:
            resolved: object = _number(value)
        else:
            resolved = _text(value)
        if resolved is None:
            report[name] = UNKNOWN
            unknown_fields.append(name)
        else:
            report[name] = resolved

    gate = _gate_reading(payload)
    report["router_generation_gate"] = gate
    report["engine_headroom"] = _number(payload.get("engine_headroom"))
    if report["engine_headroom"] is None:
        report["engine_headroom"] = report["headroom"]
    admission_headroom, admission_verdict, admission_reason = _admission_reading(
        payload, gate
    )
    report["admission_headroom"] = admission_headroom
    report["admission_verdict"] = admission_verdict
    report["admission_reason"] = admission_reason
    if admission_headroom != UNKNOWN:
        report["headroom"] = _minimum_headroom(
            report["engine_headroom"], admission_headroom
        )

    shelf = _number(payload.get(SHELF_LIFE_KEY))
    report["shelf_life_seconds"] = shelf if shelf is not None and shelf > 0 else None

    observed = _parse_stamp(payload.get("observed_at"))
    if observed is None:
        report["age_seconds"] = None
        report["stale"] = False
    else:
        reference = now if now is not None else datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        age = (reference - observed).total_seconds()
        age = max(0.0, age)
        report["age_seconds"] = int(age)
        report["stale"] = bool(
            report["shelf_life_seconds"] is not None
            and age > report["shelf_life_seconds"]
        )

    report["unknown_fields"] = unknown_fields
    report["malformed"] = False

    details: list[str] = []
    if report["stale"]:
        details.append(
            f"reading is {report['age_seconds']}s old, older than its "
            f"{report['shelf_life_seconds']:g}s shelf life"
        )
    elif report["shelf_life_seconds"] is None and observed is not None:
        details.append("document states no shelf life, so freshness is unjudged")
    if report["unknown_fields"]:
        details.append("fields unknown: " + ", ".join(report["unknown_fields"]))
    report["detail"] = "; ".join(details)
    return report


def read_lane_document_file(
    path: str | Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Read a lane document from disk and resolve it field by field.

    An unreadable path returns the all-unknown report naming the failure,
    never an exception, so a dispatch may call this outside a ``try`` block.
    """
    resolved_path = Path(path).expanduser()
    try:
        raw = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        return blank_report(
            detail=f"lane document {str(resolved_path)!r} cannot be read — {exc}"
        )
    report = read_lane_document(raw, now=now)
    if report.get("malformed"):
        report["detail"] = f"lane document {str(resolved_path)!r}: {report['detail']}"
    return report
