"""Census of the crew lane over a closed window.

Reads three instruments - the run ledger, the per-run landing records and the
admission verdict log - and reports, for each window it is given, how many
workers were working, how long a completed implement run waited to be
promoted, whether an admit pass held a request on a claim under a shared root,
and how many worker identity files the claims directory held.

    python scripts/lane_window_census.py --window 2026-11-14T21:00Z/2026-11-15T01:00Z
    python scripts/lane_window_census.py --baseline
    python scripts/lane_window_census.py --negative-control

Every figure is reported beside the population it was computed over, so an
empty result reads as empty rather than as a clean number.

Both admission instruments are bounded -- the crew-home verdict log to its last
mebibyte and the admit service's log to its last four -- so a four-hour window
with a busy queue outruns either. A census run after such a window reads a copy
taken while the window was open, by pointing ``--spool-to`` at a spool file and
repeating the command through the window; ``--admission-log`` reads a pass
record from somewhere else entirely, such as a preserved snapshot:

    python scripts/lane_window_census.py --window START/END --spool-to /run/spool.jsonl
    python scripts/lane_window_census.py --window START/END --admission-log SNAPSHOT
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import json
import os
import re
import statistics
import sys
from pathlib import Path

CREW_HOME = Path(
    os.environ.get("RECKON_CREW_HOME", "~/.config/reckon/crew")
).expanduser()
ADMISSION_LOG = CREW_HOME / "admission-verdicts.jsonl"
CLAIMS_DIR = CREW_HOME / "claims"
RUN_DIRS = (CREW_HOME / "runs", CREW_HOME / "archive" / "runs")

# The admit service prints the same pass record the crew-home verdict log keeps,
# and its own log is bounded larger than that log is, so it is the default
# companion source when a window is copied to a spool while it is open. The home
# is taken from the layout of the crew home rather than from ``~``, because a
# run's environment may point HOME at its own sandbox while the crew home it
# reads is the operator's.
_LAYOUT_HOME = CREW_HOME.parents[2] if len(CREW_HOME.parents) > 2 else CREW_HOME
SERVICE_LOG = (
    _LAYOUT_HOME
    / ".local"
    / "state"
    / "fleet"
    / "services"
    / ("reckon-admit-local.log")
)

WINDOW = ("2026-11-14T21:00:00+00:00", "2026-11-15T01:00:00+00:00")
BASELINE = ("2026-11-07T00:00:00+00:00", "2026-11-12T00:00:00+00:00")
NEGATIVE_CONTROL = ("2026-11-11T13:00:00+00:00", "2026-11-11T15:00:00+00:00")


IDENTITY_FILE = re.compile(r"^exec-\d+\.identity$")


def parse_instant(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def parse_window(text: str) -> tuple[dt.datetime, dt.datetime]:
    start, _, end = text.partition("/")
    return parse_instant(start), parse_instant(end)


def role_of(payload: dict, node: str) -> str:
    role = payload.get("role")
    if role:
        return role
    return (
        "review"
        if str(node).startswith(("review-of", "plan-review-of"))
        else "implement"
    )


def load_runs(
    path: Path | None = None, *, projects: set[str] | None = None
) -> tuple[list[dict], dict]:
    """Every run the store holds, hot and retired, flattened for the figures.

    Rows come from the store's own readers rather than from raw SQL, so the
    store decides where a run lives. ``records()`` answers the hot rows, and
    ``durable_rows()`` answers one project's rows across both databases, so
    retirement's archive file is read as well: a window older than the hot
    table would otherwise read as an empty one rather than as an unmeasured one.
    """
    from reckon import flight, run_store

    store_file = Path(path) if path is not None else run_store.store_path()
    if not store_file.exists():
        # The store creates itself on open, so an absent one would otherwise
        # report every figure as zero rather than as unmeasured.
        raise SystemExit(f"the run ledger is not at {store_file}")
    with run_store.RunStore(store_file) as store:
        hot = store.records()
        if projects is None:
            projects = {
                str(record["project"])
                for record in hot.values()
                if record.get("project")
            }
            with contextlib.suppress(flight.FlightConfigError):
                projects |= set(flight.mounted_project_docs())
        stored: dict[str, dict] = {}
        for project in sorted(projects):
            stored.update(store.durable_rows(project, refresh=False))
    merged = {**stored, **hot}
    runs = []
    for run_id, data in merged.items():
        node = data.get("node")
        worker = data.get("worker_exit") or {}
        runs.append(
            {
                "run_id": run_id,
                "project": data.get("project"),
                "node": node,
                "role": role_of(data, node),
                "dispatched_at": data.get("dispatched_at"),
                "completed_at": data.get("completed_at"),
                "launched_at": worker.get("launched_at"),
                "exited_at": worker.get("exited_at"),
            }
        )
    return runs, {
        "run_store": str(store_file),
        "runs_in_ledger": len(hot),
        "runs_in_archive": len(merged) - len(hot),
        "runs_read": len(merged),
    }


def worker_spans(runs: list[dict]) -> list[tuple[dt.datetime, dt.datetime, dict]]:
    """One span per run: its worker process alive, else its dispatch to completion.

    The worker's own launch and exit bound the time it could have been
    working. The ledger's dispatch and completion stamps bracket a wider
    interval that also carries queueing and promotion, so they are only the
    fallback for a run whose worker exit was never recorded.
    """
    spans = []
    for run in runs:
        start, end = run["launched_at"], run["exited_at"]
        try:
            if not (start and end):
                start, end = run["dispatched_at"], run["completed_at"]
            if not (start and end):
                continue
            first, last = parse_instant(start), parse_instant(end)
        except (TypeError, ValueError):
            continue
        if last > first:
            spans.append((first, last, run))
    spans.sort(key=lambda span: span[0])
    return spans


def concurrency(spans, start: dt.datetime, end: dt.datetime) -> dict:
    """Time-weighted mean and peak of overlapping spans inside the window."""
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise SystemExit("the window ends before it starts")
    events = []
    involved = 0
    for first, last, _run in spans:
        if last <= start or first >= end:
            continue
        involved += 1
        events.append((max(first, start), 1))
        events.append((min(last, end), -1))
    events.sort(key=lambda pair: (pair[0], pair[1]))
    area = 0.0
    live = 0
    peak = 0
    previous = start
    for when, delta in events:
        area += live * (when - previous).total_seconds()
        previous = when
        live += delta
        peak = max(peak, live)
    return {"mean": round(area / seconds, 2), "peak": peak, "workers": involved}


def landing_stamps() -> dict[str, dt.datetime]:
    """The time each run's landing was committed, from its landing record."""
    stamps: dict[str, dt.datetime] = {}
    for base in RUN_DIRS:
        if not base.is_dir():
            continue
        for name in os.listdir(base):
            path = base / name / "landing.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            record = data[0] if isinstance(data, list) and data else data
            if not isinstance(record, dict) or not record.get("time"):
                continue
            try:
                stamps.setdefault(name, parse_instant(record["time"]))
            except ValueError:
                continue
    return stamps


