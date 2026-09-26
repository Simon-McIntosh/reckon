"""Measure landed line lifetimes and exact semantic returns in pinned Git history.

Only the Python standard library is needed. The repository is read through Git;
neither its checkout nor its environment is changed. Output has no wall-clock
fields, so the pinned objects suffice to reproduce the JSON byte for byte.
"""

from __future__ import annotations

import argparse
import ast
import collections
import datetime as dt
import hashlib
import io
import itertools
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

HEAD = "16287918db2f90d0088c1786ec45965cd90e6652"
BASE = "e9476d94024ec5ebfb43af785c568d58aa64b3ee"
START = "2026-08-15T00:00:00Z"
CAPTURE = "2026-09-26T10:00:00Z"
LOCAL_PERIOD = "2026-09-12T00:00:00Z"
DAY = 86400
ROOTS = ("nova/", "tests/")
RUN_DIRECTORY = Path(
    "/home/ITER/mcintos/.config/reckon/crew/runs/"
    "r-20260926T104924535917-nova-flapping-history"
)


def timestamp(value):
    return dt.datetime.fromisoformat(value).timestamp()


def iso(value):
    return dt.datetime.fromtimestamp(value, dt.UTC).isoformat().replace("+00:00", "Z")


def ratio(numerator, denominator):
    return round(numerator / denominator, 8) if denominator else None


class Git:
    def __init__(self, repo):
        self.repo = str(repo)

    def read(self, *args, binary=False):
        result = subprocess.check_output(["git", "-C", self.repo, *args])
        return result if binary else result.decode("utf-8", errors="replace")


def lane(record):
    backend = (
        record.get("backend") or record.get("agent", {}).get("backend") or "unknown"
    )
    if (
        backend == "clive"
        or record.get("local")
        or record.get("agent", {}).get("local")
    ):
        return "local"
    if backend.startswith("codex"):
        return "codex"
    if backend.startswith("claude"):
        return "claude"
    return backend


def signature(node):
    args = node.args
    # Defaults have their own keys; signatures measure names, kinds and annotations.
    return (
        ast.dump(
            ast.arguments(
                posonlyargs=args.posonlyargs,
                args=args.args,
                vararg=args.vararg,
                kwonlyargs=args.kwonlyargs,
                kw_defaults=[None for _ in args.kwonlyargs],
                kwarg=args.kwarg,
                defaults=[],
            ),
            include_attributes=False,
        )
        + " -> "
        + (ast.unparse(node.returns) if node.returns else "unannotated")
    )


