"""Derive cumulative token and time measurements along a run's lineage.

A run's spend is read from its streams and folded across every attempt that
reached the same chain: resumes keep the run id and add a stream file, while
redispatches and lane changes mint a new run id and chain to a recorded root.
A shadow mints a new run id too but is never a further attempt, so its
evidence stays on its own row and is excluded from its primary's total under
one exported predicate (:func:`is_durable`).
"""

from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from numbers import Real
from pathlib import Path
from typing import Any, Literal

from reckon._timestamps import parse_utc
from reckon.crew.rollout import directory_entries, file_identity, fold_continues

# A request above this published threshold is charged at twice the input rate and
# one and a half times the output rate for the whole request, not only the excess.
SURCHARGED_REQUEST_INPUT_TOKENS = 272_000


class MeasurementState(Enum):
    """An explicit state for a quantity the stream cannot measure."""

    UNMEASURED = "unmeasured"


UNMEASURED = MeasurementState.UNMEASURED
MeasuredTokenCount = int | MeasurementState
StreamDialect = Literal["claude", "codex"]


@dataclass(frozen=True)
class StreamTokenUsage:
    """Token totals and request-level measurements available from one stream."""

    cumulative_input_tokens: int
    cumulative_cached_input_tokens: int
    cumulative_output_tokens: int
    maximum_request_input_tokens: MeasuredTokenCount
    surcharged_request_count: MeasuredTokenCount
    turn_count: int
    dialect: StreamDialect
    # The identity a client thread declared in stream. Only a codex exec
    # stream declares one today. Two attempts naming the same thread are
    # attempts at one conversation whose newer stream restates everything the
    # older did, so they must be folded rather than added; a stream that
    # declares none is its own thread and is summed with the others.
    thread_id: str | None = None


_CLAUDE_INPUT_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_CLAUDE_CACHED_FIELDS = ("cache_read_input_tokens",)
_CLAUDE_OUTPUT_FIELDS = ("output_tokens",)
_CODEX_INPUT_FIELDS = ("input_tokens", "cached_input_tokens")
_CODEX_CACHED_FIELDS = ("cached_input_tokens",)
_CODEX_OUTPUT_FIELDS = ("output_tokens", "reasoning_output_tokens")


