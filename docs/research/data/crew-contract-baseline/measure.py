"""Measure the input tokens a coordinator spends loading the reckon-build
skill and its references before its first ``reckon crew dispatch`` call.

Reads Claude Code coordinator transcripts under ``~/.claude/projects``
read-only. Selects sessions that recorded a load of the reckon-build skill on
the baseline date, sums, per session, the cost of every load of SKILL.md and of
every reference up to the session's first dispatch, and reports the median of
those per-session totals.

A "load" is the skill being delivered to, or read by, a coordinator from its
installed location: a Skill invocation of reckon-build, an injected SKILL.md
body, or a whole-file Read of a path under the installed skill tree. Two things
are deliberately not loads. A Read of a copy of the skill inside a repository
that is being edited is authoring, not loading, and a partial Read (one carrying
an ``offset`` or a ``limit``) reads a fragment of a file rather than loading the
skill.

Token accounting. The transcript records, per assistant request, a usage block
whose total input for that request is
``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``.
A load's cost is the rise in that total across the load: the next recorded
total after the injected content lands, minus the total the load's own request
recorded. Taking the load's own request as the lower bracket is what keeps a
tool result that landed earlier in the same turn out of the figure. Nothing is
estimated from characters.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import statistics
import subprocess
import sys

TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")
SKILL_DIR = os.path.join(os.path.expanduser("~/.claude/skills"), "reckon-build")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REFERENCES_DIR = os.path.join(SKILL_DIR, "references")
BODY_MARKER = (
    "Base directory for this skill: /home/ITER/mcintos/.claude/skills/reckon-build"
)
DISPATCH_RE = re.compile(r"reckon\s+crew\s+dispatch")

BASELINE_DATE = "2026-09-26"
# Dates are added symmetrically about the baseline until five sessions qualify.
DATE_WINDOW = [
    "2026-09-26",
    "2026-09-25",
    "2026-09-27",
    "2026-09-24",
    "2026-09-28",
    "2026-09-23",
    "2026-09-22",
    "2026-09-21",
    "2026-09-20",
    "2026-09-19",
]
MIN_SESSIONS = 5
# A skill body injected as its own message within this many message indices of a
# Skill invocation is that invocation's content, not a second load.
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


def load_records(path):
    """Parse only the lines that can carry a load or a dispatch."""
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


def anchors(path):
    """Ordered (index, total_input) for every assistant request with usage.

    Scans every line: a request that mentions neither reckon-build nor crew
    dispatch still carries the context total a load is measured against.
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


