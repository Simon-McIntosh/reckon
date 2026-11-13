"""Read the first wave's crew runs and emit the interim arm comparison.

The population is the five runs the seeded draw assigned to the five arms
(``interim-arm-wave.json`` beside the record). Every figure is read from the
run's own directory: its stream files carry the token and tool-call records,
its ``attempt-*.worker.json`` carries the run's launch. The run store is read
only to cross-check the launches it holds, because it keeps one row per run
and several of these runs were resumed, so its cumulative figures cover one
launch rather than the run.

No verdict is drawn here. At one run per arm the figures also measure the
difficulty of the sections the runs were drawn for, so the record reports them
beside the spread a same-arm split already shows and names no arm better.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import re
import sqlite3
import statistics
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRAGMENT = REPO / "docs/evidence/fragments/workers-spend-fewer-tokens-per-landed-node"
WAVE = FRAGMENT / "interim-arm-wave.json"
RECORD = FRAGMENT / "interim-arm-report.html"
FIXTURE = REPO / "docs/research/fixtures/codex-refused-patch-stream.jsonl"
ARMS = ["clive", "clive-anchored", "omp-local", "codex", "codex-anchored"]
BOOTSTRAP = 10_000
INTERVAL = 0.90

# A refused codex patch is not an item in `codex exec --json`; the refusal is
# only in the model's own message text, so this counts a lower bound.
CODEX_REFUSAL = re.compile(r"apply_patch verification failed", re.IGNORECASE)
STORE_QUERY = "SELECT detail FROM run_details WHERE run_id = ?"
ANCHORED_EDIT = "mcp__reckon-anchored-edit__edit_anchored"
ANCHORED_READ = "mcp__reckon-anchored-edit__read_anchored"


def launches(run_dir: Path) -> list[Path]:
    """The run's stream files: the first launch's stream, then each resume."""
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
        except json.JSONDecodeError:
            continue
    return records


def _read_records(path: Path) -> list[dict]:
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


def _dedupe_assistant(records) -> tuple[int, int, int]:
    """Whole-prompt tokens, output tokens and the widest prompt.

    Claude Code emits one usage record per content block with the same message
    id, so the records are deduplicated by message id; the result event's usage
    agrees exactly with that sum where a launch wrote one.
    """
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
            for name in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        output = int(usage.get("output_tokens") or 0)
        prior = seen.get(key)
        if prior is None or output > prior[1]:
            seen[key] = (prompt, output)
    prompts = [entry[0] for entry in seen.values()]
    return (
        sum(prompts),
        sum(entry[1] for entry in seen.values()),
        max(prompts, default=0),
    )


def parse_claude(records) -> dict:
    deduped_input, deduped_output, peak = _dedupe_assistant(records)
    result_input = 0
    result_output = 0
    results = 0
    tool_calls = 0
    anchored_reads = 0
    anchored_edits = 0
    edit_names = {"Edit", "MultiEdit", ANCHORED_EDIT}
    edits: list[str] = []
    for record in records:
        kind = record.get("type")
        if kind == "result":
            results += 1
            usage = record.get("usage") or {}
            result_input += sum(
                int(usage.get(name) or 0)
                for name in (
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
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
    edit_failures = sum(1 for tool_id in edits if failures.get(tool_id))
    return {
        "input": deduped_input,
        "output": result_output if results else deduped_output,
        "output_measured": bool(results),
        "peak": peak,
        "tool_calls": tool_calls,
        "edits": len(edits),
        "edit_failures": edit_failures,
        "anchored_reads": anchored_reads,
        "anchored_edits": anchored_edits,
        "terminal_records": results,
    }


def parse_omp(records) -> dict:
    total_input = 0
    total_output = 0
    peak = 0
    tool_calls = 0
    edits = 0
    edit_failures = 0
    names: dict[str, str] = {}
    for record in records:
        kind = record.get("type")
        if kind == "tool_execution_start":
            tool_calls += 1
            names[str(record.get("toolCallId"))] = str(record.get("toolName"))
            if record.get("toolName") == "edit":
                edits += 1
        elif kind == "tool_execution_end":
            if names.get(str(record.get("toolCallId"))) == "edit" and record.get(
                "isError"
            ):
                edit_failures += 1
        elif kind == "message_end":
            message = record.get("message") or {}
            if str(message.get("role") or "") != "assistant":
                continue
            usage = message.get("usage") or {}
            if not usage:
                continue
            prompt = sum(
                int(usage.get(name) or 0)
                for name in ("input", "cacheRead", "cacheWrite")
            )
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


def parse_codex(records) -> dict:
    total_input = 0
    total_output = 0
    tool_calls = 0
    edits = 0
    edit_failures = 0
    anchored_reads = 0
    anchored_edits = 0
    turns = 0
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
                    if item.get("error") or item.get("status") not in (
                        None,
                        "",
                        "completed",
                    ):
                        edit_failures += 1
            elif item_type == "agent_message":
                edit_failures += len(CODEX_REFUSAL.findall(item.get("text") or ""))
    return {
        "input": total_input,
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


def read_stream(path: Path, dialect: str) -> dict:
    records = _read_records(path)
    if dialect == "omp-local":
        return parse_omp(records)
    if dialect in {"codex", "codex-anchored"}:
        return parse_codex(records)
    return parse_claude(records)


def session_start(run_dir: Path) -> dict | None:
    """The first init record of the run's launches, which names the servers."""
    for path in launches(run_dir):
        for record in _read_records(path):
            if record.get("type") == "system" and record.get("subtype") == "init":
                return record
    return None


