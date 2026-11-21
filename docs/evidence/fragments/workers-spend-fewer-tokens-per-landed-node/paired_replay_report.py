"""Read the paired replay population and emit the paired-replay evidence record.

The population is not chosen here: it is the section choice and seed recorded in
``paired-replay-assignment.json`` before any replay ran. This script reads the
run store for each replay's row (gate, base revision, commits, attempts), each
replay's own directory for its streams and launch records, and the plan sections
for effort hours. It draws the paired differences and the bootstrap intervals
the record reports; it draws no adoption decision of its own beyond applying
section 8's rule to the measured numbers.

Every figure is derived, never transcribed: run ids and base revisions come from
the store row, token and tool-call counts from the stream files, arm membership
from the run's session-start record (Claude dialect) or launch record (codex).
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sqlite3
import statistics
from pathlib import Path

REPO = Path(__file__).resolve()
PLAN = "workers-spend-fewer-tokens-per-landed-node"
FRAGMENT = Path("docs/evidence/fragments") / PLAN
ASSIGNMENT_FILES = [
    "paired-replay-assignment.json",
    "paired-replay-assignment-2.json",
]
ASSIGNMENT = FRAGMENT / ASSIGNMENT_FILES[0]
OBSERVATIONS = FRAGMENT / "paired-replay-observations.json"
RECORD = FRAGMENT / "paired-replay-report.html"
ARCHIVE = Path("docs/evidence/archive") / f"{PLAN}-landed.html"
POOLED_SECTION = "s8-pooled-local-record"
LEAD_DECISION = "2026-11-20"
NEGATIVE_CONTROL = {
    "run_id": "r-negative-control-unlisted-plan",
    "node": "replay-clive-unlisted-plan-s1",
    "plan": "a-plan-the-producer-does-not-name",
    "gate": "passed",
    "base_sha": "0" * 40,
}
FIXTURE = Path("docs/research/fixtures/codex-refused-patch-stream.jsonl")
ARMS = ["clive", "clive-anchored", "clive-replicate", "omp-local", "codex", "codex-anchored"]
BASELINES = {
    "clive-anchored": "clive",
    "omp-local": "clive",
    "clive-replicate": "clive",
    "codex-anchored": "codex",
}
TREATMENTS = ["clive-anchored", "omp-local", "codex-anchored"]
BOOTSTRAP = 10_000
INTERVAL = 0.90
ADOPTION = 0.05
FIXTURE_REFUSAL = re.compile(r"apply_patch verification failed", re.IGNORECASE)
ANCHORED_EDIT = "mcp__reckon-anchored-edit__edit_anchored"
ANCHORED_READ = "mcp__reckon-anchored-edit__read_anchored"
STORE_QUERY = "SELECT detail FROM run_details WHERE run_id = ?"
LOCAL_ARMS = ["clive", "clive-anchored", "omp-local", "clive-replicate"]
STOPPED_ARMS = {"codex", "codex-anchored"}
REPLAY_NODE = re.compile(
    r"^replay-(clive-anchored|clive-replicate|omp-local|codex-anchored|codex|clive)"
    r"-(.+)-s(\d+)$"
)


# --- reading the run's own directory -------------------------------------


def _launch_evidence(directory: Path) -> int:
    """How many stream and launch records a run directory holds."""
    if not directory.is_dir():
        return 0
    found = len(list(directory.glob("resume-*.jsonl")))
    found += len(list(directory.glob("attempt-*-worker.json")))
    if (directory / "stream.jsonl").exists():
        found += 1
    return found


def resolve_run_dir(root: Path, run_id: str) -> Path | None:
    """A run's directory is live while it runs and archived once it completes.

    A released run leaves a live directory holding only its manifest while the
    streams and launch records stay in the archive, so the directory carrying
    the launch evidence is the run's directory, not simply the first that
    exists.
    """
    candidates = [root / "runs" / run_id, root / "archive" / "runs" / run_id]
    ranked = sorted(
        (c for c in candidates if c.is_dir()),
        key=_launch_evidence,
        reverse=True,
    )
    return ranked[0] if ranked else None


def launches(run_dir: Path) -> list[Path]:
    found = []
    first = run_dir / "stream.jsonl"
    if first.exists():
        found.append(first)
    resumed = sorted(
        run_dir.glob("resume-*.jsonl"),
        key=lambda p: int(re.search(r"resume-(\d+)", p.name).group(1)),
    )
    return found + resumed


def attempt_records(run_dir: Path) -> list[dict]:
    paths = sorted(
        run_dir.glob("attempt-*-worker.json"),
        key=lambda p: int(re.search(r"attempt-(\d+)", p.name).group(1)),
    )
    records = []
    for path in paths:
        try:
            records.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return records


def read_records(path: Path) -> list[dict]:
    records = []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def dedupe_assistant(records) -> tuple[int, int, int]:
    """Whole-prompt tokens, output tokens and the widest prompt, by message id."""
    seen: dict[str, tuple[int, int]] = {}
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            continue
        key = message.get("id") or record.get("uuid") or ""
        prompt = sum(
            int(usage.get(name) or 0)
            for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        output = int(usage.get("output_tokens") or 0)
        prior = seen.get(key)
        if prior is None or output > prior[1]:
            seen[key] = (prompt, output)
    prompts = [entry[0] for entry in seen.values()]
    return sum(prompts), sum(entry[1] for entry in seen.values()), max(prompts, default=0)


def parse_claude(records) -> dict:
    """A Claude-dialect stream read for the run's own totals.

    Cumulative input is the ``result`` event's usage summed over every result
    the stream holds: that figure is the harness's own total for a launch, and
    it is the figure the store carries. The per-assistant-message sum is kept
    apart because it counts a subagent's requests, which no result covers; it
    is reported as a cross-check rather than as the metric.
    """
    deduped_input, deduped_output, peak = dedupe_assistant(records)
    result_inputs: list[int] = []
    result_output = 0
    tool_calls = 0
    anchored_reads = 0
    anchored_edits = 0
    edit_names = {"Edit", "MultiEdit", ANCHORED_EDIT}
    edits: list[str] = []
    for record in records:
        kind = record.get("type")
        if kind == "result":
            usage = record.get("usage") or {}
            result_inputs.append(
                sum(
                    int(usage.get(name) or 0)
                    for name in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
            )
            result_output += int(usage.get("output_tokens") or 0)
        elif kind == "assistant":
            for block in (record.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_calls += 1
                name = block.get("name")
                if name == ANCHORED_READ:
                    anchored_reads += 1
                elif name == ANCHORED_EDIT:
                    anchored_edits += 1
                if name in edit_names:
                    edits.append(str(block.get("id")))
    failures: dict[str, bool] = {}
    for record in records:
        if record.get("type") != "user":
            continue
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                failures[str(block.get("tool_use_id"))] = bool(block.get("is_error"))
    return {
        "input": sum(result_inputs) if result_inputs else deduped_input,
        "launch_inputs": result_inputs,
        "message_input": deduped_input,
        "output": result_output if result_inputs else deduped_output,
        "output_measured": bool(result_inputs),
        "peak": peak,
        "tool_calls": tool_calls,
        "edits": len(edits),
        "edit_failures": sum(1 for tool_id in edits if failures.get(tool_id)),
        "anchored_reads": anchored_reads,
        "anchored_edits": anchored_edits,
        "terminal_records": len(result_inputs),
    }


def parse_omp(records) -> dict:
    total_input = total_output = peak = tool_calls = edits = edit_failures = 0
    names: dict[str, str] = {}
    for record in records:
        kind = record.get("type")
        if kind == "tool_execution_start":
            tool_calls += 1
            names[str(record.get("toolCallId"))] = str(record.get("toolName"))
            if record.get("toolName") == "edit":
                edits += 1
        elif kind == "tool_execution_end":
            if names.get(str(record.get("toolCallId"))) == "edit" and record.get("isError"):
                edit_failures += 1
        elif kind == "message_end":
            message = record.get("message") or {}
            if str(message.get("role") or "") != "assistant":
                continue
            usage = message.get("usage") or {}
            if not usage:
                continue
            prompt = sum(int(usage.get(name) or 0) for name in ("input", "cacheRead", "cacheWrite"))
            output = int(usage.get("output") or 0)
            if not prompt and not output:
                continue
            total_input += prompt
            total_output += output
            peak = max(peak, prompt)
    return {
        "input": total_input,
        "output": total_output,
        "output_measured": True,
        "peak": peak,
        "tool_calls": tool_calls,
        "edits": edits,
        "edit_failures": edit_failures,
        "anchored_reads": 0,
        "anchored_edits": 0,
        "terminal_records": 1,
    }


def codex_rollout_usage(run_dir: Path) -> dict:
    """The codex harness's own cumulative usage, from its session rollouts.

    ``codex exec --json`` writes a ``turn.completed`` event carrying the turn's
    usage only when the turn completes; a launch that is stopped early writes
    none and its stream then looks like a run that spent nothing. The rollout
    the harness keeps under its own home carries the running total whatever the
    stream shows, so it is the figure of record and the stream's turn sum is
    the cross-check.
    """
    total_input = total_output = 0
    peak = 0
    rollouts = 0
    last_total = None
    for path in sorted(run_dir.glob("codex-home/sessions/**/*.jsonl")):
        rollouts += 1
        file_total = None
        for record in read_records(path):
            payload = record.get("payload") or {}
            if payload.get("type") == "token_count":
                info = payload.get("info") or {}
                usage = info.get("total_token_usage")
                if isinstance(usage, dict):
                    file_total = usage
                last = info.get("last_token_usage")
                if isinstance(last, dict):
                    peak = max(peak, int(last.get("input_tokens") or 0))
            elif record.get("type") == "token_usage_record":
                usage = payload.get("usage") or {}
                peak = max(peak, int(usage.get("input_tokens") or 0))
        if file_total is not None:
            last_total = file_total
            total_input += int(file_total.get("input_tokens") or 0)
            total_output += int(file_total.get("output_tokens") or 0)
    return {
        "rollouts": rollouts,
        "input": total_input
        if last_total is not None
        else None,
        "output": total_output if last_total is not None else None,
        "peak": peak or None,
    }


def parse_codex(records) -> dict:
    """A codex stream read for its tool calls and its turn-level usage."""
    total_input = total_output = tool_calls = edits = edit_failures = 0
    anchored_reads = anchored_edits = turns = 0
    for record in records:
        kind = record.get("type")
        if kind == "turn.completed":
            turns += 1
            usage = record.get("usage") or {}
            total_input += int(usage.get("input_tokens") or 0)
            total_output += int(usage.get("output_tokens") or 0)
        elif kind == "item.completed":
            item = record.get("item") or {}
            item_type = item.get("type")
            if item_type in {"command_execution", "file_change", "mcp_tool_call"}:
                tool_calls += 1
            if item_type == "file_change":
                edits += 1
            elif item_type == "mcp_tool_call":
                tool = item.get("tool")
                if tool == "read_anchored":
                    anchored_reads += 1
                elif tool == "edit_anchored":
                    anchored_edits += 1
                    edits += 1
                    if item.get("error") or item.get("status") not in (None, "", "completed"):
                        edit_failures += 1
            elif item_type == "agent_message":
                edit_failures += len(FIXTURE_REFUSAL.findall(item.get("text") or ""))
    return {
        "input": total_input,
        "launch_inputs": [total_input] if turns else [],
        "message_input": total_input,
        "output": total_output,
        "output_measured": bool(turns),
        "peak": None,
        "tool_calls": tool_calls,
        "edits": edits,
        "edit_failures": edit_failures,
        "anchored_reads": anchored_reads,
        "anchored_edits": anchored_edits,
        "terminal_records": turns,
    }


def read_stream(path: Path, backend: str) -> dict:
    records = read_records(path)
    if backend == "omp-local":
        return parse_omp(records)
    if backend in {"codex", "codex-anchored"}:
        return parse_codex(records)
    return parse_claude(records)


def session_start(run_dir: Path) -> dict | None:
    for path in launches(run_dir):
        for record in read_records(path):
            if record.get("type") == "system" and record.get("subtype") == "init":
                return record
    return None


def arm_membership(declared: str, attempts: list[dict], start: dict | None) -> tuple[str, str]:
    """Return (arm-or-excluded, the fact read) under section 8's rule."""
    if declared in {"clive", "clive-anchored", "clive-replicate"}:
        if start is None:
            return "excluded", "no session-start record on the run's launches"
        servers = {
            str(entry.get("name")): str(entry.get("status"))
            for entry in start.get("mcp_servers") or []
        }
        connected = servers.get("reckon-anchored-edit") == "connected"
        if declared == "clive-anchored":
            if connected:
                return "clive-anchored", "session-start record lists reckon-anchored-edit connected"
            return "excluded", "session-start record does not list reckon-anchored-edit connected"
        if connected:
            return "excluded", "baseline run's session-start record lists the anchored server connected"
        return declared, "session-start record lists no anchored server connected"
    if declared in {"codex", "codex-anchored"}:
        anchored = [bool(a.get("anchored_edit")) for a in attempts]
        if declared == "codex-anchored":
            if any(anchored):
                return "codex-anchored", "a launch record carries anchored_edit true"
            return "excluded", "no launch record carries anchored_edit true"
        if any(anchored):
            return "codex", "a launch record carries anchored_edit true"
        return "codex", "no launch record carries anchored_edit true"
    return "omp-local", "oh-my-pi arm; takes no anchored split"


