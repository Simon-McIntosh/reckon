# ruff: noqa: I001
from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, get_type_hints

# ── SDK import ─────────────────────────────────────────────────────────────
from reckon._mcp_tools import StorageSlowResult
from reckon._store import (
    OpError,
    VersionConflict,
    _resolve_html_file,
    state_path,
)
from reckon.crew.runs import CrewError
from reckon.crew.runs import read_pointer as read_run_pointer
from reckon.lifecycle import TERMINAL_STATUSES
from reckon.mcp_budget import bound_response
from reckon.resources import (
    canonical_type,
)
# ── Storage deadlines ──────────────────────────────────────────────────────
#
# FastMCP calls a synchronous tool function directly on its one event loop
# (``mcp.server.fastmcp.utilities.func_metadata``: ``if fn_is_async: return await
# fn(...)`` else ``return fn(...)``), so a single read blocked on a slow
# filesystem stalls every later tool call, and a caller cannot tell a slow
# filesystem from a missing plan. Every registered tool is therefore wrapped as
# an async adapter that runs its synchronous body on a worker thread under a
# deadline. A body that outlives its deadline is abandoned rather than
# cancelled — a blocked filesystem call cannot be interrupted — and reported as
# a typed ``storage-slow`` result, which leaves the loop free for the next call.
# Every filesystem touch on that path runs on the worker thread, including the
# stats that fingerprint the write's file: an unbounded stat against the mount
# that has just stalled is the same hazard as the read that stalled, and must
# not execute on the loop either.

READ_DEADLINE_SECONDS = 30.0
WRITE_DEADLINE_SECONDS = 60.0
WRITE_LANDING_GRACE_SECONDS = 0.5

# The landing check polls for the write's file to appear, and each poll is a
# stat on the same mount that may have stalled the write, so the check carries
# a deadline of its own just above its grace and reports unknown past it.
LANDING_POLL_SECONDS = 0.02
LANDING_STAT_MARGIN_SECONDS = 0.5

# Distinguishes "the file was absent before the write" from "the baseline stat
# never answered", which are different states and only the first is evidence.
_UNSET = object()

DEADLINE_ENV = "RECKON_MCP_DEADLINE_SECONDS"
READ_DEADLINE_ENV = "RECKON_MCP_READ_DEADLINE_SECONDS"
WRITE_DEADLINE_ENV = "RECKON_MCP_WRITE_DEADLINE_SECONDS"
LANDING_GRACE_ENV = "RECKON_MCP_WRITE_LANDING_GRACE_SECONDS"

# How much of a read's deadline is held back when a stored composition can
# answer it. The recompute is awaited only up to the deadline less this, so the
# stored answer is loaded and returned inside the deadline rather than after
# it: the reserve pays for reading one snapshot file and serialising it back,
# and it is deliberately a small fraction of the read deadline.
STALE_SNAPSHOT_RESERVE_SECONDS = 1.0
STALE_RESERVE_ENV = "RECKON_ROADMAP_STALE_RESERVE_SECONDS"


def _positive_seconds(value: str | None) -> float | None:
    """Parse an override into a non-negative number of seconds, or None."""

    if value is None or not value.strip():
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds)


def _deadline_seconds(kind: str) -> float:
    """Resolve the deadline for one call, most specific override first."""

    override = _positive_seconds(os.environ.get(DEADLINE_ENV))
    if override is not None:
        return override
    specific = _positive_seconds(
        os.environ.get(WRITE_DEADLINE_ENV if kind == "write" else READ_DEADLINE_ENV)
    )
    if specific is not None:
        return specific
    return WRITE_DEADLINE_SECONDS if kind == "write" else READ_DEADLINE_SECONDS


def _stale_reserve_seconds() -> float:
    """The part of a read's deadline held back for loading a stored answer."""

    override = _positive_seconds(os.environ.get(STALE_RESERVE_ENV))
    return STALE_SNAPSHOT_RESERVE_SECONDS if override is None else override


def _landing_grace_seconds() -> float:
    override = _positive_seconds(os.environ.get(LANDING_GRACE_ENV))
    return WRITE_LANDING_GRACE_SECONDS if override is None else override


# A timed-out body that was computing is one that wanted the CPU: it either
# burned thread CPU time, or sat runnable on the runqueue while an oversubscribed
# node ran other work first. A body waiting on storage does neither — it is
# blocked, so it burns no CPU and spends no time runnable. These two together
# are the computing measure, compared against the wall time the body held. The
# fraction separates the two for a wait long enough to time, and the floor keeps
# counter granularity from reading a very short wait as computing. They decide
# only the reported cause; the ``error`` value stays ``storage-slow`` so no
# existing caller's branch changes.
_COMPUTING_WORK_FRACTION = 0.5
_COMPUTING_WORK_FLOOR_SECONDS = 0.02


