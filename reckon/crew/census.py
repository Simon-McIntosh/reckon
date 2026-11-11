"""Inspect recorded process ownership and stop only proven local orphans."""

from __future__ import annotations

import json
import math
import os
import platform
import select
import signal
import socket
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from reckon.crew.process_liveness import (
    _process_start_time,
    _process_stat_fields,
    process_alive,
)
from reckon.crew.run_paths import _read_json_file, crew_home


def _integer(value: Any) -> int | None:
    try:
        return int(value) if not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None


def _processes() -> dict[int, dict[str, Any]]:
    """Read this user's local processes; argv never establishes ownership."""
    processes = {}
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    ticks = os.sysconf("SC_CLK_TCK")
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            pid = int(path.name)
            fields = _process_stat_fields(pid)
            if len(fields) < 20:
                continue
            environment = {}
            try:
                for token in (path / "environ").read_bytes().split(b"\0"):
                    key, separator, value = token.partition(b"=")
                    if separator:
                        environment[os.fsdecode(key)] = os.fsdecode(value)
            except OSError:
                pass
            try:
                cwd = os.readlink(path / "cwd")
            except OSError:
                cwd = None
            try:
                wchan = (path / "wchan").read_text().strip()
            except OSError:
                wchan = None
            processes[pid] = {
                "pid": pid,
                "parent_pid": int(fields[1]),
                "start_time": fields[19],
                "age_seconds": max(0.0, uptime - int(fields[19]) / ticks),
                "state": fields[0],
                "wait_channel": wchan,
                "cwd": cwd,
                "environment": environment,
            }
        except (OSError, ValueError):
            continue
    return processes


def _ancestry() -> set[int]:
    excluded = {os.getpid()}
    pid = os.getppid()
    while pid > 0 and pid not in excluded:
        excluded.add(pid)
        fields = _process_stat_fields(pid)
        if len(fields) < 2:
            break
        pid = int(fields[1])
    return excluded


def _ownership_records(home: Path) -> tuple[dict[int, list[dict]], dict[str, dict]]:
    records = {}
    for pattern, kind in (
        ("watch/*.followers/*.lock", "follower"),
        ("watch/*.lock.registration", "producer"),
    ):
        for path in sorted(home.glob(pattern)):
            record = _read_json_file(path)
            if record and (pid := _integer(record.get("pid"))) is not None:
                records.setdefault(pid, []).append(
                    {**record, "kind": kind, "source": str(path)}
                )
    pointers = {}
    for path in sorted((home / "live").glob("*.json")):
        record = _read_json_file(path)
        if record:
            pointers[path.stem] = {**record, "source": str(path)}
    return records, pointers


def _owner(
    process: Mapping[str, Any],
    records: Mapping[int, list[dict]],
    pointers: Mapping[str, dict],
) -> dict[str, Any] | None:
    pid = process["pid"]
    environment = process["environment"]
    choices = records.get(pid, [])
    matching = [
        record
        for record in choices
        if record.get("host") == socket.gethostname()
        and str(record.get("pid_start_time") or "") == process["start_time"]
    ]
    record = matching[0] if matching else next(iter(choices), None)
    if record:
        owner = {
            "kind": record["kind"],
            "source": record["source"],
            "session": record.get("session"),
            "project": record.get("project"),
        }
        if len(matching) > 1:
            return {
                **owner,
                "unknown": "multiple registrations claim this process identity",
            }
        if record.get("host") != socket.gethostname():
            return {**owner, "unknown": "registration is not on this host"}
        if str(record.get("pid_start_time") or "") != process["start_time"]:
            return {**owner, "unknown": "registration process identity is unverified"}
        if record["kind"] == "producer":
            return {**owner, "lease_renewed_at": record.get("lease_renewed_at")}
        return {
            **owner,
            "pid": record.get("parent_pid"),
            "start_time": record.get("parent_start_time"),
        }
    session = environment.get("RECKON_SESSION_ID") or environment.get(
        "RECKON_CREW_SESSION"
    )
    if "RECKON_OWNER_PID" in environment:
        if (
            environment.get("RECKON_OWNER_HOST", socket.gethostname())
            != socket.gethostname()
        ):
            return {
                "kind": "stamped",
                "source": "RECKON_OWNER_PID",
                "unknown": "recorded owner is not on this host",
            }
        return {
            "kind": "stamped",
            "source": "RECKON_OWNER_PID",
            "pid": environment["RECKON_OWNER_PID"],
            "start_time": environment.get("RECKON_OWNER_START_TIME"),
            "session": session,
        }
    if "RECKON_FOLLOWER_OWNER" in environment:
        try:
            value = json.loads(environment["RECKON_FOLLOWER_OWNER"])
        except (ValueError, TypeError):
            value = None
        if not isinstance(value, dict):
            value = {}
        return {
            "kind": "follower",
            "source": "RECKON_FOLLOWER_OWNER",
            "pid": value.get("pid"),
            "start_time": value.get("start_time"),
            "session": session,
        }
    run_id = environment.get("RECKON_RUN_ID")
    if run_id and (record := pointers.get(run_id)):
        owner = {
            "kind": "run",
            "source": record["source"],
            "session": record.get("session"),
            "run_id": run_id,
        }
        if record.get("launcher_host") != socket.gethostname():
            return {**owner, "unknown": "run pointer is not on this host"}
        return {
            **owner,
            "pid": record.get("pid"),
            "start_time": record.get("pid_start_time"),
        }
    return None