@dataclass(slots=True)
class _StreamFold:
    """One run stream decoded once, extended by the bytes appended since.

    A stream carries two independent measurements — its token totals and the
    span its own records date — and both fold from the same decoded lines, so
    one read answers both; the station's tick read each stream twice for them.
    The fields are the state a whole-file read builds, kept between reads so a
    terminal attempt's stream is decoded once and a grown one is decoded only
    past its offset.
    """

    offset: int = 0
    pending: bytes = b""
    claude_messages: dict[str, dict[str, int]] = field(default_factory=dict)
    claude_aggregate: tuple[dict[str, int], int | None] | None = None
    codex_totals: dict[str, int] = field(default_factory=dict)
    codex_turn_count: int = 0
    thread_ids: set[str] = field(default_factory=set)
    anonymous_message: int = 0
    wall_ms: float | None = None
    model_ms: float | None = None
    first: datetime | None = None
    last: datetime | None = None
    unreadable: bool = False
    revision: int = 0

    def extend(self, path: Path) -> None:
        """Decode the bytes of ``path`` past this fold's offset.

        A worker writes its stream while this reads it, so a read can end
        inside a line or inside a multibyte character; an incomplete tail is
        held rather than decoded and read again with the bytes that finish it.
        A final record a writer finished without writing its newline is folded
        rather than held for ever: the stream of a terminal attempt stops
        growing, and a request whose record was folded must not wait on a
        newline that will never come.
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
        # The tail a cut write leaves behind is replaced rather than refused,
        # so a character split across two reads cannot fail a whole tick.
        for line in data[: end + 1].decode("utf-8", errors="replace").split("\n")[:-1]:
            self._observe(line)
        self._fold_tail()
        self.revision += 1

    def _fold_tail(self) -> None:
        """Fold a held-back tail once it decodes as one whole record.

        The fold reads from an offset, so a record whose bytes are still
        arriving must stay unread; a record a writer finished without writing
        its newline must not.  The two are told apart by whether the held bytes
        decode whole as one record: a record cut short does not, and stays
        held.
        """
        if not self.pending:
            return
        try:
            text = self.pending.decode("utf-8")
        except UnicodeError:
            return
        if not self._observe(text):
            return
        self.pending = b""

    def _observe(self, line: str) -> bool:
        """Fold one decoded record into the stream's token and time state.

        Answers whether the line was one whole record, which is the reading
        that tells a held-back final record from one still being written.
        """
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(record, Mapping):
            return False
        self._observe_time(record)

        kind = record.get("type")
        if kind == "thread.started":
            thread_id = record.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                self.thread_ids.add(thread_id)
            return True

        if kind == "assistant":
            message = record.get("message")
            if not isinstance(message, Mapping):
                return True
            usage = _measured_fields(
                message.get("usage"),
                _CLAUDE_INPUT_FIELDS + _CLAUDE_OUTPUT_FIELDS,
            )
            if usage is None:
                return True
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id:
                self.anonymous_message += 1
                message_id = f"anonymous-{self.anonymous_message}"
            maxima = self.claude_messages.setdefault(message_id, {})
            # Claude emits an assistant record for each content block.  A
            # record carries the message's usage at that point, so the maximum
            # for each field preserves the most complete value.
            for field, value in usage.items():
                maxima[field] = max(maxima.get(field, 0), value)
            return True

        if kind == "result":
            usage = _measured_fields(
                record.get("usage"),
                _CLAUDE_INPUT_FIELDS + _CLAUDE_OUTPUT_FIELDS,
            )
            if usage is not None:
                self.claude_aggregate = (
                    usage,
                    _nonnegative_int(record.get("num_turns")),
                )
            return True

        if kind == "turn.completed":
            usage = _measured_fields(
                record.get("usage"),
                _CODEX_INPUT_FIELDS + _CODEX_OUTPUT_FIELDS,
            )
            if usage is not None:
                self.codex_turn_count += 1
                for field, value in usage.items():
                    self.codex_totals[field] = self.codex_totals.get(field, 0) + value
            return True
        return True

    def _observe_time(self, record: Mapping[str, Any]) -> None:
        """Fold this record's own dates and reported durations into the span."""
        if record.get("type") == "result":
            if _seconds_value(record.get("duration_ms")) is not None:
                self.wall_ms = record["duration_ms"]
            if _seconds_value(record.get("duration_api_ms")) is not None:
                self.model_ms = record["duration_api_ms"]
        raw = record.get("timestamp")
        if isinstance(raw, str) and raw.strip():
            parsed = _event_timestamp(raw)
            if parsed is not None:
                if self.first is None or parsed < self.first:
                    self.first = parsed
                if self.last is None or parsed > self.last:
                    self.last = parsed

    def token_usage(self) -> StreamTokenUsage | MeasurementState:
        """The token measure this fold's state supports."""
        if self.unreadable:
            return UNMEASURED
        has_claude = bool(self.claude_messages) or self.claude_aggregate is not None
        has_codex = self.codex_turn_count > 0
        if has_claude == has_codex:
            return UNMEASURED
        # One distinct thread id is the stream's identity; two mean the file
        # merged conversations, which has no single identity to fold under, so
        # it carries none and is summed with the other streams.
        thread_id = next(iter(self.thread_ids)) if len(self.thread_ids) == 1 else None
        if has_codex:
            return _codex_usage(
                self.codex_totals,
                self.codex_turn_count,
                thread_id=thread_id,
            )
        return _claude_usage(
            self.claude_messages, self.claude_aggregate, thread_id=thread_id
        )

    def time_span(self) -> tuple[float | None, float | None]:
        """This stream's own wall and generation span.

        Wall prefers the harness-reported duration of the attempt's own
        terminal result wholesale — the same authority a promoted throughput
        block reads — and falls back to the interval between the stream's own
        first and last event timestamps, so an attempt the harness did not
        clock still has a bounded span.  Generation is only the harness's own
        reported inference span; neither the event interval nor the wall is
        restated as a model span, because either would invent a measurement
        the stream did not take.
        """
        if self.unreadable:
            return None, None
        wall = _seconds_from_ms(self.wall_ms)
        if (
            wall is None
            and self.first is not None
            and self.last is not None
            and self.last > self.first
        ):
            wall = round((self.last - self.first).total_seconds(), 3)
        return wall, _seconds_from_ms(self.model_ms)


