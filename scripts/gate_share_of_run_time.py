"""Measure how much of an implement run's wall time its gates spend.

A run's gates are found in two places and joined. Cited log paths
(``test_logs``, ``baseline_suite.log_path``, ``after_suite.log_path``) give a
gate whose duration is the runner's own reported figure. Test-command tool
calls in the run's stream give a gate whose duration is the wall time of the
foreground call that ran it. A tool call that wrote a cited log is the same
gate as that log, so the log's duration wins and the call is charged once.

The denominator is the span between the ledger row's own ``dispatched_at`` and
``completed_at`` stamps, as :func:`reckon.ledger.read_records` reports it under
``wall_seconds``. A run whose gate has neither a logged duration nor a
foreground tool call has no measurable gate share; it is listed and enters
neither median.

Reused owners: :func:`reckon.ledger.read_records`,
:data:`reckon.crew.promotion_gate._GATE_LOG_DURATION`,
:func:`reckon.crew.promotion_gate._recorded_gate_seconds`,
:func:`reckon.shadow_reads.stream_tool_calls`,
:func:`reckon.crew.reports.parse_manifest`, and
:func:`reckon.flight.resolve` for the resolved watchdog fence.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import flight, ledger
from reckon.crew import promotion_gate
from reckon.crew.reports import parse_manifest
from reckon.crew.runs import run_dir
from reckon.shadow_reads import stream_tool_calls

_PARTIAL_COUNT = re.compile(r"\b(\d+) of (\d+)\b")
_STREAM_GLOBS = ("stream.jsonl", "resume-*.jsonl", "attempt-*.jsonl")


def _stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stream_paths(run_id: str) -> list[Path]:
    directory = run_dir(run_id)
    found: list[Path] = []
    for pattern in _STREAM_GLOBS:
        found.extend(sorted(directory.glob(pattern)))
    seen: set[Path] = set()
    unique = []
    for path in found:
        if path not in seen and path.is_file():
            seen.add(path)
            unique.append(path)
    return unique


_NON_RUNNERS = frozenset(
    {
        "grep",
        "rg",
        "cat",
        "head",
        "tail",
        "less",
        "sed",
        "awk",
        "find",
        "ls",
        "echo",
        "git",
        "ruff",
        "which",
        "command",
        "test",
        "wc",
    }
)

# Programs a gate is launched through rather than by: each one is skipped, so
# the runner after it is what the segment is asked about.
_PREFIX_WRAPPERS = frozenset(
    {
        "env",
        "cd",
        "nohup",
        "time",
        "exec",
        "command",
        "setsid",
        "stdbuf",
        "uv",
        "uvx",
        "run",
        "srun",
        "salloc",
        "sbatch",
    }
)

_BASH_PROGRAMS = frozenset({"bash", "sh", "zsh", "dash"})

_SHELLS_OR_SCHEDULERS = _BASH_PROGRAMS | {"sbatch", "srun", "salloc"}

# The flags that make a shell take the command it is to run as an argument, so
# the words after them are a nested command line rather than this one's.
_SHELL_STRING_FLAGS = ("-ic", "-lc", "-cl", "-c", "--wrap")

# ``env`` options whose value is the next word rather than part of the flag.
_ENV_ARG_FLAGS = frozenset({"-u", "-C", "--unset", "--chdir"})


def _is_gate_command(command: str) -> bool:
    """Whether a shell call actually runs a test suite in any of its segments.

    A stream mentions pytest in greps, reads and commit messages far more often
    than it runs it, so the runner must be the program being executed: a
    segment's leading tokens, after a ``uv``/``srun``/``env``/assignment prefix
    and an optional ``timeout N``, name a test runner or the run's own gate
    script. Every segment of a compound line is asked, because a worker usually
    runs its gate behind assignments and a ``cd`` rather than alone, and a
    shell's ``bash -lc "<script>"`` carries the runner one level down.
    """
    return _runs_a_gate(command, depth=0)


def _runs_a_gate(command: str, *, depth: int) -> bool:
    if depth > 3:
        return False
    return any(
        _segment_runs_a_gate(segment, depth=depth) for segment in _segments(command)
    )


def _segments(command: str) -> list[list[str]]:
    """Split a command line into the words of each simple command it runs.

    ``shlex`` is asked for its punctuation so ``;``, ``&&``, ``||`` and ``|``
    arrive as their own tokens; a token made only of those characters ends one
    segment. A line with no separator yields the whole line as one segment.
    """
    segments: list[list[str]] = [[]]
    for line in command.splitlines() or [command]:
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = line.split()
        for token in tokens:
            if token and set(token) <= set(";&|"):
                segments.append([])
            else:
                segments[-1].append(token)
    return [segment for segment in segments if segment]


def _segment_runs_a_gate(tokens: list[str], *, depth: int) -> bool:
    index = 0
    while index < len(tokens):
        word = tokens[index]
        if word in _PREFIX_WRAPPERS:
            index += 1
            if word == "env":
                # ``env -u NAME`` and ``env -C DIR`` take their own argument.
                while index + 1 < len(tokens) and tokens[index] in _ENV_ARG_FLAGS:
                    index += 2
        elif word == "timeout":
            index += 1
            if index < len(tokens) and tokens[index].isdigit():
                index += 1
        elif word in _SHELL_STRING_FLAGS and any(
            Path(candidate).name in _SHELLS_OR_SCHEDULERS for candidate in tokens
        ):
            # ``bash -lc "<script>"`` and ``sbatch --wrap "<script>"`` carry a
            # whole command line as one word; the runner is inside that word.
            if index + 1 < len(tokens):
                return _runs_a_gate(tokens[index + 1], depth=depth + 1)
            return False
        elif word.startswith("-") or ("=" in word and not word.startswith("=")):
            index += 1
        else:
            break
    rest = tokens[index:]
    if not rest:
        return False
    program = Path(rest[0]).name
    if program in _BASH_PROGRAMS:
        if not any(flag in rest for flag in _SHELL_STRING_FLAGS):
            return False
        inner = rest[-1]
        return inner != program and _runs_a_gate(inner, depth=depth + 1)
    if program in _NON_RUNNERS:
        return False
    if program in {"pytest", "ctest", "nosetests", "py.test"}:
        return True
    if program.startswith("python") and "-m" in rest:
        position = rest.index("-m") + 1
        module = rest[position] if position < len(rest) else ""
        return module in {"pytest", "py.test", "unittest"}
    return bool(re.search(r"gate", program, re.IGNORECASE))


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _bash_calls_with_timing(run_id: str) -> list[dict[str, Any]]:
    """Each bash tool call in a run's streams, with the span it ran for.

    A call's span is the gap between the assistant record that issued it and
    the user record carrying its result. A call with no result yet (still
    running when the attempt ended) has no span and is reported as such, so a
    backgrounded gate is not charged the time it happened to be open.
    """
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for path in _stream_paths(run_id):
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                message = event.get("message")
                content = (
                    message.get("content") if isinstance(message, Mapping) else None
                )
                if not isinstance(content, list):
                    continue
                when = _stamp(event.get("timestamp"))
                for block in content:
                    if not isinstance(block, Mapping):
                        continue
                    kind = block.get("type")
                    if kind == "tool_use" and block.get("name") == "Bash":
                        payload = block.get("input") or {}
                        command = str(payload.get("command") or "").strip()
                        call_id = str(block.get("id") or "")
                        if not call_id or not command:
                            continue
                        calls[call_id] = {
                            "command": command,
                            "start": when,
                            "background": bool(payload.get("run_in_background")),
                            "end": None,
                        }
                        order.append(call_id)
                    elif kind == "tool_result":
                        call_id = str(block.get("tool_use_id") or "")
                        entry = calls.get(call_id)
                        if entry is not None and entry["end"] is None:
                            entry["end"] = when
                            text = block.get("content")
                            if (
                                isinstance(text, str)
                                and "running in background" in text
                            ):
                                entry["background"] = True
    result = []
    for call_id in order:
        entry = calls[call_id]
        start, end = entry["start"], entry["end"]
        seconds = (
            (end - start).total_seconds()
            if start is not None and end is not None
            else None
        )
        result.append(
            {
                "command": entry["command"],
                "seconds": seconds,
                "background": entry["background"],
            }
        )
    return result


def _manifest_of(record: Mapping[str, Any]) -> dict[str, Any]:
    path = str(record.get("manifest_path") or "").strip()
    if not path:
        return {}
    try:
        return dict(parse_manifest(Path(path).read_text(encoding="utf-8"), path=path))
    except (OSError, ValueError):
        return {}


def _resolve_cited(value: Any, manifest_path: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and manifest_path:
        return Path(manifest_path).expanduser().parent / path
    return path


def _cited_logs(record: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[Path]:
    """Distinct log paths the run cites, each read once."""
    manifest_path = str(record.get("manifest_path") or "")
    raw: list[Any] = list(manifest.get("test_logs") or [])
    for field in ("baseline_suite", "after_suite"):
        suite = manifest.get(field)
        if isinstance(suite, Mapping) and suite.get("log_path"):
            raw.append(suite["log_path"])
    seen: set[Path] = set()
    out: list[Path] = []
    for value in raw:
        text = str(value or "").strip()
        if not text:
            continue
        resolved = _resolve_cited(text, manifest_path)
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _log_seconds(run_id: str, path: Path) -> float | None:
    """The duration a cited gate log records, reusing the promotion reader.

    ``_recorded_gate_seconds`` reads the cited path and, when it cannot, the
    copy promotion preserves under the run directory, so a run whose worktree
    was released still carries a duration. Reading it here rather than
    open-coding the pattern keeps one definition of "the runner's duration".
    """
    return promotion_gate._recorded_gate_seconds(run_id, {"log_path": str(path)})


def _partial_test_count(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for first, second in _PARTIAL_COUNT.findall(str(text)):
        if int(first) < int(second):
            return True
    return False


def _truncation_markers(commands: Iterable[str], logs: Iterable[Path]) -> list[str]:
    """Markers that show a gate was cut short, read from gate commands only.

    A stream carries every shell call, so a ``-x`` on some unrelated tool is not
    evidence about the gate. Only a command that runs a test suite is read, and
    its flags are tokenised so ``-x`` means to a pytest run what it means there.
    """
    markers: list[str] = []
    for command in commands:
        if not _is_gate_command(command):
            continue
        tokens = _tokens(command)
        joined = " ".join(tokens)
        if (
            tokens and Path(tokens[0]).name == "timeout"
        ) or " timeout " in f" {joined}":
            markers.append("timeout")
        if "-x" in tokens or any(t.startswith("--exitfirst") for t in tokens):
            markers.append("-x")
        if any(t.startswith("--maxfail") for t in tokens):
            markers.append("--maxfail")
    markers.extend("partial test count" for log in logs if _partial_test_count(log))
    return sorted(set(markers))


def _call_wrote_log(command: str, log: Path, log_names: Iterable[str]) -> bool:
    """Whether this tool call is the call that produced this cited log.

    The absolute path is the strong match. A stream usually records the command
    with a shell variable in place of the run directory, so the file name is the
    fallback, and only when it names one of the run's cited logs alone: two
    ``gate.log`` files beside each other are not joined by name.
    """
    if str(log) in command:
        return True
    name = log.name
    return name in command and sum(1 for other in log_names if other == name) == 1


def measure_run(record: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(record.get("run_id") or "")
    manifest = _manifest_of(record)
    logs = _cited_logs(record, manifest)
    calls = _bash_calls_with_timing(run_id)
    stream_commands = [
        text
        for kind, text in stream_tool_calls(_stream_paths(run_id))
        if kind == "bash"
    ]
    # A codex stream records each shell call as a completed item with no
    # timestamp, so its calls are visible to the stream reader but carry no
    # span. Reporting that as "no tool-call timing" keeps it distinct from a
    # run that simply made no gate call.
    if calls:
        tool_call_timing = "timestamped"
    elif stream_commands:
        tool_call_timing = "none: streams carry no timestamps"
    else:
        tool_call_timing = "no tool calls"

    gates: list[dict[str, Any]] = []
    joined_call_ids: set[int] = set()
    # A command usually reaches its log through a shell variable (``$R/logs/x.log``)
    # rather than by the absolute path, so the log is joined by its file name
    # when that name is this run's alone. Without it the same gate is charged
    # twice: once at the log's duration and again as the call that wrote it.
    log_names = [log.name for log in logs]
    logs_without_duration: list[str] = []
    for log in logs:
        seconds = _log_seconds(run_id, log)
        writers = [
            index
            for index, call in enumerate(calls)
            if _call_wrote_log(call["command"], log, log_names)
        ]
        if (
            seconds is None
            and log.is_file()
            and not any(_is_gate_command(calls[index]["command"]) for index in writers)
        ):
            # A cited log the run produced but no test runner wrote is an
            # artifact (a soak, a census, a probe), not a gate. Counting it as
            # an unmeasured gate would make an otherwise fully readable run
            # unmeasurable, so it is named instead of charged.
            logs_without_duration.append(str(log))
            continue
        gates.append(
            {
                "source": "log",
                "path": str(log),
                "seconds": seconds,
                "background": False,
            }
        )
        joined_call_ids.update(writers)

    for index, call in enumerate(calls):
        if index in joined_call_ids:
            continue
        if not _is_gate_command(call["command"]):
            continue
        if not call["background"] and call["seconds"] is None:
            continue
        gates.append(
            {
                "source": "tool-call",
                "path": None,
                "command": call["command"][:200],
                "seconds": None if call["background"] else call["seconds"],
                "background": call["background"],
            }
        )

    wall = record.get("wall_seconds")
    measured = [g["seconds"] for g in gates if g["seconds"] is not None]
    unmeasured = [g for g in gates if g["seconds"] is None]
    all_from_logs = bool(gates) and all(
        g["source"] == "log" and g["seconds"] is not None for g in gates
    )
    measurable = bool(gates) and not unmeasured and wall
    fallback_used = any(g["source"] == "tool-call" for g in gates)
    truncated = _truncation_markers(stream_commands, logs)
    suite = manifest.get("baseline_suite")
    if isinstance(suite, Mapping) and suite.get("completed") is False:
        truncated.append("baseline arm incomplete")

    return {
        "run_id": run_id,
        "lane": str(record.get("lane") or record.get("backend") or ""),
        "project": str(record.get("_project") or ""),
        "wall_seconds": wall,
        "gate_seconds": sum(measured) if measured else 0.0,
        "share": (sum(measured) / wall) if measurable else None,
        "gates": len(gates),
        "gates_measured": len(measured),
        "all_from_logs": all_from_logs,
        "fallback_used": fallback_used,
        "unmeasurable": not measurable,
        "truncated": truncated,
        "background": any(g["background"] for g in gates),
        "logs_without_duration": logs_without_duration,
        "tool_call_timing": tool_call_timing,
        "wall_covers_attempts": _wall_covers_attempts(record),
    }


def _wall_covers_attempts(record: Mapping[str, Any]) -> bool | None:
    """Whether the row's wall span covers every attempt the run made.

    Read against the per-attempt exit records the supervisor writes: a run
    whose last attempt exited after ``completed_at``, or whose first attempt
    launched before ``dispatched_at``, has a span that misses an attempt.
    """
    run_id = str(record.get("run_id") or "")
    directory = run_dir(run_id)
    stamps = []
    for path in sorted(directory.glob("attempt-*-exit.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("launched_at", "exited_at"):
            value = _stamp(payload.get(key))
            if value is not None:
                stamps.append(value)
    if len(stamps) < 2:
        return None
    started = _stamp(record.get("dispatched_at"))
    finished = _stamp(record.get("completed_at"))
    if started is None or finished is None:
        return None
    return min(stamps) >= started - timedelta(seconds=5) and max(
        stamps
    ) <= finished + timedelta(seconds=5)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def measure(project: str, *, days: int, now: datetime) -> dict[str, Any]:
    since = (now - timedelta(days=days)).isoformat()
    rows, version = ledger.read_records(project, since=since)
    implement = [r for r in rows if str(r.get("role") or "") == "implement"]
    measured = []
    for record in implement:
        entry = measure_run({**record, "_project": project})
        measured.append(entry)
    return {
        "project": project,
        "ledger_version": version,
        "since": since,
        "until": now.isoformat(),
        "runs": len(implement),
        "measured": measured,
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        for entry in result["measured"]:
            by_lane[entry["lane"] or "unknown"].append(entry)
    table = []
    for lane, entries in sorted(by_lane.items()):
        all_log = [
            e["share"] for e in entries if e["all_from_logs"] and e["share"] is not None
        ]
        every = [e["share"] for e in entries if e["share"] is not None]
        table.append(
            {
                "lane": lane,
                "runs": len(entries),
                "median_all_log": _median(all_log),
                "all_log_runs": len(all_log),
                "median_measurable": _median(every),
                "measurable_runs": len(every),
                "fallback_share": (
                    round(
                        sum(1 for e in entries if e["fallback_used"]) / len(entries), 3
                    )
                    if entries
                    else None
                ),
                "tool_call_timing": {
                    "no_timestamps": sum(
                        1 for e in entries if e["tool_call_timing"].startswith("none")
                    ),
                    "no_calls": sum(
                        1 for e in entries if e["tool_call_timing"] == "no tool calls"
                    ),
                },
                "unmeasurable": sorted(
                    e["run_id"] for e in entries if e["unmeasurable"]
                ),
                "truncated": sorted(e["run_id"] for e in entries if e["truncated"]),
                "background": sorted(e["run_id"] for e in entries if e["background"]),
                "wall_misses_attempts": sorted(
                    e["run_id"] for e in entries if e["wall_covers_attempts"] is False
                ),
                "logs_without_duration": sorted(
                    e["run_id"] for e in entries if e["logs_without_duration"]
                ),
            }
        )
    return {"lanes": table}


def _watchdog_fences() -> list[dict[str, Any]]:
    rows = []
    for project, docs in sorted(flight.mounted_project_docs().items()):
        checkout = Path(docs).expanduser().parent
        try:
            resolved = flight.resolve(project, checkout_path=checkout)
            config = resolved.config
            fences = config.get("fences") or {}
            value = fences.get("enforce_budget_watchdog")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            rows.append({"project": project, "error": str(exc)})
            continue
        rows.append({"project": project, "resolved": value})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--now", default=None, help="ISO instant the window ends at")
    parser.add_argument("--project", action="append", default=None)
    parser.add_argument("--json", default=None, help="write the full result here")
    parser.add_argument(
        "--control-run", default=None, help="report one run's own gate seconds"
    )
    parser.add_argument(
        "--fences", action="store_true", help="report the resolved watchdog fence"
    )
    parser.add_argument(
        "--control-detector",
        action="store_true",
        help="show the gate detector's answers on command shapes whose answer is known",
    )
    args = parser.parse_args(argv)

    now = _stamp(args.now) or datetime.now(UTC)
    if args.control_detector:
        return _report_detector()
    if args.control_run:
        return _report_control(args.control_run)
    if args.fences:
        print(json.dumps(_watchdog_fences(), indent=2))
        return 0

    projects = args.project or sorted(flight.mounted_project_docs())
    results = [measure(project, days=args.days, now=now) for project in projects]
    summary = summarise(results)
    payload = {
        "read_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "projects": results,
    }
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


_BRACKET_DURATION = re.compile(r"\((\d+):(\d{2}):(\d{2})\)")


def _bracket_seconds(path: Path) -> float | None:
    """The h:mm:ss figure a runner prints beside its seconds summary.

    It is a second, independently formatted field of the same log line, so a
    scanner that read the wrong number (a test count, a date) would disagree
    with it rather than agree with itself.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = _BRACKET_DURATION.findall(text)
    if not matches:
        return None
    hours, minutes, seconds = matches[-1]
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _duration_lines(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(promotion_gate._GATE_LOG_DURATION.findall(text))


def _report_control(run_id: str) -> int:
    """Show the timing scanner agreeing with an independent figure, or failing.

    The scanned figure comes from the runner's ``in N s`` summary; the
    comparison is the ``(h:mm:ss)`` field the same log prints beside it. Two
    differently formatted fields must agree within 5%, so the control can fail.
    A log carrying several summaries (several arms appended to one file) has no
    single figure to agree with, so it is listed as not comparable rather than
    scored against the wrong arm.
    """
    record = _control_record(run_id)
    entry = measure_run(record)
    manifest = _manifest_of(record)
    logs = _cited_logs(record, manifest)
    findings = []
    not_comparable = []
    ok = True
    for log in logs:
        scanned = _log_seconds(run_id, log)
        independent = _bracket_seconds(log)
        if scanned is None or independent is None:
            continue
        if _duration_lines(log) > 1:
            not_comparable.append(str(log))
            continue
        delta = abs(scanned - independent) / independent if independent else 1.0
        within = delta <= 0.05
        ok = ok and within
        findings.append(
            {
                "log": str(log),
                "scanned_seconds": scanned,
                "independent_bracket_seconds": independent,
                "relative_delta": round(delta, 5),
                "within_5pct": within,
            }
        )
    if not findings:
        ok = False
    print(
        json.dumps(
            {
                "run_id": run_id,
                "run": entry,
                "control": findings,
                "not_comparable": not_comparable,
                "passes": ok,
            },
            indent=2,
        )
    )
    return 0 if ok else 1


def _control_record(run_id: str) -> dict[str, Any]:
    for project in flight.mounted_project_docs():
        rows, _ = ledger.read_records(project)
        for row in reversed(rows):
            if row.get("run_id") == run_id:
                return {**row, "_project": project}
    return {
        "run_id": run_id,
        "manifest_path": str(run_dir(run_id) / "manifest.md"),
        "wall_seconds": None,
    }


# Command shapes the detector must classify, each paired with its answer. Both
# directions are present, so a detector that answered yes to everything fails
# on the mentions, and one that only read the line's first program fails on the
# compound rows. The shapes are the ones workers on this host actually write.
_DETECTOR_CASES: tuple[tuple[str, bool], ...] = (
    ("pytest -q tests/test_a.py", True),
    (".venv/bin/python -m pytest -q tests/a.py", True),
    ("PYTHONPATH=. .venv/bin/python -m pytest -q", True),
    ("uv run pytest -q tests/test_a.py", True),
    ("uv run --no-sync pytest tests", True),
    ("env -u PYTHONPATH python -m pytest tests", True),
    ("timeout 560 .venv/bin/python -m pytest tests", True),
    ("ctest --output-on-failure", True),
    ("bash -lc 'uv run pytest tests'", True),
    ("srun --partition=all_debug bash -lc 'cd w; uv run pytest -q'", True),
    (
        (
            'D=$(mktemp -d); git archive HEAD | tar -x -C "$D"; cd "$D" && '
            'PYTHONPATH="$D" timeout 600 /repo/.venv/bin/python -m pytest -q tests'
        ),
        True,
    ),
    ("grep -rn pytest reck", False),
    ("git commit -m 'ran pytest on the module'", False),
    ("python -c 'import pytest'", False),
    ("sed -n '1,20p' tests/test_a.py", False),
    ("uv pip install pytest-xdist", False),
    ("git status", False),
)


def _report_detector() -> int:
    """Show the gate detector's answers on shapes whose answer is known."""
    wrong = []
    for command, expected in _DETECTOR_CASES:
        found = _is_gate_command(command)
        print(f"{'ok ' if found == expected else 'BAD'} {found!s:5} {command}")
        if found != expected:
            wrong.append(command)
    print(f"{len(_DETECTOR_CASES) - len(wrong)} of {len(_DETECTOR_CASES)} as expected")
    return 0 if not wrong else 1


if __name__ == "__main__":
    sys.exit(main())