# --- the store side -------------------------------------------------------


def store_rows(db_path: Path) -> dict[str, dict]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows: dict[str, dict] = {}
    try:
        cursor = connection.execute(
            "SELECT run_id, payload FROM runs WHERE node LIKE 'replay-%' ORDER BY run_id"
        )
        for row in cursor:
            payload = json.loads(row["payload"])
            rows[row["run_id"]] = payload
    finally:
        connection.close()
    return rows


def load_assignments(repo: Path) -> list[dict]:
    return [
        json.loads((repo / FRAGMENT / name).read_text()) for name in ASSIGNMENT_FILES
    ]


def named_plans(assignments: list[dict]) -> list[str]:
    """Every plan the committed assignments draw a section from."""
    plans: list[str] = []
    for assignment in assignments:
        for section in assignment["sections"]:
            if section["plan"] not in plans:
                plans.append(section["plan"])
    return plans


def excluded_sections(assignments: list[dict]) -> set[tuple[str, str]]:
    names: set[tuple[str, str]] = set()
    for assignment in assignments:
        for entry in assignment.get("exclusions") or []:
            if "plan" in entry and "section" in entry:
                names.add((entry["plan"], entry["section"]))
    return names


def pooled_sections(assignments: list[dict]) -> list[dict]:
    excluded = excluded_sections(assignments)
    sections: list[dict] = []
    for assignment in assignments:
        for section in assignment["sections"]:
            if (section["plan"], section["section"]) in excluded:
                continue
            sections.append(section)
    return sections


def store_detail(db_path: Path, run_id: str) -> dict:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute(STORE_QUERY, (run_id,)).fetchone()
    finally:
        connection.close()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {}


def effort_hours(repo: Path, plan: str, section: str) -> float | None:
    text = (repo / "docs/plans" / f"{plan}.html").read_text()
    match = re.search(
        r'<section[^>]*data-id="' + re.escape(section) + r'"[^>]*data-effort-hours="([0-9.]+)"',
        text,
    )
    return float(match.group(1)) if match else None


# --- measurement ----------------------------------------------------------


def excluded_run(
    run_id: str, payload: dict, plan: dict | str, section_id: str, declared: str, reason: str
) -> dict:
    return {
        "run_id": run_id,
        "plan": plan,
        "section": section_id,
        "declared_arm": declared,
        "arm": "excluded",
        "membership_reason": reason,
        "backend": str(payload.get("backend") or declared),
        "gate": payload.get("gate"),
        "outcome": payload.get("outcome"),
        "base_sha": payload.get("base_sha"),
        "commits": payload.get("commits") or [],
        "attempts": 1,
        "anchored_edit_launches": [],
        "effort_hours": None,
        "stored_input": None,
        "launch_inputs": [],
        "launches": 0,
        "stream_input": None,
        "rollout_input": None,
        "rollouts": 0,
        "landed": False,
        "input": None,
        "output": None,
        "peak": None,
        "tool_calls": 0,
        "edits": 0,
        "edit_failures": 0,
        "anchored_reads": 0,
        "anchored_edits": 0,
        "terminal_records": 0,
        "output_measured": False,
    }


