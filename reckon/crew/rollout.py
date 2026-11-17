"""Read per-session quota and token receipts from Codex client rollouts.

This surface is deliberately separate from :mod:`reckon.crew.metering`, which
derives cumulative token usage from the crew event stream.  The client rollout
contains the per-request usage needed to measure the largest request; the crew
stream does not.  Both surfaces are correct for the evidence they carry.

The client's quota ``used_percent`` is quantised to whole percentage points.
One run can therefore appear to consume zero, one, two, or three points, and a
run that consumed four tenths of a point is indistinguishable from one that
consumed nothing.  Per-run attribution from this field is not sound; aggregates
over many runs are.  This module returns the raw quota reading and deliberately
does not compute a per-run delta.

The notional spend a receipt can carry is a different measurement: it is
derived from the declared per-million rates and the run's measured tokens,
never from quota movement and never from the harness's own cost figure.  The
surcharge rule belongs to :mod:`reckon.crew.quota_weight`, which this module
calls rather than restating.  The figure is deliberately read beside the rate
pair that produced it, and a lane without a dated rate is explicitly unpriced
rather than priced at a number that can go stale.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum, auto
from pathlib import Path

from reckon._timestamps import parse_iso
from reckon.crew.quota_weight import RequestTokenUsage, UnknownQuotaWeight, quota_weight
from reckon.file_memo import file_signature

CLIENT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# A fenced run writes its client rollout under its own run directory's harness
# home rather than the client's session home.  One owner resolves that root for
# every reader, so a receipt lookup and the paid-lanes account scan cannot drift
# on where a fenced run keeps its rollout.
RUN_HOME_SESSIONS = ("codex-home", "sessions")

# The published rule counts only requests strictly above 272,000 input tokens.
REQUEST_INPUT_CROSSING_THRESHOLD = 272_000

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\Z")


class Unmeasured(StrEnum):
    """Reasons a rollout receipt cannot support a returned figure."""

    MISSING_ROLLOUT = auto()
    UNREADABLE_ROLLOUT = auto()
    NO_TOKEN_COUNT = auto()
    NO_TOTAL_TOKEN_USAGE = auto()
    NO_REQUEST_TOKEN_USAGE = auto()
    NO_CONTEXT_WINDOW = auto()
    NO_RATE_LIMITS = auto()
    NO_RATE_LIMIT_VALUE = auto()
    NO_BOUNDED_TOOL_SPANS = auto()
    NO_MODEL_IDENTIFIER = auto()
    NO_DATED_RATE = auto()


WEEKLY_WINDOW_MINUTES = 7 * 24 * 60


@dataclass(frozen=True, slots=True)
class RateBasis:
    """The dated rate pair a notional spend figure was derived from.

    A reader who wants to recompute the figure, or to judge how current it is,
    holds the same pair the figure was priced at: the per-million input and
    output rates and the date they were published.  Only a priced lane carries
    a basis; a lane without a dated rate carries the unmeasured marker instead,
    so the basis never pretends a figure was computed when none was.
    """

    model_identifier: str
    input_per_million: float
    output_per_million: float
    as_of: date


@dataclass(frozen=True, slots=True)
class QuotaReading:
    """One quota window, retained with the dimensions that identify it.

    The quantisation limit is carried on the reading itself, not only in this
    module's docstring: ``used_percent`` reports whole percentage points, so a
    run consuming four tenths of a point is indistinguishable from one
    consuming nothing, and a surface reading the figure must be able to say so.
    """

    window_minutes: int
    used_percent: int | float | Unmeasured
    resets_at: int | float | Unmeasured
    used_percent_quantisation: float = 1.0


@dataclass(frozen=True, slots=True)
class RolloutReceipt:
    """Measured client receipt values, with an explicit marker for every gap."""

    cumulative_input_tokens: int | Unmeasured
    cumulative_cached_input_tokens: int | Unmeasured
    cumulative_output_tokens: int | Unmeasured
    maximum_request_input_tokens: int | Unmeasured
    requests_over_threshold: int | Unmeasured
    model_context_window: int | Unmeasured
    quota_readings: Mapping[int, QuotaReading] | Unmeasured
    plan_type: str | Unmeasured
    generation_seconds: float | Unmeasured
    machine_seconds: float | Unmeasured
    # Notional spend derived from declared per-million rates and the run's
    # measured tokens. The name is deliberately distinct from the ledger's
    # cost_usd_imputed flag, which marks a figure nulled because its lane is
    # unmetered: a computed figure must never be tagged with the flag whose
    # consumers read it as a reason to discard the cost.
    notional_cost_usd: float | Unmeasured = Unmeasured.NO_MODEL_IDENTIFIER
    rate_basis: RateBasis | Unmeasured = Unmeasured.NO_MODEL_IDENTIFIER

    def quota_for_window(self, window_minutes: int) -> QuotaReading | Unmeasured:
        """Return the reading for one window length without positional lookup."""
        if isinstance(self.quota_readings, Unmeasured):
            return self.quota_readings
        return self.quota_readings.get(window_minutes, Unmeasured.NO_RATE_LIMIT_VALUE)

    @property
    def weekly_quota(self) -> QuotaReading | Unmeasured:
        """Return the seven-day reading, wherever it appeared in the receipt."""
        return self.quota_for_window(WEEKLY_WINDOW_MINUTES)

    @property
    def short_horizon_quota(self) -> QuotaReading | Unmeasured:
        """Return the shortest positive window shorter than one week."""
        if isinstance(self.quota_readings, Unmeasured):
            return self.quota_readings
        short_windows = (
            window
            for window in self.quota_readings
            if 0 < window < WEEKLY_WINDOW_MINUTES
        )
        window = min(short_windows, default=None)
        if window is None:
            return Unmeasured.NO_RATE_LIMIT_VALUE
        return self.quota_readings[window]

    @property
    def five_hour_quota(self) -> QuotaReading | Unmeasured:
        """Return the account's short-horizon reading."""
        return self.short_horizon_quota

    @property
    def quota_used_percent(self) -> int | float | Unmeasured:
        """Compatibility view of the weekly utilisation."""
        reading = self.weekly_quota
        return reading if isinstance(reading, Unmeasured) else reading.used_percent

    @property
    def quota_window_minutes(self) -> int | Unmeasured:
        """Compatibility view of the weekly window length."""
        reading = self.weekly_quota
        return reading if isinstance(reading, Unmeasured) else reading.window_minutes

    @property
    def quota_resets_at(self) -> int | float | Unmeasured:
        """Compatibility view of the weekly reset time."""
        reading = self.weekly_quota
        return reading if isinstance(reading, Unmeasured) else reading.resets_at