def _thread_cpu_seconds(tid: int) -> float | None:
    """CPU seconds one thread of this process has consumed, or None.

    Read from ``/proc`` so the deadline can ask about the abandoned worker
    thread by id: ``time.thread_time`` answers only for the calling thread, and
    the caller here is the event loop, which is the thread that is not doing
    the work. Returns None when the thread has exited or the counter is
    unreadable, which is an unknown state rather than a zero.
    """

    try:
        with open(f"/proc/self/task/{tid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    close = data.rfind(b")")
    if close < 0:
        return None
    fields = data[close + 2 :].split()
    if len(fields) < 13:
        return None
    try:
        utime = int(fields[11])
        stime = int(fields[12])
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    if ticks <= 0:
        return None
    return (utime + stime) / ticks


def _thread_run_wait_seconds(tid: int) -> float | None:
    """Seconds one thread has spent runnable but not running, or None.

    The second field of ``/proc/self/task/<tid>/schedstat`` counts the time the
    thread was on a runqueue waiting for a CPU, which is the other half of
    "this body wanted the CPU". It is read beside the CPU counter so a body
    starved by an oversubscribed node — almost no CPU, most of the wall spent
    runnable — is not mistaken for one blocked on storage. A blocked thread is
    not runnable, so that time does not grow for it.
    """

    try:
        with open(f"/proc/self/task/{tid}/schedstat", "rb") as handle:
            fields = handle.read().split()
    except OSError:
        return None
    if len(fields) < 2:
        return None
    try:
        return int(fields[1]) / 1e9
    except ValueError:
        return None


def _cause_of_timeout(
    cpu_seconds: float | None,
    run_wait_seconds: float | None,
    waited: float,
) -> str:
    """Name whether the abandoned body was computing or waiting on storage."""

    if cpu_seconds is None and run_wait_seconds is None:
        return "waiting"
    measure = (cpu_seconds or 0.0) + (run_wait_seconds or 0.0)
    threshold = max(_COMPUTING_WORK_FLOOR_SECONDS, _COMPUTING_WORK_FRACTION * waited)
    return "computing" if measure >= threshold else "waiting"


# The CLI command that answers each registered tool's work without the MCP
# deadline, keyed by the tool's label. A tool with no CLI counterpart, such as
# the plan writer, names nothing here rather than inventing a command.
_CLI_ANSWER_COMMANDS = {
    "roadmap": "reckon roadmap --project <project>",
    "audit": "reckon audit --project <project>",
    "crew": "reckon crew view live --project <project>",
}


def _file_fingerprint(path: str | None) -> tuple[int, int] | None:
    """The identity of one file as (mtime, size), or None when it is absent."""

    if path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _landing_state(
    path: str, before: tuple[int, int] | None, body_future: Any
) -> bool | None:
    """Report whether an abandoned write reached its file within a short grace.

    The grace exists because a body can complete just after the deadline: the
    thread is still running, and reporting ``landed=False`` for a write that
    actually landed would invite a blind retry that duplicates it. When the
    grace ends with the file unchanged, the abandoned body's own future decides
    the answer: if it has finished — returned or raised — the file is unchanged
    because the write never touched it, so the result is ``False``; if it is
    still running, the write may yet land and the result is ``None``, unknown.
    ``body_future`` is the future ``_InflightBody`` already holds, so no second
    record of whether the body ended is kept.

    This is synchronous and calls ``os.stat`` directly, so it runs on a worker
    thread. A stat issued against the mount that has just stalled the write is
    itself unbounded, so running it on the event loop would hold every later
    call for as long as the filesystem takes to answer.
    """

    stop = time.monotonic() + _landing_grace_seconds()
    while True:
        if _file_fingerprint(path) != before:
            return True
        if body_future.done():
            return False
        if time.monotonic() >= stop:
            return None
        time.sleep(LANDING_POLL_SECONDS)


async def _bounded_landing(
    path: str, before: tuple[int, int] | None, body_future: Any
) -> bool | None:
    """Answer the landing question off the loop, or None if the fs will not.

    The check gets its own deadline, slightly longer than its grace so a
    healthy filesystem always answers, and reports ``None`` — unknown, not
    false — when even that expires. ``None`` is also reported when the body is
    still running at the end of the grace: either way the landing state is
    unknown, and a caller must not read unknown as "did not land" and resubmit
    a write that may well have succeeded.
    """

    limit = _landing_grace_seconds() + LANDING_STAT_MARGIN_SECONDS
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_landing_state, path, before, body_future), limit
        )
    except TimeoutError:
        return None


class _InflightBody:
    """A worker body running on a thread, keepable past its own deadline.

    The thread pool future outlives ``asyncio.wait_for``: a timed-out body is
    abandoned, not cancelled, so a later call for the same work can await the
    same future and receive the answer the abandoned body finally produced.
    ``waiting`` carries the per-thread diagnostics the deadline reads, and
    ``started`` is the monotonic clock the body began at.
    """

    __slots__ = ("future", "started", "waiting")

    def __init__(self, future: Any, waiting: dict[str, Any], started: float) -> None:
        self.future = future
        self.waiting = waiting
        self.started = started


