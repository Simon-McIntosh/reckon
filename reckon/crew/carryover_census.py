"""Census of the context a run's first turn inherited from an earlier run's session.

A crew member owns a session so a later node can continue a conversation the
member started. When a *fresh* node is dispatched to a member whose session
already holds an earlier, unrelated node's turns, the harness resumes that
session: the new node's opening request carries the previous node's whole
gathered context, and every subsequent turn re-sends it. The node's own prompt
is a small part of what it pays for, and on a lane with a fixed context window
the inherited history is what finally overflows it.

This module walks the run directories and, for each run, reads the input its
*first* assistant turn carried in — the node's own prompt plus whatever history
the session already held — and attributes that history to the earlier run that
last used the same session.

Reading the first turn's own usage record rather than the run's terminal total is
the point of the measure. On the codex grammar the terminal total re-counts the
same history on every round-trip; on the claude grammar it is the run's un-cached
input, a function of run length and cache behaviour. Either way it describes the
whole run's spend rather than what the node read on its opening request, and a
fresh run with many turns can read as if it had inherited a great deal. The two
fields are not interchangeable and the census uses the first-turn one.

Dialects. Two stream grammars are present and they carry the figure differently:

* ``claude`` — the claude-code harness writes ``assistant`` records whose
  ``message.usage`` is that one turn's charged input, and a terminal ``result``
  whose ``modelUsage.inputTokens`` is the run's total *un-cached* input, the
  cached shares living in ``cacheReadInputTokens``/``cacheCreationInputTokens``.
  It is a run-length aggregate that tracks cache behaviour rather than turn
  count, so the census reads the first ``assistant`` record instead.
* ``codex`` — the codex harness writes no ``result`` record and no
  ``modelUsage``, and no per-assistant usage either: its only usage-bearing
  record is the run's single ``turn.completed``, whose ``usage.input_tokens`` is
  the charged input *summed over every model request the turn made*. A turn makes
  one request per tool round-trip, and each request re-sends the whole context,
  so that sum grows with how many tool round-trips the run performed, not with
  how much history it opened holding. The first request's own input — the
  quantity this census measures — is nowhere published on this dialect, so codex
  runs are reported with no first-turn figure and the run-cumulative sum is
  recorded separately. A codex run's inheritance therefore cannot be separated
  from its length, and the census declines to guess it rather than report the
  contaminated number.

Both readings use :func:`reckon.capabilities._charged_input_from_usage`, which
sums ``input_tokens`` with the disjoint cache read/create fields where a dialect
publishes them and leaves the already-total ``cached_input_tokens`` out where it
does not.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon.capabilities import (
    _charged_input_from_usage,
    _cumulative_event_input,
    _message_usage,
)
from reckon.crew.runs import runs_dir

#: The dialect lane a run belongs to, keyed by the harness grammar its stream
#: opens with.  The claude-code dialect is served on the local clive lane and
#: the codex dialect on the codex lane; a run whose backend is recorded keeps
#: that backend name instead.
DIALECT_LANE = {"claude": "clive", "codex": "codex"}

#: Characters-per-token estimate used to price a node's own prompt file.  It is
#: an estimate, recorded as such, and is only ever compared against inherited
#: tokens whose own order of magnitude it cannot change.
_CHARS_PER_TOKEN = 4


def _read_first_record(stream: Path) -> Mapping[str, Any] | None:
    """Return a stream's opening record, or None when it is unreadable."""

    try:
        with stream.open(encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                line = raw.strip()

                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, Mapping):
                    return record
    except OSError:
        return None
    return None


def _dialect(first: Mapping[str, Any]) -> str | None:
    """Name the stream grammar from its opening record."""

    kind = first.get("type")
    if kind == "thread.started":
        return "codex"
    if kind == "system":
        return "claude"
    return None


def _session_id(first: Mapping[str, Any], dialect: str | None) -> str | None:
    """Return the runtime session the run's harness reported, per dialect."""

    value = first.get("thread_id") if dialect == "codex" else first.get("session_id")
    value = str(value).strip() if value is not None else ""
    return value or None


