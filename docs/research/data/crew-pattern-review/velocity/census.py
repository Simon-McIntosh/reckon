"""Capture immutable crew/history inputs and measure primary-branch velocity.

Run with --capture once; subsequent runs use the compressed input snapshot in
the run directory. Only compact aggregate output is checked in. Git hunk
coordinates preserve line identity across edits and renames.
No source text, worker prompts, credentials, or transcript bodies are retained.
"""

from __future__ import annotations

import argparse
import ast
import collections
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import json
import re
import sqlite3
import statistics
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN = Path(
    "/home/ITER/mcintos/.config/reckon/crew/runs/r-20260926T104517371666-velocity-census"
)
FULL_OUTPUT = RUN / "velocity.json"
COMPACT_OUTPUT = HERE / "summary.json"
CODE = Path("/home/ITER/mcintos/Code")
START = "2026-09-12T00:00:00Z"
END = "2026-09-26T10:00:00Z"
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


def stamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=parsed.tzinfo or dt.UTC).timestamp()
    except ValueError:
        return None


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=180)


def dump(value):
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def file_class(path):
    lower = path.lower()
    parts = lower.split("/")
    if lower.startswith("docs/state/"):
        return "docs_state"
    if lower.startswith("docs/figures/") or Path(lower).suffix in {
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".pdf",
        ".mp4",
    }:
        return "figures"
    if lower.endswith(".html") and lower.startswith(
        ("docs/plans/", "docs/evidence/", "docs/research/")
    ):
        return "plan_evidence_research_html"
    if any(p in {"tests", "test", "testing"} for p in parts) or Path(
        lower
    ).name.startswith("test_"):
        return "tests"
    if lower.startswith(("docs/", "data/", "artifacts/", "results/")):
        return "other"
    if (
        Path(lower).suffix
        in {
            ".py",
            ".pyi",
            ".c",
            ".cc",
            ".cpp",
            ".cxx",
            ".h",
            ".hpp",
            ".f",
            ".f90",
            ".f95",
            ".f03",
            ".for",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".rs",
            ".cu",
            ".cuh",
            ".sh",
            ".bash",
            ".cmake",
            ".css",
            ".html",
        }
        or Path(lower).name == "cmakelists.txt"
    ):
        return "source"
    # The planning SPA is product code despite living inside docs/.
    return "other"


def path_class(path):
    if path.startswith(("docs/ui/", "docs/_ui/", "docs/_shared/")) and Path(
        path
    ).suffix in {".js", ".jsx", ".css"}:
        return "source"
    return file_class(path)


def lane(record):
    name = str(
        record.get("backend") or (record.get("agent") or {}).get("backend") or ""
    )
    for family in ("clive", "codex", "claude", "native"):
        if name == family or name.startswith(family + "-"):
            return family
    return "unattributed"


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


def capture_project(project, branch):
    repo = CODE / project
    head = (
        git(
            repo,
            "log",
            "-1",
            "--first-parent",
            "--before=" + END,
            "--format=%H",
            branch,
        )
        .decode()
        .strip()
    )
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
        sha for sha in first if stamp(START) <= graph[sha]["epoch"] <= stamp(END)
    ]
    base = first[first.index(selected[0]) - 1]
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
    # Keep ancestry order, including any skewed commit clocks between endpoints.
    assert [c["sha"] for c in history] == first[first.index(base) + 1 :]
    ledger_raw = git(repo, "show", head + ":docs/state/" + project + "/crew.json")
    ledger = json.loads(ledger_raw)
    records = {r["run_id"]: r for r in ledger.get("data", ledger).get("runs", [])}
    paths = (
        git(
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            head,
            "--",
            "docs/state/" + project + "/runs",
        )
        .decode()
        .splitlines()
    )
    for path in paths:
        if path.endswith(".json"):
            row = json.loads(git(repo, "show", head + ":" + path))
            row = row.get("data", row)
            if row.get("run_id"):
                records[row["run_id"]] = row
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
    promotions = collections.defaultdict(list)
    for sha, data in graph.items():
        match = re.match(r"promote\((r-[^)]+)\)", data["subject"])
        if match:
            promotions[match[1]].append(
                {
                    "sha": sha,
                    "epoch": data["epoch"],
                    "landing_sha": introduction.get(sha),
                }
            )
    sources = dict.fromkeys(records, "committed_primary_snapshot")
    recovered = []
    database = Path("/home/ITER/mcintos/.config/reckon/crew/run_store.db")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        for rid in sorted(set(promotions) - set(records)):
            if not any(
                stamp(START) <= item["epoch"] <= stamp(END) for item in promotions[rid]
            ):
                continue
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
    print(
        project,
        "head",
        head,
        "primary_commits",
        len(history),
        "ledger_runs",
        len(compact),
        "product_commits",
        len(product),
        flush=True,
    )
    return {
        "project": project,
        "primary_branch": branch,
        "head": head,
        "base": base,
        "ledger_sha256": hashlib.sha256(ledger_raw).hexdigest(),
        "per_run_files": len(paths),
        "recovered_from_sqlite": recovered,
        "promotion_ids_without_record": sorted(
            rid
            for rid in set(promotions) - set(records)
            if any(
                stamp(START) <= item["epoch"] <= stamp(END) for item in promotions[rid]
            )
        ),
        "runs": compact,
        "commits": history,
    }


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


