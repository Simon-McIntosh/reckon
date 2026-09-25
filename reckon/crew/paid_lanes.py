"""Publish each account's metered headroom as one document a pre-flight reads.

Headroom for a paid backend is read only when someone runs pre-flight or
dispatch, from sources that can disagree and can be a day old. The local lane
already publishes its own position in ``lane.json``; this module gives the
metered accounts the same treatment: one document, refreshed on its own clock,
carrying every window's utilisation, reset time, burn multiple, projected
exhaustion, the source it was read from and when it was observed.

**One document, one entry per account.** Each entry reconciles the sources that
reported for that account by recency, per window, and names the source it used.
The reconciliation is per window rather than per account because the sources
carry different windows: a rollout receipt reports the week alone on this
workstation, and letting the freshest reading speak for every window at once
would blank a five-hour clock the older receipt had already measured. Per-window
resolution is also what keeps one failed precondition from turning every field
``unknown`` -- the two windows are resolved independently.

**Absence of a reading is not a position.** A window no source resolved reports
``state: "unknown"`` with every figure ``None`` and never a ``0.0``, which
cannot be told apart from a measured empty window. A window whose own observation
is older than the staleness horizon keeps its figure and is marked ``stale:
true`` -- stale alone, so a fresh sibling window is not dragged down with it.

The reader of this document is the pre-flight, which reconstructs one window
reading per account from it and paces each declared group from those readings.

The command writes the document atomically, because a reader that opens it while
it is half-written would read a truncated account list as a real absence:
``python -m reckon.crew.paid_lanes --once``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import window_reading

#: The windows the document carries, in the order a reader expects them.
PERIODS = window_reading.PERIODS

#: The length of each named window in hours, used to place it in its own period
#: so a burn multiple and a projected exhaustion can be derived. A period the
#: provider reports that is not named here is still carried, with its derived
#: figures withheld rather than guessed.
WINDOW_HOURS = {"five_hour": 5.0, "seven_day": 168.0}

#: A window figure older than this is reported stale for that window alone. The
#: horizon is generous because a metered stream reports only a few times a day;
#: the point is to mark a figure no reader can trust as current, not to demand a
#: fresh one on every read.
DEFAULT_STALE_SECONDS = 3600.0

#: Where the document lands when no path is configured or supplied. The
#: ``public`` tree is what the docs server and peer sessions read.
DEFAULT_DOCUMENT_PATH = "~/public/reckon/paid-lanes.json"

#: The document's filename under the crew home when ``RECKON_HOME`` names a tree.
#: Whatever the caller isolates through ``RECKON_HOME`` reads and writes the one
#: document at this name, so a publisher and a reader resolve the same file.
DOCUMENT_NAME = "paid-lanes.json"

#: The environment variable naming the crew home an isolated run reads and writes.
RECKON_HOME_ENV = "RECKON_HOME"

#: The environment variable that overrides the published path, so a deployment
#: can point every reader at one document without editing code.
DOCUMENT_ENV = "RECKON_PAID_LANES_DOCUMENT"

# The local service publishes this document independently of the metered
# account timer.  The override exists so an isolated publisher can read a
# fixture without consulting the operator's live lane.
LOCAL_LANE_DOCUMENT_PATH = "~/public/imas-ambix/lane.json"
LOCAL_LANE_DOCUMENT_ENV = "RECKON_LOCAL_LANE_DOCUMENT"

#: The user units the refresh deployment installs. The service publishes the
#: document once; the timer activates that service on its own clock so no
#: dispatch or pre-flight has to be the thing that refreshes it.
SERVICE_NAME = "reckon-paid-lanes.service"
TIMER_NAME = "reckon-paid-lanes.timer"

#: The environment variable naming the user's config root, per the XDG base
#: directory specification. ``systemd`` resolves user units under this when it
#: is set, so the installer must resolve the same directory it would.
XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"

#: The refresh cadence, expressed in the timer's own directives so the document
#: is republished a minute after boot and every five minutes of timer activity.
BOOT_DELAY = "1min"
REFRESH_INTERVAL = "5min"

SERVICE_TEMPLATE = """\
[Unit]
Description=publish the metered-backend headroom document
After=network.target