def landed_commit(repo: Path, run_id: str) -> str | None:
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "main",
            "--format=%H %s",
            "--grep",
            f"land({run_id})",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not out:
        return None
    return out.splitlines()[0].split()[0]


def effort_hours(repo: Path, plan: str, section: str) -> float | None:
    path = repo / "docs/plans" / f"{plan}.html"
    text = path.read_text()
    match = re.search(
        r'<section[^>]*data-id="'
        + re.escape(section)
        + r'"[^>]*data-effort-hours="([0-9.]+)"',
        text,
    )
    return float(match.group(1)) if match else None


def arm_membership(
    dialect: str, attempts: list[dict], start: dict | None
) -> tuple[str, str]:
    """Return (arm-or-excluded, reason) under the rule the section states."""
    if dialect in {"clive", "clive-anchored"}:
        if start is None:
            return "excluded", "no session-start record on the run's launch"
        servers = {
            str(entry.get("name")): str(entry.get("status"))
            for entry in start.get("mcp_servers") or []
        }
        connected = servers.get("reckon-anchored-edit") == "connected"
        if dialect == "clive-anchored":
            if connected:
                return (
                    "clive-anchored",
                    "session-start record lists reckon-anchored-edit connected",
                )
            return (
                "excluded",
                "session-start record does not list reckon-anchored-edit connected",
            )
        if connected:
            return (
                "excluded",
                "baseline run's session-start record lists the anchored server",
            )
        return "clive", "session-start record lists no anchored server"
    if dialect in {"codex", "codex-anchored"}:
        anchored = [bool(a.get("anchored_edit")) for a in attempts]
        if dialect == "codex-anchored":
            if any(anchored):
                return "codex-anchored", "a launch record carries anchored_edit true"
            return "excluded", "no launch record carries anchored_edit true"
        if any(anchored):
            return "excluded", "baseline run's launch record carries anchored_edit true"
        return "codex", "no launch record carries anchored_edit true"
    return "omp-local", "oh-my-pi arm; takes no anchored split"


def bootstrap_interval(
    values: list[float], seed: int
) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = random.Random(seed)  # noqa: S311 - seeded resampling, reproducibility not secrecy
    means = []
    for _ in range(BOOTSTRAP):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(statistics.fmean(sample))
    means.sort()
    low = means[int((1 - INTERVAL) / 2 * len(means))]
    high = means[int((1 + INTERVAL) / 2 * len(means)) - 1]
    return low, high


def splits(runs: list[dict]):
    """Every distinct way to cut the runs into halves as equal as the count allows."""
    size = len(runs) // 2
    seen = set()
    for combo in itertools.combinations(range(len(runs)), size):
        complement = tuple(i for i in range(len(runs)) if i not in combo)
        key = tuple(sorted((combo, complement)))
        if key in seen:
            continue
        seen.add(key)
        yield [runs[i] for i in combo], [runs[i] for i in complement]


