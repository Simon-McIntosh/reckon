#!/usr/bin/env python3
"""Census of local-lane session inheritance, by the run's own session join.

The local lane carries the claude grammar, which publishes a run's charged
input per assistant turn. A run that *resumes* a session sends the earlier
session's whole transcript on its opening request, so its first assistant
turn's input is far above what a session-opening run's first turn carries:
the difference is the history it inherited. This script measures that figure
for every run dispatched to the local lane, splits the population at the
commit that keyed session reuse to the run's own task, and writes the counts
the companion codex census reports on the same boundaries, so the two files
compare field for field.

Reading is bounded on purpose. For each run directory the scan reads its
launch record — the run store row keyed by run id, plus the run directory's
own attempt record where one exists — and a *prefix* of the stream that stops
at the first assistant record carrying usage. A run's whole stream is never
parsed: the prefix is the only part the first-turn figure lives in, and the
bound is recorded in the output so a reader can see how much of each stream
was read.

Session inheritance is recovered from the stream, not from a launch flag. A
run reports its session id in its opening record; runs sharing an id are one
conversation, and the earlier run by dispatch order is the one that built the
history the later run's first turn carried in. A dispatch that *declares* a
resume in its argv but whose stream opens a fresh session inherited nothing —
the stream is the record of what was actually sent.

Usage (bounded by the caller; the prefix read runs at roughly 60 MB/s):

    timeout 1200 <venv>/bin/python \
        docs/research/scripts/scan_local_session_carryover.py \
        --out docs/research/data/local-session-carryover.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CREW_HOME = Path.home() / ".config/reckon" / "crew"
RUNS_ROOT = CREW_HOME / "runs"
RUN_STORE = CREW_HOME / "run_store.db"

# The backends this workstation serves from its own GPU lane rather than
# through a metered provider. ``clive-glm`` is a route alias that runs the
# same local command, which is why both names are the local lane here.
LOCAL_BACKENDS = ("clive", "clive-glm")

# The models the local route serves. A run older than the run store's coverage
# records no backend anywhere, but its own opening record names the model it
# was served, so that name identifies the lane for those runs alone.
LOCAL_MODELS = ("deepseek-v4.1-flash", "deepseek-v4-flash")

# The committer time of adbb6067, the merge that keyed dispatch session reuse
# to the run's own (project, plan, node) task instead of the member's session.
# Run ids carry a UTC timestamp, so the window split needs no filesystem join.
BOUNDARY_UTC = "2026-09-23T10:40:20Z"
BOUNDARY_COMMIT = "adbb606797e2561dcc31758e49458f1d1bac3fef"
BOUNDARY = datetime(2026, 9, 23, 10, 40, 20, tzinfo=UTC).timestamp()

REVIEW_PREFIX = "review-of-"

# Bounds on one run's prefix read. The first assistant record sits at a median
# depth of about forty lines and never past a few hundred in the sample this
# instrument was built against; the cap exists so a stream that never emits a
# charged turn cannot turn the bounded read into a whole-stream read.
MAX_PREFIX_LINES = 20_000
MAX_PREFIX_BYTES = 64 * 1024 * 1024

# Per-run rows are stored for every resume and for a sample of fresh runs, so
# the file stays a census rather than a transcript dump. The fresh sample is
# the most recent rows in each window, taken deterministically.
FRESH_ROWS_PER_WINDOW = 25

RESURRECTED_CLASSES = (
    "same_task",
    "cross_task",
    "same_run_id",
    "resumed_prior_unknown",
)


def run_timestamp(run_id: str) -> float:
    """A run's dispatch moment, from the UTC timestamp in its id."""

    stamp = run_id.split("-")[1]
    moment = datetime(
        int(stamp[0:4]),
        int(stamp[4:6]),
        int(stamp[6:8]),
        int(stamp[9:11]),
        int(stamp[11:13]),
        int(stamp[13:15]),
        int(stamp[15:21].ljust(6, "0")),
        tzinfo=UTC,
    )
    return moment.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def charged_input(usage: Any) -> float | None:
    """One request's full charged input, including cached context.

    Mirrors ``reckon.capabilities._charged_input_from_usage``: the stream
    reports disjoint ``cache_read``/``cache_creation`` parts on this dialect,
    and ``cached_input_tokens`` where it appears is a subset of the
    already-total input rather than a part to add again.
    """

    if not isinstance(usage, dict):
        return None
    direct = usage.get("input_tokens")
    direct = (
        direct
        if isinstance(direct, (int, float)) and not isinstance(direct, bool)
        else None
    )
    parts = [
        value
        for value in (
            usage.get("cache_read_input_tokens"),
            usage.get("cache_creation_input_tokens"),
        )
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if parts:
        total = ([direct] if direct is not None else []) + parts
    else:
        total = [direct] if direct is not None else []
    return float(sum(total)) if total else None


def read_prefix(stream: Path) -> dict[str, Any]:
    """Read a stream's opening record and its first charged assistant turn.

    The read stops at the first assistant record that carries usage, so the
    stream beyond that turn is never parsed. ``bound_hit`` records that the
    cap was reached before a figure was found rather than leaving the absence
    to be inferred from a null.
    """

    head: dict[str, Any] = {
        "dialect": None,
        "session_id": None,
        "cwd": None,
        "model": None,
        "first_input": None,
        "lines_read": 0,
        "bytes_read": 0,
        "bound_hit": False,
    }
    try:
        handle = stream.open(encoding="utf-8", errors="ignore")
    except OSError:
        return head
    with handle:
        for raw in handle:
            head["lines_read"] += 1
            head["bytes_read"] += len(raw)
            if (
                head["lines_read"] > MAX_PREFIX_LINES
                or head["bytes_read"] > MAX_PREFIX_BYTES
            ):
                head["bound_hit"] = True
                break
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            if head["dialect"] is None:
                if kind == "system":
                    head["dialect"] = "claude"
                elif kind == "thread.started":
                    head["dialect"] = "codex"
                else:
                    head["dialect"] = f"<other:{kind}>"
                if head["dialect"] != "claude":
                    # Other dialects carry no first-turn figure this census
                    # can read; the opening record is all that is taken.
                    break
            if kind == "system":
                if head["session_id"] is None and record.get("session_id"):
                    head["session_id"] = str(record["session_id"])
                if head["cwd"] is None and record.get("cwd"):
                    head["cwd"] = str(record["cwd"])
                if head["model"] is None and record.get("model"):
                    head["model"] = str(record["model"])
                continue
            if kind == "assistant":
                message = record.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                value = charged_input(usage)
                if value is not None:
                    head["first_input"] = value
                    break
    return head


def worktree_key(cwd: str | None) -> str | None:
    if not cwd:
        return None
    return cwd.replace("\\", "/").rstrip("/").split("/")[-1] or None


def load_launch_records(store_path: Path) -> dict[str, dict[str, Any]]:
    """Run identity and session as the launch record stores them.

    The run store's row per run is the launch record this census reads:
    backend, the (project, plan, node) identity the reuse rule keys on, and
    the session id the dispatch recorded. Read-only, so a scan never writes
    the store a live fleet is using.
    """

    records: dict[str, dict[str, Any]] = {}
    try:
        handle = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"scan: launch-record store unreadable: {exc}", file=sys.stderr)
        return records
    handle.row_factory = sqlite3.Row
    try:
        rows = handle.execute(
            "select run_id, project, node, plan, backend, payload from runs"
        )
        for row in rows:
            ident: dict[str, Any] = {
                "project": str(row["project"] or ""),
                "node": str(row["node"] or ""),
                "plan": str(row["plan"] or ""),
                "backend": str(row["backend"] or ""),
                "role": "",
                "session_id": "",
                "attempt": None,
                "attempt_kind": "",
            }
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                for key in ("project", "plan", "node", "role", "backend"):
                    if payload.get(key):
                        ident[key] = str(payload[key])
                ident["session_id"] = str(payload.get("session_id") or "")
                ident["attempt"] = payload.get("attempt")
                ident["attempt_kind"] = str(payload.get("attempt_kind") or "")
            records[str(row["run_id"])] = ident
    except sqlite3.Error as exc:
        print(f"scan: launch-record store query failed: {exc}", file=sys.stderr)
    finally:
        handle.close()
    return records


