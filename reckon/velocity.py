"""Measure what the fleet delivers: promotions, landed lines and latency.

Ported from the crew-pattern review's census study. The study answered "how
much work lands and how fast" by reading each project's primary-branch history
and its committed run ledger; this module carries the same measurement so the
product can answer it for a window the caller names rather than only the one
the review pinned.

The caller-facing entry point is ``velocity``: it captures a window and returns
the compact summary. The layers it composes — ``capture_project``, ``replay``,
``recover_ledger_clocks``, ``ratio``, ``distribution``, ``measure`` and
``compact_summary`` — stay separate so a test can drive any of them over a
synthesised repository under ``tmp_path``. ``capture`` assembles a window's
projects into the snapshot ``measure`` consumes.

The unit of measurement is one line of a primary-branch net patch, tracked by
identity through edit coordinates, so a deletion is charged against the commit
that introduced the line rather than against the commit that removed it. A
line's deletion enters the seven-day share only when it was born early enough
to have a full seven days of follow-up before the window closed; later births
are right censored and excluded from the denominator. ``ratio`` returns null
rather than dividing by a zero denominator, and ``distribution`` reports median
and p75 beside the population each was drawn from, so a missing denominator is
visible rather than silently read as zero.
"""

from __future__ import annotations

import ast
import collections
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

from reckon._timestamps import parse_utc
from reckon.path_classes import file_class, path_class

__all__ = [
    "CLASSES",
    "CLOSED_STATUSES",
    "LANES",
    "TOKEN_KEYS",
    "capture",
    "capture_project",
    "compact_summary",
    "coordinator_cost",
    "distribution",
    "file_class",
    "measure",
    "path_class",
    "plan_census",
    "plan_cohort",
    "promotion_receipts",
    "ratio",
    "recover_ledger_clocks",
    "report",
    "replay",
    "session_continuity",
    "session_usage",
    "transcript_index",
    "velocity",
]

CODE = Path("/home/ITER/mcintos/Code")
RUN_STORE = Path("/home/ITER/mcintos/.config/reckon/crew/run_store.db")
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
START = "2026-09-12T00:00:00Z"
END = "2026-09-26T10:00:00Z"
# Logical input is the sum of the first three keys measured per response; the
# cache-read share stays visible rather than folded into the uncached figure.
TOKEN_KEYS = (
    "uncached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "input_tokens",
    "output_tokens",
)
PROJECTS = {
    "reckon": "main",
    "imas-ambix": "main",
    "nova": "main",
    "imas-efit": "develop",
    "imas-codex": "main",
}
LANES = ("clive", "codex", "claude", "native", "mixed", "unattributed")
CLASSES = (
    "source",
    "tests",
    "plan_evidence_research_html",
    "figures",
    "docs_state",
    "other",
)
IMPLEMENT = {"implement", "documentation", "test", "cleanup"}
REVIEW = {"review", "investigate"}
WEEK = 7 * 86400
# A plan is closed once its workflow status reaches one of these, or its archive
# flag is set; the earliest such moment is its closing event, counted once
# however many later commits repeat a closed state.
CLOSED_STATUSES = frozenset({"shipped", "done", "superseded", "abandoned"})
PLAN_DIRECTORY = "docs/plans/"
PLAN_SUFFIX = ".html"
_PLAN_STATUS = re.compile(rb'name="plan-status" content="([^"]*)"')
_PLAN_ARCHIVED = re.compile(rb'name="plan-archived" content="([^"]*)"')


def stamp(value):
    if not value or not isinstance(value, str):
        return None
    if value != value.strip() or value.endswith("z"):
        return None
    parsed = parse_utc(value)
    return parsed.timestamp() if parsed is not None else None


def _window_day(text: str):
    """The calendar day a window stamp names at its front, as a date."""

    parsed = parse_utc(text[:10])
    if parsed is None:
        raise ValueError(f"window stamp names no day: {text!r}")
    return parsed.date()


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def _epoch(value):
    """Accept an ISO clock or an epoch second and return an epoch second."""
    return stamp(value) if isinstance(value, str) else value


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=180)


def dump(value):
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def lane(record):
    name = str(
        record.get("backend") or (record.get("agent") or {}).get("backend") or ""
    )
    for family in ("clive", "codex", "claude", "native"):
        if name == family or name.startswith(family + "-"):
            return family
    return "unattributed"


def session_continuity(runs):
    """Classify each run by how its recorded session relates to earlier runs.

    A run continues **another task's** session when its recorded ``session_id``
    equals that of an earlier-dispatched run whose ``(project, plan, node)``
    differs. The first dispatch of a session is ``fresh``; a later dispatch of
    the same task is a ``same_task`` resume, which is not a continuation; a run
    with no recorded ``session_id`` is ``unmeasured`` rather than fresh, because
    an absent signal is not a signal.

    The join is on the recorded ``session_id`` and the run's own dispatch clock,
    so a resolved session is classified by the run it inherits, not by any
    stream artifact. Classification runs over the whole population before any
    lane/day cell is formed, because an earlier run may land outside the cell a
    continuation is counted in.
    """
    result = {}
    prior_tasks = collections.defaultdict(set)
    order = sorted(
        runs, key=lambda r: (r.get("dispatched_at") or "", str(r.get("run_id")))
    )
    for run in order:
        session = run.get("session_id")
        if not session:
            result[run["run_id"]] = "unmeasured"
            continue
        task = (run.get("project"), run.get("plan"), run.get("node"))
        seen = prior_tasks[session]
        if not seen:
            result[run["run_id"]] = "fresh"
        elif seen - {task}:
            result[run["run_id"]] = "continued"
        else:
            result[run["run_id"]] = "same_task"
        seen.add(task)
    return result


def parse_stats(raw):
    result = []
    for chunk in raw.split(b"\x1e")[1:]:
        header, rest = chunk.split(b"\0", 1)
        sha, epoch, parents, subject = header.decode().split("\t", 3)
        files = []
        items = rest.split(b"\0")
        index = 0
        while index < len(items):
            entry = items[index].lstrip(b"\n")
            index += 1
            if not entry:
                continue
            added, removed, path = entry.split(b"\t", 2)
            old = path
            if not path:
                old, path = items[index : index + 2]
                index += 2
            path, old = path.decode(), old.decode()
            files.append(
                {
                    "path": path,
                    "old_path": old,
                    "class": path_class(path),
                    "added": None if added == b"-" else int(added),
                    "removed": None if removed == b"-" else int(removed),
                }
            )
        result.append(
            {
                "sha": sha,
                "epoch": int(epoch),
                "parents": parents.split(),
                "subject": subject,
                "files": files,
            }
        )
    return result


def unquote(value):
    return ast.literal_eval(value) if value.startswith('"') else value


def parse_hunks(raw):
    files, item = [], None
    old_line = new_line = None
    change = None

    def flush():
        nonlocal change
        if change:
            a, removed, b, added = change
            item["hunks"].append(
                [a if removed else a - 1, removed, b if added else b - 1, added]
            )
            change = None

    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            flush()
            item = {"old_path": None, "path": None, "hunks": []}
            files.append(item)
            old_line = new_line = None
        elif line.startswith("@@ "):
            flush()
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                raise ValueError(line)
            a, b, c, d = match.groups()
            old_line = int(a) + (int(b or 1) == 0)
            new_line = int(c) + (int(d or 1) == 0)
        elif old_line is not None and line.startswith(("+", "-", " ")):
            if line.startswith(" "):
                flush()
                old_line += 1
                new_line += 1
            else:
                if change is None:
                    change = [old_line, 0, new_line, 0]
                if line.startswith("-"):
                    change[1] += 1
                    old_line += 1
                else:
                    change[3] += 1
                    new_line += 1
        elif item is not None:
            if line.startswith("rename from "):
                item["old_path"] = unquote(line[12:])
            elif line.startswith("rename to "):
                item["path"] = unquote(line[10:])
            elif line.startswith("--- "):
                name = unquote(line[4:])
                item["old_path"] = None if name == "/dev/null" else name[2:]
            elif line.startswith("+++ "):
                name = unquote(line[4:])
                item["path"] = None if name == "/dev/null" else name[2:]
    flush()
    return [f for f in files if f["hunks"] or f["path"] != f["old_path"]]


def _plan_documents(repo, head, prefix=PLAN_DIRECTORY):
    """Every plan document reachable at ``head`` under the plans directory."""
    listing = git(repo, "ls-tree", "-r", "--name-only", head, "--", prefix).decode()
    return [path for path in listing.splitlines() if path.endswith(PLAN_SUFFIX)]


def _plan_history(repo, head, prefix=PLAN_DIRECTORY):
    """Ordered per-path history of the plans directory, oldest commit first.

    Rename detection is on so a plan that was moved to a new filename is a
    ``R`` entry at the destination rather than a spurious addition. Each path
    carries the commits that touched it, each with its change state (``A`` for
    an added file, ``R`` for a rename, ``M`` for a modification).
    """
    raw = git(
        repo,
        "log",
        "--reverse",
        "--format=%x1e%H%x09%ct",
        "-M",
        "--name-status",
        head,
        "--",
        prefix,
    )
    history = collections.defaultdict(list)
    for chunk in raw.split(b"\x1e")[1:]:
        lines = chunk.decode("utf-8", errors="replace").strip("\n").splitlines()
        if not lines:
            continue
        sha, epoch = lines[0].split("\t")
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split("\t")
            state = parts[0]
            if state[0] == "R":
                path, old = parts[2], parts[1]
            elif state[0] in "AMDC":
                path, old = parts[1], None
            else:
                continue
            if path.endswith(PLAN_SUFFIX):
                history[path].append(
                    {
                        "sha": sha,
                        "epoch": int(epoch),
                        "state": state[0],
                        "old_path": old,
                    }
                )
    return history


def _plan_metas(repo, specs):
    """Read each plan blob once and return its ``(status, archived)`` pair.

    ``specs`` is an ordered list of ``(sha, path)`` pairs; the returned mapping
    is keyed the same way. One ``cat-file --batch`` process serves every plan,
    so a full-history census is a single pass over the object store rather than
    one subprocess per commit.
    """
    if not specs:
        return {}
    payload = "".join(f"{sha}:{path}\n" for sha, path in specs).encode()
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=payload,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    data, position, metas = result.stdout, 0, {}
    for sha, path in specs:
        newline = data.index(b"\n", position)
        header = data[position : newline + 1].decode()
        position = newline + 1
        if header.strip().endswith("missing"):
            content = b""
        else:
            size = int(header.split()[2])
            content = data[position : position + size]
            position += size + 1
        status = _PLAN_STATUS.search(content)
        archived = _PLAN_ARCHIVED.search(content)
        metas[(sha, path)] = (
            status.group(1).decode() if status else "",
            archived.group(1).decode() if archived else "",
        )
    return metas


def _iso_week(epoch):
    day = dt.datetime.fromtimestamp(epoch, dt.UTC).date()
    year, week, _ = day.isocalendar()
    return {
        "iso_week": f"{year}-W{week:02d}",
        "week_start": (day - dt.timedelta(days=day.weekday())).isoformat(),
    }