@dataclass(slots=True)
class _CachedStream:
    """One stream's fold, with both readings already built from it.

    The lock is the entry's own because the fold is mutated: a station thread
    and a served request can read one stream at once, and without it both
    extends would seek from the same offset and the appended turns would be
    folded twice.
    """

    signature: tuple[int, int, int, int, int]
    fold: _StreamFold
    usage: StreamTokenUsage | MeasurementState | None = None
    span: tuple[float | None, float | None] | None = None
    built_revision: int = -1
    lock: threading.Lock = field(default_factory=threading.Lock)


# A terminal attempt's stream stops growing, so its measure and its span are
# the same on every tick.  The store is per process, keyed by path, and cleared
# whole at its bound; a fresh process reads every byte again, which is what
# makes a cached figure checkable.  Each entry's lock covers its own fold, and
# the store's lock covers the dict those entries live in.
_STREAMS: dict[str, _CachedStream] = {}
_STREAMS_LIMIT = 4096
_STREAMS_LOCK = threading.Lock()


def _stream_entry(key: str, signature: tuple[int, int, int, int, int]) -> _CachedStream:
    """This stream's stored fold, created when it carries none."""
    with _STREAMS_LOCK:
        if len(_STREAMS) >= _STREAMS_LIMIT:
            _STREAMS.clear()
        entry = _STREAMS.get(key)
        if entry is None:
            entry = _CachedStream(signature=signature, fold=_StreamFold())
            _STREAMS[key] = entry
        return entry


def _read_stream(
    stream_path: str | Path,
) -> tuple[StreamTokenUsage | MeasurementState, tuple[float | None, float | None]]:
    """Both readings of one stream, extended by whatever it gained.

    One decode answers both, under the lock of the entry the fold lives in: a
    second reader extends what the first left rather than the bytes it also
    read, so an entry's bytes are folded once.
    """
    path = Path(stream_path)
    signature = file_identity(path)
    if signature is None:
        return UNMEASURED, (None, None)
    entry = _stream_entry(str(path), signature)
    with entry.lock:
        # The identity is read again under the entry's own lock: another thread
        # may have extended this fold between the lookup and here, and the
        # decision to extend must be made against what this fold has read.
        current = file_identity(path) or signature
        if not fold_continues(
            previous=entry.signature, offset=entry.fold.offset, signature=current
        ):
            entry.signature = current
            entry.fold = _StreamFold()
            entry.built_revision = -1
        if entry.fold.offset < current[2]:
            entry.fold.extend(path)
            entry.built_revision = -1
            refreshed = file_identity(path)
            if refreshed is not None and refreshed[:2] == current[:2]:
                current = refreshed
        entry.signature = current
        if entry.built_revision != entry.fold.revision:
            entry.usage = entry.fold.token_usage()
            entry.span = entry.fold.time_span()
            entry.built_revision = entry.fold.revision
        usage = entry.usage if entry.usage is not None else UNMEASURED
        span = entry.span if entry.span is not None else (None, None)
        return usage, span


def measure_stream_tokens(
    stream_path: str | Path,
) -> StreamTokenUsage | MeasurementState:
    """Measure token usage from a Claude or Codex JSON-lines run stream.

    Missing and unreadable paths, malformed streams, and streams without any
    usage counters return ``UNMEASURED``. A recognized usage record whose
    counters are all zero remains a measured zero.

    A stream an earlier call already decoded is answered from that read: only
    the bytes appended since are decoded, and an unchanged stream is not
    decoded at all. A replaced or truncated stream is read again from the
    start, so a rewritten file never answers with its predecessor's figures.
    """
    usage, _span = _read_stream(stream_path)
    return usage


