#!/usr/bin/env python3
"""Measure plan-review re-review cycles and replay five damping mechanisms.

Reads the stored plan reviews under ``~/.config/reckon/crew/reviews/``, recovers
each reviewed blob from the project's git object store (or the nearest commit at
or before the review timestamp), and answers five questions about why chains of
re-reviews fail to converge:

1. cadence of re-review chains,
2. which authored unit changed between consecutive reviews,
3. whether a review's finding lands in a unit that changed since the previous
   review (so acting on the previous review plausibly caused it) or in an
   unchanged unit (a fresh reading of content already seen),
4. how often a finding repeats the type of a finding acted on in the previous
   review (instance-at-a-time reporting),
5. a counterfactual replay of five damping mechanisms over the recorded
   timeline.

Run from the worktree root:

    PYTHONPATH=$PWD <repo>/.venv/bin/python \
        docs/figures/plan-review-cycles/measure_review_cycles.py

Writes, beside this file: ``review-cycles-data.json`` (summary rows only),
``fig-review-timeline.png`` and ``fig-mechanisms.png``.
"""

from __future__ import annotations

import collections
import glob
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.style.use("data-ink")

from reckon import _plan_html  # noqa: E402
from reckon.crew import plan_review  # noqa: E402

REVIEWS_ROOT = Path.home() / ".config/reckon/crew/reviews"
MOUNTS_PATH = Path.home() / ".config/reckon/mounts.json"
OUT_DIR = Path(__file__).resolve().parent

# The default window the reflex sweep waits for a plan file to settle, from the
# defaults file; the longer windows are replayed against it.
BASE_SETTLE_SECONDS = 600
REPLAY_SETTLE_SECONDS = (1800, 3600)

# The settle-window replay is a counterfactual baseline calibrated against the
# actual schedule rather than a bound on it: where its quiet-window model fires
# more reviews than a plan actually ran, that disagreement is a calibration
# result reported in the data file and the research document, not a bug the run
# should die on. Every other mechanism must not exceed the actual count, and
# the run exits non-zero if one does.
# The settle-window rows are exempt from the invariant on the documented
# ground that they are a calibrated baseline rather than a bound: the model
# reviews every content state that settled quietly, while the actual system
# reviewed only those a dispatcher demanded, so a plan whose actual schedule
# was sparser than its quiet windows is expected to exceed there. Those
# exceedances are listed in the data file and the research document (the
# m4_calibration table), and M4 savings are always reported against the 600 s
# replay rather than against the actual count. Every other mechanism must not
# exceed the plan's actual count in its window, and would fail the run if it did.
INVARIANT_EXEMPT = {
    f"M4-settle-{window}s"
    for window in (BASE_SETTLE_SECONDS,) + REPLAY_SETTLE_SECONDS
}

CODE_PATH_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./-]*"
    r"\.(?:py|js|jsx|ts|tsx|html|json|yaml|yml|md|cfg|toml|sh|txt)"
    r"(?::\d+)?"
)

COLOR_MAIN = "#3B6FA0"
COLOR_ALT = "#B4762B"
COLOR_DE_GATED = "#5E8F63"
COLOR_NEUTRAL = "#6E6E6E"

# Parsing a plan document is the script's unit of work; every digest, prose map
# and unit set is memoised per document so a blob shared by neighbouring pairs
# or replayed windows is read once.
_DOC_CACHE: dict[bytes, dict] = {}


def _cached(text, name, fn):
    key = hashlib.sha1(text.encode("utf-8", "replace")).digest()
    slot = _DOC_CACHE.setdefault(key, {})
    if name not in slot:
        slot[name] = fn(text)
    return slot[name]


def note(message):
    print(f"[measure] {message}", flush=True)


# ``_section_digests`` and ``_digest_state`` each parse the document; the pair
# comparison and the key diff both ask for one, so the parse is memoised at the
# module boundary the digest helpers call through. The patch is transparent:
# non-string inputs and the keyword are passed to the original unchanged.
_STATE_CACHE: dict[tuple[bytes, bool], object] = {}
_ORIGINAL_DIGEST_STATE = plan_review._digest_state


def _memo_digest_state(plan, *, keep_declarations=False):
    if isinstance(plan, str):
        key = (hashlib.sha1(plan.encode("utf-8", "replace")).digest(), bool(keep_declarations))
        if key not in _STATE_CACHE:
            _STATE_CACHE[key] = _ORIGINAL_DIGEST_STATE(
                plan, keep_declarations=keep_declarations
            )
        return _STATE_CACHE[key]
    return _ORIGINAL_DIGEST_STATE(plan, keep_declarations=keep_declarations)


plan_review._digest_state = _memo_digest_state