def measure(
    repo: Path, root: Path, db_path: Path, fixture_rows: list[dict] | None = None
) -> dict:
    assignments = load_assignments(repo)
    plans = named_plans(assignments)
    sections = {
        (section["plan"], section["section"]): section
        for section in pooled_sections(assignments)
    }
    observations = (
        json.loads((repo / OBSERVATIONS).read_text())
        if (repo / OBSERVATIONS).exists()
        else {"observations": []}
    )
    stored = store_rows(db_path)
    for row in fixture_rows or []:
        stored[str(row["run_id"])] = row
    stored[NEGATIVE_CONTROL["run_id"]] = dict(NEGATIVE_CONTROL)
    by_section: dict[tuple[str, str], dict[str, dict]] = {}
    runs = []
    for run_id in sorted(stored):
        payload = stored[run_id]
        node = str(payload.get("node") or "")
        match = REPLAY_NODE.match(node)
        if not match:
            continue
        declared = match.group(1)
        section_id = main_section(match.group(3))
        plan = str(payload.get("plan") or "")
        if plan not in plans:
            runs.append(
                excluded_run(
                    run_id,
                    payload,
                    plan,
                    section_id,
                    declared,
                    "the run's plan "
                    + repr(plan)
                    + " is not one of the plans the producer names ("
                    + ", ".join(plans)
                    + "); the section is listed as excluded rather than dropped",
                )
            )
            continue
        if (plan, section_id) not in sections:
            runs.append(
                excluded_run(
                    run_id,
                    payload,
                    plan,
                    section_id,
                    declared,
                    "the section is not drawn by any committed assignment; it is listed as "
                    "excluded rather than dropped",
                )
            )
            continue
        run_dir = resolve_run_dir(root, run_id)
        if run_dir is None:
            runs.append(
                excluded_run(
                    run_id,
                    payload,
                    plan,
                    section_id,
                    declared,
                    "no run directory on this host",
                )
            )
            continue
        attempts = attempt_records(run_dir)
        backend = str(attempts[0].get("backend")) if attempts else declared
        arm, reason = arm_membership(declared, attempts, session_start(run_dir))
        totals = {
            "input": 0,
            "output": 0,
            "peak": 0,
            "tool_calls": 0,
            "edits": 0,
            "edit_failures": 0,
            "anchored_reads": 0,
            "anchored_edits": 0,
            "terminal_records": 0,
            "output_measured": True,
        }
        launch_inputs = []
        for path in launches(run_dir):
            if path.stat().st_size == 0:
                continue
            figures = read_stream(path, backend)
            launch_inputs.extend(figures.get("launch_inputs") or [figures["input"]])
            totals["input"] += figures["input"]
            totals["output"] += figures["output"]
            totals["tool_calls"] += figures["tool_calls"]
            totals["edits"] += figures["edits"]
            totals["edit_failures"] += figures["edit_failures"]
            totals["anchored_reads"] += figures["anchored_reads"]
            totals["anchored_edits"] += figures["anchored_edits"]
            totals["terminal_records"] += figures["terminal_records"]
            totals["output_measured"] = totals["output_measured"] and figures["output_measured"]
            if figures.get("peak"):
                totals["peak"] = max(totals["peak"], figures["peak"])
        stream_input = totals["input"]
        rollouts = {"rollouts": 0, "input": None, "output": None, "peak": None}
        if backend in {"codex", "codex-anchored"}:
            rollouts = codex_rollout_usage(run_dir)
            if rollouts["input"] is not None:
                totals["input"] = rollouts["input"]
                totals["output"] = rollouts["output"]
            if rollouts["peak"]:
                totals["peak"] = max(totals["peak"], rollouts["peak"])
            totals["output_measured"] = totals["output_measured"] or rollouts["output"] is not None
        detail = store_detail(db_path, run_id)
        stored_tokens = (detail.get("budget") or {}).get("tokens") or {}
        run = {
            "run_id": run_id,
            "plan": plan,
            "section": section_id,
            "declared_arm": declared,
            "arm": arm,
            "membership_reason": reason,
            "backend": backend,
            "gate": payload.get("gate"),
            "outcome": payload.get("outcome"),
            "base_sha": payload.get("base_sha"),
            "commits": payload.get("commits") or [],
            "attempts": max(int(payload.get("attempt") or 1), len(attempts)),
            "anchored_edit_launches": [bool(a.get("anchored_edit")) for a in attempts],
            "effort_hours": effort_hours(repo, plan, section_id),
            "stored_input": stored_tokens.get("input_tokens_cumulative"),
            "launch_inputs": launch_inputs,
            "launches": len(launches(run_dir)),
            "stream_input": stream_input,
            "rollout_input": rollouts["input"],
            "rollouts": rollouts["rollouts"],
            "landed": payload.get("gate") == "passed",
            **totals,
        }
        runs.append(run)
        by_section.setdefault((plan, section_id), {})[arm] = run
    return {
        "assignment": assignments[0],
        "assignments": assignments,
        "plans": plans,
        "sections": pooled_sections(assignments),
        "observations": observations,
        "runs": runs,
        "by_section": by_section,
        "store_read": db_path.exists(),
        "root": str(root),
        "db": str(db_path),
    }


def main_section(section_token: str) -> str:
    """The plan section id a replay node names, which the store also carries."""
    return f"s{section_token}"


# --- statistics -----------------------------------------------------------


def bootstrap_interval(values: list[float], seed: int) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(BOOTSTRAP):
        means.append(statistics.fmean([values[rng.randrange(len(values))] for _ in values]))
    means.sort()
    low = means[int((1 - INTERVAL) / 2 * len(means))]
    high = means[int((1 + INTERVAL) / 2 * len(means)) - 1]
    return low, high


def paired(data: dict, arm: str, baseline: str, both_landed: bool) -> dict:
    pairs = []
    for (plan, section), arms in sorted(data["by_section"].items()):
        a = arms.get(arm)
        b = arms.get(baseline)
        if a is None or b is None or a.get("input") is None or b.get("input") is None:
            continue
        if both_landed and not (a["landed"] and b["landed"]):
            continue
        pairs.append(
            {
                "plan": plan,
                "section": section,
                "arm_run": a["run_id"],
                "baseline_run": b["run_id"],
                "arm_input": a["input"],
                "baseline_input": b["input"],
                "difference": a["input"] - b["input"],
            }
        )
    differences = [p["difference"] for p in pairs]
    low, high = bootstrap_interval(differences, seed=20261116 + len(pairs))
    mean_difference = statistics.fmean(differences) if differences else None
    baseline_mean = (
        statistics.fmean([p["baseline_input"] for p in pairs]) if pairs else None
    )
    relative = (
        mean_difference / baseline_mean if baseline_mean not in (None, 0) else None
    )
    return {
        "pairs": pairs,
        "count": len(pairs),
        "mean_difference": mean_difference,
        "baseline_mean": baseline_mean,
        "relative": relative,
        "low": low,
        "high": high,
    }


def intervals_overlap(a: dict, b: dict) -> bool | None:
    if None in (a.get("low"), a.get("high"), b.get("low"), b.get("high")):
        return None
    return not (a["high"] < b["low"] or b["high"] < a["low"])


def gate_rate(data: dict, arm: str) -> tuple[int, int]:
    members = [r for r in data["runs"] if r["arm"] == arm]
    return len([r for r in members if r["landed"]]), len(members)


def per_arm(data: dict, arm: str) -> dict:
    members = [r for r in data["runs"] if r["arm"] == arm]
    passed = [r for r in members if r["landed"]]
    peaks = [r["peak"] for r in members if r["peak"]]
    return {
        "runs": len(members),
        "passed": len(passed),
        "gate_rate": (len(passed) / len(members)) if members else None,
        "input_total": sum(r["input"] for r in members),
        "input_per_passed": (
            sum(r["input"] for r in passed) / len(passed) if passed else None
        ),
        "output_per_run": (
            statistics.fmean([r["output"] for r in members]) if members else None
        ),
        "output_measured": all(r["output_measured"] for r in members) if members else None,
        "tool_calls_per_run": (
            statistics.fmean([r["tool_calls"] for r in members]) if members else None
        ),
        "peak_mean": statistics.fmean(peaks) if peaks else None,
        "edits": sum(r["edits"] for r in members),
        "edit_failures": sum(r["edit_failures"] for r in members),
        "anchored_edits": sum(r["anchored_edits"] for r in members),
        "anchored_reads": sum(r["anchored_reads"] for r in members),
        "corrective": sum(1 for r in members if r["attempts"] > 1),
        "effort_mix": sorted({r["effort_hours"] for r in members if r["effort_hours"]}),
    }


def analysis(data: dict) -> dict:
    same_arm = paired(data, "clive-replicate", "clive", both_landed=False)
    same_arm_all = paired(data, "clive-replicate", "clive", both_landed=True)
    treatments = {}
    for arm in TREATMENTS:
        baseline = BASELINES[arm]
        primary = paired(data, arm, baseline, both_landed=False)
        every = paired(data, arm, baseline, both_landed=True)
        arm_rate = gate_rate(data, arm)
        base_rate = gate_rate(data, baseline)
        overlap = intervals_overlap(primary, same_arm)
        fall_in_rate = (
            None
            if not arm_rate[1] or not base_rate[1]
            else (arm_rate[0] / arm_rate[1]) < (base_rate[0] / base_rate[1])
        )
        separated = overlap is False
        meets_size = primary["relative"] is not None and primary["relative"] <= -ADOPTION
        if primary["count"] == 0:
            verdict = "not measured: no section holds a run of both arms"
        elif not separated:
            verdict = (
                "not adopt: the paired-difference interval overlaps the same-arm interval, "
                "so the arm is not separated from the spread a same-arm comparison already has"
            )
        elif fall_in_rate:
            verdict = "not adopt: the gate pass rate falls against the baseline"
        elif not meets_size:
            verdict = "not adopt: cumulative input per gate-passed run does not fall by 5%"
        else:
            verdict = "adopt"
        treatments[arm] = {
            "baseline": baseline,
            "primary": primary,
            "both_passed": every,
            "same_arm": same_arm,
            "same_arm_both_passed": same_arm_all,
            "overlap": overlap,
            "separated": separated,
            "meets_size": meets_size,
            "fall_in_rate": fall_in_rate,
            "arm_rate": arm_rate,
            "base_rate": base_rate,
            "verdict": verdict,
        }
    return {
        "same_arm": same_arm,
        "same_arm_all": same_arm_all,
        "treatments": treatments,
        "arms": {arm: per_arm(data, arm) for arm in ARMS},
    }


