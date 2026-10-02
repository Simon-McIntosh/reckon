"""Per-lane run-time distributions and the local lane's live load.

A router weighing where to send a node needs two figures the ledger and the
serving lane already hold: how long a run of a given shape has taken to return,
and how loaded the local lane is right now. This module derives both.

:func:`run_time_profile` reads completed ledger rows over a trailing window and
groups them by the four fields that describe a node's shape -- serving backend,
effort, role and specification level. For each group it reports how many runs
the group held, the median and 90th-percentile wall time, the median output
tokens, and the fraction of that group's closed gate verdicts that passed. It
reports the same wall time by node size for each backend, bucketed first on the
declared time budget the row recorded and on output tokens only where no budget
was recorded; the bucket key it used is named beside the buckets.

:func:`local_lane_load` reads the local serving document and reports the lane's
load: nodes running, nodes waiting, remaining headroom, the worker slots the
router's admission block offers, and the mean tokens per second its generating
population achieved, with the instant the reading was taken.

Two properties are the point of the module.

* **A missing figure is ``None``, never zero.** A group with no closed gate
  verdict has an undefined pass fraction, not a zero one; a bucket with no runs
  has no median. Reading an undefined figure as a measured zero would let a
  router weigh a lane it never heard from. Every ratio and every median here is
  ``None`` when its denominator is empty, and the load reader carries the lane
  document's ``unknown`` and a null observed stamp through as ``None``.
* **A figure that is not a number is not coerced.** A JSON ``true`` is rejected
  by the numeric guard, so a boolean never counts as a one, and a stamp or text
  field resolves to ``None`` rather than being read as a figure it never made.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from reckon import ledger
from reckon.crew import lane_document
from reckon.crew.paid_lanes import local_lane_path

#: Bucket labels for a run classified by its declared time budget, ascending.
#: The top of each band is inclusive, so a 30-minute node sits in the first and
#: a 60-minute node in the second.
BUDGET_BUCKETS = ("up_to_30m", "30m_to_60m", "over_60m")
#: Bucket labels for a run classified by its output tokens, ascending.
TOKEN_BUCKETS = ("under_20k", "20k_to_100k", "over_100k")

#: The two keys a size bucket may be computed from, named on the result.
BUDGET_KEY = "time_budget"
OUTPUT_SIZE_KEY = "output_tokens"
MIXED_KEY = "mixed"

#: The gate verdicts that are closed: a pass or a fail. ``not-run`` is not a
#: verdict and is excluded from the pass fraction rather than counted as a fail.
PASSED = "passed"
FAILED = "failed"

_MINUTES_RE = re.compile(
    r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>m|min|minute|minutes|h|hr|hour|hours)?$"
)


def _number(value: object) -> int | float | None:
    """Return ``value`` when it is a real number, else ``None``.

    ``bool`` is a numeric subclass and is rejected, so a JSON ``true`` never
    counts as the number one.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _text(value: object) -> str | None:
    """Return a non-empty stripped string for ``value``, else ``None``."""

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _percentile(values: list[float], quantile: float) -> float | None:
    """Return the nearest-rank ``quantile`` of ``values``, or ``None``.

    Nearest rank needs no interpolation, so the 90th percentile of a small
    sample is one of its members rather than a figure between two, and a test
    can name the member it expects. The rank is clamped into range so a
    quantile of zero still returns the smallest member.
    """

    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(quantile * len(ordered))
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


def _median(values: list[float]) -> float | None:
    """Return the median of ``values``, or ``None`` when there are none."""

    if not values:
        return None
    return statistics.median(values)


def _output_tokens(row: Mapping[str, Any]) -> int | float | None:
    """Read a row's output tokens, from the budget block or the throughput row.

    The budget block is the primary source; the throughput block is the fallback
    for a row promoted before the budget block carried tokens. A row that
    recorded neither resolves to ``None``.
    """

    budget = row.get("budget")
    if isinstance(budget, Mapping):
        tokens = budget.get("tokens")
        if isinstance(tokens, Mapping):
            value = _number(tokens.get("output_tokens"))
            if value is not None:
                return value
    throughput = row.get("throughput")
    if isinstance(throughput, Mapping):
        return _number(throughput.get("generated_tokens"))
    return None