def split_selfcheck() -> tuple[int, float]:
    """Prove the split instrument defines a difference where one exists.

    The measured baseline arm holds one run, so its only split is undefined;
    the same code is run here over a synthetic four-run arm whose inputs are
    known, so "undefined" is read as a fact about the population rather than
    a broken instrument.
    """
    synthetic = [
        {"run_id": f"synthetic-{index}", "input": value, "landed": True}
        for index, value in enumerate((100, 200, 300, 400))
    ]
    found = list(splits(synthetic))
    differences = []
    for left, right in found:
        a, b = per_landed(left), per_landed(right)
        if a is not None and b is not None:
            differences.append(abs(a - b))
    return len(found), max(differences) if differences else 0.0


def per_landed(members: list[dict]) -> float | None:
    landed = [m for m in members if m["landed"]]
    if not landed:
        return None
    return sum(m["input"] for m in landed) / len(landed)


def store_inputs(db_path: Path, run_ids: list[str]) -> dict[str, int]:
    """Read each run's stored cumulative input, where the store holds the run."""
    if not db_path.exists():
        return {}
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    found = {}
    try:
        for run_id in run_ids:
            row = connection.execute(STORE_QUERY, (run_id,)).fetchone()
            if not row:
                continue
            try:
                detail = json.loads(row[0])
            except json.JSONDecodeError:
                continue
            tokens = (detail.get("budget") or {}).get("tokens") or {}
            value = tokens.get("input_tokens_cumulative")
            if isinstance(value, int):
                found[run_id] = value
    finally:
        connection.close()
    return found


def measure(runs_root: Path, repo: Path, db_path: Path | None = None) -> dict:
    wave = json.loads(WAVE.read_text())
    rows = []
    for entry in wave["rows"]:
        run_id = entry["run_id"]
        run_dir = runs_root / run_id
        attempts = attempt_records(run_dir)
        dialect = str(attempts[0].get("backend")) if attempts else str(entry["ran_arm"])
        start = session_start(run_dir)
        arm, membership_reason = arm_membership(dialect, attempts, start)
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
        launch_names = []
        launch_inputs = []
        for path in launches(run_dir):
            if path.stat().st_size == 0:
                continue
            figures = read_stream(path, dialect)
            launch_names.append(path.name)
            launch_inputs.append(figures["input"])
            totals["input"] += figures["input"]
            totals["output"] += figures["output"]
            totals["tool_calls"] += figures["tool_calls"]
            totals["edits"] += figures["edits"]
            totals["edit_failures"] += figures["edit_failures"]
            totals["anchored_reads"] += figures["anchored_reads"]
            totals["anchored_edits"] += figures["anchored_edits"]
            totals["terminal_records"] += figures["terminal_records"]
            totals["output_measured"] = (
                totals["output_measured"] and figures["output_measured"]
            )
            if figures.get("peak"):
                totals["peak"] = max(totals["peak"], figures["peak"])
        commit = landed_commit(repo, run_id)
        rows.append(
            {
                **entry,
                "backend": dialect,
                "arm": arm,
                "membership_reason": membership_reason,
                "effort_hours": effort_hours(repo, entry["plan"], entry["section"]),
                "attempts": len(attempts),
                "corrective_rounds": max(0, len(attempts) - 1),
                "anchored_edit_launches": [
                    bool(a.get("anchored_edit")) for a in attempts
                ],
                "landed": commit is not None,
                "land_commit": commit,
                "launches": launch_names,
                "launch_inputs": launch_inputs,
                **totals,
            }
        )
    return {"wave": wave, "runs": rows, "store_check": store_check(rows, db_path)}