def _owner_alive(owner: Mapping[str, Any], now: float) -> tuple[bool | None, str]:
    if owner.get("unknown"):
        return None, owner["unknown"]
    if owner["kind"] == "producer":
        from reckon.crew.runs import producer_lease_seconds

        renewed = owner.get("lease_renewed_at")
        if (
            isinstance(renewed, bool)
            or not isinstance(renewed, (float, int))
            or not math.isfinite(renewed)
        ):
            return None, "producer lease is unreadable"
        alive = now - renewed < producer_lease_seconds()
        return alive, "producer lease is live" if alive else "producer lease expired"
    pid = _integer(owner.get("pid"))
    if pid is None:
        return None, "owner pid is unreadable"
    if pid <= 1:
        return False, "recorded owner is gone"
    alive = process_alive(pid)
    if alive is False:
        return False, "recorded owner is dead"
    if alive is None:
        return None, "owner liveness is unknown"
    expected = owner.get("start_time")
    if expected:
        actual = _process_start_time(pid)
        if actual is None:
            return None, "owner identity is unreadable"
        if str(expected) != actual:
            return False, "recorded owner pid was reused"
    return True, "recorded owner is live"


def census(*, home: Path | None = None) -> dict[str, Any]:
    """List proven orphans and explain why other recorded processes are spared."""
    state_home = crew_home() if home is None else home
    records, pointers = _ownership_records(state_home)
    excluded = _ancestry()
    orphaned, spared = [], []
    followers = defaultdict(list)
    processes = _processes()
    for pid, process in sorted(processes.items()):
        owner = _owner(process, records, pointers)
        if owner is None:
            continue
        row = {key: value for key, value in process.items() if key != "environment"}
        row["owner"] = owner
        alive, reason = _owner_alive(owner, time.time())
        row["reason"] = reason
        if pid in excluded:
            row["reason"] = "census process or its ancestry"
            spared.append(row)
        elif process["state"] in {"Z", "X"}:
            row["reason"] = "process has already exited"
            spared.append(row)
        elif alive is False:
            orphaned.append(row)
        else:
            spared.append(row)
        if (
            owner["kind"] == "follower"
            and owner.get("session")
            and not owner.get("unknown")
            and process["state"] not in {"Z", "X"}
        ):
            followers[owner["session"]].append(pid)
    return {
        "host": socket.gethostname(),
        "state_home": str(state_home),
        "orphaned": orphaned,
        "spared": spared,
        "duplicate_followers": [
            {"session": session, "pids": pids, "count": len(pids)}
            for session, pids in sorted(followers.items())
            if len(pids) > 1
        ],
        "unrecorded_count": len(processes) - len(orphaned) - len(spared),
        "excluded_pids": sorted(excluded),
    }


def _pidfd_syscall(number: int, *arguments: int) -> int:
    """Use the Linux pidfd ABI when Python was built without its wrappers.

    Linux assigns these syscall numbers on x86-64 and asm-generic machines.
    Unsupported architectures refuse signalling rather than falling back to
    a bare pid, which could have been reused since the census read it.
    """
    import ctypes

    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise OSError("pidfd signalling is unavailable on this architecture")
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    result = library.syscall(
        ctypes.c_long(number), *(ctypes.c_long(arg) for arg in arguments)
    )
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _pidfd_syscall(434, pid, 0)


def _pidfd_signal(descriptor: int, signum: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(descriptor, signum)
    else:
        _pidfd_syscall(424, descriptor, signum, 0, 0)


def apply_census(
    report: Mapping[str, Any], *, emit: Callable[[dict], None] | None = None
) -> list[dict]:
    """Revalidate each listed identity and owner before signalling its pidfd.

    Only listed pids can be stopped. A changed owner or a renewed producer lease
    is spared; the process descriptor prevents a recycled pid being signalled.
    Live pointers are read only and remain available for classified completion.
    """
    results = []
    home = Path(report["state_home"])
    for row in report["orphaned"]:
        pid = row["pid"]
        result = {"pid": pid, "action": "spared"}
        descriptor = None
        try:
            descriptor = _pidfd_open(pid)
            current = census(home=home)
            candidate = next(
                (item for item in current["orphaned"] if item["pid"] == pid), None
            )
            if (
                candidate is None
                or candidate["start_time"] != row["start_time"]
                or candidate["owner"] != row["owner"]
            ):
                result["reason"] = "process identity or recorded ownership changed"
            else:
                _pidfd_signal(descriptor, signal.SIGTERM)
                signals = ["SIGTERM"]
                poller = select.poll()
                poller.register(descriptor, select.POLLIN)
                if not poller.poll(1000):
                    _pidfd_signal(descriptor, signal.SIGKILL)
                    signals.append("SIGKILL")
                    exited = bool(poller.poll(1000))
                else:
                    exited = True
                result.update(
                    action="stopped" if exited else "refused", signals=signals
                )
                if not exited:
                    result["reason"] = "process has not exited after SIGKILL"
        except ProcessLookupError:
            result["reason"] = "process already exited"
        except (OSError, AttributeError) as exc:
            result.update(action="refused", reason=str(exc))
        finally:
            if descriptor is not None:
                os.close(descriptor)
        results.append(result)
        if emit is not None:
            emit(result)
    return results