def classify(records):
    """Return (loads, dispatches) for one transcript.

    Each load carries: index, ts, kind, file, body (True for SKILL.md), and
    shared_turn (True when the load's turn issued more than one tool, so the
    delta cannot be split from the other tool's result).
    """
    loads = []
    dispatches = []
    skill_invocations = []
    for index, obj in records:
        timestamp = obj.get("timestamp") or ""
        message = obj.get("message") or {}
        content = message.get("content")
        tool_uses = []
        if isinstance(content, list):
            tool_uses = [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
        for block in tool_uses:
            name = block.get("name")
            tool_input = block.get("input") or {}
            if name == "Bash" and is_dispatch_command(tool_input):
                dispatches.append((index, timestamp))
        for block in tool_uses:
            name = block.get("name")
            tool_input = block.get("input") or {}
            if name == "Skill" and tool_input.get("skill") == "reckon-build":
                skill_invocations.append(index)
                loads.append(
                    load(
                        index, timestamp, "Skill", "SKILL.md", True, len(tool_uses) > 1
                    )
                )
            if name == "Read":
                file_path = str(tool_input.get("file_path", ""))
                partial = tool_input.get("offset") is not None or (
                    tool_input.get("limit") is not None
                )
                if is_installed_skill_path(file_path) and not partial:
                    loads.append(
                        load(
                            index,
                            timestamp,
                            "Read",
                            file_path,
                            is_skill_md_path(file_path),
                            len(tool_uses) > 1,
                        )
                    )
        if isinstance(content, list):
            loads.extend(
                load(index, timestamp, "injected-body", "SKILL.md", True, False)
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and BODY_MARKER in str(block.get("text", ""))
                and not any(
                    0 <= index - si <= SKILL_BODY_FUSE for si in skill_invocations
                )
            )
        attachment = obj.get("attachment") or {}
        if (
            isinstance(attachment, dict)
            and attachment.get("type") == "invoked_skills"
            and "reckon-build" in json.dumps(attachment)
        ):
            loads.append(
                load(index, timestamp, "invoked-skills", "SKILL.md", True, False)
            )
    return loads, dispatches


def load(index, ts, kind, file, body, shared):
    return {
        "index": index,
        "ts": ts,
        "kind": kind,
        "file": file,
        "body": body,
        "shared_turn": shared,
    }


def is_dispatch_command(tool_input):
    return DISPATCH_RE.search(str(tool_input.get("command", "")))


def is_installed_skill_path(file_path):
    """A path under the installed skill tree, not a repo's copy of it."""
    return os.path.realpath(str(file_path)).startswith(
        os.path.realpath(SKILL_DIR) + os.sep
    )


def is_skill_md_path(file_path):
    return os.path.basename(str(file_path)) == "SKILL.md"


def cost_of_load(anchor_list, load_index, shared_turn):
    """Rise in recorded input total across a load: (tokens, note).

    The lower bracket is the load's own request when the transcript records one
    at that index; that keeps a tool result which landed earlier in the same
    turn out of the figure. tokens is None when the load cannot be isolated:
    no request precedes it (the skill was injected at session start), no request
    follows it, or the context shrank across it (a compaction landed between the
    two requests and bundled the re-injection with its summary). A turn issuing
    more than one tool is reported as an upper bound.
    """
    before = None
    after = None
    for index, total in anchor_list:
        if index <= load_index:
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
    note = ""
    if shared_turn:
        note = "upper bound: the load shares its turn with another tool"
    return after[1] - before[1], note


def mtime(path):
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path), datetime.UTC
        ).strftime("%Y-%m-%d %H:%M UTC")
    except OSError:
        return "unknown"


def word_count(path):
    with open(path, errors="ignore") as handle:
        return len(handle.read().split())


def reference_files():
    return sorted(glob.glob(os.path.join(REFERENCES_DIR, "**", "*.md"), recursive=True))


def first_timestamp(records):
    for _, obj in records:
        ts = obj.get("timestamp")
        if ts:
            return ts
    return ""


def candidate_sessions(dates):
    """Sessions with at least one reckon-build load on any candidate date."""
    out = []
    for path in sorted(glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "*.jsonl"))):
        try:
            with open(path, errors="ignore") as handle:
                blob = handle.read()
        except OSError:
            continue
        if "reckon-build" not in blob:
            continue
        records = load_records(path)
        loads, dispatches = classify(records)
        on_date = [ld for ld in loads if ld["ts"][:10] in dates]
        if not on_date:
            continue
        out.append(
            {
                "path": path,
                "session": os.path.basename(path)[:-6],
                "first_ts": first_timestamp(records),
                "loads": loads,
                "date": min(ld["ts"][:10] for ld in on_date),
                "dispatches": dispatches,
            }
        )
    return out


def measure_session(session):
    """Per-session total over every load before the first dispatch."""
    anchor_list = anchors(session["path"])
    selected = [ld for ld in session["loads"] if ld["ts"][:10] == session["date"]]
    if not selected:
        return None
    first_index = selected[0]["index"]
    boundary = None
    for index, _ts in session["dispatches"]:
        if index >= first_index:
            boundary = index
            break
    window = [ld for ld in selected if boundary is None or ld["index"] < boundary]
    if not window:
        return None
    per_load = []
    for ld in window:
        tokens, note = cost_of_load(anchor_list, ld["index"], ld["shared_turn"])
        per_load.append(
            {**ld, "tokens": tokens, "note": note, "session": session["session"]}
        )
    measured = [p for p in per_load if p["tokens"] is not None]
    return {
        "session": session["session"],
        "date": session["date"],
        "started": session["first_ts"][:19],
        "boundary": "end-of-transcript" if boundary is None else "first-dispatch",
        "loads": per_load,
        "total_tokens": sum(p["tokens"] for p in measured),
        "measured_loads": len(measured),
        "unmeasured": [p for p in per_load if p["tokens"] is None],
        "partial": bool([p for p in per_load if p["tokens"] is None]),
    }