def _unmeasured(reason: Unmeasured) -> RolloutReceipt:
    return RolloutReceipt(*(reason for _ in RolloutReceipt.__dataclass_fields__))


def run_home_sessions_root(run_dir: str | Path) -> Path:
    """The client-session root inside one crew run's harness home."""
    return Path(run_dir).joinpath(*RUN_HOME_SESSIONS)


# ── The one stat identity and the one listing cache ─────────────────────────
#
# The monitor's two reads of a harness home — a rollout receipt and a run's
# spend — walk the same directories and ask the same question of a file: has it
# moved since the last read?  Both ask it here rather than each spelling the
# fields out, so a correction to the rule reaches both and a new field is added
# in one place.  The identity itself is ``reckon.file_memo``'s to declare: it
# carries the change time beside the modification time, so an in-place rewrite
# that leaves a file's length and stamp where they were still reads as a change
# rather than as an unchanged file.

# A directory's own identity, size and stamp move when an entry is created or
# removed in it, so an unchanged directory answers from its last listing while a
# new attempt's day directory still forces a fresh one.  A station tick resolves
# the same session's rollout and enumerates the same run's streams on every
# pass, so one store serves both.  It is cleared whole when it reaches its
# bound, the shape the completion-stamp store uses: a long-lived reader forgets
# rather than growing without limit.
_FileIdentity = tuple[int, int, int, int, int]
_LISTINGS: dict[Path, tuple[_FileIdentity, tuple[str, ...]]] = {}
_LISTINGS_LIMIT = 4096
_LISTINGS_LOCK = threading.Lock()


def file_identity(path: Path) -> _FileIdentity | None:
    """One file or directory's stat identity, or None if it cannot be read.

    The fields and their order are ``reckon.file_memo``'s to declare rather
    than this module's, so a reader that folds a file's bytes and a reader that
    lists a directory agree on what "the same file" means — and an identity
    field added there is seen by both without another copy to keep in step.
    """
    try:
        return file_signature(path)
    except OSError:
        return None


