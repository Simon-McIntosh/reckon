"""Census of carried monitor rows, and a sampler for duties published while a review runs.

The Monitor prints a row only when it names something the coordinator oversees,
so the rows this census reclassifies are exactly the ones a coordinator was
shown. Each transition in every project's watch stream is reclassified through
:func:`reckon.crew.ticker.stream_carries` and
:func:`reckon.crew.ticker._pane_word`; the census counts the carried rows, and
among the carried *halted* rows it counts three kinds, one per mechanism that
drew a live worker as halted.

The three halted kinds are read from the run's own directory beside the stream,
because a stream row alone cannot tell them apart:

* ``process_alive`` — the row's own event records ``process_alive: true`` while
  its destination draws halted. The worker was provably running when the row
  was written.
* ``returns_to_progress`` — a later row for the same run draws ``working``,
  ``delivered``, ``in review`` or ``landed``, and no attempt claim falls between
  the halted row and that progress row. A claim between them means a new
  attempt ran and could have produced the halt itself, so the row is a real
  halt rather than a verdict read through records that no longer described the
  run.
* ``after_status_repair`` — the row was printed within
  :data:`REPAIR_WINDOW_SECONDS` after the run's ``status-repair.json``
  ``repaired_at``, with no attempt claim between the repair and the row. A run
  can halt on its own after a repair only if an attempt ran, and an attempt
  writes a claim, so a row with no claim between is the repair's own
  transition.

Claim times are compared at second granularity at **both** bounds, because the
stream records ``observed_at`` to the second and a claim carries milliseconds:
a claim whose truncated time equals either bound's second is *not* between. An
attempt claimed in the same second as a row cannot have run, so it cannot have
produced the halt; reading the milliseconds instead would excuse a row in the
same second its attempt was claimed.

The census also counts budget-lift transitions per lift id and same-word
``waiting -> waiting`` rows, the two repeating-noise mechanisms this plan
repaired.

The second half of the module is the review-missing sampler. The obligations
view publishes ``review-missing`` from a session's snapshot, and a review that
is already live for the run makes that duty wrong. The defect cannot be counted
after the fact — the snapshot is overwritten in place — so the sampler reads
every session's snapshot on a fixed cadence (at most once a minute), and for
each ``review-missing`` duty it resolves the review standing for the reviewed
run's own live pointer, which is the question that gates the duty when it is
published. A run the reflex dispatched a review for is answered from the
reflex's own recorded dispatch; every other run is answered by
:func:`reckon.crew.obligations._in_flight_review` over the live pointers that
have launched. A duty counts as the defect only when a review stands for it and
the snapshot's ``computed_at`` is at or after that review pointer's
``launched_at``. A snapshot computed before the launch and read after it is the
sampler's ``stale_snapshot`` reading — a fact about the snapshot's age rather
than a defect in the publisher — and a review pointer with no launch time yet
reads ``review_not_launched``.

The sampler is not started by the node that commits it: a crew worker gets no
turn after its last one and its background tasks end at a 600-second ceiling,
so a day-long job started inside it would die with the turn. ``--once`` is for
a test or a manual probe; the operator runs the sampler as one bounded job from
the main checkout::

    python -m reckon.crew.live_worker_census --sample-review-missing \\
        --interval 60 --samples <path>

The module is a normal package member rather than a script loaded by path, so
the gate collects its test and the report node imports the census by name.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import obligations, query, recovery, ticker
from reckon.crew.run_paths import crew_home
from reckon.crew.runs import (
    STATUS_REPAIR_RECORD_NAME,
    pointer_path,
    runs_dir,
)

#: The pane word that reads as halted. The pane names that word for its states
#: that ask the coordinator for a move, so the census takes it from the ticker's
#: own mapping of a blocked run rather than spelling whichever word it uses. A
#: word written by an earlier pane is read in as the current one by that same
#: mapping, so rows produced before a rename are still counted.
HALTED_PANE_WORD = ticker._display_state("blocked")

#: The pane words a row may draw after a halted row and still describe the run
#: moving again. ``landed`` is the word a promoted or recorded row draws, and
#: ``in review`` is a delivered run whose review has started.
PROGRESS_PANE_WORDS = frozenset({"working", "delivered", "in review", "landed"})

#: How long after a recorded status repair a halted row is still read as the
#: repair's own transition.
REPAIR_WINDOW_SECONDS = 60.0

#: The obligations duty kind a review already standing for a run contradicts.
REVIEW_MISSING_KIND = "review-missing"

#: The phase a review pointer carries before its worker has launched.
QUEUED_PHASE = "queued"

#: The states a budget-lift transition is counted in. A row in any other state
#: carries no transition of this kind, whatever lift id it names.
COUNTED_LIFT_STATES = (
    ticker.LIFT_GRANTED,
    ticker.LIFT_ENDED,
    ticker.LIFT_IN_FORCE,
)

#: The duty keys a snapshot entry carries. A duty entry is not a pointer, so the
#: sampler loads the reviewed run's own pointer by id before asking the reader.
DUTY_KEYS = ("run_id", "head", "kind")

#: The default cadence. The plan fixes at most one sample a minute.
SAMPLER_INTERVAL_SECONDS = 60.0

#: The default sample log. It lives on GPFS so samples outlive the run that
#: took them.
DEFAULT_SAMPLES_PATH = (
    Path.home()
    / ".cache/reckon-population/a-live-worker-census/review-missing-samples.jsonl"
)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _second(instant: datetime) -> int:
    """The whole second an instant falls in, which is the stream's own grain."""
    return int(instant.timestamp())