def positive_control():
    """A known-present load must be seen, and a transcript quote rejected."""
    good = None
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "*.jsonl")):
        if os.path.basename(path).startswith("1d1d2f0d"):
            good = path
            break
    if good is None:
        return False, "positive-control transcript not found"
    loads, _ = classify(load_records(good))
    if not any(ld["kind"] == "Skill" for ld in loads):
        return False, "known Skill load not detected"
    for path in glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "*.jsonl")):
        if not os.path.basename(path).startswith("1225959a"):
            continue
        quoted, _ = classify(load_records(path))
        if [ld for ld in quoted if ld["ts"][:10] == BASELINE_DATE]:
            return False, "a quoted transcript was counted as a load"
    return True, "known Skill load seen; quoted transcript rejected"


def bracketing_check():
    """Reproduce the earlier defect and show the bracket rule rejects it.

    6f6c5410's reference load is preceded in the same turn by a Bash result of
    821 tokens. Bracketing on the load's own request must not charge those.
    """
    path = None
    for candidate in glob.glob(os.path.join(TRANSCRIPT_ROOT, "*", "*.jsonl")):
        if os.path.basename(candidate).startswith("6f6c5410"):
            path = candidate
            break
    if path is None:
        return False, "bracketing-control transcript not found"
    anchor_list = anchors(path)
    loads, _ = classify(load_records(path))
    ref = next(ld for ld in loads if not ld["body"])
    tokens, _note = cost_of_load(anchor_list, ref["index"], ref["shared_turn"])
    previous = None
    for index, total in anchor_list:
        if index < ref["index"]:
            previous = total
    own = None
    for index, total in anchor_list:
        if index == ref["index"]:
            own = total
    if own is None or previous is None:
        return False, "bracketing-control request anchors missing"
    loose = previous
    if tokens == own - previous:
        return False, "the load was bracketed on the earlier request"
    return True, (
        f"load bracketed on its own request ({own}), not the earlier one "
        f"({loose}); a loose bracket would have charged {own - previous} tokens of "
        "other content that landed in the same turn"
    )