def store_check(rows: list[dict], db_path: Path | None = None) -> dict:
    """Compare each run's stored cumulative input against its own launch figures.

    The store is written by the harness at a launch boundary, so a stored figure
    should reproduce exactly the input of one launch of that run. A mismatch
    would mean the stream reading and the harness disagree about what a token of
    input is, and every figure in the record would rest on the wrong basis.
    """
    if db_path is None:
        db_path = Path.home() / ".config/reckon/crew/run_store.db"
    stored = store_inputs(db_path, [r["run_id"] for r in rows])
    compared = []
    for row in rows:
        value = stored.get(row["run_id"])
        if value is None:
            continue
        compared.append(
            {
                "run_id": row["run_id"],
                "stored": value,
                "launch_inputs": row["launch_inputs"],
                "matches": value in row["launch_inputs"],
                "launches": len(row["launch_inputs"]),
            }
        )
    return {
        "store_read": db_path.exists(),
        "runs_in_store": len(compared),
        "runs_compared": len(rows),
        "exact_matches": sum(1 for c in compared if c["matches"]),
        "compared": compared,
    }


def tokens(value) -> str:
    if value is None:
        return "undefined"
    return f"{value / 1_000_000:.2f}M"


def fmt(value) -> str:
    if value is None:
        return "undefined"
    return f"{value:,.0f}"