def _read_join_key(label: str, kwargs: Mapping[str, Any]) -> tuple[str, str]:
    """A stable identity for one read call: its tool and its arguments.

    Two calls with the same tool and the same arguments share a key, so a retry
    joins the body still running; a call whose arguments differ gets its own.
    Arguments are serialised with sorted keys so mapping order never splits a
    key, falling back to a sorted ``repr`` when a value is not JSON-encodable.
    """

    try:
        encoded = json.dumps(kwargs, sort_keys=True, default=str)
    except (TypeError, ValueError):
        encoded = repr(sorted((str(key), repr(value)) for key, value in kwargs.items()))
    return (label, encoded)


# Read bodies still running after their deadline, keyed by tool and arguments,
# so a retry of the same read awaits the body already answering it rather than
# starting a second copy. A body is registered for as long as it runs and
# forgotten when it finishes; a write is never registered, so a write always
# runs its own body under its own deadline. Mutated only on the event loop.
_INFLIGHT_READS: dict[tuple[str, str], _InflightBody] = {}


def _forget_read(key: tuple[str, str], body: _InflightBody) -> None:
    """Drop a finished read's registration, unless a newer body took its place."""

    if _INFLIGHT_READS.get(key) is body:
        _INFLIGHT_READS.pop(key, None)


def _start_body(
    body: Callable[[], Any],
    *,
    kind: str,
    label: str,
    path: str | None = None,
    path_hint: Callable[[], str | None] | None = None,
) -> _InflightBody:
    """Submit one synchronous storage body to the thread pool, undeadlined.

    The future is the pool's own, so it survives the wait that bounds the call
    and is available to a later join. Path resolution, the baseline stat and
    the CPU/run-queue baselines all run on the worker thread.
    """

    waiting: dict[str, Any] = {
        "path": str(path) if path is not None else None,
        "before": _UNSET,
        "tid": None,
        "cpu_base": None,
        "run_base": None,
    }
    started = time.monotonic()

    def _work() -> Any:
        waiting["tid"] = threading.get_native_id()
        if waiting["path"] is None and path_hint is not None:
            try:
                resolved = path_hint()
            except Exception:  # noqa: BLE001 — naming the path must never fail a call
                resolved = None
            if resolved is not None:
                waiting["path"] = str(resolved)
        # The baseline stat runs here, on this worker thread, for the same
        # reason the landing check does: an unbounded stat on a stalled mount
        # must never execute on the event loop.
        if kind == "write" and waiting["path"] is not None:
            waiting["before"] = _file_fingerprint(waiting["path"])
        # The CPU and run-queue baselines are read here too, so the deadline
        # can subtract them from the worker thread's counters and report what
        # the abandoned body itself spent rather than what the whole process
        # did. Run-queue time is the second half of wanting the CPU: a body
        # starved on an oversubscribed node burns little CPU but sits runnable.
        waiting["cpu_base"] = _thread_cpu_seconds(waiting["tid"])
        waiting["run_base"] = _thread_run_wait_seconds(waiting["tid"])
        return body()

    loop = asyncio.get_running_loop()
    return _InflightBody(loop.run_in_executor(None, _work), waiting, started)


async def _await_body(
    inflight: _InflightBody,
    *,
    kind: str,
    label: str,
    limit: float,
    started: float | None = None,
    on_timeout: Callable[[], Any] | None = None,
) -> Any:
    """Await one started body under the deadline, naming a timeout's cause.

    ``on_timeout`` is the answer to give instead of the ``storage-slow``
    result when the wait expires and it can produce one: a read with a stored
    composition answers from that composition rather than reporting the
    storage as slow. The body is abandoned either way, so it still runs and
    still stores what it composes for the next call.
    """

    try:
        # Shield the body's future from this wait's cancellation: a timeout
        # must abandon the wait, not the body, so a retry that joins it can
        # still receive the answer it finally produces.
        return await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(inflight.future)), limit
        )
    except TimeoutError:
        if on_timeout is not None:
            fallback = on_timeout()
            if fallback is not None:
                return fallback
        waited = time.monotonic() - (inflight.started if started is None else started)
        waiting = inflight.waiting
        cpu_seconds: float | None = None
        run_wait_seconds: float | None = None
        tid = waiting["tid"]
        cpu_base = waiting["cpu_base"]
        if tid is not None and cpu_base is not None:
            current = _thread_cpu_seconds(tid)
            if current is not None:
                cpu_seconds = max(0.0, current - cpu_base)
        run_base = waiting["run_base"]
        if tid is not None and run_base is not None:
            current_run = _thread_run_wait_seconds(tid)
            if current_run is not None:
                run_wait_seconds = max(0.0, current_run - run_base)
        cause = _cause_of_timeout(cpu_seconds, run_wait_seconds, waited)
        landed: bool | None = None
        if kind == "write" and waiting["path"] is not None:
            before = waiting["before"]
            landed = (
                None
                if before is _UNSET
                else await _bounded_landing(waiting["path"], before, inflight.future)
            )
        return StorageSlowResult.for_call(
            kind=kind,
            label=label,
            path=waiting["path"],
            waited=waited,
            deadline=limit,
            landed=landed,
            cpu_seconds=cpu_seconds,
            run_seconds=run_wait_seconds,
            cause=cause,
            cli_command=_CLI_ANSWER_COMMANDS.get(label),
        ).model_dump()


