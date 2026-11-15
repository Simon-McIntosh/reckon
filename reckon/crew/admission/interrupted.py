"""Recover admission intent after the process holding its handover has died."""

from __future__ import annotations

import copy
import json
import socket
from collections.abc import Mapping
from pathlib import Path

from reckon.crew import dispatch_launch, runs
from reckon.crew.node import CrewError


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise CrewError(f"admission recovery record {path} is not an object")
    return value


def _owner_ended(request: Mapping) -> bool:
    owner = request.get("admitted_by") or {}
    if owner.get("host") != socket.gethostname():
        return False
    return runs.record_process_alive(owner, runs.process_alive) is False


def recover() -> list[str]:
    """Settle each dead pass's handover under the admission and pointer locks.

    An absent pointer is recoverable intent, unless a discard or promotion
    records its departure. A worker receipt or a live supervisor outranks a
    retry: adopting it closes the spawn-to-pointer gap without another launch.
    Only a request with neither is returned to the queue. The handover remains
    until the replacement pointer and its admission stamp are durable.
    """
    from reckon.crew.admission import launching
    from reckon.crew.promotion import discard_recorded

    recovered = []
    for path in sorted(runs.handover_dir().glob("*.json")):
        request = _read(path)
        run_id = str(request.get("run_id") or "")
        if not run_id or not _owner_ended(request):
            continue
        project = str(request.get("project") or "")
        repo = str(request.get("repo") or "")
        with runs._pointer_lock(run_id):
            promoted = runs._promoted_record_project(
                run_id,
                project or None,
                roots={project: Path(repo)} if project and repo else None,
            )
            if discard_recorded(run_id) or promoted:
                runs.clear_handover(request)
                continue
            current = _read(runs.pointer_path(run_id))
            directory = runs.run_dir(run_id)
            worker = _read(directory / dispatch_launch.WORKER_RECORD_NAME)
            if worker and worker.get("run_id") != run_id:
                raise CrewError(f"worker receipt for {run_id!r} names another run")
            held = dispatch_launch.held_launch_reservation(directory)
            unsettled = not current or current.get("phase") in {
                "queued",
                "starting",
                "dispatched",
                "admitted",
                "launching",
            }
            if unsettled:
                if worker or held:
                    # The supervisor may be between starting its worker and
                    # publishing its identity. Keep its attempt and its paths.
                    current = current or copy.deepcopy(request)
                    spec = _read(directory / dispatch_launch.SUPERVISOR_SPEC_NAME)
                    for key in ("attempt", "attempt_kind", "attempt_started_at"):
                        if spec.get(key) is not None:
                            current[key] = spec[key]
                    supervisor = {
                        "pid": (held or worker).get("supervisor_pid"),
                        "pid_start_time": (held or worker).get(
                            "supervisor_pid_start_time"
                        ),
                    }
                    holder = (
                        supervisor
                        if runs.record_process_alive(supervisor, runs.process_alive)
                        is True
                        else worker
                    )
                    current.update(
                        phase="working" if worker else "starting",
                        launcher_host=request["admitted_by"]["host"],
                    )
                    for key in ("pid", "pid_start_time", "launched_at"):
                        if holder.get(key) is not None:
                            current[key] = holder[key]
                    current["admit_request"] = copy.deepcopy(request)
                    runs._write_json(runs.pointer_path(run_id), current)
                else:
                    current = launching._requeued_request(request, run_id)
                    current.pop("admitted_by", None)
                    # A supervisor delayed before its spawn must re-read this
                    # queued phase under the same lock before starting a worker.
                    runs._write_json(runs.pointer_path(run_id), current)
                    (directory / "attempt.json").unlink(missing_ok=True)
                    (directory / "claim.json").unlink(missing_ok=True)
            if current.get("phase") != "queued":
                admitted_at = runs._utc_now()
                current.update(
                    admitted_at=admitted_at, admit_request=copy.deepcopy(request)
                )
                launching._archive_queued_life(
                    current, request, admitted_at=admitted_at
                )
                runs._write_json(runs.pointer_path(run_id), current)
            runs.clear_handover(request)
            recovered.append(run_id)
    return recovered