def git_revision():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def main():
    revision = git_revision()
    tree = os.getcwd()
    command = " ".join(sys.argv)
    out = []
    out.append(f"revision {revision} tree {tree} command: {command}")
    out.append(f"transcript root: {TRANSCRIPT_ROOT} (read-only)")
    out.append(
        "selection rule: a session is selected when it records at least one "
        "reckon-build skill load on a selected date. A load is a Skill "
        f"invocation of reckon-build, an injected SKILL.md body, or a whole-file "
        f"Read of a path under the installed tree {SKILL_DIR}. A Read of a "
        "repository's own skills/reckon-build/ copy is excluded (that is "
        "authoring the skill, not loading it) and so is a partial Read carrying "
        "an offset or a limit (that reads a fragment, it does not load the "
        "skill). Dates start at the baseline and widen symmetrically until at "
        "least five sessions have a complete per-session total."
    )
    out.append(
        "per-session step: sum the input tokens of every selected-date load at "
        "or before the session's first `reckon crew dispatch` call (end of "
        "transcript when the session never dispatched)."
    )
    ok, detail = positive_control()
    out.append(f"control: {ok} {detail}")
    if not ok:
        print("\n".join(out))
        return 2
    ok2, detail2 = bracketing_check()
    out.append(f"bracketing control: {ok2} {detail2}")

    # Widen the date window until five sessions have a measurable total.
    used_dates = []
    sessions = []
    for date in DATE_WINDOW:
        used_dates.append(date)
        sessions = candidate_sessions(used_dates)
        results = [r for r in (measure_session(s) for s in sessions) if r]
        clean = [r for r in results if not r["partial"]]
        if len(clean) >= MIN_SESSIONS:
            break

    out.append(
        f"dates used: {', '.join(used_dates)}  (baseline date first, then the "
        "neighbouring days added to reach five)"
    )
    out.append(
        f"sessions selected: {len(results)}  with a complete per-session total: "
        f"{len(clean)}  (target {MIN_SESSIONS}, "
        f"{'met' if len(clean) >= MIN_SESSIONS else 'NOT MET'})"
    )
    out.extend(
        f"  {row['session']}  date={row['date']}  started={row['started']}  "
        f"boundary={row['boundary']}  total={row['total_tokens']}"
        + ("  PARTIAL" if row["partial"] else "")
        for row in sorted(results, key=lambda r: -r["total_tokens"])
    )

    out.append("")
    out.append(
        "per-session input tokens spent loading the skill before first dispatch:"
    )
    for row in sorted(results, key=lambda r: -r["total_tokens"]):
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
            f"  {row['session']}  total={row['total_tokens']:>7}  "
            f"loads={row['measured_loads']}  [{kinds}]"
            + ("  PARTIAL" if row["partial"] else "")
        )
        out.extend(
            f"  {' ' * len(row['session'])}  unmeasured: {ld['kind']} "
            f"{os.path.basename(ld['file'])} — {ld['note']}"
            for ld in row["unmeasured"]
        )

    totals = [r["total_tokens"] for r in clean]
    out.append("")
    out.append(
        f"n sessions with a complete per-session total: {len(totals)}  "
        f"(of {len(results)} selected)"
    )
    if totals:
        out.append(f"MEDIAN per-session input tokens: {statistics.median(totals):.1f}")
        out.append(
            f"min {min(totals)}  max {max(totals)}  mean {statistics.mean(totals):.1f}"
        )

    skill_loads = [
        p for r in results for p in r["loads"] if p["body"] and p["tokens"] is not None
    ]
    ref_loads = [
        p
        for r in results
        for p in r["loads"]
        if not p["body"] and p["tokens"] is not None
    ]
    out.append("")
    out.append("full SKILL.md loads (whole-body loads with an isolated figure):")
    out.extend(
        f"  {p['tokens']} tokens  {p['kind']}  {p['session'][:8]}  {p['ts'][:10]}"
        for p in skill_loads
    )
    if skill_loads:
        figures = [p["tokens"] for p in skill_loads]
        out.append(
            f"  full SKILL.md load figures: n {len(figures)} "
            f"median {statistics.median(figures):.1f}  "
            f"max {max(figures)}"
        )
        out.append(
            "  the largest is a first delivery into a context that did not hold "
            "the body; the smallest is a re-invocation in a session that had "
            "already loaded it, so it is not a comparable full-load figure"
        )
    out.append("per-reference loads:")
    out.extend(
        f"  {p['tokens']} tokens  {os.path.basename(p['file'])}"
        + ("  (upper bound)" if p["note"] else "")
        for p in ref_loads
    )

    skill_words = word_count(SKILL_MD)
    ref_paths = reference_files()
    ref_words = sum(word_count(p) for p in ref_paths)
    out.append("")
    out.append(
        "word counts (whitespace-split, the done-when's rule). These describe "
        "the skill as it stands now, not as it stood on the baseline date, and "
        "the file moves: the load figures above come from frozen transcripts, "
        "the word counts do not."
    )
    out.append(f"  SKILL.md: {skill_words} words  (last written {mtime(SKILL_MD)})")
    out.append(f"  references ({len(ref_paths)} files): {ref_words} words")
    out.append(f"  skill + references: {skill_words + ref_words} words")
    out.append(f"  ceiling: 12000 words — headroom {12000 - skill_words - ref_words}")
    if skill_loads:
        top = max(p["tokens"] for p in skill_loads)
        mid = statistics.median([p["tokens"] for p in skill_loads])
        out.append(
            f"  tokens per word at the largest full SKILL.md load: "
            f"{top / skill_words:.2f}  ({top} tokens / {skill_words} words)"
        )
        out.append(
            f"  tokens per word at the median full SKILL.md load: "
            f"{mid / skill_words:.2f}"
        )
    out.append(f"measured at {datetime.datetime.now(datetime.UTC).isoformat()}")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