async def _run_under_deadline(
    body: Callable[[], Any],
    *,
    kind: str,
    label: str,
    path: str | None = None,
    path_hint: Callable[[], str | None] | None = None,
    deadline: float | None = None,
) -> Any:
    """Run one synchronous storage body on a worker thread under a deadline."""

    limit = _deadline_seconds(kind) if deadline is None else deadline
    inflight = _start_body(body, kind=kind, label=label, path=path, path_hint=path_hint)
    return await _await_body(inflight, kind=kind, label=label, limit=limit)


def _safe_fallback(fallback: Callable[[], Any]) -> Any:
    """Ask a fallback for an answer, treating any failure as no answer.

    A read with a stored composition available must never be failed by the
    look-up for it: an unreadable or malformed record is the same as no
    record, and the call then waits for its recompute as it would have without
    one.
    """

    try:
        return fallback()
    except Exception:  # noqa: BLE001 — a fallback never becomes the read's failure
        return None


async def _read_joining_running(
    body: Callable[[], Any],
    *,
    label: str,
    kwargs: Mapping[str, Any],
    path_hint: Callable[[], str | None] | None = None,
    stale: Callable[[Mapping[str, Any]], Any] | None = None,
    stale_present: Callable[[Mapping[str, Any]], bool] | None = None,
) -> Any:
    """Run a read under its deadline, joining a body already answering it.

    A read call whose tool and arguments match a body still running awaits that
    body under this call's own deadline rather than starting a second copy, so
    a retry after a timeout gets the first body's answer. A body is forgotten
    when it finishes, so the next identical read after that starts fresh. A
    read whose arguments differ keys its own body, and a write never reaches
    here — it always runs its own body under its own deadline.

    ``stale`` answers the call from a stored composition when there is one for
    it. ``stale_present`` says whether one exists without building it, because
    the question asked here is only whether the reserve is worth keeping: the
    answer itself is built at the moment the shortened wait expires, where its
    age is the reader's own, and a read with nothing stored pays neither the
    lookup's payload nor the wait. The recompute is not cancelled: it runs on
    and stores what the next call reads.
    """

    limit = _deadline_seconds("read")
    wait_limit = limit
    on_timeout: Callable[[], Any] | None = None
    if stale is not None:

        def _fallback() -> Any:
            return stale(kwargs)

        if stale_present is not None:
            has_stored = bool(_safe_fallback(lambda: stale_present(kwargs)))
        else:
            has_stored = _safe_fallback(_fallback) is not None
        if has_stored:
            wait_limit = max(0.0, limit - _stale_reserve_seconds())
            on_timeout = _fallback
    key = _read_join_key(label, kwargs)
    inflight = _INFLIGHT_READS.get(key)
    join_started: float | None = None
    if inflight is None:
        inflight = _start_body(body, kind="read", label=label, path_hint=path_hint)
        _INFLIGHT_READS[key] = inflight
        inflight.future.add_done_callback(
            lambda _future, _key=key, _body=inflight: _forget_read(_key, _body)
        )
    else:
        join_started = time.monotonic()
    return await _await_body(
        inflight,
        kind="read",
        label=label,
        limit=wait_limit,
        started=join_started,
        on_timeout=on_timeout,
    )


def _resolved_signature(function: Callable[..., Any]) -> inspect.Signature:
    """Copy a signature with its annotations resolved to objects, not strings.

    FastMCP re-introspects the registered callable with ``eval_str=True``. A
    copied signature carrying PEP 563 strings would be re-evaluated against the
    adapter's globals; resolving them here keeps the published argument model
    byte-for-byte the one the synchronous body publishes.
    """

    hints = get_type_hints(function, include_extras=True)
    parameters = [
        parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
        for parameter in inspect.signature(function).parameters.values()
    ]
    return inspect.Signature(
        parameters,
        return_annotation=hints.get("return", inspect.Signature.empty),
    )