def _word_of_state(state: Any) -> str:
    """The pane word a bare state draws, read through the ticker's own mapping."""
    return ticker._pane_word({"to_state": state})


def _attempt_claims(run_path: Path) -> list[datetime]:
    """Every attempt claim's mtime in a run's directory, as an instant.

    A claim is the durable evidence that an attempt was launched: the file is
    created exclusively by the launch that takes its attempt number, so its
    mtime is the instant the attempt was reserved. A retired claim is renamed
    and no longer matches ``attempt-N.claim``.
    """
    claims: list[datetime] = []
    for path in sorted(run_path.glob("attempt-*.claim")):
        try:
            stat = path.stat()
        except OSError:
            continue
        claims.append(datetime.fromtimestamp(stat.st_mtime, tz=UTC))
    return claims


def _claim_between(claims: Iterable[datetime], start: datetime, end: datetime) -> bool:
    """Whether an attempt claim sits strictly between two observed instants.

    Both bounds are compared at second granularity, and strictly on each side:
    a claim in the same second as either bound is not between, because an
    attempt claimed in the same second as a row cannot have run.
    """
    start_second = _second(start)
    end_second = _second(end)
    return any(start_second < _second(claim) < end_second for claim in claims)


def _status_repair_instant(run_path: Path) -> datetime | None:
    """The instant a run's status repair was recorded, or None when it has none."""
    try:
        record = json.loads(
            (run_path / STATUS_REPAIR_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    return parse_utc(record.get("repaired_at"))


def _event_instant(event: Mapping[str, Any]) -> datetime | None:
    return parse_utc(event.get("observed_at"))


def _iter_events(
    paths: Iterable[Path], since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Read every transition event in the window, ordered by its observed time."""
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"observed_at"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") not in ("transition", None):
                    continue
                instant = _event_instant(event)
                if instant is None or not since <= instant <= until:
                    continue
                event["_observed"] = instant
                rows.append(event)
    rows.sort(key=lambda event: event["_observed"])
    return rows


def _later_progress(
    events: Sequence[Mapping[str, Any]], start: int
) -> Mapping[str, Any] | None:
    """The first row after ``start`` whose pane word is a progress word."""
    for event in events[start + 1 :]:
        if ticker._pane_word(event) in PROGRESS_PANE_WORDS:
            return event
    return None


def _row_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    """The facts a counted row is reported with, so a reader can find it."""
    instant = event.get("_observed")
    return {
        "run_id": str(event.get("run_id") or ""),
        "project": str(event.get("project") or ""),
        "node": str(event.get("node") or ""),
        "from_state": str(event.get("from_state") or ""),
        "to_state": str(event.get("to_state") or ""),
        "observed_at": _iso(instant) if isinstance(instant, datetime) else "",
    }


def census_streams(
    *,
    since: datetime,
    until: datetime,
    watch_dir: str | Path | None = None,
    runs_root: str | Path | None = None,
) -> dict[str, Any]:
    """Reclassify every project's watch events in ``[since, until]`` and count them.

    ``watch_dir`` defaults to the live fleet's own watch directory and
    ``runs_root`` to its run directory, so the caller measures the fleet by
    default and a fixture by passing its own. ``watch_dir`` names the ``watch``
    directory inside a crew home, which is how the streams are enumerated.
    """
    watch = Path(watch_dir) if watch_dir is not None else crew_home() / "watch"
    root = Path(runs_root) if runs_root is not None else runs_dir()
    events = _iter_events(query.watch_event_paths(home=watch.parent), since, until)

    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_run[str(event.get("run_id") or "")].append(event)

    carried: dict[str, int] = defaultdict(int)
    halted_total = 0
    kind_alive: dict[str, int] = defaultdict(int)
    kind_progress: list[dict[str, Any]] = []
    kind_repair: list[dict[str, Any]] = []
    lift_transitions: dict[str, dict[str, int]] = defaultdict(
        lambda: {"granted": 0, "ended": 0, "in_force": 0}
    )
    same_word_waits: list[dict[str, Any]] = []

    claims_by_run: dict[str, list[datetime]] = {}
    repairs_by_run: dict[str, datetime | None] = {}

    def claims_for(run_id: str) -> list[datetime]:
        if run_id not in claims_by_run:
            claims_by_run[run_id] = _attempt_claims(root / run_id) if run_id else []
        return claims_by_run[run_id]

    def repair_for(run_id: str) -> datetime | None:
        if run_id not in repairs_by_run:
            repairs_by_run[run_id] = (
                _status_repair_instant(root / run_id) if run_id else None
            )
        return repairs_by_run[run_id]

    for run_id, run_events in by_run.items():
        for index, event in enumerate(run_events):
            word = ticker._pane_word(event)
            if _word_of_state(event.get("from_state")) == "waiting" and word == (
                "waiting"
            ):
                same_word_waits.append(_row_summary(event))

            to_state = str(event.get("to_state") or "")
            if to_state in COUNTED_LIFT_STATES and event.get("lift_id"):
                lift = lift_transitions[str(event["lift_id"])]
                if to_state == ticker.LIFT_GRANTED:
                    lift["granted"] += 1
                elif to_state == ticker.LIFT_ENDED:
                    lift["ended"] += 1
                elif to_state == ticker.LIFT_IN_FORCE:
                    lift["in_force"] += 1

            if not ticker.stream_carries(event):
                continue
            carried[f"{event.get('project') or ''}:{word}"] += 1
            if word != HALTED_PANE_WORD:
                continue
            halted_total += 1
            halted_at = event["_observed"]

            if event.get("process_alive") is True:
                key = f"{event.get('from_state')}->{event.get('to_state')}"
                kind_alive[key] += 1

            progress = _later_progress(run_events, index)
            if progress is not None and not _claim_between(
                claims_for(run_id), halted_at, progress["_observed"]
            ):
                row = _row_summary(event)
                row["progress"] = _row_summary(progress)
                kind_progress.append(row)

            repaired_at = repair_for(run_id)
            if repaired_at is not None:
                delay = (halted_at - repaired_at).total_seconds()
                if 0 <= delay <= REPAIR_WINDOW_SECONDS and not _claim_between(
                    claims_for(run_id), repaired_at, halted_at
                ):
                    row = _row_summary(event)
                    row["repaired_at"] = _iso(repaired_at)
                    kind_repair.append(row)

    return {
        "window": {"since": _iso(since), "until": _iso(until)},
        "watch_dir": str(watch),
        "runs_root": str(root),
        "events": len(events),
        "runs": len(by_run),
        "carried_rows": dict(sorted(carried.items())),
        "halted_rows": halted_total,
        "kinds": {
            "process_alive": {
                "count": sum(kind_alive.values()),
                "by_transition": dict(sorted(kind_alive.items())),
            },
            "returns_to_progress": {"count": len(kind_progress), "rows": kind_progress},
            "after_status_repair": {"count": len(kind_repair), "rows": kind_repair},
        },
        "lift_transitions": dict(sorted(lift_transitions.items())),
        "same_word_wait_rows": {"count": len(same_word_waits), "rows": same_word_waits},
    }


# ── Review-missing sampler ──────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _snapshot_documents(home: Path) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Yield ``(project_token, session, document)`` for every stored snapshot."""
    for directory in sorted((home / "obligations").glob("*")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            document = _read_json(path)
            if document is None:
                continue
            yield directory.name, path.stem, document


def _reflex_review_pointer(pointer: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The review the reflex recorded for this run, when that review is live.

    The reflex's own record names the review run it launched, and it is the
    reviewer's pointer that carries the launch time a duty is judged against.
    A recorded review whose pointer is gone reads as no review, which is the
    rule the reflex and the obligations view both apply.
    """
    recorded = pointer.get(recovery.REVIEW_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return None
    review_run_id = str(recorded.get("run_id") or "")
    if not review_run_id:
        return None
    return _read_json(pointer_path(review_run_id))


def _matching_review_pointer(
    pointer: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The live review pointer standing for a run at its current head, or None.

    A run the reflex dispatched a review for is answered from that record: the
    reflex's claim is dated from its own launch window and is not re-judged
    against the revision the run carries now, so only the record names the
    review while the sweep would find none. Every other run is resolved by the
    obligations view's own rule, over the candidates that have launched, so a
    sampler and the publisher cannot disagree about which review covers a duty.
    """
    if obligations._reflex_review_in_flight(pointer):
        return _reflex_review_pointer(pointer)
    project = str(pointer.get("project") or "")
    candidates = [
        candidate
        for candidate in obligations._live_pointers(project)
        if str(candidate.get("phase") or "") != QUEUED_PHASE
    ]
    review_run_id = obligations._in_flight_review(pointer, candidates)
    if not review_run_id:
        return None
    for candidate in candidates:
        if str(candidate.get("run_id") or "") == review_run_id:
            return candidate
    return None


def sample_once(
    *,
    samples_path: str | Path | None = None,
) -> dict[str, Any]:
    """Take one sample of every session's snapshot and count what it published.

    Every record is read through the crew home, which ``RECKON_HOME`` names: the
    snapshots, the reviewed run's pointer and the review pointer are all
    resolved by the same readers the publisher uses, so a sample taken against
    another home reads only that home.

    Each ``review-missing`` duty is judged by the reader that gates the duty
    when it is published. A duty whose review is not in flight counts nothing.
    A duty whose review is in flight counts as the defect when the snapshot was
    computed at or after that review's launch, as ``stale_snapshot`` when it was
    computed before the launch, and as ``review_not_launched`` when the review
    pointer carries no launch time yet — the review is recorded and standing,
    so its snapshot is not being read too late. Every counted pair copies the
    snapshot entry and the matching pointer into the sample log.
    """
    base = crew_home()
    samples = Path(samples_path) if samples_path is not None else DEFAULT_SAMPLES_PATH
    counts = {
        "snapshots": 0,
        "review_missing_duties": 0,
        "live_review": 0,
        "defect": 0,
        "stale_snapshot": 0,
        "review_not_launched": 0,
        "unreadable": 0,
    }
    samples_taken: list[dict[str, Any]] = []

    for project_token, session, document in _snapshot_documents(base):
        counts["snapshots"] += 1
        computed_at = parse_utc(document.get("computed_at"))
        for duty in document.get("obligations") or ():
            if not isinstance(duty, Mapping):
                continue
            if str(duty.get("kind") or "") != REVIEW_MISSING_KIND:
                continue
            counts["review_missing_duties"] += 1
            run_id = str(duty.get("run_id") or "")
            pointer = _read_json(pointer_path(run_id)) if run_id else None
            if pointer is None:
                counts["unreadable"] += 1
                continue
            review_pointer = _matching_review_pointer(pointer)
            if review_pointer is None:
                continue
            counts["live_review"] += 1
            launched_at = parse_utc(review_pointer.get("launched_at"))
            entry = {
                "sampled_at": _iso(datetime.now(tz=UTC)),
                "project_token": project_token,
                "session": session,
                "snapshot_computed_at": _iso(computed_at)
                if computed_at is not None
                else None,
                "duty": {key: duty.get(key) for key in DUTY_KEYS},
                "review_pointer": dict(review_pointer) if review_pointer else None,
            }
            if launched_at is None:
                counts["review_not_launched"] += 1
                entry["reading"] = "review_not_launched"
            elif computed_at is not None and computed_at >= launched_at:
                counts["defect"] += 1
                entry["reading"] = "defect"
            else:
                counts["stale_snapshot"] += 1
                entry["reading"] = "stale_snapshot"
            samples_taken.append(entry)

    if samples_taken:
        samples.parent.mkdir(parents=True, exist_ok=True)
        with samples.open("a", encoding="utf-8") as handle:
            for entry in samples_taken:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")

    counts["samples_path"] = str(samples)
    return counts


def run_sampler(
    *,
    samples_path: str | Path | None = None,
    interval: float = SAMPLER_INTERVAL_SECONDS,
    iterations: int | None = None,
) -> int:
    """Sample on a fixed cadence, forever or for ``iterations`` samples."""
    taken = 0
    while True:
        counts = sample_once(samples_path=samples_path)
        print(
            "review-missing sample: defects {defect} stale {stale_snapshot} "
            "not-launched {review_not_launched} "
            "duties {review_missing_duties}".format(**counts),
            flush=True,
        )
        taken += 1
        if iterations is not None and taken >= iterations:
            return 0
        time.sleep(interval)


# ── Command line ────────────────────────────────────────────────────────────


def _window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    until = parse_utc(args.until) if args.until else datetime.now(tz=UTC)
    if until is None:
        raise SystemExit(f"could not read --until {args.until!r}")
    if args.since:
        since = parse_utc(args.since)
        if since is None:
            raise SystemExit(f"could not read --since {args.since!r}")
        return since, until
    return until - timedelta(hours=float(args.hours)), until


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reclassify every project's carried monitor rows over a window, or "
            "sample the obligations snapshots for review-missing duties whose "
            "review is already running."
        ),
    )
    parser.add_argument("--since", default=None, help="window start (ISO instant)")
    parser.add_argument("--until", default=None, help="window end (ISO instant)")
    parser.add_argument(
        "--hours", default=48.0, help="window length when --since is omitted"
    )
    parser.add_argument("--watch-dir", default=None)
    parser.add_argument("--runs-dir", default=None)
    parser.add_argument("--out", default=None, help="write the census JSON here")
    parser.add_argument(
        "--sample-review-missing",
        action="store_true",
        help="run the review-missing sampler instead of the stream census",
    )
    parser.add_argument("--once", action="store_true", help="take one sample and exit")
    parser.add_argument("--interval", default=SAMPLER_INTERVAL_SECONDS, type=float)
    parser.add_argument("--iterations", default=None, type=int)
    parser.add_argument("--samples", default=None, help="sample log path")
    args = parser.parse_args(argv)

    if args.sample_review_missing:
        if args.once:
            counts = sample_once(samples_path=args.samples)
            print(json.dumps(counts, indent=2, sort_keys=True))
            return 0
        return run_sampler(
            samples_path=args.samples,
            interval=args.interval,
            iterations=args.iterations,
        )

    since, until = _window(args)
    census = census_streams(
        since=since,
        until=until,
        watch_dir=args.watch_dir,
        runs_root=args.runs_dir,
    )
    text = json.dumps(census, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text, end="")
    kinds = census["kinds"]
    print(
        "carried halted rows {halted}; process_alive {a}; returns_to_progress "
        "{b}; after_status_repair {c}".format(
            halted=census["halted_rows"],
            a=kinds["process_alive"]["count"],
            b=kinds["returns_to_progress"]["count"],
            c=kinds["after_status_repair"]["count"],
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
