import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import click

# ``checkout_trust`` is read through this module by callers and tests, and the
# reloader family lives in ``reckon.crew.follower_reload``; both stay importable
# (re-exported) here so a caller reaching them through this module keeps working.
from reckon import checkout_trust  # noqa: F401 - re-exported for callers
from reckon._timestamps import parse_utc
from reckon.crew.follower_reload import (  # noqa: F401 - re-exported for callers
    _FollowerReloader,
    _probe_retry_jitter,
    _StampPoll,
)
from reckon.crew.session_host import SETTLE_ENV, SETTLED_EXIT_CODE, owner_alive
from reckon.crew_dispatch_commands import _crew_modules, _emit_crew_result, crew
from reckon.env_duration import read_duration_override


@crew.command(name="station")
@click.option(
    "--interval", type=click.FloatRange(min=0.01), default=1.0, show_default=True
)
@click.option(
    "--once", is_flag=True, help="Run one ingest and classification tick, then exit."
)
def crew_station(interval: float, once: bool) -> None:
    """Run the single node-local station for every mounted project."""
    from reckon.crew.station import StationHeldError, run_station
    from reckon.crew.station_store import StationStoreError

    try:
        run_station(interval=interval, once=once)
    except (StationHeldError, StationStoreError) as exc:
        raise click.ClickException(str(exc)) from exc


def _follow_selects(
    event: Mapping[str, Any],
    *,
    session: str | None,
    observed: Iterable[str] = (),
    run_ids: tuple[str, ...] = (),
) -> bool:
    """Decide whether one transition belongs to this follower's fleet.

    An event whose owner cannot be established — a line a pre-upgrade producer
    rendered, or a pointer carrying no session — reaches every follower. A
    reader that drops what it cannot attribute converts an unknown into a
    silence, which is the failure this whole surface exists to prevent.

    A follower narrows by the owning ``session`` and, when named, also admits
    the ``observed`` sessions. The two halves are not the same: the owning
    session is registered for delivery so a dispatch guard may trust it, while
    an observed session is admitted for oversight only — nothing is recorded
    for it, and a guard consulted for it still reports no delivery.

    Every transition that belongs to the fleet is delivered; the follower
    carries no state filter. A filter that reports how a run stopped and never
    how it recovered is worse than none, because it hides the all-clear the
    very same reader is waiting for, and a filter matching nothing is
    indistinguishable from a follower that never started.

    A line an older producer wrote names neither a session nor a run, so it is
    attributable to no fleet in particular. Only a fleet-wide reader — one with
    no session and no named runs — can claim it. A follower that has scoped
    itself must not: handing a scoped follower an unattributable row is how a
    session received another session's history as though it were its own, which
    is the leak the ``--session`` and ``--run`` filters exist to close.
    """
    if event.get("legacy"):
        return session is None and not run_ids
    owner = str(event.get("session") or "")
    if session is not None and owner and owner != session and owner not in observed:
        return False
    if run_ids and str(event.get("run_id") or "") not in run_ids:
        return False
    return True



def _follow_render_event(
    event: Mapping[str, Any],
    *,
    session: str | None,
    observed: Iterable[str],
) -> Mapping[str, Any]:
    """Prepare one delivered row for the ticker, marking observed rows foreign.

    The ticker draws its owner column — and its foreign-owner glyph, for any
    row that carries a session — only when asked ``with_session``. An observing
    follower asks for it on every row so the grid stays aligned, which means
    the owning session's own rows must reach the ticker without their session:
    they are this reader's own, so the cell reads blank rather than foreign.
    Observed rows keep their session and so carry the glyph. Without observed
    sessions no row is rewritten and nothing about the scoped rendering
    changes.
    """
    if not observed or session is None:
        return event
    if str(event.get("session") or "") == session:
        return {**event, "session": ""}
    return event



class _StationRegistration:
    """A subscriber's registration, in the shape the wait pass expects.

    The registration is the pane's own row in the node's store rather than a
    lock under the crew home. The wait pass only asks whether it is held, so a
    small adapter keeps the delivery lease — which a reader on another host
    still reads — renewing exactly as before.
    """

    def __init__(self, subscriber) -> None:
        self.subscriber = subscriber

    @property
    def held(self) -> bool:
        return True

    def acquire(self) -> bool:
        return True


def _station_subscriber_for(
    project: str,
    *,
    session: str | None,
    observed: Iterable[str] = (),
    run_ids: Iterable[str] = (),
):
    """The station subscriber this follower should read, or None.

    A station is used only when its store exists, it has beaten at least once,
    and it was built from this follower's own crew home. A store built from
    another home holds another fleet's rows, and a store that has never beaten
    holds none, so both fall back to the producer's stream. A stale heartbeat is
    not a reason to fall back: the pane is owed the station's rows behind it and
    one line saying the station has stopped.
    """
    from reckon.crew import station_store, station_subscriber
    from reckon.crew.runs import crew_home

    try:
        store = station_store.StationStore.at_runtime_root()
    except (station_store.StationStoreError, OSError):
        return None
    try:
        if store.station_value(station_store.CREW_HOME_KEY) != str(crew_home()):
            store.close()
            return None
        if not store.station_value(station_store.HEARTBEAT_KEY):
            store.close()
            return None
    except Exception:  # noqa: BLE001 - an unreadable station is the producer's pane
        store.close()
        return None
    return station_subscriber.StationSubscriber(
        store,
        project,
        session=session,
        observed=observed,
        run_ids=run_ids,
    )