def _claude_usage(
    messages: Mapping[str, Mapping[str, int]],
    aggregate: tuple[Mapping[str, int], int | None] | None,
    *,
    thread_id: str | None = None,
) -> StreamTokenUsage:
    request_inputs = [
        _sum_fields(usage, _CLAUDE_INPUT_FIELDS) for usage in messages.values()
    ]
    if aggregate is None:
        cumulative = _sum_usage(messages.values())
        turn_count = len(messages)
    else:
        # Claude's result.usage is the final run aggregate. Assistant
        # message.usage records supply request-level input, while their opening
        # output counts can be incomplete even after content-block deduplication.
        cumulative, reported_turn_count = aggregate
        turn_count = (
            reported_turn_count if reported_turn_count is not None else len(messages)
        )

    if request_inputs:
        maximum_request_input: MeasuredTokenCount = max(request_inputs)
        surcharge_count: MeasuredTokenCount = sum(
            value > SURCHARGED_REQUEST_INPUT_TOKENS for value in request_inputs
        )
    else:
        maximum_request_input = UNMEASURED
        surcharge_count = UNMEASURED

    return StreamTokenUsage(
        # Cache reads are returned separately but remain included in cumulative
        # input because a metered lane charges total input even though a cache
        # hit can feel free to a caller. Cache creation is fresh work and is
        # therefore included only in the total-input side of this split.
        cumulative_input_tokens=_sum_fields(cumulative, _CLAUDE_INPUT_FIELDS),
        cumulative_cached_input_tokens=_sum_fields(cumulative, _CLAUDE_CACHED_FIELDS),
        cumulative_output_tokens=_sum_fields(cumulative, _CLAUDE_OUTPUT_FIELDS),
        maximum_request_input_tokens=maximum_request_input,
        surcharged_request_count=surcharge_count,
        turn_count=turn_count,
        dialect="claude",
        thread_id=thread_id,
    )


def _codex_usage(
    totals: Mapping[str, int],
    turn_count: int,
    *,
    thread_id: str | None = None,
) -> StreamTokenUsage:
    """The codex measure from its folded per-field totals and turn count.

    The fold sums each turn's fields as they arrive, so the totals are the same
    sum every turn's usage would give, kept without holding a list of turns.
    """
    # A Codex exec stream reports usage once per turn in turn.completed.usage,
    # not once per model request. Surcharge exposure is therefore unmeasurable
    # from the record rather than absent.
    return StreamTokenUsage(
        cumulative_input_tokens=_sum_fields(totals, _CODEX_INPUT_FIELDS),
        cumulative_cached_input_tokens=_sum_fields(totals, _CODEX_CACHED_FIELDS),
        cumulative_output_tokens=_sum_fields(totals, _CODEX_OUTPUT_FIELDS),
        maximum_request_input_tokens=UNMEASURED,
        surcharged_request_count=UNMEASURED,
        turn_count=turn_count,
        dialect="codex",
        thread_id=thread_id,
    )


def _sum_usage(usages: Any) -> dict[str, int]:
    total: dict[str, int] = {}
    for usage in usages:
        for name, value in usage.items():
            total[name] = total.get(name, 0) + value
    return total


def _sum_fields(usage: Mapping[str, int], fields: tuple[str, ...]) -> int:
    return sum(usage.get(field, 0) for field in fields)


def _measured_fields(value: Any, fields: tuple[str, ...]) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    measured = {
        field: token_count
        for field in fields
        if (token_count := _nonnegative_int(value.get(field))) is not None
    }
    return measured or None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Real) or value < 0:
        return None
    return int(value)


# ── Lineage accumulation: spend that adds up along the chain ─────────────────


def is_durable(record: Mapping[str, Any]) -> bool:
    """Whether a run's spend belongs in its lineage chain's cumulative total.

    The rule names the exclusion rather than enumerating the durable kinds: a
    run is non-durable only when it is a shadow, so a lineage kind recorded
    after this module was written is durable by default and only an explicit
    shadow is carved out. Durability and measurement quality are separate
    questions — a contaminated row is still durable because its spend merged,
    and a shadow is not durable whether or not it is contaminated.
    """

    lineage = record.get("lineage")
    if not isinstance(lineage, Mapping):
        return True
    return str(lineage.get("kind") or "") != "shadow"