def recover_ledger_clocks(snapshot):
    """Locate the first durable ledger appearance when no promote commit exists."""
    for project in snapshot["projects"]:
        candidates = {
            run["run_id"]: run
            for run in project["runs"]
            if not run["promotion_commits"]
            and stamp(START) <= (stamp(run.get("completed_at")) or 0) <= stamp(END)
        }
        if not candidates:
            continue
        repo = CODE / project["project"]
        path = "docs/state/" + project["project"] + "/crew.json"
        before = json.loads(git(repo, "show", project["base"] + ":" + path))
        present = {r["run_id"] for r in before.get("data", before).get("runs", [])}
        candidates = {rid: row for rid, row in candidates.items() if rid not in present}
        if not candidates:
            continue
        expression = "|".join(re.escape(rid) for rid in sorted(candidates))
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
            project["base"] + ".." + project["head"],
            "--",
            path,
            "docs/state/" + project["project"] + "/runs",
        )
        commit, epoch = None, None
        found = set()
        for line in raw.split(b"\n"):
            if line.startswith(b"\x1e"):
                commit, value = line[1:].decode().split("\t")
                epoch = int(value)
            elif line.startswith(b"+"):
                match = re.search(rb'"run_id"\s*:\s*"([^"]+)"', line)
                if match and match[1].decode() in candidates:
                    rid = match[1].decode()
                    if rid not in found:
                        candidates[rid]["promotion_commits"] = [
                            {
                                "sha": commit,
                                "epoch": epoch,
                                "landing_sha": commit,
                                "source": "first_primary_ledger_appearance_upper_bound",
                            }
                        ]
                        found.add(rid)
        project["ledger_clock_recoveries"] = sorted(found)
        print(
            project["project"],
            "first durable ledger clocks",
            len(found),
            "/",
            len(candidates),
            flush=True,
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


def measure(snapshot, *, weekly_cells=None):
    start, end = stamp(START), stamp(END)
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
            },
            "attempts_per_landed_node": ratio(
                sum(observed_attempts[rid] for rid in attempt_ids), len(landed_roots)
            ),
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
        (dt.date.fromisoformat(START[:10]) + dt.timedelta(days=i)).isoformat()
        for i in range(
            (dt.date.fromisoformat(END[:10]) - dt.date.fromisoformat(START[:10])).days
            + 1
        )
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
        first_day = dt.date.fromisoformat(START[:10])
        monday = first_day - dt.timedelta(days=first_day.weekday())
        last_day = dt.date.fromisoformat(END[:10])
        while monday <= last_day:
            next_monday = monday + dt.timedelta(days=7)
            first, last = monday.isoformat(), next_monday.isoformat()
            weekly_cells.append(
                {
                    "week_start": first,
                    "window_start": max(START, first + "T00:00:00Z"),
                    "window_end": min(END, last + "T00:00:00Z"),
                    "metrics": aggregate(
                        [r for r in all_runs if first <= r["day"] < last],
                        [c for c in all_commits if first <= c["day"] < last],
                    ),
                }
            )
            monday = next_monday

    return {
        "window": {
            "start": START,
            "end": END,
            "elapsed_days": (end - start) / 86400,
            "complete_seven_day_followup_through": iso(end - WEEK),
        },
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
            "quantiles": "Median and linearly interpolated percentile at (n-1)*p; missing denominator explicitly recorded.",
        },
        "positive_controls": positive_controls(),
        "coverage": coverage,
        "august_baseline": snapshot["august_baseline"],
        "total": aggregate(all_runs, all_commits),
        "by_project": cells([{"project": p} for p in PROJECTS]),
        "by_lane": cells([{"lane": p} for p in LANES]),
        "by_day": cells([{"day": p} for p in days]),
        "by_project_day_lane": cells(
            [
                {"project": p, "day": d, "lane": lane_name}
                for p in PROJECTS
                for d in days
                for lane_name in LANES
            ]
        ),
        "runs": all_runs,
        "commits": all_commits,
    }


