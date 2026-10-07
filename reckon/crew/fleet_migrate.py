"""Read a fleet census and keep its migration checkpoints on shared storage."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon.crew import fleet_node, placement, runs
from reckon.crew.fleet_supervisor import (
    REQUEST_FIFO_NAME,
    runtime_directory,
    state_directory,
)
from reckon.crew.recovery import _resume_remedy
from reckon.crew.resumption import resolve_session


class MigrationError(RuntimeError):
    """A migration checkpoint cannot safely advance."""


class SessionVerificationError(MigrationError):
    """A resumed session did not match its recorded layout and process census."""

    def __init__(self, message: str, observed: dict[str, Any]) -> None:
        super().__init__(message)
        self.observed = observed


_TAB = re.compile(r'^\s*tab name="([^"]+)"')
_CWD = re.compile(r'\bcwd(?:=| )"([^"]+)"')
_PANE = re.compile(r"^\s*pane(?:\s|\{|$)")
_COMMAND = re.compile(r'\bcommand="([^"]+)"')
_ARGS = re.compile(r'^\s*args(?:\s+"[^"]*")+')
_QUOTED = re.compile(r'"((?:\\.|[^"\\])*)"')
_CONTENTS_FILE = re.compile(r'\s+contents_file="(?:\\.|[^"\\])*"')


def parse_layout(source: str) -> list[dict[str, Any]]:
    """Read ordered tabs and terminal panes from a zellij layout dump."""
    tabs: list[dict[str, Any]] = []
    root_cwd = ""
    tab: dict[str, Any] | None = None
    pane: dict[str, Any] | None = None
    depth = 0
    tab_depth = 0
    pane_depth = 0
    for line in source.splitlines():
        structure = _QUOTED.sub('""', line)
        opened = structure.count("{")
        closed = structure.count("}")
        if tab is None and (match := _CWD.search(line)):
            root_cwd = match.group(1)
        if match := _TAB.match(line):
            tab = {"name": match.group(1), "panes": []}
            tabs.append(tab)
            tab_depth = depth + opened
        elif tab is not None and (match := _PANE.match(line)):
            command = _COMMAND.search(line)
            cwd = _CWD.search(line)
            pane = {
                "command": command.group(1) if command else None,
                "cwd": cwd.group(1) if cwd else root_cwd,
            }
            tab["panes"].append(pane)
            pane_depth = depth + opened
        elif pane is not None:
            if match := _CWD.search(line):
                pane["cwd"] = match.group(1)
            elif _ARGS.match(line):
                pane["args"] = [
                    json.loads(f'"{value}"') for value in _QUOTED.findall(line)
                ]
            elif line.lstrip().startswith("plugin "):
                pane["plugin"] = True
        depth += opened - closed
        if pane is not None and depth < pane_depth:
            pane = None
        if tab is not None and depth < tab_depth:
            tab = None
    for item in tabs:
        item["panes"] = [pane for pane in item["panes"] if not pane.get("plugin")]
    return tabs


def _listed_sessions(
    *,
    invoke: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    check: bool = False,
    timeout: float | None = None,
) -> list[tuple[str, bool]]:
    """Read zellij's session list with each server's exited state."""
    options: dict[str, Any] = {"capture_output": True, "text": True, "check": check}
    if timeout is not None:
        options["timeout"] = timeout
    result = invoke(["zellij", "list-sessions", "--no-formatting"], **options)
    if result.returncode:
        raise MigrationError(f"zellij session list failed: {result.stderr.strip()}")
    sessions = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields:
            sessions.append((fields[0], "EXITED" in line))
    return sessions


def _live_claude_records(
    claude_sessions: Path,
    process_root: Path,
    *,
    skip_invalid: bool = False,
    coerce_pid: bool = False,
) -> dict[str, dict[str, Any]]:
    """Join session records to live processes by pid and process start."""
    records = {}
    for path in claude_sessions.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            if skip_invalid:
                continue
            raise
        try:
            conversation = record.get("sessionId")
            raw_pid = record.get("pid")
        except AttributeError:
            if skip_invalid:
                continue
            raise
        if coerce_pid:
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
        elif isinstance(raw_pid, int):
            pid = raw_pid
        else:
            continue
        if not conversation:
            continue
        try:
            start = (
                (process_root / str(pid) / "stat")
                .read_text()
                .rsplit(") ", 1)[1]
                .split()[19]
            )
        except (IndexError, OSError):
            continue
        if start != str(record.get("procStart")):
            continue
        records[conversation] = {
            "conversation": conversation,
            "cwd": record.get("cwd"),
            "pid": pid,
            "status": record.get("status"),
        }
    return records


def _local_observation(
    *,
    state: Path,
    claude_sessions: Path,
    transcript_root: Path,
    session: str | None = None,
    process_root: Path = Path("/proc"),
    invoke: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Read the old node's zellij and Claude records without changing them."""
    names = [
        name
        for name, exited in _listed_sessions(invoke=invoke, check=True)
        if (session is not None and name == session and not exited)
        or (session is None and name.endswith("-fleet"))
    ]
    if not names:
        raise MigrationError(
            "zellij reported no sessions; census cannot prove an empty fleet"
        )
    layouts = {}
    for name in names:
        result = invoke(
            ["zellij", "--session", name, "action", "dump-layout"],
            capture_output=True,
            text=True,
            check=True,
        )
        if not parse_layout(result.stdout):
            raise MigrationError(f"layout dump for {name} has no tabs")
        layouts[name] = result.stdout
    records = _live_claude_records(claude_sessions, process_root)
    log = state / "sessions.tsv"
    starts = log.read_text().splitlines() if log.is_file() else []
    return {
        "layouts": layouts,
        "claude": records,
        "starts": starts,
        "transcript_root": str(transcript_root),
    }