[Service]
Type=oneshot
WorkingDirectory={working_directory}
{environment}\
ExecStart={exec_start}
"""

TIMER_TEMPLATE = """\
[Unit]
Description=republish the metered-backend headroom document every five minutes

[Timer]
OnBootSec={boot_delay}
OnUnitActiveSec={interval}
Unit={service}

[Install]
WantedBy=timers.target
"""

OBSERVED = "observed"
UNKNOWN = "unknown"

#: What ``--help`` and ``-h`` print. Asking what the command does must never be
#: the same act as running it, so the text is emitted and the run stops here.
USAGE = """\
usage: python -m reckon.crew.paid_lanes [--once] [--path PATH]
       [--project PROJECT] [--checkout-path PATH] [--install-timer]

Compose the metered-backend headroom document, one entry per account, and
write it atomically. The document is what the pre-flight reads between
dispatches rather than only at a refusal.

options:
  --once                 publish the document one time and exit (the default)
  --path PATH            write to PATH instead of the default location
  --project PROJECT      read accounts from PROJECT's resolved flight config
  --checkout-path PATH   resolve PROJECT's config relative to PATH
  --install-timer        install the user timer that republishes the document
                         every five minutes, then exit without publishing
  -h, --help             print this message and exit without publishing
"""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One source's reading for one account, with the source named."""

    source: str
    reading: window_reading.WindowReading


def local_lane_path(path: str | Path | None = None) -> Path:
    """Resolve the local-lane telemetry document."""
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get(LOCAL_LANE_DOCUMENT_ENV)
    return Path(override or LOCAL_LANE_DOCUMENT_PATH).expanduser()