def _budget_minutes(value: object) -> float | None:
    """Parse a declared time budget into minutes, or ``None``.

    The ledger records budgets as strings such as ``"60m"`` or ``"45m"``; an
    ``"h"`` suffix is honoured too, and a bare number is read as minutes. A
    value with no usable magnitude resolves to ``None`` so it falls through to
    the token key rather than landing in the smallest band by default.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value)
    if text is None:
        return None
    match = _MINUTES_RE.match(text.lower())
    if match is None:
        return None
    magnitude = float(match.group("value"))
    unit = match.group("unit") or "m"
    if unit.startswith("h"):
        return magnitude * 60.0
    return magnitude


def _size_class(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Classify one row into a size bucket, naming the key it used.

    The declared time budget is preferred, per the row's recorded value; output
    tokens are the fallback when no budget parses. A row carrying neither
    resolves to ``(None, None)`` and contributes to no bucket.
    """

    minutes = _budget_minutes(row.get("time_budget"))
    if minutes is not None:
        if minutes <= 30:
            return BUDGET_KEY, "up_to_30m"
        if minutes <= 60:
            return BUDGET_KEY, "30m_to_60m"
        return BUDGET_KEY, "over_60m"
    tokens = _output_tokens(row)
    if tokens is None:
        return None, None
    if tokens < 20000:
        return OUTPUT_SIZE_KEY, "under_20k"
    if tokens <= 100000:
        return OUTPUT_SIZE_KEY, "20k_to_100k"
    return OUTPUT_SIZE_KEY, "over_100k"


