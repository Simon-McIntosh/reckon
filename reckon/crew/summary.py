from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from reckon.crew.node import SUMMARY_AXES

# ── The summary reflex ──────────────────────────────────────────────────────


def validate_summary(text: str, *, occasion: str) -> dict[str, Any]:
    """Check a four-axis summary, and that a reporting one carries evidence.

    One discipline binds the reflex to the gating reflex and is why the format
    earns its place: at completion, WHY carries the gate evidence. That forces
    every wave report to be quantitative, and makes a wave that cannot state its
    measure visibly incomplete rather than plausibly done. A hold is held to the
    same standard, because "we are out of budget" without a figure and a reset
    time is not a report a lead can act on.
    """
    axes: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^(WHAT|WHY|HOW|WHEN)\b\s*(.*)$", line)
        if match:
            current = match.group(1)
            axes.setdefault(current, [])
            if match.group(2).strip():
                axes[current].append(match.group(2).strip())
        elif current and line:
            axes[current].append(line)
    findings = [
        f"axis {axis} is missing" for axis in SUMMARY_AXES if not axes.get(axis)
    ]
    findings += [
        f"axis {axis} runs to {len(lines)} lines; at most two"
        for axis, lines in sorted(axes.items())
        if len(lines) > 2
    ]
    if occasion in ("completion", "hold"):
        why = " ".join(axes.get("WHY", []))
        if not re.search(r"\d", why):
            findings.append(
                f"{occasion} WHY carries no quantitative evidence; state the "
                "measure and its value"
            )
    return {
        "ok": not findings,
        "axes": {k: list(v) for k, v in sorted(axes.items())},
        "findings": findings,
    }


# ── The concurrency bound reflex ────────────────────────────────────────────
#
# Concurrency is bounded by whichever resource runs out first, and a fleet that
# reports a single number cannot say which. Three candidates are read here: the
# roster ceiling a backend declares, the cores a placement's reservation admits,
# and the login memory slice the coordinator still lives inside. Each answers
# "may one more worker start" and the tightest readable one is the binding
# bound, so a refusal can name the resource that actually ran out rather than
# the one a reader happened to be watching.
#
# The memory slice is read as ``memory.current`` against ``memory.max`` with the
# allocation-stall counter beside them, because those are the numbers that move
# before a kill. ``oom_kill`` is reported beside them as history only: it
# increments once a process is already dead, so it can confirm a kill and can
# never warn of one.

BOUND_ORDER = ("roster", "partition-cores", "login-memory")

# Where the login host's cgroup tree is mounted. A cgroup v2 hierarchy reads
# its limits from ``memory.max`` and ``memory.events`` files; a tree whose files
# cannot be read yields an unknown bound rather than a refusal, because an
# absent reading is never evidence of exhaustion.
LOGIN_CGROUP_ROOT = Path("/sys/fs/cgroup")

# Path the running process reports its own cgroup membership in. Read rather
# than assumed so a session scope nested under the user slice resolves to the
# slice above it, which is where the fleet's memory limit actually sits.
SELF_CGROUP_PATH = Path("/proc/self/cgroup")

_LOGIN_SLICE_UNREADABLE = "cgroup files unreadable"


@dataclass(frozen=True)
class LoginSlice:
    """One reading of the login host's memory slice, or an honest absence."""

    readable: bool
    current: int | None = None
    maximum: int | None = None
    alloc_stalls: int | None = None
    oom_kills: int | None = None
    detail: str = ""

    @property
    def utilisation(self) -> float | None:
        """Fraction of the slice in use, or None when it cannot be read."""
        if not self.readable or not self.maximum:
            return None
        return (self.current or 0) / self.maximum


@dataclass(frozen=True)
class Bound:
    """One candidate limit on concurrency, with the figure a reader needs.

    ``capacity`` is the number of concurrent workers the bound admits and
    ``utilisation`` how much of it is spent now; both are None when the bound
    could not be read. ``admits_one_more`` answers the admission question
    directly rather than by comparing a label, so a bound whose capacity is not
    expressible in workers (the memory slice covers resident bytes, not counts)
    is still enforced.
    """

    name: str
    capacity: float | None
    utilisation: float | None
    admits_one_more: bool
    value: str
    history: str = ""
    detail: str = field(default="")


def _option_values(options: Any, names: tuple[str, ...]) -> list[str]:
    """Values declared by any of ``names`` among a placement's options.

    Options are scheduler argument strings. An option is read as ``name=value``
    or as ``name value``; anything else is not this option and is skipped, so a
    wrapper's unrelated flags never parse as a resource request.
    """
    found: list[str] = []
    tokens = [str(option) for option in options or ()]
    for index, token in enumerate(tokens):
        for name in names:
            if token.startswith(f"{name}="):
                found.append(token[len(name) + 1 :].strip())
            elif token == name and index + 1 < len(tokens):
                found.append(str(tokens[index + 1]).strip())
    return [value for value in found if value]


