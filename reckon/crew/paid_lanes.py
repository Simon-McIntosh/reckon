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

OBSERVED = "observed"
UNKNOWN = "unknown"

#: What ``--help`` and ``-h`` print. Asking what the command does must never be
#: the same act as running it, so the text is emitted and the run stops here.
USAGE = """\
usage: python -m reckon.crew.paid_lanes [--once] [--path PATH]
       [--project PROJECT] [--checkout-path PATH]

Compose the metered-backend headroom document, one entry per account, and
write it atomically. The document is what the pre-flight reads between
dispatches rather than only at a refusal.

options:
  --once                 publish the document one time and exit (the default)
  --path PATH            write to PATH instead of the default location
  --project PROJECT      read accounts from PROJECT's resolved flight config
  --checkout-path PATH   resolve PROJECT's config relative to PATH
  -h, --help             print this message and exit without publishing
"""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One source's reading for one account, with the source named."""

    source: str
    reading: window_reading.WindowReading


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
) -> dict[str, list[Candidate]]:
    """Gather each account's candidate readings from the three recorded homes.

    The homes are the ones the pre-flight already reads: a run's committed (or
    live) ``lane_receipt``, the session rollout of an in-flight run, and the
    stream a served run reported its windows on. Each is returned as a named
    candidate so the document can say which source it used -- a thing a single
    reconciled reading could not say.
    """
    from reckon import budget, crew, ledger

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
    return by_account


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
        elif flag == "--once":
            pass
        else:
            path = flag
        index += 1

    if help_requested:
        sys.stdout.write(USAGE)
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
    document = compose_document(accounts, sources=sources, moment=moment)
    written = write_document_atomically(document, path)
    print(f"wrote {written}: {len(document['accounts'])} account(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
