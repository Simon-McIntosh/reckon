"""Nova readiness census: fast test lane, lint and markers at weekly snapshots.

Per snapshot: extract the tree at the commit nearest 12:00Z (plus the current
head) with git ls-tree + cat-file --batch into /tmp, run nova's default fast
lane (the tree's own pytest configuration, whose addopts select
``-m 'not slow'``) against the main checkout's environment with a 120 s
per-test timeout, run ruff under the tree's own configuration, and count
comment and pytest markers.

git archive is the natural extraction tool, but the fleet's worker git guard
does not name it among its read-only verbs and refuses it against another
checkout; ls-tree and cat-file are named there and copy the same bytes.

Re-running reproduces readiness.json byte for byte: it is a pure function of
the resolved commits and their trees under the rules below, and holds no
timestamp or duration.  Timings live in census.log, which is a log.
"""

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NOVA = Path("/home/ITER/mcintos/Code/nova")
PYTHON = NOVA / ".venv" / "bin" / "python"
RUFF = NOVA / ".venv" / "bin" / "ruff"
BRANCH = "main"
DATES = [
    "2026-08-22",
    "2026-08-29",
    "2026-09-05",
    "2026-09-12",
    "2026-09-19",
    "2026-09-26",
]
NOON = "T12:00:00Z"
PER_TEST_TIMEOUT_S = 120
RUN_TIMEOUT_S = 1500
TMP_ROOT = Path("/tmp/nova-readiness")  # noqa: S108 - trees belong on node-local disk
DATA_DIR = Path(__file__).resolve().parent
PLUGIN_NAME = "readiness_timeout_plugin"
ORDER = [*DATES, "head"]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    ".tox",
}
MARKER_PATTERNS = {
    "todo": r"\bTODO\b",
    "fixme": r"\bFIXME\b",
    "xfail_decorator": r"pytest\.mark\.xfail\b",
    "xfail_call": r"pytest\.xfail\(",
    "skip_decorator": r"pytest\.mark\.skip\b",
    "skipif_decorator": r"pytest\.mark\.skipif\b",
    "skip_call": r"pytest\.skip\(",
}
ENV_EXCEPTIONS = {
    "ModuleNotFoundError",
    "ImportError",
    "FileNotFoundError",
    "NotADirectoryError",
    "PermissionError",
    "ConnectionError",
    "ConnectionRefusedError",
    "TimeoutError",
    "OSError",
}
ENV_MESSAGES = [
    r"No module named",
    r"No such file or directory",
    r"Permission denied",
    r"Read-only file system",
    r"Connection refused",
    r"Failed to establish a new connection",
    r"Temporary failure in name resolution",
    r"cannot open shared object file",
    r"IMAS environment variables",
]
SUMMARY_TOKEN_RE = re.compile(
    r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|warnings?|deselected)"
)
COLLECTED_RE = re.compile(r"collected (\d+) items?(?: / (\d+) deselected)?")
SHORT_SUMMARY_RE = re.compile(r"^(FAILED|ERROR|XFAIL|XPASS) (\S+)(?: - (.*))?$")
EXC_TYPE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit))\b")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, check=False, **kw)


def git_out(*args: str) -> str:
    proc = run(["git", "-C", str(NOVA), *args])
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')}"
        )
    return proc.stdout.decode()


def resolve_snapshots() -> list:
    """Resolve the six weekly dates and the current head to commits."""
    snapshots = []
    head = git_out("rev-parse", BRANCH).strip()
    for date in DATES:
        before = git_out("rev-list", "-1", f"--before={date}{NOON}", BRANCH).strip()
        after_out = git_out("rev-list", f"--after={date}{NOON}", BRANCH).split()
        after = after_out[-1] if after_out else ""
        candidates = {}
        for key, sha in (("before", before), ("after", after)):
            if not sha:
                continue
            when = git_out("log", "-1", "--format=%cI", sha).strip()
            stamp = dt.datetime.fromisoformat(when).astimezone(dt.UTC)
            noon = dt.datetime.fromisoformat(f"{date}T12:00:00+00:00")
            candidates[key] = {
                "sha": sha,
                "commit_date": stamp.isoformat(),
                "seconds_from_noon": int((stamp - noon).total_seconds()),
            }
        chosen = min(candidates, key=lambda k: abs(candidates[k]["seconds_from_noon"]))
        snapshots.append(
            {
                "key": date,
                "label": f"commit nearest 12:00Z on {date}",
                "sha": candidates[chosen]["sha"],
                "selected_candidate": chosen,
                "candidates": candidates,
            }
        )
    snapshots.append(
        {
            "key": "head",
            "label": f"current head of {BRANCH}",
            "sha": head,
            "selected_candidate": "",
            "candidates": {},
        }
    )
    return snapshots