def plan_census(repo, head, *, start=START, end=END, prefix=PLAN_DIRECTORY):
    """Report plans opened, closed and pending for one repository at ``head``.

    A plan is *opened* by the commit that added its file: the earliest commit
    at which the plan's own path exists, counted only when that commit added
    the file (a path whose first appearance is a rename was not added here).
    It is *closed* by the earliest commit at which its ``plan-status`` reaches
    a closed state or its archive flag is set, counted once however many later
    commits repeat the state. *Pending* is every plan not closed; ``opened``
    and ``closed`` are the project-wide totals, and ``by_week`` buckets the two
    events by ISO week. The scope is the whole repository history reachable at
    ``head``, so a plan opened long before ``start`` still counts as pending.
    """
    repo = Path(repo)
    start, end = _epoch(start), _epoch(end)
    history = _plan_history(repo, head, prefix)
    paths = _plan_documents(repo, head, prefix)
    specs = [
        (entry["sha"], path)
        for path in paths
        for entry in history.get(path, [])
        if entry["state"] in ("A", "C", "M")
    ]
    metas = _plan_metas(repo, specs)
    plans, opened_weeks, closed_weeks = [], collections.Counter(), collections.Counter()
    for path in sorted(paths):
        chain = history.get(path, [])
        if not chain:
            continue
        opened = chain[0]["epoch"] if chain[0]["state"] in ("A", "C") else None
        closed = None
        for entry in chain:
            status, archived = metas.get((entry["sha"], path), ("", ""))
            if archived == "1" or status in CLOSED_STATUSES:
                closed = entry["epoch"]
                break
        plans.append(
            {
                "path": path,
                "opened_epoch": opened,
                "opened_week": _iso_week(opened)["iso_week"]
                if opened is not None
                else None,
                "closed_epoch": closed,
                "closed_week": _iso_week(closed)["iso_week"]
                if closed is not None
                else None,
            }
        )
        if opened is not None:
            opened_weeks[
                (_iso_week(opened)["week_start"], _iso_week(opened)["iso_week"])
            ] += 1
        if closed is not None:
            closed_weeks[
                (_iso_week(closed)["week_start"], _iso_week(closed)["iso_week"])
            ] += 1
    weeks = sorted(set(opened_weeks) | set(closed_weeks))
    return {
        "head": head,
        "window": {"start": iso(start), "end": iso(end)},
        "opened": sum(1 for p in plans if p["opened_epoch"] is not None),
        "closed": sum(1 for p in plans if p["closed_epoch"] is not None),
        "pending": sum(1 for p in plans if p["closed_epoch"] is None),
        "by_week": [
            {
                "week_start": week_start,
                "iso_week": iso_week,
                "opened": opened_weeks.get((week_start, iso_week), 0),
                "closed": closed_weeks.get((week_start, iso_week), 0),
            }
            for week_start, iso_week in weeks
        ],
        "plans": plans,
    }


def plan_cohort(repo, head, *, start=START, end=END, prefix=PLAN_DIRECTORY):
    """Of the plans opened in the window, how many are closed as of ``head``.

    The as-of commit is named rather than taken from today, so the reading is
    reproducible against a pinned revision; a plan counted as closed here is
    one whose closing commit is reachable from ``head``.
    """
    census = plan_census(repo, head, start=start, end=end, prefix=prefix)
    start, end = _epoch(start), _epoch(end)
    opened = [
        plan
        for plan in census["plans"]
        if plan["opened_epoch"] is not None and start <= plan["opened_epoch"] <= end
    ]
    closed = [plan for plan in opened if plan["closed_epoch"] is not None]
    return {
        "head": head,
        "window": {"start": iso(start), "end": iso(end)},
        "opened": len(opened),
        "closed": len(closed),
        "pending": census["pending"],
        "opened_plans": [plan["path"] for plan in opened],
        "closed_plans": [plan["path"] for plan in closed],
        "census": census,
    }


# A project's captured history is expensive to rebuild from the repository:
# the ledger and every committed run record are separate object reads, and the
# primary-branch window is walked commit by commit. The cache below holds one
# entry per repository so a repeated window read answers from it, and a window
# whose head has moved extends the captured commits rather than replaying them.
CACHE_VERSION = 1


def _velocity_cache_root():
    """The directory the captured-history cache lives under.

    Outside every repository, so a cache write never dirties a checkout. The
    location resolves from ``RECKON_VELOCITY_CACHE`` when a caller names one,
    else the user's cache directory (``XDG_CACHE_HOME`` or ``~/.cache``). When
    neither is set but ``RECKON_HOME`` is, the cache stays under that home so a
    caller that isolated its configuration also isolated its cache.
    """
    configured = os.environ.get("RECKON_VELOCITY_CACHE")
    if configured:
        return Path(configured).expanduser()
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home) / "reckon" / "velocity"
    reckon_home = os.environ.get("RECKON_HOME")
    if reckon_home:
        return Path(reckon_home) / "cache" / "velocity"
    return Path.home() / ".cache" / "reckon" / "velocity"


def _velocity_cache_path(repo, cache_root=None):
    """One cache file per repository path.
    """
    root = Path(cache_root) if cache_root is not None else _velocity_cache_root()
    digest = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()
    return root / (digest + ".json")


def _load_captured_history(path):
    """Return the cached entry, or None when it is absent, unreadable or stale.

    A corrupt or version-mismatched entry is never trusted: it is discarded and
    the caller rebuilds it, so a shape this module no longer writes cannot be
    read as if it were current.
    """
    try:
        entry = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("version") != CACHE_VERSION:
        return None
    if not entry.get("window") or not isinstance(entry.get("ledger"), dict):
        return None
    return entry


def _store_captured_history(path, entry):
    from reckon._store import write_json_atomically

    write_json_atomically(path, entry, fsync=False, indent=None)


LEDGER_CACHE_VERSION = 1


def _ledger_cache_path(repo, cache_root=None):
    """The ledger-clock cache beside a repository's captured-history entry.

    A separate file from the captured history because the two are rebuilt by
    different walks: the history by the commit census, the clock recovery by a
    patch-generating log over the ledger path alone.
    """
    root = Path(cache_root) if cache_root is not None else _velocity_cache_root()
    digest = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()
    return root / (digest + ".ledger.json")


def _load_ledger_cache(path):
    """Return the cached recovery, or None when it cannot be trusted.

    Absent, unreadable, version-mismatched and shapeless entries all read as
    None, so a corrupt file is rebuilt rather than read as a recovery that was
    never found.
    """
    try:
        entry = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("version") != LEDGER_CACHE_VERSION:
        return None
    if not isinstance(entry.get("recoveries"), dict):
        return None
    if not isinstance(entry.get("present"), list):
        return None
    return entry


def _store_ledger_cache(path, entry):
    from reckon._store import write_json_atomically

    write_json_atomically(path, entry, fsync=False, indent=None)


def _first_parent_head(repo, branch, end):
    """The first-parent head a window closes on."""
    return (
        git(
            repo,
            "log",
            "-1",
            "--first-parent",
            "--before=" + end,
            "--format=%H",
            branch,
        )
        .decode()
        .strip()
    )


def _is_ancestor(repo, ancestor, descendant):
    if not ancestor or not descendant or ancestor == descendant:
        return ancestor == descendant
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
    )
    return result.returncode == 0


def _blob_sha(repo, head, path):
    """The blob a path names at ``head``, or None when it is absent there."""
    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", head, "--", path],
        capture_output=True,
    ).stdout.decode()
    line = raw.strip()
    return line.split()[2] if line else None


def _run_blobs(repo, head, project):
    """Map every committed run-record path to its blob, in tree order."""
    raw = git(
        repo, "ls-tree", "-r", head, "--", "docs/state/" + project + "/runs"
    ).decode()
    blobs = {}
    for line in raw.splitlines():
        meta, path = line.split("\t", 1)
        blobs[path] = meta.split()[2]
    return blobs


def _read_ledger(repo, head, project, cache):
    """Read the committed ledger, reusing a cached parse when the blob matches.

    The ledger is tens of megabytes on a mature project, so it is read through
    its blob identity: an unchanged blob answers from the cache and the parse is
    skipped, and the raw digest the snapshot reports is kept beside it.
    """
    path = "docs/state/" + project + "/crew.json"
    blob = _blob_sha(repo, head, path)
    if cache is not None and blob is not None and cache.get("blob") == blob:
        return dict(cache["records"]), cache["sha256"]
    raw = git(repo, "show", head + ":" + path)
    payload = json.loads(raw)
    records = {r["run_id"]: r for r in payload.get("data", payload).get("runs", [])}
    digest = hashlib.sha256(raw).hexdigest()
    if cache is not None and blob is not None:
        cache.clear()
        cache.update({"blob": blob, "sha256": digest, "records": records})
    return records, digest


def _read_run_rows(repo, head, project, cache):
    """Read each committed run record, reusing cached rows by blob identity."""
    blobs = _run_blobs(repo, head, project)
    rows = {}
    for path in blobs:
        if not path.endswith(".json"):
            continue
        sha = blobs[path]
        row = cache.get(sha) if cache is not None else None
        if row is None:
            row = json.loads(git(repo, "show", head + ":" + path))
            row = row.get("data", row)
            if cache is not None:
                cache[sha] = row
        if row.get("run_id"):
            rows[row["run_id"]] = row
    return rows, len(blobs)


def _capture_commits(repo, base, head):
    """Capture the primary-branch commits in ``base..head`` and their patches."""
    history = parse_stats(
        git(
            repo,
            "log",
            "--first-parent",
            "--reverse",
            "--diff-merges=first-parent",
            "--find-renames",
            "--numstat",
            "-z",
            "--format=%x1e%H%x09%ct%x09%P%x09%s%x00",
            base + ".." + head,
        )
    )
    product = [
        c for c in history if any(f["class"] in {"source", "tests"} for f in c["files"])
    ]

    def patches(c):
        paths = sorted(
            {
                p
                for f in c["files"]
                if f["class"] in {"source", "tests"}
                for p in (f["path"], f["old_path"])
            }
        )
        raw = git(
            repo,
            "diff",
            "--no-ext-diff",
            "--find-renames",
            "--unified=3",
            c["parents"][0],
            c["sha"],
            "--",
            *paths,
        )
        return c["sha"], parse_hunks(raw)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        patch_index = dict(pool.map(patches, product))
    for c in history:
        c["product_patches"] = patch_index.get(c["sha"], [])
    return history