def directory_entries(directory: Path) -> tuple[str, ...]:
    """One directory's entry names, listed again only when it has changed."""
    signature = file_identity(directory)
    if signature is None:
        return ()
    with _LISTINGS_LOCK:
        stored = _LISTINGS.get(directory)
        if stored is not None and stored[0] == signature:
            return stored[1]
    try:
        names = tuple(os.listdir(directory))
    except OSError:
        return ()
    with _LISTINGS_LOCK:
        if len(_LISTINGS) >= _LISTINGS_LIMIT:
            _LISTINGS.clear()
        _LISTINGS[directory] = (signature, names)
    return names


def fold_continues(
    *, previous: _FileIdentity, offset: int, signature: _FileIdentity
) -> bool:
    """Whether a stored fold still describes the file it was read from.

    The same object grown is extended from its offset.  A different object (a
    replaced file), a shorter file (a truncated one), or one rewritten in place
    is folded again from the start, because its earlier bytes are no longer the
    ones the state was folded from.  A rewrite at the same length moves the
    change time, so it is a change even where its length and stamp hold still.

    Both readers of a harness home ask this of their own store, so the rule is
    written once here rather than in each.
    """
    if previous[:2] != signature[:2]:
        return False
    if signature[2] < offset:
        return False
    return not (signature[2] == offset and previous != signature)


def _subdirectories(directory: Path) -> list[Path]:
    return [
        child
        for child in (directory / name for name in directory_entries(directory))
        if child.is_dir()
    ]


def _session_rollouts(root: Path, session_id: str) -> list[Path]:
    """Rollouts under one client-session root: ``*/*/*/rollout-*-<session>.jsonl``."""
    pattern = re.compile(rf"rollout-.*-{re.escape(session_id)}\.jsonl\Z")
    found: list[Path] = []
    for year in _subdirectories(root):
        for month in _subdirectories(year):
            for day in _subdirectories(month):
                found.extend(
                    day / name
                    for name in directory_entries(day)
                    if pattern.match(name) and (day / name).is_file()
                )
    return found


def run_home_rollout_paths(session_id: str, *, run_id: str | None = None) -> list[Path]:
    """Rollout files one session may occupy under the crew run homes.

    A fenced run keeps its rollout in its own run directory, so a receipt
    lookup that searched only the client's home missed it.  Both the receipt
    itself and the paid-lanes account scan resolve this location through the
    same two functions rather than each spelling the harness layout out.
    """
    from reckon.crew import runs as crew_runs

    runs_root = crew_runs.runs_dir()
    if run_id is not None:
        run_dirs = [runs_root / run_id]
    else:
        run_dirs = _subdirectories(runs_root)
    paths: list[Path] = []
    for run_dir in run_dirs:
        root = run_home_sessions_root(run_dir)
        if root.is_dir():
            paths.extend(_session_rollouts(root, session_id))
    return paths


def _locate_rollout(session_id: str, *, run_id: str | None = None) -> Path | None:
    if _SESSION_ID.fullmatch(session_id) is None:
        return None
    candidates = _session_rollouts(CLIENT_SESSIONS_DIR, session_id)
    candidates.extend(run_home_rollout_paths(session_id, run_id=run_id))
    return max(candidates, key=str, default=None)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> int | float | None:
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _quota_number(
    quota: Mapping[str, object] | None, key: str
) -> int | float | Unmeasured:
    if quota is None:
        return Unmeasured.NO_RATE_LIMITS
    value = _number(quota.get(key))
    return value if value is not None else Unmeasured.NO_RATE_LIMIT_VALUE


def _quota_text(quota: Mapping[str, object] | None, key: str) -> str | Unmeasured:
    if quota is None:
        return Unmeasured.NO_RATE_LIMITS
    value = quota.get(key)
    return value if isinstance(value, str) and value else Unmeasured.NO_RATE_LIMIT_VALUE