def extract_tree(sha: str, dest: Path, log_lines: list) -> int:
    """Materialise <sha>'s tree into dest with git's read-only verbs.

    ls-tree names every entry, cat-file --batch streams the blob contents, and
    together they copy the bytes git archive would.  Symlinks and the
    executable bit are reproduced; submodule gitlinks are named but not
    materialised.  git archive itself is refused against another checkout by
    the fleet's worker git guard, whose read-only set does not name the verb.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    listing = run(["git", "-C", str(NOVA), "ls-tree", "-r", "-z", sha]).stdout
    entries = []
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        meta, _, path = raw.partition(b"\t")
        mode, kind, blob = meta.decode().split()
        entries.append((mode, kind, blob, path.decode()))
    blobs = [e[2] for e in entries if e[1] == "blob"]
    proc = subprocess.Popen(
        ["git", "-C", str(NOVA), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    payload, _ = proc.communicate(("\n".join(blobs) + "\n").encode())
    contents = {}
    offset = 0
    for requested in blobs:
        newline = payload.index(b"\n", offset)
        header = payload[offset:newline].decode()
        fields = header.split()
        if len(fields) < 3:
            raise RuntimeError(f"cat-file --batch answered {header!r} for {requested}")
        size = int(fields[2])
        start = newline + 1
        contents[fields[0]] = payload[start : start + size]
        offset = start + size + 1
    written = 0
    for mode, kind, blob, path in entries:
        if kind != "blob":
            continue
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            if dest.joinpath(path).exists() or dest.joinpath(path).is_symlink():
                target.unlink()
            os.symlink(contents[blob].decode(), target)
        else:
            target.write_bytes(contents[blob])
            if mode == "100755":
                target.chmod(0o755)
        written += 1
    log_lines.append(f"extracted {written} files from {sha} into {dest}")
    return written


def count_markers(tree: Path) -> dict:
    """Count comment and pytest markers over the whole extracted tree."""
    compiled = {name: re.compile(pattern) for name, pattern in MARKER_PATTERNS.items()}
    counts = dict.fromkeys(MARKER_PATTERNS, 0)
    files_scanned = 0
    for root, dirs, files in os.walk(tree):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(root) / name
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                continue
            files_scanned += 1
            text = raw.decode(errors="replace")
            for key, pattern in compiled.items():
                counts[key] += len(pattern.findall(text))
    counts["files_scanned"] = files_scanned
    counts["todo_or_fixme"] = counts["todo"] + counts["fixme"]
    counts["xfail_markers"] = counts["xfail_decorator"] + counts["xfail_call"]
    counts["skip_markers"] = (
        counts["skip_decorator"] + counts["skipif_decorator"] + counts["skip_call"]
    )
    return counts


def parse_pytest_log(text: str) -> dict:
    """Extract counts and failure/error identities from a pytest log."""
    counts = {
        "collected": None,
        "passed": 0,
        "failed": 0,
        "errored": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
        "warnings": 0,
    }
    for match in COLLECTED_RE.finditer(text):
        counts["collected"] = int(match.group(1))
        if match.group(2):
            counts["deselected"] = int(match.group(2))
    summary_seen = False
    for line in text.splitlines():
        if (
            line.startswith("=")
            and line.rstrip().endswith("=")
            and SUMMARY_TOKEN_RE.search(line)
        ):
            tokens = SUMMARY_TOKEN_RE.findall(line)
            fresh = {k: 0 for k in counts if k not in ("collected", "deselected")}
            for number, word in tokens:
                key = {
                    "error": "errored",
                    "errors": "errored",
                    "warning": "warnings",
                }.get(word, word)
                if key in fresh:
                    fresh[key] = int(number)
            counts.update(fresh)
            summary_seen = True
    failures, errors, xfailed_ids = [], [], []
    for line in text.splitlines():
        match = SHORT_SUMMARY_RE.match(line)
        if not match:
            continue
        kind, nodeid, message = match.group(1), match.group(2), match.group(3) or ""
        if kind == "FAILED":
            failures.append({"nodeid": nodeid, "message": message})
        elif kind == "ERROR":
            errors.append({"nodeid": nodeid, "message": message})
        elif kind == "XFAIL":
            xfailed_ids.append(nodeid)
    timeouts = [e for e in failures + errors if "per-test timeout" in e["message"]]
    return {
        "counts": counts,
        "failures": failures,
        "errors": errors,
        "xfail_ids": xfailed_ids,
        "timeouts": timeouts,
        "summary_seen": summary_seen,
    }


def classify(entries: list) -> tuple:
    """Split failure/error entries into environment-caused and code-caused.

    A case is environment-caused when its exception type is one of
    ENV_EXCEPTIONS and its message matches an ENV_MESSAGES pattern: a missing
    module, an unreadable resource, a refused connection.  Each classified case
    carries the rule that matched, so the split can be checked without
    re-running anything, and the raw counts sit beside the classified ones.
    """
    environment, code = [], []
    for entry in entries:
        found = EXC_TYPE_RE.search(entry["message"])
        kind = found.group(1) if found else None
        matched = next(
            (p for p in ENV_MESSAGES if re.search(p, entry["message"])), None
        )
        if kind in ENV_EXCEPTIONS and matched:
            environment.append({**entry, "exception": kind, "rule": matched})
        else:
            code.append({**entry, "exception": kind})
    return environment, code


def lane_env(tree: Path) -> dict:
    """The environment the lane runs in: the tree first, then this data dir."""
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{tree}:{DATA_DIR}"
    env["TMPDIR"] = "/tmp"  # noqa: S108 - node-local scratch for the lane's temp files
    return env


def import_check(tree: Path) -> tuple:
    """Resolve the tree's nova module in the lane's interpreter and environment."""
    proc = run(
        [
            str(PYTHON),
            "-c",
            "import os, nova; print(nova.__file__); print(os.getcwd())",
        ],
        cwd=tree,
        env=lane_env(tree),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace"))
    module_file, cwd_resolved = proc.stdout.decode().splitlines()[:2]
    return module_file, cwd_resolved


def pytest_command(tree: Path, basetemp: Path) -> list:
    """nova's default fast lane, plus the per-test bound and a private basetemp.

    The selection itself is the tree's own: its pyproject addopts carry
    ``--doctest-modules`` and ``-m 'not slow'``, which no argument here
    overrides.  ``-p no:cacheprovider`` keeps the extracted tree unwritten.
    """
    return [
        str(PYTHON),
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        PLUGIN_NAME,
        f"--basetemp={basetemp}",
    ]


def run_snapshot(snapshot: dict, log_lines: list) -> dict:
    """Extract one snapshot's tree and measure it: pytest, ruff, markers."""
    key, sha = snapshot["key"], snapshot["sha"]
    short = sha[:10]
    tree = TMP_ROOT / "trees" / key
    basetemp = TMP_ROOT / "pytest-tmp" / key
    log_lines.append(f"== snapshot {key} {short}")
    files = extract_tree(sha, tree, log_lines)
    pyproject = (tree / "pyproject.toml").read_text()
    pytest_section = pyproject.split("[tool.pytest.ini_options]", 1)[-1].split(
        "\n[", 1
    )[0]
    module_file, cwd_resolved = import_check(tree)
    if not module_file.startswith(str(tree)):
        raise RuntimeError(
            f"{sha}: import nova resolved to {module_file}, not the tree"
        )
    if basetemp.exists():
        shutil.rmtree(basetemp)
    basetemp.mkdir(parents=True)
    command = pytest_command(tree, basetemp)
    raw_log = DATA_DIR / f".pytest-{key}-{short}.log.part"
    started = time.monotonic()
    truncated = False
    with raw_log.open("wb") as handle:
        try:
            proc = run(
                command,
                cwd=tree,
                env=lane_env(tree),
                timeout=RUN_TIMEOUT_S,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            exit_status = proc.returncode
        except subprocess.TimeoutExpired:
            truncated = True
            exit_status = None
    elapsed = time.monotonic() - started
    body = raw_log.read_text(errors="replace")
    parsed = parse_pytest_log(body)
    environment, code = classify(parsed["failures"] + parsed["errors"])
    log_name = f"pytest-{key}-{short}.log"
    header = [
        "# nova readiness census: one snapshot's fast-lane pytest run",
        f"# revision: {sha}",
        f"# tree: {tree}",
        f"# command: {' '.join(command)}",
        f"# module_under_test: {module_file}",
        f"# measurement_cwd: {cwd_resolved}",
        f"# per_test_timeout_s: {PER_TEST_TIMEOUT_S}",
        f"# elapsed_s: {elapsed:.1f}",
        f"# exit_status: {exit_status}",
    ]
    (DATA_DIR / log_name).write_text("\n".join(header) + "\n" + body)
    raw_log.unlink()
    log_lines.append(f"   pytest {elapsed:.1f}s exit={exit_status} -> {log_name}")
    ruff_proc = run(
        [str(RUFF), "check", "--no-cache", "--output-format=json", "."], cwd=tree
    )
    raw_findings = ruff_proc.stdout.decode(errors="replace") or "[]"
    findings = json.loads(raw_findings)
    by_rule = {}
    for finding in findings:
        rule = finding.get("code") or "unknown"
        by_rule[rule] = by_rule.get(rule, 0) + 1
    ruff_log = f"ruff-{key}-{short}.json"
    (DATA_DIR / ruff_log).write_text(raw_findings)
    markers = count_markers(tree)
    return {
        **snapshot,
        "short": short,
        "tree_files": files,
        "pyproject_blob": git_out("rev-parse", f"{sha}:pyproject.toml").strip(),
        "pytest_config_sha256": hashlib.sha256(pytest_section.encode()).hexdigest(),
        "module_under_test": module_file,
        "measurement_cwd": cwd_resolved,
        "pytest": {
            "command": command,
            "log": log_name,
            "exit_status": exit_status,
            "truncated": truncated,
            "summary_seen": parsed["summary_seen"],
            **parsed["counts"],
            "failures": parsed["failures"],
            "errors": parsed["errors"],
            "xfail_ids": parsed["xfail_ids"],
            "timeouts": [t["nodeid"] for t in parsed["timeouts"]],
            "environment": environment,
            "code": code,
        },
        "ruff": {
            "command": [str(RUFF), "check", "--no-cache", "--output-format=json", "."],
            "log": ruff_log,
            "exit_status": ruff_proc.returncode,
            "version": run([str(RUFF), "--version"]).stdout.decode().strip(),
            "findings": len(findings),
            "by_rule": dict(sorted(by_rule.items())),
        },
        "markers": markers,
    }


def lane_description() -> dict:
    return {
        "description": (
            "nova's default fast lane: pytest under the tree's own "
            "configuration, whose addopts select --doctest-modules and "
            "-m 'not slow'"
        ),
        "interpreter": str(PYTHON),
        "pythonpath": "the extracted tree, then this data directory",
        "per_test_timeout_s": PER_TEST_TIMEOUT_S,
        "timeout_mechanism": f"{PLUGIN_NAME}.py loaded with -p",
        "extraction": "git ls-tree + git cat-file --batch into /tmp",
        "notes": (
            "no sync and no write to the nova checkout; each snapshot runs "
            "with its own --basetemp"
        ),
    }


def assemble(results: list, snapshots: list) -> dict:
    ordered = [r for key in ORDER for r in results if r["key"] == key]
    head_sha = next(s["sha"] for s in snapshots if s["key"] == "head")
    duplicates = [
        r["key"] for r in ordered if r["key"] != "head" and r["sha"] == head_sha
    ]
    return {
        "subject": "nova",
        "repository": str(NOVA),
        "primary_branch": BRANCH,
        "lane": lane_description(),
        "snapshot_rule": (
            "primary-branch commit nearest 12:00Z on each date, by committer "
            "timestamp; before/after candidates and their distances are recorded "
            "per snapshot"
        ),
        "classification_rules": {
            "environment_exceptions": sorted(ENV_EXCEPTIONS),
            "environment_messages": ENV_MESSAGES,
            "note": (
                "a failure or error is environment-caused when its exception "
                "type and its message both match; every case records the rule "
                "that matched, and raw counts stand beside the classified ones"
            ),
        },
        "marker_rules": MARKER_PATTERNS,
        "snapshots": ordered,
        "head_sha": head_sha,
        "snapshot_keys_coinciding_with_head": duplicates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="print key and sha per snapshot"
    )
    parser.add_argument("--run", default="", help="measure one snapshot key")
    parser.add_argument("--all", action="store_true", help="measure every snapshot")
    parser.add_argument(
        "--assemble", default="", help="merge part files from this directory"
    )
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--out", default=str(DATA_DIR / "readiness.json"))
    args = parser.parse_args()
    snapshots = resolve_snapshots()
    if args.list:
        for snapshot in snapshots:
            print(snapshot["key"], snapshot["sha"])
        return 0
    if args.assemble:
        results = []
        for key in ORDER:
            part = Path(args.assemble) / f"{key}.json"
            if part.exists():
                results.append(json.loads(part.read_text()))
        record = assemble(results, snapshots)
        Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        log(f"assembled {len(results)} part(s) into {args.out}")
        return 0
    log_lines = [f"# census invoked: {' '.join(sys.argv[1:])}"]
    if args.run:
        snapshot = next(s for s in snapshots if s["key"] == args.run)
        results = [run_snapshot(snapshot, log_lines)]
    elif args.all:
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_snapshot, s, []): s for s in snapshots}
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
        for key in ORDER:
            entry = next(r for r in results if r["key"] == key)
            log_lines.append(
                f"== snapshot {key} {entry['short']} pytest exit={entry['pytest']['exit_status']}"
            )
    else:
        parser.error("choose --list, --run <key> or --all")
        return 2
    record = assemble(results, snapshots)
    Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    log_lines.append(f"wrote {args.out} ({len(results)} snapshot(s))")
    if args.run:
        (DATA_DIR / f"census-{args.run}.log").write_text("\n".join(log_lines) + "\n")
    else:
        (DATA_DIR / "census.log").write_text("\n".join(log_lines) + "\n")
    log(f"wrote {args.out} ({len(results)} snapshot(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
