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
from datetime import datetime, timezone
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

CODE_PATH_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./-]*"
    r"\.(?:py|js|jsx|ts|tsx|html|json|yaml|yml|md|cfg|toml|sh|txt)"
    r"(?::\d+)?"
)

COLOR_MAIN = "#3B6FA0"
COLOR_ALT = "#B4762B"
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
        ts = parse_ts(record.get("timestamp"))
        if ts is None:
            recovery["no_timestamp"] += 1
            continue
        record["_ts"] = ts
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

    plans = {key: {"reviews": reviews} for key, reviews in by_plan.items()}
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
    actual_total = sum(len(v["reviews"]) for v in plans.values())

    # M1 — review at demand: one review per build dispatch.
    m1_run = 0
    m1_lost = []
    m1_plans = {}
    for key, plan in plans.items():
        reviews = plan["reviews"]
        dispatches = dispatch_times.get(key, [])
        selected = set()
        cursor = None
        for moment in dispatches:
            window = [
                idx
                for idx, review in enumerate(reviews)
                if review["_ts"] <= moment and (cursor is None or review["_ts"] > cursor)
            ]
            if window:
                selected.add(window[-1])
            cursor = moment
        m1_run += len(dispatches)
        dropped = [r for idx, r in enumerate(reviews) if idx not in selected]
        lost = [label_for(rv, f) for rv, f in acted_findings(dropped)]
        m1_lost.extend(lost)
        m1_plans[f"{key[0]}/{key[1]}"] = {
            "dispatches": len(dispatches),
            "reviews_actual": len(reviews),
            "reviews_run": len(dispatches),
            "reviews_dropped": len(dropped),
            "acted_lost": len(lost),
        }
    mechanisms["M1-review-at-demand"] = {
        "reviews_run": m1_run,
        "reviews_actual": actual_total,
        "acted_lost_or_deferred": len(m1_lost),
    }
    lost_lists["M1-review-at-demand"] = m1_lost

    # M2 — delta-scoped re-review: the chain continues only while some finding
    # of the newest review lands in a unit changed since the previous review.
    m2_run = 0
    m2_deferred = []
    m2_lost = []
    m2_plans = {}
    for key, plan in plans.items():
        reviews = plan["reviews"]
        stop = len(reviews)
        for i in range(1, len(reviews)):
            prev, nxt = reviews[i - 1], reviews[i]
            if prev["_text"] is None or nxt["_text"] is None:
                continue
            changed = changed_units(prev["_text"], nxt["_text"])
            responses = nxt.get("responses") or {}
            gating = False
            for finding in nxt.get("findings", []):
                unit, _ = finding_unit(finding, nxt["_text"])
                if unit is None or unit in changed:
                    gating = True
                elif (responses.get(finding.get("id")) or {}).get("action") == "acted":
                    m2_deferred.append(label_for(nxt, finding))
            if not gating:
                stop = i + 1
                break
        m2_run += stop
        m2_lost.extend(label_for(rv, f) for rv, f in acted_findings(reviews[stop:]))
        prefix = f"{key[0]}/{key[1]}#v"
        m2_plans[f"{key[0]}/{key[1]}"] = {
            "reviews_actual": len(reviews),
            "reviews_run": stop,
            "acted_deferred": sum(1 for x in m2_deferred if x.startswith(prefix)),
            "acted_lost": sum(1 for x in m2_lost if x.startswith(prefix)),
        }
    mechanisms["M2-delta-scoped"] = {
        "reviews_run": m2_run,
        "reviews_actual": actual_total,
        "acted_lost_or_deferred": len(m2_deferred) + len(m2_lost),
        "detail": {"deferred": len(m2_deferred), "lost": len(m2_lost)},
    }
    lost_lists["M2-delta-scoped"] = m2_deferred + m2_lost

    # M3 — round cap K: after K consecutive all-acted rounds, later findings
    # do not gate, so the chain's edits stop there.
    for k_cap in (1, 2, 3):
        run = 0
        lost = []
        for key, plan in plans.items():
            reviews = plan["reviews"]
            stop = len(reviews)
            streak = 0
            for i, review in enumerate(reviews):
                if i >= 1 and streak >= k_cap:
                    stop = i + 1
                    break
                responses = review.get("responses") or {}
                findings = review.get("findings", [])
                all_acted = bool(findings) and all(
                    (responses.get(f.get("id")) or {}).get("action") == "acted"
                    for f in findings
                )
                streak = streak + 1 if all_acted else 0
            run += stop
            lost.extend(label_for(rv, f) for rv, f in acted_findings(reviews[stop:]))
        mechanisms[f"M3-round-cap-{k_cap}"] = {
            "reviews_run": run,
            "reviews_actual": actual_total,
            "acted_lost_or_deferred": len(lost),
        }
        lost_lists[f"M3-round-cap-{k_cap}"] = lost

    # M4 — longer settle windows, replayed on the plan file's commit times.
    for window in (BASE_SETTLE_SECONDS,) + REPLAY_SETTLE_SECONDS:
        run = 0
        deferred = []
        per_plan = {}
        for key, plan in plans.items():
            reviews = plan["reviews"]
            repo = repos.get(key[0])
            if repo is None:
                continue
            relpath = plan_relative_path(reviews[0])
            hist = store.history(repo, relpath)
            count = 0
            seen = set()
            cluster_last = None
            prev_time = None

            def close_cluster():
                nonlocal count, cluster_last
                if cluster_last is None:
                    return
                text = store.blob_text(repo, cluster_last)
                if text is not None:
                    fp = _cached(text, "fp", plan_review.plan_fingerprint)
                    if fp not in seen:
                        seen.add(fp)
                        count += 1
                cluster_last = None

            for when, sha in hist:
                if (
                    prev_time is not None
                    and (when - prev_time).total_seconds() > window
                ):
                    close_cluster()
                cluster_last = sha
                prev_time = when
            close_cluster()
            for review in reviews:
                sha = str(review.get("reviewed_blob_sha") or "").strip()
                when = (
                    store.commit_time_for_instance(repo, relpath, sha, review["_ts"])
                    if sha
                    else None
                )
                if when is not None and (review["_ts"] - when).total_seconds() > 0:
                    quiet = (review["_ts"] - when).total_seconds()
                else:
                    quiet = None
                if quiet is not None and quiet < window:
                    deferred.extend(
                        label_for(rv, f) for rv, f in acted_findings([review])
                    )
            run += count
            per_plan[f"{key[0]}/{key[1]}"] = {
                "reviews_run": count,
                "reviews_actual": len(reviews),
            }
        mechanisms[f"M4-settle-{window}s"] = {
            "reviews_run": run,
            "reviews_actual": actual_total,
            "acted_lost_or_deferred": len(deferred),
            "per_plan": per_plan,
        }
        lost_lists[f"M4-settle-{window}s"] = deferred

    # M5 — document-unit split: followups and comments leave the fingerprint.
    m5_run = 0
    m5_lost = []
    for key, plan in plans.items():
        reviews = plan["reviews"]
        seen = set()
        stop = len(reviews)
        for i, review in enumerate(reviews):
            if review["_text"] is None:
                continue
            fp = trimmed_fingerprint(review["_text"])
            if fp in seen:
                if stop == len(reviews):
                    stop = i
            else:
                seen.add(fp)
        m5_run += stop
        m5_lost.extend(label_for(rv, f) for rv, f in acted_findings(reviews[stop:]))
    mechanisms["M5-document-split"] = {
        "reviews_run": m5_run,
        "reviews_actual": actual_total,
        "acted_lost_or_deferred": len(m5_lost),
    }
    lost_lists["M5-document-split"] = m5_lost
    note("replay complete")

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
            "lost_counts": {name: len(ids) for name, ids in lost_lists.items()},
            "lost_examples": {name: ids[:40] for name, ids in lost_lists.items()},
            "m1_per_plan": m1_plans,
            "m2_per_plan": m2_plans,
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
    lost = [mechanisms[name]["acted_lost_or_deferred"] for name in order]

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

    ax2.barh(ys, lost, color=COLOR_ALT, height=0.62)
    for y, value in zip(ys, lost):
        ax2.text(value, y, f" {value}", va="center", fontsize=20, color=COLOR_ALT)
    ax2.set_yticks(ys)
    ax2.set_yticklabels([])
    ax2.set_xlabel("acted findings not gating", fontsize=22)
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
        print(f"{name}: run={row['reviews_run']} lost={row['acted_lost_or_deferred']}")
    print("wrote", data_path.name)


if __name__ == "__main__":
    main()