def _quota_readings(
    rate_limits: Mapping[str, object] | None,
) -> Mapping[int, QuotaReading] | Unmeasured:
    if rate_limits is None:
        return Unmeasured.NO_RATE_LIMITS

    readings: dict[int, QuotaReading] = {}
    for name in ("primary", "secondary"):
        quota = rate_limits.get(name)
        if not isinstance(quota, Mapping):
            continue
        window_minutes = _integer(quota.get("window_minutes"))
        if window_minutes is None or window_minutes <= 0:
            continue
        readings[window_minutes] = QuotaReading(
            window_minutes=window_minutes,
            used_percent=_quota_number(quota, "used_percent"),
            resets_at=_quota_number(quota, "resets_at"),
        )
    return readings


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a rollout record's ISO timestamp, tolerating the trailing Z.

    A stamp that states its zone is returned aware, one that names no zone is
    returned naive, and the distinction is read from the parsed result's
    ``tzinfo`` rather than from the text's shape — so an offset that carries
    seconds is no longer mistaken for a zoneless wall clock.
    """
    return parse_iso(value)


@dataclass(slots=True)
class _SegmentTotal:
    """A cumulative client reading folded across its resets as it grows.

    The client's running token total resets when a session compacts its
    context; every reset shows up as a decrease in the reading.  Between
    resets the reading climbs from the first reading after the reset to the
    consumed amount.  The first segment starts from zero — its first reading
    is itself already-consumed input — so it is counted in full; each later
    segment contributes its climb above the reset base.  A single monotone run
    therefore reproduces the last reading exactly, while a session that
    compacted once or more reconstructs a total that no single reading
    carries.

    Folding as the readings arrive answers identically to walking the whole
    series, and keeps a four-number state rather than every reading a grown
    rollout has ever carried.
    """

    completed: int = 0
    base: int = 0
    previous: int = 0
    first_segment: bool = True
    seen: bool = False

    def observe(self, value: int) -> None:
        if not self.seen:
            self.seen = True
            self.base = self.previous = value
            return
        if value < self.previous:
            if self.first_segment:
                self.completed += self.previous
                self.first_segment = False
            else:
                self.completed += self.previous - self.base
            self.base = value
        self.previous = value

    def total(self) -> int:
        if self.first_segment:
            return self.completed + self.previous
        return self.completed + self.previous - self.base


def _bounded_tool_span(
    spans: Sequence[tuple[datetime, datetime]],
    earliest: datetime,
    latest: datetime,
) -> tuple[float, float, float] | None:
    """Fold bounded tool intervals into wall, machine and model seconds.

    A bounded tool span joins a tool request to its result — the window the
    model sat waiting on the machine.  Overlapping spans (parallel tool calls)
    are joined so a wait is charged once, and the model's own time is wall
    minus that joined wait.  Returns None only when no span is bounded, which
    is the explicit-unmeasured state rather than a claim of zero model time.
    """
    if not spans:
        return None
    intervals = sorted(spans)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    machine = sum((end - start).total_seconds() for start, end in merged)
    wall = (latest - earliest).total_seconds()
    generation = max(0.0, wall - machine)
    return wall, machine, generation


def _notional_price(
    request_usage: Sequence[RequestTokenUsage],
    model_identifier: str | None,
) -> tuple[float | Unmeasured, RateBasis | Unmeasured]:
    """Derive a notional spend figure and its rate basis for one rollout.

    The figure is surcharge-aware by delegating to :func:`quota_weight`, the
    same application lane evidence uses, rather than restating its rule: a
    request with input above the long-context threshold is charged at the
    published whole-request multipliers, and the declared per-million rates
    price the surcharged quantities.  An absent model identity is ``None``
    only when the caller did not supply one; a lane whose rate pair carries no
    ``as_of`` date is explicitly unpriced.  Neither absence is ever a zero.
    """
    if model_identifier is None:
        marker = Unmeasured.NO_MODEL_IDENTIFIER
        return marker, marker
    if not request_usage:
        marker = Unmeasured.NO_REQUEST_TOKEN_USAGE
        return marker, marker
    priced = quota_weight(model_identifier, request_usage)
    if isinstance(priced, UnknownQuotaWeight):
        marker = Unmeasured.NO_DATED_RATE
        return marker, marker
    rate = priced.rate
    usd = (
        priced.surcharged_input_tokens * rate.input_per_million
        + priced.surcharged_output_tokens * rate.output_per_million
    ) / 1_000_000
    return (
        round(usd, 4),
        RateBasis(
            model_identifier=model_identifier,
            input_per_million=rate.input_per_million,
            output_per_million=rate.output_per_million,
            as_of=rate.as_of,
        ),
    )


@dataclass(slots=True)
class _ReceiptFold:
    """One rollout read once, extended by the bytes appended since.

    The fields are the state a whole-file read folds a rollout into, kept
    between reads: a terminal run's rollout no longer changes, so its receipt
    cannot move between ticks, and a grown one is decoded only past this
    offset.  A replaced or truncated file discards the fold, because its
    earlier bytes are no longer the ones the state was folded from.
    """

    offset: int = 0
    pending: bytes = b""
    token_count_seen: bool = False
    input_total: _SegmentTotal = field(default_factory=_SegmentTotal)
    cached_total: _SegmentTotal = field(default_factory=_SegmentTotal)
    output_total: _SegmentTotal = field(default_factory=_SegmentTotal)
    maximum_request_input: int | None = None
    requests_over_threshold: int = 0
    # A last_token_usage object can carry an input count without an output
    # count, so the request series the maximum and crossing count range over is
    # counted separately from the pairs the price needs.
    request_input_count: int = 0
    # The per-request quantities the notional price is derived from.  The
    # published surcharge charges a whole request differently from its
    # neighbours, so a price cannot be rebuilt from token sums; one entry per
    # measured request is the state that rule needs, and far less than the
    # bytes it came from.
    request_usage: list[RequestTokenUsage] = field(default_factory=list)
    context_window: int | None = None
    rate_limits: Mapping[str, object] | None = None
    earliest: datetime | None = None
    latest: datetime | None = None
    open_calls: dict[object, datetime] = field(default_factory=dict)
    bounded_spans: list[tuple[datetime, datetime]] = field(default_factory=list)
    unreadable: bool = False
    revision: int = 0

    def extend(self, path: Path) -> None:
        """Decode the bytes of ``text`` past this fold's offset.

        A writer leaves the file inside a line or inside a multibyte character,
        so an incomplete tail is held rather than decoded and read again with
        the bytes that finish it.  A final record that simply carries no
        trailing newline is folded rather than held for ever: a terminal run's
        rollout stops growing, and a whole-file read decodes that record.
        Bytes that cannot be decoded mark the file unreadable and stop the
        fold, which is what a whole-file read of the same bytes reports.
        """
        try:
            with path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
        except OSError:
            self.unreadable = True
            return
        if not chunk:
            # Nothing arrived, so a held tail is still the same bytes: it is
            # not attempted again, which is what keeps an unchanged file at no
            # decode at all.
            return
        self.offset += len(chunk)
        data = self.pending + chunk
        self.pending = b""
        end = data.rfind(b"\n")
        if end < 0:
            self.pending = data
            self._fold_tail()
            return
        self.pending = data[end + 1 :]
        try:
            text = data[: end + 1].decode("utf-8")
        except UnicodeError:
            self.unreadable = True
            return
        for line in text.split("\n")[:-1]:
            self._observe(line)
        self._fold_tail()
        self.revision += 1

    def _fold_tail(self) -> None:
        """Fold a held-back tail once it decodes as one whole record.

        The fold reads from an offset, so a record whose bytes are still
        arriving must stay unread; a record a writer finished without writing
        its newline must not.  The two are told apart by whether the held bytes
        decode as one record: a record cut short does not, and stays held.
        """
        if not self.pending:
            return
        try:
            text = self.pending.decode("utf-8")
        except UnicodeError:
            return
        if not self._observe(text):
            return
        # The record is folded and will not be folded again: the next read
        # starts at the offset the record ended on, which is where the bytes
        # following it also begin.
        self.pending = b""

    def _observe(self, line: str) -> bool:
        """Fold one decoded record into the receipt's state.

        Answers whether the line was one whole record, which is the reading the
        held-back tail needs beside every other caller's.  A line that does not
        decode is skipped rather than refused: a rollout carries records this
        reader does not shape.
        """
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(record, Mapping):
            return False
        self._observe_timestamp(record.get("timestamp"))
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return True
        payload_type = payload.get("type")
        if payload_type == "token_count":
            self.token_count_seen = True
            info = payload.get("info")
            if isinstance(info, Mapping):
                total = info.get("total_token_usage")
                if isinstance(total, Mapping):
                    for bucket, key in (
                        (self.input_total, "input_tokens"),
                        (self.cached_total, "cached_input_tokens"),
                        (self.output_total, "output_tokens"),
                    ):
                        value = _integer(total.get(key))
                        if value is not None:
                            bucket.observe(value)
                request = info.get("last_token_usage")
                if isinstance(request, Mapping):
                    request_input = _integer(request.get("input_tokens"))
                    if request_input is not None:
                        self.request_input_count += 1
                        if (
                            self.maximum_request_input is None
                            or request_input > self.maximum_request_input
                        ):
                            self.maximum_request_input = request_input
                        if request_input > REQUEST_INPUT_CROSSING_THRESHOLD:
                            self.requests_over_threshold += 1
                        request_output = _integer(request.get("output_tokens"))
                        if request_output is not None:
                            self.request_usage.append(
                                RequestTokenUsage(request_input, request_output)
                            )
                context_window = _integer(info.get("model_context_window"))
                if context_window is not None:
                    self.context_window = context_window
            rate_limits = payload.get("rate_limits")
            if isinstance(rate_limits, Mapping):
                self.rate_limits = rate_limits
        elif payload_type in ("custom_tool_call", "function_call"):
            call_id = payload.get("call_id")
            moment = _parse_timestamp(record.get("timestamp"))
            if call_id is not None and moment is not None:
                self.open_calls[call_id] = moment
        elif payload_type in ("custom_tool_call_output", "function_call_output"):
            call_id = payload.get("call_id")
            moment = _parse_timestamp(record.get("timestamp"))
            started = self.open_calls.pop(call_id, None)
            if started is not None and moment is not None and moment >= started:
                self.bounded_spans.append((started, moment))
        return True

    def _observe_timestamp(self, value: object) -> None:
        moment = _parse_timestamp(value)
        if moment is None:
            return
        if self.earliest is None or moment < self.earliest:
            self.earliest = moment
        if self.latest is None or moment > self.latest:
            self.latest = moment

    def receipt(self, model_identifier: str | None) -> RolloutReceipt:
        """The receipt this fold's state supports, with every gap named."""
        if self.unreadable:
            return _unmeasured(Unmeasured.UNREADABLE_ROLLOUT)
        if not self.token_count_seen:
            return _unmeasured(Unmeasured.NO_TOKEN_COUNT)

        maximum_request: int | Unmeasured = (
            self.maximum_request_input
            if self.request_input_count
            else Unmeasured.NO_REQUEST_TOKEN_USAGE
        )
        crossing_count: int | Unmeasured = (
            self.requests_over_threshold
            if self.request_input_count
            else Unmeasured.NO_REQUEST_TOKEN_USAGE
        )
        context_window: int | Unmeasured = (
            self.context_window
            if self.context_window is not None
            else Unmeasured.NO_CONTEXT_WINDOW
        )

        span = (
            _bounded_tool_span(self.bounded_spans, self.earliest, self.latest)
            if self.earliest is not None and self.latest is not None
            else None
        )
        if span is None:
            generation_seconds: float | Unmeasured = Unmeasured.NO_BOUNDED_TOOL_SPANS
            machine_seconds: float | Unmeasured = Unmeasured.NO_BOUNDED_TOOL_SPANS
        else:
            _wall, machine, generation = span
            generation_seconds = round(generation, 3)
            machine_seconds = round(machine, 3)

        # Each field is guarded independently: a truncated total object may
        # carry only some buckets, and an absent bucket is unmeasured, never
        # zero.
        cumulative_input: int | Unmeasured = (
            self.input_total.total()
            if self.input_total.seen
            else Unmeasured.NO_TOTAL_TOKEN_USAGE
        )
        cumulative_cached: int | Unmeasured = (
            self.cached_total.total()
            if self.cached_total.seen
            else Unmeasured.NO_TOTAL_TOKEN_USAGE
        )
        cumulative_output: int | Unmeasured = (
            self.output_total.total()
            if self.output_total.seen
            else Unmeasured.NO_TOTAL_TOKEN_USAGE
        )

        notional_cost_usd, rate_basis = _notional_price(
            self.request_usage, model_identifier
        )

        return RolloutReceipt(
            cumulative_input_tokens=cumulative_input,
            cumulative_cached_input_tokens=cumulative_cached,
            cumulative_output_tokens=cumulative_output,
            maximum_request_input_tokens=maximum_request,
            requests_over_threshold=crossing_count,
            model_context_window=context_window,
            quota_readings=_quota_readings(self.rate_limits),
            plan_type=_quota_text(self.rate_limits, "plan_type"),
            generation_seconds=generation_seconds,
            machine_seconds=machine_seconds,
            notional_cost_usd=notional_cost_usd,
            rate_basis=rate_basis,
        )