def semantic_records(source, path):
    """Return uniquely addressed constants, defaults, signatures and expectations."""
    tree = ast.parse(source, filename=path)
    features = {}
    ambiguous = set()
    functions = {}
    expectation_sets = collections.defaultdict(list)

    def put(kind, scope, name, value, line):
        key = f"{kind}|{scope}|{name}"
        if key in features:
            ambiguous.add(key)
        features[key] = {
            "kind": kind,
            "scope": scope,
            "name": name,
            "value": value,
            "line": line,
        }

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = ".".join(filter(None, (scope, node.name)))
            functions[scope] = hashlib.sha256(ast.dump(node).encode()).hexdigest()
            put("signature", scope, "parameters", signature(node), node.lineno)
            positional = node.args.posonlyargs + node.args.args
            for param, default in zip(
                positional[-len(node.args.defaults) :], node.args.defaults, strict=False
            ):
                put("default", scope, param.arg, ast.unparse(default), param.lineno)
            for param, default in zip(
                node.args.kwonlyargs, node.args.kw_defaults, strict=True
            ):
                if default is not None:
                    put("default", scope, param.arg, ast.unparse(default), param.lineno)
        elif isinstance(node, ast.ClassDef):
            scope = ".".join(filter(None, (scope, node.name)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None:
                for target in targets:
                    try:
                        ast.literal_eval(node.value)
                        literal = True
                    except (ValueError, TypeError):
                        literal = False
                    if isinstance(target, (ast.Name, ast.Attribute, ast.Tuple)) and (
                        literal
                        or (isinstance(target, ast.Name) and target.id.isupper())
                    ):
                        put(
                            "constant",
                            scope,
                            ast.unparse(target),
                            ast.unparse(node.value),
                            node.lineno,
                        )
        elif path.startswith("tests/") and isinstance(node, ast.Assert):
            expectation_sets[scope].append((ast.unparse(node.test), node.lineno))
            if isinstance(node.test, ast.Compare):
                actual = ast.unparse(node.test.left)
                operators = ",".join(type(op).__name__ for op in node.test.ops)
                expected = ", ".join(
                    ast.unparse(item) for item in node.test.comparators
                )
                put(
                    "test_expectation",
                    scope,
                    actual + " " + operators,
                    expected,
                    node.lineno,
                )
        elif path.startswith("tests/") and isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            if (
                callee.rsplit(".", 1)[-1]
                in {
                    "assert_allclose",
                    "assert_equal",
                    "assert_array_equal",
                    "assert_array_almost_equal",
                    "assert_almost_equal",
                    "assertEquals",
                    "assertEqual",
                }
                and len(node.args) >= 2
            ):
                expectation_sets[scope].append((ast.unparse(node), node.lineno))
                name = callee + "(" + ast.unparse(node.args[0]) + ")"
                value = ", ".join(
                    [ast.unparse(item) for item in node.args[1:]]
                    + [ast.unparse(k) for k in node.keywords]
                )
                put("test_expectation", scope, name, value, node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, "")
    for scope, expectations in sorted(expectation_sets.items()):
        put(
            "test_expectation_set",
            scope,
            "all_assertions",
            "\n".join(sorted(value for value, _ in expectations)),
            min(line for _, line in expectations),
        )
    return (
        {k: v for k, v in features.items() if k not in ambiguous},
        functions,
        len(ambiguous),
    )


def returns(history):
    """Find a value that returns after at least one different, continuously present value."""
    seen = {}
    result = []
    for index, state in enumerate(history):
        value = state["value"]
        if value is None:
            seen.clear()
            continue
        previous = seen.get(value)
        if previous is not None and previous < index - 1:
            result.append(history[previous : index + 1])
        seen[value] = index
    return result


def parse_patch(patch):
    files = []
    current = None
    hunk = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(.*?) b/(.*)", line)
            if not match or match[1] != match[2]:
                raise ValueError("Unexpected quoted or renamed path: " + line)
            current = {"path": match[1], "hunks": [], "binary": False, "deleted": False}
            files.append(current)
            hunk = None
        elif line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match is None:
                raise ValueError(line)
            old_start, old_count, new_start, new_count = match.groups()
            hunk = {
                "old_start": int(old_start),
                "old_count": int(old_count or 1),
                "new_start": int(new_start),
                "new_count": int(new_count or 1),
                "removed": [],
                "added": [],
            }
            current["hunks"].append(hunk)
        elif line.startswith("deleted file mode "):
            current["deleted"] = True
        elif line.startswith("Binary files "):
            current["binary"] = True
        elif hunk is not None and line.startswith("-"):
            hunk["removed"].append(line[1:])
        elif hunk is not None and line.startswith("+"):
            hunk["added"].append(line[1:])
    return files


def instrument_controls():
    before = "LIMIT = 2\ndef solve(tolerance=0.1):\n    return tolerance\ndef test_value():\n    assert result == 2\n"
    middle = (
        before.replace("2", "3")
        .replace("0.1", "0.2")
        .replace("solve(tolerance", "solve(extra, tolerance")
    )
    first = semantic_records(before, "tests/control.py")[0]
    second = semantic_records(middle, "tests/control.py")[0]
    detected = []
    for key in first.keys() & second.keys():
        history = [
            {"value": value}
            for value in [
                first[key]["value"],
                second[key]["value"],
                first[key]["value"],
            ]
        ]
        if history[0] != history[1] and returns(history):
            detected.append(first[key]["kind"])
    assert sorted(detected) == [
        "constant",
        "default",
        "signature",
        "test_expectation",
        "test_expectation_set",
    ]
    assert not returns([{"value": "one"}, {"value": "two"}, {"value": "three"}])
    assert not returns([{"value": "one"}, {"value": None}, {"value": "one"}])
    patch = "diff --git a/nova/control.py b/nova/control.py\n@@ -2 +2,2 @@\n-old\n+new\n+another\n"
    parsed = parse_patch(patch)[0]["hunks"][0]
    assert parsed["removed"] == ["old"] and parsed["added"] == ["new", "another"]
    return {
        "semantic_roundtrips_detected": sorted(detected),
        "monotone_and_absent_returns_rejected": True,
        "patch_add_delete_control": True,
    }


def summarize(rows):
    output = {"commits": len(rows), "code_commits": sum(bool(r["files"]) for r in rows)}
    for root in ("nova", "tests"):
        output[root] = {
            key: sum(row[root][key] for row in rows)
            for key in (
                "added",
                "removed",
                "deleted_within_seven_days",
                "mature_added",
                "mature_deleted_within_seven_days",
            )
        }
        counts = output[root]
        counts["observed_deletion_share"] = ratio(
            counts["deleted_within_seven_days"], counts["added"]
        )
        counts["mature_deletion_share"] = ratio(
            counts["mature_deleted_within_seven_days"], counts["mature_added"]
        )
    output["code_commits_by_lane"] = dict(
        sorted(
            collections.Counter(
                row["attribution"]["lane"] for row in rows if row["files"]
            ).items()
        )
    )
    output["commits_by_lane"] = dict(
        sorted(collections.Counter(row["attribution"]["lane"] for row in rows).items())
    )
    output["revert_commits"] = [row["sha"] for row in rows if row["is_explicit_revert"]]
    return output


def read_reverts(git, owners, runs, landing_of, row_by_sha):
    """Keep branch experiments withdrawn before merging distinct from landed changes."""
    result = []
    authored_log = git.read(
        "log", "--reverse", "--format=%H%x00%cI%x00%B%x00%x1e", BASE + ".." + HEAD
    )
    for block in authored_log.split("\x1e"):
        if not block.strip():
            continue
        sha, date, message, _ = block.lstrip("\n").split("\x00", 3)
        if not timestamp(START) <= timestamp(date) <= timestamp(CAPTURE):
            continue
        if not re.search(r"(?im)^revert\b|^this reverts commit ", message):
            continue
        primary = landing_of[sha]
        matched = []
        for run_id in sorted(owners.get(sha, [])):
            run = runs[run_id]
            matched.append(
                {
                    "run_id": run_id,
                    "lane": lane(run),
                    "backend": run.get("backend"),
                    "plan": run.get("plan"),
                    "join": "explicit_ledger_commit",
                }
            )
        if not matched:
            for candidate in row_by_sha[primary]["attribution"]["runs"]:
                run = runs[candidate["run_id"]]
                base = run.get("base_sha")
                if not base:
                    continue
                before = subprocess.run(
                    ["git", "-C", git.repo, "merge-base", "--is-ancestor", base, sha],
                    check=False,
                ).returncode
                tips = [
                    tip
                    for tip in candidate["commits"]
                    if subprocess.run(
                        [
                            "git",
                            "-C",
                            git.repo,
                            "merge-base",
                            "--is-ancestor",
                            sha,
                            tip,
                        ],
                        check=False,
                    ).returncode
                    == 0
                ]
                if before == 0 and tips:
                    matched.append(
                        {
                            "run_id": candidate["run_id"],
                            "lane": candidate["lane"],
                            "backend": candidate["backend"],
                            "plan": candidate["plan"],
                            "join": "within_ledger_base_to_listed_tip_ancestry",
                            "base_sha": base,
                            "descendant_tips": tips,
                        }
                    )
        labels = sorted({item["lane"] for item in matched})
        result.append(
            {
                "sha": sha,
                "committed_at": iso(timestamp(date)),
                "message": message.strip(),
                "primary_landing": primary,
                "primary_landing_time": row_by_sha[primary]["committed_at"],
                "primary_landing_week": row_by_sha[primary]["week"],
                "is_first_parent_commit": sha == primary,
                "attribution": {
                    "lane": labels[0]
                    if len(labels) == 1
                    else "mixed"
                    if labels
                    else "unattributed",
                    "runs": matched,
                },
            }
        )
    return result


def census(repo):
    if sys.version_info < (3, 14):
        raise RuntimeError(
            "Python 3.14 or newer is required to parse the captured Nova syntax"
        )
    git = Git(repo)
    controls = instrument_controls()
    raw_ledger = git.read("show", HEAD + ":docs/state/nova/crew.json", binary=True)
    ledger = json.loads(raw_ledger)["data"]["runs"]
    runs = {row["run_id"]: row for row in ledger}
    graph = git.read("rev-list", "--parents", HEAD).splitlines()
    parents = {parts[0]: parts[1:] for row in graph if (parts := row.split())}
    prefixes = collections.defaultdict(list)
    for sha in parents:
        prefixes[sha[:7]].append(sha)
    owners = collections.defaultdict(set)
    unresolved = []
    for run in ledger:
        for citation in run.get("commits", []):
            match = re.match(r"[0-9a-f]{7,40}\b", citation)
            if match is None:
                unresolved.append(
                    {
                        "run_id": run["run_id"],
                        "citation": citation,
                        "reason": "not_a_commit_id",
                    }
                )
                continue
            prefix = match[0]
            hits = [sha for sha in prefixes[prefix[:7]] if sha.startswith(prefix)]
            if len(hits) == 1:
                owners[hits[0]].add(run["run_id"])
            else:
                unresolved.append(
                    {
                        "run_id": run["run_id"],
                        "citation": citation,
                        "reason": "not_uniquely_reachable_at_capture",
                    }
                )

    raw = git.read(
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x00%cI%x00%B%x00%x1e",
        BASE + ".." + HEAD,
    )
    commits = []
    for block in raw.split("\x1e"):
        if not block.strip():
            continue
        sha, date, message, _ = block.lstrip("\n").split("\x00", 3)
        when = timestamp(date)
        if timestamp(START) <= when <= timestamp(CAPTURE):
            commits.append(
                {
                    "sha": sha,
                    "committed_at": iso(when),
                    "subject": message.splitlines()[0],
                    "message": message,
                    "seconds": when,
                }
            )
    code_shas = set(
        git.read(
            "log", "--first-parent", "--format=%H", BASE + ".." + HEAD, "--", *ROOTS
        ).splitlines()
    )
    assert code_shas and commits
    print(
        f"Pinned history: {len(commits)} first-parent commits, {len(code_shas)} source/test-changing commits",
        flush=True,
    )

    texts = {}
    provenance = {}
    archive = git.read("archive", BASE, *ROOTS, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for item in tar:
            if not item.isfile():
                continue
            content = tar.extractfile(item).read()
            if b"\x00" in content:
                continue
            lines = content.decode("utf-8", errors="replace").splitlines()
            texts[item.name] = lines
            provenance[item.name] = [None] * len(lines)
    features = {}
    function_hashes = {}
    histories = collections.defaultdict(list)
    parse_failures = []
    ambiguous_total = 0
    for path, lines in sorted(texts.items()):
        if path.endswith(".py"):
            try:
                features[path], function_hashes[path], ambiguity = semantic_records(
                    "\n".join(lines), path
                )
                ambiguous_total += ambiguity
            except SyntaxError as error:
                parse_failures.append({"sha": BASE, "path": path, "error": str(error)})
                continue
            for key, value in features[path].items():
                histories[(path, key)].append(
                    {
                        "sha": BASE,
                        "value": value["value"],
                        "line": value["line"],
                        "baseline": True,
                    }
                )

    file_weeks = collections.defaultdict(set)
    function_weeks = collections.defaultdict(set)
    line_deaths = collections.Counter()
    binary_changes = []
    rows = []
    landing_of = {}
    monotonicity = []
    for index, commit in enumerate(commits):
        sha = commit["sha"]
        when = commit["seconds"]
        week = int((when - timestamp(START)) // (7 * DAY))
        if index and when < commits[index - 1]["seconds"]:
            monotonicity.append(sha)
        inherited = []
        if len(parents[sha]) > 1:
            inherited = git.read("rev-list", sha, "^" + parents[sha][0]).splitlines()
        found = collections.defaultdict(list)
        for source in [sha, *inherited]:
            landing_of.setdefault(source, sha)
            for run_id in owners.get(source, []):
                found[run_id].append(source)
        message_ids = [
            run_id
            for run_id in re.findall(r"r-\d{8}T\d+-[a-zA-Z0-9_-]+", commit["message"])
            if run_id in runs
        ]
        for run_id in message_ids:
            found[run_id]
        attributed = []
        for run_id, sources in sorted(found.items()):
            run = runs[run_id]
            attributed.append(
                {
                    "run_id": run_id,
                    "lane": lane(run),
                    "backend": run.get("backend"),
                    "plan": run.get("plan"),
                    "node": run.get("node"),
                    "role": run.get("role"),
                    "commits": sorted(set(sources)),
                    "join": "ledger_commit_reachable_in_landing"
                    if sources
                    else "run_id_in_commit_message",
                }
            )
        lanes = sorted({entry["lane"] for entry in attributed})
        row = {key: commit[key] for key in ("sha", "committed_at", "subject")}
        row.update(
            {
                "week": week,
                "parents": parents[sha],
                "files": [],
                "is_explicit_revert": bool(
                    re.search(
                        r"(?im)^revert\b|^this reverts commit ", commit["message"]
                    )
                ),
                "attribution": {
                    "lane": lanes[0]
                    if len(lanes) == 1
                    else "mixed"
                    if lanes
                    else "unattributed",
                    "runs": attributed,
                },
            }
        )
        for root in ("nova", "tests"):
            row[root] = dict.fromkeys(
                (
                    "added",
                    "removed",
                    "deleted_within_seven_days",
                    "mature_added",
                    "mature_deleted_within_seven_days",
                ),
                0,
            )
        rows.append(row)
        if sha not in code_shas:
            continue
        patch = git.read(
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--unified=0",
            parents[sha][0],
            sha,
            "--",
            *ROOTS,
        )
        for change in parse_patch(patch):
            path = change["path"]
            root = path.split("/", 1)[0]
            if change["binary"]:
                binary_changes.append({"sha": sha, "path": path})
                continue
            row["files"].append(path)
            file_weeks[path].add(week)
            lines = texts.setdefault(path, [])
            origins = provenance.setdefault(path, [])
            for hunk in reversed(change["hunks"]):
                start = hunk["old_start"] - (1 if hunk["old_count"] else 0)
                count = hunk["old_count"]
                assert lines[start : start + count] == hunk["removed"], (
                    sha,
                    path,
                    start,
                )
                assert len(hunk["removed"]) == count
                assert len(hunk["added"]) == hunk["new_count"]
                row[root]["removed"] += count
                row[root]["added"] += hunk["new_count"]
                for origin in origins[start : start + count]:
                    if origin is not None:
                        age = when - commits[origin]["seconds"]
                        if 0 <= age <= 7 * DAY:
                            rows[origin][root]["deleted_within_seven_days"] += 1
                            line_deaths[(origin, index, path)] += 1
                lines[start : start + count] = hunk["added"]
                origins[start : start + count] = [index] * hunk["new_count"]
            if not path.endswith(".py"):
                continue
            try:
                next_features, next_functions, ambiguity = semantic_records(
                    "\n".join(lines), path
                )
                ambiguous_total += ambiguity
            except SyntaxError as error:
                parse_failures.append({"sha": sha, "path": path, "error": str(error)})
                next_features, next_functions = {}, {}
            prior_features = features.get(path, {})
            for key in sorted(prior_features.keys() | next_features.keys()):
                prior = prior_features.get(key, {}).get("value")
                next_value = next_features.get(key, {}).get("value")
                if next_value != prior:
                    histories[(path, key)].append(
                        {
                            "sha": sha,
                            "value": next_value,
                            "line": next_features.get(key, prior_features.get(key))[
                                "line"
                            ],
                            "baseline": False,
                        }
                    )
            for function in sorted(
                function_hashes.get(path, {}).keys() | next_functions.keys()
            ):
                if function_hashes.get(path, {}).get(function) != next_functions.get(
                    function
                ):
                    function_weeks[(path, function)].add(week)
            features[path], function_hashes[path] = next_features, next_functions
        if (sum(bool(row["files"]) for row in rows)) % 100 == 0:
            print(f"Processed {index + 1}/{len(commits)} landings", flush=True)

    for row, commit in zip(rows, commits, strict=True):
        if commit["seconds"] + 7 * DAY <= timestamp(CAPTURE):
            for root in ("nova", "tests"):
                row[root]["mature_added"] = row[root]["added"]
                row[root]["mature_deleted_within_seven_days"] = row[root][
                    "deleted_within_seven_days"
                ]
    row_by_sha = {row["sha"]: row for row in rows}
    reachable_reverts = read_reverts(git, owners, runs, landing_of, row_by_sha)
    oscillations = []
    for (path, key), history in sorted(histories.items()):
        oscillations.extend(
            {
                "path": path,
                "key": key,
                "kind": key.split("|", 1)[0],
                "states": states,
                "return_sha": states[-1]["sha"],
                "return_week": row_by_sha[states[-1]["sha"]]["week"],
            }
            for states in returns(history)
        )
    grouped = collections.defaultdict(list)
    for item in oscillations:
        grouped[(item["path"], tuple(state["sha"] for state in item["states"]))].append(
            item
        )
    episodes = []
    for (path, shas), items in grouped.items():
        episodes.append(
            {
                "path": path,
                "shas": list(shas),
                "returned_keys": len(items),
                "keys": [item["key"] for item in items],
                "return_week": items[0]["return_week"],
            }
        )
    episodes.sort(key=lambda item: (-item["returned_keys"], item["path"], item["shas"]))
    weekly = []
    for week in range(7):
        selected = [row for row in rows if row["week"] == week]
        entry = summarize(selected)
        entry.update(
            {
                "week": week,
                "start": iso(timestamp(START) + week * 7 * DAY),
                "end_exclusive": iso(
                    min(timestamp(START) + (week + 1) * 7 * DAY, timestamp(CAPTURE))
                ),
                "exact_semantic_returns": sum(
                    item["return_week"] == week for item in oscillations
                ),
                "grouped_episodes": sum(
                    item["return_week"] == week for item in episodes
                ),
            }
        )
        entry["reachable_revert_commits"] = [
            item["sha"]
            for item in reachable_reverts
            if item["primary_landing_week"] == week
        ]
        weekly.append(entry)
    first_local_code = min(
        row["committed_at"]
        for row in rows
        if row["files"] and row["attribution"]["lane"] == "local"
    )
    cohorts = {}
    for name, predicate in {
        "before_study_local_period": lambda row: row["committed_at"] < LOCAL_PERIOD,
        "study_local_period": lambda row: row["committed_at"] >= LOCAL_PERIOD,
        "before_first_local_code_landing": lambda row: (
            row["committed_at"] < first_local_code
        ),
        "from_first_local_code_landing": lambda row: (
            row["committed_at"] >= first_local_code
        ),
    }.items():
        selected = [row for row in rows if predicate(row)]
        metrics = summarize(selected)
        metrics["exact_semantic_returns"] = sum(
            predicate(row_by_sha[item["return_sha"]]) for item in oscillations
        )
        metrics["grouped_episodes"] = sum(
            predicate(row_by_sha[item["shas"][-1]]) for item in episodes
        )
        metrics["episodes_per_hundred_code_commits"] = ratio(
            100 * metrics["grouped_episodes"], metrics["code_commits"]
        )
        cohorts[name] = metrics
    by_lane = {}
    for label in sorted({row["attribution"]["lane"] for row in rows}):
        selected = [row for row in rows if row["attribution"]["lane"] == label]
        metrics = summarize(selected)
        metrics["returned_keys_at_landing"] = sum(
            row_by_sha[item["return_sha"]]["attribution"]["lane"] == label
            for item in oscillations
        )
        by_lane[label] = metrics
    # Independently check replay against final Git blobs, not just hunk consistency.
    compared = 0
    final_archive = git.read("archive", HEAD, *ROOTS, binary=True)
    with tarfile.open(fileobj=io.BytesIO(final_archive)) as tar:
        for item in tar:
            if not item.isfile():
                continue
            content = tar.extractfile(item).read()
            if b"\x00" in content:
                continue
            assert (
                texts.get(item.name)
                == content.decode("utf-8", errors="replace").splitlines()
            ), item.name
            compared += 1
    independent_counts = collections.Counter()
    for line in git.read(
        "log",
        "--first-parent",
        "--diff-merges=first-parent",
        "--format=",
        "--numstat",
        "--unified=0",
        "--no-renames",
        BASE + ".." + HEAD,
        "--",
        *ROOTS,
    ).splitlines():
        if not re.match(r"^\d+\t\d+\t", line):
            continue
        added, removed, path = line.split("\t", 2)
        if added == "-":
            continue
        root = path.split("/", 1)[0]
        independent_counts[root + "_added"] += int(added)
        independent_counts[root + "_removed"] += int(removed)
    for root in ("nova", "tests"):
        for measure in ("added", "removed"):
            assert (
                sum(row[root][measure] for row in rows)
                == independent_counts[root + "_" + measure]
            )
    controls["independent_git_numstat_matches"] = dict(
        sorted(independent_counts.items())
    )
    controls["final_text_files_equal_git_blobs"] = compared
    controls["known_real_attributed_code_commit"] = next(
        {"sha": row["sha"], "attribution": row["attribution"]}
        for row in rows
        if row["files"] and row["attribution"]["runs"]
    )
    controls["real_semantic_returns"] = len(oscillations)
    controls["feature_histories_by_kind"] = dict(
        sorted(
            collections.Counter(key.split("|", 1)[0] for _, key in histories).items()
        )
    )
    controls["semantic_value_changes_by_kind"] = dict(
        sorted(
            collections.Counter(
                key.split("|", 1)[0]
                for (_, key), history in histories.items()
                for before, after in itertools.pairwise(history)
                if before["value"] is not None and after["value"] is not None
            ).items()
        )
    )
    output = {
        "source": {
            "repository": "nova",
            "primary_branch": "main",
            "head": HEAD,
            "base": BASE,
            "start": START,
            "capture": CAPTURE,
            "study_local_period_start": LOCAL_PERIOD,
            "first_local_code_landing": first_local_code,
            "ledger_path": "docs/state/nova/crew.json",
            "ledger_sha256": hashlib.sha256(raw_ledger).hexdigest(),
            "ledger_records": len(ledger),
        },
        "method": {
            "history": "first-parent landings; merge diff against first parent; no worker/merge double counting. Explicit revert messages are also enumerated over all newly reachable ancestors, with their first primary landing reported separately.",
            "time": "UTC commit time at landing; weeks begin Saturday 00:00 UTC; final 10-hour partial bin included",
            "line_lifetime": "Replay zero-context Git diffs; tag each added physical text line with its landing; count deletion within 0..604800 seconds, at most once per birth. Whitespace/comments count. Renames/moves count as delete/add. Binary files excluded and listed.",
            "censoring": "Observed shares are lower bounds for incomplete follow-up; mature shares restrict BOTH numerator and denominator to additions at least seven days before capture.",
            "attribution": "Pinned committed ledger; match exact or unique abbreviated reachable commit IDs. Merge inherits only newly reachable ledger commits; explicit run IDs in messages also join. Multiple lanes stay mixed; absence stays unattributed. A merge attribution names contributing runs, not the integrator's lane.",
            "oscillation": "Exact AST value returns at a stable path/scope/key after one or more different values, continuously present. Constants use literal assignments to names/attributes/tuples plus uppercase assignments; defaults use named parameters; signatures omit defaults; Test expectations use stable actual expressions in comparisons and equality helpers, plus the full sorted assertion set per function so a changed assertion shape can also return. Duplicate keys in a snapshot are excluded. Formatting changes alone do not qualify.",
            "function_changes": "AST hash changes including addition/deletion, keyed by path and qualified function/method name; nested functions also counted in enclosing body. No rename tracking.",
            "interpretation": "Returned keys and grouped same-file/same-commit-sequence episodes are screening measures, not proof of a regression or a lane-caused effect. Read ranked episodes as code.",
        },
        "instrument_controls": controls,
        "weekly": weekly,
        "cohorts": cohorts,
        "by_lane": by_lane,
        "totals": summarize(rows),
        "commits": rows,
        "reachable_revert_commits": reachable_reverts,
        "files_changed_in_three_or_more_weeks": [
            {"path": path, "weeks": sorted(weeks), "week_count": len(weeks)}
            for path, weeks in sorted(file_weeks.items())
            if len(weeks) >= 3
        ],
        "functions_changed_in_three_or_more_weeks": [
            {
                "path": path,
                "function": function,
                "weeks": sorted(weeks),
                "week_count": len(weeks),
            }
            for (path, function), weeks in sorted(function_weeks.items())
            if len(weeks) >= 3
        ],
        "oscillations": oscillations,
        "ranked_episodes": episodes,
        "short_lived_line_edges": [
            {
                "added_by": rows[a]["sha"],
                "deleted_by": rows[b]["sha"],
                "path": path,
                "lines": count,
            }
            for (a, b, path), count in sorted(line_deaths.items())
        ],
        "limitations": {
            "parse_failures": parse_failures,
            "ambiguous_feature_snapshot_keys_excluded": ambiguous_total,
            "binary_changes_excluded": binary_changes,
            "nonmonotonic_landing_timestamps": monotonicity,
            "unresolved_ledger_citations": unresolved,
        },
    }
    print(
        f"Measured {len(oscillations)} exact semantic returns in {len(episodes)} grouped episodes; replay matches {compared} final text blobs",
        flush=True,
    )
    return output


def encode_json(value):
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def compact_result(result, full_payload):
    """Keep aggregate measures and five named histories, with bulk detail external."""
    episode_bytes = (RUN_DIRECTORY / "episode-evidence.json").read_bytes()
    episodes = json.loads(episode_bytes)["episodes"]
    named_episodes = []
    for episode in episodes:
        snapshots = episode["snapshots"]
        selected = {row["sha"] for row in snapshots}
        deaths = [
            row
            for row in result["short_lived_line_edges"]
            if row["path"] == episode["path"]
        ]
        swings = []
        for row in snapshots:
            owners = (
                row["explicit_attribution"]
                or row["containing_landing_attribution"]["runs"]
            )
            added, removed, _ = row["diff_numstat"].split("\t", 2)
            swings.append(
                {
                    "sha": row["sha"],
                    "primary_landing": row["primary_landing"],
                    "lanes": sorted({owner["lane"] for owner in owners}),
                    "plans": sorted({owner["plan"] for owner in owners}),
                    "added": int(added),
                    "removed": int(removed),
                }
            )
        restorations = []
        for equality in episode["whole_file_restorations"]:
            before = next(row for row in snapshots if row["sha"] == equality["before"])
            restorations.append(
                {
                    **equality,
                    "file_bytes": before["file_bytes"],
                    "file_sha256": before["file_sha256"],
                }
            )
        named_episodes.append(
            {
                "topic": episode["topic"],
                "path": episode["path"],
                "swings": swings,
                "whole_file_restorations": restorations,
                "file_lines_deleted_within_seven_days": sum(
                    row["lines"] for row in deaths
                ),
                "selected_swing_deletion_totals": [
                    row
                    for row in deaths
                    if row["added_by"] in selected and row["deleted_by"] in selected
                ],
            }
        )
    compact = {
        key: result[key]
        for key in (
            "source",
            "method",
            "weekly",
            "cohorts",
            "by_lane",
            "totals",
            "files_changed_in_three_or_more_weeks",
            "functions_changed_in_three_or_more_weeks",
            "ranked_episodes",
        )
    }
    compact["by_project"] = {result["source"]["repository"]: result["totals"]}
    compact["instrument_controls"] = {
        key: value
        for key, value in result["instrument_controls"].items()
        if key != "known_real_attributed_code_commit"
    }
    compact["coverage"] = {
        "recurrent_files": len(result["files_changed_in_three_or_more_weeks"]),
        "recurrent_functions": len(result["functions_changed_in_three_or_more_weeks"]),
        "exact_returned_keys": len(result["oscillations"]),
        "exact_return_episodes": len(result["ranked_episodes"]),
        "reachable_revert_commits": len(result["reachable_revert_commits"]),
        "first_parent_revert_commits": len(result["totals"]["revert_commits"]),
        **{
            key: len(value) if isinstance(value, list) else value
            for key, value in result["limitations"].items()
        },
    }
    compact["named_episodes"] = named_episodes
    compact["full_outputs"] = {
        "flapping": {
            "path": str(RUN_DIRECTORY / "flapping.json"),
            "bytes": len(full_payload),
            "sha256": hashlib.sha256(full_payload).hexdigest(),
        },
        "episodes": {
            "path": str(RUN_DIRECTORY / "episode-evidence.json"),
            "bytes": len(episode_bytes),
            "sha256": hashlib.sha256(episode_bytes).hexdigest(),
        },
        "diffs": {"path": str(RUN_DIRECTORY / "episode-diffs.patch")},
    }
    return compact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path("/home/ITER/mcintos/Code/nova")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=RUN_DIRECTORY / "flapping.json",
        help="Full census output, outside the repository.",
    )
    parser.add_argument(
        "--compact-output", type=Path, default=Path(__file__).with_name("flapping.json")
    )
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[5]
    if args.output.resolve().is_relative_to(repository):
        parser.error("the full census must be written outside the repository")
    result = census(args.repo)
    payload = encode_json(result)
    compact = encode_json(compact_result(result, payload))
    if len(compact) >= 300_000:
        raise ValueError("compact census exceeds the repository artifact size limit")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    args.compact_output.write_bytes(compact)
    print(
        f"Full JSON sha256={hashlib.sha256(payload).hexdigest()} bytes={len(payload)}",
        flush=True,
    )
    print(
        f"Compact JSON sha256={hashlib.sha256(compact).hexdigest()} bytes={len(compact)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