def read_local_lane(
    path: str | Path | None = None,
    *,
    moment: datetime | None = None,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    """Read the local lane's published occupancy and mark an old sample stale."""
    now = moment or datetime.now(tz=UTC)
    try:
        with local_lane_path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        payload = None
    if not isinstance(payload, Mapping):
        return {"state": UNKNOWN, "observed_at": None, "stale": False}
    observed = _parse_stamp(payload.get("observed_at"))
    if observed is None:
        return {"state": UNKNOWN, "observed_at": None, "stale": False}
    gate = payload.get("router_generation_gate")
    gate = gate if isinstance(gate, Mapping) else {}
    age = (now - observed).total_seconds()
    shelf_life = payload.get("suggested_shelf_life_seconds")
    if isinstance(shelf_life, bool) or not isinstance(shelf_life, (int, float)):
        shelf_life = stale_seconds
    return {
        "state": str(payload.get("state") or "measured"),
        "running": payload.get("running"),
        "ceiling": payload.get("concurrent_requests"),
        "headroom": payload.get("headroom"),
        "gate_width": gate.get("width"),
        "in_flight": gate.get("in_flight"),
        "waiting": payload.get("waiting"),
        "gate_waiting": gate.get("waiting"),
        "kv_occupancy": payload.get("kv_occupancy"),
        "prefix_hit_rate": payload.get("prefix_hit_rate"),
        "observed_at": observed.isoformat(),
        "age_seconds": age,
        "stale": age > float(shelf_life),
    }


def document_path(path: str | Path | None = None) -> Path:
    """Resolve the published document through one rule for reader and writer.

    Order: the explicit argument, then the ``DOCUMENT_ENV`` override, then the
    document under the crew home when ``RECKON_HOME`` names one, then the
    default location. The crew home is what a caller isolates to run against its
    own tree, so both the publisher and every reader resolve the isolated
    document rather than the operator's real home; a fallback that consulted
    ``HOME`` alone would make an isolated run publish to one file and read
    another. Both the publishing command and the pre-flight reach the document
    through this one function, so there is no second rule to drift from it.
    """
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get(DOCUMENT_ENV)
    if override:
        return Path(override).expanduser()
    home = os.environ.get(RECKON_HOME_ENV)
    if home:
        return Path(home).expanduser() / DOCUMENT_NAME
    return Path(DEFAULT_DOCUMENT_PATH).expanduser()


def _parse_stamp(value: Any) -> datetime | None:
    """A usable UTC instant from an ISO string, else ``None``.

    A stamp without a zone is read as UTC, matching the stream reader: reading
    one as local would invert a statement about how old a reading is.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _derived(
    period: str, utilisation: float, resets: datetime | None, *, moment: datetime
) -> tuple[float | None, str | None]:
    """A window's burn multiple and projected exhaustion, from its own clocks.

    The burn multiple is the fraction spent over the fraction of the window
    elapsed, so ``1.0`` is exactly on pace and a higher figure is a window
    draining faster than it refills. The projected exhaustion is the instant the
    window reaches ``1.0`` at that burn. Both need the window to have begun and
    something to have been spent, so an unreadable reset, a spent-nothing window
    and a window that has not opened yet all withhold both rather than invent one.
    """
    total = WINDOW_HOURS.get(period)
    if total is None or resets is None or utilisation <= 0.0:
        return None, None
    elapsed_hours = total - (resets - moment).total_seconds() / 3600.0
    if elapsed_hours <= 0.0:
        return None, None
    burn = utilisation / (elapsed_hours / total)
    hours_to_exhaust = (1.0 - utilisation) * elapsed_hours / utilisation
    exhausts_at = moment + timedelta(hours=hours_to_exhaust)
    return burn, exhausts_at.isoformat()


def _window_block(
    period: str,
    candidate: Candidate,
    figure: window_reading.WindowFigure,
    *,
    moment: datetime,
    stale_seconds: float,
) -> dict[str, Any]:
    """One window of one account as the document publishes it."""
    observed_at = figure.observed_at
    if observed_at is None:  # the caller resolves only dated figures
        return _unknown_window(period)
    age = (moment - observed_at).total_seconds()
    burn, exhaustion = _derived(
        period, float(figure.utilisation), _parse_stamp(figure.resets_at), moment=moment
    )
    return {
        "period": period,
        "state": OBSERVED,
        "utilisation": float(figure.utilisation),
        "resets_at": figure.resets_at,
        "burn_multiple": burn,
        "projected_exhaustion": exhaustion,
        "source": candidate.source,
        "observed_at": observed_at.isoformat(),
        "age_seconds": age,
        "stale": age > stale_seconds,
    }


def _unknown_window(period: str) -> dict[str, Any]:
    """A window no source resolved: an explicit absence, never a zero."""
    return {
        "period": period,
        "state": UNKNOWN,
        "utilisation": None,
        "resets_at": None,
        "burn_multiple": None,
        "projected_exhaustion": None,
        "source": None,
        "observed_at": None,
        "age_seconds": None,
        "stale": False,
    }


def account_entry(
    account: str,
    candidates: Iterable[Candidate],
    *,
    moment: datetime,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    """One account's entry, reconciling its sources per window by recency.

    Each window is resolved independently: among the candidates that carried it
    with a usable observation time, the newest speaks and its source is named. A
    window no candidate resolved is ``unknown`` while its sibling is observed,
    which is the point of resolving per window rather than per account.
    """
    ordered = list(candidates)
    windows: dict[str, Any] = {}
    freshest: tuple[datetime, Candidate] | None = None
    for period in PERIODS:
        best: tuple[datetime, Candidate, window_reading.WindowFigure] | None = None
        for candidate in ordered:
            figure = candidate.reading.figure(period)
            if figure is None or figure.observed_at is None:
                continue
            if best is None or figure.observed_at > best[0]:
                best = (figure.observed_at, candidate, figure)
        if best is None:
            windows[period] = _unknown_window(period)
            continue
        when, candidate, figure = best
        windows[period] = _window_block(
            period, candidate, figure, moment=moment, stale_seconds=stale_seconds
        )
        if freshest is None or when > freshest[0]:
            freshest = (when, candidate)
    return {
        "account": account,
        "state": (
            OBSERVED
            if any(block["state"] == OBSERVED for block in windows.values())
            else UNKNOWN
        ),
        "source": None if freshest is None else freshest[1].source,
        "observed_at": None if freshest is None else freshest[0].isoformat(),
        "windows": windows,
    }


def _candidate(
    item: Candidate | tuple[str, window_reading.WindowReading],
) -> Candidate:
    """Coerce a caller's ``(source, reading)`` pair into a candidate."""
    if isinstance(item, Candidate):
        return item
    source, reading = item
    return Candidate(source=str(source), reading=reading)


def compose_document(
    accounts: Iterable[str],
    *,
    sources: Mapping[
        str, Iterable[Candidate | tuple[str, window_reading.WindowReading]]
    ]
    | None = None,
    reader: Callable[
        [str], Iterable[Candidate | tuple[str, window_reading.WindowReading]]
    ]
    | None = None,
    moment: datetime | None = None,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    local_lane: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the one published document, one entry per account.

    ``sources`` maps an account to the candidates that reported for it; ``reader``
    is a per-account callable used for any account ``sources`` does not name,
    which is how the command supplies the production gatherer and a test supplies
    one account at a time.
    """
    now = moment or datetime.now(tz=UTC)
    supplied = sources or {}
    entries: dict[str, Any] = {}
    for account in accounts:
        raw = supplied.get(account)
        if raw is None and reader is not None:
            raw = reader(account)
        candidates = [_candidate(item) for item in (raw or ())]
        entries[account] = account_entry(
            account, candidates, moment=now, stale_seconds=stale_seconds
        )
    return {
        "document": "paid-lanes",
        "generated_by": "reckon.crew.paid_lanes",
        "observed_at": now.isoformat(),
        "accounts": entries,
        "local_lane": dict(local_lane or {"state": UNKNOWN}),
    }


def document_windows(
    document: Mapping[str, Any] | None, *, moment: datetime | None = None
) -> dict[str, window_reading.WindowReading]:
    """Rebuild one window reading per account from a published document.

    Only observed windows compete; an ``unknown`` window contributes no figure,
    so a document that resolves nothing yields an empty mapping rather than a
    reading that would be mistaken for a measured position. Each window's own
    observation time is carried through, so a downstream reader sees the same
    age the document reported.
    """
    if not isinstance(document, Mapping):
        return {}
    accounts = document.get("accounts")
    if not isinstance(accounts, Mapping):
        return {}
    windows: dict[str, window_reading.WindowReading] = {}
    for account, entry in accounts.items():
        if not isinstance(entry, Mapping):
            continue
        blocks = entry.get("windows")
        if not isinstance(blocks, Mapping):
            continue
        figures: list[window_reading.WindowFigure] = []
        for period, block in blocks.items():
            if not isinstance(block, Mapping) or block.get("state") != OBSERVED:
                continue
            utilisation = block.get("utilisation")
            observed = _parse_stamp(block.get("observed_at"))
            if utilisation is None or observed is None:
                continue
            age = block.get("age_seconds")
            figures.append(
                window_reading.WindowFigure(
                    period=str(period),
                    utilisation=float(utilisation),
                    observed_at=observed,
                    age_seconds=None if age is None else float(age),
                    resets_at=(
                        block.get("resets_at")
                        if isinstance(block.get("resets_at"), str)
                        else None
                    ),
                )
            )
        if not figures:
            continue
        newest = max(figure.observed_at for figure in figures)
        windows[str(account)] = window_reading.WindowReading(
            figures=tuple(figures),
            observed_at=newest,
            age_seconds=(
                (moment - newest).total_seconds()
                if moment is not None
                else max(figure.age_seconds or 0.0 for figure in figures)
            ),
        )
    return windows


def read_document(path: str | Path | None = None) -> dict[str, Any] | None:
    """Load the published document, or ``None`` when there is none to read.

    An absent or unreadable file is reported as no document rather than raising:
    a reader that could not reach the document falls back to what it already has
    recorded, rather than failing the wave.
    """
    resolved = document_path(path)
    try:
        with resolved.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, Mapping) else None


def write_document_atomically(
    document: Mapping[str, Any], path: str | Path | None = None
) -> Path:
    """Write the document to ``path`` in one indivisible step.

    A reader may open the document at any moment, and a plain write would let it
    read a truncated file whose missing accounts look like real absences. The
    write lands in a sibling temporary file and is renamed into place, so a
    reader sees either the previous document or the new one, never a half one.
    """
    resolved = document_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(resolved.parent),
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        pending_path = Path(handle.name)
        handle.write(payload)
    try:
        os.replace(pending_path, resolved)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(pending_path)
        raise
    return resolved


def gather_sources(
    accounts: Iterable[str],
    *,
    project: str | None = None,
    root: str | Path | None = None,
    moment: datetime | None = None,
    records: Iterable[Mapping[str, Any]] | None = None,
    pointers: Iterable[Mapping[str, Any]] | None = None,
    rollouts: Callable[[str], object] | None = None,
    rollout_root: str | Path | None = None,
) -> dict[str, list[Candidate]]:
    """Gather each account's candidate readings from the three recorded homes.

    The homes are the ones the pre-flight already reads: a run's committed (or
    live) ``lane_receipt``, the session rollout of an in-flight run, and the
    stream a served run reported its windows on. Each is returned as a named
    candidate so the document can say which source it used -- a thing a single
    reconciled reading could not say.
    """
    from reckon import budget, crew, ledger
    from reckon.crew import rollout as rollout_module

    now = moment or datetime.now(tz=UTC)
    if records is not None:
        rows = list(records)
    elif project:
        rows = list(ledger.runs(project, root))
    else:
        rows = []
    if pointers is None:
        live = [
            record
            for record in crew.list_live()
            if str(record.get("project") or "") == project
        ]
    else:
        live = list(pointers)
    wanted = set(accounts)
    by_account: dict[str, list[Candidate]] = {name: [] for name in wanted}

    receipts: dict[str, tuple[datetime, Mapping[str, Any]]] = {}
    runs_by_account: dict[str, list[tuple[str, str]]] = {}
    sessions: dict[str, tuple[str, str, datetime]] = {}
    for row in [*rows, *(record for record in live if isinstance(record, Mapping))]:
        if not isinstance(row, Mapping):
            continue
        name = budget._run_backend(row)
        if name not in wanted:
            continue
        run_id = str(row.get("run_id") or "").strip()
        if run_id:
            runs_by_account.setdefault(name, []).append(
                (budget._run_order(row), run_id)
            )
        receipt = row.get("lane_receipt")
        if isinstance(receipt, Mapping):
            observed = budget._parse_stamp(receipt.get("observed_at"))
            if observed is not None and (
                name not in receipts or observed > receipts[name][0]
            ):
                receipts[name] = (observed, receipt)
    for record in live:
        if not isinstance(record, Mapping):
            continue
        name = budget._run_backend(record)
        if name not in wanted:
            continue
        session_id = str(record.get("session_id") or "").strip()
        observed = budget._run_observed_at(record)
        if not session_id or observed is None:
            continue
        order = budget._run_order(record)
        if name not in sessions or order > sessions[name][0]:
            sessions[name] = (order, session_id, observed)

    for name in sorted(wanted):
        receipt = receipts.get(name)
        if receipt is not None:
            reading = budget._receipt_reading(receipt[1], moment=now)
            if reading.known:
                by_account[name].append(Candidate(source="receipt", reading=reading))
        session = sessions.get(name)
        if session is not None:
            reading = budget._rollout_reading(
                budget._read_rollout(session[1], rollouts),
                observed_at=session[2],
                moment=now,
            )
            if reading.known:
                by_account[name].append(Candidate(source="rollout", reading=reading))
        stream = budget._newest_stream_reading(
            runs_by_account.get(name, ()), moment=now
        )
        if stream is not None:
            by_account[name].append(Candidate(source="stream", reading=stream))

    # A promoted or unrelated Codex run may have no live crew pointer at all.
    # Its client rollout still records the account's rate limits, so scan the
    # durable rollout homes and let the newest event for each account speak.
    session_backends = {
        str(row.get("session_id")): budget._run_backend(row)
        for row in [*rows, *live]
        if isinstance(row, Mapping) and row.get("session_id")
    }
    if rollouts is None:
        rollout_candidates = _codex_rollout_candidates(
            wanted,
            root=rollout_root or rollout_module.CLIENT_SESSIONS_DIR,
            moment=now,
            session_backends=session_backends,
        )
        for account, candidate in rollout_candidates.items():
            by_account.setdefault(account, []).append(candidate)
    return by_account


ROLLOUT_TAIL_BYTES = 1_048_576


def _codex_rollout_candidates(
    accounts: set[str],
    *,
    root: str | Path,
    moment: datetime,
    session_backends: Mapping[str, str],
) -> dict[str, Candidate]:
    """Read the newest in-window Codex rollout reading for each account."""
    from reckon import budget

    newest: dict[str, tuple[datetime, Candidate]] = {}
    base = Path(root).expanduser()
    if not base.is_dir():
        return {}
    for path in base.glob("*/*/*/rollout-*.jsonl"):
        try:
            age = (
                moment - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            ).total_seconds()
            if age > 7 * 24 * 3600:
                continue
        except OSError:
            continue
        latest: tuple[datetime, Mapping[str, Any]] | None = None
        session_id = ""
        try:
            with path.open("rb") as stream:
                first = stream.readline()
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - ROLLOUT_TAIL_BYTES))
                tail = stream.read().decode("utf-8", errors="ignore")
            lines = [first.decode("utf-8", errors="ignore"), *tail.splitlines()]
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                payload = record.get("payload")
                if record.get("type") == "session_meta" and isinstance(
                    payload, Mapping
                ):
                    session_id = str(
                        payload.get("session_id") or payload.get("id") or ""
                    )
                if not isinstance(payload, Mapping):
                    continue
                limits = payload.get("rate_limits")
                stamp = budget._parse_stamp(record.get("timestamp"))
                if (
                    isinstance(limits, Mapping)
                    and stamp is not None
                    and (latest is None or stamp > latest[0])
                ):
                    latest = (stamp, limits)
        except (OSError, UnicodeError):
            continue
        if latest is None:
            continue
        observed, limits = latest
        account = str(limits.get("limit_id") or "").strip()
        profile = session_backends.get(session_id, "")
        # Profiles share a provider account in the raw object.  When a run
        # explicitly names a configured Codex profile, retain that account key
        # so the document reflects the backend the run actually used.
        if profile.startswith("codex") and profile in accounts:
            account = profile
        if account not in accounts:
            continue
        if (moment - observed).total_seconds() > 7 * 24 * 3600:
            continue
        reading = budget._rate_limits_reading(
            limits, observed_at=observed, moment=moment
        )
        if not reading.known:
            continue
        candidate = Candidate(source="rollout", reading=reading)
        if account not in newest or observed > newest[account][0]:
            newest[account] = (observed, candidate)
    return {account: candidate for account, (_stamp, candidate) in newest.items()}


def systemd_user_dir() -> Path:
    """The directory the user's systemd units are read from.

    Resolved the way systemd itself resolves it: ``XDG_CONFIG_HOME`` when the
    user set one, otherwise ``~/.config``. The installer writes here so the
    units land where the manager looks for them, and a caller that isolates
    ``XDG_CONFIG_HOME`` runs against its own directory rather than the
    operator's -- which is what lets a test exercise the install without ever
    reaching the real user manager.
    """
    base = os.environ.get(XDG_CONFIG_HOME_ENV)
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


def checkout_root() -> Path:
    """The checkout the running module is imported from.

    Derived from this file's own location rather than the interpreter's, so a
    unit installed from a worktree still names the tree whose code it will run.
    """
    return Path(__file__).resolve().parents[2]


def render_service_unit(
    executable: str | Path | None = None,
    *,
    root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render the oneshot service that publishes the document one time.

    ``ExecStart`` is the interpreter that ran the install with ``-m
    reckon.crew.paid_lanes --once``, so the timer runs the checkout's own code
    under the environment the command was invoked from. ``WorkingDirectory`` is
    the checkout root, which puts that tree on the module search path even
    where the package is not installed into the interpreter's environment.
    """
    interpreter = str(executable or sys.executable)
    working_directory = str(root or checkout_root())
    argv = [interpreter, "-m", "reckon.crew.paid_lanes", "--once"]
    forwarded = dict(environment or {})
    home = os.environ.get(RECKON_HOME_ENV)
    if home:
        forwarded.setdefault(RECKON_HOME_ENV, str(Path(home).expanduser().resolve()))
    override = "".join(
        f'Environment="{name}={value}"\n' for name, value in sorted(forwarded.items())
    )
    return SERVICE_TEMPLATE.format(
        working_directory=working_directory,
        environment=override,
        exec_start=" ".join(shlex.quote(part) for part in argv),
    )


def render_timer_unit(*, service: str = SERVICE_NAME) -> str:
    """Render the timer that activates the publish service on its own clock."""
    return TIMER_TEMPLATE.format(
        boot_delay=BOOT_DELAY, interval=REFRESH_INTERVAL, service=service
    )


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only when it differs; report whether it did.

    The deployment is idempotent: an unchanged definition is left untouched, so
    a re-run neither rewrites the file nor gives the manager a reason to reload.
    The comparison is on the rendered bytes, which is what systemd reads.
    """
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def install_timer(
    executable: str | Path | None = None,
    *,
    directory: str | Path | None = None,
    run: Callable[[Sequence[str]], Any] | None = None,
) -> dict[str, Any]:
    """Install the refresh service and timer, then bring the timer up.

    Two units are written under the user's systemd directory and, only when the
    definition actually changed, the manager is reloaded and the timer enabled
    and started. An unchanged re-run writes nothing and calls nothing, so
    installing repeatedly is free and leaves a running timer untouched. The
    manager is reached through ``systemctl --user`` on PATH, so a caller can
    substitute a stub by putting one earlier on PATH.
    """
    from reckon import service

    target = Path(directory) if directory is not None else systemd_user_dir()
    service_path = target / SERVICE_NAME
    timer_path = target / TIMER_NAME
    changed = _write_if_changed(service_path, render_service_unit(executable))
    changed = _write_if_changed(timer_path, render_timer_unit()) or changed
    commands: list[list[str]] = []
    if changed:
        command = run or (lambda args: service.systemctl(*args))
        for args in (("daemon-reload",), ("enable", "--now", TIMER_NAME)):
            command(list(args))
            commands.append(list(args))
    return {
        "changed": changed,
        "service": service_path,
        "timer": timer_path,
        "commands": commands,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Compose and publish the document; ``--once`` writes it a single time.

    ``--help`` and ``-h`` print the usage and return without composing or
    writing anything, because asking what the command does must not be the same
    act as running it. ``argv`` defaults to the process's own command line, so
    invoking the module as ``python -m reckon.crew.paid_lanes --once --path
    <file>`` publishes where it was asked to rather than to the default
    location; a caller passing a list supplies the same tokens it would have
    typed.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    path: str | None = None
    project: str | None = None
    root: str | None = None
    help_requested = False
    install_requested = False
    index = 0
    while index < len(args):
        flag = args[index]
        if flag in ("--path", "--project", "--checkout-path"):
            index += 1
            value = args[index] if index < len(args) else None
            if flag == "--path":
                path = value
            elif flag == "--project":
                project = value
            else:
                root = value
        elif flag in ("--help", "-h"):
            help_requested = True
        elif flag == "--install-timer":
            install_requested = True
        elif flag == "--once":
            pass
        else:
            path = flag
        index += 1

    if help_requested:
        sys.stdout.write(USAGE)
        return 0

    if install_requested:
        from reckon import service

        try:
            result = install_timer()
        except service.ServiceError as exc:
            print(f"could not install the refresh timer: {exc}")
            return 1
        if result["changed"]:
            print(f"installed {result['timer']}")
        else:
            print(f"{result['timer']} is already current")
        return 0

    from reckon import flight

    try:
        config = flight.resolve(project, checkout_path=root).config
    except flight.FlightConfigError as exc:
        print(f"no flight config to read accounts from: {exc}")
        return 1
    accounts = sorted(str(name) for name in (config.get("backends") or {}))
    moment = datetime.now(tz=UTC)
    sources = (
        gather_sources(accounts, project=project, root=root, moment=moment)
        if accounts and project
        else {}
    )
    document = compose_document(
        accounts,
        sources=sources,
        moment=moment,
        local_lane=read_local_lane(moment=moment),
    )
    written = write_document_atomically(document, path)
    print(f"wrote {written}: {len(document['accounts'])} account(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