def parse_ts(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


RUN_ID_TIME_RE = re.compile(r"r-(\d{8}T\d{6})(\d{0,6})")


def review_run_time(record):
    """The review's own time, from the stamp in its run id.

    A record's ``timestamp`` is when the store wrote it, which for imported
    batches is not when the review ran; the run id carries the launch time.
    """
    match = RUN_ID_TIME_RE.search(str(record.get("review_run_id") or ""))
    if not match:
        return None
    moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
        tzinfo=timezone.utc
    )
    fraction = match.group(2)
    if fraction:
        moment = moment.replace(microsecond=int(fraction.ljust(6, "0")[:6]))
    return moment


def response_round(prev, nxt):
    """True when ``nxt`` answers ``prev``: prev has an acted response stamped
    before nxt ran. Otherwise nxt opens a new chain at fresh authoring."""
    for response in (prev.get("responses") or {}).values():
        if response.get("action") != "acted":
            continue
        when = parse_ts(response.get("when"))
        if when is not None and when < nxt["_ts"]:
            return True
    return False


def review_chains(reviews):
    """Split a plan's reviews into chains, each opening at fresh authoring."""
    chains = []
    current = []
    for index, review in enumerate(reviews):
        if index and not response_round(reviews[index - 1], review):
            chains.append(current)
            current = []
        current.append(review)
    if current:
        chains.append(current)
    return chains


def git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


class BlobStore:
    """Recover plan documents from each project's git object store."""

    def __init__(self):
        self._texts: dict[tuple[str, str], str | None] = {}
        self._histories: dict[tuple[str, str], list[tuple[datetime, str]]] = {}
        self._all_history: dict[str, dict[str, list[tuple[datetime, str]]]] = {}
        self._batches: dict[str, subprocess.Popen] = {}

    def _batch(self, repo):
        key = str(repo)
        if key not in self._batches:
            self._batches[key] = subprocess.Popen(
                ["git", "-C", key, "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        return self._batches[key]

    def blob_text(self, repo, sha):
        key = (str(repo), sha)
        if key not in self._texts:
            proc = self._batch(repo)
            try:
                proc.stdin.write((sha + "\n").encode("utf-8"))
                proc.stdin.flush()
                header = proc.stdout.readline().decode("utf-8", "replace").split()
                if len(header) < 3 or header[1] == "missing":
                    self._texts[key] = None
                else:
                    size = int(header[2])
                    data = proc.stdout.read(size)
                    proc.stdout.read(1)
                    self._texts[key] = data.decode("utf-8", "replace")
            except (BrokenPipeError, OSError, ValueError):
                self._texts[key] = None
        return self._texts[key]

    def history(self, repo, relpath):
        """Return [(commit_time, blob_sha)] for commits touching the path."""
        key = (str(repo), relpath)
        if key in self._histories:
            return self._histories[key]
        entries = self._repo_history(repo).get(relpath, [])
        self._histories[key] = entries
        return entries

    def _repo_history(self, repo):
        """One walk per repository, keyed by plan path."""
        key = str(repo)
        if key in self._all_history:
            return self._all_history[key]
        by_path: dict[str, list[tuple[datetime, str]]] = collections.defaultdict(list)
        code, out, _ = git(
            repo,
            "log",
            "--no-color",
            "--format=%x01%H%x09%cI",
            "--raw",
            "--first-parent",
            "--diff-merges=first-parent",
            "--",
            "docs/plans/",
        )
        if code == 0:
            when = None
            for line in out.splitlines():
                if line.startswith("\x01"):
                    _, _, stamp = line[1:].partition("\t")
                    when = parse_ts(stamp)
                elif line.startswith(":") and when is not None:
                    parts = line[1:].split("\t", 1)
                    meta = parts[0].split()
                    path = parts[1].strip() if len(parts) > 1 else ""
                    if len(meta) >= 4 and path and set(meta[3]) != {"0"}:
                        by_path[path].append((when, meta[3]))
        merged_map: dict[str, list[tuple[datetime, str]]] = {}
        for path, entries in by_path.items():
            entries.sort(key=lambda item: item[0])
            merged: list[tuple[datetime, str]] = []
            for when, sha in entries:
                if not merged or merged[-1][1] != sha:
                    merged.append((when, sha))
            merged_map[path] = merged
        self._all_history[key] = merged_map
        return merged_map

    def commit_time_for_instance(self, repo, relpath, blob_sha, at):
        """Latest commit time at or before ``at`` whose blob equals ``blob_sha``."""
        hist = self.history(repo, relpath)
        exact = [
            when for when, sha in hist if sha == blob_sha and (at is None or when <= at)
        ]
        if exact:
            return max(exact)
        near = [when for when, _ in hist if at is None or when <= at]
        if near:
            return max(near)
        return None


def load_mounts():
    mounts = json.loads(MOUNTS_PATH.read_text(encoding="utf-8"))
    return {project: Path(docs).resolve().parent for project, docs in mounts.items()}


def load_reviews():
    records = []
    for path in sorted(glob.glob(str(REVIEWS_ROOT / "*" / "plan-*.v*.json"))):
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        record["_path"] = path
        records.append(record)
    return records


def plan_relative_path(record):
    return f"docs/plans/{record['plan_slug']}.html"


def load_dispatch_times(repos, projects):
    """Build-dispatch times per (project, plan); role ``review`` is excluded."""
    times: dict[tuple[str, str], list[datetime]] = collections.defaultdict(list)
    for project in projects:
        repo = repos.get(project)
        if repo is None:
            continue
        ledger = repo / "docs" / "state" / project / "crew.json"
        if not ledger.is_file():
            continue
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))["data"]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
        for row in data.get("runs", []):
            if not row.get("plan") or row.get("role") == "review":
                continue
            moment = parse_ts(row.get("dispatched_at")) or parse_ts(
                row.get("completed_at")
            )
            if moment is not None:
                times[(project, str(row["plan"]))].append(moment)
    for key in times:
        times[key].sort()
    return times