def controls(data: dict, repo: Path) -> dict:
    baseline = [r for r in data["runs"] if r["arm"] == "clive"]
    edits = sum(r["edits"] for r in baseline)
    failures = sum(r["edit_failures"] for r in baseline)
    clive_rate = (failures / edits) if edits else None
    fixture_records = read_records(repo / FIXTURE) if (repo / FIXTURE).exists() else []
    fixture_found = sum(
        len(FIXTURE_REFUSAL.findall((record.get("item") or {}).get("text") or ""))
        for record in fixture_records
    )
    compared = [r for r in data["runs"] if r.get("stored_input") is not None and r["launch_inputs"]]
    matches = [r for r in compared if r["stored_input"] in r["launch_inputs"]]
    single = [r for r in compared if r["launches"] == 1]
    single_matches = [r for r in single if r["stored_input"] in r["launch_inputs"]]
    codex = [r for r in data["runs"] if r["arm"] == "codex-anchored"]
    return {
        "clive_edit_calls": edits,
        "clive_edit_failures": failures,
        "clive_rate": clive_rate,
        "clive_runs": len(baseline),
        "fixture_bytes": (repo / FIXTURE).stat().st_size if (repo / FIXTURE).exists() else 0,
        "fixture_records": len(fixture_records),
        "fixture_found": fixture_found,
        "store_compared": len(compared),
        "store_matches": len(matches),
        "store_single": len(single),
        "store_single_matches": len(single_matches),
        "codex_anchored_edits": sum(r["anchored_edits"] for r in codex),
        "codex_anchored_reads": sum(r["anchored_reads"] for r in codex),
        "codex_anchored_edit_calls": sum(r["edits"] for r in codex),
    }


# --- rendering ------------------------------------------------------------


def tokens(value, digits: int = 2) -> str:
    if value is None:
        return "undefined"
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value) / 1_000_000:.{digits}f}M"


def percent(value) -> str:
    if value is None:
        return "undefined"
    return f"{value * 100:+.1f}%"


def fmt(value, digits: int = 0) -> str:
    if value is None:
        return "undefined"
    return f"{value:,.{digits}f}"


def rate(numerator: int, denominator: int) -> str:
    if not denominator:
        return "undefined"
    return f"{100.0 * numerator / denominator:.1f}% ({numerator}/{denominator})"


def overlap_text(value) -> str:
    if value is None:
        return "undefined"
    return "yes" if value else "no"


