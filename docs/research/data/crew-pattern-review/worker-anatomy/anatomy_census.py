"""Census of how crew workers spend turns and context, by lane and role.

Reads only durable records: the run store's committed run rows, the five
projects' committed ledgers, the per-run stream files and the promotion
commits on each primary branch. The study window is a constant, not the
clock, so re-running reproduces byte-identical JSON.

Metrics per run, per lane and role, stratified by outcome:

* first-request input tokens (claude dialect only: the codex grammar
  publishes no per-request figure, its single ``turn.completed`` sums the
  whole turn, so that cell states its own unmeasurability rather than
  publishing a contaminated number)
* assistant turns
* turns before the first file edit
* share of tool calls that read plan/evidence/research HTML, AGENTS.md,
  skill files or manifests
* share of file edits by class (source, tests, plan/evidence/research
  HTML, figures, manifest and reports, other)
* share of observed wall time inside gate or test commands (claude dialect
  only: the codex grammar emits no timestamps)
* self-verification reads (a Read of a path the worker itself wrote
  earlier in the same stream)

Run with the repository's root environment and the worktree on the path::

    PYTHONPATH=$PWD /home/ITER/mcintos/Code/reckon/.venv/bin/python \
        docs/research/data/crew-pattern-review/worker-anatomy/anatomy_census.py

The parser is proved on one named healthy run per stream schema; the proof
block records what the parser saw for each, and the script refuses to write
output if either proof run parses to zero turns or zero tool calls.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from reckon.crew.carryover_census import _dialect, _first_turn_input
from reckon.crew.metering import run_streams

WINDOW_START = "2026-09-12T00:00:00"
WINDOW_END = "2026-09-26T10:00:00"  # the window closes here; the population is pinned at this instant
CAPTURE_INSTANT = WINDOW_END
PROJECTS = ("reckon", "imas-ambix", "nova", "imas-efit", "imas-codex")
CODE_ROOT = Path("/home/ITER/mcintos/Code")
RUNS_ROOT = Path("/home/ITER/mcintos/.config/reckon/crew/runs")
STORE_DB = Path("/home/ITER/mcintos/.config/reckon/crew/run_store.db")
HERE = Path(__file__).resolve().parent
OUT = HERE / "anatomy.json"
#: The window's run population is pinned on first build. The run store grows
#: after the capture instant as late-fired runs reconcile, so a reader that
#: re-derived it each time could not reproduce the census byte for byte.
POPULATION_FILE = HERE / "population.json"

PLAN_EVIDENCE_RE = re.compile(
    r"(^|/)(docs/(plans|evidence|research)/.*\.html|docs/[^/]*\.html)$", re.IGNORECASE
)
FIGURE_RE = re.compile(r"(^|/)docs/figures/", re.IGNORECASE)
MANIFEST_REPORT_RE = re.compile(
    r"(^|/)(manifest\.md$|docs/research/data/|docs/reports/|docs/state/)", re.IGNORECASE
)
GUIDANCE_RE = re.compile(r"(^|/)(AGENTS\.md|CLAUDE\.md)$", re.IGNORECASE)
SKILL_RE = re.compile(r"(^|/)(SKILL\.md$|skills/)", re.IGNORECASE)
TEST_RE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)", re.IGNORECASE)
SOURCE_RE = re.compile(
    r"\.(py|pyi|ts|tsx|js|jsx|mjs|c|cc|cpp|h|hpp|f90|f95|cmake|toml|yaml|yml|sh|sql)$",
    re.IGNORECASE,
)
GATE_TEST_RE = re.compile(
    r"\b(pytest|ctest|ruff check|ruff format|scons|npm (run )?test|mypy|pyright)\b"
)
READ_VERB_RE = re.compile(r"\b(cat|head|tail|less|sed|awk|grep|rg|python3?|wc|stat)\b")
DOCS_PATH_RE = re.compile(
    r"[\w./-]*(?:docs/[\w./-]+\.html|AGENTS\.md|CLAUDE\.md|SKILL\.md|manifest\.md)"
)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
READ_KINDS = ("plan_evidence_research_html", "agents_md", "skill_files", "manifests")
EDIT_CLASSES = (
    "source",
    "tests",
    "plan_evidence_research_html",
    "figures",
    "manifest_and_reports",
    "other",
)
DIALECTS = ("claude", "codex")


def lane_of(backend):
    b = (backend or "").strip().lower()
    if b.startswith("codex"):
        return "codex"
    if b.startswith("claude"):
        return "claude"
    return b or "unknown"


def edit_class(path):
    p = (path or "").lstrip("./")
    if not p:
        return "other"
    if FIGURE_RE.search(p):
        return "figures"
    if MANIFEST_REPORT_RE.search(p):
        return "manifest_and_reports"
    if TEST_RE.search(p):
        return "tests"
    if PLAN_EVIDENCE_RE.search(p):
        return "plan_evidence_research_html"
    if SOURCE_RE.search(p):
        return "source"
    return "other"


def read_target_kind(path):
    p = (path or "").lstrip("./")
    if not p:
        return None
    if PLAN_EVIDENCE_RE.search(p):
        return "plan_evidence_research_html"
    if GUIDANCE_RE.search(p):
        return "agents_md"
    if SKILL_RE.search(p):
        return "skill_files"
    if MANIFEST_REPORT_RE.search(p):
        return "manifests"
    return None


def bash_read_kinds(command):
    kinds = set()
    if not READ_VERB_RE.search(command):
        return kinds
    for hit in DOCS_PATH_RE.findall(command):
        kind = read_target_kind(hit)
        if kind:
            kinds.add(kind)
    return kinds


def ts_of(record):
    value = record.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _blank(metrics, timed):
    m = {
        "assistant_turns": 0,
        "tool_calls": 0,
        "read_kinds": Counter(),
        "edits": Counter(),
        "first_edit_turn": None,
        "gate_test_seconds": 0.0,
        "tool_seconds": 0.0,
        "observed_wall_seconds": None,
        "self_verification_reads": None,
    }
    if not timed:
        m["gate_test_seconds"] = None
        m["tool_seconds"] = None
    return m


def parse_claude(streams, metrics, proof):
    turns = 0
    seen_msg = set()
    edit_turns = []
    written = set()
    self_reads = 0
    open_calls = {}
    stamps = []
    for stream in streams:
        try:
            handle = stream.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                stamp = ts_of(record)
                if stamp is not None:
                    stamps.append(stamp)
                if kind == "assistant":
                    message = record.get("message") or {}
                    mid = str(message.get("id") or "").strip()
                    if mid and mid in seen_msg:
                        continue
                    if mid:
                        seen_msg.add(mid)
                    turns += 1
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if (
                            not isinstance(block, dict)
                            or block.get("type") != "tool_use"
                        ):
                            continue
                        metrics["tool_calls"] += 1
                        name = str(block.get("name") or "")
                        args = block.get("input") or {}
                        if name in READ_TOOLS:
                            path = str(args.get("file_path") or args.get("path") or "")
                            hit = read_target_kind(path)
                            if name == "Read" and path and path in written:
                                self_reads += 1
                            if hit:
                                metrics["read_kinds"][hit] += 1
                                if len(proof) < 6:
                                    proof.append(
                                        {"tool": name, "path": path, "read_kind": hit}
                                    )
                        elif name == "Bash":
                            command = str(args.get("command") or "")
                            for hit in sorted(bash_read_kinds(command)):
                                metrics["read_kinds"][hit] += 1
                                if len(proof) < 6:
                                    proof.append(
                                        {
                                            "tool": "Bash",
                                            "path": command[:120],
                                            "read_kind": hit,
                                        }
                                    )
                            open_calls[str(block.get("id"))] = {
                                "gate": bool(GATE_TEST_RE.search(command)),
                                "start": stamp,
                            }
                        elif name in EDIT_TOOLS:
                            path = str(
                                args.get("file_path") or args.get("notebook_path") or ""
                            )
                            cls = edit_class(path)
                            metrics["edits"][cls] += 1
                            edit_turns.append(turns)
                            if path:
                                written.add(path)
                            if len(proof) < 6:
                                proof.append(
                                    {"tool": name, "path": path, "edit_class": cls}
                                )
                elif kind == "user":
                    content = (record.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if (
                            not isinstance(block, dict)
                            or block.get("type") != "tool_result"
                        ):
                            continue
                        call = open_calls.pop(str(block.get("tool_use_id")), None)
                        if call is None or call.get("start") is None or stamp is None:
                            continue
                        span = max(0.0, stamp - call["start"])
                        metrics["tool_seconds"] += span
                        if call.get("gate"):
                            metrics["gate_test_seconds"] += span
    metrics["assistant_turns"] = turns
    metrics["self_verification_reads"] = self_reads
    metrics["first_edit_turn"] = min(edit_turns) if edit_turns else None
    if len(stamps) >= 2:
        metrics["observed_wall_seconds"] = round(max(stamps) - min(stamps), 3)


def parse_codex(streams, metrics, proof):
    turns = 0
    edit_seen = 0
    for stream in streams:
        try:
            handle = stream.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    not isinstance(record, dict)
                    or record.get("type") != "item.completed"
                ):
                    continue
                item = record.get("item")
                if not isinstance(item, dict):
                    continue
                itype = str(item.get("type") or "")
                if itype == "agent_message":
                    turns += 1
                elif itype == "command_execution":
                    metrics["tool_calls"] += 1
                    command = str(item.get("command") or "")
                    for hit in sorted(bash_read_kinds(command)):
                        metrics["read_kinds"][hit] += 1
                    if len(proof) < 6:
                        proof.append(
                            {"tool": "command_execution", "path": command[:120]}
                        )
                elif itype == "file_change":
                    edit_seen += 1
                    metrics["tool_calls"] += 1
                    paths = []
                    changes = item.get("changes")
                    if isinstance(changes, list):
                        paths.extend(
                            str(change.get("path") or "")
                            for change in changes
                            if isinstance(change, dict)
                        )
                    else:
                        paths.append(str(item.get("path") or item.get("file") or ""))
                    for path in paths:
                        cls = edit_class(path)
                        metrics["edits"][cls] += 1
                        if len(proof) < 6:
                            proof.append(
                                {"tool": "file_change", "path": path, "edit_class": cls}
                            )
                    if metrics["first_edit_turn"] is None:
                        metrics["first_edit_turn"] = max(1, turns + 1)
    metrics["assistant_turns"] = turns


def load_population():
    if POPULATION_FILE.exists():
        frozen = json.loads(POPULATION_FILE.read_text())
        records = {entry["run_id"]: entry for entry in frozen["runs"]}
        return records, [entry["run_id"] for entry in frozen["runs"]]
    rebuilt, order = build_population()
    entries = [pinned(rid, rebuilt[rid]) for rid in order]
    POPULATION_FILE.write_text(
        json.dumps(
            {
                "note": "window run population, pinned on first build so the census re-runs byte for byte",
                "runs": entries,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    return {entry["run_id"]: entry for entry in entries}, order


def build_population():
    records = {}
    order = []
    conn = sqlite3.connect(str(STORE_DB))
    for run_id, payload in conn.execute(
        "select run_id, payload from runs where run_id like 'r-2026%'"
    ):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        dispatched = str(data.get("dispatched_at") or "")
        if not (WINDOW_START <= dispatched < WINDOW_END):
            continue
        if str(data.get("project") or "") not in PROJECTS:
            continue
        records[run_id] = data
        order.append(run_id)
    conn.close()
    for project in PROJECTS:
        ledger = CODE_ROOT / project / "docs/state" / project / "crew.json"
        try:
            data = json.loads(ledger.read_text())["data"]["runs"]
        except (OSError, KeyError, json.JSONDecodeError):
            continue
        for run in data:
            run_id = str(run.get("run_id") or "")
            dispatched = str(run.get("dispatched_at") or "")
            if not run_id or run_id in records:
                continue
            if WINDOW_START <= dispatched < WINDOW_END:
                records[run_id] = run
                order.append(run_id)
    return records, sorted(set(order))


def pinned(run_id, record):
    """The few fields the census reads, so the pinned population stays small."""
    agent = record.get("agent") or {}
    return {
        "run_id": run_id,
        "project": str(record.get("project") or ""),
        "node": str(record.get("node") or ""),
        "role": str(record.get("role") or ""),
        "dispatched_at": str(record.get("dispatched_at") or ""),
        "agent": {
            "backend": str(agent.get("backend") or record.get("backend") or ""),
            "model": str(agent.get("model") or ""),
        },
    }


def promotion_verdicts():
    verdicts = {}
    pattern = re.compile(r"^promote\((r-[^)]+)\):\s*(.*)$")
    for project in PROJECTS:
        repo = CODE_ROOT / project
        try:
            log = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    "--since=2026-09-12",
                    "--until=" + CAPTURE_INSTANT,
                    "--format=%s",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=180,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for line in log.splitlines():
            match = pattern.match(line.strip())
            if match:
                verdicts[match.group(1)] = match.group(2).strip()
    return verdicts


def manifest_status(run_id):
    manifest = RUNS_ROOT / run_id / "manifest.md"
    try:
        text = manifest.read_text(errors="ignore")
    except OSError:
        return None
    match = re.search(r"^status:\s*(\S+)", text, re.MULTILINE)
    return match.group(1).strip().lower() if match else None


def classify_outcome(run_id, verdicts, statuses):
    verdict = verdicts.get(run_id)
    if verdict == "passed":
        return "promoted_passed"
    if verdict == "not-run":
        return "promoted_not_run"
    if verdict in {"failed", "malformed-node"}:
        return "failed"
    status = statuses.get(run_id)
    if status == "blocked":
        return "blocked"
    if status == "failed":
        return "failed"
    if status in {"complete", "completed"}:
        return "complete_unpromoted"
    return "no_terminal_record"


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def summarise(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "median": None, "p90": None}
    return {
        "n": len(vals),
        "median": round(
            float(
                sorted(vals)[len(vals) // 2]
                if len(vals) % 2
                else (sorted(vals)[len(vals) // 2 - 1] + sorted(vals)[len(vals) // 2])
                / 2
            ),
            4,
        ),
        "p90": round(float(percentile(vals, 0.9)), 4),
    }


def main():
    records, run_ids = load_population()
    verdicts = promotion_verdicts()
    statuses = {rid: manifest_status(rid) for rid in run_ids}
    per_run = {}
    dialect_by_backend = Counter()
    no_stream = 0
    for run_id in run_ids:
        run_dir = RUNS_ROOT / run_id
        base = run_dir / "stream.jsonl"
        if not base.exists():
            no_stream += 1
            continue
        streams = run_streams(base)
        first = None
        for stream in streams:
            try:
                with stream.open(encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        try:
                            candidate = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(candidate, dict):
                            first = candidate
                            break
                if first is not None:
                    break
            except OSError:
                continue
        dialect = _dialect(first) if first is not None else None
        timed = dialect == "claude"
        metrics = _blank({}, timed)
        proof_sample = []
        if dialect == "claude":
            parse_claude(streams, metrics, proof_sample)
        elif dialect == "codex":
            parse_codex(streams, metrics, proof_sample)
        else:
            continue
        data = records[run_id]
        agent = data.get("agent") or {}
        lane = lane_of(str(agent.get("backend") or data.get("backend") or ""))
        dialect_by_backend[(lane, dialect)] += 1
        first_input = None
        for stream in streams:
            first_input = _first_turn_input(stream, dialect)
            if first_input is not None:
                break
        per_run[run_id] = {
            "lane": lane,
            "role": str(data.get("role") or "unknown"),
            "outcome": classify_outcome(run_id, verdicts, statuses),
            "dialect": dialect,
            "backend": str(agent.get("backend") or data.get("backend") or ""),
            "model": str(agent.get("model") or ""),
            "project": str(data.get("project") or ""),
            "first_request_input_tokens": first_input,
            "assistant_turns": metrics["assistant_turns"],
            "tool_calls": metrics["tool_calls"],
            "first_edit_turn": metrics["first_edit_turn"],
            "read_kinds": dict(metrics["read_kinds"]),
            "record_keeping_reads": sum(metrics["read_kinds"].values()),
            "edits": dict(metrics["edits"]),
            "self_verification_reads": metrics["self_verification_reads"],
            "gate_test_seconds": metrics["gate_test_seconds"],
            "tool_seconds": metrics["tool_seconds"],
            "observed_wall_seconds": metrics["observed_wall_seconds"],
        }
    # aggregate
    metric_names = (
        "first_request_input_tokens",
        "assistant_turns",
        "first_edit_turn",
        "self_verification_reads",
    )
    share_names = (
        "read_share",
        "edit_share_source",
        "edit_share_tests",
        "edit_share_plan_evidence_research_html",
        "edit_share_figures",
        "edit_share_manifest_and_reports",
        "edit_share_other",
        "gate_share_of_wall",
        "gate_share_of_tool_time",
    )
    grouped = defaultdict(list)
    for run in per_run.values():
        grouped[(run["lane"], run["role"])].append(run)
    cells = {}
    for (lane, role), runs in sorted(grouped.items()):
        cell = {"n": len(runs), "metrics": {}}
        for name in metric_names:
            cell["metrics"][name] = summarise([r.get(name) for r in runs])
        shares = {name: [] for name in share_names}
        reads_pool = Counter()
        edits_pool = Counter()
        tools_pool = 0
        gate_pool = 0.0
        tool_sec_pool = 0.0
        wall_pool = 0.0
        for r in runs:
            tools_pool += r["tool_calls"]
            for kind, count in r["read_kinds"].items():
                reads_pool[kind] += count
            for cls, count in r["edits"].items():
                edits_pool[cls] += count
            if r["tool_calls"]:
                shares["read_share"].append(r["record_keeping_reads"] / r["tool_calls"])
            total_edits = sum(r["edits"].values())
            if total_edits:
                for cls in EDIT_CLASSES:
                    shares["edit_share_" + cls].append(
                        r["edits"].get(cls, 0) / total_edits
                    )
            if r["gate_test_seconds"] is not None and r["observed_wall_seconds"]:
                shares["gate_share_of_wall"].append(
                    r["gate_test_seconds"] / r["observed_wall_seconds"]
                )
                gate_pool += r["gate_test_seconds"]
                wall_pool += r["observed_wall_seconds"]
            if r["gate_test_seconds"] is not None and r["tool_seconds"]:
                shares["gate_share_of_tool_time"].append(
                    r["gate_test_seconds"] / r["tool_seconds"]
                )
                tool_sec_pool += r["tool_seconds"]
        for name in share_names:
            cell["metrics"][name] = summarise(shares[name])
        cell["pooled"] = {
            "tool_calls": tools_pool,
            "record_keeping_reads": sum(reads_pool.values()),
            "record_keeping_read_share": round(sum(reads_pool.values()) / tools_pool, 4)
            if tools_pool
            else None,
            "reads_by_kind": dict(sorted(reads_pool.items())),
            "edits": sum(edits_pool.values()),
            "edits_by_class": dict(sorted(edits_pool.items())),
            "gate_test_seconds": round(gate_pool, 1),
            "tool_seconds": round(tool_sec_pool, 1),
            "observed_wall_seconds": round(wall_pool, 1),
            "gate_share_of_wall": round(gate_pool / wall_pool, 4)
            if wall_pool
            else None,
        }
        cells[f"{lane}/{role}"] = cell
    strata = defaultdict(list)
    for run in per_run.values():
        strata[(run["lane"], run["role"], run["outcome"])].append(run)
    outcome_cells = {}
    for (lane, role, outcome), runs in sorted(strata.items()):
        outcome_cells[f"{lane}/{role}/{outcome}"] = {
            "n": len(runs),
            "assistant_turns": summarise([r["assistant_turns"] for r in runs]),
            "tool_calls": summarise([r["tool_calls"] for r in runs]),
            "first_edit_turn": summarise([r["first_edit_turn"] for r in runs]),
            "first_request_input_tokens": summarise(
                [r["first_request_input_tokens"] for r in runs]
            ),
            "read_share": summarise(
                [
                    r["record_keeping_reads"] / r["tool_calls"]
                    for r in runs
                    if r["tool_calls"]
                ]
            ),
            "in_window": True,
        }
    # parser proof: one named healthy run per schema
    proof = {}
    for dialect in DIALECTS:
        candidates = sorted(
            rid
            for rid, r in per_run.items()
            if r["dialect"] == dialect
            and r["outcome"] == "promoted_passed"
            and r["assistant_turns"] >= 5
            and r["tool_calls"] >= 20
        )
        chosen = candidates[0] if candidates else None
        if chosen is None:
            raise SystemExit(f"no healthy {dialect} run found for the parser proof")
        run = per_run[chosen]
        proof[dialect] = {
            "run_id": chosen,
            "finding": run["outcome"],
            "dialect": dialect,
            "assistant_turns": run["assistant_turns"],
            "tool_calls": run["tool_calls"],
            "first_edit_turn": run["first_edit_turn"],
            "reads_by_kind": run["read_kinds"],
            "edits_by_class": run["edits"],
        }
        if run["assistant_turns"] == 0 or run["tool_calls"] == 0:
            raise SystemExit(
                f"parser proof failed for {dialect}: {chosen} parsed empty"
            )
    payload = {
        "window": {
            "start": WINDOW_START + "Z",
            "end": WINDOW_END + "Z",
            "filter": "dispatched_at",
        },
        "population": {
            "window_runs": len(run_ids),
            "streams_parsed": len(per_run),
            "no_stream_file": no_stream,
            "projects": list(PROJECTS),
            "dialect_by_lane": {
                f"{lane}/{dialect}": n
                for (lane, dialect), n in sorted(dialect_by_backend.items())
            },
            "outcomes": dict(
                sorted(Counter(r["outcome"] for r in per_run.values()).items())
            ),
        },
        "lanes": sorted({r["lane"] for r in per_run.values()}),
        "cells": cells,
        "outcome_strata": outcome_cells,
        "parser_proof": proof,
        "definitions": {
            "lane": "run record agent.backend, codex-* folded to codex, claude* to claude",
            "role": "run record role",
            "outcome": "promotion commit verdict first, else manifest status",
            "first_request_input_tokens": "first assistant record usage, charged input; codex grammar publishes none",
            "read_share": "tool calls reading docs plan/evidence/research HTML, AGENTS.md/CLAUDE.md, skill files or manifests, over all tool calls",
            "edit classes": "tests before source; docs/figures; docs/research/data, docs/reports, docs/state and manifest.md; docs plan/evidence/research HTML",
            "gate_share_of_wall": "seconds inside pytest/ctest/ruff/scons/mypy commands over observed run wall time (claude dialect only)",
            "self_verification_reads": "Read of a path written earlier in the same run's streams",
        },
    }
    text = json.dumps(payload, indent=1, sort_keys=True)
    OUT.write_text(text + "\n")
    print(f"wrote {OUT} ({len(text)} bytes); runs={len(run_ids)} parsed={len(per_run)}")


if __name__ == "__main__":
    main()