def _first_turn_input(stream: Path, dialect: str | None) -> float | None:
    """Read the charged input carried by a run's first assistant turn.

    For the claude-code grammar this is the first ``assistant`` record's own
    usage.  The codex grammar publishes no per-request figure — its single
    ``turn.completed`` sums every request the turn made — so a codex run has no
    measurable first-turn input and this returns ``None`` for it; the run's
    cumulative sum is read separately by :func:`_run_cumulative_input`.

    The claude run's terminal ``result`` record is deliberately never consulted:
    its modelUsage is a run-length aggregate of un-cached input, a different
    quantity.
    """

    if dialect != "claude":
        return None

    try:
        with stream.open(encoding="utf-8", errors="ignore") as handle:
            seen: set[str] = set()
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                message_id = None
                if isinstance(message, Mapping):
                    message_id = str(message.get("id") or "").strip() or None
                if message_id is not None:
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                return _charged_input_from_usage(_message_usage(record))
    except OSError:
        return None
    return None


def _run_cumulative_input(stream: Path, dialect: str | None) -> float | None:
    """Read a run's terminal total is a distinct quantity, per dialect.

    On the codex grammar this is the run's single ``turn.completed`` usage — the
    sum over every request the turn made, which is where the codex figure lives
    once it is known not to be a first-turn quantity.  On the claude grammar it
    is the ``result`` record's ``modelUsage`` input for the run's model, the
    run-level un-cached total.  Recorded as a distinct field so a reader sees
    what was actually published rather than mistaking it for the first turn.
    """

    if dialect == "codex":
        try:
            with stream.open(encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, Mapping):
                        continue
                    if record.get("type") == "turn.completed":
                        return _cumulative_event_input(record)
        except OSError:
            return None
        return None

    entry = _result_model_usage_input(stream)
    value = entry.get("inputTokens") if isinstance(entry, Mapping) else None
    return _measured(value)


def _own_prompt_tokens(run_path: Path) -> int | None:
    """Estimate the node's own prompt size in tokens from its prompt file."""

    prompt = run_path / "prompt.txt"
    try:
        size = prompt.stat().st_size
    except OSError:
        return None
    return max(1, round(size / _CHARS_PER_TOKEN))


def _run_stamp(run_id: str) -> str:
    """Return a run's dispatch order key — its id's leading timestamp.

    A run id is ``r-<YYYYMMDDThhmmssffffff>-<slug>``; the key is the timestamp
    field, whose fixed-width layout orders lexicographically by dispatch time.
    """

    _, _, remainder = run_id.partition("-")
    stamp, _, _ = remainder.partition("-")
    return stamp or run_id