@dataclass(slots=True)
class _CachedReceipts:
    """One rollout's fold and the receipts already built from it.

    The lock is the entry's own because the fold is mutated: a station thread
    and a served request can read one rollout at once, and without it both
    extends would seek from the same offset and the appended bytes would be
    folded twice.
    """

    signature: _FileIdentity
    fold: _ReceiptFold
    receipts: dict[tuple[int, str | None], RolloutReceipt] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


# A tick reads the same sessions' receipts over and over.  The store is per
# process, keyed by the rollout path, and cleared whole at its bound — the same
# shape the completion-stamp store uses.  A fresh process holds no fold and
# reads every byte again, which is what makes a cached answer checkable.  The
# store itself is guarded because it is shared: the entry locks cover each
# file's fold, and this lock covers the dict those entries live in.
_RECEIPTS: dict[str, _CachedReceipts] = {}
_RECEIPTS_LIMIT = 4096
_RECEIPTS_LOCK = threading.Lock()


def _receipt_entry(key: str, signature: _FileIdentity) -> _CachedReceipts:
    """This file's stored fold, created when it carries none."""
    with _RECEIPTS_LOCK:
        if len(_RECEIPTS) >= _RECEIPTS_LIMIT:
            _RECEIPTS.clear()
        entry = _RECEIPTS.get(key)
        if entry is None:
            entry = _CachedReceipts(signature=signature, fold=_ReceiptFold())
            _RECEIPTS[key] = entry
        return entry