def render(data: dict) -> str:
    wave = data["wave"]
    runs = data["runs"]
    seed = int(wave["seed"])
    members = {arm: [r for r in runs if r["arm"] == arm] for arm in ARMS}
    excluded = [r for r in runs if r["arm"] == "excluded"]
    baseline = members["clive"]

    arm_rows = []
    intervals = {}
    for index, arm in enumerate(ARMS):
        arm_runs = members[arm]
        landed = [r for r in arm_runs if r["landed"]]
        low, high = bootstrap_interval([r["input"] for r in landed], seed + index)
        intervals[arm] = (low, high)
        mix = ", ".join(f"{r['effort_hours']:g} h" for r in arm_runs) or "none"
        peaks = [r["peak"] for r in arm_runs if r["peak"]]
        peak_text = fmt(statistics.fmean(peaks)) if peaks else "not measured"
        tool_text = (
            fmt(statistics.fmean([r["tool_calls"] for r in arm_runs]))
            if arm_runs
            else "undefined"
        )
        output_text = (
            fmt(statistics.fmean([r["output"] for r in arm_runs]))
            if arm_runs
            else "undefined"
        )
        if arm_runs and not all(r["output_measured"] for r in arm_runs):
            output_text += "*"
        rounds = sum(r["corrective_rounds"] for r in arm_runs)
        cells = [
            f"<code>{arm}</code>",
            str(len(arm_runs)),
            str(len(landed)),
            mix,
            tokens(sum(r["input"] for r in arm_runs)),
            tokens(per_landed(arm_runs)),
            f"{tokens(low)} to {tokens(high)}",
            tool_text,
            output_text,
            peak_text,
            f"{len(landed)}/{len(arm_runs)}",
            str(rounds),
        ]
        arm_rows.append(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        )

    run_rows = []
    for run in runs:
        failure_text = (
            f"{run['edit_failures']} / {run['edits']}" if run["edits"] else "0 / 0"
        )
        peak_text = f"{run['peak']:,}" if run["peak"] else "not measured"
        output_text = fmt(run["output"]) + ("" if run["output_measured"] else "*")
        cells = [
            f"<code>{run['run_id']}</code>",
            f"{run['plan']}#{run['section']}",
            f"<code>{run['backend']}</code>",
            f"<code>{run['arm']}</code>",
            f"{run['effort_hours']:g}",
            str(run["attempts"]),
            tokens(run["input"]),
            output_text,
            peak_text,
            str(run["tool_calls"]),
            failure_text,
            f"{run['anchored_edits']} / {run['anchored_reads']}",
            "landed" if run["landed"] else "closed, not landed",
        ]
        run_rows.append(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        )

    split_rows = []
    differences = []
    undefined_splits = 0
    for left, right in splits(baseline):
        a, b = per_landed(left), per_landed(right)
        left_ids = ", ".join(r["run_id"] for r in left) or "no run"
        right_ids = ", ".join(r["run_id"] for r in right) or "no run"
        if a is None or b is None:
            undefined_splits += 1
            shown = "undefined"
        else:
            difference = abs(a - b)
            differences.append(difference)
            shown = tokens(difference)
        cells = [left_ids, tokens(a), right_ids, tokens(b), shown]
        split_rows.append(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        )
    largest = max(differences) if differences else None

    fixture_found = 0
    if FIXTURE.exists():
        for record in _read_records(FIXTURE):
            item = record.get("item") or {}
            if item.get("type") == "agent_message":
                fixture_found += len(CODEX_REFUSAL.findall(item.get("text") or ""))

    selfcheck_splits, selfcheck_difference = split_selfcheck()
    check = data["store_check"]
    clive_calls = sum(r["edits"] for r in baseline)
    clive_failures = sum(r["edit_failures"] for r in baseline)
    if clive_calls:
        clive_control = (
            f"{100.0 * clive_failures / clive_calls:.1f}% "
            f"({clive_failures} refused edits in {clive_calls} edit calls on the single "
            "baseline run); one run is one sample, so the control shows the instrument reads "
            "refusals where they exist and not that the population's rate is &sect;1's 11.2%"
        )
    else:
        clive_control = "no edit calls on the baseline run, so no rate is defined"

    if check["store_read"]:
        store_control = (
            f"{check['exact_matches']} of {check['runs_compared']} runs reproduce a launch's input "
            f"exactly in the harness's own run store (the store holds {check['runs_in_store']} of "
            f"the {check['runs_compared']}), so the stream reading and the harness agree on what a "
            "token of cumulative input is; the store's per-run figure covers one launch, which is "
            "why the run totals are summed across launches rather than read from it"
        )
    else:
        store_control = (
            "the run store was not readable, so this control could not be taken"
        )

    if largest is not None:
        negative_control = f"largest absolute difference {tokens(largest)}"
    else:
        negative_control = (
            f"The control could not be taken: the baseline local arm holds {len(baseline)} run, "
            "so every split puts it in one half and leaves the other empty, and a half with no "
            "landed node has no defined difference. Every split is listed as undefined, and each "
            "arm's figures are reported without it."
        )

    if largest is not None:
        spread_text = (
            f"The spread a same-arm comparison already shows is <strong>{tokens(largest)}</strong> "
            "of cumulative input per landed node: the largest absolute difference over every "
            "distinct half-split of the baseline local arm "
            f"({len(split_rows)} split{'s' if len(split_rows) != 1 else ''} considered; "
            f"{undefined_splits} undefined)."
        )
    else:
        spread_text = (
            "The spread a same-arm comparison already shows is <strong>undefined</strong>: the "
            f"baseline local arm holds {len(baseline)} run, so its only distinct half-split "
            f"({len(split_rows)} considered, {undefined_splits} undefined) leaves an empty half, "
            "and a half with no landed node has no defined difference. The negative control "
            "therefore could not be taken, and each arm's figures are reported without one."
        )

    codex_anchored = members["codex-anchored"]
    anchored_edits = sum(r["anchored_edits"] for r in codex_anchored)
    anchored_reads = sum(r["anchored_reads"] for r in codex_anchored)
    anchored_edits_counted = sum(r["edits"] for r in codex_anchored)
    if anchored_edits_counted:
        share_text = (
            f"{anchored_edits} of {anchored_edits_counted} counted edit calls were "
            "<code>edit_anchored</code> calls, against "
            f"{anchored_reads} <code>read_anchored</code> calls."
        )
    else:
        share_text = (
            f"No edit call in the codex-anchored arm was counted at all: {anchored_edits} "
            f"<code>edit_anchored</code> calls and no <code>file_change</code> item, so the share "
            "of edits routed through the tool is undefined; the run's edits went through "
            f"<code>command_execution</code> commands, after {anchored_reads} "
            "<code>read_anchored</code> calls."
        )

    if excluded:
        exclusion_text = (
            "Excluded runs, counted in neither arm: "
            + ", ".join(
                f"<code>{r['run_id']}</code> ({r['membership_reason']})"
                for r in excluded
            )
            + "."
        )
    else:
        exclusion_text = "No run is excluded from both arms."

    draw_rows = []
    for run in runs:
        cells = [
            f"{run['plan']}#{run['section']}",
            f"{run['effort_hours']:g}",
            f"<code>{run['drawn_arm']}</code>",
            f"<code>{run['arm']}</code>",
            run["departure"] or "none",
        ]
        draw_rows.append(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Interim arm comparison, first wave</title>
</head>
<body>
<article class="r-evidence" id="interim-arm-report">
  <h3 id="interim-arm-report-summary">Interim comparison of the five arms at the first wave's population</h3>

  <p id="interim-arm-report-no-verdict"><strong>No verdict is drawn.</strong> Five runs, one per arm,
  were drawn onto five different sections (seed <code>{seed}</code>), so every figure below also
  measures the difficulty of the section its run was given. At this count neither an interval nor a
  same-arm split separates an arm's effect from the sections' difficulty, so no arm is called
  better or worse, no flight default moves and no treatment is removed; those remain &sect;8's at
  its own counts.</p>

  <h4 id="interim-arm-report-per-arm">Per-arm table</h4>
  <p>Spend columns are per landed node; count columns are per run. The interval is a 90% percentile
  bootstrap over the arm's landed runs, {BOOTSTRAP:,} resamples, seeded. At one run per arm every
  resample is that run, so an interval is a point, not evidence of stability; it is shown because
  &sect;8 asks for it. Output tokens marked <strong>*</strong> are a lower bound: a launch that was
  killed before writing its <code>result</code> event recorded no output total, and the stream's
  per-message usage carries none.</p>
  <table>
    <thead><tr><th>Arm</th><th>Runs</th><th>Landed</th><th>Effort-hour mix</th>
    <th>Cumulative input</th><th>Input per landed node</th><th>90% interval</th>
    <th>Tool calls / run</th><th>Output tokens / run</th><th>Peak context</th>
    <th>Landing rate</th><th>Corrective rounds</th></tr></thead>
    <tbody>
      {"".join(arm_rows)}
    </tbody>
  </table>
  <p id="interim-arm-report-spread">{spread_text} Every arm's figure above is reported beside that
  spread, and none is read as an arm difference.</p>

  <h4 id="interim-arm-report-population">The population, every run by id</h4>
  <p>The wave is these five runs and none other. A/B requests dispatched on 11 November under node
  ids beginning <code>ab-</code> or <code>ab2-</code> were not assigned by this draw and are outside
  it. A resume, repair round or corrective redispatch belongs to its run's node: its input is added
  to the node's total under the original arm and adds no run to the query.
  {exclusion_text}</p>
  <table>
    <thead><tr><th>Run</th><th>Plan section</th><th>Backend</th><th>Arm</th><th>Effort h</th>
    <th>Launches</th><th>Cumulative input</th><th>Output</th><th>Peak context</th>
    <th>Tool calls</th><th>Edit failures / calls</th><th>Anchored edits / reads</th><th>State</th></tr></thead>
    <tbody>
      {"".join(run_rows)}
    </tbody>
  </table>
  <p id="interim-arm-report-share">{share_text}</p>

  <h4 id="interim-arm-report-draw">The draw</h4>
  <p>Seed <strong>{seed}</strong>; the arm list <code>{", ".join(ARMS)}</code> was shuffled by
  <code>random.Random({seed})</code> and zipped onto these sections in the order recorded in
  <code>interim-arm-wave.json</code>:</p>
  <table>
    <thead><tr><th>Section</th><th>Effort hours</th><th>Drawn arm</th><th>Ran arm</th><th>Departure</th></tr></thead>
    <tbody>
      {"".join(draw_rows)}
    </tbody>
  </table>
  <p id="interim-arm-report-departures">Two sections departed from the draw: the codex lane was
  budget-held when the section drawn to <code>codex-anchored</code> was ready, so it ran
  <code>clive-anchored</code>; and the local lane refused the section drawn to
  <code>clive-anchored</code> on context fit (about 297k tokens against a 230k window), so it ran
  <code>codex-anchored</code>. Each run is listed under the arm it actually ran on.</p>

  <h4 id="interim-arm-report-membership">Arm membership</h4>
  <p>A Claude-dialect run belongs to an anchored arm only when its session-start record lists
  <code>reckon-anchored-edit</code> as <code>connected</code>. The clive-anchored run's first launch
  recorded <code>anchored_edit: true</code> and its resumed launch recorded <code>false</code>
  (reason <code>disabled</code>), yet the resumed session's own start record still lists the server
  connected with both tools, because the run home kept the server the first launch seeded. The
  session-start record is the fact read, which is why this run counts in the anchored arm. A codex
  run belongs to the anchored arm when a launch record carries <code>anchored_edit: true</code>: the
  codex-anchored run's first launch did, its later launches did not, and its stream carries
  {anchored_reads} <code>read_anchored</code> and {anchored_edits} <code>edit_anchored</code> calls,
  the reads all in the first launch.</p>

  <h4 id="interim-arm-report-controls">Controls</h4>
  <table>
    <thead><tr><th>Control</th><th>Result</th></tr></thead>
    <tbody>
      <tr><td>Positive, local: the edit-failure rate read by &sect;1's method over the baseline
      local arm is of the same order as &sect;1's 11.2%</td>
      <td>{clive_control}</td></tr>
      <tr><td>Positive, codex: the edit-failure parser first finds a refused patch in the stream
      known to contain one (<code>docs/research/fixtures/codex-refused-patch-stream.jsonl</code>)</td>
      <td>{fixture_found} refused patch found by the same text rule the codex arms are read with, so
      a zero for a codex arm means zero and not an unread field. A refused codex patch is not an
      item in <code>codex exec --json</code> at all, so a codex edit-failure rate is a lower bound
      read from message text.</td></tr>
      <tr><td>Instrument: the split code run over a synthetic four-run arm, so that
      &ldquo;undefined&rdquo; is a fact about the measured population and not a broken control</td>
      <td>{selfcheck_splits} distinct splits, largest absolute difference {selfcheck_difference:,.0f}</td></tr>
      <tr><td>Instrument: each run's stored cumulative input in the harness's own run store
      reproduces the input the stream reader counts for one of that run's launches</td>
      <td>{store_control}</td></tr>
      <tr><td>Negative: the baseline local arm split into two halves in every distinct way,
      reporting the largest absolute difference in cumulative input per landed node</td>
      <td>{negative_control}</td></tr>
    </tbody>
  </table>
  <table>
    <thead><tr><th>Half A</th><th>Input per landed node</th><th>Half B</th><th>Input per landed node</th><th>|difference|</th></tr></thead>
    <tbody>{"".join(split_rows)}</tbody>
  </table>

  <h4 id="interim-arm-report-method">The query that produced this record</h4>
  <p>The record is generated, not transcribed: <code>scripts/interim_arm_report.py</code> reads the
  wave file, each run's stream files (<code>stream.jsonl</code> and <code>resume-*.jsonl</code> under
  the run's own directory), each run's <code>attempt-*-worker.json</code> launch records, the plan
  sections' effort hours, and <code>git -C reckon log main --grep "land(&lt;run_id&gt;)"</code> for
  landing. Its run-store cross-check reads <code>{STORE_QUERY}</code>:
  the store's <code>budget.tokens.input_tokens_cumulative</code> is written at a launch boundary and
  covers one launch rather than the whole run, so the per-run totals are summed from the streams,
  while the store's figure is used to check the reading against the harness's own count. Definitions: cumulative input is the whole
  prompt of every request, cached segments included (Claude Code assistant records deduplicated by
  message id, which reproduces each launch's <code>result</code> usage exactly; omp's per-turn
  <code>message_end</code> usage; codex's <code>turn.completed</code> usage); edit calls are
  <code>Edit</code>, <code>MultiEdit</code>, <code>edit_anchored</code>, omp's <code>edit</code> tool
  and codex <code>file_change</code> items, with a refusal read from the tool result's error flag or,
  for codex, from the model's message text; peak context is the widest single prompt, which codex's
  stream does not carry. Landing is a <code>land(&lt;run_id&gt;)</code> commit on <code>main</code>;
  every one of the five runs has one.</p>
</article>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=Path.home() / ".config/reckon/crew/runs",
    )
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument(
        "--run-store",
        type=Path,
        default=None,
        help="path to the crew run store, for the instrument cross-check",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the measured data only"
    )
    parser.add_argument(
        "--write", action="store_true", help="write the evidence record"
    )
    args = parser.parse_args()
    data = measure(args.runs_root, args.repo, args.run_store)
    if args.json:
        print(json.dumps(data, indent=1))
        return 0
    html = render(data)
    if args.write:
        RECORD.write_text(html)
        print(f"wrote {RECORD}")
    else:
        print(html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