def section_prose_map(text):
    prose = {}
    for identity, raw in plan_review._prose_slices(text):
        chunk = _plan_html.strip_tags(raw)
        if chunk:
            prose[identity] = (prose.get(identity, "") + " " + chunk).strip()
    return prose


def trimmed_section_digests(text):
    """Section digests with the followups and comments state keys dropped."""
    buckets: dict[str, list[str]] = {plan_review._DOCUMENT_UNIT: []}
    for identity, raw in plan_review._prose_slices(text):
        buckets.setdefault(identity, []).append(_plan_html.strip_tags(raw))
    payloads = {
        identity: {"prose": " ".join(" ".join(parts).split())}
        for identity, parts in buckets.items()
    }
    state = plan_review._digest_state(text)
    for key in ("followups", "comments"):
        state = {k: v for k, v in state.items() if k != key}
    payloads[plan_review._DOCUMENT_UNIT]["state"] = state
    return {
        identity: plan_review._digest(payload) for identity, payload in payloads.items()
    }


def trimmed_fingerprint(text):
    return _cached(
        text,
        "trimmed",
        lambda t: plan_review._digest(trimmed_section_digests(t)),
    )


def changed_units(prev_text, next_text):
    prev = _cached(prev_text, "sec", plan_review._section_digests)
    nxt = _cached(next_text, "sec", plan_review._section_digests)
    return {k for k in set(prev) | set(nxt) if prev.get(k) != nxt.get(k)}


def declared_units(text):
    units = {plan_review._DOCUMENT_UNIT}
    for identity, _ in plan_review._prose_slices(text):
        units.add(identity)
    return units


def doc_key_diff(prev_text, next_text):
    """Top-level parsed-state keys whose values moved between two documents."""
    prev = _cached(prev_text, "state", plan_review._digest_state)
    nxt = _cached(next_text, "state", plan_review._digest_state)
    moved = []
    for key in sorted(set(prev) | set(nxt)):
        if json.dumps(prev.get(key), sort_keys=True, default=str) != json.dumps(
            nxt.get(key), sort_keys=True, default=str
        ):
            moved.append(key)
    return moved


DOC_BUCKETS = {"decisions", "followups", "comments", "relationships"}


def bucket_for_key(key):
    return key if key in DOC_BUCKETS else "other"


def finding_unit(finding, next_text):
    """Resolve the authored unit a finding lands in, or (None, why-not)."""
    anchor = str(finding.get("anchor") or "")
    units_next = _cached(next_text, "units", declared_units)
    if "#" in anchor:
        frag = anchor.rsplit("#", 1)[1].strip()
        if frag in {"decisions", "followups", "comments"}:
            return plan_review._DOCUMENT_UNIT, "anchor-document"
        candidate = _plan_html.section_record_id(frag)
        if candidate in units_next and candidate != plan_review._DOCUMENT_UNIT:
            return candidate, "anchor-section"
    text = " ".join(
        str(finding.get(field) or "") for field in ("text", "reason", "anchor")
    )
    prose = _cached(next_text, "prose", section_prose_map)
    prose.pop(plan_review._DOCUMENT_UNIT, None)
    hits = []
    for match in CODE_PATH_RE.finditer(text):
        spelled = match.group(0).split(":")[0]
        base = spelled.rsplit("/", 1)[-1]
        for identity, chunk in prose.items():
            if spelled in chunk or base in chunk:
                hits.append(identity)
    hits = sorted(set(hits))
    if len(hits) == 1:
        return hits[0], "code-path"
    if len(hits) > 1:
        return hits[0], "code-path-ambiguous"
    return None, "unclassifiable"