def _as_count(text: str) -> int | None:
    """A positive integer count, or None for anything that is not one."""
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


_MEMORY_UNITS = {"": 1024**2, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _as_bytes(text: str) -> int | None:
    """A memory figure in bytes, or None for anything that is not one.

    Scheduler clients spell memory as a bare number of mebibytes or with a
    KiB/MiB/GiB/TiB suffix, so both forms are read. An unreadable figure is
    None rather than zero: zero would admit every worker and turn a typo into
    an unbounded lane.
    """
    match = re.fullmatch(r"\s*(\d+)\s*([KMGTkmgt]?)[Bb]?\s*", str(text))
    if not match:
        return None
    return int(match.group(1)) * _MEMORY_UNITS[match.group(2).upper()]


def _finite(text: str) -> int | None:
    """A cgroup limit as an integer, or None when the file says 'max'.

    ``max`` is the kernel's word for no limit rather than a large number, so it
    is read as an absent bound and never as a value a comparison could use.
    """
    stripped = str(text).strip()
    if not stripped or stripped == "max":
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _user_slice_dir(cgroup_root: Path, self_cgroup: str | None) -> Path | None:
    """The outermost cgroup under ``cgroup_root`` carrying a finite memory.max.

    The process's own scope sits inside the user slice, and the slice is the
    level the fleet's limit is declared at. Walking outward and keeping the
    last finite limit found reads that level without hardcoding a uid or a
    session id, so the same code follows whichever scope the session landed in.
    """
    raw = self_cgroup
    if raw is None:
        try:
            raw = SELF_CGROUP_PATH.read_text(encoding="utf-8")
        except OSError:
            return None
    relative = ""
    for line in str(raw).splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            relative = parts[2].strip()
            break
    if not relative:
        return None
    parts = [segment for segment in relative.strip("/").split("/") if segment]
    found: Path | None = None
    for depth in range(len(parts), 0, -1):
        candidate = cgroup_root.joinpath(*parts[:depth])
        limit = candidate / "memory.max"
        try:
            text = limit.read_text(encoding="utf-8")
        except OSError:
            continue
        if _finite(text) is not None:
            found = candidate
    return found


def read_login_slice(
    *, cgroup_root: Path | None = None, self_cgroup: str | None = None
) -> LoginSlice:
    """Read the login host's memory slice, or report it as unknown.

    Nothing here refuses. A slice whose files are unreadable returns
    ``readable=False`` and the callers treat the bound as unknown, because a
    reading that could not be taken is never a reading that says the slice is
    full — the same direction an absent ceiling takes.
    """
    root = Path(cgroup_root) if cgroup_root is not None else LOGIN_CGROUP_ROOT
    directory = _user_slice_dir(root, self_cgroup)
    if directory is None:
        return LoginSlice(readable=False, detail=_LOGIN_SLICE_UNREADABLE)
    try:
        current = int((directory / "memory.current").read_text(encoding="utf-8").strip())
        maximum = _finite((directory / "memory.max").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LoginSlice(readable=False, detail=_LOGIN_SLICE_UNREADABLE)
    if maximum is None:
        return LoginSlice(
            readable=False, current=current, detail="memory.max declares no limit"
        )
    stalls: int | None = None
    kills: int | None = None
    try:
        events = (directory / "memory.events").read_text(encoding="utf-8")
    except OSError:
        events = ""
    for line in events.splitlines():
        key, _, value = line.partition(" ")
        if key == "max":
            stalls = int(value) if value.strip().isdigit() else None
        elif key == "oom_kill":
            kills = int(value) if value.strip().isdigit() else None
    return LoginSlice(
        readable=True,
        current=current,
        maximum=maximum,
        alloc_stalls=stalls,
        oom_kills=kills,
    )


def _gib(count: int | None) -> str:
    """A byte count as GiB with one decimal, the unit a slice is sized in."""
    if count is None:
        return "unknown"
    return f"{count / 1024**3:.1f} GiB"


def roster_bound(backend: Mapping[str, Any], *, occupancy: int) -> Bound:
    """The backend's declared roster ceiling, or an unbounded lane.

    ``max_concurrent_runs`` is the roster the plan makes the concurrency
    authority. A backend that declares none is unlimited: an undeclared
    ceiling cannot justify refusing work, so the bound is reported as
    unbounded rather than as a zero that would refuse the first worker.
    """
    ceiling = backend.get("max_concurrent_runs")
    if (
        ceiling is None
        or isinstance(ceiling, bool)
        or not isinstance(ceiling, int)
        or ceiling <= 0
    ):
        return Bound(
            name="roster",
            capacity=None,
            utilisation=None,
            admits_one_more=True,
            value="unbounded",
            detail="no max_concurrent_runs declared",
        )
    return Bound(
        name="roster",
        capacity=float(ceiling),
        utilisation=occupancy / ceiling,
        admits_one_more=occupancy < ceiling,
        value=f"{occupancy} live runs of {ceiling} max",
    )


def cores_bound(backend: Mapping[str, Any], *, occupancy: int) -> Bound:
    """The cores a placement's reservation admits, as a worker count.

    A placement declares its resource request among the scheduler options it
    passes: the cores admitted to the whole reservation and the share one
    worker asks for. Their quotient is how many concurrent workers fit inside
    it. Either figure being absent leaves the bound unknown — a reservation
    whose size was never stated cannot refuse a worker.
    """
    placement = backend.get("placement")
    options = (placement or {}).get("options") if isinstance(placement, Mapping) else None
    admitted = None
    for text in _option_values(options, ("--cpus", "--ntasks")):
        admitted = _as_count(text)
        if admitted is not None:
            break
    share = 1
    for text in _option_values(options, ("--cpus-per-task", "-c")):
        parsed = _as_count(text)
        if parsed is not None:
            share = parsed
            break
    if admitted is None or not isinstance(placement, Mapping) or not placement:
        return Bound(
            name="partition-cores",
            capacity=None,
            utilisation=None,
            admits_one_more=True,
            value="unknown",
            detail="placement declares no admitted core count",
        )
    demanded = occupancy * share
    return Bound(
        name="partition-cores",
        capacity=admitted / share,
        utilisation=demanded / admitted,
        admits_one_more=(demanded + share) <= admitted,
        value=f"{demanded} of {admitted} cores admitted ({share} per worker)",
    )


def memory_bound(
    backend: Mapping[str, Any], *, occupancy: int, login_slice: LoginSlice
) -> Bound:
    """The login memory slice, read against the per-worker memory it must hold.

    A worker's share is the memory its placement reserves, so the admission
    question is whether the slice can hold one more of them. When the slice
    cannot be read the bound is unknown and admits, because a reading that was
    never taken is not evidence that the slice is full. When a placement
    reserves no memory the slice still reports its own utilisation, but no
    worker is refused on it: a bound that cannot price a worker cannot say the
    next one does not fit.
    """
    if not login_slice.readable:
        return Bound(
            name="login-memory",
            capacity=None,
            utilisation=None,
            admits_one_more=True,
            value=f"unknown ({login_slice.detail or _LOGIN_SLICE_UNREADABLE})",
        )
    placement = backend.get("placement")
    options = (placement or {}).get("options") if isinstance(placement, Mapping) else None
    share: int | None = None
    for text in _option_values(options, ("--mem", "--mem-per-cpu")):
        parsed = _as_bytes(text)
        if parsed is not None:
            share = parsed
            break
    current = login_slice.current or 0
    maximum = login_slice.maximum or 0
    admits = share is None or (current + share) <= maximum
    value = f"{_gib(current)} of {_gib(maximum)} resident"
    if share is not None:
        value += f", {_gib(share)} per worker"
    history = (
        f"allocation stalls {login_slice.alloc_stalls}; "
        f"oom_kill {login_slice.oom_kills} (history)"
    )
    return Bound(
        name="login-memory",
        capacity=maximum / share if share else None,
        utilisation=login_slice.utilisation,
        admits_one_more=admits,
        value=value,
        history=history,
    )


def concurrency_bounds(
    backend: Mapping[str, Any],
    *,
    occupancy: int,
    login_slice: LoginSlice | None = None,
) -> list[Bound]:
    """Every readable candidate bound on one backend, in a fixed order."""
    reading = login_slice if login_slice is not None else read_login_slice()
    return [
        roster_bound(backend, occupancy=occupancy),
        cores_bound(backend, occupancy=occupancy),
        memory_bound(backend, occupancy=occupancy, login_slice=reading),
    ]


def binding_bound(bounds: list[Bound]) -> Bound | None:
    """The bound that actually limits the next worker.

    A bound that would refuse the next worker is binding before any bound that
    would admit it, because that is the one the dispatch will hit. Among equals
    the most nearly spent wins, and a fixed order breaks a tie so the same
    figures always name the same bound. None means every bound is unknown or
    unbounded, which is never a refusal.
    """
    readable = [bound for bound in bounds if bound.utilisation is not None]
    if not readable:
        return None
    refusing = [bound for bound in readable if not bound.admits_one_more]
    pool = refusing or readable
    return max(
        pool,
        key=lambda bound: (bound.utilisation, -BOUND_ORDER.index(bound.name)),
    )


def fleet_bound_report(
    backend: Mapping[str, Any],
    *,
    occupancy: int,
    login_slice: LoginSlice | None = None,
) -> dict[str, Any]:
    """The fleet surface's answer to which bound binds, with its figure.

    Returned as data rather than as prose so a CLI view and the ticker render
    the same reading, and so a test asserts the value beside the label instead
    of matching a sentence that a later wording change would silently break.
    """
    bounds = concurrency_bounds(
        backend, occupancy=occupancy, login_slice=login_slice
    )
    binding = binding_bound(bounds)
    return {
        "binding": binding.name if binding is not None else None,
        "value": binding.value if binding is not None else "unknown",
        "bounded": binding is not None,
        "refused": binding is not None and not binding.admits_one_more,
        "bounds": [
            {
                "name": bound.name,
                "value": bound.value,
                "history": bound.history,
                "utilisation": bound.utilisation,
                "admits_one_more": bound.admits_one_more,
                "binding": bound is binding,
            }
            for bound in bounds
        ],
    }


def bound_refusal_text(bound: Bound, *, backend_name: str, occupying: list[str]) -> str:
    """One sentence naming the bound that refused and the figure that refused it.

    The roster keeps the wording it has always had, because that sentence is
    what a reader has learned to act on and the remedy it carries — finish a
    run or raise the declared ceiling — is specific to a declared ceiling. The
    two resource bounds get their own sentence naming the resource and its
    measured value, since neither remedy is the roster's.
    """
    listed = ", ".join(sorted(occupying)) or "none"
    if bound.name == "roster":
        return (
            f"node is not dispatchable — backend {backend_name!r} is at its "
            f"concurrency ceiling ({bound.value}); the runs occupying its "
            f"slots: {listed}. Wait for one to finish, or raise "
            f"max_concurrent_runs for this backend."
        )
    if bound.name == "partition-cores":
        return (
            f"node is not dispatchable — backend {backend_name!r} is at its "
            f"partition admitted cores bound ({bound.value}); one more worker "
            f"would ask the reservation for more cores than it was admitted, "
            f"and the runs occupying it: {listed}. Wait for one to finish, or "
            f"raise the cores the placement declares."
        )
    return (
        f"node is not dispatchable — backend {backend_name!r} is at its login "
        f"memory slice bound ({bound.value}; {bound.history}); one more worker "
        f"would exceed memory.max, and the runs occupying it: {listed}. Wait "
        f"for one to finish, or reduce the memory a worker reserves."
    )


# ── The crew summary read ───────────────────────────────────────────────────
#
# Two figures a reader asks a completed node for are already in its row, and
# neither is emitted. Wall time is the span between the row's own dispatch and
# completion stamps, and width-at-start is how many runs were in flight when it
# was dispatched. Both are derived here, at read time, from the stamps rather
# than read back: the row's stored ``wall_seconds`` field was measured as a
# different quantity from the span on nearly half the ledger, and a reader who
# took it for the span's value derived a false rate from it twice. So the stored
# field is never passed through — the answer carries the stamp-derived figure
# under the same key, and an unstamped row carries nothing rather than a zero,
# because a run whose span was not measurable is not a run that took no time.


def _row_moment(record: Mapping[str, Any], key: str) -> datetime | None:
    """One stamp of a row as an aware datetime, or None when unusable.

    A stamp without an offset is read as UTC, matching the ledger's own
    convention when it derives seconds from the same pair, so a stamp written
    by a machine that omitted the offset is not silently shifted.
    """

    raw = record.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def run_rows(
    project: str,
    *,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read the project's committed run rows, each with its two derived figures.

    Each returned row is the ledger record with ``wall_seconds`` replaced by the
    figure derived from that row's own ``dispatched_at`` and ``completed_at``,
    and ``width_at_start`` added: the number of runs whose own measured span
    covers the moment this one was dispatched, counted from the same rows.

    Both figures are absent, never zero, when the stamps they need are missing
    or malformed. Width needs the dispatch stamp alone — a run created at a
    known moment has a width at that moment whatever became of its ending — while
    wall time needs the pair. A run that carries no completion stamp
    contributes to no other row's width either, because a run whose end was
    never measured cannot be shown to have been in flight; assuming it was
    still running would be inventing the measurement this read exists to derive.
    """

    from reckon import ledger

    records = ledger.runs(project, root)
    measured: list[tuple[datetime, datetime]] = []
    for record in records:
        started = _row_moment(record, "dispatched_at")
        finished = _row_moment(record, "completed_at")
        if started is not None and finished is not None:
            measured.append((started, finished))
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        # Derived per row rather than carried over: the stored field is the
        # quantity a reader must never be handed under this name.
        row["wall_seconds"] = ledger._worker_seconds(
            record.get("dispatched_at"), record.get("completed_at")
        )
        started = _row_moment(record, "dispatched_at")
        row["width_at_start"] = (
            None
            if started is None
            else sum(
                1
                for span_start, span_end in measured
                if span_start <= started <= span_end
            )
        )
        rows.append(row)
    return rows