def read_launch(run_dir: Path) -> dict[str, Any]:
    """The run directory's own launch records: attempts, backend, resume token.

    Each attempt leaves a small launch record beside the run's stream — the
    current ``worker.json`` and any ``attempt-N-worker.json`` — and
    ``attempt.json`` names the attempt in hand. On this lane the argv is
    recorded durably here, so a resume the dispatch *asked for* is readable
    from the record even when the attempt's stream is a later one that this
    census does not read. These files are read whole; they are kilobytes, not
    streams, and they are the launch records this scan is bounded to.
    """

    launch: dict[str, Any] = {
        "attempt": None,
        "attempt_kind": "",
        "backend": None,
        "declared_resume": None,
        "declared_resume_attempt": None,
        "records_read": 0,
    }
    try:
        current = json.loads((run_dir / "attempt.json").read_text())
    except (OSError, json.JSONDecodeError):
        current = None
    if isinstance(current, dict):
        launch["attempt"] = current.get("attempt")
        launch["attempt_kind"] = str(current.get("attempt_kind") or "")
        launch["records_read"] += 1
    for path in sorted(run_dir.glob("*worker.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        launch["records_read"] += 1
        attempt = payload.get("attempt")
        if launch["backend"] is None and payload.get("backend"):
            launch["backend"] = str(payload["backend"])
        argv = payload.get("argv")
        argv = [str(item) for item in argv] if isinstance(argv, list) else []
        if "--resume" in argv:
            index = argv.index("--resume")
            session = argv[index + 1] if index + 1 < len(argv) else None
            if session and (
                launch["declared_resume"] is None
                or (
                    isinstance(attempt, int)
                    and attempt >= (launch["declared_resume_attempt"] or 0)
                )
            ):
                launch["declared_resume"] = session
                launch["declared_resume_attempt"] = (
                    attempt if isinstance(attempt, int) else None
                )
        if launch["attempt"] is None and isinstance(attempt, int):
            launch["attempt"] = attempt
    return launch


def review_source(node: str) -> str:
    return node.removeprefix(REVIEW_PREFIX)


def task_identity(record: dict[str, Any], run_id: str) -> tuple[str, ...]:
    """The task a run belongs to, in the census's own terms.

    This mirrors the identity the companion codex census uses, so the two
    classifications are comparable: an implement, test or investigate run is
    (node, project, plan, node id); a review is the reviewed node's name
    rather than the reviewed run's id, which is the simplification stated in
    the parser block of the output.
    """

    node = str(record.get("node") or run_id.split("-", 2)[-1])
    project = str(record.get("project") or "")
    if str(record.get("role") or "") == "review" or node.startswith(REVIEW_PREFIX):
        return ("review", project, review_source(node))
    return ("node", project, str(record.get("plan") or ""), node)


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The window summary, field for field with the codex census."""

    classes = Counter(row["class"] for row in rows)
    firsts = [row["first"] for row in rows if isinstance(row["first"], (int, float))]
    inherited = [row["inherited"] for row in rows]
    fresh_firsts = [
        row["first"]
        for row in rows
        if row["class"] == "fresh" and isinstance(row["first"], (int, float))
    ]
    same_task = classes.get("same_task", 0) + classes.get("same_run_id", 0)
    return {
        "dispatches": len(rows),
        "sessions": len({row["session_id"] for row in rows}),
        "class_counts": dict(sorted(classes.items())),
        "fresh": classes.get("fresh", 0),
        "same_task_resumes": same_task,
        "cross_task_resumes": classes.get("cross_task", 0),
        "prior_unknown_resumes": classes.get("resumed_prior_unknown", 0),
        "first_request_median": statistics.median(firsts) if firsts else None,
        "fresh_first_request_median": (
            statistics.median(fresh_firsts) if fresh_firsts else None
        ),
        "inherited_tokens_total": sum(inherited),
        "runs_with_inherited_tokens": sum(1 for value in inherited if value > 0),
    }


def resolve_declared_resumes(rows: list[dict[str, Any]]) -> None:
    """Attribute every declared resume to the task that owned the session.

    A launch record that carries ``--resume <session>`` is a dispatch asking
    to continue that session, whether or not the attempt's stream is the one
    this census reads. The owner is the latest local run, at or before this
    one, whose own stream opened the same session — which for the common case
    of a run resumed after its own attempt died is the run itself. A
    declaration whose owner is not in the population is recorded unresolved
    rather than guessed.
    """

    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_session[row["session_id"]].append(row)
    for row in rows:
        declared = row.get("declared_resume")
        row["declared_target"] = None
        if not declared:
            continue
        row["declared_matches_stream"] = declared == row["session_id"]
        owners = [
            item for item in by_session.get(declared, []) if item["ts"] <= row["ts"]
        ]
        if not owners:
            row["declared_target"] = "unresolved"
            row["declared_owner_run"] = None
            row["declared_owner_is_own_run"] = None
            continue
        owner = max(owners, key=lambda item: item["ts"])
        row["declared_owner_run"] = owner["run_id"]
        row["declared_owner_is_own_run"] = owner["run_id"] == row["run_id"]
        if row["declared_owner_is_own_run"] or task_identity(
            owner, owner["run_id"]
        ) == task_identity(row, row["run_id"]):
            row["declared_target"] = "same_task"
        else:
            row["declared_target"] = "cross_task"


def declared_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(
        row["declared_target"] for row in rows if row.get("declared_resume")
    )
    declared = [row for row in rows if row.get("declared_resume")]
    examples = [
        {
            "run_id": row["run_id"],
            "attempt": row["attempt"],
            "declared_resume_attempt": row["declared_resume_attempt"],
            "declared_session": row["declared_resume"],
            "declared_matches_stream": row["declared_matches_stream"],
            "declared_target": row["declared_target"],
            "declared_owner_run": row["declared_owner_run"],
            "declared_owner_is_own_run": row["declared_owner_is_own_run"],
            "ts": iso(row["ts"]),
            "node": row["node"],
        }
        for row in declared
        if row["declared_target"] in ("cross_task", "unresolved")
    ][:12]
    return {
        "declared_resumes": len(declared),
        "declared_same_task": counts.get("same_task", 0),
        "declared_cross_task": counts.get("cross_task", 0),
        "declared_unresolved": counts.get("unresolved", 0),
        "declared_own_run": sum(
            1 for row in declared if row.get("declared_owner_is_own_run")
        ),
        "dispatches_with_launch_records": sum(
            1 for row in rows if row.get("launch_records_read")
        ),
        "unresolved_or_cross_task_examples": examples,
    }


def stored_row(row: dict[str, Any]) -> dict[str, Any]:
    """One per-run row, lean, in the shape the codex census stores."""

    return {
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "ts": iso(row["ts"]),
        "class": row["class"],
        "task_index": row["task_index"],
        "runs_in_session": row["runs_in_session"],
        "prior_run": row["prior_run"],
        "task_worktree": row["task_worktree"],
        "prior_worktree": row["prior_worktree"],
        "worktree_same_as_prior": row["worktree_same_as_prior"],
        "first_request_input": row["first"],
        "inherited_tokens": row["inherited"],
        "backend": row["backend"],
        "project": row["project"],
        "plan": row["plan"],
        "node": row["node"],
        "attempt": row["attempt"],
        "attempt_kind": row["attempt_kind"],
    }


def control_block(
    row: dict[str, Any],
    source: str,
    median: float | None,
    corroboration: dict[str, Any],
) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "source": source,
        "session_id": row["session_id"],
        "ts": iso(row["ts"]),
        "class": row["class"],
        "prior_run": row["prior_run"],
        "task_index": row["task_index"],
        "runs_in_session": row["runs_in_session"],
        "first_request_input": row["first"],
        "fresh_median_first_request": median,
        "inherited_tokens": row["inherited"],
        "identity": "|".join(row["identity"]),
        "prior_identity": "|".join(row["prior_identity"]),
        "task_worktree": row["task_worktree"],
        "prior_worktree": row["prior_worktree"],
        "recorded_session_id": row["recorded_session_id"],
        "session_join_agrees": row["session_join_agrees"],
        "backend": row["backend"],
        "attempt": row["attempt"],
        "attempt_kind": row["attempt_kind"],
        "project": row["project"],
        "plan": row["plan"],
        "node": row["node"],
        "role": row["role"],
        "corroboration": corroboration,
    }


def load_earlier_census(census_path: Path) -> dict[str, dict[str, Any]]:
    """The earlier census's rows by run id, for corroborating the control."""

    try:
        earlier = json.loads(census_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    rows = earlier.get("runs") if isinstance(earlier, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        str(row["run_id"]): row
        for row in rows
        if isinstance(row, dict) and row.get("run_id")
    }


def corroborate(
    run_id: str,
    prior_run: str | None,
    census_path: Path,
    earlier_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Independent records that the control run really resumed another task.

    The control is only a control if the resume is known from something
    besides the classification being tested. Two such records are read: the
    earlier census file, taken by a different instrument on 2026-09-23, and
    the run's own launch record where a launch argv declares a resume token.
    """

    block: dict[str, Any] = {
        "prior_census_file": str(census_path),
        "prior_census_names_it_resumed": None,
        "prior_census_prior_run": None,
        "prior_census_inherited_tokens": None,
        "prior_census_prior_run_matches": None,
        "launch_record_declares_resume": None,
        "launch_record_resume_session": None,
        "launch_record_resume_matches_stream_session": None,
    }
    row = earlier_rows.get(run_id)
    if row is not None:
        block["prior_census_names_it_resumed"] = bool(row.get("resumed"))
        block["prior_census_prior_run"] = row.get("prior_run")
        block["prior_census_inherited_tokens"] = row.get("inherited_tokens")
        block["prior_census_prior_run_matches"] = row.get("prior_run") == prior_run
    return block


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path, help="census JSON to write")
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    parser.add_argument("--store", type=Path, default=RUN_STORE)
    parser.add_argument(
        "--earlier-census",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "session-carryover.json",
        help="the earlier local census, used to corroborate the positive control",
    )
    parser.add_argument("--stdout-summary", action="store_true")
    args = parser.parse_args(argv)

    started = time.monotonic()
    first_types: Counter[str] = Counter()
    records = load_launch_records(args.store)
    print(
        f"stage=launch-records rows={len(records)} {time.monotonic() - started:.1f}s",
        flush=True,
    )

    rows_raw: list[dict[str, Any]] = []
    excluded_backends: Counter[str] = Counter()
    backend_basis: Counter[str] = Counter()
    other_dialect_local: list[dict[str, str]] = []
    prefix_bound_hits = 0
    no_first_figure = 0
    total_prefix_bytes = 0
    streams = 0
    for stream in sorted(args.runs_root.glob("*/stream.jsonl")):
        streams += 1
        run_id = stream.parent.name
        head = read_prefix(stream)
        total_prefix_bytes += head["bytes_read"]
        first_types[str(head["dialect"])] += 1
        if head["bound_hit"]:
            prefix_bound_hits += 1
        if head["dialect"] != "claude":
            if str((records.get(run_id) or {}).get("backend") or "") in LOCAL_BACKENDS:
                other_dialect_local.append(
                    {"run_id": run_id, "dialect": str(head["dialect"])}
                )
            continue
        ident = records.get(run_id)
        launch = read_launch(stream.parent)
        basis = "run-store"
        backend = str((ident or {}).get("backend") or "")
        if not backend:
            if launch["backend"] in LOCAL_BACKENDS:
                backend, basis = str(launch["backend"]), "launch-record"
            elif head["model"] in LOCAL_MODELS:
                backend, basis = "clive", "stream-model"
        if backend not in LOCAL_BACKENDS:
            excluded_backends[backend or "<no-recorded-backend>"] += 1
            continue
        backend_basis[basis] += 1
        if ident is None:
            ident = {
                "project": "",
                "node": "",
                "plan": "",
                "backend": backend,
                "role": "",
                "session_id": "",
                "attempt": None,
                "attempt_kind": "",
            }
        if head["first_input"] is None and not head["bound_hit"]:
            no_first_figure += 1
        try:
            ts = run_timestamp(run_id)
        except ValueError:
            continue
        attempt = ident.get("attempt")
        attempt_kind = str(ident.get("attempt_kind") or "")
        if attempt is None:
            attempt = launch["attempt"]
            attempt_kind = attempt_kind or launch["attempt_kind"]
        rows_raw.append(
            {
                "run_id": run_id,
                "session_id": head["session_id"] or "",
                "ts": ts,
                "first": head["first_input"],
                "task_worktree": worktree_key(head["cwd"]),
                "backend": backend,
                "project": str(ident.get("project") or ""),
                "plan": str(ident.get("plan") or ""),
                "node": str(ident.get("node") or ""),
                "role": str(ident.get("role") or ""),
                "attempt": attempt,
                "attempt_kind": attempt_kind,
                "recorded_session_id": str(ident.get("session_id") or ""),
                "declared_resume": launch["declared_resume"],
                "declared_resume_attempt": launch["declared_resume_attempt"],
                "launch_records_read": launch["records_read"],
            }
        )
    print(
        f"stage=streams streams={streams} local_runs={len(rows_raw)} "
        f"prefix_MB={total_prefix_bytes / 1e6:.1f} {time.monotonic() - started:.1f}s",
        flush=True,
    )

    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows_raw:
        sessions[row["session_id"]].append(row)
    for group in sessions.values():
        group.sort(key=lambda row: row["ts"])
        prior: dict[str, Any] | None = None
        for index, row in enumerate(group):
            row["task_index"] = index
            row["runs_in_session"] = len(group)
            row["prior_run"] = prior["run_id"] if prior else None
            row["prior_worktree"] = prior["task_worktree"] if prior else None
            prior = row

    fresh_firsts = [
        row["first"]
        for row in rows_raw
        if row["task_index"] == 0 and isinstance(row["first"], (int, float))
    ]
    median = statistics.median(fresh_firsts) if fresh_firsts else None

    for row in rows_raw:
        row["inherited"] = 0.0
        first = row["first"]
        if (
            isinstance(first, (int, float))
            and row["task_index"] > 0
            and median is not None
            and first > median
        ):
            row["inherited"] = float(first) - median
        row["worktree_same_as_prior"] = bool(
            row["task_index"] > 0
            and row["prior_worktree"]
            and row["task_worktree"]
            and row["prior_worktree"] == row["task_worktree"]
        )
        row["session_join_agrees"] = (
            bool(row["recorded_session_id"])
            and row["recorded_session_id"] == row["session_id"]
        )
        my_identity = task_identity(row, row["run_id"])
        if row["task_index"] == 0:
            row["class"] = "fresh"
        elif row["prior_run"] is None:
            row["class"] = "resumed_prior_unknown"
        elif row["prior_run"] == row["run_id"]:
            row["class"] = "same_run_id"
        else:
            prior = next(
                item for item in rows_raw if item["run_id"] == row["prior_run"]
            )
            prior_identity = task_identity(prior, prior["run_id"])
            row["identity"] = my_identity
            row["prior_identity"] = prior_identity
            row["class"] = (
                "same_task" if my_identity == prior_identity else "cross_task"
            )
    print(
        f"stage=classified runs={len(rows_raw)} sessions={len(sessions)} "
        f"fresh_median={median} {time.monotonic() - started:.1f}s",
        flush=True,
    )

    after = sorted(
        (row for row in rows_raw if row["ts"] > BOUNDARY), key=lambda r: r["ts"]
    )
    before = sorted(
        (row for row in rows_raw if row["ts"] <= BOUNDARY), key=lambda r: r["ts"]
    )
    resolve_declared_resumes(rows_raw)

    later_than_median_fresh = [
        row
        for row in before
        if row["class"] == "fresh"
        and isinstance(row["first"], (int, float))
        and median is not None
        and row["first"] > 2 * median
    ]

    control_row = None
    control_source = "before_window_known_cross_task_resume"
    control_corroboration: dict[str, Any] = {}
    earlier_rows = load_earlier_census(args.earlier_census)
    candidates = sorted(
        (
            row
            for row in before
            if row["class"] == "cross_task" and row["inherited"] > 0
        ),
        key=lambda row: (-row["inherited"], row["run_id"]),
    )
    for row in candidates:
        block = corroborate(
            row["run_id"], row["prior_run"], args.earlier_census, earlier_rows
        )
        if block.get("prior_census_names_it_resumed") and block.get(
            "prior_census_prior_run_matches"
        ):
            control_row = row
            control_corroboration = block
            break
    if control_row is None and candidates:
        control_row = candidates[0]
        control_corroboration = corroborate(
            control_row["run_id"],
            control_row["prior_run"],
            args.earlier_census,
            earlier_rows,
        )
        control_source = "before_window_cross_task_by_this_scanner_uncorroborated"
    if control_row is not None:
        declared = control_row.get("declared_resume")
        control_corroboration["launch_record_declares_resume"] = declared is not None
        control_corroboration["launch_record_resume_session"] = declared
        control_corroboration["launch_record_resume_matches_stream_session"] = (
            declared == control_row["session_id"]
        )

    def fresh_sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fresh = [row for row in rows if row["class"] == "fresh"]
        return fresh[-FRESH_ROWS_PER_WINDOW:]

    def window_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resumes = [row for row in rows if row["class"] != "fresh"]
        return [stored_row(row) for row in resumes + fresh_sample(rows)]

    parser_block = {
        "resume_recovered_from": (
            "the session id in the run's own opening stream record: a run that "
            "inherits an earlier transcript opens the same session, a fresh run "
            "opens a new one, so sharing an id is the only durable record of what "
            "the first turn actually sent"
        ),
        "session_join": (
            "run dirs are grouped by the session id their own stream opens with, "
            "ordered by run-id timestamp; the earlier run is recorded as the one "
            "that built the history"
        ),
        "first_request_input": (
            "the first assistant record's message.usage charged input (input_tokens "
            "plus the disjoint cache read/create fields where published)"
        ),
        "cumulative_input": (
            "not read on this lane: the terminal result record carries it, and this "
            "scan reads only a bounded prefix ending at the first assistant turn"
        ),
        "inherited": "max(0, first_request_input - fresh_run_median_first_request)",
        "task_worktree": (
            "the cwd recorded in the run's own opening record, which names the "
            "worktree the run worked in"
        ),
        "task_identity": (
            "the identity the session-reuse rule keys on: (project, plan, node) for "
            "an implement, test or investigate run, and the reviewed node for a "
            "review; the reviewed node's name is a simplification of the dispatch "
            "rule's reviewed-run id, held here so this file compares with the codex "
            "census field for field"
        ),
        "runs_in_session": (
            "the local grammar publishes no rollout, so the count the codex census "
            "records as tasks_in_rollout is here the number of local run "
            "directories sharing the session"
        ),
        "read_bound": (
            f"each stream is read only until its first charged assistant turn, "
            f"never past {MAX_PREFIX_LINES} lines or {MAX_PREFIX_BYTES} bytes"
        ),
        "lane_rule": (
            "a run is on the local lane when its recorded backend is local — the "
            "run store's backend for the run, or the run directory's launch record "
            f"where the store holds no row, local backends being {LOCAL_BACKENDS} — "
            "and its own stream opens the claude grammar, which is the grammar this "
            "lane runs and the only one that publishes a first-turn figure; runs "
            "recorded local whose stream opens the codex grammar are counted "
            "separately and excluded, because their stream is another attempt's"
        ),
        "launch_record_coverage": (
            "the run directory's launch records (attempt.json, worker.json) begin "
            "on 2026-09-24, so a resume declared in argv is measurable for the "
            "after window only; the before window's declared resumes are unrecorded "
            "rather than zero"
        ),
        "same_task_resumes": (
            "an inherited transcript whose task identity matches this run's, "
            "including a run that continues its own session"
        ),
        "cross_task_resumes": (
            "an inherited transcript whose task identity differs from this run's: "
            "the condition the reuse rule removes"
        ),
    }

    report: dict[str, Any] = {
        "census": "local-lane session inheritance by the run's own session join",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "after_window_rule": (
            f"local dispatch run-id timestamp > {BOUNDARY_UTC}, the committer time of "
            f"{BOUNDARY_COMMIT}, which keyed session reuse to the run's own task"
        ),
        "boundary_commit": BOUNDARY_COMMIT,
        "boundary_utc": BOUNDARY_UTC,
        "parser": parser_block,
        "scan": {
            "run_dirs_with_stream": streams,
            "first_record_types": dict(first_types.most_common()),
            "local_runs": len(rows_raw),
            "local_sessions": len(sessions),
            "launch_record_basis": dict(backend_basis),
            "local_runs_without_a_first_turn_figure": no_first_figure,
            "prefix_bound_hits": prefix_bound_hits,
            "prefix_bytes_read": total_prefix_bytes,
            "excluded_claude_dialect_backends": dict(excluded_backends.most_common()),
            "excluded_local_recorded_other_dialect": len(other_dialect_local),
            "excluded_local_recorded_other_dialect_examples": other_dialect_local[:10],
            "launch_records_begin_utc": (
                iso(
                    min(row["ts"] for row in rows_raw if row.get("launch_records_read"))
                )
                if any(row.get("launch_records_read") for row in rows_raw)
                else None
            ),
        },
        "fresh_median_first_request": median,
        "before_window": summarise(before),
        "after_window": summarise(after),
        "positive_control": control_block(
            control_row, control_source, median, control_corroboration
        ),
        "session_join_agrees": sum(1 for row in rows_raw if row["session_join_agrees"]),
        "session_join_records": sum(
            1 for row in rows_raw if row["recorded_session_id"]
        ),
        "session_join_disagreements": [
            {
                "run_id": row["run_id"],
                "stream_session_id": row["session_id"],
                "recorded_session_id": row["recorded_session_id"],
            }
            for row in rows_raw
            if row["recorded_session_id"]
            and row["recorded_session_id"] != row["session_id"]
        ][:50],
        "unresolved_identity_runs": sum(
            1 for row in rows_raw if not row["node"] or not row["project"]
        ),
        "wall_seconds": round(time.monotonic() - started, 1),
    }
    report["before_window"]["window"] = f"run-id timestamp <= {BOUNDARY_UTC}"
    report["after_window"]["window"] = f"run-id timestamp > {BOUNDARY_UTC}"
    if later_than_median_fresh:
        report["before_window_limitation"] = {
            "session_openers_above_twice_the_fresh_median": len(
                later_than_median_fresh
            ),
            "note": (
                "a session opener whose first turn carries far more than the fresh "
                "median either has a large prompt or resumed a session whose opening "
                "run directory is gone; the session join cannot separate the two, so "
                "they are counted fresh with zero inherited, as in the codex census"
            ),
            "examples": [row["run_id"] for row in later_than_median_fresh[-5:]],
        }
    window = report["after_window"]
    declared_before = declared_summary(before)
    declared_after = declared_summary(after)
    report["declared_resumes"] = {
        "rule": (
            "a launch record whose argv carries --resume <session> is a dispatch "
            "asking to continue that session; its owner is the latest local run "
            "before it whose own stream opened the same session, and a "
            "declaration whose owner is not in the population is recorded "
            "unresolved rather than guessed; where a run holds several attempt "
            "records, the latest declaring attempt is the one counted"
        ),
        "before": declared_before,
        "after": declared_after,
    }
    if window["cross_task_resumes"] == 0:
        report["cross_task_resumes_verdict"] = "zero"
        report["statement"] = (
            f"cross-task resumes are zero over {window['dispatches']} local-lane "
            f"dispatches after {BOUNDARY_UTC}; same-task resumes number "
            f"{window['same_task_resumes']}; {declared_after['declared_resumes']} "
            "dispatches declare a resume in a launch record, of which "
            f"{declared_after['declared_same_task']} target their own task, "
            f"{declared_after['declared_cross_task']} another task and "
            f"{declared_after['declared_unresolved']} an owner outside the population"
        )
    else:
        report["cross_task_resumes_verdict"] = "reported-non-zero"
        report["statement"] = (
            f"cross-task resumes number {window['cross_task_resumes']} over "
            f"{window['dispatches']} local-lane dispatches after {BOUNDARY_UTC}; "
            f"same-task resumes number {window['same_task_resumes']}"
        )
    report["scan_notes"] = (
        "The first-record histogram is the scan's own positive control: the claude "
        f"dialect ({first_types.get('claude', 0)} runs) and the codex dialect "
        f"({first_types.get('codex', 0)} runs) are both present, so the scan "
        "discriminates rather than returning nothing."
    )
    report["rows_note"] = (
        "Per-run rows are stored for every resume in both windows and for the most "
        f"recent {FRESH_ROWS_PER_WINDOW} fresh runs per window; the window summaries "
        "count every dispatch."
    )
    if window["same_task_resumes"] or window["cross_task_resumes"]:
        report["reuse_window_note"] = (
            "The after window contains "
            f"{window['same_task_resumes'] + window['cross_task_resumes']} "
            "inherited transcript(s) visible in the stream population, so session "
            "reuse was exercised on this lane after the boundary: the cross-task "
            "zero is measured over a window in which the reuse path was taken."
        )
    elif declared_after["declared_resumes"]:
        report["reuse_window_note"] = (
            "Every local dispatch after the boundary opened a new session in its "
            "own stream, so the stream population alone would read as a window in "
            "which no session was reused at all. Reuse was still exercised: "
            f"{declared_after['declared_resumes']} dispatches carry a launch record "
            "declaring a resume, of which "
            f"{declared_after['declared_same_task']} target the declaring run's own "
            f"task, {declared_after['declared_cross_task']} another task and "
            f"{declared_after['declared_unresolved']} an owner outside the "
            "population. The cross-task zero is therefore read over a window in "
            "which the reuse path was taken, and the declarations are attributed by "
            "the same identity the stream classification uses."
        )
    else:
        report["reuse_window_note"] = (
            "The after window contains no inherited transcript of either kind and "
            "no launch record declaring a resume, so the cross-task zero is read "
            "over a window in which no session was reused at all; the positive "
            "control proves the same instrument reads a non-zero inheritance."
        )

    # Rows go in last so a writer can see the summaries without them.
    report["before_window"]["rows"] = window_rows(before)
    report["after_window"]["rows"] = window_rows(after)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    report["file_bytes"] = 0
    text = ""
    for _ in range(4):
        text = json.dumps(report, indent=1, default=str)
        measured = len(text.encode())
        if report["file_bytes"] == measured:
            break
        report["file_bytes"] = measured
    args.out.write_text(text)
    size = args.out.stat().st_size
    print(
        f"wrote {args.out} bytes={size} wall={report['wall_seconds']}s "
        f"before={report['before_window']['dispatches']} "
        f"after={report['after_window']['dispatches']}",
        flush=True,
    )
    if args.stdout_summary:
        print(
            json.dumps(
                {
                    "before": report["before_window"]["class_counts"],
                    "after": report["after_window"]["class_counts"],
                    "control": (report["positive_control"] or {}).get("run_id"),
                    "verdict": report["cross_task_resumes_verdict"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