def capture_project(
    project,
    branch,
    *,
    code_root=CODE,
    repo_path=None,
    start=START,
    end=END,
    run_store_db=RUN_STORE,
    prior=None,
    ledger_cache=None,
    run_cache=None,
):
    # ``repo_path`` names a checkout outright, so a caller holding an arbitrary
    # path does not have to place it at ``code_root/project``; the ledger key
    # stays ``project`` either way, because the run state lives under the
    # project's own name. Without it the historical layout resolves.
    repo = Path(repo_path) if repo_path is not None else Path(code_root) / project
    head = _first_parent_head(repo, branch, end)
    graph = {}
    for line in (
        git(repo, "log", "--format=%H%x09%ct%x09%P%x09%s", head).decode().splitlines()
    ):
        sha, epoch, parents, subject = line.split("\t", 3)
        graph[sha] = {
            "epoch": int(epoch),
            "parents": parents.split(),
            "subject": subject,
        }
    first = (
        git(repo, "rev-list", "--first-parent", "--reverse", head).decode().splitlines()
    )
    selected = [
        sha for sha in first if stamp(start) <= graph[sha]["epoch"] <= stamp(end)
    ]
    base = first[first.index(selected[0]) - 1]
    # A prior capture of the same window extends: its commits up to its own
    # head are reused and only the commits after that head are captured, so a
    # repeated read over the same window does not replay the whole range. The
    # ancestry assertion below still holds, so a head that is not an ancestor
    # of the current one falls back to the full capture.
    history = None
    if prior is not None:
        cached_head = prior.get("head")
        if (
            prior.get("base") == base
            and cached_head
            and _is_ancestor(repo, cached_head, head)
        ):
            history = prior["commits"] + _capture_commits(repo, cached_head, head)
    if history is None:
        history = _capture_commits(repo, base, head)
    # Keep ancestry order, including any skewed commit clocks between endpoints.
    assert [c["sha"] for c in history] == first[first.index(base) + 1 :]
    # The ledger and every committed run record are read through their blob
    # identity, so an unchanged blob answers from the cache and the object read
    # is skipped.
    records, ledger_sha256 = _read_ledger(repo, head, project, ledger_cache)
    run_rows, per_run_files = _read_run_rows(repo, head, project, run_cache)
    records.update(run_rows)
    # Map every newly reachable commit to its first primary-branch appearance.
    seen = set(git(repo, "rev-list", base).decode().splitlines())
    introduction = {}
    for c in history:
        pending = [c["sha"]]
        while pending:
            sha = pending.pop()
            if sha in seen:
                continue
            seen.add(sha)
            introduction[sha] = c["sha"]
            pending.extend(graph[sha]["parents"])
    first_parent = set(first)
    promotions = collections.defaultdict(list)
    for sha, data in graph.items():
        match = re.match(r"promote\((r-[^)]+)\)", data["subject"])
        if match:
            promotions[match[1]].append(
                {
                    "sha": sha,
                    "epoch": data["epoch"],
                    "landing_sha": introduction.get(sha),
                    "first_parent": sha in first_parent,
                    "colon_subject": bool(
                        re.match(r"promote\(r-[^)]+\):", data["subject"])
                    ),
                }
            )
    sources = dict.fromkeys(records, "committed_primary_snapshot")
    recovered = []
    database = Path(run_store_db) if run_store_db else None
    # Only a promotion with no committed record needs the fallback store, and a
    # window whose promotions all carry a record ask it nothing: open it then
    # only, so a caller reading a self-contained checkout never touches a store
    # outside it. The rows read are the same either way.
    missing = sorted(
        rid
        for rid in set(promotions) - set(records)
        if any(
            stamp(start) <= item["epoch"] <= stamp(end) for item in promotions[rid]
        )
    )
    if database is not None and database.exists() and missing:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            for rid in missing:
                hit = connection.execute(
                    "SELECT r.payload,d.detail FROM runs r LEFT JOIN run_details d USING(run_id) WHERE r.run_id=?",
                    (rid,),
                ).fetchone()
                if hit:
                    row = json.loads(hit[0])
                    row.update(json.loads(hit[1] or "{}"))
                    assert row["project"] == project
                    records[rid] = row
                    sources[rid] = "sqlite_fallback_for_reachable_promotion"
                    recovered.append(rid)
    keep = (
        "run_id",
        "node",
        "plan",
        "role",
        "backend",
        "agent",
        "session_id",
        "dispatched_at",
        "completed_at",
        "completed_at_source",
        "worker_seconds",
        "worker_seconds_source",
        "wall_seconds",
        "gate",
        "commits",
        "base_sha",
        "promoted_revision",
        "attempt",
        "attempt_kind",
        "lineage",
        "predecessor_run",
        "failure_classification",
        "clone_matches",
    )
    compact = []
    for rid, record in sorted(records.items()):
        row = {key: record.get(key) for key in keep}
        row["project"] = project
        row["record_source"] = sources[rid]
        row["coordinator"] = (record.get("node_definition") or {}).get(
            "coordinator"
        ) or {}
        row["coordinator"].pop("authoring_turn", None)
        row["promotion_commits"] = sorted(
            promotions.get(rid, []), key=lambda x: (x["epoch"], x["sha"])
        )
        resolved = []
        unresolved = []
        for ref in row["commits"] or []:
            candidates = [sha for sha in graph if sha.startswith(str(ref))]
            if len(candidates) == 1:
                resolved.append(
                    {
                        "sha": candidates[0],
                        "landing_sha": introduction.get(candidates[0]),
                    }
                )
            else:
                unresolved.append(ref)
        row["resolved_commits"] = resolved
        row["unresolved_or_unreachable_commits"] = unresolved
        compact.append(row)
    product_commits = sum(
        1
        for c in history
        if any(f["class"] in {"source", "tests"} for f in c["files"])
    )
    print(
        project,
        "head",
        head,
        "primary_commits",
        len(history),
        "ledger_runs",
        len(compact),
        "product_commits",
        product_commits,
        flush=True,
        file=sys.stderr,
    )
    return {
        "project": project,
        "primary_branch": branch,
        "head": head,
        "base": base,
        "ledger_sha256": ledger_sha256,
        "plan_census": plan_census(repo, head, start=start, end=end),
        "per_run_files": per_run_files,
        "recovered_from_sqlite": recovered,
        "promotion_ids_without_record": sorted(
            rid
            for rid in set(promotions) - set(records)
            if any(
                stamp(start) <= item["epoch"] <= stamp(end) for item in promotions[rid]
            )
        ),
        "runs": compact,
        "commits": history,
    }


def _capture_project_cached(
    project,
    branch,
    *,
    code_root=CODE,
    repo_path=None,
    start=START,
    end=END,
    run_store_db=RUN_STORE,
    cache_root=None,
):
    """Capture a project through the per-repository captured-history cache.

    A read over a window whose head is unchanged answers from the cached
    capture; a head that has moved extends it with only the commits after the
    cached head, and the ledger and committed run records are re-read only
    where their blobs changed. The values returned are exactly those
    ``capture_project`` returns, so a warm read and a cold read agree by
    construction.
    """
    repo = Path(repo_path) if repo_path is not None else Path(code_root) / project
    path = _velocity_cache_path(repo, cache_root)
    entry = _load_captured_history(path)
    ledger_cache = (entry or {}).get("ledger")
    if not isinstance(ledger_cache, dict):
        ledger_cache = {}
    run_cache = (entry or {}).get("run_rows")
    if not isinstance(run_cache, dict):
        run_cache = {}
    window = (entry or {}).get("window") or {}
    prior = window.get("project")
    # The window start fixes the base commit, so a match on it makes the cached
    # capture extendable; the branch name may differ because it only resolves
    # the head, and the cached commits sit on the same line of history either
    # way.
    if not (isinstance(prior, dict) and window.get("start") == start):
        prior = None
    if (
        prior is not None
        and window.get("branch") == branch
        and window.get("end") == end
    ):
        head = _first_parent_head(repo, branch, end)
        if head and head == prior.get("head"):
            return prior
    payload = capture_project(
        project,
        branch,
        code_root=code_root,
        repo_path=repo_path,
        run_store_db=run_store_db,
        start=start,
        end=end,
        prior=prior,
        ledger_cache=ledger_cache,
        run_cache=run_cache,
    )
    _store_captured_history(
        path,
        {
            "version": CACHE_VERSION,
            "repo": str(repo.resolve()),
            "window": {
                "branch": branch,
                "start": start,
                "end": end,
                "project": payload,
            },
            "ledger": ledger_cache,
            "run_rows": run_cache,
        },
    )
    return payload


def replay(commits):
    """Track added-line positions, counting a deletion only against its birth."""
    files = {}
    births = {}
    for commit in commits:
        sha, epoch = commit["sha"], commit["epoch"]
        births[sha] = {
            "added": 0,
            "deleted_within_seven_days": 0,
            "deleted_by_cutoff": 0,
        }
        renames = {}
        for patch in commit["product_patches"]:
            old, new = patch["old_path"], patch["path"]
            positions = files.pop(old, {}) if old else {}
            shift = 0
            for old_start, removed, new_start, added in patch["hunks"]:
                # Zero-length hunk coordinates name the preceding line.
                start = (old_start if removed else old_start + 1) + shift
                after = {}
                for position, born in positions.items():
                    if start <= position < start + removed:
                        births[born]["deleted_by_cutoff"] += 1
                        elapsed = epoch - births[born]["epoch"]
                        if 0 <= elapsed <= WEEK:
                            births[born]["deleted_within_seven_days"] += 1
                    else:
                        after[
                            position
                            + (added - removed if position >= start + removed else 0)
                        ] = born
                target = new_start if added else new_start + 1
                if added:
                    assert target == start, (sha, old, target, start)
                for position in range(target, target + added):
                    assert position not in after, (sha, old, position)
                    after[position] = sha
                positions = after
                births[sha]["added"] += added
                shift += added - removed
            if new:
                renames[new] = positions
            else:
                assert not positions, (
                    sha,
                    old,
                    "deleted file still holds tracked lines",
                )
        files.update(renames)
        births[sha]["epoch"] = epoch
        expected = sum(
            f["added"] or 0
            for f in commit["files"]
            if f["class"] in {"source", "tests"}
        )
        assert births[sha]["added"] == expected, (sha, births[sha]["added"], expected)
        parsed_removed = sum(
            hunk[1] for patch in commit["product_patches"] for hunk in patch["hunks"]
        )
        expected_removed = sum(
            f["removed"] or 0
            for f in commit["files"]
            if f["class"] in {"source", "tests"}
        )
        if any("removed" in f for f in commit["files"]):
            assert parsed_removed == expected_removed, (
                sha,
                parsed_removed,
                expected_removed,
            )
    return births