def _sweep_lapsed_holds(
    project: str, *, session: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Resume this project's runs whose provider refusal has lapsed.

    ``session`` confines the sweep to the runs the named session owns, so a
    follower armed to observe another session never resumes that session's runs
    or composes its reviews. A caller with no session to confine to passes
    nothing and sweeps the whole fleet.

    Deferred like every other crew import in this file, and wrapped so a
    recovery that cannot run never takes the follower down with it: the pane a
    reader is watching matters more than any single sweep, and the next cadence
    tick tries again.
    """
    from reckon.crew.resumption import sweep

    return sweep(project, session=session, dry_run=dry_run)


_FOLLOWER_CHECKPOINT_ENV = "RECKON_FOLLOWER_CHECKPOINT"
_FOLLOWER_SOURCE_ROOT_ENV = "RECKON_FOLLOWER_SOURCE_ROOT"
_FOLLOWER_IMAGE_COMMIT_ENV = "RECKON_FOLLOWER_IMAGE_COMMIT"
_FOLLOWER_IMAGE_PIN_ENV = "RECKON_FOLLOWER_IMAGE_PIN"
# The interpreter the host shims start: a reload image has no ``.venv`` of its
# own, so the srun/sbatch/salloc/git shims fall back to a bare ``python3.12``
# on ``PATH`` that cannot import the package's dependencies unless the reloaded
# process names the interpreter it runs under. Carried across the exec so every
# shim the replacement invokes resolves it before any system interpreter.
_SHIM_PYTHON_ENV = "RECKON_SHIM_PYTHON"



_FOLLOWER_LIFETIME_ENV = "RECKON_FOLLOWER_LIFETIME_DEADLINE"



def _carried_lifetime_deadline() -> float | None:
    """Return the UTC epoch an arming ends at, when a reload handed one over.

    Absent for a fresh arming, which is the command's normal path: the host
    starts it in its own environment and no deadline is inherited. Present only
    in an image the follower re-executed into, so a reload continues the
    original arming rather than beginning a new one. The value is read and
    removed from ``os.environ`` in the same step, so no child this image goes on
    to start inherits a deadline that bounds only the follower.
    """
    raw = os.environ.pop(_FOLLOWER_LIFETIME_ENV, "")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None



def _take_follower_checkpoint(project: str) -> dict[str, Any]:
    """Consume the stream position handed across an in-place process reload."""
    raw = os.environ.pop(_FOLLOWER_CHECKPOINT_ENV, "")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("project") != project:
        return {}
    checkpoint = payload.get("checkpoint")
    return dict(checkpoint) if isinstance(checkpoint, dict) else {}



def _import_root(module) -> Path:
    """Return the sys.path entry that supplied an already-imported module."""
    package_name = module.__name__.partition(".")[0]
    package = sys.modules[package_name]
    module_path = Path(module.__file__).resolve()
    package_paths = [
        Path(location).resolve()
        for location in package.__path__
        if module_path.is_relative_to(Path(location).resolve())
    ]
    for entry in sys.path:
        root = Path(entry or os.getcwd()).resolve()
        if any((root / package_name).resolve() == path for path in package_paths):
            return root
    raise RuntimeError(f"cannot identify the import root for {module.__name__}")


# The one proof that a checkout's current code loads. A fresh interpreter is
# required: the running process is the wrong place to discover a broken module,
# because the check must not import the new code into the image it is judging.
# Every tracked module is compiled first, so a syntax error anywhere is reported
# as itself, and then imported, so a rename bound at a call site is caught at
# load rather than at first use.
#
# ``reckon.checkout_trust`` reads this same text, so the follower's reload proof
# and the following process's own use of it cannot disagree about what a
# loadable checkout is.
_FOLLOWER_RELOAD_PROBE = (
    "import importlib, pathlib, sys\n"
    "root = pathlib.Path(sys.argv[1]).resolve()\n"
    "sys.path.insert(0, str(root))\n"
    "package = root / 'reckon'\n"
    "sources = sorted(package.rglob('*.py'))\n"
    "for path in sources:\n"
    "    compile(path.read_bytes(), str(path), 'exec')\n"
    "for path in sources:\n"
    "    parts = list(path.relative_to(root).with_suffix('').parts)\n"
    "    if parts and parts[-1] == '__init__':\n"
    "        parts = parts[:-1]\n"
    "    if not parts or parts[-1] == '__main__':\n"
    "        continue\n"
    "    importlib.import_module('.'.join(parts))\n"
)



# The gap a follower keeps after a probe that ran out of its bound, before it
# spends another probe on the same checkout. The follower's own cadence is a
# second: re-probing that fast would run a thirty-second probe back to back on
# a node already slow enough to have timed the first one out. The gap starts
# here and doubles on every further timeout, so a sustained spike costs a
# handful of probes rather than one per second, and it is capped so a
# checkout that stays busy is still re-checked well inside a session.
FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS = 60.0
FOLLOWER_RELOAD_PROBE_RETRY_CAP_SECONDS = 900.0

# The share of a probe retry gap, drawn at random for each timeout, by which the
# wait before the next probe is shortened. Every follower on a node checks its
# checkout on the same cadence, so without it the retries a herd of followers
# earns for one slow checkout stay in step and re-probe together; drawing each
# wait back by its own fraction spreads them. The shortening is downward only,
# so the gap and its cap remain a true ceiling on how long a checkout waits.
FOLLOWER_RELOAD_PROBE_RETRY_JITTER_FRACTION = 0.25

# The wait before a follower re-reads an image's verdict when another process
# held the proof lock. Nothing failed, so the gap neither doubles nor earns a
# deferral line; it only keeps a follower from re-materialising the image on
# every one-second cadence while the one prover finishes.
FOLLOWER_RELOAD_PROOF_PENDING_SECONDS = 3.0


FOLLOWER_END_EVENT = "follower-end"



FOLLOWER_SETTLE_EVENT = "follower-settle"


# The standing needs-you summary line is gone from the stream: no constant and
# no emitter, because a line that repeats a list the stream already carries woke
# the coordinator to read what it had just seen. What a reader can act on is the
# runs' own rows, and the settle row's tail naming what a drained session owes.
# The rule is stated once in :func:`reckon.crew.ticker.stream_carries`.


# How long a follower's population must hold no unfinished run before the pane
# settles. A dispatch asks its session's host for a follower before it
# publishes the run's pointer, and an admission handover -- the queued
# request's pointer removed, the admitted run's pointer not yet written --
# lasts as long as the dispatch that builds the admitted run's worktree. The
# grace bounds that handover, so a run admitted inside it keeps the pane
# rather than being drawn on a follower that has already left.
FOLLOWER_SETTLE_GRACE_SECONDS = 120.0


# A caller that runs a follower of its own may name the grace for that follower
# through this variable. It exists for the caller that drives a real follower end
# to end and cannot wait two minutes for a drain it arranged itself. Unset, the
# constant above is the grace, so production and every ordinary child keep it.
FOLLOWER_SETTLE_GRACE_ENV = "RECKON_FOLLOWER_SETTLE_GRACE"


def _settle_grace_default() -> float:
    """The grace a follower uses when its caller names none.

    A value the variable cannot parse is the constant rather than an error: the
    grace bounds how long a pane is held open, and a follower that refused to run
    because its environment carried a typo would take the pane down with it.
    """
    return read_duration_override(
        FOLLOWER_SETTLE_GRACE_ENV, FOLLOWER_SETTLE_GRACE_SECONDS, 0, True
    )


# How often a follower allowed to settle samples its session's population while
# the population is live. The scan is a live-pointer read and the wait pass is
# fast, so it is throttled rather than paid on every pass. It is also the cap
# on the sampling cadence, so an empty population is noticed soon after it
# drains and the grace measures the emptiness rather than the poll.
FOLLOWER_SETTLE_SAMPLE_SECONDS = 1.0


FOLLOWER_RESUME_EVENT = "follower-resume"



FOLLOWER_FORMAT_EVENT = "follower-format-changed"



FOLLOWER_STALE_PRODUCER_EVENT = "stale-producer"



FOLLOWER_REATTACH_EVENT = "follower-reattached"



def _snapshot_module() -> Any:
    """The snapshot reader, loaded by file path when nothing has imported it."""
    loaded = sys.modules.get("reckon.crew.obligation_snapshot")
    if loaded is not None:
        return loaded
    from importlib.util import module_from_spec, spec_from_file_location

    path = Path(__file__).resolve().parent / "crew" / "obligation_snapshot.py"
    specification = spec_from_file_location("reckon.crew.obligation_snapshot", path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load the snapshot reader from {path}")
    module = module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module



_SNAPSHOT_READER = _snapshot_module()



_FOLLOWER_RELOAD_PROBE_TIMEOUT = _SNAPSHOT_READER.FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS



PRODUCER_POLL_INTERVAL_CAP_SECONDS = _SNAPSHOT_READER.PRODUCER_POLL_INTERVAL_CAP_SECONDS



PRODUCER_RELOAD_WINDOW_SECONDS = _SNAPSHOT_READER.PRODUCER_RELOAD_WINDOW_SECONDS



FOLLOWER_PRODUCER_RELOADING_EVENT = "producer-reloading"



FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT = "producer-reload-failed"



FOLLOWER_PRODUCER_STOPPED_EVENT = "producer-stopped"



def _logged_producer_stop(project: str) -> str | None:
    """Read the producer's final reason without loading its long service log."""
    from reckon.crew import runs

    try:
        with runs.watch_log_path(project).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.strip():
            marker = "reckon crew watch stopped: "
            candidate = line.strip()
            if candidate.startswith("[") and "] " in candidate:
                candidate = candidate.split("] ", 1)[1]
            return candidate if candidate.startswith(marker) else None
    return None


def _read_seat_host(project: str) -> tuple[str, bool]:
    """The host a project's held seat names, and whether it is a foreign one.

    A seat record that names no host -- erased, or written before the field
    existed -- is read exactly as a seat on this host always was, so the remedy
    is unchanged for it. One reader for the two facts a follower needs about the
    seat's host, so the one-line overrun choice and the advice it accompanies
    never resolve the same seat differently.
    """
    from reckon.crew import runs

    try:
        with runs.watch_lock_path(project).open("rb") as handle:
            record = runs._read_watch_record(handle)
    except OSError:
        return "", False
    return runs._seat_host(record), runs._seat_names_a_foreign_host(record)


def _producer_seat_is_foreign(project: str) -> bool:
    """Whether a project's held seat names a host other than this one.

    The one fact that chooses an overrun's one-line pane: a foreign seat's
    reloading note is withheld because its host-specific advice already carries
    the news in the same line, while a local seat draws both the reloading note
    and the cycle command. It is read from the seat record rather than from the
    shape of the advice dict, so a later change that adds a host field to the
    local branch of the advice cannot collapse the local remedy line.
    """
    return _read_seat_host(project)[1]


def _ticker_layout_signature() -> str:
    """Fingerprint the ticker's fixed grid geometry, not the code that emits rows.

    A follower reloads onto new code many times an hour on a busy branch, and
    the format marker exists to tell a reader that the rows below it were drawn
    differently from the rows above. Two images that lay a row out in the same
    columns drew it identically, so there is nothing to mark: the marker is
    owed only when the replacement image's grid differs from the one on screen.
    The comparison keys on what the grid fixes -- the ordered widths of the
    fixed cells, the gutters between them, and the minimum width they compose --
    which is what moves when a column is added, resized or reordered, and is
    unchanged when only the rendering of a cell's contents changes. The column
    order is fingerprinted beside the widths, so a reorder that keeps every
    width still marks the format change.
    """
    from reckon.crew import ticker as ticker_module

    layout = (
        ticker_module.COLUMN_ORDER,
        ticker_module.CLOCK,
        ticker_module.ROLE,
        ticker_module.NODE,
        ticker_module.STATE,
        ticker_module.SPEND,
        ticker_module.STATS,
        ticker_module.RATE,
        ticker_module.MODEL,
        ticker_module.EFFORT,
        ticker_module.GAP,
        ticker_module.SPEND_GAP,
        ticker_module.MIN_WIDTH,
    )
    return hashlib.sha256(repr(layout).encode()).hexdigest()[:16]



def _needs_you_runs(project: str, *, session: str | None) -> list[dict[str, Any]]:
    """List the owning session's runs whose state needs the coordinator now.

    The list itself is :func:`reckon.crew.needs_you.needs_you`'s, so the row the
    pane prints at the end of an arming names exactly the runs the standing line
    names while it lives. Read at the moment the follower ends rather than
    accumulated from the stream: a run falls into a needs-action state whether or
    not a producer ever wrote a transition for it, and a run that finished while
    no producer was up would otherwise be named by nobody.
    """
    from reckon.crew import needs_you as needs_you_module

    return needs_you_module.needs_you_runs(project, session=session)



def _population_has_live_work(project: str, *, session: str | None) -> bool:
    """Whether this follower's own fleet still holds a run that has not ended.

    The seat a follower reads is a shared producer that exits when nothing
    renews its ten-minute lease, so its going is the designed idle exit rather
    than an outage whenever the follower's population — its session's runs, or
    the project's when unscoped — holds nothing left to watch. Only a run that
    has not reached a terminal phase is work still owed the pane at that
    moment; a pointer already terminal waits on the coordinator's promotion
    rather than on a producer, and the dispatch guard names an absent producer
    with its remedy to the session that is trying to dispatch.
    """
    from reckon.crew import node as node_module
    from reckon.crew import runs as runs_module

    for pointer in runs_module._list_live_records(project=project):
        owner = str(pointer.get("session") or "")
        # A pointer with no recorded owner stays in the counted set, matching
        # the dispatch fence: absence cannot prove it belongs to a peer.
        if session is not None and owner and owner != session:
            continue
        if str(pointer.get("phase") or "") in node_module._TERMINAL_RUN_PHASES:
            continue
        return True
    return False



def _session_host_child(project: str, session: str | None) -> bool:
    """Whether this follower is a child the session host runs for the pair.

    The host publishes a census record naming every child it started, and the
    live follower for a project and session carries the pid the census lists.
    The reader for that census already exists -- ``_session_host_runs_follower``
    in the dispatch watch module, which dispatch and the dry run call -- so this
    hands it this process's own pid rather than reading the record a second time.
    A follower the census names is one the host already consumes: it never hands
    its session back to the host, because that would ask the host for a follower
    it is already running. A follower the census does not name is one a
    coordinator armed by hand, and only that one hands over.
    """
    if not session:
        return False
    from reckon.crew.dispatch_watch import _session_host_runs_follower

    return _session_host_runs_follower(project, session, os.getpid())


def _host_handoff_line(project: str, session: str | None) -> str:
    """The one line a hand-armed follower prints as it hands its session over.

    It states the fact the reader acts on -- the session host now delivers this
    session, so an operator reading a pane that ended early is not left to work
    out why -- and it says nothing needs re-arming, so the arm-on-exit reflex the
    old end row invited does not fire against a host that is delivering.
    """
    target = f"{project}/{session}" if session else project
    return f"session host now delivers {target}; nothing needs re-arming"


def _hand_off_from_follower(project: str, session: str | None) -> bool:
    """Hand this hand-armed follower's session to a waiting host, if one waits.

    A follower the host owns stays put: the host delivers the session and would
    only be asked for a follower it runs. Otherwise a host waiting on its FIFO
    takes the pair over once this follower is gone, so the request is written
    here and the caller prints the handoff line rather than a re-arm command.
    """
    from reckon.crew.dispatch_watch import (
        _hand_off_to_waiting_host,
        _session_host_may_be_waiting,
    )

    # Cheap gate first: with no host FIFO under this Claude process there is
    # nothing to ask and nothing to read, and resolving the owner to find that
    # out would delay the first delivered row by a transcript lookup for every
    # follower. Only a possible host pays for the census read and the wait.
    if not _session_host_may_be_waiting():
        return False
    if _session_host_child(project, session):
        return False
    return _hand_off_to_waiting_host(project, session)


def _follower_end_line(
    *,
    attach_line: str,
    needs: list[Mapping[str, str]],
    handed_to_host: bool = False,
) -> str:
    """Compose the single line a reader acts on as the pane goes dark.

    When the arming handed its session to a waiting host, the line says so and
    carries no ``re-arm with``: the host runs the session's next follower, so a
    re-arm command would arm a second follower beside one the host consumes.
    """
    if not needs:
        tail = "no runs need you"
    else:
        outstanding = ", ".join(f"{row['run_id']} ({row['node']})" for row in needs)
        if len(needs) == 1:
            tail = f"1 run needs you: {outstanding}"
        else:
            tail = f"{len(needs)} runs need you: {outstanding}"
    if handed_to_host:
        head = (
            "follower end: arming lifetime elapsed; session host now delivers "
            "this session; nothing needs re-arming"
        )
    else:
        head = f"follower end: arming lifetime elapsed; re-arm with: {attach_line}"
    return f"{head}; {tail}"


def _follower_end_event(
    project: str,
    *,
    session: str | None,
    armed_seconds: float | None,
    elapsed: float,
) -> dict[str, Any]:
    """Build the follower's own final transition, carrying the line to print.

    A hand-armed follower reaching its deadline hands its session to a waiting
    host before it composes the line, so the row a reader acts on says the host
    now delivers rather than naming a re-arm command.
    """
    from reckon.crew import runs as runs_module

    handed = _hand_off_from_follower(project, session)
    attach_line = runs_module._watch_attach_line(project, session=session)
    needs = _needs_you_runs(project, session=session)
    return {
        "event": FOLLOWER_END_EVENT,
        "project": project,
        "session": session or "",
        "run_id": None,
        "armed_seconds": armed_seconds,
        "elapsed_seconds": round(float(elapsed), 3),
        "attach_line": attach_line,
        "needs_you": needs,
        "handed_to_host": handed,
        "line": _follower_end_line(
            attach_line=attach_line, needs=needs, handed_to_host=handed
        ),
    }



def _follower_settle_line(
    *, since: float, observed: Iterable[str] = (), tail: str = ""
) -> str:
    """The line the pane shows when a session drains, or nothing at all.

    A line the pane prints names something the coordinator can act on. A settle
    whose fleet has drained names nothing to act on, so the composed line is
    empty: the pane shows nothing, while the follower still exits with its
    settle code so its session host retires the child as it does today.

    The tail is composed by :func:`reckon.crew.needs_you.drain_tail`, the same
    derivation the standing needs-you line reads: it names the runs the session
    still owes, and is empty when the fleet has drained or when that line
    already named the same set. With no tail there is no line.

    When it does print, the stamp is the wall clock the reader's own tools
    print, taken at the moment the session's last live run was seen, so
    ``since`` names when the fleet actually drained rather than when this pane
    happened to notice. A session at a terminal already knows its own id, so the
    bare stamp is the whole of the news; the line names a session only when this
    pane carries more than its own — the ones it was asked to observe — because
    then the reader has more than one fleet in view.
    """
    if not tail:
        return ""
    stamp = time.strftime("%H:%M", time.localtime(since))
    names = ", ".join(sorted({str(name) for name in observed if name}))
    head = f"no live work since {stamp}"
    if names:
        head = f"no live work in {names} since {stamp}"
    return f"{head} · {tail}"


def _follower_settle_event(
    project: str,
    *,
    session: str | None,
    since: float,
    shown: tuple[Any, ...] | None = None,
    observed: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the follower's terminal row for a session whose work drained.

    Like the lifetime end row it is about the follower rather than the fleet,
    so it carries the line to print verbatim. The line is empty when the fleet
    drained with nothing outstanding: the row is still emitted, because the
    follower leaves with its settle code either way, and the reader keys on the
    event rather than the line. ``shown`` is the signature of the set the
    pane's last needs-you line printed, which the tail compares against so the
    row repeats nothing that line already said.
    """
    from reckon.crew import needs_you as needs_you_module

    return {
        "event": FOLLOWER_SETTLE_EVENT,
        "project": project,
        "session": session or "",
        "run_id": None,
        "line": _follower_settle_line(
            since=since,
            observed=observed,
            tail=needs_you_module.drain_tail(project, session=session, shown=shown),
        ),
    }


def _follow_resume_plan(
    project: str,
    session: str | None,
    *,
    stream_path: Path,
    resume_state: Mapping[str, Any],
) -> tuple[str, int, dict[str, str], tuple[str, ...]]:
    """Decide how a follower starts against the stream it is about to read.

    Three modes, one per kind of place an arming can begin from:

    ``baseline`` — a first attachment, which emits the fleet report as it is
    derived. ``continue`` — the stream has only advanced since the recorded
    place, so the recorded offset is the boundary to seek to and nothing before
    it is replayed. ``restart`` — the stream was replaced or truncated, so its
    recorded offset no longer names a boundary: the file is re-read from its
    start and filtered to the runs whose state differs from the checkpoint's.

    A checkpoint whose place is older than the pane's own history window is
    treated as no place at all and resumes in ``baseline``: the rows written
    since it are a margin the pane would not have shown, because the history
    replay beside it is capped at that same window, and replaying them draws
    days of transitions the reader never had on screen. The arming then reports
    each run's current state from live run state, and a run that has not moved
    is not announced again.

    The recorded state travels back with the mode either way, so a continuation
    delivers only what changed rather than re-announcing the fleet. The lift
    groups the pane already drew travel back with it, so a continuation holds
    the producer's re-arm baseline for a lift the pane already shows.
    """
    from reckon.crew import follow_checkpoint

    if resume_state:
        # A checkpoint handed across an in-place reload: the same image is
        # continuing, so the stream path it recorded still governs.
        offset = resume_state.get("offset")
        recorded = {
            str(run_id): str(state)
            for run_id, state in dict(resume_state.get("reported") or {}).items()
        }
        carried_lifts = tuple(
            str(group) for group in (resume_state.get("lift_groups") or ()) if group
        )
        if (
            isinstance(offset, int)
            and not isinstance(offset, bool)
            and resume_state.get("stream_path") == str(stream_path)
        ):
            return "continue", max(0, offset), recorded, carried_lifts
        return "baseline", 0, recorded, carried_lifts

    if session is None:
        # A session-less follower observes the whole fleet in one pass and has
        # no pane to restore, so it has no durable place. Reading one would find
        # a place a concurrent session-less follower wrote, resume mid-stream,
        # and deliver a resume event and rows the pane already showed.
        return "baseline", 0, {}, ()

    record = follow_checkpoint.read(project, session)
    if not record:
        return "baseline", 0, {}, ()
    recorded = {
        str(run_id): str(state)
        for run_id, state in dict(record.get("reported") or {}).items()
    }
    recorded_lifts = tuple(
        str(group) for group in (record.get("lift_groups") or ()) if group
    )
    if follow_checkpoint.continues(record, stream_path):
        if _follow_checkpoint_stale(project, session, record):
            # The recorded place is older than the pane's history window, so the
            # rows written since it are not a margin the pane would have shown:
            # the arming resumes as a first attachment does, reporting each run's
            # current state from live run state rather than reading that margin.
            return "baseline", 0, recorded, recorded_lifts
        return "continue", max(0, int(record["offset"])), recorded, recorded_lifts
    return "restart", 0, recorded, recorded_lifts


def _follow_checkpoint_stale(
    project: str,
    session: str | None,
    record: Mapping[str, Any],
    *,
    now: float | None = None,
) -> bool:
    """Whether a checkpoint's place is older than the pane's history window.

    The pane's history replay is capped at that window, so a margin older than
    it is not a margin the pane would otherwise show and replaying it draws days
    of rows the reader never had on screen. The window is read through the same
    reader the history burst resolves it with, so the margin replay and the
    history replay cannot disagree about where the window falls. A record whose
    moment cannot be read is not treated as stale, because an age that cannot be
    established is not evidence of one.
    """
    from reckon.crew import follow_checkpoint

    _, window = _follow_history_caps(project)
    moment = follow_checkpoint.written_moment(project, session, record)
    if moment is None:
        return False
    current = time.time() if now is None else now
    return (current - moment) > window



# The fixed age past which a lift claim is stale. A claim is normally removed
# when its lift ends; this bounds one whose end row never reached the pane, so a
# later grant of the same lift id is not blocked forever.
LIFT_CLAIM_MAX_AGE_SECONDS = 24 * 60 * 60.0


def lift_claim_key(session: str | None) -> str:
    """The key one follower's lift claims are filed under, one pane at a time.

    A session host runs every follower of one pane, and it may follow more than
    one crew session — a coordinator dispatching under two session names, say.
    Keying the claim on the session therefore draws each lift once per followed
    session. Only ``SessionHost._spawn`` stamps ``RECKON_SESSION_HOST_SETTLE``
    into a child's environment, and a reload's exec carries it through unchanged,
    so that variable's presence — never ``RECKON_FOLLOWER_OWNER``, which the
    follower's own reloader writes into its replacement unconditionally — is the
    signal that selects the host key. An unhosted follower — a hand-armed one, or
    one that has reloaded without ever having had a host — therefore keeps the
    per-session key before and after any number of reloads.

    The host key names the node the host runs on as well as the owner pair the
    host stamped, so a sweep can tell a host that died here from one alive on
    another node that shares this home. A follower with no host, or one whose
    owner stamp cannot be read, keeps the per-session key it used before, so a
    pane is never left with no claim at all.
    """
    from reckon.crew import runs

    if not os.environ.get(SETTLE_ENV):
        return str(session or "")
    owner = os.environ.get(runs._FOLLOWER_OWNER_ENV)
    if not owner:
        return str(session or "")
    node = _lift_claim_token(socket.gethostname())
    parsed = runs._parse_follower_owner(owner)
    if parsed is None:
        return f"host-{node}-{_lift_claim_token(owner)}"
    pid, start_time = parsed
    return f"host-{node}-{pid}-{start_time or 'unknown'}"


def lift_claim_dir(session: str | None) -> Path:
    """The directory holding one pane's lift claims, shared by its followers.

    One pane is one session host, and every follower that host runs writes that
    one pane, so the claims live under the pane's key beneath the crew home's
    watch directory. The key is the host that spawned the followers when they
    have one, and the session otherwise, so a host following two sessions draws
    each lift once rather than once per session. An exclusive create there is the
    arbitration every follower of the pane shares, whether they run in one
    process or several on the same shared home.
    """
    from reckon.crew.runs import crew_home

    name = re.sub(r"[^A-Za-z0-9._-]", "-", lift_claim_key(session)).strip("-")
    return crew_home() / "watch" / "lifts" / (name or "pane")


def _lift_claim_token(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(text)).strip("-") or "lift"


def _lift_claim_path(session: str | None, lift_id: str, transition: str) -> Path:
    return lift_claim_dir(session) / f"{_lift_claim_token(lift_id)}__{transition}"


def _lift_claimed(session: str | None, lift_id: str) -> bool:
    """Whether any transition of this lift is already claimed for the pane."""
    directory = lift_claim_dir(session)
    if not directory.is_dir():
        return False
    return next(directory.glob(f"{_lift_claim_token(lift_id)}__*"), None) is not None


def _prune_lift_claims(session: str | None, lift_id: str) -> None:
    """Remove every claim for one lift id, so the directory keeps no standing one.

    The pane's own claims are removed wherever this pass runs, and the pass also
    sweeps the claim directories hosts that have ended left behind — see
    :func:`_prune_abandoned_lift_claim_dirs` — so a host's directory does not
    outlive the host for as long as a lift stays in force.
    """
    directory = lift_claim_dir(session)
    for stale in directory.glob(f"{_lift_claim_token(lift_id)}__*"):
        with suppress(OSError):
            stale.unlink()
    _prune_abandoned_lift_claim_dirs()


# A claim directory keyed on a host cannot be checked for liveness once the key
# stops naming the node the owner pair is meaningful on, so such a directory is
# removed only once nothing has touched it for a week. A host that has ended on
# this node leaves one directory per pane it served, and nothing else removes
# them: the per-lift prune takes only the files inside a pane's own directory.
LIFT_CLAIM_DIR_MAX_AGE_SECONDS = 7 * 24 * 60 * 60.0

# A host key names the node its host ran on and then the owner pair, so a
# directory reads ``host-<node>-<pid>-<start>`` -- the reading
# :func:`lift_claim_key` writes. A node name may itself carry dashes, so the pid
# and the start tick are taken from the right, where each is a run of digits and
# nothing else is: the greedy node group takes every dash the node's own name
# holds before the two trailing numeric runs. A node-less directory, from the
# layout before the node entered the key, carries the owner pair alone.
_HOST_CLAIM_DIR = re.compile(r"^host-(?P<node>.+)-(?P<pid>\d+)-(?P<start>\d+)$")
_HOST_CLAIM_DIR_NODELESS = re.compile(r"^host-(?P<pid>\d+)-(?P<start>\d+)$")


def _prune_abandoned_lift_claim_dirs(*, now: float | None = None) -> list[Path]:
    """Remove the lift-claim directories of hosts that have ended on this node.

    A pane's claims live under ``crew_home/watch/lifts`` in a directory keyed on
    the host that spawned the followers, one directory per session-host lifetime.
    Nothing else removes them: the per-lift prune empties the directory of the
    pane that is running, and a host's directory is never that pane's. So a host
    that ends leaves its directory standing for every pane it served.

    Liveness is the host's pid *and* its kernel start tick, exactly as
    :func:`reckon.crew.session_host.owner_alive` decides it, so a pid the node has
    since reused does not keep a dead host's directory alive. Only a directory
    whose key names this node is judged that way: a pid means nothing on any
    other node, and a directory naming another node is never removed here. A
    directory in the node-less layout that predates the node entering the key,
    ``host-<pid>-<start>``, names no node at all, so its owner pair cannot be
    placed on this node or another; it is removed only once nothing has touched
    it for :data:`LIFT_CLAIM_DIR_MAX_AGE_SECONDS`. A host key this node cannot
    read as either layout is left alone, since it may name another node's host.

    Per-session directories carry no ``host-`` prefix, so they are skipped: a
    hand-armed follower's claim is that pane's live state, not an abandoned
    host's. The directory removed is returned, so a caller or a test can see
    what the pass took.
    """
    from reckon.crew.runs import crew_home

    root = crew_home() / "watch" / "lifts"
    if not root.is_dir():
        return []
    node = _lift_claim_token(socket.gethostname())
    moment = time.time() if now is None else float(now)
    removed: list[Path] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if not name.startswith("host-") or not entry.is_dir():
            continue
        keyed = _HOST_CLAIM_DIR.match(name)
        if keyed is None:
            if _HOST_CLAIM_DIR_NODELESS.match(name) is None:
                # A host key naming no node this node can read may name another
                # node's host, whose pid means nothing here, so it is left alone
                # rather than guessed at.
                continue
            # Node-less: no node to judge the owner pair on, so only the age
            # bound decides it.
            try:
                age = moment - entry.stat().st_mtime
            except OSError:
                continue
            if age < LIFT_CLAIM_DIR_MAX_AGE_SECONDS:
                continue
        elif keyed.group("node") != node:
            # Another node's host: its pid means nothing here, so nothing on this
            # node can decide that its host has ended.
            continue
        elif owner_alive(
            {"pid": int(keyed.group("pid")), "start_time": keyed.group("start")}
        ):
            continue
        removed.append(entry)
    for entry in removed:
        with suppress(OSError):
            shutil.rmtree(entry)
    return [entry for entry in removed if not entry.exists()]


def take_lift_claim(
    session: str | None, lift_id: str, transition: str, *, now: float | None = None
) -> bool:
    """Whether this follower should draw one lift transition on the pane.

    Every project's producer sweeps the workstation's whole lift document into
    its own event stream, so the same grant reaches a session's pane once per
    project the session follows. The followers of one pane share one claim
    per (pane, lift id, transition) — a file created with an exclusive create,
    which the shared home makes atomic across processes and nodes. The first
    follower to reach the row creates the claim and draws it; the rest find it
    taken and draw nothing. A claim is taken when the row would print,
    never earlier, so no follower claims and then withholds.

    A baseline in-force row is inventory of a lift already on the pane, so it
    draws nothing when any claim for the lift already exists — a peer follower
    has shown it. A stale claim, older than :data:`LIFT_CLAIM_MAX_AGE_SECONDS`,
    is cleared and retaken, so a lift whose end row never reached the pane does
    not block a later grant. When the end row is drawn the lift's other claims
    are pruned, so the directory keeps no standing claim for the lift id.

    A claim that cannot be written at all leaves the row drawn rather than
    dropped: a pane that stops showing a live grant is worse than a repeated row.
    """
    from reckon.crew.ticker import LIFT_ENDED, LIFT_IN_FORCE

    moment = time.time() if now is None else float(now)
    if transition == LIFT_IN_FORCE and _lift_claimed(session, lift_id):
        return False
    path = _lift_claim_path(session, lift_id, transition)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = moment - path.stat().st_mtime
            except OSError:
                return False
            if age < LIFT_CLAIM_MAX_AGE_SECONDS:
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return True
        else:
            os.close(descriptor)
            if transition == LIFT_ENDED:
                _prune_lift_claims(session, lift_id)
            return True
    return False


def follower_row_path(
    *,
    session: str | None = None,
    observed: Iterable[str] = (),
    run_ids: Iterable[str] = (),
    reported: Mapping[str, str] | None = None,
    lift_groups: Iterable[str] = (),
    lift_claim: Callable[[str, str], bool] | None = None,
):
    """The row path a follower runs on, for a reader that has to measure one.

    A replay that drove the policy directly would count rows the follower never
    offered it. This builds the very object the follower feeds — the same
    selection function, the same policy, the same memory — so a measurement
    taken through it is the pane's own answer rather than a second opinion.

    The pane's lift claim is keyed on the host that spawned the follower when it
    has one and on its own session otherwise, unless a caller supplies its own.
    Either way a live follower draws each lift transition once however many
    projects it follows, and a host's followers draw it once between them however
    many sessions the host serves. A caller that supplies ``lift_claim`` replaces
    it outright; a caller measuring lift rows without a shared home passes one
    that always permits.
    """
    from reckon.crew import ticker as ticker_module

    observed_sessions = frozenset(observed)
    selected_runs = tuple(run_ids)
    if lift_claim is None:
        lift_claim = lambda lift_id, transition: take_lift_claim(  # noqa: E731
            session, lift_id, transition
        )
    return ticker_module.PaneRowPath(
        selects=lambda event: _follow_selects(
            event, session=session, observed=observed_sessions, run_ids=selected_runs
        ),
        reported=reported,
        # The pane's own session, so the row policy delivers this session's own
        # run starts. Without it the policy holds every start as observer
        # context, and a live follower shows nothing between a run's launch and
        # its end — the silence this pane's second reader cannot read.
        own_session=session,
        # The sessions this pane was asked to observe. Observing a session is a
        # request to see its rows, so the policy must know which sessions those
        # are; without this the observed session's rows are held back as observer
        # context and an observing follower shows nothing of the session it was
        # armed to watch.
        observed_sessions=observed_sessions,
        # The lift groups the pane already drew, carried in the checkpoint a
        # reload hands across, so the memory of them starts where the previous
        # image left it rather than empty.
        lift_groups=lift_groups,
        # The pane's shared lift claim: the session's followers draw each lift
        # transition once between them, and a live follower bound to its session
        # draws the grant once no matter how many projects it follows.
        lift_claim=lift_claim,
    )



def _follow_boundary(stream_path: Path) -> int:
    """The byte after the last complete record when the boundary is taken.

    The producer appends one JSON record per line, so a line boundary is a
    record boundary. The size captured from the file can fall inside a record
    the producer is still writing, and a boundary taken there would leave the
    replay reading a half-written line it cannot parse and the follow loop
    opening in the middle of that record, so the record would be delivered in
    halves or not at all. So the guard snaps the captured stream offset back to
    the byte after the last newline at or before it: the boundary always lands
    on a newline, the replay reads only whole records, and the loop opens at the
    start of any record still being written, which it then reads whole and once.
    """
    from reckon.crew import runs

    return runs.line_boundary(stream_path)



def _stream_events_upto(stream_path: Path, *, offset: int, boundary: int) -> list[dict]:
    """Read a stream's events from one byte offset up to a fixed boundary.

    The boundary is captured before the read rather than taken from the file
    afterwards, so a line appended while the read is in progress belongs to the
    read that follows rather than to this one. That is what makes a transition
    written between the replay and the read loop arrive exactly once: the replay
    stops at the boundary, and the loop opens at the same boundary.
    """
    from reckon.crew import runs as runs_module

    events: list[dict] = []
    if boundary <= offset:
        return events
    try:
        stream = Path(stream_path).open(encoding="utf-8")
    except OSError:
        return events
    with stream:
        stream.seek(offset)
        while stream.tell() < boundary:
            line = stream.readline()
            if not line:
                break
            if stream.tell() > boundary:
                # A line that runs past the boundary is not this read's to
                # deliver: the reader that follows opens at the boundary and
                # reads it whole, so it arrives once rather than in halves.
                break
            event = runs_module.parse_stream_line(line)
            if event is not None:
                events.append(event)
    return events



def _recorded_fleet_times(
    stream_path: Path, *, boundary: int
) -> dict[str, tuple[str, str]]:
    """The state each run is in, and the time it entered that state.

    A row that re-announces a run should carry the clock the run's own record
    gave it rather than the moment the pane happened to attach, so the time is
    read from the stream the producer wrote. A run's recorded time is the first
    event in the unbroken trailing run of events whose ``to_state`` equals that
    state, read as the earliest stamp that the run carries for the state: a
    re-emitted baseline names a state the run already sits in and is stamped
    when it is emitted, so it extends the run without moving the time, while a
    genuine change of state opens a new run and stamps it at the change. The
    read stops at the boundary the replay was given so the answer describes the
    same span the rows do.
    """
    latest: dict[str, tuple[str, str]] = {}
    for event in _stream_events_upto(stream_path, offset=0, boundary=boundary):
        if event.get("legacy"):
            continue
        run_id = str(event.get("run_id") or "")
        to_state = event.get("to_state")
        if not run_id or not to_state:
            continue
        state = str(to_state)
        stamp = str(event.get("observed_at") or "")
        recorded = latest.get(run_id)
        if recorded is not None and recorded[1] == state:
            # The same state again: this event continues the trailing run, so
            # the run's recorded time stays the earlier stamp it already holds.
            held = parse_utc(recorded[0])
            moment = parse_utc(stamp)
            if moment is not None and (held is None or moment < held):
                latest[run_id] = (stamp, state)
            continue
        latest[run_id] = (stamp, state)
    return latest



def _fleet_replay(
    path,
    *,
    baseline,
    stream_path: Path,
    clock,
    reannounce: bool,
    resume_from: int,
    boundary: int,
) -> list[dict]:
    """One row per live run, under its own recorded clock and in time order.

    The rows come from two places and neither may deliver a run twice. A run
    whose record moved between the place this arming resumes from and the
    boundary the replay read to is news: the transitions in that span are handed
    to the row path in recorded order, so the policy that holds a noise pair,
    resolves one silently, or withholds a row until its window closes applies to
    a gap exactly as it does to a live read. A run with no news of its own in
    that span is re-announced instead — one row carrying the state the live
    fleet gives it, stamped with the time its own record entered that state.

    The recorded time is taken only where the stream's last word for the run
    agrees with the live classification. Where they disagree the live fleet is
    the authority, because it is the current pointer while the stream is
    history, so the row keeps the live state and the fleet's own clock rather
    than a state the live classification contradicts: a row never announces a
    state the fleet denies.

    The rows are ordered by their own stamp, so a re-arm reads as a timeline and
    an old transition leads a run that has sat at its state since.

    Only an arming that is re-announcing the fleet marks its rows
    ``reannounced``: the stored history is the pane's record of what it was
    handed, so a first arming that draws the fleet records it, while a re-arm
    that draws the same fleet leaves the history it restores alone rather than
    doubling it. A gap transition is news and is recorded.
    """
    from reckon.crew import ticker as ticker_module

    recorded = _recorded_fleet_times(stream_path, boundary=boundary)
    rows: list[dict] = []
    announced: set[str] = set()
    for event in _stream_events_upto(
        stream_path, offset=resume_from, boundary=boundary
    ):
        run_id = str(event.get("run_id") or "")
        if run_id:
            announced.add(run_id)
        rows.append(dict(event))
    for event in baseline:
        row = dict(event)
        run_id = str(row.get("run_id") or "")
        if run_id and run_id in announced:
            continue
        latest = recorded.get(run_id)
        if latest is not None:
            stamp, state = latest
            if stamp and state == str(row.get("to_state") or ""):
                row["observed_at"] = stamp
        if reannounce:
            row["reannounced"] = True
        rows.append(row)
    rows.sort(key=_follow_row_stamp)
    printed: list[dict] = []
    # What the pane holds for each run: the word it draws as the left half,
    # paired with the duty it was told beside that word, or None where only the
    # word survived the handover. A replayed baseline is judged against it, so a
    # row re-announcing both is inventory rather than a self-transition, while a
    # first sighting, a real move, and a replay naming the word with a different
    # duty beside it still print.
    shown: dict[str, tuple[str, tuple | None]] = {
        str(run_id): (str(word), None)
        for run_id, word in dict(path.reported).items()
    }
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if ticker_module.is_baseline(row) and _replayed_row_repeats_the_pane(
            row, shown
        ):
            continue
        printed.extend(path.feed(row, now=clock()))
        # The pane's memory is what a later replay is judged against, so it
        # follows every row this replay delivers rather than only the ones drawn
        # as transitions: a row the policy holds for its settle window is still
        # the word the pane will show, and the path's own map lags it until the
        # window passes.
        if run_id:
            shown[run_id] = _pane_shown_memory(row)
            for other, word in dict(path.reported).items():
                shown.setdefault(str(other), (str(word), None))
    return printed


def _pane_shown_memory(row: Mapping[str, Any]) -> tuple[str, tuple]:
    """The word a row puts on the pane, with the duty the pane is shown beside it.

    The duty is exactly the evidence :func:`reckon.crew.ticker.same_word_row_is_news`
    reads — the recovery classification the fleet calls actionable, and the
    external condition the run declares it is waiting on — so a replay is
    compared on the same terms the live row path uses to keep a same-word row.
    """
    return (
        str(row.get("to_state") or ""),
        (
            str(row.get("recovery_classification") or ""),
            row.get("wait_condition_state"),
            row.get("wait_overdue"),
            row.get("expected_horizon_seconds"),
        ),
    )


def _replayed_row_repeats_the_pane(
    row: Mapping[str, Any], shown: Mapping[str, tuple[str, tuple | None]]
) -> bool:
    """Whether a replayed row re-announces what the pane already holds for its run.

    A producer reload replays every live run as a fresh baseline row, so each
    run's row returns after every reload. Where the replay names the word the
    pane already draws and carries the same duty beside it the row is inventory:
    the grid takes the row's left half from the pane's memory, so it renders as
    a self-transition carrying only a growing elapsed. The duty is compared with
    the word because the live row path keeps a same-word row whose declaration
    the coordinator must be able to read, so a replay naming a changed condition
    is news and is left to the row path — as are a run the pane has not drawn
    and a move to another word.
    """
    run_id = str(row.get("run_id") or "")
    if not run_id:
        return False
    remembered = shown.get(run_id)
    if remembered is None:
        return False
    word, duty = remembered
    if str(row.get("to_state") or "") != word:
        return False
    return duty is None or duty == _pane_shown_memory(row)[1]


_FOLLOW_REATTACH_FRAME = (
    "── re-attached · {live} live runs · {changed} changed since {when} ──"
)


def _follow_reattach_line(live: int, changed: int, recorded_at: str) -> str:
    """Compose the framing line of a re-attach, stamped with the record's time."""
    from reckon.crew import ticker as ticker_module

    return _FOLLOW_REATTACH_FRAME.format(
        live=live, changed=changed, when=ticker_module.local_clock(recorded_at)
    )


def _follow_gap_rows(
    baseline: Iterable[Mapping[str, Any]],
    record: Mapping[str, Any],
    *,
    session: str | None,
    observed: Iterable[str],
    run_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    """The rows a re-attach replays: one per owned run whose state moved.

    The record holds the state each of the session's runs was last shown at, so
    a run still at the state the record names carries no news and draws nothing:
    the gap between two armings is replayed rather than a whole fleet
    re-announced. A run the record does not name was dispatched while nothing
    was attached, so it is replayed from ``dispatched``, the state every launch
    passes through. Each row keeps the remembered state as its left half, so it
    reads as the ordinary transition the run made while the pane was away, and
    takes its right half and its clock from the live classification rather than
    from any stored phase.
    """
    states = {
        str(run_id): str(state)
        for run_id, state in dict(record.get("states") or {}).items()
    }
    rows: list[dict[str, Any]] = []
    for row in baseline:
        if not _follow_selects(
            row, session=session, observed=observed, run_ids=run_ids
        ):
            continue
        run_id = str(row.get("run_id") or "")
        event = dict(row)
        recorded = states.get(run_id)
        # The record names the word alone, so a run still at it repeats what the
        # pane holds and draws nothing; the duty beside the word was not
        # recorded and is not compared here.
        if _replayed_row_repeats_the_pane(
            event, {run_id: (recorded, None)} if recorded is not None else {}
        ):
            continue
        event["from_state"] = recorded or "dispatched"
        rows.append(event)
    return rows


def _follow_watch_lines(
    project: str,
    *,
    session: str | None = None,
    observed: Iterable[str] = (),
    run_ids: Iterable[str] = (),
    poll_interval: float = 0.1,
    sleeper=time.sleep,
    stop=None,
    on_poll=None,
    sweep=_sweep_lapsed_holds,
    sweep_interval: float | None = None,
    clock=time.monotonic,
    resume: Mapping[str, Any] | None = None,
    lifetime: float | None = None,
    lifetime_deadline: float | None = None,
    registration=None,
    producer_reload_window: float | None = None,
    reloaded_in_place: bool = False,
    settle: bool = False,
    settle_interval: float | None = None,
    remind: float | None = None,
    subscriber=None,
):
    """Yield this follower's transitions for as long as its session lives.

    Two properties matter more than anything else here, and both were learned
    from measured silence:

    A follower outlives the producer it reads. The seat is released when a wave
    drains, so a follower that ended there covered exactly one wave — the next
    wave armed a fresh producer while the session's monitor had already exited.
    Waiting for the next producer instead makes one arming cover the session.

    A follower replays the fleet, and a re-arm's gap is news. Every arming of a
    fresh image — a first attach and a re-arm alike — draws one row for each
    live run, stamped with the recorded time that run entered its current state
    and ordered by it. A reader that re-arms therefore sees the whole fleet with
    its own clocks rather than a blank pane, and never a burst of rows sharing
    the moment the arming attached.

    A re-arm did have a place in the stream, and what the stream gained between
    that place and the arming's own start is delivered as news ahead of the
    fleet: those transitions go through the row path in recorded order, so the
    same policy that governs a live read governs them — a noise pair still
    prints nothing, a held row is still held — and a run that moved while the
    pane was away appears once, in its new state, at its recorded transition
    time, rather than being drawn twice. A first arming, having no place in the
    stream, derives the fleet as it stands. Only an in-place reload, which
    replaces the process image with the grid still on screen, continues from its
    recorded offset and delivers just what moved.

    The byte the replay reads to is captured once, before it reads, and the read
    loop then opens at exactly that byte, so a transition appended in between
    falls to one side or the other and is delivered once.

    ``observed`` names sessions delivered for oversight alongside the owning
    ``session``. They carry no registration: the attachment and its dispatch
    guard stay the owning session's alone, so observing never vouches for
    delivery of the observed session's runs.

    ``lifetime`` bounds the arming itself. The host ends a Monitor at thirty
    minutes and announces it only in its own words, so a follower armed for
    slightly less reaches its own deadline first: it prints one line a reader
    can act on and releases its registration, rather than leaving the pane to
    stop silently. That line is the one deliberate exception to this stream's
    fleet-only vocabulary, because the end of the stream is the one fact about
    the follower a reader must act on.
    """
    from reckon.crew import follow_checkpoint, runs
    from reckon.flight import FlightConfigError

    selected_runs = tuple(run_ids)
    observed_sessions = frozenset(observed)
    resume_state = dict(resume or {})
    # The follower's own row path: the filter that decides which events are
    # this follower's, the policy that holds a row opening a noise pair, and
    # the memory of the states the pane was actually shown. It is the object
    # the checkpoint stores and a re-arm restores, so a row it withholds is not
    # in that memory and a re-arm re-evaluates it instead of believing it was
    # delivered.
    path = follower_row_path(
        session=session,
        observed=observed_sessions,
        run_ids=selected_runs,
        reported=resume_state.get("reported"),
        lift_groups=resume_state.get("lift_groups"),
    )

    started_at = clock()
    if lifetime_deadline is None and lifetime is not None:
        # A caller that hands only a duration is granted it from this arming's
        # own start; a caller that hands the absolute instant in keeps the
        # deadline the arming was granted, which a replacement image must not
        # re-anchor.
        lifetime_deadline = started_at + max(0.0, float(lifetime))
    lifetime_elapsed = False
    consumer_gone = False
    # The settle: a follower a settle-aware host runs ends once no live work
    # remains, printing one idle line. ``saw_live`` is the hinge -- a dispatch
    # asks the host for this follower before it publishes the run's pointer, so
    # a follower that settled on its first empty reading would leave before its
    # own run appeared. A population that stays empty for the grace settles;
    # one that refills inside it, an admitted run above all, does not.
    settled = False
    saw_live = False
    last_live_at: float | None = None
    # When the population was first seen empty, so the grace is measured
    # against the drain rather than against the poll that happened to notice
    # it. Cleared by any sighting of unfinished work.
    settle_empty_since: float | None = None
    settle_sampled_at: float | None = None
    # The sessions whose work this pane carries: its own, plus each one it was
    # asked to observe. A run of any other session is work this follower never
    # prints.
    settle_populations = (session, *sorted(observed_sessions - {None}))

    def _is_live(name: str | None) -> bool:
        return _population_has_live_work(project, session=name)

    # Resolved once per arming: an explicit interval from a caller that drives
    # this generator itself, otherwise the module's grace, which a caller that
    # runs a follower as a child may name through its environment.
    grace = (
        _settle_grace_default() if settle_interval is None else float(settle_interval)
    )

    def _check_settle() -> None:
        """End this arming when the work this pane carries has drained.

        The judgement is ``_population_has_live_work``'s, not one derived here:
        the same call a producer stop is reported against. It is sampled on a
        cadence because a settle costs a live-pointer scan.

        One empty reading does not settle the pane. An admission handover
        leaves the session with no unfinished run for as long as the dispatch
        adopting a queued request takes to build the admitted run's worktree
        and publish its pointer, so the pane ends only once its population has
        held nothing unfinished for the grace. A run
        that appears inside the grace, an admitted run above all, clears the
        emptiness and keeps the pane.
        """
        nonlocal settled, saw_live, last_live_at, settle_empty_since
        nonlocal settle_sampled_at
        if not settle or settled:
            return
        moment = clock()
        # The wait pass is fast, so the population is sampled on a cadence
        # rather than on every pass. The grace bounds that cadence from above,
        # so a caller that shortens the grace shortens the hold and the poll
        # together, while the cap keeps an empty population noticed soon after
        # it drains.
        cadence = min(grace, FOLLOWER_SETTLE_SAMPLE_SECONDS)
        if settle_sampled_at is not None and moment - settle_sampled_at < cadence:
            return
        settle_sampled_at = moment
        # The population is the pane's own session plus the sessions it was
        # asked to observe, and nothing else: a peer session this follower does
        # not observe is work it never prints, so it must not hold the pane
        # open. Each session is asked through the same judgement that owns it.
        if any(_is_live(name) for name in settle_populations):
            saw_live = True
            last_live_at = time.time()
            settle_empty_since = None
            return
        if not saw_live:
            return
        if settle_empty_since is None:
            settle_empty_since = moment
        if moment - settle_empty_since >= grace:
            settled = True

    def _check_consumer() -> None:
        """End this arming when the process it reports to has gone.

        The follower delivers to the session that armed it, and that owner is
        fixed once, at the follower's first start, as a pid plus the kernel
        start time that tells it apart from an unrelated process reusing the
        pid. It is read from the owner identity rather than from
        ``os.getppid()``, because once the arming session dies the parent names
        init or a subreaper; and rather than from the registration record,
        because a second follower that takes the registration over rewrites
        that record from its own parent, so a record read back here could name
        the wrong process. The owner having gone means the lines reach nobody,
        so holding the registration only denies the session its next follower:
        the orphan keeps the advisory lock, and every dispatch from the session
        is refused ``watcher-required`` until someone kills it by hand.

        This runs on every wait pass, whether or not the follower holds the
        registration: a read-only follower holds no lock and still outlives its
        owner, and it is the read-only ones that accumulate, one per re-arm. An
        owner of pid 1 or below has already been re-parented to init or a
        subreaper, so it is gone rather than a reason to skip the check. Nothing
        is printed on this path: it is the one path whose whole point is that no
        reader is left.
        """
        nonlocal consumer_gone
        if consumer_gone:
            return
        owner_pid, owner_start = runs.follower_owner()
        if owner_pid <= 1 or runs.process_alive(owner_pid) is not True:
            consumer_gone = True
            return
        if owner_start and runs._process_start_time(owner_pid) != owner_start:
            consumer_gone = True

    def _check_lifetime() -> None:
        """Mark this arming ended when its own deadline has passed.

        The deadline is the follower's own, so it is read on the wait pass
        rather than on any stream event: a follower with no producer up must
        still reach its deadline, which is exactly the case where a reader
        would otherwise see nothing at all and assume the pane is quiet.
        """
        nonlocal lifetime_elapsed
        if lifetime_deadline is not None and clock() >= lifetime_deadline:
            lifetime_elapsed = True

    def _stopped() -> bool:
        return stop is not None and stop.is_set()

    # The place this arming last wrote, so a poll that changes nothing does not
    # rewrite it. Declared beside the writer that owns it.
    written_place: tuple[Any, ...] | None = None
    # When this follower last wrote its own delivery lease, so the throttling in
    # the renewal below is decided in memory rather than by re-reading the file
    # on every wait pass.
    lease_renewed_at: float | None = None

    def _record_checkpoint(
        stream_path: Path,
        offset: int,
        *,
        identity: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist this follower's place, so its next arming continues here.

        Written as lines are delivered, so the place advances with the stream
        rather than with the arming's end: an arming that dies without reaching
        its own teardown has still left behind everything it delivered. A
        checkpoint that cannot be written costs a later re-arm its place and
        must never cost this arming its stream, so it is not allowed to raise.

        ``identity`` is the open stream's own identity when the offset was read
        from that handle, so a replacement landing between the read and this
        write cannot pair the old offset with the new file's inode.

        A write is performed only when the place has moved — the stream, the
        offset, the stream's identity or the reported map — or when the file
        this place names is gone. The per-tick hook runs on every wait pass, so
        an arming sitting against a quiet stream would otherwise rewrite the
        same record once a pass, two fsyncs and a rename each, for as long as it
        sat there. The existence half is what makes a checkpoint survive a
        deletion: a file removed while the pane sat idle is rewritten on the
        next poll rather than staying absent until the place moves, so a reload
        in that window still finds the pane's memory.
        """
        from reckon.crew import follow_checkpoint

        nonlocal written_place
        if session is None:
            # A session-less follower has no pane to restore, so it keeps no
            # durable place: writing one would hand a concurrent session-less
            # follower a spot to resume from, which is a place it never had.
            return
        try:
            resolved_identity: Mapping[str, Any] | None = (
                identity
                if identity is not None
                else follow_checkpoint.stream_identity(stream_path)
            )
        except OSError:
            resolved_identity = None
        place = (
            str(stream_path),
            int(offset),
            None
            if resolved_identity is None
            else (resolved_identity.get("dev"), resolved_identity.get("ino")),
            tuple(
                sorted((str(key), str(value)) for key, value in path.reported.items())
            ),
            tuple(sorted(path.lift_groups)),
        )
        if place == written_place and follow_checkpoint.exists(project, session):
            return
        try:
            follow_checkpoint.write(
                project,
                session,
                stream_path=stream_path,
                offset=offset,
                reported=path.reported,
                lift_groups=path.lift_groups,
                identity=identity,
            )
        except OSError:
            return
        written_place = place

    def _renew_producer_lease() -> None:
        """Push this live follower's project producer's lease forward.

        A producer ends one lease interval after the last renewal, so a live
        follower must renew repeatedly for as long as it lives — a single
        renewal at attach would let the producer die under a follower that is
        still reading it. The writer throttles itself to half the interval and
        refuses when no producer is live, so this pass never resurrects the
        registration of a producer that has already exited into the lease. A
        failure to write must never end the pane: the renewal is best-effort and
        a missed one merely ages the lease.

        The same pass renews this session's own delivery lease, beside its
        registration. A reader on another host cannot take the registration's
        advisory lock — flock is node-local on a shared home — so the lease is
        the only fact it has by which to judge whether this follower still
        delivers. It is renewed only while this follower holds the registration,
        and throttled to half the producer's interval so a tight poll loop does
        not rewrite it on every tick.
        """
        try:
            runs.renew_producer_lease(project)
        except OSError:
            return
        nonlocal lease_renewed_at
        if session is None or registration is None or not registration.held:
            return
        try:
            moment = time.time()
            if (
                lease_renewed_at is not None
                and moment - lease_renewed_at < runs.producer_lease_seconds() / 2
            ):
                return
            from reckon.crew import follower_registration

            follower_registration.renew_follower_lease(project, session, now=moment)
            lease_renewed_at = moment
        except OSError:
            return

    def _tick(
        *,
        stream_path: Path | None = None,
        offset: int = 0,
        identity: Mapping[str, Any] | None = None,
    ) -> None:
        """Run the caller's per-wait work — reclaiming a registration, say."""
        if on_poll is not None:
            on_poll(
                {
                    "reported": dict(path.reported),
                    "stream_path": str(stream_path) if stream_path else "",
                    "offset": offset,
                    # The lift groups the pane has drawn as in force, carried
                    # to the replacement image beside the states, so a reload
                    # holds the producer's re-arm baseline for a stand the pane
                    # already shows.
                    "lift_groups": sorted(path.lift_groups),
                    # The grid this image draws with, carried to the replacement
                    # so a reload that leaves every column where it was does not
                    # claim the drawing style changed.
                    "layout": _ticker_layout_signature(),
                }
            )
        if stream_path is not None:
            _record_checkpoint(stream_path, offset, identity=identity)
        _check_lifetime()
        _check_consumer()
        _check_settle()
        _renew_producer_lease()

    # The follower prints no standing summary line. Under the session host every
    # printed line is a notification that starts a coordinator turn, and the
    # needs-you summary named the orchestrator rather than the lead, repeated the
    # rows the stream already carries, and was restated by the obligations hook
    # on every prompt; its reminder cadence woke a coordinator to read a list it
    # had just seen. The rule is stated once, in
    # :func:`reckon.crew.ticker.stream_carries`: the stream carries a line only
    # when it names something only the coordinator can do next. The settle row's
    # tail still names the runs a drained session owes, which is the one summary
    # a reader can act on.
    #
    # ``shown`` is what the settle tail compares against. With no standing line
    # to show a set, nothing has been shown, so the tail names every outstanding
    # run rather than suppressing a set a line already named.
    shown_needs_signature: tuple | None = None

    from reckon.crew.resumption import DEFAULT_SWEEP_SECONDS

    cadence = DEFAULT_SWEEP_SECONDS if sweep_interval is None else float(sweep_interval)
    swept_at: float | None = None

    def _sweep_quietly() -> None:
        try:
            # The follower's own sweep takes the session it serves, so a sweep
            # armed here never reaches another session's runs. A sweep injected
            # by a test may still keep the older single-argument shape, so the
            # session is offered only to a callable that declares it.
            if runs.accepts_keyword(sweep, "session"):
                sweep(project, session=session)
            else:
                sweep(project)
        except Exception:  # noqa: BLE001 - a failed recovery must not end the pane
            return

    def _sweep_on_cadence() -> None:
        """Run the recovery sweep at most once per cadence.

        The follower polls in a tight loop, so the cadence is what stops a
        cheap sweep from becoming a hot path. It is time since the last sweep
        rather than a count of iterations, because the loop's own rate depends
        on whether a producer is up.

        An arming's deadline bounds the poll itself, not only the gap between
        two of them. A sweep walks every registered worktree and can run for
        hours, and while it ran the check that ends an arming waited for it to
        return — a 29 m arming held open for 3 h 36 m. A sweep on its own
        thread is waited for only as long as the arming has left; one still
        walking at the deadline is abandoned and the follower ends.
        """
        nonlocal swept_at, lifetime_elapsed
        if sweep is None:
            return
        moment = clock()
        if swept_at is not None and moment - swept_at < cadence:
            return
        swept_at = moment
        if lifetime_deadline is None:
            _sweep_quietly()
            return
        remaining = lifetime_deadline - clock()
        if remaining <= 0:
            # A poll begun now would outlive the arming before it did any
            # work: the arming is over and the passes that follow take the
            # end path.
            lifetime_elapsed = True
            return
        poll = threading.Thread(target=_sweep_quietly, daemon=True)
        poll.start()
        poll.join(remaining)
        if poll.is_alive():
            lifetime_elapsed = True

    # A resume handed in the environment is an image replacing itself, which
    # already has the pane's rows on screen and needs only the format switch
    # marked. A resume read from the durable checkpoint is a re-arm, whose pane
    # is empty and must be given the stored history before anything else.
    reloading = bool(resume_state)
    # Only the first pass through the loop is an attachment. A later pass is the
    # stream still not existing, and the pane has already been told what it
    # needed to know; re-announcing it would replay the history on every poll.
    first_attach = True

    # A producer whose recorded stamp differs from this follower's is either
    # mid-reload or a seat genuinely left behind, and the record does not say
    # which. An in-place reload proves the code moved moments ago, so a mismatch
    # first seen on that path is granted the producer's reload window before it
    # is reported as staleness; a mismatch that outlasts the window is reported
    # with the cycle advice. A fresh arming has no such proof, so it is reported
    # at once. The window is only consulted while a decision is pending, so the
    # stamp comparison is not paid on every idle poll.
    reload_window = (
        PRODUCER_RELOAD_WINDOW_SECONDS
        if producer_reload_window is None
        else float(producer_reload_window)
    )
    producer_stale_since: float | None = None
    producer_reload_deferred = False
    producer_stale_advised = False
    producer_reload_failure_reported = False
    producer_stop_reported = False

    def _producer_stale_event(identity: Mapping[str, Any]) -> dict[str, Any]:
        """Name a producer running old code and a remedy its reader can run.

        The follower and the seat are judged from the same shared home, so a
        reader routinely reads a seat armed on another node. There the cycle
        pair is a command this reader cannot run: ``watch --stop`` refuses a seat
        whose record names a foreign host, so printing it sends the reader to a
        refusal. A producer runs the follower's own image held as a seat, and
        that image re-executes in place when the code stamp moves -- the reload
        the note for it already names -- so a foreign seat is one that will
        catch up on its own rather than one to cycle by hand. The line says so,
        and carries no command the reader's ``watch --stop`` would refuse.
        """
        host, foreign = _read_seat_host(project)
        stamps = (
            f"{_short_code_stamp(identity.get('code_stamp'))} vs "
            f"{_short_code_stamp(identity.get('current_stamp'))}"
        )
        if foreign:
            return {
                "event": FOLLOWER_STALE_PRODUCER_EVENT,
                "project": project,
                "session": session or "",
                "run_id": None,
                "code_stamp": identity.get("code_stamp"),
                "current_stamp": identity.get("current_stamp"),
                "host": host,
                "line": (
                    f"producer {project} runs older code than this follower "
                    f"({stamps}); its seat runs on {host} and reloads itself "
                    "when the code changes, so it will catch up on its own"
                ),
            }
        remedy = runs.watch_cycle_line(project)
        return {
            "event": FOLLOWER_STALE_PRODUCER_EVENT,
            "project": project,
            "session": session or "",
            "run_id": None,
            "code_stamp": identity.get("code_stamp"),
            "current_stamp": identity.get("current_stamp"),
            "remedy": remedy,
            "line": (
                f"producer {project} runs older code than this follower "
                f"({stamps}); cycle it with: {remedy}"
            ),
        }

    def _producer_reloading_event(
        identity: Mapping[str, Any], *, pane_line: bool
    ) -> dict[str, Any]:
        """Name a producer still catching up.

        ``pane_line`` says whether this note reaches the pane. While the window
        is open the producer may catch up on its own at any poll, and a
        follower reloading onto the same new code many times an hour must not
        put a line on the pane for each -- the event still reaches the JSON
        stream. An overrun reaches the pane when the seat is local; a foreign
        seat's host-specific advice carries the same news in one line.
        """
        return {
            "event": FOLLOWER_PRODUCER_RELOADING_EVENT,
            "project": project,
            "session": session or "",
            "run_id": None,
            "pane_line": pane_line,
            "code_stamp": identity.get("code_stamp"),
            "current_stamp": identity.get("current_stamp"),
            "line": (
                f"producer {project} runs older code than this follower "
                f"({_short_code_stamp(identity.get('code_stamp'))} vs "
                f"{_short_code_stamp(identity.get('current_stamp'))}); "
                "it is reloading itself, waiting for it to catch up"
            ),
        }

    def _producer_code_events(identity: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Lines about a producer whose code the follower has outrun.

        A mismatch first seen after this image reloaded is deferred for the
        reload window: while the window is open the producer may catch up on
        its own, so the note that it is reloading reaches the JSON stream but
        stays off the pane, which otherwise carries a line for every reload on
        a branch that moves many times an hour. Only a mismatch that outlasts
        the window earns a pane line -- the reloading note and cycle advice for
        a local seat, or only the host-specific advice for a foreign seat. A
        fresh arming has no such window to grant: it never saw the code move
        and cannot vouch that the mismatch is fresh, so the advice is owed at
        once. The deferral keys on ``reloaded_in_place`` rather than on a
        non-empty checkpoint: the fact that matters is that this image replaced
        another, and a reload whose checkpoint was empty is still a reload.
        """
        nonlocal producer_stale_since, producer_reload_deferred, producer_stale_advised
        if not identity.get("stale"):
            producer_stale_since = None
            producer_reload_deferred = False
            producer_stale_advised = False
            return []
        if producer_stale_advised:
            return []
        moment = clock()
        if producer_stale_since is None:
            producer_stale_since = moment
            if reloaded_in_place:
                producer_reload_deferred = True
                # Inside the window: the event is kept for the JSON stream but
                # not echoed to the pane.
                return [_producer_reloading_event(identity, pane_line=False)]
        if not producer_reload_deferred:
            producer_stale_advised = True
            return [_producer_stale_event(identity)]
        if moment - producer_stale_since < reload_window:
            return []
        # The reload overran its window. A local seat needs both the reloading
        # note and the cycle remedy. A foreign seat's advice already says it
        # reloads itself and names its host, so only that line reaches the pane.
        # The choice is the seat's own host, read from the seat record, rather
        # than the shape of the advice dict: a later change that adds a host
        # field to the local branch of the advice must not collapse the local
        # remedy line.
        producer_stale_advised = True
        advice = _producer_stale_event(identity)
        return [
            _producer_reloading_event(
                identity, pane_line=not _producer_seat_is_foreign(project)
            ),
            advice,
        ]

    def _producer_reload_pending() -> bool:
        return producer_stale_since is not None and not producer_stale_advised

    # A config layer a merge left momentarily malformed must cost this arming a
    # tick, never the stream. The reader is called every tick; without this it
    # would raise out of the loop and end the pane, so a transient config error
    # would take down a follower that has nothing to do with the file that broke
    # it. The line is printed once per contiguous deferral rather than once per
    # tick: a misconfigured layer is not news a reader needs refreshed every
    # poll, and repeating it would bury the fleet rows with the same sentence.
    deferred_config = False

    def _defer_config_tick(exc: Exception) -> None:
        nonlocal deferred_config
        if deferred_config:
            return
        deferred_config = True
        line = _dim_history_line(
            "reckon crew follow deferred a tick: the flight configuration could "
            f"not be read ({exc}); keeping this image and retrying on the next tick"
        )
        _echo_follow_line(line)

    # Set once the pane has been told its stored history is being restored, so
    # the resume event is emitted on the first pass alone.
    station_stale_reported = False

    while not _stopped() and not lifetime_elapsed and not consumer_gone and not settled:
        if subscriber is not None:
            # A subscriber is a query and a painter: it reads the station's
            # rows after its stored cursor, paints them with the row path, and
            # writes its place back. Nothing on this pass opens a file under
            # the crew home except the two leases the producer and the
            # delivery registration still need until they are retired.
            if first_attach:
                first_attach = False
                resume_state = {}
                if session is not None and subscriber.history:
                    yield {
                        "event": FOLLOWER_RESUME_EVENT,
                        "project": project,
                        "session": session or "",
                        "reported": {},
                    }
            # Painted before the sweep is paid for: the rows are what the
            # reader is waiting on and the sweep is the long call.
            for event in subscriber.poll():
                yield event
            for event in subscriber.flush():
                yield event
            if not station_stale_reported:
                line = subscriber.stale_line()
                if line is not None:
                    station_stale_reported = True
                    yield {
                        "event": "station-stale",
                        "project": project,
                        "session": session or "",
                        "run_id": None,
                        "line": line,
                    }
            _tick()
            if lifetime_elapsed or consumer_gone or settled:
                break
            _sweep_on_cadence()
            sleeper(poll_interval)
            continue
        if not runs.producer_live(project):
            identity = runs.watch_producer_identity(project)
            reason = _logged_producer_stop(project)
            # A producer that stops while this follower's fleet still holds
            # live work is a silent fleet the pane must name; one that stops
            # with nothing left to watch -- the measured idle exit that armed
            # no run -- keeps the pane quiet, and the event still reaches the
            # JSON consumer and the session's record. The predicate is read
            # once, at the moment the stop is observed, and only when a line is
            # actually owed: this branch spins every poll while the seat is
            # down, so a scan on each pass would tax a follower that has
            # nothing to say.
            report_stop = bool(reason) and not producer_stop_reported
            report_reload = (
                bool(identity.get("reload_started_at"))
                and not producer_reload_failure_reported
            )
            if report_stop or report_reload:
                watched = _population_has_live_work(project, session=session)
            if report_stop:
                producer_stop_reported = True
                yield {
                    "event": FOLLOWER_PRODUCER_STOPPED_EVENT,
                    "project": project,
                    "session": session or "",
                    "run_id": None,
                    "pane_line": watched,
                    "line": f"producer {project} is gone; {reason}",
                }
            if report_reload:
                producer_reload_failure_reported = True
                yield {
                    "event": FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT,
                    "project": project,
                    "session": session or "",
                    "run_id": None,
                    "pane_line": watched,
                    "line": (
                        f"producer {project} stopped during its reload begun "
                        f"{identity['reload_started_at']}; last output: "
                        f"{identity.get('log_path') or 'not recorded'}; re-arm with: "
                        f"{runs.watcher_ensure_line(project)}"
                    ),
                }
            # The sweep runs here rather than at the top of the loop: a pane
            # must show its rows before the recovery sweep's cost is paid,
            # because the sweep is the long call and the rows are what the
            # reader is waiting for.
            _sweep_on_cadence()
            _tick()
            sleeper(poll_interval)
            continue
        producer_reload_failure_reported = False
        producer_stop_reported = False

        try:
            cursor = runs.watch_stream_cursor(project)
        except FlightConfigError as exc:
            _defer_config_tick(exc)
            sleeper(poll_interval)
            continue
        deferred_config = False
        producer = cursor["producer"]
        # A producer whose code this follower has outrun is named once: a
        # reloading line inside its reload window, or the cycle advice a fresh
        # arming owes at once — see ``_producer_code_events``. It travels as an
        # event rather than being printed here so a JSON reader receives an
        # object like every other line, and the caller decides how it renders.
        for event in _producer_code_events(producer):
            yield event
        # The baseline is the fleet report: one transition per live run, in the
        # ticker's own vocabulary. Nothing about the follower itself goes on this
        # stream — a reader wants worker transitions and the fleet posture, not
        # two streams interleaved into one pane.
        stream_path = Path(cursor["stream_path"])
        mode, offset, recorded, carried_lifts = _follow_resume_plan(
            project,
            session,
            stream_path=stream_path,
            resume_state=resume_state,
        )
        if reloading and mode != "continue":
            # An in-place reload continues only where its recorded place still
            # names this stream. A resume whose checkpoint names a replaced
            # stream has no place here to continue from, so it is treated as a
            # fresh arm: the fleet baseline is re-derived from the live runs and
            # fed through the row path on this same first pass, rather than
            # reading a stream whose recorded offset never described it. Reading
            # instead defers the owed rows to a pass gated on this arming's
            # remaining lifetime, which a slow fleet snapshot can exhaust first,
            # leaving the pane with none of the rows it was owed.
            reloading = False
        if reloading:
            # An in-place reload replaces the process image with the grid still
            # on screen, so it picks the stream up where the previous image left
            # it: at the recorded offset for a file that has only advanced, or
            # at the file's own start when it was replaced or truncated — with
            # the states the pane already showed restored, so only what moved is
            # delivered and nothing is re-announced.
            cursor["offset"] = offset
            path.reseed(recorded, lift_groups=carried_lifts)
            if first_attach:
                # Continue each run's chain from what the pane last showed it,
                # for the runs the checkpoint does not name. The checkpoint is
                # the primary carrier and is read first; the log is the memory
                # for a reload whose checkpoint is gone, so a run renders
                # ``abandoned → working`` rather than restarting from a state
                # the reader never saw. A state the checkpoint already names
                # keeps the checkpoint's word, so the log cannot overwrite it.
                # The replacement image's grid starts empty, so it needs the
                # same memory a re-arm does, or the first row it draws falls
                # back to the producer's own ``from_state``. The lift groups the
                # log carries come back the same way, so a reload whose carried
                # checkpoint named no lift still holds the producer's re-arm
                # baseline for a stand the pane already drew.
                history = follow_checkpoint.read_history(project, session)
                for run_id, state in follow_checkpoint.seed_states(history).items():
                    path.remember(run_id, state)
                for group in follow_checkpoint.seed_lift_groups(history):
                    path.remember_lift(group)
            # The pane's own line, before the rows': the format switch, never a
            # run's row. It carries the remembered states so the renderer can
            # seed its grid from the same map, which is what keeps a row's left
            # side on the state the pane last showed. The line is owed only when
            # the replacement image's grid differs from the one the rows already
            # on screen were drawn with; a reload that left every column where
            # it was is not news, so the event still reaches the JSON stream but
            # the pane stays quiet. ``pane_line`` carries that decision to the
            # consumer, the way the producer's stop and reload-failed lines do.
            if first_attach:
                layout_changed = resume_state.get("layout") != _ticker_layout_signature()
                yield {
                    "event": FOLLOWER_FORMAT_EVENT,
                    "project": project,
                    "session": session or "",
                    "reported": dict(path.reported),
                    "layout_changed": layout_changed,
                    "pane_line": layout_changed,
                }
        else:
            # Every arming of a fresh image — a first attach and a re-arm alike
            # — replays the fleet: one row per live run, each stamped with the
            # recorded time the run entered its current state, in ascending time
            # order. A re-arm is therefore never blank and never a burst of rows
            # all stamped with the moment it attached. A re-arm whose pane is a
            # terminal restores the stored history first, under its frame; the
            # remembered states travel on that event rather than through the row
            # path, so the replay below is not suppressed by the very states it
            # is about to re-announce.
            #
            # The boundary is read from the file once, before the replay reads
            # anything, and the read loop then opens at exactly that byte. A
            # transition appended in between therefore falls on one side of the
            # boundary or the other — the replay's list or the loop's read — and
            # arrives once, where taking the size afterwards would drop it from
            # both. The capture also snaps back to the byte after the last
            # newline, so a record the producer is still writing is not split
            # across the two reads: a line boundary is a record boundary.
            boundary = _follow_boundary(stream_path)
            # A re-arm reads its memory from the stored history: the groups the
            # pane last showed as in force are the grants on that log not yet
            # ended. Restored before the replay feeds the fleet, so the
            # producer's re-arm baseline row for a stand already drawn adds no
            # row. A first attach has no history, so it draws the standing lift
            # once as before.
            if session is not None:
                for group in follow_checkpoint.seed_lift_groups(
                    follow_checkpoint.read_history(project, session)
                ):
                    path.remember_lift(group)
            # What this session's pane was last shown, one state per run. It is
            # consulted only by an attach that has no place to resume from. A
            # re-arm whose recorded offset still names this stream continues
            # from it, and what its pane missed is exactly what the stream
            # gained — the fleet replay's job, as it was. An attach with no
            # place — one whose checkpoint is gone, or whose recorded offset
            # names a stream that no longer exists — has no such gap to read,
            # and re-deriving the fleet would stamp a run that has been working
            # all along with the moment it attached and drop the runs that
            # finished. The record replays that gap as a diff instead: one row
            # per run that moved, from the state the record names, under a
            # header that says how many runs were found and how many changed.
            record = (
                runs.read_delivered(project, session)
                if first_attach and not reloaded_in_place and mode in ("baseline", "restart")
                else {}
            )
            gap_rows = (
                _follow_gap_rows(
                    cursor["baseline"],
                    record,
                    session=session,
                    observed=observed_sessions,
                    run_ids=selected_runs,
                )
                if record
                else []
            )
            if record:
                live_runs = sum(
                    1
                    for row in cursor["baseline"]
                    if _follow_selects(
                        row,
                        session=session,
                        observed=observed_sessions,
                        run_ids=selected_runs,
                    )
                )
                yield {
                    "event": FOLLOWER_REATTACH_EVENT,
                    "project": project,
                    "session": session or "",
                    "run_id": None,
                    "line": _follow_reattach_line(
                        live_runs, len(gap_rows), str(record.get("recorded_at") or "")
                    ),
                }
            if mode != "baseline" and first_attach:
                # The event restores the pane's stored history for a terminal,
                # and carries no remembered states: the replay below draws the
                # runs that moved, and seeding the pane's memory with the very
                # states it is about to re-announce would suppress every row of
                # it. The rows themselves restore the memory as they are drawn.
                yield {
                    "event": FOLLOWER_RESUME_EVENT,
                    "project": project,
                    "session": session or "",
                    "reported": {},
                }
            if record:
                # The diff covers the whole gap, so the read loop opens at the
                # boundary and the rows above are the only rows for the runs
                # they name.
                for row in gap_rows:
                    for printed in path.feed(row, now=clock()):
                        yield printed
            else:
                # A first arming has no place in this stream, so it has no gap
                # to deliver and derives the fleet as it stands: the place the
                # replay resumes from is the boundary itself. A re-arm did have
                # a place, and everything the stream gained between it and the
                # boundary is what the pane missed.
                resume_from = boundary if mode == "baseline" else offset
                for printed in _fleet_replay(
                    path,
                    baseline=cursor["baseline"],
                    stream_path=stream_path,
                    clock=clock,
                    reannounce=mode != "baseline",
                    resume_from=resume_from,
                    boundary=boundary,
                ):
                    yield printed
            # A re-arm's replay delivered its gap as news up to the boundary, so
            # the read loop opens there: the gap it already covered is not read
            # again, which is what stops a run that moved from being drawn twice
            # — as a replay row and then as a transition. The record's diff
            # covers the same ground from the pane's own memory, so an attach
            # that drew it opens at the boundary too. A first arming read
            # nothing to the boundary — it derives the fleet — so it leaves every
            # line already in the stream to the read loop, which opens at the
            # cursor's own offset and delivers them. Opening a first arming at
            # the boundary would drop the lines it never replayed.
            if record or mode != "baseline":
                cursor["offset"] = boundary
        # Left behind before the first read rather than after the first line:
        # an arming that starts against a quiet stream and then ends has still
        # established its place. Without this the baseline's own arming wrote
        # nothing, so a re-arm before the fleet next moved found no checkpoint
        # and replayed the baseline — the defect this removes, on the quiet path.
        _record_checkpoint(stream_path, cursor["offset"])
        resume_state = {}
        reloading = False
        first_attach = False
        # After the first row is on screen, not before it: on a first arming
        # that is the baseline, on a re-arm the history burst. Deferring the
        # sweep past them is what stops a long recovery from holding an empty
        # pane, and the cadence is unchanged because the pass after the first
        # row gates the same as any other.
        _sweep_on_cadence()

        while not stream_path.exists() and not consumer_gone and not settled:
            if not runs.producer_live(project):
                break
            if _stopped():
                return
            _tick()
            if _producer_reload_pending():
                # A deferred mismatch is re-read on the wait pass rather than
                # only on attach: the producer's reload window can close while
                # the follower waits, and the same pass that renews the lease is
                # where the escalation is owed.
                for event in _producer_code_events(
                    runs.watch_producer_identity(project)
                ):
                    yield event
            if lifetime_elapsed or consumer_gone or settled:
                break
            sleeper(poll_interval)
        if not stream_path.exists():
            continue

        with stream_path.open(encoding="utf-8") as stream:
            stream.seek(cursor["offset"])
            # The open file's identity, taken once here and carried with every
            # offset read from it: a replacement that lands while this handle is
            # open changes the path's inode but not this file's, and the recorded
            # place must describe the stream this reader is actually reading.
            stream_file_identity = follow_checkpoint.identity_of(stream)
            while True:
                # A half-written record is held, not parsed: readline returns
                # the producer's bytes so far without their newline, and
                # admitting that fragment would deliver the record truncated
                # while its completion arrived as a second one. read_whole_line
                # leaves the handle at the line's start until the fuller line
                # arrives, so the record is delivered once and whole.
                line = runs.read_whole_line(stream)
                if line:
                    event = runs.parse_stream_line(line)
                    for printed in path.feed(event, now=clock()):
                        yield printed
                    _tick(
                        stream_path=stream_path,
                        offset=stream.tell(),
                        identity=stream_file_identity,
                    )
                    if lifetime_elapsed or consumer_gone or settled:
                        break
                    _sweep_on_cadence()
                    continue
                # Stopping and losing what is already written would be the same
                # defect at the other end of the pipe, so both endings drain
                # the stream first.
                if _stopped():
                    for printed in path.flush(now=float("inf")):
                        yield printed
                    return
                if not runs.producer_live(project):
                    break
                _tick(
                    stream_path=stream_path,
                    offset=stream.tell(),
                    identity=stream_file_identity,
                )
                if _producer_reload_pending():
                    # The idle wait pass is where a deferred mismatch is
                    # re-read: the producer's reload window can close while the
                    # stream is quiet, and the follower is not otherwise looking
                    # at the seat on a busy stream.
                    for event in _producer_code_events(
                        runs.watch_producer_identity(project)
                    ):
                        yield event
                # A held opener is released on elapsed time, not on the next
                # row, so the wait pass is where its window can close while the
                # fleet is quiet. Without this a held row would wait for a
                # later transition that may never come.
                for printed in path.flush(now=clock()):
                    yield printed
                if lifetime_elapsed or consumer_gone or settled:
                    break
                # The gate is time-based, so calling it from the wait pass as
                # well as the line pass runs the recovery on elapsed time while
                # a producer is up; the outer loop only iterates after attach.
                _sweep_on_cadence()
                sleeper(poll_interval)

    for printed in path.flush(now=float("inf")):
        yield printed

    if settled:
        yield _follower_settle_event(
            project,
            session=session,
            since=last_live_at if last_live_at is not None else time.time(),
            shown=shown_needs_signature,
            observed=observed_sessions - {None},
        )
        return

    if lifetime_elapsed:
        yield _follower_end_event(
            project,
            session=session,
            armed_seconds=lifetime,
            elapsed=clock() - started_at,
        )



def _echo_follow_line(line: str, *, stream=None) -> None:
    """Write and flush one ticker line so pipe readers receive it immediately.

    The stream this writes to is almost never a terminal — it is a pipe into
    a pane or a log file — so Click's default auto-detection would strip any
    escape codes the ticker painted before a single reader ever saw them.
    Whether the line carries colour at all was already decided when it was
    painted (``Ticker.color``, honouring ``--no-color``/``NO_COLOR``); once
    painted, those bytes must reach the reader unstripped.
    """
    output = stream or click.get_text_stream("stdout")
    click.echo(line, file=output, color=True)
    output.flush()



HISTORY_DIM = "\x1b[2m"



HISTORY_RESET = "\x1b[0m"



def _short_code_stamp(stamp: Any) -> str:
    """Name a code stamp in the width a one-line report can carry.

    The full digest is 64 characters and two of them in one sentence push the
    remedy off the pane's width. Eight is what `git log --oneline` uses for the
    same reason, and the two sides of a comparison are read against each other
    rather than resolved by a reader.
    """
    text = str(stamp or "")
    return text[:8] if text else "none"



def _dim_history_line(text: str) -> str:
    """Wrap one replayed row in the dim styling, leaving its own bytes intact.

    The row's text is exactly what it was first rendered as; the mark is applied
    around it, never by rewriting the row, so a reader comparing a replayed row
    with the original sees the same columns and the same clock.
    """
    return f"{HISTORY_DIM}{text}{HISTORY_RESET}"



def _follow_history_caps(project: str) -> tuple[int, float]:
    """The pane-history caps for this project, from flight config or shipped.

    Both caps are configuration: rows and a wall-clock window, applied together
    so whichever admits fewer governs. A config that cannot be read falls back
    to the shipped defaults rather than silencing the pane — the misconfigured
    layer already surfaces on ``reckon flight``, and a reader watching a pane is
    not the person to tell about it.
    """
    from reckon.crew.follow_checkpoint import (
        DEFAULT_HISTORY_ROWS,
        DEFAULT_HISTORY_SECONDS,
    )

    fallback = (DEFAULT_HISTORY_ROWS, DEFAULT_HISTORY_SECONDS)
    try:
        from reckon import flight as flight_module
        from reckon.crew.node import parse_duration

        config = flight_module.resolve(project).config
        ticker = config.get("ticker") or {}
        rows = int(ticker.get("history_rows", DEFAULT_HISTORY_ROWS))
        window = str(ticker.get("history_window") or "").strip()
        seconds = float(parse_duration(window)) if window else DEFAULT_HISTORY_SECONDS
    except Exception:  # noqa: BLE001 - a pane must not die for its caps
        return fallback
    if rows < 1 or seconds <= 0:
        return fallback
    return rows, seconds



def _follow_row_stamp(event: Mapping[str, Any]) -> float:
    """The epoch a delivered row was drawn under, read from its own stamp.

    The history window is measured against the time each row carried, not the
    time the log was written, so a re-arm after a long outage replays the rows
    that are still inside the window rather than the ones written most recently.
    A row whose stamp cannot be read is placed at the moment of reading. Only a
    stamp that states a time of day is read; a bare calendar date carries no
    hour to place the row at, so it is placed at the moment of reading too.
    """
    text = str(event.get("observed_at") or "")
    if text != text.strip() or ("T" not in text and " " not in text):
        return time.time()
    moment = parse_utc(text)
    if moment is None:
        return time.time()
    return moment.timestamp()



def _follow_replay_visible() -> bool:
    """Whether this follower's reader is watching a live pane, not a pipe.

    A re-arm restores the pane a reader has been watching, so the rows land
    above whatever the terminal still shows. A pipe has no scrollback: the
    host's line-batching Monitor is one, and every line it carries is news to
    the transcript it feeds. Handing that reader the history makes rows it has
    already acted on arrive again as transitions, which is the replay this
    removes. Only a terminal is handed the history burst.
    """
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False



_HISTORY_FRAME = "── history · {count} rows ──"



def _follow_history_burst(rows, *, dim=_dim_history_line) -> str:
    """Compose a re-arm's history replay as one string, to be written once.

    One string rather than one write per row: a consumer that batches the stream
    into notifications then sees the whole replay as a single event.

    The rows carry their own timestamps, but a clock alone does not say a row
    is *old*: a reader skimming the pane cannot tell a restored row from a
    fresh one by its time. So the replay opens with one dim frame line that
    names it as earlier history, and every row under it reads as something the
    pane has already shown. The line is furniture only for the reader that
    needs it: this composer is reached only when a terminal is watching.
    """
    if not rows:
        return ""
    frame = dim(_HISTORY_FRAME.format(count=len(rows)))
    return "\n".join([frame, *(dim(row["text"]) for row in rows)])



TICKER_THEMES = ("dark", "light")



def _row_is_stale_inventory(event) -> bool:
    """Whether a text reader is better served by this row's absence.

    A follower emits one baseline per live run the moment it attaches, so a
    restart prints a burst of rows for work that already finished, each reading
    exactly like a landing that just happened. Only the inventory that asks the
    reader for nothing is withheld: a run waiting to be promoted, one that
    failed and one that was abandoned are finished work with a duty still
    outstanding, and their rows are exactly what a reader attaching must see.
    The machine stream still carries every one of them — a consumer
    reconstructing the fleet needs the inventory — so the judgement is made
    here, on the human path only.
    """
    from reckon.crew import ticker as ticker_module

    return ticker_module.hidden_at_attach(event)



def _ticker_grid(width, theme, no_color):
    """Build the reader's grid, deferring the import that owns its defaults."""
    from reckon.crew import ticker as ticker_module

    return ticker_module.Ticker(
        width=ticker_module.resolve_terminal_width() if width is None else width,
        theme=ticker_module.DEFAULT_THEME if theme is None else theme,
        color=not no_color,
    )



def _seed_ticker_memory(grid, states) -> None:
    """Give the grid the states the pane already showed, before its next row.

    A grid remembers the state it last put on screen for each run, and reads a
    row's left side from that memory rather than from the producer's own record.
    A replacement image builds a fresh grid, so the memory has to be handed to
    it: without this a reloaded pane draws the producer's ``from_state`` and
    shows a bare state where the reader had a transition. The map travels on the
    attach event, which is where the follower has already merged the checkpoint
    and the stored history into one remembered set.
    """
    if not states:
        return
    from reckon.crew import ticker as ticker_module

    grid._reported.update(
        {
            str(key): ticker_module._remembered_word(value)
            for key, value in dict(states).items()
        }
    )



def _ticker_options(command):
    """Attach the reader's grid choices to a command that prints ticker lines.

    The follower's own stdout is a pipe, so ``isatty`` is false and ``COLUMNS``
    is unset where the stream is read; the pane it fills is owned by a terminal
    further up the process tree, and width is resolved from that ancestor's
    window so it stays current across a resize. Colour is stated, not
    inferred. ``--width`` is the override, and the fallback for a detached
    follower whose ancestry holds no terminal.
    """
    for option in reversed(
        (
            click.option(
                "--width",
                type=int,
                default=None,
                help=(
                    "Column the right-aligned fleet counts end on. Raised to "
                    "whatever the fixed columns need, so no line ever wraps."
                ),
            ),
            click.option(
                "--theme",
                type=click.Choice(TICKER_THEMES),
                default=None,
                help="Colour set to read against a dark or light pane.",
            ),
            click.option(
                "--no-color",
                "no_color",
                is_flag=True,
                help="Print plain lines. NO_COLOR in the environment does the same.",
            ),
        )
    ):
        command = option(command)
    return command



_ATTENTION_REMOVED_AFTER = "2027-06-30"



_ATTENTION_DEPRECATION = (
    "Warning: --attention no longer filters; the follower now delivers every "
    "transition. The flag is deprecated and stops being accepted after "
    f"{_ATTENTION_REMOVED_AFTER}; remove it from any arming line before then."
)



@crew.command(name="host", hidden=True)
@click.option(
    "--owner-pid",
    type=int,
    default=None,
    help="The Claude process this host lives for; defaults to the parent."
)
@click.option(
    "--owner-start",
    default=None,
    help="The owner's kernel start time, so a reused pid is not mistaken for it.",
)
@click.option(
    "--follower-command",
    default=None,
    hidden=True,
    help="JSON argv run per request in place of this checkout's crew follow.",
)
@click.option(
    "--fifo",
    "fifo_path",
    default=None,
    hidden=True,
    help="Read requests from this FIFO path rather than the inherited stdin.",
)
@click.option(
    "--fd",
    "fd_number",
    type=int,
    default=None,
    hidden=True,
    help="Read requests from this inherited descriptor.",
)
@click.option(
    "--first-request",
    "first_request",
    default=None,
    hidden=True,
    help="A request line the caller already read off the descriptor.",
)
def crew_host(
    owner_pid, owner_start, follower_command, fifo_path, fd_number, first_request
):
    """Supervise one live session's crew followers for as long as it lives.

    Hidden from help because only the plugin entry point runs it. It reads one
    JSON request per line naming a ``project`` and a ``session``, and runs one
    ``crew follow`` per pair, restarting a child that exits while the host does.
    Its stdout is the pane, so it writes nothing there and lets its children
    speak.
    """
    import json as json_module
    import os as os_module

    from reckon.crew import session_host as session_host_module

    owner: dict[str, object] | None = None
    if owner_pid is not None:
        owner = {
            "pid": owner_pid,
            "start_time": owner_start
            or (session_host_module.process_start_time(owner_pid) or ""),
        }
    follower_argv = (
        json_module.loads(follower_command) if follower_command else None
    )
    requests = None
    if fd_number is not None:
        requests = os_module.fdopen(fd_number, "r", encoding="utf-8", errors="replace")
    elif fifo_path:
        descriptor = os_module.open(fifo_path, os_module.O_RDWR)
        requests = os_module.fdopen(descriptor, "r", encoding="utf-8", errors="replace")
    return session_host_module.run(
        owner=owner,
        follower_argv=follower_argv,
        requests=requests,
        first_request=first_request,
    )


def _needs_remind_seconds(remind: str | None) -> float:
    """The standing line's cadence: ``--remind`` when given, ten minutes else.

    The default lives where the option is read, so an arming that passes no flag
    still carries a cadence, and an operator who wants a different interval
    overrides it with the flag.
    """
    from reckon.crew import needs_you as needs_you_module
    from reckon.crew import runs as runs_module
    from reckon.crew.node import parse_duration

    if remind is None:
        return float(needs_you_module.DEFAULT_REMIND_SECONDS)
    try:
        return float(parse_duration(remind))
    except runs_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc


@crew.command(name="follow")
@click.option("--project", required=True, help="Project whose watch stream to follow.")
@click.option(
    "--session",
    default=None,
    help=(
        "Deliver only the runs this session dispatched, and register the "
        "session as attached so dispatch can verify delivery."
    ),
)
@click.option(
    "--observe-session",
    "observe_sessions",
    multiple=True,
    help=(
        "Deliver another session's transitions to this pane as well, to keep "
        "older work in view beside your own. Repeat for several. Observing "
        "registers nothing for the named session: it does not vouch for "
        "delivery, and that session's own dispatch still requires its own "
        "follower."
    ),
)
@click.option(
    "--run",
    "run_ids",
    multiple=True,
    help="Deliver only these run ids. Repeat for several.",
)
@click.option(
    "--attention",
    is_flag=True,
    hidden=True,
    help=(
        "Deprecated no-op, accepted so a follower armed before the removal "
        "reconnects instead of failing to parse. It no longer filters; its "
        f"passing is announced on stderr. Removed after {_ATTENTION_REMOVED_AFTER}."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable transition objects rather than ticker lines.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
@click.option(
    "--lifetime",
    default=None,
    metavar="DURATION",
    help=(
        "End this follower after DURATION (an integer plus s, m or h, e.g. 29m), "
        "printing one final line that names how to re-arm and this session's "
        "runs that need the coordinator. The host ends a Monitor at thirty "
        "minutes, so arm slightly less and re-arm on the final line instead of "
        "discovering the end when the host kills the pane."
    ),
)
@click.option(
    "--remind",
    default=None,
    metavar="DURATION",
    help=(
        "Re-emit the standing needs-you line every DURATION (an integer plus s, "
        "m or h, e.g. 10m) while the session still owes the coordinator a run, "
        "even when nothing has changed. Defaults to ten minutes; this overrides "
        "that interval. An empty set is never re-emitted on this cadence."
    ),
)
@_ticker_options
def crew_follow(
    project,
    session,
    observe_sessions,
    run_ids,
    attention,
    json_output,
    pretty,
    lifetime,
    remind=None,
    width=0,
    theme=None,
    no_color=False,
):
    """Follow one session's live runs without acquiring the project watcher seat.

    The seat is project-global and this delivery is session-local, so a
    follower is what a coordinator arms to be woken about its own fleet. Arm it
    through the host harness's per-line notification primitive: this command
    produces lines and does not exit, so a mechanism that reports only on exit
    delivers nothing at all.

    Every change that asks the coordinator for action, or that reverses an
    action row the pane was already shown, is delivered; a row that asks for
    nothing and reverses nothing is held back. So a start, a bare
    re-announcement of a state the pane already shows, and the terminal echo of
    the coordinator's own command do not reach the pane, while a run that
    leaves a state the pane was shown -- a recovery, a repair, a return to
    working -- does. The hold hides no recovery, which is why no option exists
    to widen or narrow it: a reader who wants every row reads the producer's
    log, which keeps the complete stream.

    ``observe_sessions`` names other sessions delivered for oversight beside
    the owning one. Only the owning session is registered — an observed
    session's dispatch guard still sees no delivery, so observing vouches for
    hearing its rows, never for hearing every run it might finish.
    """
    if attention:
        click.echo(_ATTENTION_DEPRECATION, err=True)
    from reckon.crew import runs as runs_module
    from reckon.crew.node import parse_duration
    from reckon.crew.recovery import format_watch_transition

    lifetime_seconds: float | None = None
    if lifetime is not None:
        try:
            lifetime_seconds = float(parse_duration(lifetime))
        except runs_module.CrewError as exc:
            raise click.ClickException(str(exc)) from exc
    remind_seconds = _needs_remind_seconds(remind)
    # The absolute instant this arming ends, in UTC epoch. Fixed here and never
    # rewritten, so a reload continues the original arming rather than granting
    # a fresh lifetime. Held in a local rather than ``os.environ``: the only
    # process allowed to see it is the replacement this follower execs into,
    # which the reloader below carries it to.
    carried_deadline = _carried_lifetime_deadline()
    deadline_epoch: float | None = None
    if carried_deadline is not None:
        # This image replaced one already armed with a lifetime, so it spends
        # what is left of that arming rather than arming its own. A reload that
        # arrived past the deadline yields a lifetime already elapsed, which
        # ends the follower at once — a reload never extends an arming.
        deadline_epoch = carried_deadline
        lifetime_seconds = max(0.0, carried_deadline - time.time())
    elif lifetime_seconds is not None:
        deadline_epoch = time.time() + lifetime_seconds

    # The same instant on the clock the watch loop reads, taken once, here. The
    # replacement image imports, resolves its delivery and replays the fleet
    # before its first wait pass, so a deadline re-anchored at that pass
    # charges the arming for all of it — measured at up to 2.6 s past a
    # six-second grant, and more when the first sweep is slow.
    lifetime_deadline: float | None = None
    if deadline_epoch is not None:
        lifetime_deadline = time.monotonic() + max(0.0, deadline_epoch - time.time())

    delivery = runs_module.delivery_mode()
    # Whether the host that started this follower understands a settled child.
    # Only that host re-arms the pair on its next request; an older one restarts
    # a child that exited, so a follower under it keeps following rather than
    # settling and being restarted in a loop.
    settle = os.environ.get(SETTLE_ENV) == "1"
    grid = _ticker_grid(width, theme, no_color)
    # Whether this image replaced a previous follower in place. The reloader
    # sets the checkpoint variable for exactly that replacement, so its presence
    # -- not a non-empty checkpoint -- is what says so: a reload whose checkpoint
    # came through empty is still a reload, and the staleness deferral keys on
    # the reload rather than on the checkpoint's contents.
    reloaded_in_place = _FOLLOWER_CHECKPOINT_ENV in os.environ
    resume = _take_follower_checkpoint(project)
    from reckon.crew.dispatch import (
        WATCH_ARMING_ENV,
        _adopt_launched_workers_from_reexec,
    )

    # The variable that lets a child under a throwaway home arm a producer is
    # an instruction to the process doing the arming, and this follower is not
    # that process: a child of an armed follower — the recovery sweep's probes
    # among them — would inherit the instruction and act on it, exactly as the
    # lifetime deadline did before it was carried to the replacement alone.
    os.environ.pop(WATCH_ARMING_ENV, None)

    # The follower may have just replaced its own process image; the launched
    # workers it spawned before the replacement are still its children, so it
    # adopts them exactly as if it had launched them and collects them as they
    # end.
    _adopt_launched_workers_from_reexec()

    # A follower armed by hand beside a session host that is waiting is the one
    # path that keeps a session off its host: the host is the session's own
    # delivery and this pane would only duplicate it and die at its lifetime.
    # Handing the pair over lets the host run its own long-lived follower once
    # this one is gone, and it happens after the adoption above so a reloaded
    # follower re-registers the children it already carries before it exits --
    # a handoff taken first would abandon them uncollected. A follower the host
    # already runs is left alone, and a session with no waiting host streams as
    # before, so this only ever upgrades delivery.
    if _hand_off_from_follower(project, session):
        line = _host_handoff_line(project, session)
        if json_output:
            _emit_crew_result(
                {
                    "event": FOLLOWER_END_EVENT,
                    "project": project,
                    "session": session or "",
                    "handed_to_host": True,
                    "line": line,
                },
                pretty,
                observation=True,
            )
        else:
            _echo_follow_line(line)
        return

    # Fix the process this follower reports to, once, before anything claims a
    # registration. A registration taken over later records this owner rather
    # than whatever process happens to be the parent by then, and the reloader
    # carries the same value to a replacement image.
    owner = runs_module.follower_owner()

    # The station this follower reads, when this node runs one over this crew
    # home. A node without one keeps the producer's stream, so a follower
    # armed before the station exists still delivers.
    subscriber = _station_subscriber_for(
        project,
        session=session,
        observed=observe_sessions,
        run_ids=run_ids,
    )
    if subscriber is not None:
        # The station's own row is the pane's place, so its restored checkpoint
        # replaces whatever the producer's file held.
        resume = {
            "reported": dict(subscriber.path.reported),
            "lift_groups": sorted(subscriber.path.lift_groups),
        }

    def stream_events(registration):
        reloader = _FollowerReloader(
            project,
            registration,
            deadline=deadline_epoch,
            owner=owner,
            color=getattr(grid, "color", False),
        )

        def poll(checkpoint) -> None:
            """Take over a registration whose holder has gone, while streaming.

            Silently. A second follower streaming read-only, and its later
            takeover, are facts about the followers rather than about the fleet.
            The session that needs telling is one trying to dispatch, and the
            dispatch guard tells it there, with the remedy.
            """
            if registration is not None and not registration.held:
                registration.acquire()
            if subscriber is None:
                reloader.poll(checkpoint)

        from reckon.crew import follow_checkpoint as history_module

        def read_history() -> list[dict]:
            """The pane's memory, from the station when it is the delivery path."""
            if subscriber is not None:
                return list(subscriber.history)
            return history_module.read_history(project, session)

        def append_history(**kwargs) -> None:
            if subscriber is not None:
                subscriber.append_history(**kwargs)
                return
            history_module.append_history(project, session, **kwargs)

        # The pane's memory of what it drew, for a session that will re-arm.
        # A session-less follower exists to observe the whole fleet in one
        # pass, so it has no pane to restore and keeps no log.
        history_caps = _follow_history_caps(project) if session is not None else None
        replay_dim = _dim_history_line if getattr(grid, "color", False) else str

        # The state each run was last handed to this reader at, so the session's
        # next attach can replay the gap rather than re-announce the fleet. It
        # is written as rows are delivered, because a row the pane never
        # received is not one the reader saw and must not subtract a run from
        # the next replay. The record this attach inherited is carried forward:
        # a run drawing nothing here keeps the state its last row carried, and
        # a write that dropped it would replay the run from `dispatched` as soon
        # as one other run moved.
        if subscriber is not None:
            # The subscriber's checkpoint carries the same states and lives in
            # the station's row, so the delivery path opens nothing under the
            # crew home to seed the pane's memory.
            delivered_states: dict[str, str] = {
                str(run_id): str(state)
                for run_id, state in subscriber.path.reported.items()
            }
        else:
            delivered_states = {
                str(run_id): str(state)
                for run_id, state in (
                    runs_module.read_delivered(project, session).get("states") or {}
                ).items()
            }

        def note_delivered(row) -> None:
            run_id = str(row.get("run_id") or "")
            state = str(row.get("to_state") or "")
            if not run_id or not state or delivered_states.get(run_id) == state:
                return
            delivered_states[run_id] = state
            if subscriber is not None:
                subscriber.note_delivered(row)
                return
            runs_module.write_delivered(project, session, delivered_states)

        # Whether the last event was the settle row. The follower ends its
        # generator after that row, so this is read once the loop has drained,
        # and the process leaves with the settle status its host keys on.
        settled = False
        for event in _follow_watch_lines(
            project,
            session=session,
            observed=observe_sessions,
            run_ids=run_ids,
            on_poll=poll,
            resume=resume,
            lifetime=lifetime_seconds,
            lifetime_deadline=lifetime_deadline,
            registration=registration,
            reloaded_in_place=reloaded_in_place,
            settle=settle,
            remind=remind_seconds,
            subscriber=subscriber,
        ):
            # An attach event carries the states the pane already showed, so the
            # grid is seeded from the same remembered map the follower filtered
            # against — before any fresh row is consumed. The map is the
            # follower's own bookkeeping rather than part of the pane's event,
            # so it is taken off before the event goes on to the reader.
            _seed_ticker_memory(grid, event.pop("reported", None))
            # A row the arming re-announced rather than drew as news: it is a
            # fresh line for the pane, but the run's row is already the one the
            # stored history carries, so recording it again would replay the
            # same run twice on the next burst.
            reannounced = bool(event.pop("reannounced", False))
            if event.get("event") in (FOLLOWER_END_EVENT, FOLLOWER_SETTLE_EVENT):
                # This one line is about the follower, not the fleet, so it is
                # printed as it was written rather than rendered as a fleet row.
                # The settle row is the child's last: the process leaves with
                # the settle status so its host does not restart it. A composed
                # line that is empty names nothing to act on, so nothing is
                # printed -- but the event is still emitted for a JSON reader,
                # and the child still leaves with its settle code below.
                if event.get("event") == FOLLOWER_SETTLE_EVENT:
                    settled = True
                line = str(event.get("line") or "")
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif line:
                    _echo_follow_line(line)
                continue
            if event.get("event") == FOLLOWER_FORMAT_EVENT:
                # The follower reloaded onto a new image mid-stream. The rows it
                # drew before the reload were drawn by the old format and are
                # replayed verbatim from the log, so the marker stands between
                # them and the new ones; the DIM row is the reader's signal that
                # the drawing style changed here. A reload whose grid did not
                # move is not news: its event still reaches the JSON stream, but
                # the pane is handed no marker and the log records none, so a
                # later re-arm does not replay a switch that never happened.
                pane_line = bool(event.pop("pane_line", True))
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif pane_line and session is not None:
                    _echo_follow_line(replay_dim(history_module.FORMAT_CHANGED_TEXT))
                    append_history(
                        text=history_module.FORMAT_CHANGED_TEXT,
                        at=time.time(),
                        kind=history_module.FORMAT_CHANGED_KIND,
                        max_rows=history_caps[0],
                        max_seconds=history_caps[1],
                    )
                continue
            if event.get("event") == FOLLOWER_REATTACH_EVENT:
                # The pane's own header, written before any fleet row: it says
                # the follower re-attached, how many live runs it found and how
                # many the replay below will draw. Like the other framing lines
                # it is about the pane rather than the fleet, so it is never
                # rendered as a run's row.
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                else:
                    _echo_follow_line(replay_dim(str(event.get("line") or "")))
                continue
            if event.get("event") == FOLLOWER_RESUME_EVENT:
                # A re-arm, before any fresh row: restore the pane exactly as
                # the reader last saw it, in one write so the whole view lands
                # as a single event, then let the live rows follow.
                #
                # Only a terminal is handed the history. A pipe reader — the
                # host's line-batching Monitor is one — has no scrollback to
                # fill and would read the restored rows as fresh transitions,
                # which is precisely the replay this removes. When there is a
                # terminal, the burst opens under one dim frame line that names
                # it as earlier history, so a restored row is never acted on.
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif session is not None and _follow_replay_visible():
                    restored = history_module.cap_history(
                        read_history(),
                        now=time.time(),
                        max_rows=history_caps[0],
                        max_seconds=history_caps[1],
                    )
                    burst = _follow_history_burst(restored, dim=replay_dim)
                    if burst:
                        _echo_follow_line(burst)
                continue
            if event.get("event") in (
                FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT,
                FOLLOWER_PRODUCER_STOPPED_EVENT,
            ):
                # A producer stop reaches the JSON stream in every case and
                # the pane only when the follower's own fleet still holds live
                # work. The emission site marks that predicate on the event as
                # ``pane_line``; stripping it here keeps the emitted object the
                # shape every other JSON row carries, and a quiet pane is the
                # whole point of the mark rather than a dropped event.
                pane_line = bool(event.pop("pane_line", True))
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif pane_line:
                    _echo_follow_line(replay_dim(str(event.get("line") or "")))
                continue
            if event.get("event") in (
                FOLLOWER_STALE_PRODUCER_EVENT,
                FOLLOWER_PRODUCER_RELOADING_EVENT,
            ):
                # The seat's producer runs older code than this follower — with
                # the cycle remedy once it is confirmed stale, or a line saying
                # it is still catching up inside its reload window. Like the
                # format marker it is about the pane rather than the fleet, so
                # it is never rendered as a run's row: JSON mode emits the
                # object with the stamps (and the remedy when there is one), and
                # text mode prints the one dim line. A reloading note marks its
                # own pane reach on ``pane_line``: while the window is still
                # open it stays off the pane, and a foreign seat's overrun
                # leaves the pane to the host-specific advice.
                pane_line = bool(event.pop("pane_line", True))
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif pane_line:
                    _echo_follow_line(replay_dim(str(event.get("line") or "")))
                continue
            if json_output:
                _emit_crew_result(event, pretty, observation=True)
                # The record is the pane's memory, and there is one per session
                # rather than one per output mode: a row only the JSON consumer
                # received is not one the pane in front of the session drew, and
                # counting it would subtract the run from that pane's next
                # re-attach.
                if not _row_is_stale_inventory(event):
                    note_delivered(event)
            elif not _row_is_stale_inventory(event):
                # An observing follower draws the owner column on every row so
                # the grid stays aligned, and the owning session's own rows are
                # blanked in it; only the observed rows carry the foreign glyph.
                with_session = session is None or bool(observe_sessions)
                rendered = format_watch_transition(
                    _follow_render_event(
                        event, session=session, observed=observe_sessions
                    ),
                    with_session=with_session,
                    ticker=grid,
                    session=session,
                )
                _echo_follow_line(rendered)
                note_delivered(event)
                if history_caps is not None and not reannounced:
                    # The row's bytes as drawn, and the stamp it carried, so a
                    # later re-arm replays the pane rather than a re-derivation
                    # of it: the clock a reader saw is the clock that returns.
                    append_history(
                        text=rendered,
                        at=_follow_row_stamp(event),
                        run_id=str(event.get("run_id") or ""),
                        state=str(event.get("to_state") or ""),
                        group=str(event.get("node") or ""),
                        max_rows=history_caps[0],
                        max_seconds=history_caps[1],
                    )

        if settled:
            # The session's work drained and the idle line is on the pane. The
            # process leaves with the settle status rather than as a failed
            # child, so the host that runs it retires the pair instead of
            # restarting it and repeating the line.
            sys.exit(SETTLED_EXIT_CODE)

    try:
        if subscriber is not None:
            # The subscriber's place and registration are its row in the node's
            # store, so nothing under the crew home is opened to hold them.
            if session is not None:
                subscriber.register(
                    delivery=delivery,
                    scope={"runs": list(run_ids)},
                )
            try:
                stream_events(_StationRegistration(subscriber))
            finally:
                if session is not None:
                    subscriber.detach()
                subscriber.store.close()
            return
        if session is None:
            stream_events(None)
            return
        with runs_module.follower_registration(
            project,
            session,
            delivery=delivery,
            scope={"runs": list(run_ids)},
        ) as registration:
            stream_events(registration)
    except runs_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc



@crew.command(name="watch")
@click.option("--project", required=True, help="Project whose live fleet to watch.")
@click.option(
    "--stop",
    is_flag=True,
    help="Stop the registered project watcher and release its live seat.",
)
@click.option(
    "--stall-window",
    default="15m",
    show_default=True,
    help="Exit when a non-terminal run's stream stays quiet this long.",
)
@click.option(
    "--exit-on-empty",
    is_flag=True,
    help=(
        "With --once, exit when no live pointers remain instead of "
        "waiting for the first one."
    ),
)
@click.option(
    "--once",
    is_flag=True,
    help=(
        "Return after a single fleet event instead of following. The seat is "
        "released on return, so the caller must re-arm before its next dispatch."
    ),
)
@click.option(
    "--ensure",
    "ensure_service",
    is_flag=True,
    help=(
        "Start or restart this project's watcher user service and return, "
        "instead of watching here. Idempotent: a second call on a live, "
        "unchanged unit reports it and starts nothing."
    ),
)
@click.option(
    "--follow",
    is_flag=True,
    hidden=True,
    help=(
        "Accepted for compatibility; following is now the default, and this "
        "flag still wins over --once."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable transition objects rather than ticker lines.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
@_ticker_options
def crew_watch(
    project,
    stop,
    stall_window,
    exit_on_empty,
    once,
    ensure_service,
    follow,
    json_output,
    pretty,
    width,
    theme,
    no_color,
):
    """Watch project-wide live runs as the single producer through reconciliation.

    Following is the default because the watcher seat is what
    ``reckon crew dispatch`` requires. A seat released after every landing has
    to be re-armed before each dispatch, and a coordinator that forgets meets
    ``watcher-required`` instead of a worker. Following holds the seat from an
    empty project, through every landing, until the fleet drains.
    """
    if stop:
        from reckon.crew.node import CrewError
        from reckon.crew.recovery import unwatch

        try:
            result = unwatch(project)
        except CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(result, pretty)
        return

    from reckon.crew import dispatch_watch
    from reckon.crew.dispatch_watch import _scrub_session_identity

    # The producer outlives the session that armed it and sweeps every
    # session's runs, so it must carry no coordinator's identity. Its
    # environment is fixed when it starts, but it reloads in place rather than
    # restarting, so a producer armed before this shed the identity here, at
    # its next reload, keeping its pid and its seat rather than being restarted
    # into a clean environment.
    _scrub_session_identity(os.environ)
    crew_module, _ = _crew_modules()
    if ensure_service:
        # The service is the durable seat: it survives the shell that ensured
        # it, carries the backend directory on its PATH, and is restarted onto
        # a rewritten unit. Nothing here holds the seat in the caller's name.
        from reckon.crew import runs as runs_module

        try:
            result = runs_module.ensure_watcher_service(project)
        except crew_module.CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(result, pretty)
        if result.get("cycling"):
            # A cycling unit is the fault ``ensure`` exists to surface, so its
            # verdict carries a non-zero exit for a script or hook that reads
            # the status rather than the JSON that is also printed above.
            sys.exit(1)
        return
    # The station reads and classifies each fact once for the whole node, so it
    # is requested before this process claims the producer seat. A node whose
    # station is missing would otherwise have every producer re-derive the
    # fleet from scratch. Outside a fleet allocation this is a no-op, and a
    # held station is left alone.
    dispatch_watch._ensure_node_station()
    # --exit-on-empty only means anything to the single-event mode, so asking
    # for it selects that mode rather than being silently ignored.
    single_event = once or exit_on_empty
    try:
        if follow or not single_event:
            from reckon.crew import runs as runs_module
            from reckon.crew.recovery import format_watch_transition, watch_follow

            grid = _ticker_grid(width, theme, no_color)
            # The producer holds the seat for the life of its stream, so it
            # takes changed follower-side modules the way the follower does:
            # the same content-hash stamp gates the check, the same throwaway
            # import proves the replacement loads, and the seat descriptor
            # rides the exec so the seat is never released.
            reloader = _FollowerReloader(
                project,
                None,
                label="crew watch",
                seat=True,
                color=getattr(grid, "color", False),
            )
            poller = _StampPoll(reloader, runs_module.FOLLOWER_FRESHNESS_SECONDS)
            stopped_by_signal: int | None = None

            def stop_on_signal(signum, frame) -> None:
                nonlocal stopped_by_signal
                stopped_by_signal = signum
                raise SystemExit(128 + signum)

            previous_handlers = {
                signum: signal.signal(signum, stop_on_signal)
                for signum in (signal.SIGTERM, signal.SIGHUP)
            }
            stop_reason = "watch stream ended"
            try:
                poller.start()
                try:
                    for result in watch_follow(
                        project, stall_window=stall_window, transitions=True
                    ):
                        if json_output or result.get("event") not in {
                            "baseline",
                            "transition",
                        }:
                            _emit_crew_result(result, pretty, observation=True)
                        elif not _row_is_stale_inventory(result):
                            click.echo(
                                format_watch_transition(result, ticker=grid), color=True
                            )
                finally:
                    poller.stop()
                renewed = runs_module.watch_lease_renewed_at(project)
                if (
                    renewed is not None
                    and time.time() - renewed >= runs_module.producer_lease_seconds()
                ):
                    stop_reason = "producer lease expired"
            except BaseException as exc:
                if stopped_by_signal is not None:
                    stop_reason = f"received {signal.Signals(stopped_by_signal).name}"
                else:
                    stop_reason = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                click.echo(f"reckon crew watch stopped: {stop_reason}", err=True)
                for signum, previous in previous_handlers.items():
                    signal.signal(signum, previous)
            return
        result = crew_module.watch(
            project,
            stall_window=stall_window,
            exit_on_empty=exit_on_empty,
        )
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)