def _measured(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _median(values: Iterable[float]) -> float | None:
    collected = [value for value in values if value is not None]
    return statistics.median(collected) if collected else None


def carryover_census(
    runs_root: str | Path | None = None,
    *,
    recorded_backends: Mapping[str, str] | None = None,
    recorded_members: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Census every run directory for the context its first turn inherited.

    Each run's session id, dialect lane and first-turn charged input are read
    from its own stream.  A run *resumed* an existing session when an earlier
    run directory (by dispatch order) shares its session id, and that earlier
    run is recorded as the one that built the history the first turn carried in.

    The inherited figure prices the history a run carried in beyond its own
    prompt.  A run that opened its session carried in nothing, so its figure is
    exactly zero by construction (``inherited_basis`` says so).  A run that
    resumed a session sent its own prompt, plus whatever harness preamble the
    dialect wraps around it, plus the earlier run's turns; its inherited figure is
    the first-turn input less both the run's own prompt and that preamble, where
    the preamble is the median of ``first_turn - own_prompt`` over the runs that
    opened a session (those runs carried in nothing beyond their prompt and the
    preamble, so the residual prices the preamble itself).

    Subtracting a single population median from every run, rather than pricing
    each run's own prompt and the shared preamble, would report a fresh run whose
    prompt happens to be above the median as if it had inherited history; the
    positive control below catches exactly that.  A fresh run therefore measures
    zero inherited context, which is the positive control the census reports.

    The prompt-size estimate under ``characters_per_token`` makes the inherited
    figure an estimate; it is recorded as one, and its error is far below the
    hundreds of thousands of tokens an inherited context carries.

    ``recorded_backends``/``recorded_members`` let a caller supply whatever
    dispatch-time metadata it holds (a live pointer, the run store) keyed by run
    id; where a run's backend is not supplied the census falls back to its
    dialect's lane name.
    """

    root = Path(runs_root) if runs_root is not None else runs_dir()
    recorded_backends = recorded_backends or {}
    recorded_members = recorded_members or {}

    rows: list[dict[str, Any]] = []
    for path in sorted(Path(root).glob("*/stream.jsonl")):
        run_id = path.parent.name
        first = _read_first_record(path)
        if first is None:
            continue
        dialect = _dialect(first)
        if dialect is None:
            continue
        session_id = _session_id(first, dialect)
        first_turn = _first_turn_input(path, dialect)
        rows.append(
            {
                "run_id": run_id,
                "dialect": dialect,
                "session_id": session_id,
                "stream": str(path),
                "first_turn_input_tokens": first_turn,
                "first_turn_basis": (
                    "first-assistant-turn-usage"
                    if first_turn is not None
                    else "unavailable-in-dialect"
                ),
                "turn_cumulative_input_tokens": _run_cumulative_input(path, dialect),
                "own_prompt_tokens": _own_prompt_tokens(path.parent),
            }
        )

    # Order each session's runs by dispatch order; the earliest opened it.
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["session_id"]:
            by_session[row["session_id"]].append(row)
    for session_rows in by_session.values():
        session_rows.sort(key=lambda r: _run_stamp(r["run_id"]))

    for session_rows in by_session.values():
        prior: str | None = None
        for row in session_rows:
            row["resumed"] = prior is not None
            row["prior_run"] = prior
            prior = row["run_id"]

    # A session-opening run carried in nothing but its own prompt and the
    # harness preamble, so its first turn prices exactly those two; the median
    # residual over such runs estimates the shared preamble for the dialect.
    baselines: dict[str, float | None] = {}
    overheads: dict[str, float | None] = {}
    for dialect in sorted({row["dialect"] for row in rows}):
        opening = [
            row
            for row in rows
            if row["dialect"] == dialect
            and not row.get("resumed")
            and row["first_turn_input_tokens"] is not None
        ]
        baselines[dialect] = _median(row["first_turn_input_tokens"] for row in opening)
        overheads[dialect] = _median(
            max(0.0, row["first_turn_input_tokens"] - row["own_prompt_tokens"])
            for row in opening
            if row["own_prompt_tokens"] is not None
        )

    for row in rows:
        dialect = row["dialect"]
        overhead = overheads.get(dialect)
        first_turn = row["first_turn_input_tokens"]
        own = row["own_prompt_tokens"]
        backend = recorded_backends.get(row["run_id"]) or DIALECT_LANE.get(dialect)
        row["backend"] = backend
        row["backend_basis"] = (
            "recorded" if recorded_backends.get(row["run_id"]) else "dialect-lane"
        )
        row["member"] = recorded_members.get(row["run_id"])
        if row.get("resumed") is False:
            # Known to have carried nothing in: the positive control is exactly
            # zero, and zero by construction rather than by a threshold a large
            # prompt could slip past.
            row["inherited_tokens"] = 0.0
            row["inherited_basis"] = "session-opening"
            row["inherited_over_own_prompt"] = False
        elif first_turn is None or own is None or overhead is None:
            row["inherited_tokens"] = None
            row["inherited_basis"] = "unavailable"
            row["inherited_over_own_prompt"] = None
        else:
            inherited = max(0.0, first_turn - own - overhead)
            row["inherited_tokens"] = inherited
            row["inherited_basis"] = "first-turn-minus-own-prompt-and-harness-preamble"
            row["inherited_over_own_prompt"] = inherited > own

    per_backend: dict[str, dict[str, Any]] = {}
    for lane in sorted({str(row["backend"]) for row in rows}):
        lane_rows = [row for row in rows if str(row["backend"]) == lane]
        resumed = [row for row in lane_rows if row.get("resumed")]
        inherited = [
            row["inherited_tokens"]
            for row in resumed
            if row["inherited_tokens"] is not None
        ]
        over_own = [row for row in resumed if row["inherited_over_own_prompt"] is True]
        per_backend[lane] = {
            "runs": len(lane_rows),
            "resumed_runs": len(resumed),
            "resumed_inherited_over_own_prompt": len(over_own),
            "median_inherited_tokens": _median(inherited),
            "max_inherited_tokens": max(inherited) if inherited else None,
        }

    examples: dict[str, Any] = {}
    for dialect, sample_id in _example_runs(rows).items():
        row = next((r for r in rows if r["run_id"] == sample_id), None)
        if row is None:
            continue
        examples[dialect] = {
            "run_id": row["run_id"],
            "first_turn_input_tokens": row["first_turn_input_tokens"],
            "first_turn_basis": row["first_turn_basis"],
            "turn_cumulative_input_tokens": row["turn_cumulative_input_tokens"],
            "result_model_usage_input_tokens": _result_model_usage_input(
                Path(row["stream"])
            ),
        }

    return {
        "runs_dir": str(root),
        "baseline_basis": (
            "fresh-overhead = median over session-opening runs of "
            "first-turn-input minus own-prompt; a session-opening run's inherited "
            "figure is zero by construction; a resumed run's inherited figure is "
            "first-turn-input minus own-prompt minus fresh-overhead"
        ),
        "chars_per_token_estimate": _CHARS_PER_TOKEN,
        "fresh_baseline_tokens": baselines,
        "fresh_overhead_tokens": overheads,
        "dialect_notes": _dialect_notes(examples),
        "examples": examples,
        "per_backend": per_backend,
        "runs": rows,
    }


def recorded_dispatch_metadata(
    home: str | Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read per-run backend and member from whatever dispatch-time record survives.

    The live pointer is the only record that names both, but a promoted run's
    pointer is deleted; the run store keeps a backend for some of those.  Both
    are read here and merged, the live pointer winning where both know a run, so
    the census's per-backend aggregation covers every run whose lane was ever
    recorded rather than only the runs still in flight.  A run in neither store
    keeps its dialect lane, and its row notes the basis.
    """

    backends: dict[str, str] = {}
    members: dict[str, str] = {}

    if home is None:
        from reckon.crew.runs import crew_home

        home = crew_home()
    home = Path(home)

    live = home / "live"
    if live.is_dir():
        for pointer in sorted(live.glob("*.json")):
            if ".bak" in pointer.name or "before" in pointer.name:
                continue
            try:
                record = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, Mapping):
                continue
            run_id = str(record.get("run_id") or "").strip()
            if not run_id:
                continue
            backend = str(record.get("backend") or "").strip()
            member = str(record.get("member") or "").strip()
            if backend:
                backends[run_id] = backend
            if member:
                members[run_id] = member

    store = home / "run_store.db"
    if store.is_file():
        import sqlite3

        try:
            connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
            try:
                for run_id, detail in connection.execute(
                    "select run_id, detail from run_details"
                ):
                    if run_id in backends:
                        continue
                    try:
                        parsed = json.loads(detail)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    definition = (
                        parsed.get("node_definition")
                        if isinstance(parsed, Mapping)
                        else None
                    )
                    declaration = (
                        definition.get("lane_declaration")
                        if isinstance(definition, Mapping)
                        else None
                    )
                    backend = (
                        declaration.get("backend")
                        if isinstance(declaration, Mapping)
                        else None
                    )
                    if backend:
                        backends[str(run_id)] = str(backend)
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            pass

    return backends, members


def _example_runs(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Name one run of each dialect to illustrate what its stream publishes.

    The example must expose the cumulative field the notes contrast against the
    first-turn figure, so a run whose stream has not yet written one (still in
    flight) is skipped.  Among the rest the run with the largest cumulative
    figure is chosen, because the notes' point is that the figure is not a
    first-turn size and the largest one makes the divergence from the first turn
    unambiguous rather than a near-tie a reader could dismiss.
    """

    chosen: dict[str, str] = {}
    ordered = sorted(
        (row for row in rows if row.get("turn_cumulative_input_tokens") is not None),
        key=lambda r: (r["turn_cumulative_input_tokens"], _run_stamp(r["run_id"])),
        reverse=True,
    )
    for row in ordered:
        dialect = row["dialect"]
        if dialect in chosen or row.get("session_id") is None:
            continue
        chosen[dialect] = row["run_id"]
    return chosen


def _result_model_usage_input(stream: Path) -> dict[str, Any] | None:
    """Read a run's terminal modelUsage input, the run-total example.

    ``result.modelUsage`` keys one entry per model the run touched, and a run
    that used a small helper model alongside its main one carries an entry with a
    near-zero input count.  The entry reported is the one with the largest
    ``inputTokens`` — the model that actually carried the run's context — so the
    example illustrates the quantity rather than whichever helper sorted first.
    """

    try:
        with stream.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping) or record.get("type") != "result":
                    continue
                usage = record.get("modelUsage")
                if not isinstance(usage, Mapping):
                    continue
                best: dict[str, Any] | None = None
                for name, entry in usage.items():
                    if not isinstance(entry, Mapping):
                        continue
                    value = _measured(entry.get("inputTokens"))
                    if value is None:
                        continue
                    if best is None or value > best["inputTokens"]:
                        best = {
                            "model": name,
                            "inputTokens": value,
                            "contextWindow": entry.get("contextWindow"),
                        }
                if best is not None:
                    return best
    except OSError:
        return None
    return None


def _dialect_notes(examples: Mapping[str, Any]) -> dict[str, str]:
    """State in words what the aggregate field measures on each dialect."""

    clive = examples.get("claude", {})
    codex = examples.get("codex", {})
    clive_run = clive.get("run_id", "<clive run>")
    clive_usage = clive.get("result_model_usage_input_tokens") or {}
    codex_run = codex.get("run_id", "<codex run>")
    return {
        "clive": (
            "claude-code grammar: the terminal result record carries a "
            "modelUsage entry per model the run used, whose inputTokens is the "
            "run's total un-cached input — the share of every request's prompt "
            "served from outside the cache — while the same entry also reports "
            "cacheReadInputTokens and cacheCreationInputTokens. It is therefore "
            "a run-level aggregate that tracks the run's length and its cache "
            "behaviour rather than any single request: example run "
            f"{clive_run} reports modelUsage.inputTokens="
            f"{clive_usage.get('inputTokens')} against contextWindow="
            f"{clive_usage.get('contextWindow')} "
            f"while its first turn carried {clive.get('first_turn_input_tokens')}, "
            "and a run whose context is almost entirely cached reports a couple "
            "of hundred un-cached tokens against millions of cache-read ones. "
            "Neither reading is the first turn's size, so the census reads the "
            "first assistant turn's own assistant.message.usage instead."
        ),
        "codex": (
            "codex grammar: there is no result record and no modelUsage, and no "
            "per-assistant usage either. A run writes exactly one turn.completed, "
            "whose usage.input_tokens is the charged input summed over every "
            "model request the turn made — one request per tool round-trip, each "
            "re-sending the whole context — so the figure grows with the run's "
            "length and is not any single request's input or a first-turn size. "
            f"Example run {codex_run} reports turn.completed usage.input_tokens="
            f"{codex.get('turn_cumulative_input_tokens')}. Because the first "
            "request's own input is nowhere published on this dialect, a codex "
            "run's inherited context cannot be separated from how many tool "
            "round-trips it performed, and the census reports no first-turn "
            "figure for it rather than the contaminated sum."
        ),
    }