def q1_cadence(plans):
    rows = []
    for key, plan in plans.items():
        reviews = plan["reviews"]
        if len(reviews) < 3:
            continue
        stamps = [r["_ts"] for r in reviews]
        intervals = [
            (stamps[i + 1] - stamps[i]).total_seconds() / 60
            for i in range(len(stamps) - 1)
        ]
        span_hours = (stamps[-1] - stamps[0]).total_seconds() / 3600
        response_gaps = []
        for i in range(len(reviews) - 1):
            whens = [
                parse_ts(resp.get("when"))
                for resp in (reviews[i].get("responses") or {}).values()
            ]
            whens = [w for w in whens if w is not None]
            if whens:
                response_gaps.append(
                    median((stamps[i + 1] - w).total_seconds() / 60 for w in whens)
                )
        rows.append(
            {
                "project": key[0],
                "plan": key[1],
                "reviews": len(reviews),
                "span_hours": round(span_hours, 2),
                "reviews_per_hour": round(len(reviews) / span_hours, 2)
                if span_hours > 0
                else None,
                "median_interval_min": round(median(intervals), 1),
                "median_response_to_next_min": round(median(response_gaps), 1)
                if response_gaps
                else None,
                "response_pairs": len(response_gaps),
            }
        )
    rows.sort(key=lambda row: -row["reviews"])
    return rows


def acted_findings(reviews):
    out = []
    for review in reviews:
        responses = review.get("responses") or {}
        for finding in review.get("findings", []):
            if (responses.get(finding.get("id")) or {}).get("action") == "acted":
                out.append((review, finding))
    return out


def label_for(review, finding):
    return (
        f"{review['project']}/{review['plan_slug']}"
        f"#v{review.get('plan_version')}:{finding.get('id')}"
    )