def _deadline_tool(
    body: Callable[..., Any],
    *,
    kind: str | Callable[[Mapping[str, Any]], str],
    label: str | None = None,
    path_hint: Callable[[Mapping[str, Any]], str | None] | None = None,
    response_budget: bool = False,
    stale: Callable[[Mapping[str, Any]], Any] | None = None,
    stale_present: Callable[[Mapping[str, Any]], bool] | None = None,
) -> Callable[..., Any]:
    """Wrap one synchronous tool body as the async entry FastMCP registers.

    ``stale`` is the read's fallback for a call whose body outlives the reserve
    its deadline keeps: it answers the call from a stored result rather than
    reporting the storage as slow, and is used only when it can produce one.
    ``stale_present`` is the cheap half of it, asked up front to decide whether
    that reserve is worth keeping; without it the fallback itself answers the
    question, which builds an answer only to discard it.
    """

    signature = _resolved_signature(body)
    label = label or body.__name__.removeprefix("_").removesuffix("_tool")

    async def tool(**kwargs: Any) -> Any:
        resolved_kind = kind(kwargs) if callable(kind) else kind
        hint = (lambda: path_hint(kwargs)) if path_hint is not None else None
        if resolved_kind == "read":
            result = await _read_joining_running(
                lambda: body(**kwargs),
                label=label,
                kwargs=kwargs,
                path_hint=hint,
                stale=stale,
                stale_present=stale_present,
            )
        else:
            result = await _run_under_deadline(
                lambda: body(**kwargs),
                kind=resolved_kind,
                label=label,
                path_hint=hint,
            )
        if response_budget and resolved_kind == "read":
            return bound_response(result, tool=label, arguments=kwargs)
        return result

    tool.__name__ = body.__name__
    tool.__qualname__ = body.__qualname__
    tool.__doc__ = body.__doc__
    tool.__module__ = body.__module__
    tool.__signature__ = signature
    return tool


def _plan_path_hint(kwargs: Mapping[str, Any]) -> str | None:
    """Name the file a plan-shaped call is about to touch, best effort.

    Resolved on the worker thread, so a slow mount is what the deadline bounds
    rather than something it has to get past first.
    """

    project = kwargs.get("project")
    slug = kwargs.get("slug")
    doc_type = kwargs.get("doc_type")
    resource = kwargs.get("resource")
    if isinstance(resource, Mapping):
        project = project or resource.get("project")
        slug = slug or resource.get("id")
        doc_type = doc_type or resource.get("type")
    if not project or not slug:
        return None
    checkout = kwargs.get("checkout_path")
    if checkout is None:
        checkout, _refusal = _run_scoped_checkout(str(project), str(slug), doc_type)
    return _written_path(str(project), str(slug), checkout, doc_type)


def _stale_plan_review(project: str, plan_slug: str) -> tuple[int, str] | None:
    """The newest stored review of a plan, with its version.

    A plan with stored reviews none of which covers the current content reads
    as unreviewed however many it carries, so a reader that reports only "no
    stored review" hides the review that exists and misleads its author. This
    names the newest stored review and the command that composes a review of
    the content now present, reading it through the review module's own lookup
    rather than re-selecting a newest itself. ``None`` means the plan has no
    stored review at all, which is a different fact the caller states plainly.
    """
    from reckon.crew import plan_review

    newest = plan_review.read_plan_review(project, plan_slug)
    if newest is None:
        return None
    version = int(newest.get("plan_version") or 0)
    detail = (
        f"the newest stored review of {project}:{plan_slug} is version {version} "
        f"and no longer covers the plan; run `{plan_review.review_invocation(project, plan_slug)}` "
        "to compose a review of the current content"
    )
    return version, detail