def _identity(
    row: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return the (backend, effort, role, spec_level) key for a row."""

    agent = row.get("agent") if isinstance(row.get("agent"), Mapping) else {}
    backend = _text(row.get("backend")) or (
        _text(agent.get("backend")) if isinstance(agent, Mapping) else None
    )
    effort = _text(agent.get("effort")) if isinstance(agent, Mapping) else None
    return (backend, effort, _text(row.get("role")), _text(row.get("spec_level")))


def _wall_seconds(row: Mapping[str, Any]) -> float | None:
    """Return the row's recorded wall seconds, when it is a number."""

    return _number(row.get("wall_seconds"))


def _group_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build the per-shape group table from the selected rows."""

    buckets: dict[tuple, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if isinstance(row, Mapping):
            buckets[_identity(row)].append(row)

    groups: list[dict[str, Any]] = []
    for key in sorted(
        buckets, key=lambda k: tuple("" if part is None else part for part in k)
    ):
        members = buckets[key]
        wall = [value for row in members if (value := _wall_seconds(row)) is not None]
        tokens = [
            value for row in members if (value := _output_tokens(row)) is not None
        ]
        passed = sum(1 for row in members if _text(row.get("gate")) == PASSED)
        failed = sum(1 for row in members if _text(row.get("gate")) == FAILED)
        closed = passed + failed
        backend, effort, role, spec_level = key
        groups.append(
            {
                "backend": backend,
                "effort": effort,
                "role": role,
                "spec_level": spec_level,
                "runs": len(members),
                "wall_seconds_median": _median(wall),
                "wall_seconds_p90": _percentile(wall, 0.90),
                "output_tokens_median": _median(tokens),
                "passed": passed,
                "failed": failed,
                "passed_fraction": (passed / closed) if closed else None,
            }
        )
    return groups


def _size_table(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build the per-backend size-bucket table from the selected rows."""

    per_backend: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    keys_used: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        agent = row.get("agent") if isinstance(row.get("agent"), Mapping) else {}
        backend = _text(row.get("backend")) or _text(agent.get("backend"))
        if backend is None:
            continue
        key, label = _size_class(row)
        if key is None or label is None:
            continue
        keys_used.setdefault(backend, set()).add(key)
        wall = _wall_seconds(row)
        if wall is not None:
            per_backend[backend][label].append(wall)

    table: list[dict[str, Any]] = []
    for backend in sorted(per_backend):
        used = keys_used.get(backend, set())
        if used == {BUDGET_KEY}:
            key_name = BUDGET_KEY
            labels = BUDGET_BUCKETS
        elif used == {OUTPUT_SIZE_KEY}:
            key_name = OUTPUT_SIZE_KEY
            labels = TOKEN_BUCKETS
        else:
            key_name = MIXED_KEY
            labels = BUDGET_BUCKETS + TOKEN_BUCKETS
        buckets = {
            label: {
                "runs": len(per_backend[backend].get(label, [])),
                "wall_seconds_median": _median(per_backend[backend].get(label, [])),
            }
            for label in labels
        }
        table.append({"backend": backend, "key": key_name, "buckets": buckets})
    return table


def run_time_profile(
    project: str,
    days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive per-lane run-time distributions over a trailing window.

    ``project`` names the ledger to read, ``days`` the trailing window length,
    and ``now`` the window's end (defaulting to the current UTC instant). The
    rows are read through :func:`reckon.ledger.runs`, filtered to those whose
    completion stamp falls inside the window.

    Returns the window boundaries it used, the number of rows it read, the
    per-shape ``groups`` table and the per-backend ``size_buckets`` table. Every
    figure that has no population behind it is ``None``.
    """

    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    since = reference - timedelta(days=days)
    rows = ledger.runs(project, since=since.isoformat())
    selected = [row for row in rows if isinstance(row, Mapping)]
    return {
        "project": project,
        "days": days,
        "since": since.isoformat(),
        "until": reference.isoformat(),
        "rows": len(selected),
        "groups": _group_rows(selected),
        "size_buckets": _size_table(selected),
    }


def _known(value: object) -> object | None:
    """Return ``value`` unless it is the lane reader's ``unknown`` marker."""

    if value is None or value == lane_document.UNKNOWN:
        return None
    return value


def local_lane_load() -> dict[str, Any]:
    """Report the local serving lane's live load.

    Reads the published lane document through
    :func:`reckon.crew.lane_document.read_lane_document` for its count,
    headroom, stamp and throughput fields and
    :func:`reckon.crew.lane_document.read_lane_admission` for the router's
    worker-slot arithmetic. Returns ``running``, ``waiting``, ``headroom``,
    ``worker_slots`` and ``mean_tokens_per_second`` beside the instant the
    reading was taken (``read_at``) and the lane's own ``observed_at`` stamp.

    Every field the document did not publish is ``None`` -- a missing lane
    document is not a lane with zero of anything -- and the function never
    raises.
    """

    read_at = datetime.now(UTC)
    document: object = None
    try:
        text = local_lane_path().read_text(encoding="utf-8")
    except OSError:
        text = None
    if text is not None:
        try:
            document = json.loads(text)
        except ValueError:
            document = None

    reading = lane_document.read_lane_document(document, now=read_at)
    admission = lane_document.read_lane_admission(document)
    throughput = lane_document.read_lane_throughput(
        document,
        reading_stamp=str(reading.get("observed_at") or ""),
        reading_age_seconds=int(reading.get("age_seconds") or 0),
        now=read_at,
    )
    return {
        "read_at": read_at.isoformat(),
        "observed_at": _known(reading.get("observed_at")),
        "state": _known(reading.get("state")),
        "running": _known(reading.get("running")),
        "waiting": _known(reading.get("waiting")),
        "headroom": _known(reading.get("headroom")),
        "worker_slots": _known(admission.get("worker_slots")),
        "mean_tokens_per_second": _known(throughput.get("mean_tokens_per_second")),
        "detail": reading.get("detail") or "",
    }