def main():
    repos = load_mounts()
    records = load_reviews()
    store = BlobStore()

    by_plan: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    recovery = collections.Counter()
    for record in records:
        store_ts = parse_ts(record.get("timestamp"))
        run_ts = review_run_time(record)
        ts = run_ts or store_ts
        if ts is None:
            recovery["no_timestamp"] += 1
            continue
        recovery["time_from_run_id" if run_ts else "time_from_timestamp"] += 1
        if run_ts is not None and store_ts is not None:
            if abs((store_ts - run_ts).total_seconds()) > 300:
                recovery["time_delta_over_5min"] += 1
        record["_ts"] = ts
        record["_store_ts"] = store_ts
        repo = repos.get(record["project"])
        relpath = plan_relative_path(record)
        text = None
        if repo is not None:
            sha = str(record.get("reviewed_blob_sha") or "").strip()
            if sha:
                text = store.blob_text(repo, sha)
                if text is not None:
                    recovery["blob"] += 1
            if text is None:
                before = [item for item in store.history(repo, relpath) if item[0] <= ts]
                if before:
                    text = store.blob_text(repo, before[-1][1])
                    if text is not None:
                        recovery["commit"] += 1
        if text is None:
            recovery["unrecovered"] += 1
        record["_text"] = text
        by_plan[(record["project"], record["plan_slug"])].append(record)

    for reviews in by_plan.values():
        reviews.sort(key=lambda r: (r["_ts"], int(r.get("plan_version") or 0)))
    note(
        f"loaded {len(records)} reviews over {len(by_plan)} plans; "
        f"recovery {dict(recovery)}"
    )

    plans = {}
    for key, reviews in by_plan.items():
        # Every replay stays inside the plan's observed review window: from the
        # settle interval before its first review to its last review.
        plans[key] = {
            "reviews": reviews,
            "window": (
                reviews[0]["_ts"] - timedelta(seconds=BASE_SETTLE_SECONDS),
                reviews[-1]["_ts"],
            ),
        }
    q1 = q1_cadence(plans)

    # ── Q2 / Q3 / Q4: consecutive pairs ─────────────────────────────────────
    q2_rows = []
    q3_rows = []
    q4_by_type: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    q3_counts = collections.Counter()
    doc_only = 0
    doc_key_tally = collections.Counter()
    pair_total = 0
    note("classifying consecutive review pairs")
    for key, plan in plans.items():
        project, slug = key
        reviews = plan["reviews"]
        for i in range(len(reviews) - 1):
            prev, nxt = reviews[i], reviews[i + 1]
            if prev["_text"] is None or nxt["_text"] is None:
                continue
            pair_total += 1
            changed = changed_units(prev["_text"], nxt["_text"])
            moved_keys = doc_key_diff(prev["_text"], nxt["_text"])
            only_document = changed == {plan_review._DOCUMENT_UNIT}
            if only_document:
                doc_only += 1
                for bucket in sorted({bucket_for_key(k) for k in moved_keys}):
                    doc_key_tally[bucket] += 1
            q2_rows.append(
                {
                    "project": project,
                    "plan": slug,
                    "v_prev": prev.get("plan_version"),
                    "v_next": nxt.get("plan_version"),
                    "changed_units": sorted(changed),
                    "only_document": only_document,
                    "doc_keys_moved": moved_keys,
                }
            )
            prev_acted_types = {
                str(f.get("type"))
                for f in prev.get("findings", [])
                if (prev.get("responses") or {}).get(f.get("id"), {}).get("action")
                == "acted"
            }
            prev_all_types = {str(f.get("type")) for f in prev.get("findings", [])}
            for finding in nxt.get("findings", []):
                unit, how = finding_unit(finding, nxt["_text"])
                if unit is None:
                    classification = "unclassifiable"
                elif unit in changed:
                    classification = "changed"
                else:
                    classification = "unchanged"
                q3_counts[classification] += 1
                q3_counts[f"basis:{how}"] += 1
                q3_rows.append(
                    {
                        "project": project,
                        "plan": slug,
                        "v_next": nxt.get("plan_version"),
                        "finding": finding.get("id"),
                        "type": finding.get("type"),
                        "unit": unit,
                        "unit_basis": how,
                        "classification": classification,
                    }
                )
                ftype = str(finding.get("type"))
                q4_by_type[ftype]["findings"] += 1
                if ftype in prev_acted_types:
                    q4_by_type[ftype]["after_acted"] += 1
                if ftype in prev_all_types:
                    q4_by_type[ftype]["after_present"] += 1

    q4_rows = [
        {
            "type": ftype,
            "findings": counts["findings"],
            "after_acted": counts["after_acted"],
            "after_present": counts["after_present"],
        }
        for ftype, counts in sorted(
            q4_by_type.items(), key=lambda kv: -kv[1]["findings"]
        )
    ]

    # ── Q5: counterfactual replay ───────────────────────────────────────────
    dispatch_times = load_dispatch_times(repos, {key[0] for key in plans})
    note(f"dispatch times loaded for {len(dispatch_times)} plans; replaying mechanisms")
    mechanisms: dict[str, dict] = {}
    lost_lists: dict[str, list[str]] = {}
    plan_runs: dict[str, dict[str, tuple[int, int]]] = collections.defaultdict(dict)
    actual_total = sum(len(v["reviews"]) for v in plans.values())

    # M1 — review at demand: one review per build dispatch, counted only when
    # the state it would review differs from the last state a demand review saw.
    m1_run = 0
    m1_lost = []
    m1_plans = {}
    for key, plan in plans.items():
        reviews = plan["reviews"]
        low, high = plan["window"]
        dispatches = [m for m in dispatch_times.get(key, []) if low <= m <= high]
        selected = set()
        last_fp = None
        for moment in dispatches:
            prior = [r for r in reviews if r["_ts"] <= moment]
            if not prior or prior[-1]["_text"] is None:
                continue
            review = prior[-1]
            fp = _cached(review["_text"], "fp", plan_review.plan_fingerprint)
            if fp != last_fp:
                selected.add(id(review))
                last_fp = fp
        m1_run += len(selected)
        dropped = [r for r in reviews if id(r) not in selected]
        lost = [label_for(rv, f) for rv, f in acted_findings(dropped)]
        m1_lost.extend(lost)
        label = f"{key[0]}/{key[1]}"
        m1_plans[label] = {
            "dispatches_in_window": len(dispatches),
            "reviews_actual": len(reviews),
            "reviews_run": len(selected),
            "reviews_dropped": len(dropped),
            "acted_lost": len(lost),
        }
        plan_runs["M1-review-at-demand"][label] = (len(selected), len(reviews))
    mechanisms["M1-review-at-demand"] = {
        "reviews_run": m1_run,
        "reviews_actual": actual_total,
        "acted_lost": len(m1_lost),
        "acted_de_gated": 0,
    }
    lost_lists["M1-review-at-demand"] = m1_lost

    # M2 — delta-scoped re-review, per chain: a chain opens at fresh authoring;
    # after its first round, a round's findings gate only when they land in a
    # unit that changed since the previous round. Rounds after the gating stops
    # are not run; their findings are de-gated.
    m2_run = 0
    m2_de_gated = []
    m2_plans = {}
    for key, plan in plans.items():
        reviews = plan["reviews"]
        chains = review_chains(reviews)
        run = 0
        de_gated = 0
        for chain in chains:
            keep = len(chain)
            for i in range(1, len(chain)):
                prev, nxt = chain[i - 1], chain[i]
                if prev["_text"] is None or nxt["_text"] is None:
                    continue
                changed = changed_units(prev["_text"], nxt["_text"])
                responses = nxt.get("responses") or {}
                gating = False
                for finding in nxt.get("findings", []):
                    unit, _ = finding_unit(finding, nxt["_text"])
                    if unit is None or unit in changed:
                        gating = True
                    elif (responses.get(finding.get("id")) or {}).get(
                        "action"
                    ) == "acted":
                        m2_de_gated.append(label_for(nxt, finding))
                        de_gated += 1
                if not gating:
                    keep = i + 1
                    break
            run += keep
            tail = [label_for(rv, f) for rv, f in acted_findings(chain[keep:])]
            m2_de_gated.extend(tail)
            de_gated += len(tail)
        m2_run += run
        label = f"{key[0]}/{key[1]}"
        m2_plans[label] = {
            "reviews_actual": len(reviews),
            "reviews_run": run,
            "acted_de_gated": de_gated,
            "chains": len(chains),
        }
        plan_runs["M2-delta-scoped"][label] = (run, len(reviews))
    mechanisms["M2-delta-scoped"] = {
        "reviews_run": m2_run,
        "reviews_actual": actual_total,
        "acted_lost": 0,
        "acted_de_gated": len(m2_de_gated),
    }
    lost_lists["M2-delta-scoped"] = m2_de_gated

    # M3 — round cap K, applied per chain: within a chain, rounds run until K
    # consecutive rounds whose findings were all acted have occurred; the
    # remaining rounds of that chain do not run and do not gate.
    for k_cap in (1, 2, 3):
        run = 0
        de_gated = []
        for key, plan in plans.items():
            reviews = plan["reviews"]
            plan_run = 0
            for chain in review_chains(reviews):
                streak = 0
                keep = 0
                for review in chain:
                    if keep >= 1 and streak >= k_cap:
                        break
                    keep += 1
                    responses = review.get("responses") or {}
                    findings = review.get("findings", [])
                    all_acted = bool(findings) and all(
                        (responses.get(f.get("id")) or {}).get("action") == "acted"
                        for f in findings
                    )
                    streak = streak + 1 if all_acted else 0
                plan_run += keep
                de_gated.extend(
                    label_for(rv, f) for rv, f in acted_findings(chain[keep:])
                )
            run += plan_run
            plan_runs[f"M3-round-cap-{k_cap}"][f"{key[0]}/{key[1]}"] = (
                plan_run,
                len(reviews),
            )
        mechanisms[f"M3-round-cap-{k_cap}"] = {
            "reviews_run": run,
            "reviews_actual": actual_total,
            "acted_lost": 0,
            "acted_de_gated": len(de_gated),
        }
        lost_lists[f"M3-round-cap-{k_cap}"] = de_gated

    # M4 — longer settle windows, replayed on the plan file's commit times
    # inside each plan's window. A content state is reviewed when it has been
    # quiet for the window and its fingerprint was not already reviewed.
    m4_calibration = {}
    m4_baseline_fps = {}
    for window in (BASE_SETTLE_SECONDS,) + REPLAY_SETTLE_SECONDS:
        run = 0
        lost = []
        per_plan = {}
        for key, plan in plans.items():
            reviews = plan["reviews"]
            repo = repos.get(key[0])
            low, high = plan["window"]
            if repo is None:
                continue
            relpath = plan_relative_path(reviews[0])
            history = [
                item for item in store.history(repo, relpath) if low <= item[0] <= high
            ]
            fires = []
            prev_time = None
            last_sha = None
            for when, sha in history:
                if (
                    prev_time is not None
                    and (when - prev_time).total_seconds() > window
                ):
                    fires.append((prev_time, last_sha))
                prev_time = when
                last_sha = sha
            if prev_time is not None:
                fires.append((prev_time, last_sha))
            reviewed_fps = set()
            count = 0
            for write_time, sha in fires:
                # A cluster counts by the time the content it reviews was
                # written; counting by its fire time instead would make the
                # count non-monotone in the window, because a smaller window
                # fires before the window opens and is discarded there.
                if not (low <= write_time <= high):
                    continue
                text = store.blob_text(repo, sha) if sha else None
                if text is None:
                    continue
                fp = _cached(text, "fp", plan_review.plan_fingerprint)
                if fp in reviewed_fps:
                    continue
                reviewed_fps.add(fp)
                count += 1
            matched = 0
            baseline = m4_baseline_fps.get(f"{key[0]}/{key[1]}", set())
            for review in reviews:
                if review["_text"] is None:
                    continue
                fp = _cached(review["_text"], "fp", plan_review.plan_fingerprint)
                if fp in reviewed_fps:
                    matched += 1
                elif window == BASE_SETTLE_SECONDS or fp in baseline:
                    # At the baseline window a finding is lost against the
                    # actual history (the calibration gap); at longer windows
                    # it is lost only when the baseline would have read it and
                    # this window does not, so the count is the mechanism's
                    # own marginal effect rather than the model's gap.
                    lost.extend(
                        label_for(rv, f) for rv, f in acted_findings([review])
                    )
            label = f"{key[0]}/{key[1]}"
            if window == BASE_SETTLE_SECONDS:
                m4_baseline_fps[label] = set(reviewed_fps)
            per_plan[label] = {
                "reviews_run": count,
                "reviews_actual": len(reviews),
                "reviews_matched": matched,
                "reviews_baseline_600s": len(baseline) if window != BASE_SETTLE_SECONDS else count,
            }
            plan_runs[f"M4-settle-{window}s"][label] = (count, len(reviews))
            if window == BASE_SETTLE_SECONDS:
                m4_calibration[label] = dict(per_plan[label])
            run += count
        mechanisms[f"M4-settle-{window}s"] = {
            "reviews_run": run,
            "reviews_actual": actual_total,
            "acted_lost": len(lost),
            "acted_de_gated": 0,
        }
        lost_lists[f"M4-settle-{window}s"] = lost

    # M4 savings are ratios to the 600 s replay baseline, never to the actual
    # count, because the replay is a calibrated model rather than a bound.
    baseline_600 = mechanisms[f"M4-settle-{BASE_SETTLE_SECONDS}s"]["reviews_run"]
    for window in REPLAY_SETTLE_SECONDS:
        mechanisms[f"M4-settle-{window}s"]["ratio_to_600s"] = (
            round(
                mechanisms[f"M4-settle-{window}s"]["reviews_run"] / baseline_600, 2,
            )
            if baseline_600
            else None
        )

    # M5 — document-unit split: followups and authored comments leave the
    # fingerprint, so a review whose non-document content was already reviewed
    # is not run; its findings are recorded without gating.
    m5_run = 0
    m5_de_gated = []
    for key, plan in plans.items():
        reviews = plan["reviews"]
        seen = set()
        run = 0
        for review in reviews:
            if review["_text"] is None:
                run += 1
                continue
            fp = trimmed_fingerprint(review["_text"])
            if fp in seen:
                m5_de_gated.extend(
                    label_for(rv, f) for rv, f in acted_findings([review])
                )
                continue
            seen.add(fp)
            run += 1
        m5_run += run
        plan_runs["M5-document-split"][f"{key[0]}/{key[1]}"] = (run, len(reviews))
    mechanisms["M5-document-split"] = {
        "reviews_run": m5_run,
        "reviews_actual": actual_total,
        "acted_lost": 0,
        "acted_de_gated": len(m5_de_gated),
    }
    lost_lists["M5-document-split"] = m5_de_gated

    # Invariant: a damping mechanism may not run more reviews than the plan
    # actually ran inside its window. An exceedance means the replay is wrong.
    violations = []
    exempted = []
    for name, rows in plan_runs.items():
        for label, (ran, actual) in rows.items():
            if ran > actual:
                if name in INVARIANT_EXEMPT:
                    exempted.append(f"{name} {label}: {ran} > {actual}")
                else:
                    violations.append(f"{name} {label}: ran {ran} > actual {actual}")
    exempt_rows = []
    for line in exempted:
        note(f"invariant exemption (documented): {line}")
        name, rest = line.split(" ", 1)
        label, counts = rest.split(": ", 1)
        ran_text, actual_text = counts.split(" > ")
        exempt_rows.append(
            {
                "mechanism": name,
                "plan": label,
                "reviews_run": int(ran_text),
                "reviews_actual": int(actual_text),
            }
        )
    if violations:
        for line in violations:
            print("INVARIANT VIOLATION:", line, flush=True)
        note("invariant violated; refusing to write outputs")
        return 2
    note("replay complete")
    m4_calibration_rows = {}
    for label, row in sorted(m4_calibration.items()):
        actual = row["reviews_actual"]
        replayed = row["reviews_run"]
        ratio = round(replayed / actual, 3) if actual else None
        m4_calibration_rows[label] = {
            "actual": actual,
            "replayed_600s": replayed,
            "ratio": ratio,
            "over_25pct": ratio is not None and abs(ratio - 1.0) > 0.25,
        }

    # ── Data file ───────────────────────────────────────────────────────────
    data = {
        "store": {
            "reviews": len(records),
            "plans": len(plans),
            "plans_with_3plus": len(q1),
            "recovery": dict(recovery),
            "consecutive_pairs_with_blobs": pair_total,
            "zero_finding_reviews": sum(
                1
                for plan in plans.values()
                for r in plan["reviews"]
                if not r.get("findings")
            ),
            "responses_acted": sum(
                1
                for plan in plans.values()
                for r in plan["reviews"]
                for resp in (r.get("responses") or {}).values()
                if resp.get("action") == "acted"
            ),
            "responses_declined": sum(
                1
                for plan in plans.values()
                for r in plan["reviews"]
                for resp in (r.get("responses") or {}).values()
                if resp.get("action") == "declined"
            ),
            "time_from_run_id": recovery.get("time_from_run_id", 0),
            "time_from_timestamp_fallback": recovery.get("time_from_timestamp", 0),
            "time_delta_over_5min": recovery.get("time_delta_over_5min", 0),
        },
        "q1_cadence": q1,
        "q2_trigger": {
            "pairs": q2_rows,
            "only_document": doc_only,
            "doc_key_breakdown": dict(doc_key_tally),
        },
        "q3_classification": {
            "counts": dict(q3_counts),
            "total_findings": len(q3_rows),
            "by_plan": {
                f"{pk[0]}:{pk[1]}": {
                    cls: sum(
                        1
                        for r in q3_rows
                        if (r["project"], r["plan"]) == pk
                        and r["classification"] == cls
                    )
                    for cls in ("changed", "unchanged", "unclassifiable")
                }
                for pk in sorted({(r["project"], r["plan"]) for r in q3_rows})
            },
        },
        "q4_instance_at_a_time": q4_rows,
        "q5_mechanisms": {
            "mechanisms": mechanisms,
            "stopped_counts": {name: len(ids) for name, ids in lost_lists.items()},
            "stopped_examples": {name: ids[:12] for name, ids in lost_lists.items()},
            "m1_per_plan": m1_plans,
            "m2_per_plan": m2_plans,
            "m4_calibration": m4_calibration_rows,
            "m4_exceedances": exempt_rows,
        },
    }
    data_path = OUT_DIR / "review-cycles-data.json"
    data_path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")

    # ── Figures ─────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(14, 10))
    for ax, row in zip(axes, q1[:3]):
        key = (row["project"], row["plan"])
        reviews = plans[key]["reviews"]
        xs = [r["_ts"] for r in reviews]
        ys = [len(r.get("findings", [])) for r in reviews]
        ax.vlines(xs, 0, ys, color=COLOR_MAIN, linewidth=1.6, alpha=0.85)
        ax.plot(xs, ys, "o", color=COLOR_MAIN, markersize=4)
        ax.set_ylim(0, max(ys) + 1.6 if ys else 1)
        ax.set_ylabel("findings per review", fontsize=22)
        ax.text(
            0.01,
            0.94,
            f"{row['plan']}\n{row['reviews']} reviews",
            transform=ax.transAxes,
            fontsize=20,
            va="top",
            ha="left",
            color=COLOR_MAIN,
        )
        ax.tick_params(axis="x", labelsize=20)
        ax.tick_params(                    axis="y", labelsize=20)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "fig-review-timeline.png", dpi=100)
    plt.close(fig)

    order = [
        "M1-review-at-demand",
        "M2-delta-scoped",
        "M3-round-cap-1",
        "M3-round-cap-2",
        "M3-round-cap-3",
        "M4-settle-1800s",
        "M4-settle-3600s",
        "M5-document-split",
    ]
    labels = [name.split("-", 1)[1] for name in order]
    runs = [mechanisms[name]["reviews_run"] for name in order]
    # Two different consequences, kept apart: a "lost" finding is one no
    # reviewer would ever have read (the schedule-changing mechanisms M1 and
    # M4); a "de-gated" finding is one still recorded but without gating force
    # (the gating-changing mechanisms M2, M3 and M5).
    stopped = [
        mechanisms[name]["acted_lost"]
        + mechanisms[name]["acted_de_gated"]
        for name in order
    ]
    category = [
        "de-gated" if mechanisms[name]["acted_de_gated"] else "lost"
        for name in order
    ]
    colors = [
        COLOR_DE_GATED if cat == "de-gated" else COLOR_ALT for cat in category
    ]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 7))
    ys = list(range(len(order)))
    ax1.barh(ys, runs, color=COLOR_MAIN, height=0.62)
    ax1.axvline(actual_total, color=COLOR_NEUTRAL, linestyle=":", linewidth=1.2)
    ax1.text(
        actual_total,
        7.9,
        f"actual {actual_total}",
        fontsize=20,
        color=COLOR_NEUTRAL,
        va="top",
        ha="right",
    )
    for y, value in zip(ys, runs):
        ax1.text(value, y, f" {value}", va="center", fontsize=20, color=COLOR_MAIN)
    ax1.set_yticks(ys)
    ax1.set_yticklabels(labels, fontsize=20)
    ax1.set_xlabel("reviews run", fontsize=22)
    ax1.tick_params(axis="x", labelsize=20)

    ax2.barh(ys, stopped, color=colors, height=0.62)
    for y, value, cat in zip(ys, stopped, category):
        ax2.text(value, y, f" {value} {cat}", va="center", fontsize=20,
                 color=COLOR_DE_GATED if cat == "de-gated" else COLOR_ALT)
    ax2.set_yticks(ys)
    ax2.set_yticklabels([])
    ax2.set_xlabel("acted findings stopped", fontsize=22)
    ax2.tick_params(axis="x", labelsize=20)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "fig-mechanisms.png", dpi=100)
    plt.close(fig)

    print("recovery:", dict(recovery))
    print("plans:", len(plans), "reviews:", len(records), "pairs:", pair_total)
    print("q3:", dict(q3_counts))
    print(
        "q2 only-document:",
        doc_only,
        "of",
        pair_total,
        "breakdown:",
        dict(doc_key_tally),
    )
    for name in order:
        row = mechanisms[name]
        print(
            f"{name}: run={row['reviews_run']} lost={row['acted_lost']} "
            f"de_gated={row['acted_de_gated']} actual={row['reviews_actual']}"
        )
    disagree = [label for label, row in m4_calibration_rows.items() if row["over_25pct"]]
    total_actual = sum(row["actual"] for row in m4_calibration_rows.values())
    total_replayed = sum(row["replayed_600s"] for row in m4_calibration_rows.values())
    print(
        "m4 calibration vs actual:",
        f"{total_replayed}/{total_actual}",
        f"({round(100 * total_replayed / total_actual)}%)",
        "plans over 25%:",
        len(disagree),
        "of",
        len(m4_calibration_rows),
    )
    print("wrote", data_path.name)
    for window in REPLAY_SETTLE_SECONDS:
        row = mechanisms[f"M4-settle-{window}s"]
        print(
            f"M4-settle-{window}s ratio to 600s baseline: "
            f"{row['reviews_run']}/{baseline_600} = {row['ratio_to_600s']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())