def accumulation_key(record: Mapping[str, Any]) -> str:
    """Run id whose cumulative total carries this run's spend.

    A shadow accumulates onto its own run id even when it carries a chain
    root, so its never-merged evidence stays off its primary's total; the
    shadow branch therefore precedes the chain-root branch, which is the
    ordering the accidental ``root_run_id or run_id`` form gets wrong the day
    a shadow records a root. Every other kind folds into the chain root it
    recorded, falling back to its own run id, which lets a resumed run (no
    new run id) and a self-rooted lane change both accumulate under the id
    that carries them.
    """

    lineage = record.get("lineage")
    run_id = str(record.get("run_id") or "")
    if isinstance(lineage, Mapping):
        if str(lineage.get("kind") or "") == "shadow":
            return run_id
        root = str(lineage.get("root_run_id") or "")
        if root:
            return root
    return run_id


def _resume_stream_order(path: Path) -> tuple[int, str]:
    """Order numbered resume streams by attempt rather than by filename text."""
    match = re.fullmatch(r"resume-(\d+)\.jsonl", path.name)
    return (int(match.group(1)), path.name) if match else (sys.maxsize, path.name)


def _lane_change_order(path: Path) -> tuple[int, str]:
    """Order lane-change streams by lane number rather than by filename text."""
    match = re.fullmatch(r"lane-change-(\d+)\.jsonl", path.name)
    return (int(match.group(1)), path.name) if match else (sys.maxsize, path.name)


def _attempt_rank(path: Path) -> tuple[int, str]:
    """Rank one stream by attempt within its thread: the original, then resumes."""
    if path.name == "stream.jsonl":
        return (0, path.name)
    match = re.fullmatch(r"resume-(\d+)\.jsonl", path.name)
    return (int(match.group(1)), path.name) if match else (sys.maxsize, path.name)


def run_streams(stream_path: str | Path) -> list[Path]:
    """Every surviving stream one run wrote, in attempt then lane order.

    ``log_path`` is repointed on every resume, so a figure read from the named
    file alone resets each time the run restarts. The original stream, every
    numbered resume and every lane change share one directory, so all of them
    are returned: a lane change leaves the run id and the directory intact and
    writes ``lane-change-N.jsonl`` beside the stream, and dropping those files
    is how a run's spend silently loses a whole lane's work.
    """

    value = str(stream_path or "")
    if not value.strip():
        return []

    stream = Path(value).expanduser()
    original = (
        stream.parent / "stream.jsonl" if stream.name.startswith("resume-") else stream
    )
    names = directory_entries(stream.parent)
    resumes = sorted(
        (
            stream.parent / name
            for name in names
            if name.startswith("resume-") and name.endswith(".jsonl")
        ),
        key=_resume_stream_order,
    )
    lane_changes = sorted(
        (
            stream.parent / name
            for name in names
            if name.startswith("lane-change-") and name.endswith(".jsonl")
        ),
        key=_lane_change_order,
    )
    gathered: list[Path] = []
    seen: set[Path] = set()
    for candidate in (original, *resumes, *lane_changes):
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        gathered.append(candidate)
    return gathered


@dataclass(frozen=True)
class AccumulatedRunSpend:
    """Cumulative token and time figures for one run's whole lineage chain.

    Tokens are the charged totals a meter actually charges: all input
    including cache reads, plus all generated output. Times are folded from
    the chain's rows' recorded throughput blocks. The chain reports how many
    run rows and measured streams it folded, so an unmeasured chain is an
    explicit count of zeros rather than an ambiguity.

    ``durable`` is the exported exclusion predicate applied to the queried
    record: a shadow's own row carries its spend, but that spend is never a
    durable contribution to its primary's total.

    ``unmeasured_time_attempt_count`` names how many attempts the folded times
    could not measure. A completed row whose directory holds one stream folds
    its committed throughput block untouched; one whose directory holds
    several is a resumed run whose block describes only the newest attempt, so
    each attempt contributes its own span, and an attempt whose span cannot be
    measured nulls the wall and model totals rather than letting a partial sum
    read as complete.
    """

    run_id: str
    durable: bool
    folded_run_count: int
    measured_stream_count: int
    unmeasured_stream_count: int
    cumulative_input_tokens: int
    cumulative_cached_input_tokens: int
    cumulative_output_tokens: int
    elapsed_seconds: float | None
    generation_seconds: float | None
    machine_seconds: float | None
    elapsed_from_stamps: bool = False
    unmeasured_time_attempt_count: int = 0

    @property
    def total_charged_tokens(self) -> int:
        """Input (including cache reads) plus output — what a meter charges."""
        return self.cumulative_input_tokens + self.cumulative_output_tokens