def _review_owed_fields(
    project: str,
    slug: str,
    written_path: str | None,
    *,
    uncovered_before: set[str] | None = None,
) -> dict[str, Any]:
    """The review a successful plan write owes its author.

    A plan is reviewed before it is built, and the moment it changes is the
    moment its author is present, so the write that changed it names the units
    the coverage predicate now reports uncovered rather than leaving a later
    dispatcher to refuse the build. ``review_owed`` lists each uncovered unit
    with the measured change the predicate computes where one exists, and is an
    empty list when nothing is owed; ``review_invocation`` is the one-line
    command that composes a review scoped to exactly those units. The tool composes no
    review itself — the author decides when a session's authoring is finished —
    so a burst of edits earns one review and no review is attributed to a
    session that did not ask for it.

    ``uncovered_by_write`` is the narrower list: the units THIS write moved from
    covered to uncovered, found by subtracting the coverage the caller read
    before the write (``uncovered_before``) from the coverage now. A stored
    review leaves a unit uncovered until its prose is reviewed again, so a
    metadata or lifecycle write reports the whole debt in ``review_owed`` while
    uncovering nothing here, and a coordinator acting on its own write would
    otherwise compose a review the plan did not need. A create declares an
    empty ``uncovered_before`` — it has no content before it — so it names
    every unit it wrote. A caller that could not read the plan's coverage
    before the write passes ``None``, and the field is null: what the write
    uncovered is unknown, which is a different fact from "it uncovered nothing".

    A predicate that cannot answer is reported as unknown, not as nothing owed:
    ``review_owed`` and ``uncovered_by_write`` are null and
    ``review_owed_error`` names why (the written path was not reported, or the
    predicate raised). An empty list and a null
    are different facts — nothing is owed, or the debt is unknown — and
    collapsing the second into the first would let a failed read pass for a
    clean answer. The write itself has already succeeded, so every exception at
    this one point is caught and named in the response rather than escaping
    after the write and reporting a completed write as a tool error, which its
    author may retry into a version conflict. Nothing is hidden: the error is
    stated in ``review_owed_error``.
    """
    from reckon.crew import plan_review

    invocation = plan_review.review_invocation(project, slug)
    if written_path is None:
        return {
            "review_owed": None,
            "uncovered_by_write": None,
            "review_owed_error": "the successful write reported no path to the plan",
            "review_invocation": invocation,
        }
    try:
        _records, uncovered, changes = plan_review.review_coverage(
            project, slug, plan=Path(written_path)
        )
    except Exception as error:  # noqa: BLE001 — named in the response, not swallowed
        return {
            "review_owed": None,
            "uncovered_by_write": None,
            "review_owed_error": f"{type(error).__name__}: {error}",
            "review_invocation": invocation,
        }
    owed = [{"unit": unit, "change": changes.get(unit)} for unit in sorted(uncovered)]
    if uncovered_before is None:
        fields: dict[str, Any] = {
            "review_owed": owed,
            "uncovered_by_write": None,
            "uncovered_by_write_error": (
                "the plan's coverage before the write could not be read, so what "
                "this write uncovered is unknown"
            ),
            "review_invocation": invocation,
        }
        return fields
    by_write = [
        {"unit": unit, "change": changes.get(unit)}
        for unit in sorted(uncovered - uncovered_before)
    ]
    return {
        "review_owed": owed,
        "uncovered_by_write": by_write,
        "review_invocation": invocation,
    }