def recover_ledger_clocks(
    snapshot, *, code_root=CODE, repos=None, start=START, end=END, cache_root=None
):
    """Locate the first durable ledger appearance when no promote commit exists.

    ``repos`` maps a project name to its checkout path, for a snapshot whose
    projects were captured from arbitrary paths rather than beneath one
    ``code_root``; a project absent from it falls back to ``code_root/project``.

    The search is a patch-generating log over the ledger path, one object read
    per commit that touches it, so its result is cached beside the captured
    history — keyed by repository, window base and the head it reached — and a
    head that has moved is searched only over the range the cache had not yet
    covered, for the run ids it had not yet placed.
    """
    for project in snapshot["projects"]:
        candidates = {
            run["run_id"]: run
            for run in project["runs"]
            if not run["promotion_commits"]
            and stamp(start) <= (stamp(run.get("completed_at")) or 0) <= stamp(end)
        }
        if not candidates:
            continue
        repo = (
            Path(repos[project["project"]])
            if repos and project["project"] in repos
            else Path(code_root) / project["project"]
        )
        path = "docs/state/" + project["project"] + "/crew.json"
        cache_path = _ledger_cache_path(repo, cache_root)
        entry = _load_ledger_cache(cache_path)
        base, head = project["base"], project["head"]
        if entry is not None and entry.get("base") == base:
            present = set(entry["present"])
            recovered = dict(entry["recoveries"])
            walked = entry.get("head")
        else:
            before = json.loads(git(repo, "show", base + ":" + path))
            present = {r["run_id"] for r in before.get("data", before).get("runs", [])}
            recovered, walked = {}, None
        candidates = {rid: row for rid, row in candidates.items() if rid not in present}
        if not candidates:
            continue
        found = {rid for rid in recovered if rid in candidates}
        for rid in found:
            candidates[rid]["promotion_commits"] = recovered[rid]
        missing = sorted(set(candidates) - found)
        # An appearance found before the cached head still holds, so only the
        # ids still missing are searched, over the range the cache had not
        # reached. A cached head this one does not descend from cannot supply
        # them, and the walk restarts from the window base.
        if missing and head != walked:
            origin = walked if walked and _is_ancestor(repo, walked, head) else base
            expression = "|".join(re.escape(rid) for rid in missing)
            raw = git(
                repo,
                "log",
                "--first-parent",
                "--reverse",
                "--diff-merges=first-parent",
                "--format=%x1e%H%x09%ct",
                "--unified=0",
                "-p",
                "-G",
                expression,
                origin + ".." + head,
                "--",
                path,
                "docs/state/" + project["project"] + "/runs",
            )
            commit, epoch = None, None
            for line in raw.split(b"\n"):
                if line.startswith(b"\x1e"):
                    commit, value = line[1:].decode().split("\t")
                    epoch = int(value)
                elif line.startswith(b"+"):
                    match = re.search(rb'"run_id"\s*:\s*"([^"]+)"', line)
                    if match and match[1].decode() in candidates:
                        rid = match[1].decode()
                        if rid not in found:
                            appearance = [
                                {
                                    "sha": commit,
                                    "epoch": epoch,
                                    "landing_sha": commit,
                                    "source": (
                                        "first_primary_ledger_appearance_upper_bound"
                                    ),
                                }
                            ]
                            candidates[rid]["promotion_commits"] = appearance
                            recovered[rid] = appearance
                            found.add(rid)
        project["ledger_clock_recoveries"] = sorted(found)
        _store_ledger_cache(
            cache_path,
            {
                "version": LEDGER_CACHE_VERSION,
                "repo": str(Path(repo).resolve()),
                "base": base,
                "head": head,
                "present": sorted(present),
                "recoveries": recovered,
            },
        )
        print(
            project["project"],
            "first durable ledger clocks",
            len(found),
            "/",
            len(candidates),
            flush=True,
            file=sys.stderr,
        )


def positive_controls():
    """Pin insertion, deletion, replacement, rename, age and merge diff parsing."""

    def commit(name, epoch, patches, added, removed=0):
        return {
            "sha": name,
            "epoch": epoch,
            "product_patches": patches,
            "files": [{"class": "source", "added": added, "removed": removed}],
        }

    def patch(old, new, hunks):
        return {"old_path": old, "path": new, "hunks": hunks}

    sample = [
        commit("birth", 0, [patch(None, "a.py", [[0, 0, 1, 3]])], 3),
        commit("insert", 1, [patch("a.py", "a.py", [[1, 0, 2, 1]])], 1),
        commit("rename", 2, [patch("a.py", "b.py", [])], 0),
        commit("remove", 3, [patch("b.py", "b.py", [[3, 1, 2, 0]])], 0, 1),
        commit("replace", 4, [patch("b.py", "b.py", [[1, 1, 1, 2]])], 2, 1),
        commit("late", WEEK + 5, [patch("b.py", None, [[1, 4, 0, 0]])], 0, 4),
    ]
    values = replay(sample)
    assert values["birth"]["deleted_within_seven_days"] == 2
    assert values["birth"]["deleted_by_cutoff"] == 3
    assert values["insert"]["deleted_within_seven_days"] == 0
    assert values["replace"]["deleted_by_cutoff"] == 2
    parsed = parse_stats(
        b"\x1eabc\t1\tp q\tMerge changes\x00\x00\n2\t1\ta.py\0"
        b"0\t0\t\0old.py\0new.py\0-\t-\tplot.png\0"
    )
    assert parsed[0]["files"][1]["old_path"] == "old.py"
    assert parsed[0]["files"][2]["added"] is None
    contextual = parse_hunks(
        b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        b"@@ -1,4 +1,5 @@\n-a\n+b\n+c\n keep\n keep\n-d\n+e\n"
    )
    assert contextual[0]["hunks"] == [[1, 1, 1, 2], [4, 1, 5, 1]]
    print(
        "positive controls: tracked deletion 2/3 within seven days; late deletion excluded; rename preserves identity; binary lines stay null",
        flush=True,
        file=sys.stderr,
    )
    return {
        "line_identity_deletions_within_seven_days": {
            "observed": 2,
            "expected": 2,
            "birth_lines": 3,
        },
        "late_deletion_excluded": True,
        "rename_preserves_identity": True,
        "numstat_rename_and_binary": True,
        "context_lines_not_counted_as_changes": True,
    }


def ratio(numerator, denominator):
    return {
        "numerator": round(numerator, 6),
        "denominator": round(denominator, 6),
        "value": round(numerator / denominator, 6) if denominator else None,
    }


def distribution(values, population):
    values = sorted(values)

    def percentile(p):
        at = (len(values) - 1) * p
        low = int(at)
        return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (
            at - low
        )

    return {
        "denominator": len(values),
        "population": population,
        "missing": population - len(values),
        "median": round(statistics.median(values), 6) if values else None,
        "p75": round(percentile(0.75), 6) if values else None,
    }


def union_seconds(intervals):
    total, end = 0, None
    for start, stop in sorted(intervals):
        if stop <= start:
            continue
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return total


def transcript_index(root):
    """Map each recorded session id to its transcript files under ``root``.

    A session's records can be spread across more than one project directory,
    so every ``*.jsonl`` whose stem is the session id is collected, in a stable
    path order. A root that does not exist yields an empty index rather than an
    error, so a machine without transcripts reports a missing session.
    """
    index = collections.defaultdict(list)
    root = Path(root)
    if not root.is_dir():
        return index
    for directory in sorted(root.iterdir()):
        if directory.is_dir():
            for path in directory.glob("*.jsonl"):
                index[path.stem].append(path)
    for paths in index.values():
        paths.sort()
    return index


