#!/usr/bin/env python3
"""Measure the input tokens a coordinator spends loading the reckon-build
skill and its references before its first ``reckon crew dispatch`` call.

Reads Claude Code coordinator transcripts under ``~/.claude/projects``
read-only. Selects every session that recorded a load of the reckon-build
skill with an event timestamp on the baseline date, and reports, per session,
the input tokens that load cost and the median across sessions.

A "load" is the fence's own definition: a Skill invocation of reckon-build,
an injected SKILL.md body, or a Read of SKILL.md or of any file under
``reckon-build/references/``.

Token accounting. The transcript records, per assistant message, a usage
block whose total input for that request is
``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``.
A load's cost is the rise in that total across the load: the next recorded
total after the injected content lands, minus the last total recorded before
it. Nothing is estimated from characters.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import statistics
import subprocess
import sys

TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")
SKILL_DIR = os.path.join(
    os.path.expanduser("~/.claude/skills"), "reckon-build"
)
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REFERENCES_DIR = os.path.join(SKILL_DIR, "references")
BODY_MARKER = "Base directory for this skill: /home/ITER/mcintos/.claude/skills/reckon-build"
DISPATCH_RE = re.compile(r"reckon\s+crew\s+dispatch")

# A skill body injected as its own message within this many message indices of
# a Skill invocation is that invocation's content, not a second load.
SKILL_BODY_FUSE = 5


def total_input(usage):
    """Total input tokens for one recorded request."""
    if not isinstance(usage, dict):
        return None
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
    )


def load_messages(path):
    records = []
    with open(path, errors="ignore") as handle:
        for index, line in enumerate(handle):
            if "reckon-build" not in line and "crew dispatch" not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append((index, obj))
    return records


def is_reference_path(file_path):
    return "reckon-build/references/" in str(file_path)


def is_skill_md_path(file_path):
    return "reckon-build/SKILL.md" in str(file_path)


def classify(records):
    """Return (loads, dispatches, hours_since_first_load) for one transcript.

    Each load is a dict: index, timestamp, kind, file, and whether it is a
    skill-body load (SKILL.md) or a reference load.
    """
    loads = []
    dispatches = []
    skill_invocations = []
    for index, obj in records:
        timestamp = obj.get("timestamp") or ""
        message = obj.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    tool_input = block.get("input") or {}
                    if name == "Bash" and DISPATCH_RE.search(
                        str(tool_input.get("command", ""))
                    ):
                        dispatches.append((index, timestamp))
                    if name == "Skill" and tool_input.get("skill") == "reckon-build":
                        skill_invocations.append(index)
                        loads.append(
                            {
                                "index": index,
                                "ts": timestamp,
                                "kind": "Skill",
                                "file": "SKILL.md",
                                "body": True,
                            }
                        )
                    if name == "Read":
                        file_path = tool_input.get("file_path", "")
                        if is_skill_md_path(file_path) or is_reference_path(file_path):
                            loads.append(
                                {
                                    "index": index,
                                    "ts": timestamp,
                                    "kind": "Read",
                                    "file": str(file_path),
                                    "body": is_skill_md_path(file_path),
                                }
                            )
                if block.get("type") == "text" and BODY_MARKER in str(
                    block.get("text", "")
                ):
                    if not any(
                        0 <= index - si <= SKILL_BODY_FUSE for si in skill_invocations
                    ):
                        loads.append(
                            {
                                "index": index,
                                "ts": timestamp,
                                "kind": "injected-body",
                                "file": "SKILL.md",
                                "body": True,
                            }
                        )
        attachment = obj.get("attachment") or {}
        if (
            isinstance(attachment, dict)
            and attachment.get("type") == "invoked_skills"
            and "reckon-build" in json.dumps(attachment)
        ):
            loads.append(
                {
                    "index": index,
                    "ts": timestamp,
                    "kind": "invoked-skills",
                    "file": "SKILL.md",
                    "body": True,
                }
            )
    return loads, dispatches


def anchors(path):
    """Ordered (index, total_input) for every assistant message with usage.

    Scans every line: a request that mentions neither reckon-build nor crew
    dispatch still carries the context total a load is measured against, so
    the load-relevant filter used for classification must not be applied here.
    """
    out = []
    with open(path, errors="ignore") as handle:
        for index, line in enumerate(handle):
            if '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            usage = (obj.get("message") or {}).get("usage") or {}
            total = total_input(usage)
            if total is not None:
                out.append((index, total))
    return out


def cost_of_load(anchor_list, load_index):
    """Rise in recorded input total across a load, from the anchors.

    Returns (tokens, note). tokens is None when the instrument cannot isolate
    the load: there is no request before it (the skill was injected at session
    start), or the context shrank across it (a compaction landed between the
    two requests, so the re-injected content is bundled with the summary).
    A non-zero gap between the bracketing requests is reported in the note.
    """
    before = None
    after = None
    for index, total in anchor_list:
        if index < load_index:
            before = (index, total)
        elif index > load_index:
            after = (index, total)
            break
    if before is None:
        return None, "no request precedes the load"
    if after is None:
        return None, "no request follows the load"
    if after[1] <= before[1]:
        return None, "context shrank across the load (compaction)"
    gap = after[0] - before[0]
    note = "" if gap <= 80 else f"{gap} lines between bracketing requests"
    return after[1] - before[1], note


def word_count(path):
    with open(path, errors="ignore") as handle:
        return len(handle.read().split())


def reference_files():
    return sorted(
        glob.glob(os.path.join(REFERENCES_DIR, "**", "*.md"), recursive=True)
    )


def selected_sessions(baseline_date):
    """Sessions with at least one reckon-build skill load on the date."""
    sessions = []
    for path in sorted(glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "*.jsonl"))):
        try:
            with open(path, errors="ignore") as handle:
                blob = handle.read()
        except OSError:
            continue
        if "reckon-build" not in blob:
            continue
        records = load_messages(path)
        loads, dispatches = classify(records)
        on_date = [ld for ld in loads if ld["ts"][:10] == baseline_date]
        if not on_date:
            continue
        sessions.append(
            {
                "path": path,
                "session": os.path.basename(path)[:-6],
                "first_ts": first_timestamp(records),
                "loads": loads,
                "on_date": on_date,
                "dispatches": dispatches,
            }
        )
    return sessions


def first_timestamp(records):
    for _, obj in records:
        ts = obj.get("timestamp")
        if ts:
            return ts
    return ""


def measure_session(session):
    """Tokens spent on 09-26 loads before the first dispatch at/after them."""
    anchor_list = anchors(session["path"])
    counted = [ld for ld in session["loads"] if ld["ts"][:10] == BASELINE_DATE]
    if not counted:
        return None
    first_index = counted[0]["index"]
    boundary = None
    for index, ts in session["dispatches"]:
        if index >= first_index:
            boundary = index
            break
    window = [ld for ld in counted if boundary is None or ld["index"] < boundary]
    per_load = []
    for load in window:
        cost, note = cost_of_load(anchor_list, load["index"])
        per_load.append({**load, "tokens": cost, "note": note})
    measured = [p["tokens"] for p in per_load if p["tokens"] is not None]
    return {
        "session": session["session"],
        "started": session["first_ts"][:19],
        "boundary": "end-of-transcript" if boundary is None else "first-dispatch",
        "loads": per_load,
        "total_tokens": sum(measured) if measured else 0,
        "measured_loads": len(measured),
        "unmeasured": [p for p in per_load if p["tokens"] is None],
    }


BASELINE_DATE = "2026-09-26"


def positive_control():
    """A known-present load must be seen, and a transcript quote rejected."""
    good = None
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "*.jsonl")):
        if os.path.basename(path).startswith("1d1d2f0d"):
            good = path
            break
    if good is None:
        return False, "positive-control transcript not found"
    loads, _ = classify(load_messages(good))
    if not any(ld["kind"] == "Skill" for ld in loads):
        return False, "known Skill load not detected"
    # A transcript quoted inside a tool_result is not a load: the string sits
    # in a tool_result block, which classify() never reads for loads.
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "*.jsonl")):
        if not os.path.basename(path).startswith("1225959a"):
            continue
        loads, _ = classify(load_messages(path))
        quoted = [ld for ld in loads if ld["ts"][:10] == BASELINE_DATE]
        if quoted:
            return False, "a quoted transcript was counted as a load"
    return True, "known Skill load seen; quoted transcript rejected"


def git_revision():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=BASELINE_DATE)
    parser.add_argument("--top", type=int, default=None)
    args = parser.parse_args(argv)

    revision = git_revision()
    tree = os.getcwd()
    command = " ".join(sys.argv)
    out = []
    out.append(f"revision {revision} tree {tree} command: {command}")
    out.append(f"transcript root: {TRANSCRIPT_ROOT} (read-only)")
    out.append(
        "selection rule: a session is selected when, on the baseline date, it "
        "records at least one reckon-build skill load -- a Skill invocation of "
        "reckon-build, an injected SKILL.md body, or a Read of SKILL.md or of a "
        "file under reckon-build/references/. The load window ends at the first "
        "`reckon crew dispatch` call at or after the first load, or at the end "
        "of the transcript when the session never dispatched."
    )
    out.append("selection date: " + args.date)
    ok, detail = positive_control()
    out.append(f"control: {ok} {detail}")
    if not ok:
        print("\n".join(out))
        return 2

    sessions = selected_sessions(args.date)
    out.append(f"selected sessions: {len(sessions)}")
    for session in sessions:
        out.append(f"  {session['session']}  started {session['first_ts'][:19]}")

    results = []
    for session in sessions:
        measured = measure_session(session)
        if measured is None:
            continue
        results.append(measured)

    out.append("")
    out.append("per-session input tokens spent loading the skill before first dispatch:")
    for row in sorted(results, key=lambda r: r["total_tokens"] or 0):
        kinds = ", ".join(
            "{}:{}={}{}".format(
                ld["kind"],
                os.path.basename(ld["file"]),
                ld["tokens"],
                f" ({ld['note']})" if ld["note"] else "",
            )
            for ld in row["loads"]
        )
        out.append(
            f"  {row['session']}  boundary={row['boundary']:>16}  "
            f"total={row['total_tokens']:>7}  loads={row['measured_loads']} "
            f"[{kinds}]"
        )
        for ld in row["unmeasured"]:
            out.append(
                f"      unmeasured: {ld['kind']} "
                f"{os.path.basename(ld['file'])} — {ld['note']}"
            )

    totals = [r["total_tokens"] for r in results if r["measured_loads"]]
    if totals:
        out.append("")
        out.append(f"n sessions measured: {len(totals)}")
        out.append(f"median input tokens: {statistics.median(totals):.1f}")
        out.append(
            f"min {min(totals)}  max {max(totals)}  mean {statistics.mean(totals):.1f}"
        )
        sk_only = [
            r["total_tokens"]
            for r in results
            if r["measured_loads"] and all(ld["body"] for ld in r["loads"])
        ]
        if sk_only:
            out.append(
                f"SKILL.md-only sessions: n {len(sk_only)} "
                f"median {statistics.median(sk_only):.1f}"
            )

    skill_words = word_count(SKILL_MD)
    ref_paths = reference_files()
    ref_words = sum(word_count(p) for p in ref_paths)
    out.append("")
    out.append("word counts (whitespace-split, the done-when's rule):")
    out.append(f"  SKILL.md: {skill_words} words")
    out.append(f"  references ({len(ref_paths)} files): {ref_words} words")
    out.append(f"  skill + references: {skill_words + ref_words} words")
    out.append(f"  ceiling: 12000 words — headroom {12000 - skill_words - ref_words}")
    out.append(f"measured at {datetime.datetime.now(datetime.UTC).isoformat()}")

    text = "\n".join(out)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())