def _plan_status(
    project: str, slug: str, checkout_path: str | None = None
) -> str | None:
    """The persisted status of a plan, or ``None`` when it cannot be read.

    The plan's own HTML is the sole store, so its status is read from the
    resolved file rather than from any derived surface. ``None`` distinguishes
    a plan whose status could not be read from one that reads an empty status,
    so a caller can reserve a status-dependent branch for a plan whose status
    it actually has.
    """
    from reckon import _plan_html

    try:
        path = _resolve_html_file(project, slug, checkout_path, "plan")
        if path is None:
            return None
        data = _plan_html.read_state(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — an unreadable status is answered by the roadmap below
        return None
    return str(data.get("status") or "").strip().lower() or None


def _plan_readiness_fields(
    project: str, slug: str, checkout_path: str | None = None
) -> dict[str, Any]:
    """The readiness a plan write leaves behind, with what stands before its close.

    A section moves to done through the landing write, so that write is the
    moment a plan can newly read finished. ``readiness`` carries the roadmap's
    own verdict after the write — ``closable`` once no declared section admits
    work — and ``closure_blockers`` enumerates every obstacle between the plan
    and its ``close``, each entry naming the op that clears it. The write that
    ticks the last implementable section therefore answers ``closable`` and the
    close op together, so the coordinator does not have to re-read the roadmap
    to learn the plan finished.

    A plan already past its close reads ``closed`` instead, and the roadmap is
    not consulted for it. The roadmap's ``pending_work`` carries open plans
    only, so a terminal plan has no row there and a lookup would answer with an
    error naming a roadmap defect where the roadmap is behaving correctly —
    read by exactly the coordinator that has just closed the plan. Every
    terminal status resolves to the same ``closed``, whatever spelling put the
    plan there: nothing stands between such a plan and its close, because it is
    already past it.

    For a live plan the verdict is the roadmap's rather than a second
    computation of the same fact, so this reads the assembled roadmap the
    roadmap surface serves and selects the row for this plan; nothing about
    readiness is recomputed here.

    A read that cannot answer is reported as unknown, not as ready:
    ``readiness`` is null and ``readiness_error`` names why (the roadmap
    declined, no row matched the plan, or the read raised). The write itself has
    already succeeded, so every failure at this one point is caught and named in
    the response rather than escaping after the write and reporting a completed
    write as a tool error, so nothing here raises out of a landed write.
    """
    if _plan_status(project, slug, checkout_path) in TERMINAL_STATUSES:
        return {"readiness": "closed", "closure_blockers": []}
    try:
        from reckon.mcp import _roadmap_scoped

        report = _roadmap_scoped(project, checkout_path=checkout_path)
    except Exception as error:  # noqa: BLE001 — named in the response, not swallowed
        return {
            "readiness": None,
            "readiness_error": f"{type(error).__name__}: {error}",
        }
    if isinstance(report, dict) and report.get("ok") is False:
        return {
            "readiness": None,
            "readiness_error": f"the roadmap declined: {report.get('error')}",
        }
    rows = report.get("pending_work") if isinstance(report, dict) else None
    row = next((item for item in rows or [] if item.get("slug") == slug), None)
    if row is None:
        return {
            "readiness": None,
            "readiness_error": f"the roadmap carries no row for {project}:{slug}",
        }
    return {
        "readiness": row.get("readiness"),
        "closure_blockers": list(row.get("closure_blockers") or []),
    }


#: Environment variable a crew run exports into every worker it launches. The
#: worker's harness — and the MCP server that harness starts as a child — inherit
#: it, so a plan write can tell it is running inside a run.
RUN_ID_ENV = "RECKON_RUN_ID"


def _run_worktree(run_id: str) -> tuple[str | None, str | None]:
    """The (worktree, project) a live run recorded, or (None, None).

    The live pointer is the run's own record of the checkout its worker holds,
    so it is the authority for scoping a write. A pointer that cannot be read
    leaves the write unscoped rather than guessing a directory.
    """

    try:
        record = read_run_pointer(run_id)
    except CrewError:
        return None, None
    worktree = record.get("worktree")
    project = record.get("project")
    return (str(worktree) if worktree else None, str(project) if project else None)


#: The remedy a run-scoped call to a granular mutator has. Those entry points
#: take no ``checkout_path`` and the run's own project is exactly what the guard
#: refuses, so the ``checkout_path``-or-own-project hint the registered entry
#: point carries would point at two options neither of which can work here.
_GUARDED_WRITE_HINT = (
    "Record the change through the registered edit_plan tool, which redirects a "
    "run-scoped write for the run's own project into the run's worktree. This "
    "entry point takes no checkout_path and cannot write into the run's own "
    "worktree."
)


def _run_scoped_refusal(
    run_id: str,
    project: str,
    slug: str,
    doc_type: str | None,
    detail: str,
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    """The structured refusal for a run-scoped write that must not proceed.

    ``hint`` defaults to the registered entry point's remedy — pass
    ``checkout_path``, or write only the run's own project. A caller whose entry
    point cannot honour either must supply its own.
    """

    target = _written_path(project, slug, None, doc_type)
    where = (
        f"the mounted main checkout ({target})"
        if target
        else "the mounted main checkout"
    )
    return {
        "ok": False,
        "error": "run_scoped_write",
        "message": (
            f"Refused a plan write for run {run_id}: {detail}. A run's plan write "
            f"lands in its own worktree; this one would have written {where}."
        ),
        "run_id": run_id,
        "project": project,
        "slug": slug,
        "would_write": target,
        "hint": (
            hint
            if hint is not None
            else (
                "Pass checkout_path explicitly to target a checkout, or write "
                "only the plan this run's own project owns."
            )
        ),
    }


def _run_scoped_checkout(
    project: str,
    slug: str,
    doc_type: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a worker's plan write to its own run worktree.

    Returns ``(root, refusal)``. When ``RECKON_RUN_ID`` names a live run, a
    write with no explicit ``checkout_path`` may only target the run's own
    project, and it lands in the worktree the run recorded. A caller with no
    run — a coordinator — is unaffected.
    """

    run_id = os.environ.get(RUN_ID_ENV)
    if not run_id:
        return None, None
    worktree, run_project = _run_worktree(run_id)
    if worktree is None:
        return None, _run_scoped_refusal(
            run_id, project, slug, doc_type, "the run has no recorded worktree"
        )
    if run_project != project:
        return None, _run_scoped_refusal(
            run_id,
            project,
            slug,
            doc_type,
            f"it is scoped to project {run_project!r}, not {project!r}",
        )
    return worktree, None


def _run_scoped_read_root(project: str | None) -> str | None:
    """The worktree a run-scoped plan read resolves to, or None for main.

    A read differs from a write: a run may legitimately read another project's
    plan, and the run's worktree holds no copy of it, so only the run's OWN
    project is redirected. A run that cannot be resolved — no pointer, no
    recorded worktree — reads the main checkout as before rather than failing,
    because a read leaves nothing behind to lose.
    """

    if not project or project == "*":
        return None
    run_id = os.environ.get(RUN_ID_ENV)
    if not run_id:
        return None
    worktree, run_project = _run_worktree(run_id)
    if worktree is None or run_project != project:
        return None
    return worktree


def _run_scoped_write_guard(
    project: str,
    slug: str,
    doc_type: str | None = None,
) -> dict[str, Any] | None:
    """Refuse a run-scoped plan write that would resolve outside its worktree.

    The registered write entry point redirects a run's own project into the
    run's worktree. The granular plan mutators take no ``checkout_path``, so
    their write would resolve to the mounts-registered main checkout — a path
    outside the run's worktree — for the run's own project as much as for any
    other. A run-scoped call is therefore refused here, naming the run and the
    path it would have written, rather than returning ``ok``. A caller with no
    run is unaffected.
    """

    run_id = os.environ.get(RUN_ID_ENV)
    if not run_id:
        return None
    worktree, run_project = _run_worktree(run_id)
    if worktree is None:
        return _run_scoped_refusal(
            run_id,
            project,
            slug,
            doc_type,
            "the run has no recorded worktree",
            hint=_GUARDED_WRITE_HINT,
        )
    if run_project != project:
        return _run_scoped_refusal(
            run_id,
            project,
            slug,
            doc_type,
            f"it is scoped to project {run_project!r}, not {project!r}",
            hint=_GUARDED_WRITE_HINT,
        )
    return _run_scoped_refusal(
        run_id,
        project,
        slug,
        doc_type,
        "this entry point cannot write into the run's own worktree",
        hint=_GUARDED_WRITE_HINT,
    )


def _document_path_hint(kwargs: Mapping[str, Any]) -> str | None:
    path = kwargs.get("path")
    return str(path) if path else None


def _crew_call_kind(kwargs: Mapping[str, Any]) -> str:
    """A crew recovery action writes; every crew read view does not."""

    return "write" if kwargs.get("action") else "read"


def _resource_reference(
    project: str,
    slug: str,
    doc_type: str | None,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Build the typed affected-resource identity used by write responses."""

    resource_type = (
        canonical_type(doc_type)
        if doc_type
        else ("project" if slug in {"index", "project"} else "plan")
    )
    resource_id = "project" if resource_type == "project" and slug == "index" else slug
    return {
        "project": project,
        "type": resource_type,
        "id": resource_id,
        "archived": False,
        "title": title or resource_id,
    }


def _resource_title(data: dict[str, Any], fallback: str) -> str:
    """Choose the human label for one write response."""

    return str(
        data.get("title")
        or data.get("theme")
        or data.get("name")
        or data.get("summary")
        or fallback
    )


def _op_error_response(exc: OpError) -> dict[str, Any]:
    """The op_error payload, carrying the entry a duplicate collision names.

    The refusal message stays the short pinned sentence; the entry already
    holding the id rides beside it as ``existing_item`` so a writer can retry
    with a distinct id without re-reading the plan to find out what it met.
    """
    response: dict[str, Any] = {
        "ok": False,
        "error": "op_error",
        "detail": str(exc),
    }
    existing_item = getattr(exc, "existing_item", None)
    if existing_item:
        response["existing_item"] = existing_item
    return response


def _conflict_response(
    exc: VersionConflict,
    *,
    project: str | None = None,
    slug: str | None = None,
    doc_type: str | None = None,
    operation: str = "edit",
) -> dict[str, Any]:
    title = _resource_title(exc.current_data, slug or "resource")
    result: dict[str, Any] = {
        "ok": False,
        "error": "version_conflict",
        "message": (
            f"Could not {operation} {title}: expected version {exc.expected}, "
            f"but the current version is {exc.current}."
        ),
        "operation": operation,
        "expected_version": exc.expected,
        "current_version": exc.current,
        "hint": "Re-read the plan with reckon.read_plan to get the current version, then retry.",
    }
    if project is not None and slug is not None:
        result["resource"] = _resource_reference(project, slug, doc_type, title=title)
    return result


def _edit_success_response(
    *,
    project: str,
    slug: str,
    doc_type: str | None,
    new_version: int,
    data: dict[str, Any] | None = None,
    created: bool = False,
) -> dict[str, Any]:
    """Translate one successful edit into human and machine-readable forms."""

    operation = "create" if created else "edit"
    title = _resource_title(data or {}, slug)
    resource = _resource_reference(project, slug, doc_type, title=title)
    verb = "Created" if created else "Updated"
    return {
        "ok": True,
        "message": (f"{verb} {resource['type']} {title} to version {new_version}."),
        "operation": operation,
        "resource": resource,
        "project": project,
        "slug": slug,
        "new_version": new_version,
    }


def _written_path(
    project: str,
    slug: str,
    root: str | None,
    doc_type: str | None = None,
) -> str | None:
    """Best-effort absolute path of the file edit_plan just wrote.

    For index/project slugs → the JSON state file; for plan slugs → the resolved
    HTML file.  Returned to callers so they can reconcile the write
    deterministically (e.g. ``git -C <dir> status`` in the right checkout).
    Never raises — returns None if the path cannot be resolved.
    """
    try:
        if slug in ("index", "project"):
            return str(state_path(project, slug, root))
        hit = _resolve_html_file(project, slug, root, doc_type)
        return str(hit) if hit is not None else None
    except Exception:  # noqa: BLE001 — path reporting must never fail the write
        return None
