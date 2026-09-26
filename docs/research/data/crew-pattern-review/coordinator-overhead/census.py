"""Measure coordinator work from frozen run records and session transcripts.

Capture once with --capture. Subsequent invocations reparse the captured JSONL,
join recorded runtime session identities, and replay merge parents in temporary
object stores. Source repositories and transcripts are never mutated. The compact
output references larger immutable evidence by absolute path and SHA-256.

A landed node is a distinct promoted run, including recorded failed dispositions;
role and gate breakdowns preserve that distinction. Primary-branch promote commits
supply historical timestamps even where the ledger has no promoted_revision field.
Assistant turns are API responses deduplicated by message.id, not user turns.
Cached input is included in total input but is also reported separately.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import collections
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

WINDOW_START = "2026-09-12T00:00:00Z"
WINDOW_END = "2026-09-26T10:00:00Z"
REPO_ROOT = Path("/home/ITER/mcintos/Code")
PROJECTS = {
    "reckon": "main",
    "imas-ambix": "main",
    "nova": "main",
    "imas-efit": "develop",
    "imas-codex": "main",
}
TRANSCRIPT_ROOT = Path.home() / ".claude/projects"
HERE = Path(__file__).resolve().parent
DEFAULT_EVIDENCE = Path(
    "/home/ITER/mcintos/.config/reckon/crew/runs/"
    "r-20260926T104635341057-coordinator-overhead-census"
)
DISPATCH_VERBS = {
    "dispatch",
    "shadow",
    "redispatch",
    "resume",
    "resume-ready",
    "attach",
}
PROMOTION_VERBS = {"complete", "promote"}
FOLLOWER_VERBS = {"follow", "watch", "ticker", "monitor"}
CREW_VERB = re.compile(r"(?<![\w-])crew\s+([a-z][a-z-]*)")
PLAN_PATH = re.compile(r"docs/(plans|evidence|research)/")
LOG_PATH = re.compile(r"(\.log\b|manifest\.md|/runs/|/reviews/|gate[-_.])")
READ_SHELL = re.compile(r"\b(cat|tail|head|grep|rg|sed|less|wc|jq)\b")
CATEGORY_ORDER = [
    "dispatch",
    "promotion",
    "crew_reads_and_mcp_views",
    "git_and_merges",
    "plan_edits",
    "reading_worker_diffs_and_gate_logs",
    "follower_notifications",
    "other",
]
TOKEN_KEYS = [
    "uncached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "input_tokens",
    "output_tokens",
]
RUN_ID = re.compile(r"r-\d{8}T\d+-[a-zA-Z0-9_.-]+")


def stamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def in_window(value):
    parsed = stamp(value)
    return parsed is not None and stamp(WINDOW_START) <= parsed <= stamp(WINDOW_END)


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def reference(path):
    return {
        "path": str(Path(path).resolve()),
        "sha256": digest(path),
        "bytes": Path(path).stat().st_size,
    }


def write_json(path, value):
    Path(path).write_bytes(canonical(value))
    return reference(path)


def git(project, *args, env=None, timeout=120):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    clean.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "TZ": "UTC"})
    clean.update(env or {})
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT / project), *args],
        env=clean,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def git_read(project, *args):
    result = git(project, *args)
    if result.returncode:
        raise RuntimeError(f"git read {project} {args}: {result.stderr[:500]}")
    return result.stdout


def capture(evidence):
    """Freeze raw in-window records, pinned branch history, and exact transcripts."""
    pin_path = HERE / "inputs.json"
    if pin_path.exists():
        raise RuntimeError(
            "inputs.json already exists; capture never overwrites a frozen corpus"
        )
    evidence.mkdir(parents=True, exist_ok=True)
    sources = evidence / "inputs"
    sources.mkdir(exist_ok=True)
    branches = {}
    runs = {}
    provenance = {}
    history_promotions = {}
    for project, branch in sorted(PROJECTS.items()):
        head = git_read(project, "rev-parse", branch).strip()
        cutoff = git_read(
            project, "rev-list", "-1", "--first-parent", f"--before={WINDOW_END}", head
        ).strip()
        history = []
        raw = git_read(
            project,
            "log",
            cutoff,
            "--first-parent",
            f"--since={WINDOW_START}",
            f"--until={WINDOW_END}",
            "--format=%H%x09%P%x09%cI%x09%s",
        )
        for line in raw.splitlines():
            sha, parents, at, subject = line.split("\t", 3)
            if not in_window(at):
                continue
            row = {"sha": sha, "parents": parents.split(), "at": at, "subject": subject}
            history.append(row)
            m = re.match(r"promote\((r-[^)]+)\):", subject)
            if m:
                history_promotions.setdefault((project, m[1]), []).append(row)
        ledger_path = f"docs/state/{project}/crew.json"
        raw = git_read(project, "show", f"{cutoff}:{ledger_path}")
        ledger_runs = json.loads(raw).get("data", {}).get("runs", [])
        for run in ledger_runs:
            key = (project, run["run_id"])
            runs[key] = run
            provenance[key] = ["committed-ledger-at-cutoff"]
        tree_paths = git_read(
            project,
            "ls-tree",
            "-r",
            "--name-only",
            cutoff,
            f"docs/state/{project}/runs/",
        ).splitlines()
        for name in tree_paths:
            if name.endswith(".json"):
                run = json.loads(git_read(project, "show", f"{cutoff}:{name}"))
                run = run.get("data", run)
                if run.get("run_id"):
                    key = (project, run["run_id"])
                    runs[key] = run
                    provenance.setdefault(key, []).append(
                        "committed-run-file-at-cutoff"
                    )
        branches[project] = {
            "primary_branch": branch,
            "capture_head": head,
            "cutoff_revision": cutoff,
            "history": history,
            "ledger_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "ledger_path": ledger_path,
            "ledger_records": len(ledger_runs),
            "run_file_records": len(tree_paths),
        }
    db = Path.home() / ".config/reckon/crew/run_store.db"
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.execute("BEGIN")
    for project, payload, detail in connection.execute(
        "SELECT r.project,r.payload,d.detail FROM runs r LEFT JOIN run_details d USING(run_id)"
    ):
        if project not in PROJECTS:
            continue
        run = json.loads(payload)
        run.update(json.loads(detail or "{}"))
        key = (project, run["run_id"])
        if key not in runs:
            runs[key] = run
        provenance.setdefault(key, []).append("sqlite-capture")
    connection.close()
    selected = []
    for key, run in sorted(runs.items()):
        if not in_window(run.get("dispatched_at")) and key not in history_promotions:
            continue
        selected.append(
            {
                "project": key[0],
                "run": run,
                "sources": provenance[key],
                "promotion_commits": history_promotions.get(key, []),
            }
        )
    run_path = sources / "runs.json.gz"
    run_path.write_bytes(gzip.compress(canonical(selected), mtime=0))
    branch_path = sources / "branches.json"
    write_json(branch_path, branches)
    ids = sorted(
        {
            ((item["run"].get("node_definition") or {}).get("coordinator") or {}).get(
                "runtime_session_id"
            )
            for item in selected
        }
        - {None, ""}
    )
    index = collections.defaultdict(list)
    for directory in sorted(TRANSCRIPT_ROOT.iterdir()):
        if directory.is_dir():
            for path in directory.glob("*.jsonl"):
                if path.stem in ids:
                    index[path.stem].append(path)
    transcripts = {}
    for sid in ids:
        paths = sorted(index.get(sid, []))
        if not paths:
            transcripts[sid] = {"status": "missing", "paths": []}
            continue
        target = sources / f"transcript-{sid}.jsonl.gz"
        source_info = []
        with (
            target.open("wb") as raw_out,
            gzip.GzipFile(fileobj=raw_out, mode="wb", mtime=0, filename="") as out,
        ):
            for path in paths:
                sha = hashlib.sha256()
                kept = malformed = total = 0
                # Pin the byte extent: an active transcript can keep growing during capture.
                extent = path.stat().st_size
                with path.open("rb") as stream:
                    remaining = extent
                    while remaining > 0:
                        line = stream.readline(remaining)
                        if not line:
                            break
                        remaining -= len(line)
                        total += 1
                        sha.update(line)
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            malformed += 1
                            continue
                        if (
                            in_window(record.get("timestamp"))
                            and not record.get("isSidechain")
                            and record.get("type")
                            in ("assistant", "user", "attachment", "system")
                        ):
                            out.write(
                                canonical(
                                    {
                                        "source": str(path),
                                        "line": total,
                                        "record": record,
                                    }
                                )
                            )
                            kept += 1
                source_info.append(
                    {
                        "path": str(path),
                        "prefix_bytes": extent,
                        "prefix_sha256": sha.hexdigest(),
                        "records": total,
                        "selected_records": kept,
                        "malformed_lines": malformed,
                    }
                )
        transcripts[sid] = {
            "status": "captured",
            "snapshot": reference(target),
            "sources": source_info,
        }
        print(
            f"captured {sid}: {sum(s['selected_records'] for s in source_info)} records",
            flush=True,
        )
    pin = {
        "window": [WINDOW_START, WINDOW_END],
        "runs": reference(run_path),
        "branches": reference(branch_path),
        "transcripts": transcripts,
        "evidence_directory": str(evidence),
        "sqlite_source": str(db),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    write_json(pin_path, pin)
    print(f"captured {len(selected)} runs, {len(ids)} recorded session IDs", flush=True)


def checked(ref):
    path = Path(ref["path"])
    if digest(path) != ref["sha256"]:
        raise RuntimeError(f"frozen input digest mismatch: {path}")
    return path


def git_verb(command):
    # Match the command word and skip ordinary git global options.
    match = re.search(
        r"(?:^|[\s;&|])(?:[\w./-]*/)?git\s+"
        r'(?:(?:-C|-c|--git-dir|--work-tree)\s+(?:"[^"]*"|\S+)\s+)*'
        r"([a-z][a-z-]*)\b",
        command,
    )
    return match.group(1) if match else None


def shell_tokens(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def crew_verbs(command):
    # A quoted goal or resume advice may mention other crew commands. Only
    # separate shell words identify calls; prose inside one argument does not.
    tokens = shell_tokens(command)
    return {tokens[i + 1] for i, word in enumerate(tokens[:-1]) if word == "crew"}


def literal_flag(command, flag):
    tokens = shell_tokens(command)
    values = {tokens[i + 1] for i, word in enumerate(tokens[:-1]) if word == flag}
    return (
        next(iter(values))
        if len(values) == 1 and not any("$" in v for v in values)
        else None
    )


def tool_call_category(name, tool_input):
    """Assign one exclusive category, with semantic crew actions taking precedence."""
    if name in ("Bash", "exec_command", "shell_command"):
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        verbs = crew_verbs(command)
        if verbs & DISPATCH_VERBS:
            return "dispatch"
        if verbs & FOLLOWER_VERBS:
            return "follower_notifications"
        if verbs & PROMOTION_VERBS:
            return "promotion"
        if verbs:
            return "crew_reads_and_mcp_views"
        verb = git_verb(command)
        if verb in ("show", "diff"):
            return "reading_worker_diffs_and_gate_logs"
        if verb:
            return "git_and_merges"
        if "/plan/" in command and re.search(r"\b(POST|PATCH)\b", command):
            return "plan_edits"
        if LOG_PATH.search(command) and READ_SHELL.search(command):
            return "reading_worker_diffs_and_gate_logs"
        if PLAN_PATH.search(command) and READ_SHELL.search(command):
            return "crew_reads_and_mcp_views"
        return "other"
    if name.startswith("mcp__reckon"):
        return (
            "plan_edits"
            if name.endswith(("edit_plan", "edit_doc", "write_plan"))
            else "crew_reads_and_mcp_views"
        )
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and PLAN_PATH.search(
        path
    ):
        return "plan_edits"
    if name in ("Read", "Grep", "Glob"):
        if LOG_PATH.search(path):
            return "reading_worker_diffs_and_gate_logs"
        if PLAN_PATH.search(path):
            return "crew_reads_and_mcp_views"
    if name in ("Monitor", "TaskOutput") and any(
        v in str(tool_input).lower() for v in ["crew follow", "crew watch", "reckon"]
    ):
        return "follower_notifications"
    return "other"


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    return ""


def json_objects(text):
    """Decode outer objects only; never treat nested fleet rows as command outcomes."""
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


def read_transcript(meta):
    turns, uses, results, notices = {}, {}, {}, {}
    assistant_records = duplicate_messages = 0
    with gzip.open(checked(meta["snapshot"]), "rt") as stream:
        for line in stream:
            wrapper = json.loads(line)
            record = wrapper["record"]
            loc = {"path": wrapper["source"], "line": wrapper["line"]}
            at = record.get("timestamp")
            msg = record.get("message") or {}
            content = msg.get("content") or []
            if record.get("type") == "assistant":
                assistant_records += 1
                mid = msg.get("id") or record.get("uuid")
                if not mid:
                    raise ValueError(f"assistant without identity: {loc}")
                usage = msg.get("usage") or {}
                if mid in turns:
                    duplicate_messages += 1
                    turn = turns[mid]
                    # Stream snapshots can grow usage; never sum the repeated response.
                    for key, value in usage.items():
                        if isinstance(value, int):
                            turn["usage"][key] = max(turn["usage"].get(key, 0), value)
                else:
                    turns[mid] = {
                        "id": mid,
                        "at": at,
                        "usage": dict(usage),
                        "source": loc,
                    }
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            uses.setdefault(
                                block["id"],
                                {
                                    "name": block.get("name", ""),
                                    "input": block.get("input") or {},
                                    "at": at,
                                    "turn": mid,
                                    "source": loc,
                                },
                            )
            if record.get("type") == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        results[block.get("tool_use_id")] = {
                            "text": content_text(block.get("content")),
                            "at": at,
                            "is_error": bool(block.get("is_error")),
                            "source": loc,
                        }
            attachment = record.get("attachment") or {}
            text = content_text(content) + " " + str(attachment.get("content") or "")
            if record.get("type") in ("user", "attachment") and (
                "reckon obligations" in text.lower()
                or (
                    "<task-notification>" in text
                    and re.search(
                        r"crew (follow|watch)|fleet|reckon", text, re.IGNORECASE
                    )
                )
            ):
                key = record.get("uuid") or (
                    at,
                    hashlib.sha256(text.encode()).hexdigest(),
                )
                notices[key] = loc
    ordered = sorted(turns.values(), key=lambda t: (stamp(t["at"]), t["id"]))
    return (
        ordered,
        uses,
        results,
        {
            "assistant_records": assistant_records,
            "deduplicated_records": duplicate_messages,
            "notifications": len(notices),
            "notification_examples": list(notices.values())[:3],
        },
    )


def receipt_objects(text, families):
    receipts = [obj for obj in json_objects(text) if isinstance(obj.get("ok"), bool)]
    for line in text.splitlines():
        if not line.startswith("{") or "'ok':" not in line:
            continue
        try:
            obj = ast.literal_eval(line)
        except (ValueError, SyntaxError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("ok"), bool):
            receipts.append({**obj, "receipt_shape": "python-literal-receipt"})
    receipts.extend(
        {
            "ok": False,
            "error": "cli-error",
            "detail": reason,
            "receipt_shape": "click-error-line",
        }
        for reason in re.findall(r"^Error: (.+)$", text, re.MULTILINE)
    )
    if not receipts:
        # Shell output is often clipped after a few hundred bytes. Preserve
        # positive protocol markers, never infer success from a quiet shell.
        for match in re.finditer(r'"ok"\s*:\s*(true|false)', text):
            fragment = text[max(0, match.start() - 20) :]
            error = re.search(r'"error"\s*:\s*"([^"\n]+)"', fragment)
            receipts.append(
                {
                    "ok": match.group(1) == "true",
                    "error": error.group(1) if error else None,
                    "detail": fragment[:1200],
                    "receipt_shape": "clipped-ok-marker",
                }
            )
        if not receipts and families == {"promotion"}:
            match = re.search(
                r'^\s*\{\s*"already_promoted"\s*:\s*(true|false)', text, re.MULTILINE
            )
            if match:
                receipts.append(
                    {
                        "ok": True,
                        "already_promoted": match.group(1) == "true",
                        "receipt_shape": "clipped-promotion-success-marker",
                    }
                )
        if not receipts and families == {"dispatch"}:
            match = re.search(
                r'"resumed_session"\s*:\s*"([^"]+)".*?"resumed_turn"\s*:\s*([1-9][0-9]*)',
                text,
            )
            if match:
                receipts.append(
                    {"ok": True, "receipt_shape": "clipped-resume-success-marker"}
                )
    return receipts


def reason_category(reason):
    patterns = [
        (
            "review-required-or-stale",
            r"no review|review.*revision|classified scoring|unreviewed|stored review",
        ),
        (
            "plan-implementation-not-advanced",
            r"impl did not move|plan.*impl|no-impl-change",
        ),
        (
            "missing-gate-receipt",
            r"passing gate requires|gate.*missing|gate.*log|gate evidence|check that produced",
        ),
        (
            "unresolvable-commit",
            r"does not resolve|unknown revision|invalid.*commit|bad object",
        ),
        ("commit-or-change-record", r"commit|uncommitted|changed_paths"),
        ("negative-control", r"negative.control|mutation|red log"),
        ("manifest-record", r"manifest|orientation|status"),
        ("scope-or-boundary", r"scope|boundary|write path|fence"),
        ("resume-path", r"resume|recoverable"),
        ("live-process", r"live run|still alive|process.*alive"),
        ("ledger-or-git-state", r"ledger|lock|git|primary branch|index"),
    ]
    return next(
        (
            label
            for label, pattern in patterns
            if re.search(pattern, reason, re.IGNORECASE)
        ),
        "other-refusal",
    )


def extract_attempts(turns, uses, results):
    rows, unmeasured = [], []
    for uid, use in uses.items():
        if use["name"] != "Bash":
            continue
        command = str(use["input"].get("command") or "")
        verbs = crew_verbs(command)
        families = set()
        if verbs & DISPATCH_VERBS:
            families.add("dispatch")
        if verbs & PROMOTION_VERBS:
            families.add("promotion")
        if (
            not families
            or "--help" in shell_tokens(command)
            or "--dry-run" in shell_tokens(command)
        ):
            continue
        result = results.get(uid)
        receipts = receipt_objects(result["text"], families) if result else []
        if not receipts:
            unmeasured.append(
                {
                    "tool_use_id": uid,
                    "families": sorted(families),
                    "source": use["source"],
                    "reason": "no positive protocol receipt or explicit CLI refusal",
                }
            )
        for index, obj in enumerate(receipts):
            family = (
                next(iter(families))
                if len(families) == 1
                else (
                    "promotion"
                    if any(
                        k in obj
                        for k in [
                            "record",
                            "ledger_path",
                            "pointer_removed",
                            "already_promoted",
                        ]
                    )
                    else "dispatch"
                    if any(k in obj for k in ["launch", "member", "worktree"])
                    else "ambiguous"
                )
            )
            if family == "ambiguous":
                unmeasured.append(
                    {
                        "tool_use_id": uid,
                        "families": sorted(families),
                        "source": use["source"],
                        "reason": "mixed command families",
                    }
                )
                continue
            run = obj.get("run_id") or (obj.get("record") or {}).get("run_id")
            if not run and family == "promotion":
                value = literal_flag(command, "--run")
                run = value if value and RUN_ID.fullmatch(value) else None
            target = literal_flag(command, "--node") if family == "dispatch" else run
            rows.append(
                {
                    "family": family,
                    "ok": obj["ok"],
                    "error": obj.get("error"),
                    "reason": str(obj.get("detail") or obj.get("error") or "")[:1200],
                    "reason_category": reason_category(
                        str(obj.get("detail") or obj.get("error") or "")
                    ),
                    "receipt_shape": obj.get("receipt_shape", "complete-json-object"),
                    "at": result["at"],
                    "tool_use_id": uid,
                    "receipt_index": index,
                    "run_id": run,
                    "target": target,
                    "already_promoted": obj.get("already_promoted"),
                    "source": result["source"],
                    "call_source": use["source"],
                }
            )
    rows.sort(key=lambda r: (stamp(r["at"]), r["tool_use_id"], r["receipt_index"]))
    turn_stamps = [stamp(t["at"]) for t in turns]
    for family in ("dispatch", "promotion"):
        success = [r for r in rows if r["family"] == family and r["ok"]]
        for row in rows:
            if row["family"] != family or row["ok"]:
                continue
            later = next(
                (s for s in success if stamp(s["at"]) > stamp(row["at"])), None
            )
            row["next_success_at"] = later["at"] if later else None
            row["next_success_tool_use_id"] = later["tool_use_id"] if later else None
            row["turns_to_next_success"] = (
                bisect.bisect_right(turn_stamps, stamp(later["at"]))
                - bisect.bisect_right(turn_stamps, stamp(row["at"]))
                if later
                else None
            )
            same = next(
                (
                    s
                    for s in success
                    if row["target"]
                    and s["target"] == row["target"]
                    and stamp(s["at"]) > stamp(row["at"])
                ),
                None,
            )
            row["next_success_same_target_at"] = same["at"] if same else None
            row["turns_to_next_success_same_target"] = (
                bisect.bisect_right(turn_stamps, stamp(same["at"]))
                - bisect.bisect_right(turn_stamps, stamp(row["at"]))
                if same
                else None
            )
    return rows, unmeasured


def refusal_summary(rows, family):
    selected = [r for r in rows if r["family"] == family]
    refused = [r for r in selected if not r["ok"]]
    intervals = [
        r["turns_to_next_success"]
        for r in refused
        if r["turns_to_next_success"] is not None
    ]
    reasons = collections.Counter(r["error"] or "unspecified" for r in refused)
    same_target = [
        r["turns_to_next_success_same_target"]
        for r in refused
        if r["turns_to_next_success_same_target"] is not None
    ]
    return {
        "receipts": len(selected),
        "success_receipts": sum(r["ok"] for r in selected),
        "receipt_shapes": dict(
            sorted(collections.Counter(r["receipt_shape"] for r in selected).items())
        ),
        "refusals": len(refused),
        "by_error_code": dict(sorted(reasons.items())),
        "by_reason": dict(
            sorted(collections.Counter(r["reason_category"] for r in refused).items())
        ),
        "recovered_refusals": len(intervals),
        "right_censored": len(refused) - len(intervals),
        "turns_to_next_success_sum_overlapping": sum(intervals),
        "turns_to_next_success_mean": ratio(sum(intervals), len(intervals)),
        "same_target_recovered_refusals": len(same_target),
        "same_target_right_censored_or_unidentified": len(refused) - len(same_target),
        "turns_to_next_success_same_target_mean": ratio(
            sum(same_target), len(same_target)
        ),
    }


def ratio(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def hand_commits(uses, results):
    confirmed, candidates = [], []
    for uid, use in uses.items():
        if use["name"] != "Bash":
            continue
        command = str(use["input"].get("command") or "")
        if not re.search(r"\bgit\b[^\n;&|]*\bcommit\b", command):
            continue
        result = results.get(uid, {})
        commits = re.findall(
            r"^\[(?:detached HEAD|[^\]\n]+) ([0-9a-f]{7,40})\]",
            result.get("text", ""),
            re.MULTILINE,
        )
        if not commits:
            continue
        row = {
            "tool_use_id": uid,
            "source": use["source"],
            "result_source": result.get("source"),
            "commits": commits,
            "command": command,
            "result": result.get("text", "")[:4000],
        }
        # An explicit git -C worker tree establishes where the commit was made.
        direct = re.search(
            r'\bgit\s+-C\s+[\'"]?(/[^\s\'";]*\.reckon-worktrees/[^\s\'";]*)'
            r"[\'\"]?\s+commit\b",
            command,
        )
        if direct:
            row["worker_tree"] = direct.group(1)
            row["basis"] = (
                "successful commit receipt with explicit git -C worker worktree"
            )
            confirmed.append(row)
        else:
            candidates.append(row)
    return (
        {
            "status": "not-measured" if not confirmed else "partial",
            "count": None,
            "confirmed_tool_calls": len(confirmed),
            "confirmed_commits": sum(len(r["commits"]) for r in confirmed),
            "other_successful_commit_calls_not_attributed": len(candidates),
            "limitation": "No exhaustive provenance join for quiet commits, variable or cd commands, scripts, and patch copies. Zero matched explicit commands is not a zero hand-integration count.",
        },
        confirmed,
        candidates,
    )


def merge_candidates(branches):
    rows = []
    for project, data in sorted(branches.items()):
        for commit in data["history"]:
            if len(commit["parents"]) < 2:
                continue
            names = git_read(
                project,
                "diff",
                "--name-only",
                commit["parents"][0],
                commit["sha"],
                "--",
                "docs/plans/",
                "docs/evidence/",
            ).splitlines()
            if names:
                rows.append({"project": project, **commit, "touched_docs": names})
    return rows


def replay_merge(row):
    if len(row["parents"]) != 2:
        return {
            **row,
            "status": "unmeasured",
            "reason": "octopus merge has more than two parents",
        }
    objects = git_read(
        row["project"], "rev-parse", "--path-format=absolute", "--git-path", "objects"
    ).strip()
    with tempfile.TemporaryDirectory(prefix="coordinator-merge-", dir="/tmp") as folder:
        env = {
            "GIT_OBJECT_DIRECTORY": folder,
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
        }
        result = git(
            row["project"],
            "-c",
            "merge.renames=true",
            "merge-tree",
            "--write-tree",
            "--name-only",
            "-z",
            *row["parents"],
            env=env,
            timeout=60,
        )
    if result.returncode not in (0, 1):
        return {
            **row,
            "status": "unmeasured",
            "exit": result.returncode,
            "reason": result.stderr[:1000],
        }
    fields = result.stdout.split("\0")
    paths = []
    if result.returncode == 1:
        for field in fields[1:]:
            if not field:
                break
            paths.append(field)
    return {
        **row,
        "status": "measured",
        "exit": result.returncode,
        "conflicted": result.returncode == 1,
        "conflict_paths": sorted(paths),
        "docs_conflicted": any(
            p.startswith(("docs/plans/", "docs/evidence/")) for p in paths
        ),
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
    }


def build(pin, output):
    evidence = Path(pin["evidence_directory"])
    runs = json.loads(gzip.decompress(checked(pin["runs"]).read_bytes()))
    branches = json.loads(checked(pin["branches"]).read_bytes())
    grouped = collections.defaultdict(list)
    for item in runs:
        coord = (item["run"].get("node_definition") or {}).get("coordinator") or {}
        sid = coord.get("runtime_session_id")
        key = (
            sid
            or f"unattributed:{item['project']}:{coord.get('session_id') or 'unknown'}"
        )
        grouped[key].append(item)
    sessions, all_attempts, all_unmeasured, hand_detail, run_detail = [], [], [], [], []
    controls = []
    for sid, items in sorted(grouped.items()):
        meta = pin["transcripts"].get(sid, {"status": "unattributed"})
        session = {
            "session_id": sid,
            "attribution": "recorded"
            if not sid.startswith("unattributed:")
            else "unattributed",
            "projects": sorted({i["project"] for i in items}),
            "labels": sorted(
                {
                    (
                        (i["run"].get("node_definition") or {}).get("coordinator") or {}
                    ).get("session_id")
                    or ""
                    for i in items
                }
            ),
            "runs_in_cohort": len(items),
            "transcript_status": meta["status"],
        }
        receipts = []
        if meta["status"] == "captured":
            turns, uses, results, diagnostics = read_transcript(meta)
            receipts, unknown = extract_attempts(turns, uses, results)
            all_attempts.extend({"session_id": sid, **r} for r in receipts)
            all_unmeasured.extend({"session_id": sid, **r} for r in unknown)
            tokens = dict.fromkeys(TOKEN_KEYS, 0)
            for turn in turns:
                usage = turn["usage"]
                tokens["uncached_input_tokens"] += usage.get("input_tokens", 0)
                for key in (
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                ):
                    tokens[key] += usage.get(key, 0)
            tokens["input_tokens"] = sum(tokens[k] for k in TOKEN_KEYS[:3])
            mix = collections.Counter(
                tool_call_category(u["name"], u["input"]) for u in uses.values()
            )
            hands, confirmed, candidates = hand_commits(uses, results)
            hand_detail.append(
                {"session_id": sid, "confirmed": confirmed, "candidates": candidates}
            )
            session.update(
                {
                    "assistant_turns": len(turns),
                    "turns_without_usage": sum(not t["usage"] for t in turns),
                    "tokens": tokens,
                    "tool_calls": {k: mix[k] for k in CATEGORY_ORDER},
                    "tool_calls_total": len(uses),
                    "follower_notifications": diagnostics["notifications"],
                    "diagnostics": diagnostics,
                    "hand_committed_worker_diffs": hands,
                    "dispatch_refusals": refusal_summary(receipts, "dispatch"),
                    "promotion_refusals": refusal_summary(receipts, "promotion"),
                    "attempt_calls_without_classifiable_receipt": len(unknown),
                }
            )
            if turns:
                controls.append(
                    {
                        "session_id": sid,
                        "source": turns[0]["source"],
                        "message_id": turns[0]["id"],
                        "usage": turns[0]["usage"],
                        "assistant_turns": len(turns),
                        "raw_assistant_records": diagnostics["assistant_records"],
                        "tools": len(uses),
                        "refusals": sum(not r["ok"] for r in receipts),
                    }
                )
        else:
            session.update(
                {
                    "assistant_turns": None,
                    "tokens": None,
                    "tool_calls": None,
                    "hand_committed_worker_diffs": {
                        "status": "not-measured",
                        "reason": "transcript unavailable",
                    },
                }
            )
        landed = []
        for item in items:
            run = item["run"]
            matching = [
                r
                for r in receipts
                if r["family"] == "promotion"
                and r["ok"]
                and r["run_id"] == run["run_id"]
            ]
            history = item["promotion_commits"]
            # A committed marker proves promotion no later than the pinned cutoff.
            marker = (
                bool(run.get("promoted_revision"))
                and "committed-ledger-at-cutoff" in item["sources"]
            )
            promoted = bool(history or matching or marker)
            if promoted:
                landed.append(item)
            run_detail.append(
                {
                    "project": item["project"],
                    "run_id": run["run_id"],
                    "session_id": sid,
                    "role": run.get("role"),
                    "gate": run.get("gate"),
                    "dispatched_at": run.get("dispatched_at"),
                    "sources": item["sources"],
                    "landed_promoted": promoted,
                    "promotion_commits": history,
                    "transcript_promotion_receipts": matching,
                    "committed_promotion_marker": marker,
                    "commits": run.get("commits") or [],
                }
            )
        session["landed_nodes"] = len(landed)
        session["landed_by_gate"] = dict(
            sorted(
                collections.Counter(
                    i["run"].get("gate") or "unknown" for i in landed
                ).items()
            )
        )
        session["landed_by_role"] = dict(
            sorted(
                collections.Counter(
                    i["run"].get("role") or "unknown" for i in landed
                ).items()
            )
        )
        session["runs_without_confirmed_promotion_in_window"] = len(items) - len(landed)
        if meta["status"] == "captured":
            session["assistant_turns_per_landed_node"] = ratio(
                session["assistant_turns"], len(landed)
            )
            session["tokens_per_landed_node"] = {
                k: ratio(v, len(landed)) for k, v in session["tokens"].items()
            }
            session["tool_calls_per_landed_node"] = {
                k: ratio(v, len(landed)) for k, v in session["tool_calls"].items()
            }
        sessions.append(session)
        print(
            f"measured {sid}: landed={len(landed)} transcript={meta['status']}",
            flush=True,
        )
    candidates = merge_candidates(branches)
    print(
        f"replaying {len(candidates)} documentation-touching merges with four threads",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        merges = list(pool.map(replay_merge, candidates))
    merge_summary = {}
    for project, branch in PROJECTS.items():
        rows = [r for r in merges if r["project"] == project]
        merge_summary[project] = {
            "primary_branch": branch,
            "cutoff_revision": branches[project]["cutoff_revision"],
            "documentation_merges": len(rows),
            "measured": sum(r["status"] == "measured" for r in rows),
            "conflicting_any_path": sum(r.get("conflicted", False) for r in rows),
            "conflicting_docs_path": sum(r.get("docs_conflicted", False) for r in rows),
            "unmeasured": sum(r["status"] != "measured" for r in rows),
        }
    transcribed = [s for s in sessions if s["transcript_status"] == "captured"]
    denominator = sum(s["landed_nodes"] for s in transcribed)
    totals = {
        "sessions": len(sessions),
        "sessions_with_transcript": len(transcribed),
        "sessions_without_transcript": len(sessions) - len(transcribed),
        "unattributed_session_groups": sum(
            s["attribution"] == "unattributed" for s in sessions
        ),
        "unattributed_runs": sum(
            s["runs_in_cohort"] for s in sessions if s["attribution"] == "unattributed"
        ),
        "runs_in_cohort": len(runs),
        "landed_nodes": sum(s["landed_nodes"] for s in sessions),
        "landed_nodes_with_transcript": denominator,
        "assistant_turns": sum(s["assistant_turns"] for s in transcribed),
        "tokens": {k: sum(s["tokens"][k] for s in transcribed) for k in TOKEN_KEYS},
        "tool_calls": {
            k: sum(s["tool_calls"][k] for s in transcribed) for k in CATEGORY_ORDER
        },
        "follower_notifications": sum(s["follower_notifications"] for s in transcribed),
        "hand_committed_worker_diffs": {
            "status": "not-measured",
            "count": None,
            "reason": "Exhaustive worker-diff provenance is not reconstructed from quiet commits, variables, scripts and patch copies; candidate receipts are retained for review.",
            "confirmed_tool_calls": sum(
                s["hand_committed_worker_diffs"]["confirmed_tool_calls"]
                for s in transcribed
            ),
            "confirmed_commits": sum(
                s["hand_committed_worker_diffs"]["confirmed_commits"]
                for s in transcribed
            ),
        },
        "dispatch_refusals": refusal_summary(all_attempts, "dispatch"),
        "promotion_refusals": refusal_summary(all_attempts, "promotion"),
        "attempt_calls_without_classifiable_receipt": len(all_unmeasured),
        "merges": {
            k: sum(m[k] for m in merge_summary.values())
            for k in [
                "documentation_merges",
                "measured",
                "conflicting_any_path",
                "conflicting_docs_path",
                "unmeasured",
            ]
        },
    }
    totals["assistant_turns_per_landed_node"] = ratio(
        totals["assistant_turns"], denominator
    )
    totals["landed_by_gate"] = dict(
        sorted(
            collections.Counter(
                row["gate"] or "unknown" for row in run_detail if row["landed_promoted"]
            ).items()
        )
    )
    totals["landed_by_role"] = dict(
        sorted(
            collections.Counter(
                row["role"] or "unknown" for row in run_detail if row["landed_promoted"]
            ).items()
        )
    )
    totals["tokens_per_landed_node"] = {
        k: ratio(v, denominator) for k, v in totals["tokens"].items()
    }
    totals["tool_calls_per_landed_node"] = {
        k: ratio(v, denominator) for k, v in totals["tool_calls"].items()
    }
    details = {
        "runs": write_json(evidence / "run-attribution.json", run_detail),
        "attempts": write_json(evidence / "attempts.json", all_attempts),
        "unmeasured_attempts": write_json(
            evidence / "unmeasured-attempts.json", all_unmeasured
        ),
        "hand_commits": write_json(evidence / "hand-commit-evidence.json", hand_detail),
        "merges": write_json(evidence / "merge-replays.json", merges),
    }
    result = {
        "window": [WINDOW_START, WINDOW_END],
        "inputs": reference(HERE / "inputs.json"),
        "method": {
            "landed_node": "Distinct run with in-window primary-branch promote commit, successful complete receipt, or promoted_revision already committed at cutoff; administrative promotions include failed and not-run outcomes, reported by gate and role.",
            "cohort": "Run dispatched in window or promoted on primary branch in window; later promotions do not enter the denominator.",
            "assistant_turn": "Unique message.id across in-window non-sidechain assistant records; usage max per repeated response; missing transcript is null, never zero.",
            "input_tokens": "uncached + cache creation + cache read; logical input volume, not billed tokens or dollars",
            "tool_mix": "One exclusive category per tool_use ID; compound shell calls count once using ordered classifier; other is retained.",
            "retry_latency": "Assistant responses after refusal result through next observed positive protocol receipt in the same operation family and session, not necessarily the same node. Same-batch successes are not assigned latency. Overlapping intervals are not additive active labor.",
            "promotion_refusal_reasons": "Error lines as well as JSON refusals are parsed; exact reasons, source lines and per-refusal turns live in attempts artifact. Summaries group by emitted code and regex-labelled reason; cli-error denotes a CLI without a structured code. Clipped success markers are counted separately from full JSON.",
            "hand_commits": "Not measured exhaustively: explicit successful worker-tree commit commands were searched, but quiet commits and copied diffs require a separate provenance join. Diagnostic zeros are not absence claims.",
            "merges": "First-parent primary history at pinned cutoff, diff against first parent touching docs/plans or docs/evidence; git merge-tree on exact two parents. Any-path and docs-path conflicts separate; non-0/1 exits unmeasured.",
            "scope": "Whole coordinator session activity inside window, including work not assigned to crew; no claim of causal time or monetary cost.",
            "worker_streams": "Not consumed; all recorded coordinator harnesses use message-style session transcripts. No absence claim about thread.started worker streams.",
        },
        "totals": totals,
        "sessions": sessions,
        "primary_branches": merge_summary,
        "positive_controls": controls,
        "protocol_controls": {
            "dispatch_refusal": next(
                (r for r in all_attempts if r["family"] == "dispatch" and not r["ok"]),
                None,
            ),
            "promotion_refusal": next(
                (r for r in all_attempts if r["family"] == "promotion" and not r["ok"]),
                None,
            ),
            "promotion_success": next(
                (r for r in all_attempts if r["family"] == "promotion" and r["ok"]),
                None,
            ),
            "conflicting_merge": next(
                (r for r in merges if r.get("docs_conflicted")), None
            ),
            "clean_merge": next(
                (
                    r
                    for r in merges
                    if r["status"] == "measured" and not r["conflicted"]
                ),
                None,
            ),
        },
        "detail_artifacts": details,
    }
    output.write_bytes(canonical(result))
    assert output.stat().st_size < 300000, "compact output must remain below 300 KB"
    assert sum(totals["tool_calls"].values()) == sum(
        s["tool_calls_total"] for s in transcribed
    )
    assert controls and any(
        c["raw_assistant_records"] > c["assistant_turns"] for c in controls
    )
    print(
        json.dumps(
            {"output": str(output), "sha256": digest(output), "totals": totals},
            sort_keys=True,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--output", type=Path, default=HERE / "overhead.json")
    args = parser.parse_args()
    if args.capture:
        capture(args.evidence_dir.resolve())
    build(json.loads((HERE / "inputs.json").read_text()), args.output.resolve())


if __name__ == "__main__":
    main()