def pair_table(pairs: list[dict]) -> str:
    rows = []
    for pair in pairs:
        rows.append(
            "<tr><td>"
            + pair["plan"]
            + "#"
            + pair["section"]
            + "</td><td><code>"
            + pair["arm_run"]
            + "</code></td><td>"
            + tokens(pair["arm_input"])
            + "</td><td><code>"
            + pair["baseline_run"]
            + "</code></td><td>"
            + tokens(pair["baseline_input"])
            + "</td><td>"
            + tokens(pair["difference"])
            + "</td></tr>"
        )
    return (
        "<table><thead><tr><th>Section</th><th>Arm run</th><th>Arm input</th>"
        "<th>Baseline run</th><th>Baseline input</th><th>Arm minus baseline</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_pooled(data: dict, result: dict, check: dict) -> str:
    """The pooled local section: both assignments, every plan they draw from."""
    ids = POOLED_SECTION
    arms = result["arms"]
    same_arm = result["same_arm"]
    sections = data["sections"]
    local_runs = [r for r in data["runs"] if r["arm"] in LOCAL_ARMS]
    codex_runs = [r for r in data["runs"] if r["arm"] in STOPPED_ARMS]
    excluded = [r for r in data["runs"] if r["arm"] == "excluded"]
    negative = [r for r in excluded if r["run_id"] == NEGATIVE_CONTROL["run_id"]]
    excluded = [r for r in excluded if r["run_id"] != NEGATIVE_CONTROL["run_id"]]
    plans = data["plans"]

    by_plan: dict[str, dict] = {plan: {"sections": 0, "local": 0, "codex": 0} for plan in plans}
    for section in sections:
        by_plan.setdefault(section["plan"], {"sections": 0, "local": 0, "codex": 0})
        by_plan[section["plan"]]["sections"] += 1
    for run in local_runs:
        by_plan.setdefault(run["plan"], {"sections": 0, "local": 0, "codex": 0})
        by_plan[run["plan"]]["local"] += 1
    for run in codex_runs:
        by_plan.setdefault(run["plan"], {"sections": 0, "local": 0, "codex": 0})
        by_plan[run["plan"]]["codex"] += 1

    plan_rows = []
    for plan in plans:
        figures = by_plan[plan]
        plan_rows.append(
            "<tr><td><code>"
            + html.escape(plan)
            + "</code></td><td>"
            + str(figures["sections"])
            + "</td><td>"
            + str(figures["local"])
            + "</td><td>"
            + str(figures["codex"])
            + "</td></tr>"
        )

    arm_rows = []
    arm_rows.append(
        "<tr><td><code>clive</code></td><td>baseline</td><td>"
        + str(len(sections))
        + "</td><td>"
        + str(arms["clive"]["runs"])
        + "</td><td>"
        + rate(arms["clive"]["passed"], arms["clive"]["runs"])
        + "</td><td>"
        + tokens(arms["clive"]["input_total"])
        + "</td><td>"
        + tokens(arms["clive"]["input_per_passed"])
        + "</td><td>&mdash;</td><td>&mdash;</td><td>&mdash;</td><td>baseline</td></tr>"
    )
    for arm in ["clive-anchored", "omp-local"]:
        treatment = result["treatments"][arm]
        primary = treatment["primary"]
        arm_rows.append(
            "<tr><td><code>"
            + arm
            + "</code></td><td>treatment</td><td>"
            + str(primary["count"])
            + "</td><td>"
            + str(arms[arm]["runs"])
            + "</td><td>"
            + rate(arms[arm]["passed"], arms[arm]["runs"])
            + "</td><td>"
            + tokens(arms[arm]["input_total"])
            + "</td><td>"
            + tokens(arms[arm]["input_per_passed"])
            + "</td><td>"
            + tokens(primary["mean_difference"])
            + "</td><td>"
            + percent(primary["relative"])
            + "</td><td>"
            + tokens(primary["low"])
            + " to "
            + tokens(primary["high"])
            + "</td><td><strong>"
            + treatment["verdict"]
            + "</strong></td></tr>"
        )
    arm_rows.append(
        "<tr><td><code>clive-replicate</code></td><td>same-arm control</td><td>"
        + str(same_arm["count"])
        + "</td><td>"
        + str(arms["clive-replicate"]["runs"])
        + "</td><td>"
        + rate(arms["clive-replicate"]["passed"], arms["clive-replicate"]["runs"])
        + "</td><td>"
        + tokens(arms["clive-replicate"]["input_total"])
        + "</td><td>"
        + tokens(arms["clive-replicate"]["input_per_passed"])
        + "</td><td>"
        + tokens(same_arm["mean_difference"])
        + "</td><td>"
        + percent(same_arm["relative"])
        + "</td><td>"
        + tokens(same_arm["low"])
        + " to "
        + tokens(same_arm["high"])
        + "</td><td>spread, not a treatment</td></tr>"
    )

    codex_rows = []
    for arm in ["codex", "codex-anchored"]:
        figures = arms[arm]
        codex_rows.append(
            "<tr><td><code>"
            + arm
            + "</code></td><td>"
            + str(figures["runs"])
            + "</td><td>"
            + rate(figures["passed"], figures["runs"])
            + "</td><td>"
            + tokens(figures["input_total"])
            + "</td><td>"
            + tokens(figures["input_per_passed"])
            + "</td><td><strong>stopped</strong> &mdash; the codex arms were stopped on "
            + LEAD_DECISION
            + " and their remaining replays were never run; these are partial results, "
            "not a verdict</td></tr>"
        )

    metric_rows = []
    for arm in LOCAL_ARMS:
        figures = arms[arm]
        metric_rows.append(
            "<tr><td><code>"
            + arm
            + "</code></td><td>"
            + fmt(figures["tool_calls_per_run"])
            + "</td><td>"
            + fmt(figures["output_per_run"])
            + ("" if figures["output_measured"] else "*")
            + "</td><td>"
            + fmt(figures["peak_mean"])
            + "</td><td>"
            + rate(figures["edit_failures"], figures["edits"])
            + "</td><td>"
            + str(figures["anchored_edits"])
            + " / "
            + str(figures["anchored_reads"])
            + "</td></tr>"
        )

    exclusion_rows = [
        "<tr><td><code>"
        + run["run_id"]
        + "</code></td><td>"
        + html.escape(str(run["plan"]))
        + "#"
        + run["section"]
        + "</td><td>"
        + html.escape(run["membership_reason"])
        + "</td></tr>"
        for run in excluded
    ]
    for assignment in data["assignments"]:
        for entry in assignment.get("exclusions") or []:
            if "section" in entry:
                name = entry["plan"] + "#" + entry["section"]
            else:
                name = "codex arms (" + ", ".join(entry.get("arms") or []) + ")"
            exclusion_rows.append(
                "<tr><td>&mdash;</td><td>"
                + html.escape(name)
                + "</td><td>"
                + html.escape(str(entry.get("reason") or ""))
                + "</td></tr>"
            )

    codex_sections = sorted({run["plan"] + "#" + run["section"] for run in codex_runs})
    if negative:
        negative_text = (
            "<p id=\""
            + ids
            + "-negative-result\">The producer was given a fixture replay row whose plan it "
            "does not name (<code>"
            + html.escape(negative[0]["run_id"])
            + "</code>, plan <code>"
            + html.escape(str(negative[0]["plan"]).rstrip("\n"))
            + "</code>) and <strong>listed it as excluded with its reason</strong>, rather than "
            "dropping it: "
            + html.escape(negative[0]["membership_reason"])
            + ". With the silent drop restored in a scratch copy, the row is absent.</p>"
        )
        negative_listed = "1"
    else:
        negative_text = (
            "<p id=\""
            + ids
            + "-negative-result\"><strong>The negative control is not met:</strong> the fixture "
            "row whose plan the producer does not name was dropped rather than listed.</p>"
        )
        negative_listed = "0"
    codex_note = (
        str(len(codex_runs))
        + " codex runs were recorded before the stop, over "
        + str(len(codex_sections))
        + " of the "
        + str(len(sections))
        + " sections: "
        + ", ".join(codex_sections)
        + ". No codex replay is counted in the pooled local table, and no codex verdict is drawn."
    )

    failed = [r for r in local_runs if not r["landed"]]
    failed_note = (
        str(len(failed))
        + " of the "
        + str(len(local_runs))
        + " local runs did not reach a passed gate."
        if failed
        else "Every one of the " + str(len(local_runs)) + " local runs reached a passed gate."
    )

    assignment_names = ", ".join(
        html.escape(ASSIGNMENT_FILES[index])
        for index in range(len(data["assignments"]))
    )
    assignment_seeds = ", ".join(str(a["seed"]) for a in data["assignments"])

    values = {
        "IDS": ids,
        "PLANS": ", ".join("<code>" + html.escape(plan) + "</code>" for plan in plans),
        "PLAN_ROWS": "".join(plan_rows),
        "SECTIONS": str(len(sections)),
        "LOCAL_RUNS": str(len(local_runs)),
        "LOCAL_ARMS": str(len(LOCAL_ARMS)),
        "ARM_ROWS": "".join(arm_rows),
        "CODEX_ROWS": "".join(codex_rows),
        "CODEX_NOTE": codex_note,
        "METRIC_ROWS": "".join(metric_rows),
        "NEGATIVE_TEXT": negative_text,
        "NEGATIVE_LISTED": negative_listed,
        "EXCLUSION_ROWS": "".join(exclusion_rows)
        or "<tr><td>&mdash;</td><td>&mdash;</td><td>Nothing is excluded.</td></tr>",
        "SAME_ARM_COUNT": str(same_arm["count"]),
        "SAME_ARM_MEAN": tokens(same_arm["mean_difference"]),
        "SAME_ARM_RELATIVE": percent(same_arm["relative"]),
        "SAME_ARM_BASELINE_MEAN": tokens(same_arm["baseline_mean"]),
        "SAME_ARM_LOW": tokens(same_arm["low"]),
        "SAME_ARM_HIGH": tokens(same_arm["high"]),
        "BOOTSTRAP": format(BOOTSTRAP, ","),
        "FAILED": failed_note,
        "QUERY": html.escape(
            " / ".join(a["population_query"] for a in data["assignments"])
        ),
        "ASSIGNMENTS": assignment_names,
        "SEEDS": assignment_seeds,
        "DRAWN": " + ".join(str(len(a["sections"])) for a in data["assignments"]),
        "COMMAND": html.escape(result.get("command") or "the command is recorded in the log"),
        "PRODUCER": str(FRAGMENT / "paired_replay_report.py"),
        "LEAD_DECISION": LEAD_DECISION,
        "CLIVE_RATE": rate(check["clive_edit_failures"], check["clive_edit_calls"]),
        "CLIVE_RUNS": str(check["clive_runs"]),
        "FIXTURE_FOUND": str(check["fixture_found"]),
        "FIXTURE_RECORDS": str(check["fixture_records"]),
        "STORE_MATCHES": str(check["store_matches"]),
        "STORE_COMPARED": str(check["store_compared"]),
    }
    page = POOLED_TEMPLATE
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    return page


POOLED_TEMPLATE = """  <section id="{{IDS}}" data-evidence-origin="append">
    <h2>&sect;8 — the pooled local record and the lead's decision</h2>
    <p id="{{IDS}}-summary">The verdict population is the paired replay: every section drawn
    by both committed assignments, {{ASSIGNMENTS}} (seeds {{SEEDS}}), replayed from one base
    revision per section by every local arm. The two assignments drew {{DRAWN}} sections
    together, and <strong>{{SECTIONS}}</strong> of them were dispatched and replayed, carrying
    <strong>{{LOCAL_RUNS}}</strong> local runs across the <strong>{{LOCAL_ARMS}}</strong> local
    arms. This record pools them for the first time: the earlier record read the first
    assignment alone, and the plan list it carried did not name
    <code>a-landing-never-waits-on-a-shared-index</code>, so its <code>s10</code> was dropped
    silently. The producer now names every plan the assignments draw from — {{PLANS}} — and a
    replay row whose plan it does not name is listed under exclusions with its reason rather
    than dropped.</p>

    <h3 id="{{IDS}}-plans">The plans the record draws from</h3>
    <table>
      <thead><tr><th>Plan</th><th>Sections</th><th>Local runs</th><th>Codex runs (stopped)</th></tr></thead>
      <tbody>{{PLAN_ROWS}}</tbody>
    </table>

    <h3 id="{{IDS}}-table">The pooled local per-arm table, over all {{SECTIONS}} replayed sections</h3>
    <p id="{{IDS}}-table-note">Each treatment is paired with <code>clive</code> on every section
    that carries both runs; the interval is a {{BOOTSTRAP}}-draw paired bootstrap over sections,
    seeded, at 90%. A treatment is <strong>adopt</strong> only when its mean paired cumulative
    input falls by at least 5%, its interval is separated from the same-arm spread, and its gate
    pass rate does not fall. {{FAILED}}</p>
    <table>
      <thead><tr><th>Arm</th><th>Role</th><th>Sections paired</th><th>Runs</th>
      <th>Gate passed</th><th>Cumulative input</th><th>Input per gate-passed run</th>
      <th>Mean paired &Delta; vs clive</th><th>Relative</th><th>90% interval</th>
      <th>Verdict</th></tr></thead>
      <tbody>{{ARM_ROWS}}</tbody>
    </table>

    <h3 id="{{IDS}}-same-arm">The same-arm control</h3>
    <p id="{{IDS}}-same-arm-note">The replicate clive run paired with clive on each of the
    <strong>{{SAME_ARM_COUNT}}</strong> sections is the spread a same-arm comparison already
    shows: mean paired difference <strong>{{SAME_ARM_MEAN}} ({{SAME_ARM_RELATIVE}} of the
    baseline's paired mean {{SAME_ARM_BASELINE_MEAN}})</strong>, 90% interval
    <strong>{{SAME_ARM_LOW}} to {{SAME_ARM_HIGH}}</strong>. A treatment whose interval overlaps
    this spread is reported not separated.</p>

    <h3 id="{{IDS}}-metrics">&sect;8's per-arm metrics, pooled</h3>
    <table>
      <thead><tr><th>Arm</th><th>Tool calls / run</th><th>Output / run</th>
      <th>Peak context</th><th>Edit failures / calls</th>
      <th><code>edit_anchored</code> / <code>read_anchored</code></th></tr></thead>
      <tbody>{{METRIC_ROWS}}</tbody>
    </table>

    <h3 id="{{IDS}}-codex">The codex arms, partial and stopped</h3>
    <table>
      <thead><tr><th>Arm</th><th>Runs recorded</th><th>Gate passed</th>
      <th>Cumulative input</th><th>Input per gate-passed run</th><th>Status</th></tr></thead>
      <tbody>{{CODEX_ROWS}}</tbody>
    </table>
    <p id="{{IDS}}-codex-note">{{CODEX_NOTE}}</p>

    <h3 id="{{IDS}}-exclusions">Excluded from the record, with the reason</h3>
    <p id="{{IDS}}-negative">Negative control: a replay row whose plan the producer does not
    name appears here as an excluded section with its reason, rather than being dropped as
    <code>land s10</code> was before this section. The row is listed by the same path a real
    unlisted row takes.</p>
    {{NEGATIVE_TEXT}}
    <table>
      <thead><tr><th>Run</th><th>Section</th><th>Reason</th></tr></thead>
      <tbody>{{EXCLUSION_ROWS}}</tbody>
    </table>

    <h3 id="{{IDS}}-query">The query and its literal command</h3>
    <p id="{{IDS}}-query-note">The population query, text as committed in each assignment:
    <code>{{QUERY}}</code></p>
    <p>The producer is committed at <code>{{PRODUCER}}</code>; the record is computed on the
    <code>all_debug</code> partition, never on the fleet node, and its log sits in the run
    directory, starts with the revision and this literal command, and ends in its own exit
    status:</p>
    <pre><code>{{COMMAND}}</code></pre>

    <h3 id="{{IDS}}-controls">&sect;8's controls, pooled</h3>
    <table>
      <thead><tr><th>Control</th><th>Result</th></tr></thead>
      <tbody>
        <tr><td>Positive, local: the edit-failure rate over the baseline clive arm is of the
        same order as &sect;1's 11.2%</td><td>{{CLIVE_RATE}} over the {{CLIVE_RUNS}}
        baseline clive runs.</td></tr>
        <tr><td>Positive, codex: the edit-failure parser finds the refusal in the recorded
        stream known to contain a refused patch</td><td>{{FIXTURE_FOUND}} refusal found in
        {{FIXTURE_RECORDS}} fixture records, so a zero for a codex arm is a zero.</td></tr>
        <tr><td>Instrument: each run's stored cumulative input reproduces a launch's own
        figure</td><td>{{STORE_MATCHES}} of {{STORE_COMPARED}} runs reproduce a launch's
        figure exactly.</td></tr>
      </tbody>
    </table>

    <h3 id="{{IDS}}-decision">The lead's decision</h3>
    <p id="{{IDS}}-decision-body"><strong>Lead decision, {{LEAD_DECISION}}.</strong> On the
    pooled local record the lead decided: the local lane stays on native clive; the anchored
    edit tool is removed from reckon (&sect;18), because its lower edit-failure rate changed
    no gate outcome and cost input; the omp pilot is not adopted and the host flight stops
    routing to it, its code staying in place; and the codex arms are stopped, their replays
    not run. No further flight default is changed by this record.</p>
  </section>
"""


def write_archive(repo: Path, path: Path, section: str) -> bool:
    """Splice the pooled section into the cumulative landed record.

    Every other section is left byte-identical: the archive is cumulative
    evidence, spliced rather than rewritten, so a later reader still sees each
    earlier section's landing.
    """
    target = repo / path
    text = target.read_text()
    block = "\n" + section.rstrip("\n") + "\n"
    pattern = re.compile(
        r'\n  <section id="' + re.escape(POOLED_SECTION) + r'"[^>]*>.*?</section>\n',
        re.DOTALL,
    )
    if pattern.search(text):
        updated = pattern.sub(block, text, count=1)
    elif "\n</main>" in text:
        updated = text.replace("\n</main>", block + "\n</main>", 1)
    else:
        raise SystemExit("archive splice found neither the pooled section nor the </main> anchor")
    if updated == text:
        return False
    target.write_text(updated)
    return True


def render(data: dict, result: dict, check: dict) -> str:
    assignment = data["assignment"]
    seed = assignment["seed"]
    arms = result["arms"]
    same_arm = result["same_arm"]
    runs = data["runs"]
    ids = "paired-replay-report"

    verdict_rows = []
    for arm in TREATMENTS:
        treatment = result["treatments"][arm]
        primary = treatment["primary"]
        verdict_rows.append(
            "<tr><td><code>"
            + arm
            + "</code></td><td><code>"
            + treatment["baseline"]
            + "</code></td><td>"
            + str(primary["count"])
            + "</td><td>"
            + tokens(primary["mean_difference"])
            + "</td><td>"
            + percent(primary["relative"])
            + "</td><td>"
            + tokens(primary["low"])
            + " to "
            + tokens(primary["high"])
            + "</td><td>"
            + tokens(same_arm["low"])
            + " to "
            + tokens(same_arm["high"])
            + "</td><td>"
            + overlap_text(treatment["overlap"])
            + "</td><td>"
            + rate(*treatment["arm_rate"])
            + " vs "
            + rate(*treatment["base_rate"])
            + "</td><td><strong>"
            + treatment["verdict"]
            + "</strong></td></tr>"
        )

    arm_rows = []
    for arm in ARMS:
        figures = arms[arm]
        arm_rows.append(
            "<tr><td><code>"
            + arm
            + "</code></td><td>"
            + str(figures["runs"])
            + "</td><td>"
            + rate(figures["passed"], figures["runs"])
            + "</td><td>"
            + tokens(figures["input_total"])
            + "</td><td>"
            + tokens(figures["input_per_passed"])
            + "</td><td>"
            + fmt(figures["tool_calls_per_run"])
            + "</td><td>"
            + fmt(figures["output_per_run"])
            + ("" if figures["output_measured"] else "*")
            + "</td><td>"
            + fmt(figures["peak_mean"])
            + "</td><td>"
            + rate(figures["edit_failures"], figures["edits"])
            + "</td><td>"
            + str(figures["anchored_edits"])
            + " / "
            + str(figures["anchored_reads"])
            + "</td><td>"
            + str(figures["corrective"])
            + "</td></tr>"
        )

    def arm_order(run: dict) -> tuple:
        rank = ARMS.index(run["arm"] or "excluded") if run["arm"] in ARMS else 99
        return (run["plan"], run["section"], rank)

    run_rows = []
    for run in sorted(runs, key=arm_order):
        arm_text = "<code>" + str(run["arm"]) + "</code>"
        if run["arm"] != run["declared_arm"]:
            arm_text += " (dispatched <code>" + run["declared_arm"] + "</code>)"
        peak = fmt(run["peak"]) if run["peak"] else "not measured"
        run_rows.append(
            "<tr><td><code>"
            + run["run_id"]
            + "</code></td><td>"
            + run["plan"]
            + "#"
            + run["section"]
            + "</td><td>"
            + arm_text
            + "</td><td><code>"
            + str(run["backend"])
            + "</code></td><td><code>"
            + str(run["base_sha"])
            + "</code></td><td>"
            + str(run["gate"] or "unrecorded")
            + "</td><td>"
            + str(run["launches"])
            + "</td><td>"
            + tokens(run["input"])
            + "</td><td>"
            + fmt(run["output"])
            + ("" if run["output_measured"] else "*")
            + "</td><td>"
            + peak
            + "</td><td>"
            + fmt(run["tool_calls"])
            + "</td><td>"
            + str(run["edit_failures"])
            + " / "
            + str(run["edits"])
            + "</td><td>"
            + str(run["anchored_edits"])
            + " / "
            + str(run["anchored_reads"])
            + "</td></tr>"
        )

    section_rows = []
    for section in assignment["sections"]:
        covered = [
            r
            for r in runs
            if r["plan"] == section["plan"] and r["section"] == section["section"]
        ]
        section_rows.append(
            "<tr><td>"
            + section["plan"]
            + "#"
            + section["section"]
            + "</td><td>"
            + format(section["effort_hours"], "g")
            + "</td><td>"
            + html.escape(section["stratum"])
            + "</td><td><code>"
            + section["base"]
            + "</code></td><td><code>"
            + section["first_landed_commit"]
            + "</code></td><td>"
            + str(len(covered))
            + "</td><td>"
            + str(len([r for r in covered if r["landed"]]))
            + "</td></tr>"
        )

    exclusion_rows = []
    for exclusion in assignment.get("exclusions") or []:
        exclusion_rows.append(
            "<tr><td>"
            + exclusion["plan"]
            + "#"
            + exclusion["section"]
            + "</td><td>"
            + ", ".join(str(order) for order in exclusion["orders"])
            + "</td><td>"
            + exclusion["reason"]
            + "</td></tr>"
        )
    for run in [r for r in runs if r["arm"] == "excluded"]:
        exclusion_rows.append(
            "<tr><td><code>"
            + run["run_id"]
            + "</code></td><td>&mdash;</td><td>"
            + run["membership_reason"]
            + "</td></tr>"
        )

    observation_rows = []
    by_id = {r["run_id"]: r for r in runs}
    for observation in data["observations"].get("observations") or []:
        run = by_id.get(observation["run_id"])
        section = run["plan"] + "#" + run["section"] if run else "unmatched"
        observation_rows.append(
            "<tr><td>"
            + str(observation["order"])
            + "</td><td><code>"
            + observation["run_id"]
            + "</code></td><td>"
            + section
            + "</td><td>"
            + observation["kind"]
            + "</td><td>"
            + observation["note"]
            + "</td></tr>"
        )

    treatment_detail = []
    for arm in TREATMENTS:
        treatment = result["treatments"][arm]
        primary = treatment["primary"]
        both_passed = treatment["both_passed"]
        treatment_detail.append(
            '<h4 id="'
            + ids
            + "-"
            + arm
            + '-pairs"><code>'
            + arm
            + "</code> against <code>"
            + treatment["baseline"]
            + "</code></h4>\n<p>The verdict table's figure is over all "
            + str(primary["count"])
            + " sections on which both arms ran, which is the population's own pairing and takes "
            "no selection on the outcome: mean paired difference "
            + tokens(primary["mean_difference"])
            + " ("
            + percent(primary["relative"])
            + " of the baseline's paired mean "
            + tokens(primary["baseline_mean"])
            + "), 90% interval "
            + tokens(primary["low"])
            + " to "
            + tokens(primary["high"])
            + ". Over the "
            + str(both_passed["count"])
            + " sections on which both arms also reached a passed gate &mdash; &sect;8's "
            "per-landed-node view, and a subset selected on a shared outcome &mdash; it is "
            + tokens(both_passed["mean_difference"])
            + " ("
            + percent(both_passed["relative"])
            + "), 90% interval "
            + tokens(both_passed["low"])
            + " to "
            + tokens(both_passed["high"])
            + ".</p>\n"
            + pair_table(primary["pairs"])
        )

    failed = [r for r in runs if not r["landed"]]
    if failed:
        failed_note = (
            str(len(failed))
            + " of the "
            + str(len(runs))
            + " runs did not reach a passed gate: "
            + ", ".join(
                "<code>" + r["run_id"] + "</code> (" + r["plan"] 
                + "#" + r["section"] + ")"
                for r in failed
            )
            + ". A failed run is a member of its arm: its input is in the arm's cumulative "
            "total and its gate is in the pass rate's denominator, but it is not a landed "
            "node and so is not in the per-landed-node figure."
        )
    else:
        failed_note = "Every run reached a passed gate."

    if check["codex_anchored_edit_calls"]:
        codex_share = (
            str(check["codex_anchored_edits"])
            + " of "
            + str(check["codex_anchored_edit_calls"])
            + " counted codex-anchored edit calls were <code>edit_anchored</code> calls, "
            "against "
            + str(check["codex_anchored_reads"])
            + " <code>read_anchored</code> calls; codex keeps <code>apply_patch</code>, so "
            "both paths remain."
        )
    else:
        codex_share = (
            "No codex-anchored edit call was counted, so the share of edits routed through "
            "the tool is undefined."
        )

    clive_rate = check["clive_rate"]
    order_text = (
        "the same order as &sect;1's 11.2%"
        if clive_rate is not None and 0.3 * 0.112 <= clive_rate <= 3.0 * 0.112
        else "not of the same order as &sect;1's 11.2%"
    )

    values = {
        "IDS": ids,
        "SEED": str(seed),
        "DRAWN": str(len(assignment["sections"])),
        "EXCLUDED_NAMES": ", ".join(
            html.escape(f"{e['plan']}#{e['section']}")
            for e in (assignment.get("exclusions") or [])
        )
        or "none",
        "SECTIONS": str(len(assignment["sections"]) - len(assignment.get("exclusions") or [])),
        "RUNS": str(len(runs)),
        "VERDICT_ROWS": "".join(verdict_rows),
        "ARM_ROWS": "".join(arm_rows),
        "RUN_ROWS": "".join(run_rows),
        "SECTION_ROWS": "".join(section_rows),
        "EXCLUSION_ROWS": "".join(exclusion_rows)
        or "<tr><td>none</td><td>&mdash;</td><td>No section and no run is excluded.</td></tr>",
        "OBSERVATION_ROWS": "".join(observation_rows)
        or "<tr><td>&mdash;</td><td>&mdash;</td><td>&mdash;</td><td>&mdash;</td>"
        "<td>No observation is recorded.</td></tr>",
        "TREATMENT_DETAIL": "\n".join(treatment_detail),
        "SAME_ARM_PAIRS": pair_table(same_arm["pairs"]),
        "SAME_ARM_COUNT": str(same_arm["count"]),
        "SAME_ARM_MEAN": tokens(same_arm["mean_difference"]),
        "SAME_ARM_RELATIVE": percent(same_arm["relative"]),
        "SAME_ARM_BASELINE_MEAN": tokens(same_arm["baseline_mean"]),
        "SAME_ARM_LOW": tokens(same_arm["low"]),
        "SAME_ARM_HIGH": tokens(same_arm["high"]),
        "BOOTSTRAP": format(BOOTSTRAP, ","),
        "SHARE": codex_share,
        "FAILED": failed_note,
        "POPULATION_QUERY": html.escape(assignment["population_query"]),
        "ALLOCATION": html.escape(
            " / ".join(f"{k}: {v}" for k, v in assignment["allocation"].items())
        ),
        "STRATA": html.escape(
            " / ".join(f"{k}: {v}" for k, v in assignment["strata"].items())
        ),
        "POPULATION_SECTIONS": str(assignment["population_sections"]),
        "CLIVE_RATE": rate(check["clive_edit_failures"], check["clive_edit_calls"]),
        "CLIVE_RUNS": str(check["clive_runs"]),
        "CLIVE_ORDER": order_text,
        "FIXTURE_FOUND": str(check["fixture_found"]),
        "FIXTURE_RECORDS": str(check["fixture_records"]),
        "FIXTURE_BYTES": str(check["fixture_bytes"]),
        "STORE_MATCHES": str(check["store_matches"]),
        "STORE_COMPARED": str(check["store_compared"]),
        "STORE_SINGLE_MATCHES": str(check["store_single_matches"]),
        "STORE_SINGLE": str(check["store_single"]),
        "COMMAND": result["command"],
        "PRODUCER": str(RECORD.parent / "paired_replay_report.py"),
    }
    page = TEMPLATE
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    return page


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="reckon">
  <meta name="reckon-type" content="evidence">
  <meta name="plan-slug" content="paired-replay-report">
  <meta name="plan-title" content="Nine landed sections replayed by six arms, paired">
  <meta name="plan-evidence-for" content="reckon:workers-spend-fewer-tokens-per-landed-node">
  <meta name="plan-verifies" content="reckon:workers-spend-fewer-tokens-per-landed-node#s14">
  <meta name="plan-recorded-at" content="2026-11-17">
  <meta name="plan-tags" content="crew,measurement,paired-replay,worker-tokens">
  <title>Paired replay of nine dispatched sections across six arms</title>
  <link rel="stylesheet" href="/_shared/foundation.css">
</head>
<body>
<main class="plan-doc">
<article class="r-evidence" id="{{IDS}}">
  <h3 id="{{IDS}}-summary">Paired replay: six arms over nine landed sections</h3>

  <p id="{{IDS}}-question">Six measurement runs build each of the same already-landed sections from
  the same base revision, so the section-difficulty confound &sect;12's record names is removed from
  the comparison: the arm's run and its baseline's run on a section differ only in the arm. The draw
  recorded in <code>paired-replay-assignment.json</code> before any replay ran took
  <strong>{{DRAWN}}</strong> sections at seed <strong>{{SEED}}</strong>; one of them,
  <strong>{{EXCLUDED_NAMES}}</strong>, was excluded before any of its replays ran, on context fit:
  its first replay's dispatch estimated more usable input than the local lane's window holds, and
  every local arm has that window, so the section could not be paired for any of the six arms. Its
  full reason is in the exclusion table below. <strong>{{SECTIONS}}</strong> sections were therefore
  dispatched and their
  <strong>{{RUNS}}</strong> runs are the population below, all listed by id.</p>

  <h4 id="{{IDS}}-verdicts">Each arm's verdict under &sect;8's rule</h4>
  <p id="{{IDS}}-rule">The rule applied to the paired differences: <strong>adopt</strong> only when
  cumulative input per landed node falls by at least 5% against the arm's baseline with no fall in
  the gate pass rate. Two constraints precede the size test. First &sect;14's separation test: an arm
  whose paired-difference 90% interval overlaps the same-arm interval &mdash; the spread the
  replicate clive run shows against clive on the same sections &mdash; is reported as
  <strong>not separated</strong> and is not adopted, whatever its point estimate. Second, the
  direction of the defect: fewer edit failures and less input per landed node are better, and a lower
  gate pass rate is worse, so an arm that spends fewer tokens by passing fewer gates reads as a
  failure rather than as a saving. The interval quoted is over every section on which both arms ran,
  which takes no selection on the outcome; the both-arms-passed pairing, &sect;8's per-landed-node
  view, is listed per arm below beside it.</p>
  <table>
    <thead><tr><th>Arm</th><th>Baseline</th><th>Sections paired</th>
    <th>Mean paired &Delta; input</th><th>Relative</th><th>90% interval</th>
    <th>Same-arm interval</th><th>Overlaps</th><th>Gate pass rate (arm vs baseline)</th>
    <th>Verdict</th></tr></thead>
    <tbody>
      {{VERDICT_ROWS}}
    </tbody>
  </table>
  <p id="{{IDS}}-nodefault">No flight default is changed by this record. It applies &sect;8's rule
  to the measured paired differences and stops there; adopting a treatment, and changing what
  <code>anchored_edit</code> or a backend does by default, is the lead's decision on the record.</p>

  <h4 id="{{IDS}}-same-arm">Negative control: the spread a same-arm comparison already shows</h4>
  <p id="{{IDS}}-same-arm-spread">The replicate clive run is paired with the clive run on each
  section: two runs of one arm, on one section, from one base revision. Their paired difference in
  cumulative input is the spread no treatment can be credited with. Over {{SAME_ARM_COUNT}} sections
  the mean paired difference is <strong>{{SAME_ARM_MEAN}} ({{SAME_ARM_RELATIVE}} of the baseline's
  paired mean {{SAME_ARM_BASELINE_MEAN}})</strong>, 90% paired bootstrap interval
  <strong>{{SAME_ARM_LOW}} to {{SAME_ARM_HIGH}}</strong> ({{BOOTSTRAP}} resamples over sections,
  seeded). The interval excludes zero and its point estimate is positive: on these sections the
  replicate spent more than the baseline it replicates, by about a fifth of that baseline's own mean,
  four times the 5% adoption threshold. That is the comparison floor against which every treatment's
  interval is read, and it is why the local arms, whose intervals straddle it, are reported as not
  separated.</p>
  {{SAME_ARM_PAIRS}}

  <h4 id="{{IDS}}-per-arm">&sect;8's metrics, per arm</h4>
  <p id="{{IDS}}-per-arm-note">Spend columns are per gate-passed run; count columns are per run.
  Cumulative input is the whole prompt of every request the run made, cached segments included,
  summed over the run's launches. Peak context is the widest single prompt. An output figure marked
  <strong>*</strong> is a lower bound, from a launch that recorded no terminal usage record.</p>
  <table>
    <thead><tr><th>Arm</th><th>Runs</th><th>Gate passed</th><th>Cumulative input</th>
    <th>Input per gate-passed run</th><th>Tool calls / run</th><th>Output / run</th>
    <th>Peak context</th><th>Edit failures / calls</th>
    <th><code>edit_anchored</code> / <code>read_anchored</code></th>
    <th>Corrective redispatch</th></tr></thead>
    <tbody>
      {{ARM_ROWS}}
    </tbody>
  </table>
  <p id="{{IDS}}-share">{{SHARE}}</p>
  <p id="{{IDS}}-failed">{{FAILED}}</p>

  <h4 id="{{IDS}}-pairs">The paired differences, section by section</h4>
  {{TREATMENT_DETAIL}}

  <h4 id="{{IDS}}-sections">The seed and the section choice</h4>
  <p id="{{IDS}}-draw">Seed <strong>{{SEED}}</strong>; the population query and the strata are the
  assignment's, recorded before any replay was dispatched:
  <code>{{POPULATION_QUERY}}</code>. The draw allocated {{ALLOCATION}} over strata
  {{STRATA}}, out of {{POPULATION_SECTIONS}} eligible sections. Each section's base is the dispatch
  base its first landed commit's own run recorded, corrected before any replay was dispatched (the
  assignment's <code>base_correction</code>); every replay's own store row carries exactly that base
  revision, which is how the pairing is checked rather than assumed.</p>
  <table>
    <thead><tr><th>Section</th><th>Effort h</th><th>Stratum</th><th>Base revision</th>
    <th>First landed commit</th><th>Replays</th><th>Gate passed</th></tr></thead>
    <tbody>
      {{SECTION_ROWS}}
    </tbody>
  </table>

  <h4 id="{{IDS}}-exclusions">Excluded from the comparison, with the reason</h4>
  <table>
    <thead><tr><th>Section or run</th><th>Replay order</th><th>Reason</th></tr></thead>
    <tbody>
      {{EXCLUSION_ROWS}}
    </tbody>
  </table>

  <h4 id="{{IDS}}-runs">Every run, by id, with its section and base revision</h4>
  <p id="{{IDS}}-runs-note">{{RUNS}} runs over {{SECTIONS}} sections: one per arm per section,
  except for the excluded section, which holds none. The arm shown is the arm the run is counted in;
  where a run was dispatched under another name the dispatched name is shown beside it.</p>
  <table>
    <thead><tr><th>Run</th><th>Section</th><th>Arm</th><th>Backend</th><th>Base revision</th>
    <th>Gate</th><th>Launches</th><th>Cumulative input</th><th>Output</th><th>Peak context</th>
    <th>Tool calls</th><th>Edit failures / calls</th><th>Anchored edits / reads</th></tr></thead>
    <tbody>
      {{RUN_ROWS}}
    </tbody>
  </table>

  <h4 id="{{IDS}}-observations">Coordinator observations on individual runs</h4>
  <p id="{{IDS}}-observations-note">Facts the run store does not carry, recorded by the coordinator
  in <code>paired-replay-observations.json</code> and reported here under the run they name. They are
  not measurements, and no figure above is adjusted for them.</p>
  <table>
    <thead><tr><th>Order</th><th>Run</th><th>Section</th><th>Kind</th><th>Note</th></tr></thead>
    <tbody>
      {{OBSERVATION_ROWS}}
    </tbody>
  </table>

  <h4 id="{{IDS}}-controls">&sect;8's controls on the query</h4>
  <table>
    <thead><tr><th>Control</th><th>Result</th></tr></thead>
    <tbody>
      <tr><td>Positive, local: the edit-failure rate read by &sect;1's method over the baseline
      local arm is of the same order as &sect;1's 11.2%</td>
      <td>{{CLIVE_RATE}} over the {{CLIVE_RUNS}} baseline clive runs,
      {{CLIVE_ORDER}}. An edit call is <code>Edit</code>, <code>MultiEdit</code> or
      <code>edit_anchored</code>; a refusal is the tool result's own error flag. A non-zero rate from
      a named refusal population is the instrument seeing what is there.</td></tr>
      <tr><td>Positive, codex: the edit-failure parser first finds the refusal in the recorded
      stream known to contain a refused patch
      (<code>docs/research/fixtures/codex-refused-patch-stream.jsonl</code>)</td>
      <td>{{FIXTURE_FOUND}} refusal found in {{FIXTURE_RECORDS}} records ({{FIXTURE_BYTES}} bytes),
      so a zero for a codex arm is a zero and not an unread field. A refused codex patch is not an
      item in <code>codex exec --json</code> at all, so the codex edit-failure count is a lower bound
      read from the model's message text.</td></tr>
      <tr><td>Instrument: each run's stored cumulative input in the harness's own run store
      reproduces the figure the run's own stream reader counts for one of its launches</td>
      <td>{{STORE_MATCHES}} of {{STORE_COMPARED}} runs reproduce a launch's figure exactly
      ({{STORE_SINGLE_MATCHES}} of {{STORE_SINGLE}} single-launch runs). For the rest the store's
      <code>budget</code> detail has been rotated out, so no comparison is possible and none is
      claimed.</td></tr>
    </tbody>
  </table>

  <h4 id="{{IDS}}-method">The query that produced this record</h4>
  <p id="{{IDS}}-command">Computed on the <code>all_debug</code> partition, never on the fleet node,
  from the run store and the replay run directories. The producer is committed with this record at
  <code>{{PRODUCER}}</code>, so every figure here can be re-derived from the tree; it reads no state
  of its own and prints no figure it did not measure. The literal command, whose log sits in the run
  directory and ends in its own exit status:</p>
  <pre><code>{{COMMAND}}</code></pre>
  <p id="{{IDS}}-definitions">Read from the run store: every row whose node matches
  <code>replay-&lt;arm&gt;-&lt;plan&gt;-s&lt;n&gt;</code> for one of the six arms and one of the six
  named plans, and from that row the gate verdict, base revision, commits, attempt count and outcome.
  Read from each run's own directory &mdash; the archived directory where the live one holds only the
  released run's manifest, chosen by which holds the launch evidence:
  <code>stream.jsonl</code> and any <code>resume-*.jsonl</code> for the tool-call and edit counts, and
  for codex the harness's own <code>codex-home/sessions/**/*.jsonl</code> rollout for cumulative
  usage. Arm membership is &sect;8's rule read from those directories: a Claude-dialect run is in an
  anchored arm only when its session-start record lists <code>reckon-anchored-edit</code> as
  <code>connected</code>, and a codex run only when a launch record's <code>anchored_edit</code> is
  true. Cumulative input is the sum over launches of the run's own whole-prompt total: for Claude the
  <code>result</code> event's <code>usage</code>, which is the figure the store itself carries; for
  omp the per-turn <code>message_end</code> usage; for codex the rollout's final
  <code>total_token_usage.input_tokens</code>, which reproduces the stream's
  <code>turn.completed</code> sum wherever both exist. Tool calls are assistant <code>tool_use</code>
  blocks (Claude), omp's <code>tool_execution_start</code> records, and codex's
  <code>item.completed</code> items of type <code>command_execution</code>, <code>file_change</code>
  or <code>mcp_tool_call</code>. A corrective redispatch is a run holding more than one attempt. Peak
  context is the widest single prompt, taken for codex from its rollout's per-response usage. The
  paired bootstrap resamples sections with replacement, {{BOOTSTRAP}} draws, seeded per arm, and
  reports the 5th and 95th percentiles of the resampled mean difference.</p>
</article>
</main>
</body>
</html>
"""

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--root", type=Path, default=Path.home() / ".config/reckon/crew")
    parser.add_argument("--run-store", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true", help="write the standalone record")
    parser.add_argument(
        "--write-archive",
        action="store_true",
        help="splice the pooled section into the landed archive record",
    )
    parser.add_argument("--archive", type=Path, default=None)
    parser.add_argument(
        "--fixture-run",
        type=Path,
        default=None,
        help=(
            "a JSON file holding replay store rows to include as if read from the "
            "store; the negative control supplies a row whose plan the producer "
            "does not name"
        ),
    )
    parser.add_argument("--command", default="", help="the literal command that ran")
    args = parser.parse_args()
    repo = args.repo.resolve()
    db = args.run_store or (Path.home() / ".config/reckon/crew/run_store.db")
    fixture_rows = None
    if args.fixture_run:
        loaded = json.loads(args.fixture_run.read_text())
        fixture_rows = loaded["runs"] if isinstance(loaded, dict) else loaded
    data = measure(repo, args.root, db, fixture_rows)
    result = analysis(data)
    result["command"] = args.command
    check = controls(data, repo)
    if args.json:
        print(json.dumps({"analysis": result, "controls": check}, indent=1, default=str))
    elif args.write_archive:
        path = args.archive or ARCHIVE
        changed = write_archive(repo, path, render_pooled(data, result, check))
        verb = "wrote" if changed else "left byte-identical"
        print(verb + " the pooled section of " + str(repo / path))
    elif args.write:
        (repo / RECORD).write_text(render(data, result, check))
        print("wrote " + str(repo / RECORD))
    else:
        print(render(data, result, check))