def _find_run(
    runs: Sequence[Mapping[str, Any]], run_id: str
) -> Mapping[str, Any] | None:
    for record in runs:
        if str(record.get("run_id") or "") == run_id:
            return record
    return None


def _resolve_streams_root(streams_root: str | Path | None) -> Path:
    """The directory holding one subdirectory per run id."""
    if streams_root is not None:
        return Path(str(streams_root)).expanduser()
    from reckon.crew.runs import runs_dir

    return runs_dir()


def _row_streams(record: Mapping[str, Any], streams_root: Path) -> list[Path]:
    """Every surviving stream path a row's attempts wrote, without duplicates.

    ``log_path`` is the stream file the launcher wrote and resumes live beside
    it; the run directory is the fallback when a pointer never carried one, so
    a record whose launcher recorded nothing still reaches whatever stream
    survived.
    """

    gathered: list[Path] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    log_value = str(record.get("log_path") or "")
    if log_value.strip():
        candidates.extend(run_streams(log_value))
    run_id = str(record.get("run_id") or "")
    if run_id:
        candidates.extend(run_streams(streams_root / run_id / "stream.jsonl"))
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            gathered.append(candidate)
    return gathered


def _measure_streams(
    paths: Sequence[Path],
) -> tuple[int, int, int, int, int]:
    """Fold one run's streams per thread into token totals and stream counts.

    A stream restates its client thread's cumulative usage, so adding every
    attempt of one thread counts the earlier attempts twice: a codex
    ``exec resume`` re-enters the thread and its stream re-states everything
    before it. A newer attempt therefore supersedes its predecessor rather
    than adding to it, and the fold keeps only the highest-ranked measured
    attempt per thread. Threads are added, because two threads are two
    conversations: a lane change opens a new thread, and its stream is
    summed with the original's.

    Thread identity is what each stream declares. A stream that declares none
    is its own thread, so a pair of unlabelled streams still adds up and an
    attempt whose identity cannot be read is never silently dropped.
    """
    total_input = 0
    total_cached = 0
    total_output = 0
    measured = 0
    unmeasured = 0
    # Thread key -> (attempt rank, reading) of the newest measured attempt.
    newest: dict[str, tuple[tuple[int, str], StreamTokenUsage]] = {}
    for path in paths:
        usage = measure_stream_tokens(path)
        if usage is UNMEASURED or not isinstance(usage, StreamTokenUsage):
            unmeasured += 1
            continue
        measured += 1
        key = usage.thread_id or f"stream:{path}"
        rank = _attempt_rank(path)
        if key not in newest or rank >= newest[key][0]:
            newest[key] = (rank, usage)
    for _rank, usage in newest.values():
        total_input += usage.cumulative_input_tokens
        total_cached += usage.cumulative_cached_input_tokens
        total_output += usage.cumulative_output_tokens
    return total_input, total_cached, total_output, measured, unmeasured