def collect_local(session: str | None = None) -> dict[str, Any]:
    """Entry point run as a scheduler step on the old fleet node."""
    return _local_observation(
        state=state_directory(),
        claude_sessions=Path.home() / ".claude" / "sessions",
        transcript_root=Path.home() / ".claude" / "projects",
        session=session,
    )


def _remote_observation() -> dict[str, Any]:
    jobs = fleet_node.query_jobs(fleet_node.fleet_size().account)
    allocation = fleet_node.find_allocation(
        jobs, preferred=fleet_node.recorded_job_id()
    )
    if allocation is None:
        raise MigrationError("no held fleet allocation can host the census")
    argv = fleet_node.placement_argv(
        allocation, [sys.executable, "-m", "reckon.crew.fleet_migrate", "collect-local"]
    )
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise MigrationError(f"old-node census failed: {exc}") from exc


def _transcript_exists(root: Path, cwd: str, conversation: str) -> bool:
    direct = root / cwd.replace("/", "-") / f"{conversation}.jsonl"
    return direct.is_file() or any(root.glob(f"*/{conversation}.jsonl"))


def build_census(
    observation: Mapping[str, Any],
    *,
    transcript_exists: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Join layout order to Claude's live records and verify transcripts."""
    records = observation["claude"]
    transcript_root = Path(observation["transcript_root"])
    exists = transcript_exists or (
        lambda cwd, conversation: _transcript_exists(transcript_root, cwd, conversation)
    )
    sessions = []
    for name, source in observation["layouts"].items():
        tabs = parse_layout(source)
        for tab in tabs:
            for pane in tab["panes"]:
                if pane["command"] != "fleet-claude":
                    continue
                args = pane.get("args", [])
                if len(args) < 2 or args[0] not in {"--session-id", "--resume"}:
                    raise MigrationError(
                        f"{name}/{tab['name']}: Claude pane has no conversation id"
                    )
                conversation = args[1]
                record = records.get(conversation)
                if not record:
                    raise MigrationError(
                        f"{name}/{tab['name']}: no live Claude session record for {conversation}"
                    )
                if not record.get("cwd") or not exists(record["cwd"], conversation):
                    raise MigrationError(
                        f"{name}/{tab['name']}: transcript missing for {conversation}"
                    )
                pane["conversation"] = record["conversation"]
                pane["cwd"] = record["cwd"]
                pane.pop("args", None)
        sessions.append({"name": name, "tabs": tabs, "source_layout": source})
    starts = "\n".join(observation.get("starts", []))
    missing_starts = [
        pane["conversation"]
        for session in sessions
        for tab in session["tabs"]
        for pane in tab["panes"]
        if pane.get("conversation") and pane["conversation"] not in starts
    ]
    return {"sessions": sessions, "missing_start_log": missing_starts}


def render_layout(
    session: Mapping[str, Any], prompts: Mapping[str, str] | None = None
) -> str:
    """Keep zellij's layout and replace only pane state needed for resumption."""
    prompts = prompts or {}
    source = session.get("source_layout")
    if not isinstance(source, str) or not source:
        raise MigrationError(f"{session['name']}: census has no source layout dump")
    lines: list[str] = []
    depth = 0
    tab_depth = 0
    pane_depth = 0
    tab_index = 0
    tab: Mapping[str, Any] | None = None
    pane: Mapping[str, Any] | None = None
    claude_panes: Any = iter(())
    for original_line in source.splitlines(keepends=True):
        line = original_line
        structure = _QUOTED.sub('""', line)
        opened = structure.count("{")
        closed = structure.count("}")
        if match := _TAB.match(line):
            if tab_index >= len(session["tabs"]):
                raise MigrationError(
                    f"{session['name']}: source gained an unrecorded tab"
                )
            tab = session["tabs"][tab_index]
            tab_index += 1
            if tab["name"] != match.group(1):
                raise MigrationError(
                    f"{session['name']}: tab order changed since census"
                )
            claude_panes = iter(
                item for item in tab["panes"] if item.get("conversation")
            )
            tab_depth = depth + opened
        elif tab is not None and _PANE.match(line):
            command = _COMMAND.search(line)
            pane = (
                next(claude_panes, None)
                if command and command.group(1) == "fleet-claude"
                else None
            )
            pane_depth = depth + opened
            if pane is not None:
                line = _COMMAND.sub('command="fleet-claude"', line, count=1)
                cwd = f"cwd={json.dumps(pane['cwd'])}"
                if match := _CWD.search(line):
                    line = line[: match.start()] + cwd + line[match.end() :]
                else:
                    brace = line.find("{")
                    if brace < 0:
                        raise MigrationError(
                            f"{session['name']}: Claude pane has no body"
                        )
                    line = line[:brace].rstrip() + f" {cwd} " + line[brace:]
            else:
                line = _CONTENTS_FILE.sub("", line)
        elif pane is not None:
            if _ARGS.match(line):
                indent = line[: len(line) - len(line.lstrip())]
                line = f'{indent}args "--resume" {json.dumps(pane["conversation"])}'
                if prompt := prompts.get(pane["conversation"]):
                    line += f" {json.dumps(prompt)}"
                line += "\n"
            elif line.lstrip().startswith("start_suspended "):
                line = ""
        if _CONTENTS_FILE.search(line):
            line = _CONTENTS_FILE.sub("", line)
        lines.append(line)
        depth += opened - closed
        if pane is not None and depth < pane_depth:
            pane = None
        if tab is not None and depth < tab_depth:
            tab = None
    if tab_index != len(session["tabs"]):
        raise MigrationError(f"{session['name']}: source lost a recorded tab")
    return "".join(lines)


def _ledger_root(state: Path) -> Path:
    return state / "migration"


def _latest_ledger(state: Path) -> Path | None:
    candidates = sorted(_ledger_root(state).glob("move-*/ledger.json"))
    return candidates[-1] if candidates else None


def _new_ledger_path(state: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _ledger_root(state) / f"move-{stamp}" / "ledger.json"


def _read_runs() -> list[dict[str, Any]]:
    rows = []
    for pointer in runs.list_live():
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        resolution = resolve_session(
            run_id,
            record=pointer,
            project=str(pointer.get("project") or ""),
            root=pointer.get("repo"),
        )
        remedy = _resume_remedy(resolution, run_id)
        rows.append(
            {
                "run_id": run_id,
                "project": pointer.get("project"),
                "phase": pointer.get("phase"),
                "resume": (remedy or {}).get("command"),
                "session_id": resolution.get("session_id"),
            }
        )
    return rows


def _fleet_record(state: Path) -> dict[str, Any]:
    try:
        record = json.loads((state / fleet_node.RECORD_NAME).read_text())
    except (OSError, ValueError) as exc:
        raise MigrationError("the fleet record cannot be read") from exc
    if not isinstance(record, dict):
        raise MigrationError("the fleet record is not a mapping")
    return record


def _run_step(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)


def _active_sessions(
    invoke: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    return [
        name
        for name, exited in _listed_sessions(invoke=invoke, timeout=30)
        if not exited
    ]


def _end_local_session(
    name: str,
    *,
    run_step: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_step,
    active_sessions: Callable[[], list[str]] = _active_sessions,
) -> dict[str, Any]:
    """End a named old-node server and confirm it left the active list."""
    before = active_sessions()
    if name in before:
        result = run_step(["zellij", "kill-session", name])
        if result.returncode:
            raise MigrationError(
                f"zellij refused to end {name}: {result.stderr.strip()}"
            )
    if name in active_sessions():
        raise MigrationError(f"zellij session {name} remains on the old node")
    return {"session": name, "ended": True, "was_running": name in before}


def _local_session_processes(
    name: str,
    *,
    claude_sessions: Path | None = None,
    process_root: Path = Path("/proc"),
    active_sessions: Callable[[], list[str]] = _active_sessions,
    run_step: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_step,
    hostname: Callable[[], str] = socket.gethostname,
) -> dict[str, Any]:
    """Read the resumed panes and their live Claude process records."""
    node = hostname().split(".")[0]
    if name not in active_sessions():
        return {"session": name, "node": node, "panes": []}
    result = run_step(["zellij", "--session", name, "action", "dump-layout"])
    if result.returncode:
        raise MigrationError(f"layout read for {name} failed: {result.stderr.strip()}")
    tabs = parse_layout(result.stdout)
    records = _live_claude_records(
        claude_sessions or Path.home() / ".claude" / "sessions",
        process_root,
        skip_invalid=True,
        coerce_pid=True,
    )
    for conversation, record in list(records.items()):
        try:
            command = (
                (process_root / str(record["pid"]) / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except OSError:
            del records[conversation]
            continue
        if "claude" not in command.lower():
            del records[conversation]
            continue
        record["command"] = command
    panes = []
    for tab in tabs:
        for pane in tab["panes"]:
            if pane["command"] != "fleet-claude":
                continue
            args = pane.get("args", [])
            conversation = (
                args[1]
                if len(args) >= 2 and args[0] in {"--resume", "--session-id"}
                else None
            )
            process = records.get(conversation, {})
            panes.append(
                {
                    "tab": tab["name"],
                    "conversation": conversation,
                    "cwd": process.get("cwd"),
                    "pid": process.get("pid"),
                    "command": process.get("command"),
                }
            )
    return {
        "session": name,
        "node": node,
        "tabs": [tab["name"] for tab in tabs],
        "panes": panes,
    }


def _inspect_remote_session(job: Mapping[str, str], name: str) -> dict[str, Any]:
    argv = fleet_node.placement_argv(
        job,
        [sys.executable, "-m", "reckon.crew.fleet_migrate", "inspect-session", name],
    )
    result = _run_step(argv)
    if result.returncode:
        raise MigrationError(
            f"session inspection failed in job {job['jobid']}: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MigrationError("new-node session inspection returned no JSON") from exc


def _worker_steps(job_id: str) -> list[str]:
    result = _run_step(["squeue", "--steps", "-h", "-j", job_id, "-o", "%i"])
    if result.returncode:
        raise MigrationError(
            f"cannot read steps in job {job_id}: {result.stderr.strip()}"
        )
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.strip().endswith((".batch", ".extern"))
    ]


def _cancel_job(job_id: str) -> subprocess.CompletedProcess[str]:
    return _run_step(["scancel", job_id])


def _send_supervisor(job: Mapping[str, str], line: str) -> None:
    argv = fleet_node.placement_argv(
        job,
        [sys.executable, "-m", "reckon.crew.fleet_migrate", "request", *line.split()],
    )
    result = _run_step(argv)
    if result.returncode:
        raise MigrationError(
            f"supervisor request failed in job {job['jobid']}: {result.stderr.strip()}"
        )


def _local_request(line: str) -> None:
    """Send a bounded request to the supervisor on this allocation's node."""
    from reckon.crew.dispatch import _write_fleet_request
    from reckon.crew.runs import CrewError

    if not line or "\n" in line:
        raise MigrationError("supervisor request must be one nonempty line")
    fifo = runtime_directory() / REQUEST_FIFO_NAME
    try:
        _write_fleet_request(fifo, (line + "\n").encode(), time.monotonic() + 10)
    except CrewError as exc:
        raise MigrationError(str(exc)) from exc


def _running_job(
    job_id: str,
    *,
    old_node: str,
    query_jobs: Callable[[str], list[dict[str, str]]],
    pause: Callable[[float], None],
) -> dict[str, str]:
    account = fleet_node.fleet_size().account
    for _ in range(120):
        job = next(
            (row for row in query_jobs(account) if row.get("jobid") == job_id), None
        )
        if job and job.get("state", "").upper() in {"RUNNING", "R"}:
            node = job.get("node", "").strip()
            if not node or node == old_node or any(char in node for char in " ()"):
                raise MigrationError(
                    f"new job {job_id} runs on {node or 'an unknown node'}; "
                    f"the old job runs on {old_node}"
                )
            return job
        pause(5)
    raise MigrationError(f"job {job_id} did not start within ten minutes")


def _wait_for_record(
    path: Path,
    *,
    matches: Callable[[dict[str, Any]], bool],
    pause: Callable[[float], None],
    description: str,
) -> dict[str, Any]:
    for _ in range(30):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict) and matches(record):
            return record
        pause(1)
    raise MigrationError(f"{description} was not recorded within thirty seconds")


def _verified_session(
    name: str,
    recorded: Mapping[str, Any],
    node: str,
    inspect: Callable[[], dict[str, Any]],
    pause: Callable[[float], None],
    *,
    check_tabs: bool = False,
) -> dict[str, Any]:
    expected_tabs = [tab["name"] for tab in recorded["tabs"]]
    expected = [
        (tab["name"], pane["conversation"], pane["cwd"])
        for tab in recorded["tabs"]
        for pane in tab["panes"]
        if pane.get("conversation")
    ]
    for _ in range(30):
        actual = inspect()
        observed = [
            (pane.get("tab"), pane.get("conversation"), pane.get("cwd"))
            for pane in actual.get("panes", [])
            if pane.get("pid") and pane.get("command")
        ]
        if (
            actual.get("session") == name
            and actual.get("node") == node
            and observed == expected
            and len(actual.get("panes", [])) == len(expected)
            and (not check_tabs or actual.get("tabs") == expected_tabs)
        ):
            return actual
        pause(2)
    if check_tabs and actual.get("tabs") != expected_tabs:
        raise SessionVerificationError(
            f"{name} tab order mismatch: expected {expected_tabs}, "
            f"observed {actual.get('tabs')}",
            actual,
        )
    raise SessionVerificationError(
        f"{name} did not show {len(expected)} recorded Claude panes and tab order "
        f"on {node}: {json.dumps(actual, sort_keys=True)}",
        actual,
    )


def _rehearse(
    *,
    name: str | None,
    dry_run: bool,
    state: Path,
    layouts_dir: Path,
    observation: Callable[[], dict[str, Any]],
    run_step: Callable[[list[str]], subprocess.CompletedProcess[str]],
    send_supervisor: Callable[[Mapping[str, str], str], None],
    inspect_session: Callable[[Mapping[str, str], str], dict[str, Any]],
    pause: Callable[[float], None],
    hostname: Callable[[], str],
) -> str:
    if not name or not re.fullmatch(r"probe-[A-Za-z0-9._-]+", name):
        raise MigrationError("rehearsal requires a session name starting with probe-")
    path = state / "migration" / "rehearsals" / name / "ledger.json"
    ledger = json.loads(path.read_text()) if path.is_file() else None
    step = "census" if ledger is None else ledger["next_step"]
    layout_name = f"rehearse-{name}"
    descriptions = {
        "census": f"census: read only {name} and its live Claude panes",
        "layout": f"layout: write {layout_name}.kdl",
        "end": f"end: stop {name} on this node",
        "start": f"start: request this node's supervisor to start {name} from {layout_name}.kdl",
        "verify": f"verify: check {name} tab order and live Claude conversation ids",
        "done": f"rehearsal complete for {name} in {path}",
    }
    if step not in descriptions:
        raise MigrationError(f"unknown rehearsal step: {step}")
    if dry_run or step == "done":
        return descriptions[step]
    if step == "census":
        source = observation()
        if name not in source.get("layouts", {}):
            raise MigrationError(f"{name} is absent from the local session census")
        source = {**source, "layouts": {name: source["layouts"][name]}}
        census = build_census(source)
        recorded = census["sessions"][0]
        if len(recorded["tabs"]) != 2 or not any(
            pane.get("conversation")
            for tab in recorded["tabs"]
            for pane in tab["panes"]
        ):
            raise MigrationError(f"{name} needs two tabs and a live Claude pane")
        ledger = {
            "session": name,
            "node": hostname().split(".")[0],
            "started_at": datetime.now(UTC).isoformat(),
            "completed": ["census"],
            "next_step": "layout",
            "census": census,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        return f"Recorded {name} in {path}; next step: layout"
    assert ledger is not None
    if step == "layout":
        layouts_dir.mkdir(parents=True, exist_ok=True)
        layout = layouts_dir / f"{layout_name}.kdl"
        layout.write_text(render_layout(ledger["census"]["sessions"][0]))
        ledger["layout"] = str(layout)
        next_step = "end"
    elif step == "end":
        if not Path(ledger["layout"]).is_file():
            raise MigrationError(f"rehearsal layout is missing: {ledger['layout']}")

        def local_sessions() -> list[str]:
            return _active_sessions(invoke=lambda argv, **_options: run_step(argv))

        ledger["old_end"] = _end_local_session(
            name, run_step=run_step, active_sessions=local_sessions
        )
        next_step = "start"
    elif step == "start":
        if not Path(ledger["layout"]).is_file():
            raise MigrationError(f"rehearsal layout is missing: {ledger['layout']}")
        request = f"session {name} {layout_name}"
        send_supervisor({"jobid": "local"}, request)
        ledger["start_request"] = request
        next_step = "verify"
    else:
        recorded = ledger["census"]["sessions"][0]
        try:
            actual = _verified_session(
                name,
                recorded,
                ledger["node"],
                lambda: inspect_session({"jobid": "local"}, name),
                pause,
                check_tabs=True,
            )
        except SessionVerificationError as exc:
            ledger["verification_failure"] = {
                "reason": str(exc),
                "expected_tabs": [tab["name"] for tab in recorded["tabs"]],
                "observed_tabs": exc.observed.get("tabs"),
                "at": datetime.now(UTC).isoformat(),
            }
            path.write_text(json.dumps(ledger, indent=2) + "\n")
            raise
        ledger.pop("verification_failure", None)
        ledger["verification"] = actual
        next_step = "done"
    ledger["completed"].append(step)
    ledger["next_step"] = next_step
    path.write_text(json.dumps(ledger, indent=2) + "\n")
    return f"Rehearsal {step} complete for {name} in {path}; next step: {next_step}"


def migrate(
    *,
    dry_run: bool = False,
    session: str | None = None,
    rehearse: bool = False,
    confirm: bool = False,
    state: Path | None = None,
    layouts_dir: Path | None = None,
    observation: Callable[[], dict[str, Any]] = _remote_observation,
    read_runs: Callable[[], list[dict[str, Any]]] = _read_runs,
    submit_hold: Callable[[str], str] = fleet_node.submit,
    query_jobs: Callable[[str], list[dict[str, str]]] = fleet_node.query_jobs,
    run_step: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_step,
    send_supervisor: Callable[[Mapping[str, str], str], None] = _send_supervisor,
    replace_reservation: Callable[..., dict[str, Any]] = placement.replace_reservation,
    read_reservation: Callable[[], dict[str, Any] | None] = placement.read_reservation,
    read_fleet_record: Callable[[Path], dict[str, Any]] = _fleet_record,
    inspect_session: Callable[
        [Mapping[str, str], str], dict[str, Any]
    ] = _inspect_remote_session,
    list_worker_steps: Callable[[str], list[str]] = _worker_steps,
    cancel_job: Callable[[str], subprocess.CompletedProcess[str]] = _cancel_job,
    pause: Callable[[float], None] = time.sleep,
    hostname: Callable[[], str] = socket.gethostname,
) -> str:
    """Perform exactly one migration checkpoint, resuming from its ledger."""
    state = state or state_directory()
    layouts_dir = (
        layouts_dir
        or Path(os.environ.get("ZELLIJ_CONFIG_DIR", Path.home() / ".config" / "zellij"))
        / "layouts"
    )
    if rehearse:
        local_observation = (
            (lambda: collect_local(session))
            if observation is _remote_observation
            else observation
        )
        local_supervisor = (
            (lambda _job, line: _local_request(line))
            if send_supervisor is _send_supervisor
            else send_supervisor
        )
        local_inspection = (
            (lambda _job, name: _local_session_processes(name))
            if inspect_session is _inspect_remote_session
            else inspect_session
        )
        return _rehearse(
            name=session,
            dry_run=dry_run,
            state=state,
            layouts_dir=layouts_dir,
            observation=local_observation,
            run_step=run_step,
            send_supervisor=local_supervisor,
            inspect_session=local_inspection,
            pause=pause,
            hostname=hostname,
        )
    path = _latest_ledger(state)
    ledger = json.loads(path.read_text()) if path else None
    step = "census" if ledger is None else ledger["next_step"]
    if session and step != "cutover":
        raise MigrationError("--session applies only to a cutover step")
    if step == "census":
        description = "census: read live runs, Claude process records, transcripts and zellij layout dumps on the old node"
    elif step == "layout":
        names = [item["name"] for item in ledger["census"]["sessions"]]
        description = "layout: write " + ", ".join(
            f"migrate-{name}.kdl" for name in names
        )
    elif step == "stand-up":
        known = ledger.get("stand_up", {}).get("job_id")
        description = (
            f"stand-up: {'reuse job ' + known if known else 'submit a standby hold'}, "
            "wait for a different node, request a readiness response, "
            "and run one step inside the new job"
        )
    elif step == "promote":
        description = (
            f"promote: replace the worker reservation with job "
            f"{ledger['stand_up']['job_id']}, request supervisor promotion, "
            "and print the fleet and reservation records before and after"
        )
    elif step == "cutover":
        moved = ledger.get("cutovers", {})
        remaining = [
            item["name"]
            for item in ledger["census"]["sessions"]
            if moved.get(item["name"], {}).get("status") != "complete"
        ]
        known = {item["name"] for item in ledger["census"]["sessions"]}
        if session and session not in known:
            raise MigrationError(f"{session} is absent from the recorded census")
        description = (
            f"cutover: end {session} on the old job, start it on the new job "
            f"from migrate-{session}.kdl, and verify each Claude pane"
            if session
            else "cutover: sessions still to move: " + (", ".join(remaining) or "none")
        )
    elif step == "retire":
        description = (
            f"retire: check job {ledger['stand_up']['old_record']['job_id']} for "
            "worker steps and zellij sessions; "
            f"scancel {ledger['stand_up']['old_record']['job_id']}"
            + (" after --confirm" if confirm else " (requires --confirm)")
        )
    elif step == "done":
        description = "migration complete"
    else:
        description = f"{step}: awaits the next migration implementation"
    if dry_run:
        return description
    if step == "cutover" and session is None:
        return description
    if step == "done":
        return description
    if step == "census":
        census = build_census(observation())
        ledger = {
            "started_at": datetime.now(UTC).isoformat(),
            "completed": ["census"],
            "next_step": "layout",
            "census": census,
            "runs": read_runs(),
            "continue_prompts": {},
        }
        path = _new_ledger_path(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        missing = census["missing_start_log"]
        warning = (
            f"; {len(missing)} live panes missing from sessions.tsv: {', '.join(missing)}"
            if missing
            else ""
        )
        return f"Recorded {len(census['sessions'])} sessions and {len(ledger['runs'])} live runs in {path}{warning}; next step: layout"
    if step == "layout":
        assert path is not None and ledger is not None
        layouts_dir.mkdir(parents=True, exist_ok=True)
        for item in ledger["census"]["sessions"]:
            (layouts_dir / f"migrate-{item['name']}.kdl").write_text(
                render_layout(item, ledger.get("continue_prompts"))
            )
        ledger["completed"].append("layout")
        ledger["next_step"] = "stand-up"
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        return f"Wrote {len(ledger['census']['sessions'])} layouts to {layouts_dir}; next step: stand-up"
    if step == "stand-up":
        assert path is not None and ledger is not None
        standing = ledger.setdefault("stand_up", {})
        old_record = standing.get("old_record") or read_fleet_record(state)
        old_job = str(old_record.get("job_id") or "")
        old_node = str(old_record.get("node") or "")
        if not old_job or not old_node:
            raise MigrationError("the old fleet record needs a job id and node")
        standing["old_record"] = old_record
        if not standing.get("job_id"):
            script = fleet_node.generate_hold_script(
                fleet_node.fleet_size(),
                log_path=fleet_node.batch_log_path(),
                standby=True,
            )
            try:
                standing["job_id"] = submit_hold(script)
            except fleet_node.FleetNodeError as exc:
                raise MigrationError(f"standby hold submission failed: {exc}") from exc
            if not standing["job_id"]:
                raise MigrationError("standby hold submission returned no job id")
            path.write_text(json.dumps(ledger, indent=2) + "\n")
        job = _running_job(
            standing["job_id"],
            old_node=old_node,
            query_jobs=query_jobs,
            pause=pause,
        )
        token = secrets.token_hex(16)
        standing["ready_token"] = token
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        response_path = state / "migration" / f"ready-{token}.json"
        send_supervisor(job, f"ready {token}")
        ready = _wait_for_record(
            response_path,
            matches=lambda answer: (
                answer.get("job_id") == standing["job_id"]
                and answer.get("node") == job["node"]
                and answer.get("standby") is True
            ),
            pause=pause,
            description="standby supervisor readiness",
        )
        result = run_step(fleet_node.placement_argv(job, ["hostname", "-s"]))
        if result.returncode or result.stdout.strip() != job["node"]:
            raise MigrationError(
                f"step in job {standing['job_id']} did not confirm node {job['node']}: "
                f"{result.stdout.strip()} {result.stderr.strip()}"
            )
        standing.update(
            {
                "node": job["node"],
                "readiness": ready,
                "step_node": result.stdout.strip(),
            }
        )
        ledger["completed"].append("stand-up")
        ledger["next_step"] = "promote"
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        return (
            f"Standby job {standing['job_id']} runs on {job['node']}; "
            f"supervisor answered ready at {ready['ready_at']}; "
            f"step ran on {result.stdout.strip()}; next step: promote"
        )
    if step == "promote":
        assert path is not None and ledger is not None
        standing = ledger["stand_up"]
        promotion = ledger.setdefault("promotion", {})
        if "fleet_before" not in promotion:
            promotion["fleet_before"] = read_fleet_record(state)
            promotion["reservation_before"] = read_reservation()
            path.write_text(json.dumps(ledger, indent=2) + "\n")
        before_fleet = promotion["fleet_before"]
        before_reservation = promotion["reservation_before"]
        job = _running_job(
            standing["job_id"],
            old_node=standing["old_record"]["node"],
            query_jobs=query_jobs,
            pause=pause,
        )
        try:
            replacement = replace_reservation(job_id=standing["job_id"])
        except placement.CrewError as exc:
            raise MigrationError(f"reservation replacement failed: {exc}") from exc
        send_supervisor(job, "promote")
        after_fleet = _wait_for_record(
            state / fleet_node.RECORD_NAME,
            matches=lambda record: (
                record.get("job_id") == standing["job_id"]
                and record.get("node") == standing["node"]
            ),
            pause=pause,
            description="promoted fleet record",
        )
        after_reservation = read_reservation()
        if (
            not after_reservation
            or after_reservation.get("job_id") != standing["job_id"]
        ):
            raise MigrationError("reservation did not retain the new job")
        promotion.update(
            {
                "fleet_after": after_fleet,
                "reservation_after": after_reservation,
                "replacement": replacement,
            }
        )
        ledger["completed"].append("promote")
        ledger["next_step"] = "cutover"
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        return (
            "Promoted standby supervisor; next step: cutover\n"
            f"fleet before: {json.dumps(before_fleet, sort_keys=True)}\n"
            f"fleet after: {json.dumps(after_fleet, sort_keys=True)}\n"
            f"reservation before: {json.dumps(before_reservation, sort_keys=True)}\n"
            f"reservation after: {json.dumps(after_reservation, sort_keys=True)}"
        )
    if step == "cutover":
        assert path is not None and ledger is not None and session is not None
        cutovers = ledger.setdefault("cutovers", {})
        outcome = cutovers.setdefault(session, {})
        if outcome.get("status") == "complete":
            return f"{session} already cut over to {outcome['node']}; skipped"
        layout = layouts_dir / f"migrate-{session}.kdl"
        if not layout.is_file():
            raise MigrationError(f"migration layout is missing: {layout}")
        old_job_id = str(ledger["stand_up"]["old_record"]["job_id"])
        new_job_id = str(ledger["stand_up"]["job_id"])
        old_job = {"jobid": old_job_id}
        new_job = {"jobid": new_job_id}
        if outcome.get("phase") != "old-ended":
            argv = fleet_node.placement_argv(
                old_job,
                [
                    sys.executable,
                    "-m",
                    "reckon.crew.fleet_migrate",
                    "end-session",
                    session,
                ],
            )
            result = run_step(argv)
            try:
                ended = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise MigrationError(
                    f"old-node end returned no receipt for {session}"
                ) from exc
            if (
                result.returncode
                or ended.get("session") != session
                or ended.get("ended") is not True
            ):
                raise MigrationError(
                    f"old-node end did not confirm {session}: {result.stderr.strip()}"
                )
            outcome["phase"] = "old-ended"
            outcome["old_end"] = ended
            path.write_text(json.dumps(ledger, indent=2) + "\n")
        send_supervisor(new_job, f"session {session} migrate-{session}")
        recorded = next(
            item for item in ledger["census"]["sessions"] if item["name"] == session
        )
        actual = _verified_session(
            session,
            recorded,
            ledger["stand_up"]["node"],
            lambda: inspect_session(new_job, session),
            pause,
        )
        expected_count = sum(
            bool(pane.get("conversation"))
            for tab in recorded["tabs"]
            for pane in tab["panes"]
        )
        outcome.update(
            {
                "status": "complete",
                "node": actual["node"],
                "panes": actual["panes"],
                "completed_at": datetime.now(UTC).isoformat(),
            }
        )
        outcome.pop("phase", None)
        if all(
            cutovers.get(item["name"], {}).get("status") == "complete"
            for item in ledger["census"]["sessions"]
        ):
            ledger["completed"].append("cutover")
            ledger["next_step"] = "retire"
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        return (
            f"Cut over {session}: {expected_count} Claude panes with recorded "
            f"conversation ids on {actual['node']}; next step: {ledger['next_step']}"
        )
    if step == "retire":
        assert path is not None and ledger is not None
        old_job_id = str(ledger["stand_up"]["old_record"]["job_id"])
        if not old_job_id.isdecimal():
            raise MigrationError(f"old job id is not numeric: {old_job_id!r}")
        census_sessions = ledger["census"]["sessions"]
        if not census_sessions or any(
            ledger.get("cutovers", {}).get(item["name"], {}).get("status") != "complete"
            for item in census_sessions
        ):
            raise MigrationError(
                "retire requires every recorded session to be cut over"
            )
        steps = list_worker_steps(old_job_id)
        if steps:
            raise MigrationError(
                f"old job {old_job_id} still holds worker steps: {', '.join(steps)}"
            )
        argv = fleet_node.placement_argv(
            {"jobid": old_job_id},
            [sys.executable, "-m", "reckon.crew.fleet_migrate", "list-sessions"],
        )
        result = run_step(argv)
        try:
            sessions = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MigrationError("old-node session list returned no JSON") from exc
        if result.returncode or not isinstance(sessions, list):
            raise MigrationError(
                f"old-node session list failed: {result.stderr.strip()}"
            )
        if sessions:
            raise MigrationError(
                f"old job {old_job_id} still holds zellij sessions: {', '.join(sessions)}"
            )
        command = f"scancel {old_job_id}"
        if not confirm:
            return f"Old job {old_job_id} has no worker steps or zellij sessions; {command}; --confirm required"
        steps = list_worker_steps(old_job_id)
        if steps:
            raise MigrationError(
                f"old job {old_job_id} acquired worker steps: {', '.join(steps)}"
            )
        answer = cancel_job(old_job_id)
        if answer.returncode:
            raise MigrationError(
                f"{command} failed ({answer.returncode}): {answer.stdout.strip()} {answer.stderr.strip()}"
            )
        ledger["retirement"] = {
            "job_id": old_job_id,
            "command": command,
            "exit_status": answer.returncode,
            "stdout": answer.stdout,
            "stderr": answer.stderr,
        }
        ledger["completed"].append("retire")
        ledger["next_step"] = "done"
        path.write_text(json.dumps(ledger, indent=2) + "\n")
        return f"{command} answered (exit {answer.returncode}): stdout={answer.stdout.strip()!r}, stderr={answer.stderr.strip()!r}"
    raise MigrationError(f"{step} is not implemented by the census and layout step")


if __name__ == "__main__":
    if sys.argv[1:] == ["collect-local"]:
        print(json.dumps(collect_local()))
    elif len(sys.argv) in {3, 4} and sys.argv[1] == "request":
        _local_request(" ".join(sys.argv[2:]))
    elif len(sys.argv) == 3 and sys.argv[1] == "end-session":
        print(json.dumps(_end_local_session(sys.argv[2])))
    elif len(sys.argv) == 3 and sys.argv[1] == "inspect-session":
        print(json.dumps(_local_session_processes(sys.argv[2])))
    elif sys.argv[1:] == ["list-sessions"]:
        print(json.dumps(_active_sessions()))
    else:
        raise SystemExit("expected a local migration observation or request")