def promotion_latency(
    runs: list[dict],
    stamps: dict[str, dt.datetime],
    start: dt.datetime,
    end: dt.datetime,
) -> dict:
    """Completion-to-promotion seconds for the implement runs completed in the window.

    The cohort is the implement runs that completed inside the window, so two
    windows compare the same population. A landing record survives only while
    its run directory survives, so a run whose directory was reclaimed reports
    as having no record; that count is reported beside the gaps rather than
    folded into them.
    """
    gaps = []
    clamped = 0
    cohort = 0
    absent = 0
    for run in runs:
        if run["role"] != "implement":
            continue
        completed = run["completed_at"]
        if not completed:
            continue
        try:
            moment = parse_instant(completed)
        except ValueError:
            continue
        if not (start <= moment < end):
            continue
        cohort += 1
        promoted = stamps.get(run["run_id"])
        if not promoted:
            absent += 1
            continue
        gap = (promoted - moment).total_seconds()
        if gap < 0:
            clamped += 1
            gap = 0.0
        gaps.append(gap)
    gaps.sort()
    if not gaps:
        return {
            "cohort": cohort,
            "n": 0,
            "no_landing_record": absent,
            "median_seconds": None,
            "p90_seconds": None,
            "clamped_negative": clamped,
        }
    p90 = gaps[min(len(gaps) - 1, round(0.9 * (len(gaps) - 1)))]
    return {
        "cohort": cohort,
        "n": len(gaps),
        "no_landing_record": absent,
        "median_seconds": round(statistics.median(gaps), 1),
        "p90_seconds": round(p90, 1),
        "clamped_negative": clamped,
    }