def _seconds_value(value: Any) -> float | None:
    """A measured seconds figure, or None for a non-number or boolean."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    return float(value)


def _seconds_from_ms(value: Any) -> float | None:
    """A reported millisecond span converted to seconds, or None."""
    seconds = _seconds_value(value)
    return None if seconds is None else round(seconds / 1000.0, 3)


def _stream_time_span(path: Path) -> tuple[float | None, float | None]:
    """One attempt's own wall and generation span, from its stream file.

    The same fold the attempt's token measure comes from answers this, so the
    two readings of one stream share a single decode; an unchanged stream
    answers from that read.
    """
    _usage, span = _read_stream(path)
    return span


def _event_timestamp(value: str) -> datetime | None:
    """Parse one event's timestamp, treating a naive stamp as UTC.

    The reader takes a string: a non-string is a caller error and is surfaced
    rather than silently reading as nothing. The shared parser is tolerant of
    surrounding space and of a lowercase zone designator; this reader's
    recorded contract refuses both.
    """
    if not isinstance(value, str):
        raise TypeError(f"timestamp must be a string, not {type(value).__name__}")
    if value != value.strip() or value.endswith("z"):
        return None
    return parse_utc(value)


def _stamped_elapsed(
    record: Mapping[str, Any], *, now_seconds: float | None = None
) -> float | None:
    """Wall seconds elapsed for an in-flight run, from the pointer's own stamps.

    A run that is still working has no terminal record, so its wall measures
    to now. The run's creation stamp is its original dispatch, which is the
    same base the completed row's folded span uses, so a run resumed several
    times counts every attempt's interval rather than only the current one.
    ``attempt_started_at`` is the fallback when no creation stamp was
    recorded, matching the budget watchdog's convention. Missing or malformed
    stamps return ``None``, which keeps an unrunnable record an unmeasured
    one rather than an invented time.
    """

    started = record.get("created_at") or record.get("attempt_started_at")
    if not isinstance(started, str) or not started:
        return None
    if started != started.strip() or started.endswith("z"):
        return None
    moment = parse_utc(started)
    if moment is None:
        return None
    if now_seconds is None:
        now_seconds = datetime.now(UTC).timestamp()
    return max(0.0, float(now_seconds) - moment.timestamp())


def _fold_attempt_spans(
    elapsed: float | None,
    generation: float | None,
    record: Mapping[str, Any],
    paths: Sequence[Path],
) -> tuple[float | None, float | None, bool, int]:
    """Sum every attempt's own span for a completed, resumed row.

    ``log_path`` is repointed to the newest resume file on every restart, so
    the row's committed throughput block — read off the named file by the
    promoter — describes only the attempt it promoted. Where the run's
    directory holds several streams, the earlier attempts are separate spans
    with no committed block, so each is measured from its own stream and the
    newest attempt prefers the block's recorded values, which may carry a
    dialect join (a codex rollout receipt) its stream alone cannot.
    """

    throughput = record.get("throughput")
    stream_spans = [_stream_time_span(path) for path in paths]
    unmeasured = 0
    contribution_wall = 0.0
    contribution_model = 0.0
    wall_measured = True
    model_measured = True
    for index, (stream_wall, stream_model) in enumerate(stream_spans):
        newest = index == len(paths) - 1
        if newest and isinstance(throughput, Mapping):
            wall = _seconds_value(throughput.get("elapsed_seconds"))
            if wall is None:
                wall = stream_wall
            model = _seconds_value(throughput.get("generation_seconds"))
            if model is None:
                model = stream_model
        else:
            wall, model = stream_wall, stream_model

        # An attempt whose span cannot be measured must not disappear from the
        # total: a partial sum would read as the complete figure. The missing
        # attempt instead nulls the folded wall, and its count records why.
        if wall is None:
            wall_measured = False
            unmeasured += 1
        else:
            contribution_wall += wall
        if model is None:
            model_measured = False
        else:
            contribution_model += model
    elapsed = None if not wall_measured else (elapsed or 0.0) + contribution_wall
    # Model seconds follow the same refusal: where an attempt has no inference
    # span the total stays unmeasured rather than summing the attempts that do.
    generation = (
        None if not model_measured else (generation or 0.0) + contribution_model
    )
    return elapsed, generation, False, unmeasured


def _fold_time(
    elapsed: float | None,
    generation: float | None,
    record: Mapping[str, Any],
    paths: Sequence[Path],
    *,
    now_seconds: float | None = None,
) -> tuple[float | None, float | None, bool, int]:
    """Fold one row's times into the chain totals, marking their provenance.

    A completed row with a single attempt contributes the span its throughput
    block recorded, unchanged. A completed row whose directory holds several
    streams is a resumed run: its block describes only the newest attempt, so
    every attempt contributes its own span. A row still in flight has no
    block, so wall is derived from the pointer's own stamps instead, and
    generation stays unmeasured because no inference span exists until a
    terminal record or a rollout with bounded tool spans does. The third
    return reports whether the contributed wall was derived from stamps, and
    the fourth reports how many attempts this row's total refused to measure.
    """

    throughput = record.get("throughput")
    if isinstance(throughput, Mapping) and len(paths) > 1:
        return _fold_attempt_spans(elapsed, generation, record, paths)
    if isinstance(throughput, Mapping):
        span = throughput.get("elapsed_seconds")
        if isinstance(span, Real):
            elapsed = (elapsed or 0.0) + float(span)
        model = throughput.get("generation_seconds")
        if isinstance(model, Real):
            generation = (generation or 0.0) + float(model)
        return elapsed, generation, False, 0
    stamped = _stamped_elapsed(record, now_seconds=now_seconds)
    if stamped is None:
        return elapsed, generation, False, 0
    return (elapsed or 0.0) + stamped, generation, True, 0


def accumulate_run_spend(
    runs: Sequence[Mapping[str, Any]],
    run_id: str,
    *,
    streams_root: str | Path | None = None,
    now_seconds: float | None = None,
) -> AccumulatedRunSpend | MeasurementState:
    """Return the whole lineage chain's cumulative spend for one run.

    Rows are folded by accumulation key. A redispatch or lane change folds
    into the chain root it recorded, a resumed run keeps its run id and its
    directory's streams fold under it per thread, and a shadow accumulates
    onto its own id so its never-merged evidence stays off its primary's
    total. Each folded row contributes every surviving stream in its run
    directory, because ``log_path`` is repointed on every resume and a figure
    read from it alone would reset at each restart.

    ``UNMEASURED`` names a run id absent from ``runs``; a present run whose
    streams carry no usage counters returns a zero total with its stream
    counts rather than the absence marker. ``streams_root`` is the directory
    holding one subdirectory per run id, defaulting to the crew runs directory
    when omitted. ``now_seconds`` pins the observation the live-run wall
    derives from and exists so a test can fix a fixture's present; omitted,
    the wall measures to the moment of reading.
    """

    record = _find_run(runs, run_id)
    if record is None:
        return UNMEASURED
    root = _resolve_streams_root(streams_root)
    key = accumulation_key(record)
    folded_rows = 0
    total_input = 0
    total_cached = 0
    total_output = 0
    measured_streams = 0
    unmeasured_streams = 0
    elapsed: float | None = None
    generation: float | None = None
    derived_from_stamps = False
    unmeasured_time_attempts = 0
    for row in runs:
        # Keying is the exclusion in action: a shadow's accumulation key is
        # its own run id, so it folds only when the query is its own row and
        # never into its primary's total, however the shadow is recorded.
        if accumulation_key(row) != key:
            continue
        folded_rows += 1
        paths = _row_streams(row, root)
        input_total, cached_total, output_total, measured, unmeasured = (
            _measure_streams(paths)
        )
        total_input += input_total
        total_cached += cached_total
        total_output += output_total
        measured_streams += measured
        unmeasured_streams += unmeasured
        elapsed, generation, derived, unmeasured_attempts = _fold_time(
            elapsed, generation, row, paths, now_seconds=now_seconds
        )
        derived_from_stamps = derived_from_stamps or derived
        unmeasured_time_attempts += unmeasured_attempts
    machine = None
    if elapsed is not None and generation is not None and not derived_from_stamps:
        machine = round(elapsed - generation, 3)
    return AccumulatedRunSpend(
        run_id=key,
        durable=is_durable(record),
        folded_run_count=folded_rows,
        measured_stream_count=measured_streams,
        unmeasured_stream_count=unmeasured_streams,
        cumulative_input_tokens=total_input,
        cumulative_cached_input_tokens=total_cached,
        cumulative_output_tokens=total_output,
        elapsed_seconds=elapsed,
        generation_seconds=generation,
        machine_seconds=machine,
        elapsed_from_stamps=derived_from_stamps,
        unmeasured_time_attempt_count=unmeasured_time_attempts,
    )