def session_usage(paths, *, window_start=START, window_end=END):
    """Assistant responses and token volume for one coordinator session.

    An assistant response is an API response deduplicated by ``message.id``; a
    repeated response — a streaming snapshot that grew between reads — keeps the
    maximum value per usage key rather than being summed again, so re-reading a
    transcript cannot inflate volume. Logical input is uncached input plus cache
    creation plus cache read, and the cache-read share is reported on its own
    rather than folded into the uncached figure. Records outside the window and
    sidechain records are excluded.
    """
    start, end = stamp(window_start), stamp(window_end)
    turns = {}
    records = duplicates = 0
    for path in paths:
        with Path(path).open("r", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("isSidechain"):
                    continue
                when = stamp(record.get("timestamp"))
                if when is None or not start <= when <= end:
                    continue
                if record.get("type") not in (
                    "assistant",
                    "user",
                    "attachment",
                    "system",
                ):
                    continue
                records += 1
                if record.get("type") != "assistant":
                    continue
                message = record.get("message") or {}
                mid = message.get("id") or record.get("uuid")
                if not mid:
                    raise ValueError(f"assistant response without identity in {path}")
                usage = message.get("usage") or {}
                if mid in turns:
                    duplicates += 1
                    turn = turns[mid]
                    for key, value in usage.items():
                        if isinstance(value, int):
                            turn[key] = max(turn.get(key, 0), value)
                else:
                    turns[mid] = {
                        key: value
                        for key, value in usage.items()
                        if isinstance(value, int)
                    }
    tokens = dict.fromkeys(TOKEN_KEYS, 0)
    for turn in turns.values():
        tokens["uncached_input_tokens"] += turn.get("input_tokens", 0)
        tokens["cache_creation_input_tokens"] += turn.get(
            "cache_creation_input_tokens", 0
        )
        tokens["cache_read_input_tokens"] += turn.get("cache_read_input_tokens", 0)
        tokens["output_tokens"] += turn.get("output_tokens", 0)
    tokens["input_tokens"] = sum(tokens[key] for key in TOKEN_KEYS[:3])
    return {
        "assistant_responses": len(turns),
        "in_window_records": records,
        "duplicate_responses": duplicates,
        "tokens": tokens,
    }


def _json_objects(text):
    """Decode whole outer JSON objects in tool-result text, in order."""
    decoder = json.JSONDecoder()
    offset = 0
    while (start := text.find("{", offset)) >= 0:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            offset = start + 1
            continue
        offset = end
        if isinstance(obj, dict):
            yield obj


def _receipt_objects(text):
    """Protocol receipts in a tool result, tolerant of clipped output.

    A receipt is an object carrying a boolean ``ok``. Results are often clipped
    after a few hundred bytes, so a bare ``"ok": true`` marker or an
    ``already_promoted`` line still counts, while a quiet shell never does. A
    printed ``Error:`` line is a failure receipt, and its presence suppresses the
    clipped-marker fallback — a run whose command refused must not also read as a
    success because its clipped tail happened to carry ``"ok": true``.
    """
    receipts = [obj for obj in _json_objects(text) if isinstance(obj.get("ok"), bool)]
    for line in text.splitlines():
        if not line.startswith("{") or "'ok':" not in line:
            continue
        try:
            obj = ast.literal_eval(line)
        except (ValueError, SyntaxError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("ok"), bool):
            receipts.append(obj)
    receipts.extend(
        {"ok": False} for _ in re.findall(r"^Error: (.+)$", text, re.MULTILINE)
    )
    if not receipts:
        receipts.extend(
            {"ok": match.group(1) == "true"}
            for match in re.finditer(r'"ok"\s*:\s*(true|false)', text)
        )
        if not receipts and re.search(
            r'^\s*\{\s*"already_promoted"\s*:\s*(true|false)', text, re.MULTILINE
        ):
            receipts.append({"ok": True, "already_promoted": True})
    return receipts


PROMOTION_VERBS = frozenset({"complete", "promote"})
RUN_ID = re.compile(r"r-\d{8}T\d+-[a-zA-Z0-9_.-]+")


def _shell_tokens(command):
    """Split a shell command the way the shell would, keeping ``_promote`` and
    like as single tokens and breaking on ``;``/``|``/``&&``; an empty list on a
    malformed command rather than raising."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def _crew_verbs(command):
    """The verb token immediately following each ``crew`` token.

    Reading the token after ``crew`` rather than searching the whole command is
    what keeps prose inside a goal or a resume advice from classifying a dispatch
    or a resume as a promotion.
    """
    tokens = _shell_tokens(command)
    return {
        tokens[index + 1] for index, word in enumerate(tokens[:-1]) if word == "crew"
    }


def _literal_flag(command, flag):
    """The one value of a ``--flag value`` pair, else None.

    An interpolated value (``$RUN``) or a repeated flag is ambiguous and refused,
    because the caller reads a plan identifier off it and a guess would attribute
    a landing to the wrong node.
    """
    tokens = _shell_tokens(command)
    values = {
        tokens[index + 1] for index, word in enumerate(tokens[:-1]) if word == flag
    }
    return (
        next(iter(values))
        if len(values) == 1 and not any("$" in v for v in values)
        else None
    )


def promotion_receipts(paths, *, window_start=START, window_end=END):
    """Run ids a coordinator session's transcript records as successfully promoted.

    A call counts only when the token following ``crew`` is a promotion verb, so
    prose inside a goal or a resume advice never classifies a call. Only a result
    that reports success counts. The run id is read from the receipt, or from an
    unquoted ``--run`` flag when the receipt omits it. This is the transcript-side
    landing source: a node can be promoted without leaving a promote commit, and
    the receipt is the only record of it.
    """
    start, end = stamp(window_start), stamp(window_end)
    landed = set()
    for path in paths:
        uses, results = {}, {}
        with Path(path).open("r", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("isSidechain"):
                    continue
                when = stamp(record.get("timestamp"))
                if when is None or not start <= when <= end:
                    continue
                content = (record.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        uses[block["id"]] = str(
                            (block.get("input") or {}).get("command") or ""
                        )
                    elif block.get("type") == "tool_result":
                        text = block.get("content")
                        if isinstance(text, list):
                            text = "\n".join(
                                str(part.get("text", ""))
                                for part in text
                                if isinstance(part, dict)
                            )
                        results[block.get("tool_use_id")] = str(text or "")
        for uid, command in uses.items():
            if not _crew_verbs(command) & PROMOTION_VERBS:
                continue
            tokens = _shell_tokens(command)
            if "--help" in tokens or "--dry-run" in tokens:
                continue
            result = results.get(uid)
            if result is None:
                continue
            for obj in _receipt_objects(result):
                if not obj.get("ok"):
                    continue
                run = obj.get("run_id") or (obj.get("record") or {}).get("run_id")
                if not run:
                    flag = _literal_flag(command, "--run")
                    run = flag if flag and RUN_ID.fullmatch(flag) else None
                if run:
                    landed.add(run)
    return landed


def _landed_in_window(run, receipts, start, end):
    """Census landed rule: an in-window promote commit, a committed marker, or a receipt.

    The promote commit must be on the primary first parent and carry the
    ``promote(<run>):`` colon subject; a bare ``promote(<run>)`` merged in from a
    branch is not a landing on the primary line. A committed ``promoted_revision``
    in the pinned ledger proves a promotion no later than the cutoff. A promotion
    receipt in the coordinator transcript covers a node promoted without either.
    """
    for promotion in run.get("promotion_commits") or []:
        epoch = promotion.get("epoch")
        if epoch is None or not start <= epoch <= end:
            continue
        if promotion.get("first_parent") and promotion.get("colon_subject"):
            return True
    if run.get("promoted_revision") and (
        run.get("record_source") == "committed_primary_snapshot"
    ):
        return True
    return run.get("run_id") in receipts


def coordinator_cost(
    runs,
    *,
    transcript_root=None,
    window_start=START,
    window_end=END,
):
    """Divide each coordinator session's work by the nodes it landed.

    ``runs`` is the cohort of candidate nodes; each row carries its
    ``coordinator`` identity from the run record and is duplicated verbatim. A row
    is counted as landed when the census rule holds: an in-window promote commit
    on the primary first parent with a ``promote(<run>):`` colon subject, or a
    committed ``promoted_revision`` in the pinned ledger, or a successful
    promotion receipt in the coordinator transcript. Rows whose
    ``node_definition.coordinator.runtime_session_id`` is absent fall into an
    explicit unattributed bucket keyed by project and recorded session label, so
    unattributed work is counted rather than silently dropped. A session's
    assistant responses, input tokens, the separately reported cache-read share
    and output tokens are divided by its landed-node count; a session whose
    transcript is absent reports null for those figures rather than zero.
    """
    groups = collections.defaultdict(list)
    attributions = {}
    for run in runs:
        coordinator = run.get("coordinator") or {}
        session = coordinator.get("runtime_session_id") or ""
        if session:
            key, attribution = session, "recorded"
        else:
            key = "unattributed:{}:{}".format(
                run.get("project") or "?", coordinator.get("session_id") or "unknown"
            )
            attribution = "unattributed"
        groups[key].append(run)
        attributions[key] = attribution
    index = transcript_index(transcript_root) if transcript_root is not None else {}
    start, end = stamp(window_start), stamp(window_end)
    sessions = []
    for key, items in sorted(groups.items()):
        paths = index.get(key, []) if transcript_root is not None else []
        receipts = (
            promotion_receipts(paths, window_start=window_start, window_end=window_end)
            if paths
            else set()
        )
        landed = sum(1 for run in items if _landed_in_window(run, receipts, start, end))
        row = {
            "session_id": key,
            "attribution": attributions[key],
            "projects": sorted({str(run.get("project") or "") for run in items}),
            "landed_nodes": landed,
        }
        usage = None
        if transcript_root is None:
            row["transcript_status"] = "unread"
        elif not index.get(key):
            row["transcript_status"] = "missing"
        else:
            usage = session_usage(
                index[key], window_start=window_start, window_end=window_end
            )
            row["transcript_status"] = (
                "captured" if usage["in_window_records"] else "empty-window"
            )
        if row["transcript_status"] == "captured":
            row["assistant_responses"] = usage["assistant_responses"]
            row["tokens"] = usage["tokens"]
            row["assistant_responses_per_landed_node"] = ratio(
                usage["assistant_responses"], landed
            )
            row["tokens_per_landed_node"] = {
                name: ratio(value, landed) for name, value in usage["tokens"].items()
            }
        else:
            row["assistant_responses"] = None
            row["tokens"] = None
            row["assistant_responses_per_landed_node"] = None
            row["tokens_per_landed_node"] = None
        sessions.append(row)
    recorded = [row for row in sessions if row["attribution"] == "recorded"]
    captured = [row for row in recorded if row["transcript_status"] == "captured"]
    denominator = sum(row["landed_nodes"] for row in captured)
    totals = {
        "sessions": len(sessions),
        "sessions_with_transcript": len(captured),
        "sessions_without_transcript": len(recorded) - len(captured),
        "unattributed_sessions": sum(
            row["attribution"] == "unattributed" for row in sessions
        ),
        "unattributed_landed_nodes": sum(
            row["landed_nodes"]
            for row in sessions
            if row["attribution"] == "unattributed"
        ),
        "landed_nodes": sum(row["landed_nodes"] for row in sessions),
        "landed_nodes_with_transcript": denominator,
        "assistant_responses": sum(row["assistant_responses"] for row in captured),
        "tokens": {
            name: sum(row["tokens"][name] for row in captured) for name in TOKEN_KEYS
        },
        "transcript_status": dict(
            sorted(
                collections.Counter(
                    row["transcript_status"] for row in recorded
                ).items()
            )
        ),
    }
    totals["assistant_responses_per_landed_node"] = ratio(
        totals["assistant_responses"], denominator
    )
    totals["tokens_per_landed_node"] = {
        name: ratio(value, denominator) for name, value in totals["tokens"].items()
    }
    return {"sessions": sessions, "totals": totals}


def commit_class(c):
    if len(c["parents"]) > 1:
        return "merge"
    subject = c["subject"].lower()
    if re.match(r"(feat|fix|refactor|perf|test)(\(|:)", subject):
        return "product"
    if re.match(r"(promote|release|record)(\(|:)", subject) or re.match(
        r"docs\((plan|plans|evidence)\)", subject
    ):
        return "record"
    return "other"


def measure(
    snapshot,
    *,
    window_start=START,
    window_end=END,
    projects=None,
    weekly_cells=None,
    transcript_root=None,
):
    if projects is None:
        projects = PROJECTS
    start, end = stamp(window_start), stamp(window_end)
    all_runs, all_commits, coverage = [], [], []
    for project in snapshot["projects"]:
        runs = project["runs"]
        committed = {r["run_id"]: r for r in runs}
        linked = collections.defaultdict(set)
        by_node = collections.defaultdict(list)
        for r in runs:
            by_node[(r["plan"], r["node"])].append(r)
            for commit in r["resolved_commits"]:
                if commit["landing_sha"]:
                    linked[commit["landing_sha"]].add(r["run_id"])
            for promotion in r["promotion_commits"]:
                if promotion["landing_sha"]:
                    linked[promotion["landing_sha"]].add(r["run_id"])
        for commit in project["commits"]:
            match = re.match(
                r"(?:promote|release|record)\((r-[^)]+)\)", commit["subject"]
            )
            if match and match[1] in committed:
                linked[commit["sha"]].add(match[1])
        births = replay(project["commits"])
        for original in project["commits"]:
            if not start <= original["epoch"] <= end:
                continue
            c = dict(original)
            c["project"] = project["project"]
            c["day"] = iso(c["epoch"])[:10]
            c["run_ids"] = sorted(linked[c["sha"]])
            lanes = {lane(committed[rid]) for rid in c["run_ids"]}
            c["lane"] = (
                next(iter(lanes))
                if len(lanes) == 1
                else "mixed"
                if lanes
                else "unattributed"
            )
            c["commit_class"] = commit_class(c)
            c["product"] = births[c["sha"]]
            c["product"]["mature"] = c["epoch"] + WEEK <= end
            c["product"]["durable_seven_days"] = (
                c["product"]["added"] - c["product"]["deleted_within_seven_days"]
                if c["product"]["mature"]
                else None
            )
            del c["product_patches"]
            all_commits.append(c)
        for r in runs:
            promotions = [p for p in r["promotion_commits"] if p["epoch"] <= end]
            promoted = min((p["epoch"] for p in promotions), default=None)
            completed, dispatched = stamp(r["completed_at"]), stamp(r["dispatched_at"])
            if promoted is None or not start <= promoted <= end:
                continue
            row = dict(r)
            row["lane"] = lane(row)
            row["promoted_epoch"] = promoted
            row["promotion_clock_source"] = min(
                promotions, key=lambda p: p["epoch"]
            ).get("source", "promote_commit")
            row["day"] = iso(promoted)[:10]
            row["role_class"] = (
                "implement"
                if r["role"] in IMPLEMENT
                else "review_investigate"
                if r["role"] in REVIEW
                else "unknown"
            )
            row["completion_seconds"] = (
                completed - dispatched
                if completed is not None
                and dispatched is not None
                and dispatched <= completed <= end
                else None
            )
            row["promotion_seconds"] = (
                promoted - dispatched
                if dispatched is not None and dispatched <= promoted
                else None
            )
            root = (r.get("lineage") or {}).get("root_run_id") or r["run_id"]
            family = {
                rid
                for rid, candidate in committed.items()
                if ((candidate.get("lineage") or {}).get("root_run_id") or rid) == root
                and (stamp(candidate.get("dispatched_at")) or end + 1) <= promoted
            }
            family.update(
                candidate["run_id"]
                for candidate in by_node[(r["plan"], r["node"])]
                if (stamp(candidate.get("dispatched_at")) or end + 1) <= promoted
            )
            # Explicit node-name extension, same plan, and earlier dispatch only.
            repair_roots = [
                candidate
                for candidate in runs
                if candidate["plan"] == r["plan"]
                and candidate["node"]
                and r["node"]
                and r["node"] != candidate["node"]
                and (
                    (
                        r["node"].startswith(candidate["node"] + "-")
                        and re.search(
                            r"(repair|retry|fix|resume|redispatch)",
                            r["node"][len(candidate["node"]) :],
                        )
                    )
                    or re.fullmatch(
                        r"(?:repair|retry|fix|resume|redispatch)-"
                        + re.escape(candidate["node"]),
                        r["node"],
                    )
                )
                and (stamp(candidate.get("dispatched_at")) or end + 1)
                < (dispatched or 0)
            ]
            family.update(candidate["run_id"] for candidate in repair_roots)
            # Attempt numbers carry redispatch ancestry; subtract the lineage
            # ordinal before summing so a redispatched attempt is not counted twice.
            attempts = 0
            for rid in family:
                candidate = committed[rid]
                ordinal = (candidate.get("lineage") or {}).get("attempt") or 1
                attempts += max(1, (candidate.get("attempt") or 1) - ordinal + 1)
            row["attempts_observed"] = attempts
            row["own_attempts"] = max(
                1,
                (r.get("attempt") or 1)
                - ((r.get("lineage") or {}).get("attempt") or 1)
                + 1,
            )
            row["attempt_family"] = sorted(family)
            row["repair_name_links"] = [
                candidate["run_id"] for candidate in repair_roots
            ]
            row["landed_in_window"] = any(
                c["landing_sha"] for c in r["resolved_commits"]
            )
            all_runs.append(row)
        current = [
            r for r in runs if start <= (stamp(r.get("completed_at")) or 0) <= end
        ]
        coverage.append(
            {
                "project": project["project"],
                "available_run_records": len(runs),
                "record_sources": dict(
                    sorted(
                        collections.Counter(
                            r.get("record_source", "committed_primary_snapshot")
                            for r in runs
                        ).items()
                    )
                ),
                "recovered_from_sqlite": project.get("recovered_from_sqlite", []),
                "promotion_ids_without_record": project.get(
                    "promotion_ids_without_record", []
                ),
                "ledger_clock_recoveries": project.get("ledger_clock_recoveries", []),
                "completed_in_window": len(current),
                "completed_in_window_without_promotion_clock": [
                    r["run_id"] for r in current if not r["promotion_commits"]
                ],
                "unresolved_or_unreachable_commit_citations": [
                    {
                        "run_id": r["run_id"],
                        "commits": r["unresolved_or_unreachable_commits"],
                    }
                    for r in current
                    if r["unresolved_or_unreachable_commits"]
                ],
            }
        )

    observed_attempts = {}
    for project in snapshot["projects"]:
        for run in project["runs"]:
            observed_attempts[run["run_id"]] = max(
                1,
                (run.get("attempt") or 1)
                - ((run.get("lineage") or {}).get("attempt") or 1)
                + 1,
            )
    parents = {}

    def root(rid):
        parents.setdefault(rid, rid)
        while parents[rid] != rid:
            rid = parents[rid]
        return rid

    for run in all_runs:
        for rid in run["attempt_family"]:
            a, b = root(run["run_id"]), root(rid)
            parents[max(a, b)] = min(a, b)
    for run in all_runs:
        run["logical_node_id"] = root(run["run_id"])
    family_by_root = collections.defaultdict(set)
    for rid in parents:
        family_by_root[root(rid)].add(rid)
    run_index = {run["run_id"]: run for run in all_runs}
    continuity = session_continuity(all_runs)
    for run in all_runs:
        run["session_continuity"] = continuity[run["run_id"]]

    def aggregate(runs, commits):
        lines = {
            category: {
                "added": 0,
                "removed": 0,
                "binary_file_changes": 0,
                "file_changes": 0,
            }
            for category in CLASSES
        }
        for c in commits:
            for f in c["files"]:
                cell = lines[f["class"]]
                cell["file_changes"] += 1
                if f["added"] is None:
                    cell["binary_file_changes"] += 1
                else:
                    cell["added"] += f["added"]
                    cell["removed"] += f["removed"]
        eligible = [c for c in commits if c["product"]["mature"]]
        crew = [c for c in commits if c["run_ids"]]
        eligible_crew = [c for c in eligible if c["run_ids"]]
        continuity_counts = collections.Counter(
            r.get("session_continuity") for r in runs
        )
        product = sum(lines[k]["added"] for k in ("source", "tests"))
        record = sum(
            lines[k]["added"]
            for k in ("plan_evidence_research_html", "figures", "docs_state")
        )
        figure_support = sum(
            f["added"] or 0
            for c in commits
            for f in c["files"]
            if f["class"] == "figures"
            and Path(f["path"]).suffix.lower()
            not in {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".mp4"}
        )
        mature_adds = sum(c["product"]["added"] for c in eligible)
        deleted = sum(c["product"]["deleted_within_seven_days"] for c in eligible)
        durable = mature_adds - deleted
        landed = [r for r in runs if r["landed_in_window"]]
        landed_roots = {r["logical_node_id"] for r in landed}
        attempt_ids = {rid for node in landed_roots for rid in family_by_root[node]}
        worker = [
            r
            for r in runs
            if isinstance(r.get("worker_seconds"), (int, float))
            and r["worker_seconds"] > 0
        ]
        worker_hours = sum(r["worker_seconds"] for r in worker) / 3600
        mature_ids = {
            rid
            for c in eligible_crew
            if c["product"]["added"] > 0
            for rid in c["run_ids"]
        }
        mature_workers = [
            run_index[rid]
            for rid in sorted(mature_ids)
            if rid in run_index
            and isinstance(run_index[rid].get("worker_seconds"), (int, float))
            and run_index[rid]["worker_seconds"] > 0
        ]
        mature_hours = sum(r["worker_seconds"] for r in mature_workers) / 3600
        measured_mature_ids = {r["run_id"] for r in mature_workers}
        matched_mature = [
            c for c in eligible_crew if set(c["run_ids"]) <= measured_mature_ids
        ]
        matched_durable = sum(
            c["product"]["durable_seven_days"] for c in matched_mature
        )
        matched_worker_ids = {
            rid
            for c in matched_mature
            if c["product"]["added"] > 0
            for rid in c["run_ids"]
        }
        mature_workers = [
            r for r in mature_workers if r["run_id"] in matched_worker_ids
        ]
        mature_hours = sum(r["worker_seconds"] for r in mature_workers) / 3600
        groups = collections.defaultdict(list)
        intervals = []
        for r in runs:
            coordinator = r["coordinator"].get("runtime_session_id") or r[
                "coordinator"
            ].get("session_id")
            a, b = stamp(r["dispatched_at"]), stamp(r["completed_at"])
            if coordinator and a is not None and b is not None and a <= b:
                interval = (max(start, a), min(end, b))
                groups[(r["project"], coordinator)].append(interval)
                intervals.append(interval)
        coordinator_hours = sum(union_seconds(v) for v in groups.values()) / 3600
        return {
            "promoted_nodes": {
                "denominator": len(runs),
                "implement_class": sum(r["role_class"] == "implement" for r in runs),
                "review_investigate": sum(
                    r["role_class"] == "review_investigate" for r in runs
                ),
                "review_share": ratio(
                    sum(r["role_class"] == "review_investigate" for r in runs),
                    len(runs),
                ),
                "by_role": dict(
                    sorted(collections.Counter(r["role"] for r in runs).items())
                ),
                "by_gate": dict(
                    sorted(collections.Counter(r["gate"] for r in runs).items())
                ),
            },
            "primary_commits": {
                "denominator": len(commits),
                "by_class": dict(
                    sorted(
                        collections.Counter(c["commit_class"] for c in commits).items()
                    )
                ),
            },
            "lines": lines,
            "crew_attributed_product_additions": ratio(
                sum(c["product"]["added"] for c in crew), product
            ),
            "dispatch_to_completion_seconds": distribution(
                [
                    r["completion_seconds"]
                    for r in runs
                    if r["completion_seconds"] is not None
                ],
                len(runs),
            ),
            "dispatch_to_promotion_seconds": distribution(
                [
                    r["promotion_seconds"]
                    for r in runs
                    if r["promotion_seconds"] is not None
                ],
                len(runs),
            ),
            "promotion_clock_sources": dict(
                sorted(
                    collections.Counter(
                        r["promotion_clock_source"] for r in runs
                    ).items()
                )
            ),
            "dispatch_to_explicit_promotion_seconds": distribution(
                [
                    r["promotion_seconds"]
                    for r in runs
                    if r["promotion_clock_source"] == "promote_commit"
                    and r["promotion_seconds"] is not None
                ],
                len(runs),
            ),
            "logical_promoted_nodes": {
                "denominator": len({r["logical_node_id"] for r in runs}),
                "implement_class": len(
                    {
                        r["logical_node_id"]
                        for r in runs
                        if r["role_class"] == "implement"
                    }
                ),
                "review_investigate": len(
                    {
                        r["logical_node_id"]
                        for r in runs
                        if r["role_class"] == "review_investigate"
                    }
                ),
                "review_share": ratio(
                    len(
                        {
                            r["logical_node_id"]
                            for r in runs
                            if r["role_class"] == "review_investigate"
                        }
                    ),
                    len({r["logical_node_id"] for r in runs}),
                ),
            },
            "attempts_per_landed_node": ratio(
                sum(observed_attempts[rid] for rid in attempt_ids), len(landed_roots)
            ),
            "session_continuity": {
                "denominator": len(runs),
                "continued": continuity_counts.get("continued", 0),
                "same_task": continuity_counts.get("same_task", 0),
                "fresh": continuity_counts.get("fresh", 0),
                "unmeasured": continuity_counts.get("unmeasured", 0),
                "share": ratio(continuity_counts.get("continued", 0), len(runs)),
            },
            "record_to_product_added_line_ratio": ratio(record, product),
            "record_to_product_excluding_figure_directory": ratio(
                record - lines["figures"]["added"], product
            ),
            "figure_directory_support_added_lines": figure_support,
            "record_to_product_excluding_figure_support": ratio(
                record - figure_support, product
            ),
            "product_deleted_within_seven_days": ratio(deleted, mature_adds),
            "durable_product_lines_seven_days": durable,
            "mature_product_additions": mature_adds,
            "right_censored_product_additions": product - mature_adds,
            "observed_surviving_product_lines_at_cutoff": product
            - sum(c["product"]["deleted_by_cutoff"] for c in commits),
            "worker_hours": {
                "value": round(worker_hours, 6),
                "zero_duration_runs": [
                    r["run_id"] for r in runs if r.get("worker_seconds") == 0
                ],
                "zero_duration_with_commits": [
                    r["run_id"]
                    for r in runs
                    if r.get("worker_seconds") == 0 and r["commits"]
                ],
                "denominator_runs": len(worker),
                "population_runs": len(runs),
                "by_source": dict(
                    sorted(
                        collections.Counter(
                            r.get("worker_seconds_source") or "unknown" for r in worker
                        ).items()
                    )
                ),
            },
            "gross_crew_product_lines_per_worker_hour": ratio(
                sum(c["product"]["added"] for c in crew), worker_hours
            ),
            "durable_crew_product_lines_per_worker_hour": {
                **ratio(matched_durable, mature_hours),
                "denominator_runs": len(mature_workers),
                "eligible_product_additions": sum(
                    c["product"]["added"] for c in matched_mature
                ),
                "unmatched_eligible_crew_product_additions": sum(
                    c["product"]["added"]
                    for c in eligible_crew
                    if c not in matched_mature
                ),
            },
            "coordinator_active_dispatch_hours": {
                "value": round(coordinator_hours, 6),
                "denominator_sessions": len(groups),
                "denominator_runs": sum(len(v) for v in groups.values()),
                "population_runs": len(runs),
            },
            "gross_crew_product_lines_per_coordinator_active_dispatch_hour": ratio(
                sum(c["product"]["added"] for c in crew), coordinator_hours
            ),
            "global_active_dispatch_hours": round(union_seconds(intervals) / 3600, 6),
        }

    days = [
        (_window_day(window_start) + dt.timedelta(days=i)).isoformat()
        for i in range((_window_day(window_end) - _window_day(window_start)).days + 1)
    ]

    def cells(keys):
        return [
            {
                **key,
                "metrics": aggregate(
                    [r for r in all_runs if all(r.get(k) == v for k, v in key.items())],
                    [
                        c
                        for c in all_commits
                        if all(c.get(k) == v for k, v in key.items())
                    ],
                ),
            }
            for key in keys
        ]

    if weekly_cells is not None:
        first_day = _window_day(window_start)
        monday = first_day - dt.timedelta(days=first_day.weekday())
        last_day = _window_day(window_end)
        while monday <= last_day:
            next_monday = monday + dt.timedelta(days=7)
            first, last = monday.isoformat(), next_monday.isoformat()
            weekly_cells.append(
                {
                    "week_start": first,
                    "window_start": max(window_start, first + "T00:00:00Z"),
                    "window_end": min(window_end, last + "T00:00:00Z"),
                    "metrics": aggregate(
                        [r for r in all_runs if first <= r["day"] < last],
                        [c for c in all_commits if first <= c["day"] < last],
                    ),
                }
            )
            monday = next_monday

    plan_projects = [
        {
            "project": project["project"],
            "opened": census["opened"],
            "closed": census["closed"],
            "pending": census["pending"],
        }
        for project in snapshot["projects"]
        if (census := project.get("plan_census"))
    ]
    plan_weeks = collections.defaultdict(lambda: {"opened": 0, "closed": 0})
    for project in snapshot["projects"]:
        for row in (project.get("plan_census") or {}).get("by_week", []):
            cell = plan_weeks[(project["project"], row["week_start"], row["iso_week"])]
            cell["opened"] += row["opened"]
            cell["closed"] += row["closed"]
    plans = {
        "by_project": plan_projects,
        "by_project_week": [
            {
                "project": project,
                "week_start": week_start,
                "iso_week": iso_week,
                **cell,
            }
            for (project, week_start, iso_week), cell in sorted(plan_weeks.items())
        ],
        "pending": {
            project["project"]: project["plan_census"]["pending"]
            for project in snapshot["projects"]
            if project.get("plan_census")
        },
        "definition": "Opened is the week of the commit that added the plan file; closed is the week its plan-status first reached shipped/done/superseded/abandoned or its archive flag was set, counted once; pending is every plan not closed.",
    }
    return {
        "window": {
            "start": window_start,
            "end": window_end,
            "elapsed_days": (end - start) / 86400,
            "complete_seven_day_followup_through": iso(end - WEEK),
        },
        "plans": plans,
        "provenance": {
            "input_sha256": hashlib.sha256(dump(snapshot)).hexdigest(),
            "branches": [
                {
                    k: p[k]
                    for k in (
                        "project",
                        "primary_branch",
                        "head",
                        "base",
                        "ledger_sha256",
                        "per_run_files",
                    )
                }
                for p in snapshot["projects"]
            ],
        },
        "definitions": {
            "promotion": "Earliest promote(run-id) commit reachable from captured primary head; event date is its committer clock. If absent, the first primary commit adding the run_id to the durable ledger supplies an explicitly labelled upper-bound clock. A row already present before the window is not recovered this way.",
            "primary_lines": "First-parent net patches, including merge diffs against parent one; additions and deletions separate. Pure renames retain line identity; binary files contribute file counts, not invented lines.",
            "product": "Source and tests by path/suffix; docs SPA JS/JSX/CSS included. Other docs, config, lockfiles and data explicitly remain other.",
            "record": "Added lines of plan/evidence/research HTML, the figures directory and docs/state. Sensitivity ratios exclude figure-support files or the entire figure directory. Other prose/config is separate; record/product is added/added.",
            "lane_attribution": "Resolve ledger commits to reachable objects; assign their first appearance on primary to the citing run lanes. More than one lane is mixed; no citation is unattributed. Final recorded backend used; lane-change runs remain flagged by lineage.",
            "survival": "Exact line identity through edit coordinates parsed from default-context patches (zero-context output can change Git matching). A changed line is deleted even when similar text is re-added. Only births at least seven days before cutoff enter seven-day denominator; later births are right censored.",
            "attempts": "Observable lower bound from committed runs: explicit lineage and same plan/node, plus earlier node-name prefixes followed by repair/retry/fix/resume/redispatch. Attempt minus lineage ordinal counts resumes without duplicating redispatch ordinals; missing discarded runs remain missing. predecessor_run alone is excluded because it can be inferred from base SHA and denotes integration ancestry, not another attempt.",
            "worker_hours": "Recorded worker_seconds, source histogram retained; no stall correction or invented time. Durable rate pairs mature attributed additions with timed promoted runs; failed/review overhead appears in all-promoted gross rate.",
            "coordinator_hours": "Union of dispatch-to-completion intervals per recorded coordinator session, summed across sessions. This is time with workers dispatched, not measured coordinator CPU or interaction time. Day cells are promotion-day cohorts, not time sliced exposure.",
            "daily": "Run rows grouped by promotion event UTC day; lines by primary landing committer UTC day; every project/day/lane combination is emitted, including zero populations.",
            "session_continuity": "Per lane and day, the share of all dispatches whose recorded session_id equals that of an earlier-dispatched run under a different (project, plan, node): a run that continued another task's session. The denominator is every dispatch in the cell; the first dispatch of a session is fresh and a same-task resume is not counted; a dispatch with no recorded session_id is unmeasured and reported beside the share rather than counted fresh or as a continuation. The join uses the recorded session_id, and the classification is computed over the whole population before any cell is formed.",
            "quantiles": "Median and linearly interpolated percentile at (n-1)*p; missing denominator explicitly recorded.",
            "coordinator_cost": "Per coordinator session named by a landed run's node_definition.coordinator.runtime_session_id: assistant responses and logical input, the separately reported cache-read share and output tokens, each divided by the nodes that session landed. A node is landed when it has an in-window promote commit on the primary first parent with a colon subject, or a committed promoted_revision in the pinned ledger, or a successful promotion receipt in the coordinator transcript. The cohort is every run dispatched in the window or promoted in it. A run with no runtime session id enters an explicit unattributed bucket; a session with no transcript reports null rather than zero.",
        },
        "coordinator_cost": coordinator_cost(
            [
                run
                for run in all_runs
                if (
                    (stamp(run.get("dispatched_at")) is not None)
                    and start <= stamp(run.get("dispatched_at")) <= end
                )
                or any(
                    promotion.get("epoch") is not None
                    and start <= promotion["epoch"] <= end
                    for promotion in run["promotion_commits"]
                )
            ],
            transcript_root=transcript_root,
            window_start=window_start,
            window_end=window_end,
        ),
        "positive_controls": positive_controls(),
        "coverage": coverage,
        "august_baseline": snapshot.get("august_baseline"),
        "total": aggregate(all_runs, all_commits),
        "by_project": cells([{"project": p} for p in projects]),
        "by_lane": cells([{"lane": p} for p in LANES]),
        "by_day": cells([{"day": p} for p in days]),
        "by_day_lane": cells(
            [{"day": d, "lane": lane_name} for d in days for lane_name in LANES]
        ),
        "by_project_day_lane": cells(
            [
                {"project": p, "day": d, "lane": lane_name}
                for p in projects
                for d in days
                for lane_name in LANES
            ]
        ),
        "runs": all_runs,
        "commits": all_commits,
    }


def compact_summary(full, weekly_cells, *, artifacts=None):
    """Keep cited aggregates and named coverage without the commit census."""
    summary = {
        key: full[key]
        for key in (
            "window",
            "provenance",
            "definitions",
            "positive_controls",
            "coverage",
            "august_baseline",
            "total",
            "plans",
            "by_project",
            "by_lane",
            "by_day",
            "coordinator_cost",
        )
    }
    summary["by_week"] = weekly_cells
    summary["weekly_definition"] = (
        "UTC Monday-start weeks clipped to the study window; aggregate the original run and commit cohorts, never average daily medians or ratios."
    )
    if artifacts is not None:
        summary["full_artifacts"] = artifacts
    summary["session_continuity"] = [
        {
            "day": cell["day"],
            "lane": cell["lane"],
            **cell["metrics"]["session_continuity"],
        }
        for cell in full["by_day_lane"]
    ]
    points = collections.defaultdict(
        lambda: {"product_additions": 0, "durable_product_lines_seven_days": 0}
    )
    for cell in full["by_project_day_lane"]:
        point = points[(cell["day"], cell["lane"])]
        point["product_additions"] += sum(
            cell["metrics"]["lines"][key]["added"] for key in ("source", "tests")
        )
        point["durable_product_lines_seven_days"] += cell["metrics"][
            "durable_product_lines_seven_days"
        ]
    summary["daily_lane_output"] = [
        {"day": day, "lane": lane_name, **values}
        for (day, lane_name), values in sorted(points.items())
    ]
    summary["named_episodes"] = {
        "zero_duration_with_commits": [
            {
                key: row[key]
                for key in (
                    "run_id",
                    "project",
                    "lane",
                    "worker_seconds",
                    "dispatched_at",
                    "completed_at",
                    "completion_seconds",
                )
            }
            for row in full["runs"]
            if row.get("worker_seconds") == 0 and row["commits"]
        ],
    }
    figure_receipts = [
        (file["added"] or 0, commit, file)
        for commit in full["commits"]
        for file in commit["files"]
        if file["class"] == "figures"
    ]
    if figure_receipts:
        added, commit, file = max(figure_receipts, key=lambda item: item[0])
        summary["named_episodes"]["largest_figure_receipt"] = {
            "project": commit["project"],
            "commit": commit["sha"],
            "path": file["path"],
            "added": added,
        }
    assert len(dump(summary)) < 300_000, (
        "Compact census exceeds its repository size bound"
    )
    return summary


def capture(
    projects=None,
    *,
    start=START,
    end=END,
    code_root=CODE,
    run_store_db=RUN_STORE,
    baseline=None,
):
    """Capture a window's projects into the snapshot ``measure`` consumes.

    ``projects`` maps a project name to its primary branch; the project set and
    the window are the caller's, so the same measurement serves the review's
    pinned study and any later window.
    """
    projects = PROJECTS if projects is None else projects
    snapshot = {
        "window": [start, end],
        "august_baseline": baseline,
        "projects": [
            capture_project(
                project,
                branch,
                code_root=code_root,
                start=start,
                end=end,
                run_store_db=run_store_db,
            )
            for project, branch in projects.items()
        ],
    }
    recover_ledger_clocks(snapshot, code_root=code_root, start=start, end=end)
    return snapshot


def velocity(
    projects=None,
    *,
    start=START,
    end=END,
    code_root=CODE,
    run_store_db=RUN_STORE,
    baseline=None,
    transcript_root=TRANSCRIPT_ROOT,
):
    """Measure a caller-named window and return the compact summary."""
    projects = PROJECTS if projects is None else projects
    snapshot = capture(
        projects,
        start=start,
        end=end,
        code_root=code_root,
        run_store_db=run_store_db,
        baseline=baseline,
    )
    weekly_cells = []
    result = measure(
        snapshot,
        window_start=start,
        window_end=end,
        projects=projects,
        weekly_cells=weekly_cells,
        transcript_root=transcript_root,
    )
    return compact_summary(result, weekly_cells)


def _interface_week_rows(repo, branch, start, end, *, cache_root=None):
    """The reckon package's interface level and weekly change over a window.

    One row per ISO week the window touches: the first-parent revision at the
    week's end, the four counts at that revision, and the change from the
    previous week, taken as the revision at this week's start. Counts are cached
    by resolved sha, so a week whose revision repeats a counted one recomputes
    nothing.
    """
    from reckon import interface_counts

    def level(repo_path, revision):
        return interface_counts.count_revision_cached(
            repo_path, revision, cache_root=cache_root
        )

    first_day, last_day = _window_day(start), _window_day(end)
    monday = first_day - dt.timedelta(days=first_day.weekday())
    rows = []
    while monday <= last_day:
        following = monday + dt.timedelta(days=7)
        week_start = monday.isoformat()
        week_end = min(end, following.isoformat() + "T00:00:00Z")
        revision = _first_parent_head(repo, branch, week_end)
        current = level(repo, revision)
        previous = level(repo, _first_parent_head(repo, branch, week_start))
        year, number, _ = monday.isocalendar()
        rows.append(
            {
                "iso_week": f"{year}-W{number:02d}",
                "week_start": week_start,
                "week_end": week_end,
                "revision": revision or None,
                "counts": current,
                "change": {
                    key: current[key] - previous[key]
                    for key in interface_counts.COUNT_KEYS
                },
            }
        )
        monday = following
    return rows


def _clone_week_cells(runs, window_start, window_end):
    """Count promoted runs' recorded clone matches per ISO week of the window.

    Each cell covers a UTC Monday-start week clipped to the window, the same
    bucketing ``measure`` uses for its own weekly cells, so a week's clone
    figures line up with the promotions that week. A run is charged to the week
    it was promoted.

    Three states are kept apart rather than folded together: a run whose
    function copied an existing one, counted once in ``runs_with_match`` with
    its matches summed into ``matches``; a run the detector measured and found
    nothing in (``runs_no_match``); and a run no revision pair could be measured
    for (``runs_unmeasured``), which is a missing reading and never counted as a
    run that copied nothing.
    """
    first_day = _window_day(window_start)
    monday = first_day - dt.timedelta(days=first_day.weekday())
    last_day = _window_day(window_end)
    cells = []
    while monday <= last_day:
        next_monday = monday + dt.timedelta(days=7)
        first, last = monday.isoformat(), next_monday.isoformat()
        with_match = no_match = unmeasured = matches = 0
        for row in runs:
            if not first <= row["day"] < last:
                continue
            report = row.get("clone_matches")
            if isinstance(report, dict):
                unmeasured += 1
            elif report:
                with_match += 1
                matches += len(report)
            else:
                no_match += 1
        year, number, _ = monday.isocalendar()
        cells.append(
            {
                "iso_week": f"{year}-W{number:02d}",
                "week_start": first,
                "runs_with_match": with_match,
                "matches": matches,
                "runs_no_match": no_match,
                "runs_unmeasured": unmeasured,
            }
        )
        monday = next_monday
    return cells


def report(
    projects,
    *,
    start,
    end,
    branches=None,
    run_store_db=RUN_STORE,
    transcript_root=None,
    baseline=None,
):
    """Measure a caller-named window over caller-named checkouts.

    ``projects`` maps a project name to its checkout path — the shape the MCP
    and command-line surfaces hold, where a mounted project's docs directory
    names its repository parent. Each project is measured on ``branches[name]``
    when given, else on the checkout's own ``HEAD``. The layers are the shared
    ones: ``capture_project`` per checkout, ``recover_ledger_clocks`` once over
    the assembled snapshot, then ``measure`` and ``compact_summary``.

    ``transcript_root`` defaults to ``None``, so the coordinator-cost block
    reports its sessions as an unread transcript rather than walking a
    coordinator transcript tree the caller did not name — the census entry point
    ``velocity`` supplies one because the study reads it.

    The returned mapping is the compact summary — no commit census — with the
    project-by-lane-by-day cells appended, so every quantity the view reports is
    available by project, by lane, by day and in three-dimensional cells. When
    the set names a project called ``reckon``, an ``interfaces`` block carries
    that package's public-definition, CLI-option, MCP-view and coded-refusal
    counts at each ISO week's end and their weekly change.
    """
    branches = branches or {}
    checkouts = {name: str(path) for name, path in projects.items()}
    snapshot = {
        "window": [start, end],
        "august_baseline": baseline,
        "projects": [
            _capture_project_cached(
                name,
                branches.get(name, "HEAD"),
                repo_path=path,
                start=start,
                end=end,
                run_store_db=run_store_db,
            )
            for name, path in checkouts.items()
        ],
    }
    recover_ledger_clocks(snapshot, repos=checkouts, start=start, end=end)
    weekly_cells = []
    full = measure(
        snapshot,
        window_start=start,
        window_end=end,
        projects=list(checkouts),
        weekly_cells=weekly_cells,
        transcript_root=transcript_root,
    )
    summary = compact_summary(full, weekly_cells)
    summary["by_project_day_lane"] = full["by_project_day_lane"]
    summary["clones"] = {
        "weeks": _clone_week_cells(full["runs"], start, end),
        "definition": (
            "Promoted runs' recorded clone matches per UTC Monday-start week "
            "clipped to the window. runs_with_match counts runs whose function "
            "copied an existing one, matches sums their matches, runs_no_match "
            "counts runs the detector measured and found nothing in, and "
            "runs_unmeasured counts runs no revision pair could be measured for "
            "— a missing reading, never counted as a run that copied nothing."
        ),
    }
    # The reckon package is the one whose interface the plan-review rubric
    # reads, so its weekly interface level is reported for the project that
    # carries that name; a window over other projects reports an empty block
    # rather than guessing which package is meant.
    if "reckon" in checkouts:
        summary["interfaces"] = {
            "project": "reckon",
            "weeks": _interface_week_rows(
                checkouts["reckon"],
                branches.get("reckon", "HEAD"),
                start,
                end,
            ),
        }
    else:
        summary["interfaces"] = {"project": None, "weeks": []}
    return summary


# The velocity view answers with its three aggregate tables by default and
# serves every other block only when the caller names it through ``fields``.
# The tables carry full per-cell metrics, so a window of more than a few weeks
# can be narrowed no further than the block choice: the project-lane-day cells
# in particular run to hundreds of thousands of characters and are paged.
OPTIONAL_BLOCKS = frozenset(
    {
        "total",
        "provenance",
        "definitions",
        "positive_controls",
        "coverage",
        "august_baseline",
        "plans",
        "by_week",
        "weekly_definition",
        "session_continuity",
        "daily_lane_output",
        "named_episodes",
        "coordinator_cost",
        "by_project_day_lane",
        "interfaces",
        "clones",
    }
)


def view(
    project,
    *,
    since,
    until=None,
    checkout_path=None,
    fields=None,
    limit=None,
    cursor=None,
):
    """Compose the velocity view for a caller-named window, for either surface.

    One composition serves the MCP read and the command line, so the two
    surfaces cannot disagree about a window. It owns the whole derivation the
    caller sees: the window parsing, the mount resolution, the default summary
    and the paging of the project-lane-day cells.

    ``project`` is one mounted project's name, or ``"*"`` for every mounted
    checkout. ``since`` names the window start and is refused by name when
    absent or unparseable, because there is no safe default: a view that
    silently measured "since forever" would report a window the caller did not
    ask for. ``until`` names the window close and defaults to now.

    The per-project, per-lane and per-day tables are the default answer. Every
    other block is served only when named in ``fields``, and the
    project-lane-day cells — the largest block — are paged by ``limit`` and
    ``cursor``: the count names the size of what is withheld. An unrecognised
    field is refused with the accepted set named.

    Returns the payload both surfaces emit. A refusal is a payload carrying
    ``ok: False`` and a ``detail`` naming the reason, so a caller reads the
    same shape whether the request is served or refused.
    """
    from reckon._store import _docs_dir_for_project
    from reckon.flight import mounted_project_docs
    from reckon.mcp_views import ViewRequestError, error_response, paginate

    if since is None or stamp(since) is None:
        return {
            "ok": False,
            "error": "crew_error",
            "project": project,
            "view": "velocity",
            "detail": (
                "the velocity view needs since=<window start, ISO-8601>; "
                "it is absent or unparseable"
            ),
        }
    window_end = until if until is not None else iso(time.time())
    if stamp(window_end) is None:
        return {
            "ok": False,
            "error": "crew_error",
            "project": project,
            "view": "velocity",
            "detail": (
                "until, when given, must be an ISO-8601 clock; it is unparseable"
            ),
        }
    if project == "*":
        checkouts = {
            name: str(docs.parent) for name, docs in mounted_project_docs().items()
        }
    else:
        docs_dir = _docs_dir_for_project(project, checkout_path)
        if docs_dir is None:
            return {
                "ok": False,
                "error": "crew_error",
                "project": project,
                "view": "velocity",
                "detail": f"project {project!r} has no readable docs directory",
            }
        checkouts = {project: str(docs_dir.parent)}
    payload = report(checkouts, start=since, end=window_end)
    cells = payload.pop("by_project_day_lane", [])
    if isinstance(fields, str):
        requested = [part.strip() for part in fields.split(",")]
    else:
        requested = [str(name) for name in (fields or [])]
    requested = list(dict.fromkeys(name for name in requested if name))
    unknown = sorted(set(requested) - OPTIONAL_BLOCKS)
    if unknown:
        return {
            "ok": False,
            "error": "crew_error",
            "project": project,
            "view": "velocity",
            "detail": (
                "unknown velocity fields "
                + ", ".join(repr(name) for name in unknown)
                + "; optional fields are "
                + ", ".join(sorted(OPTIONAL_BLOCKS))
            ),
        }
    response = {
        "ok": True,
        "view": "velocity",
        "project": project,
        "window": payload["window"],
        "by_project": payload["by_project"],
        "by_lane": payload["by_lane"],
        "by_day": payload["by_day"],
        "by_project_day_lane_count": len(cells),
    }
    for name in requested:
        if name in payload:
            response[name] = payload[name]
    if "by_project_day_lane" in requested:
        try:
            page, pagination = paginate(cells, cursor=cursor, limit=limit)
        except ViewRequestError as exc:
            return error_response(exc.code, exc.message, hint=exc.hint)
        response["by_project_day_lane"] = page
        response["pagination"] = pagination
    return response