def read_rollout_receipt(
    session_id: str,
    *,
    model_identifier: str | None = None,
    run_id: str | None = None,
) -> RolloutReceipt:
    """Return the measured client receipt values for one crew session.

    The cumulative fields are rebuilt per segment across every reset of the
    client's ``total_token_usage`` reading, because a session that compacted
    its context resets that counter and the last reading then under-reports
    while the maximum misrepresents a count that no longer accumulates.
    ``generation_seconds`` and ``machine_seconds`` bound the tool spans in
    the rollout and subtract them from wall, so a codex run's model time is
    measured rather than invented.  The maximum and crossing count range over
    every measured ``last_token_usage`` object.  Missing, unreadable,
    tokenless, span-less, and quota-less rollouts retain distinct unmeasured
    reasons; a measured zero is never used as a missing-value substitute.

    ``model_identifier`` names the lane the run was served on.  When it names
    a model whose declared rate pair carries an ``as_of`` date, the receipt
    also carries the run's notional spend derived from those per-million rates
    and the run's measured tokens; a lane without a dated rate is explicitly
    unpriced, and a caller that supplies no model gets the explicit no-model
    marker rather than a fabricated figure.

    ``run_id`` confines the lookup to one crew run's harness home.  A run that
    resumed on codex more than once keeps several clients under its own home,
    and a caller measuring that run's own sessions restricts the search and the
    run directory it resolves against this way rather than reading another
    run's receipt.  A caller that names no run searches every home, which is
    the shape the single-session paid-lanes scan uses.
    """
    path = _locate_rollout(str(session_id), run_id=run_id)
    if path is None:
        return _unmeasured(Unmeasured.MISSING_ROLLOUT)
    signature = file_identity(path)
    if signature is None:
        return _unmeasured(Unmeasured.UNREADABLE_ROLLOUT)

    entry = _receipt_entry(str(path), signature)
    with entry.lock:
        # The identity is read again under the entry's own lock: another thread
        # may have extended this fold between the lookup and here, and the
        # decision to extend must be made against what this fold has read, not
        # against what the caller saw before it waited.
        current = file_identity(path) or signature
        if not fold_continues(
            previous=entry.signature, offset=entry.fold.offset, signature=current
        ):
            entry.signature = current
            entry.fold = _ReceiptFold()
            entry.receipts.clear()
        if entry.fold.offset < current[2]:
            entry.fold.extend(path)
            entry.receipts.clear()
            refreshed = file_identity(path)
            if refreshed is not None and refreshed[:2] == current[:2]:
                current = refreshed
        entry.signature = current

        built = entry.receipts.get((entry.fold.revision, model_identifier))
        if built is None:
            built = entry.fold.receipt(model_identifier)
            entry.receipts = {(entry.fold.revision, model_identifier): built}
        return built


__all__ = [
    "REQUEST_INPUT_CROSSING_THRESHOLD",
    "RUN_HOME_SESSIONS",
    "WEEKLY_WINDOW_MINUTES",
    "QuotaReading",
    "RateBasis",
    "RolloutReceipt",
    "Unmeasured",
    "read_rollout_receipt",
    "run_home_rollout_paths",
    "run_home_sessions_root",
]