def shared_root(path_text: str) -> bool:
    """Whether a claimed path sits under the crew home every node shares."""
    if not path_text:
        return False
    path = Path(path_text).expanduser()
    return str(path).startswith(str(CREW_HOME) + os.sep)


# A path in a hold's detail is written with repr(), which chooses the quote
# style from the path itself: a path holding an apostrophe makes repr quote it
# with double quotes. Matching a whole repr token after the wording that names
# the path keeps it intact either way, where splitting on the quote character
# shifts every later field and the hold then reads as unshared, silently.
_REPR_QUOTED = r"""(?:'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
_HELD_PATH = re.compile(rf"\bholds\s+({_REPR_QUOTED})")
_CLAIMED_PATH = re.compile(rf"\blive claim\s+({_REPR_QUOTED})")
_CLAIMING_PATH = re.compile(rf"\bclaiming\s+({_REPR_QUOTED})")


def claimed_path(detail: str) -> str:
    """The claimed path an admission hold's detail names, if it names one.

    Three wordings reach a hold pointer: a run holding a path, a write path
    overlapping a live claim, and an accepted directory claim naming the path
    it took over. Each names the path after its own words, so the wording is
    what selects it rather than the position of a quote character.
    """
    for pattern in (_HELD_PATH, _CLAIMED_PATH, _CLAIMING_PATH):
        match = pattern.search(detail or "")
        if match:
            try:
                return str(ast.literal_eval(match.group(1)))
            except (ValueError, SyntaxError):
                return ""
    return ""


def run_owned(path_text: str) -> bool:
    """Whether a crew-home path lies inside one run's own directory.

    The crew home holds paths every run writes into and paths exactly one run
    owns, and a claim holds a request on the second kind while it must not on
    the first. Reported beside the broad crew-home reading rather than replacing
    it, so the figure the earlier census quoted still reconciles.
    """
    path = Path(path_text).expanduser()
    for name in ("runs", "archive/runs"):
        try:
            relative = path.relative_to(CREW_HOME / name)
        except ValueError:
            continue
        return bool(relative.parts)
    return False


def enclosing_root(path_text: str) -> Path:
    """The repository root a path outside the crew home belongs to, or its parent."""
    path = Path(path_text).expanduser()
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return path.parent


def claim_reading(path_text: str) -> dict:
    """One claimed path with the root it lies under and how that root is shared."""
    path = Path(path_text).expanduser()
    text = str(path)
    shared = shared_root(text)
    return {
        "path": text,
        "root": str(CREW_HOME if shared else enclosing_root(text)),
        "shared_root": shared,
        "run_owned": run_owned(text),
    }


def read_passes(paths) -> tuple[list[dict], list[dict]]:
    """Every pass the named sources hold, with each source's own coverage.

    The pass record is the same JSON object in the crew-home verdict log and in
    the admit service's stdout log, so either answers the census. Sources are
    merged on the pass's own instant and lane, which keeps a spooled copy from
    double-counting a pass its source still holds.
    """
    seen: dict[tuple, dict] = {}
    coverage = []
    for raw in paths:
        path = Path(raw).expanduser()
        count = 0
        stamps = []
        if path.is_file():
            for raw_line in path.read_text(errors="replace").splitlines():
                text = raw_line.strip()
                if not text.startswith("{"):
                    continue
                try:
                    row = json.loads(text)
                    stamps.append(parse_instant(row["at"]))
                except (ValueError, KeyError, TypeError):
                    continue
                count += 1
                seen.setdefault((row["at"], row.get("lane")), row)
        coverage.append(
            {
                "path": str(path),
                "passes_read": count,
                "span": [min(stamps).isoformat(), max(stamps).isoformat()]
                if stamps
                else None,
            }
        )
    rows = sorted(seen.values(), key=lambda row: row["at"])
    return rows, coverage


def spool_passes(spool_path: Path, sources, *, overlap_seconds: float = 300.0) -> dict:
    """Append to the spool every pass a source holds that the spool does not.

    Both instruments are bounded: the crew-home verdict log keeps its last
    mebibyte and the admit service's log its last four, and a four-hour window
    with a busy queue outruns either. A census run after the window therefore
    reads a copy taken while the window was open. Copies are keyed on the pass's
    instant and lane, so a source rewritten in place can neither duplicate a
    pass already taken nor hand an older one back over the edge of the overlap.
    """
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    held, _coverage = read_passes([spool_path])
    keys = {(row["at"], row.get("lane")) for row in held}
    newest = max((parse_instant(row["at"]) for row in held), default=None)
    appended = 0
    with spool_path.open("a", encoding="utf-8") as sink:
        for raw in sources:
            path = Path(raw).expanduser()
            if not path.is_file():
                continue
            for raw_line in path.read_text(errors="replace").splitlines():
                text = raw_line.strip()
                if not text.startswith("{"):
                    continue
                try:
                    row = json.loads(text)
                    when = parse_instant(row["at"])
                except (ValueError, KeyError, TypeError):
                    continue
                key = (row["at"], row.get("lane"))
                if key in keys:
                    continue
                if newest is not None and when < newest - dt.timedelta(
                    seconds=overlap_seconds
                ):
                    continue
                sink.write(json.dumps(row, sort_keys=True) + "\n")
                keys.add(key)
                appended += 1
    total, _coverage = read_passes([spool_path])
    return {
        "spool": str(spool_path),
        "appended": appended,
        "passes_held": len(total),
        "span": [
            min((row["at"] for row in total), default=None),
            max((row["at"] for row in total), default=None),
        ],
    }


def admission_holds(passes: list[dict], start: dt.datetime, end: dt.datetime) -> dict:
    """Admit passes in the window, and every claim a hold named, with its root.

    The sources are capped to the passes they still hold, so a window they do
    not cover reports that it was not measured rather than reporting zero. Each
    held claim is reported by path beside its root, so a share of the crew home
    that no run owns reads differently from a path one run owns.
    """
    stamps = []
    for row in passes:
        try:
            stamps.append(parse_instant(row["at"]))
        except (ValueError, KeyError, TypeError):
            continue
    if not stamps:
        return {
            "passes": None,
            "passes_holding_shared_claim": None,
            "shared_claim_pointers": None,
            "held_claim_paths": [],
            "note": "no admission source held a pass",
        }
    span = (min(stamps), max(stamps))
    if end <= span[0] or start >= span[1]:
        return {
            "passes": None,
            "passes_holding_shared_claim": None,
            "shared_claim_pointers": None,
            "held_claim_paths": [],
            "note": "the window lies outside the passes the admission sources still hold",
            "log_span": [span[0].isoformat(), span[1].isoformat()],
        }
    inside = 0
    holding = 0
    pointers = 0
    unnamed = 0
    claims: dict[str, dict] = {}
    for row in passes:
        try:
            when = parse_instant(row["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (start <= when < end):
            continue
        inside += 1
        shared = 0
        for pointer in row.get("pointers") or []:
            if pointer.get("verdict") != "held-on-claim":
                continue
            path = claimed_path(pointer.get("detail") or "")
            if not path:
                unnamed += 1
                continue
            entry = claims.get(path)
            if entry is None:
                entry = claims[path] = {
                    **claim_reading(path),
                    "pointers": 0,
                    "passes": 0,
                }
            entry["pointers"] += 1
            entry["passes"] += 1
            if entry["shared_root"]:
                shared += 1
        pointers += shared
        if shared:
            holding += 1
    return {
        "passes": inside,
        "passes_holding_shared_claim": holding,
        "shared_claim_pointers": pointers,
        "held_claim_paths": sorted(
            claims.values(), key=lambda entry: (-entry["passes"], entry["path"])
        ),
        "unnamed_claim_pointers": unnamed,
    }


def identity_files(as_of: dt.datetime) -> dict:
    """Worker identity files in the claims directory at an instant, with its total entries.

    An identity file is written once, when its process first publishes it, so a
    file's modification time places it at the moment it appeared. Counting by
    that stamp reports the directory as it stood at the window's end rather
    than as it stands when this census runs -- but only for a file that is
    still there. Maintenance removes an ended run's identity, so a census run
    long after the window reads what survived rather than what stood, and says
    so instead of reporting that reading as the instant's.
    """
    now = dt.datetime.now(dt.UTC)
    live = abs((now - as_of).total_seconds()) <= 3600.0
    if not CLAIMS_DIR.is_dir():
        return {
            "identity_files": None,
            "claims_entries": None,
            "as_of": as_of.isoformat(),
            "reads_live_state": live,
        }
    names = os.listdir(CLAIMS_DIR)
    identities = 0
    entries = 0
    for name in names:
        try:
            stamp = (CLAIMS_DIR / name).stat().st_mtime
        except OSError:
            continue
        if stamp > as_of.timestamp():
            continue
        entries += 1
        if IDENTITY_FILE.match(name):
            identities += 1
    reading = {
        "identity_files": identities,
        "claims_entries": entries,
        "as_of": as_of.isoformat(),
        "reads_live_state": live,
    }
    if not live:
        reading["note"] = (
            "the claims directory is a live state and maintenance removes an "
            "ended run's identity, so this counts what survived to the census "
            "rather than what stood at the window's end"
        )
    return reading


def dispatches(runs: list[dict], start: dt.datetime, end: dt.datetime) -> dict:
    """Requests dispatched in the window, and the distinct plan sections they name."""
    count = 0
    sections = set()
    for run in runs:
        stamp = run["dispatched_at"]
        if not stamp:
            continue
        try:
            moment = parse_instant(stamp)
        except ValueError:
            continue
        if start <= moment < end:
            count += 1
            sections.add((run["project"], run["node"]))
    return {"dispatched": count, "distinct_project_nodes": len(sections)}


def census(start: dt.datetime, end: dt.datetime, runs, spans, stamps, passes) -> dict:
    return {
        "window": [start.isoformat(), end.isoformat()],
        "dispatches": dispatches(runs, start, end),
        "working_workers": concurrency(spans, start, end),
        "completion_to_promotion": promotion_latency(runs, stamps, start, end),
        "admit_holds": admission_holds(passes, start, end),
        "identity_files": identity_files(end),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window", help="START/END, for example 2026-11-14T21:00Z/2026-11-15T01:00Z"
    )
    parser.add_argument(
        "--baseline", action="store_true", help="report the 7 to 11 November baseline"
    )
    parser.add_argument(
        "--negative-control",
        action="store_true",
        help="report 11 November 13:00 to 15:00 UTC, which must read below 12 working workers",
    )
    parser.add_argument(
        "--admission-log",
        action="append",
        default=[],
        help=(
            "a pass record to read the admission figures from; repeatable, and "
            "read instead of the crew-home verdict log when given"
        ),
    )
    parser.add_argument(
        "--spool-to",
        help=(
            "append every pass the sources hold that this file does not, then "
            "census the sources and the spool together"
        ),
    )
    parser.add_argument(
        "--spool-from",
        action="append",
        default=[],
        help=(
            "an extra source for --spool-to; the admit service's own log unless "
            "one is named"
        ),
    )
    args = parser.parse_args(argv)

    chosen = []
    if args.window:
        chosen.append(("window", parse_window(args.window)))
    if args.baseline:
        chosen.append(("baseline", parse_window("/".join(BASELINE))))
    if args.negative_control:
        chosen.append(("negative_controls", parse_window("/".join(NEGATIVE_CONTROL))))
    if not chosen:
        chosen.append(("window", parse_window("/".join(WINDOW))))

    runs, source_counts = load_runs()
    spans = worker_spans(runs)
    stamps = landing_stamps()

    sources = [str(path) for path in args.admission_log] or [str(ADMISSION_LOG)]
    spooled = None
    if args.spool_to:
        extras = args.spool_from or [str(SERVICE_LOG)]
        spooled = spool_passes(Path(args.spool_to), [*sources, *extras])
        sources = [*sources, *extras, str(args.spool_to)]
    passes, coverage = read_passes(sources)
    report = {
        "read": {
            "admission_log": str(ADMISSION_LOG),
            "admission_sources": coverage,
            "claims_dir": str(CLAIMS_DIR),
            "landing_records": len(stamps),
            **source_counts,
            "spooled": spooled,
            "worker_spans": len(spans),
        },
        "run_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    for name, (start, end) in chosen:
        report[name] = census(start, end, runs, spans, stamps, passes)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