def compact_summary(full, weekly_cells):
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
            "by_project",
            "by_lane",
            "by_day",
        )
    }
    summary["by_week"] = weekly_cells
    summary["weekly_definition"] = (
        "UTC Monday-start weeks clipped to the study window; aggregate the original run and commit cohorts, never average daily medians or ratios."
    )
    summary["full_artifacts"] = {
        "velocity": {
            "path": str(FULL_OUTPUT),
            "sha256": hashlib.sha256(dump(full)).hexdigest(),
        },
        "inputs": {
            "path": str(RUN / "inputs.json.gz"),
            "sha256": hashlib.sha256((RUN / "inputs.json.gz").read_bytes()).hexdigest(),
        },
    }
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    positive_controls()
    path = RUN / "inputs.json.gz"
    if args.capture:
        if path.exists():
            raise SystemExit(
                "Capture exists; do not silently replace a published input snapshot."
            )
        baseline = {
            "source": "docs/research/crew-fleet-rox-review.html#s2",
            "window": "2026-08-12 through 2026-08-16",
            "projects": 4,
            "completed_runs": 259,
            "gate_pass_percent": 90.3,
            "lines_added": 108587,
            "lines_added_reporting_projects": 3,
            "commits": 228,
            "tests_added": 679,
            "median_wall_minutes_per_run": 9.8,
            "stall_corrected_worker_hours": 66.2,
            "mean_concurrency_while_active": 2.26,
            "peak_concurrency": 13,
            "median_authored_changed_lines_per_worker_minute_range": [16, 24],
            "not_reported": [
                "seven-day line survival",
                "durable lines per worker-hour",
                "primary landed lines by lane",
                "promotion latency",
                "record/product ratio",
                "attempts per landed node",
                "coordinator active dispatch hours",
            ],
            "per_project": {
                "reckon": {
                    "completed_runs": 80,
                    "lines_added": 9145,
                    "commits": 70,
                    "median_wall_minutes": 5.4,
                    "worker_hours": 7.7,
                },
                "imas-ambix": {
                    "completed_runs": 45,
                    "lines_added": 45877,
                    "commits": 43,
                    "median_wall_minutes": 17.6,
                    "worker_hours": 20.6,
                },
                "nova": {
                    "completed_runs": 116,
                    "lines_added": 53565,
                    "commits": 108,
                    "median_wall_minutes": 13.3,
                    "worker_hours": 35.1,
                },
                "imas-codex": {
                    "completed_runs": 18,
                    "lines_added": None,
                    "commits": 7,
                    "median_wall_minutes": 8.6,
                    "worker_hours": 2.8,
                },
            },
        }
        snapshot = {
            "window": [START, END],
            "august_baseline": baseline,
            "projects": [capture_project(p, b) for p, b in PROJECTS.items()],
        }
        recover_ledger_clocks(snapshot)
        path.write_bytes(gzip.compress(dump(snapshot), mtime=0))
        print("captured immutable inputs", path, path.stat().st_size, flush=True)
    snapshot = json.loads(gzip.decompress(path.read_bytes()))
    weekly_cells = []
    result = measure(snapshot, weekly_cells=weekly_cells)
    summary = compact_summary(result, weekly_cells)
    FULL_OUTPUT.write_bytes(dump(result))
    COMPACT_OUTPUT.write_bytes(dump(summary))
    print(
        "output",
        FULL_OUTPUT,
        "sha256",
        hashlib.sha256(FULL_OUTPUT.read_bytes()).hexdigest(),
        flush=True,
    )
    print(
        "compact output",
        COMPACT_OUTPUT,
        "bytes",
        COMPACT_OUTPUT.stat().st_size,
        "sha256",
        hashlib.sha256(COMPACT_OUTPUT.read_bytes()).hexdigest(),
        flush=True,
    )
    print(json.dumps(result["total"], sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
