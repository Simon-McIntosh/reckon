"""Cross-project directory of coordinators represented by live run pointers."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon.crew.recovery import classify_pointer
from reckon.crew.runs import list_live


class DirectoryError(RuntimeError):
    """A directory selector does not identify one live run."""


def _node(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("node")
    return value if isinstance(value, Mapping) else {}


def _observed_transport(record: Mapping[str, Any]) -> dict[str, str] | None:
    """Return a messaging address only when a run event explicitly supplied it."""
    log_value = str(record.get("log_path") or "")
    if not log_value:
        return None
    log = Path(log_value)
    original = log.parent / "stream.jsonl" if log.name.startswith("resume-") else log
    streams = [
        original,
        *sorted(log.parent.glob("resume-*.jsonl"), key=ledger._resume_stream_order),
    ]
    observed: str | None = None
    observed_in: str | None = None
    for stream in streams:
        if not stream.is_file():
            continue
        try:
            with stream.open(encoding="utf-8") as events:
                for line in events:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    address = str(event.get("messaging_socket_path") or "").strip()
                    if address:
                        observed = address
                        observed_in = str(stream)
        except OSError:
            continue
    if observed is None or observed_in is None:
        return None
    return {
        "address": observed,
        "kind": "messaging-socket",
        "observed_in": observed_in,
    }


def _run_row(record: Mapping[str, Any]) -> dict[str, Any]:
    node = _node(record)
    classified = classify_pointer(record)
    row = {
        "run_id": str(record.get("run_id") or ""),
        "node": str(node.get("id") or ""),
        "project": str(record.get("project") or ""),
        "repository": str(record.get("repo") or ""),
        "plan": str(node.get("plan") or ""),
        "section": str(node.get("section") or ""),
        "phase": str(record.get("phase") or ""),
        "classification": str(classified.get("classification") or ""),
        "process_alive": classified.get("process_alive"),
    }
    sprint = record.get("sprint") or node.get("sprint")
    if sprint:
        row["sprint"] = str(sprint)
    transport = _observed_transport(record)
    if transport is not None:
        row["transport"] = transport
    return row


def _is_dispatching(run: Mapping[str, Any]) -> bool:
    """A session remains active while a worker lives or has resumable work."""
    if run.get("process_alive") is True:
        return True
    return str(run.get("classification") or "") in {"running", "waiting"}


def directory(
    project: str | None = None,
    *,
    run_id: str | None = None,
    node_id: str | None = None,
    pointers: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Group every live pointer by its owning coordinator session."""
    if run_id and node_id:
        raise DirectoryError("select by run id or node id, not both")
    records = list(pointers) if pointers is not None else list_live()
    if project not in (None, "*"):
        records = [
            record for record in records if str(record.get("project") or "") == project
        ]

    selected_by: str | None = None
    selected_value: str | None = None
    if run_id:
        selected_by, selected_value = "run", run_id
        records = [record for record in records if record.get("run_id") == run_id]
    elif node_id:
        selected_by, selected_value = "node", node_id
        records = [
            record
            for record in records
            if str(_node(record).get("id") or "") == node_id
        ]
    if selected_by and not records:
        raise DirectoryError(f"no live {selected_by} {selected_value!r}")
    if selected_by == "node" and len(records) > 1:
        matches = ", ".join(
            sorted(str(record.get("run_id") or "") for record in records)
        )
        raise DirectoryError(
            f"node {selected_value!r} is not unique across live runs: {matches}"
        )

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    unowned_runs: list[dict[str, Any]] = []
    for record in records:
        if not str(record.get("session") or ""):
            unowned_runs.append(_run_row(record))
            continue
        key = (
            str(record.get("session") or ""),
            str(record.get("project") or ""),
            str(record.get("repo") or ""),
        )
        grouped[key].append(_run_row(record))

    coordinators: list[dict[str, Any]] = []
    for (session, project_name, repository), runs in sorted(grouped.items()):
        ordered_runs = sorted(runs, key=lambda row: (row["node"], row["run_id"]))
        transports = {
            run["transport"]["address"]: run["transport"]
            for run in ordered_runs
            if "transport" in run
        }
        coordinator = {
            "session": session or None,
            "project": project_name or None,
            "repository": repository or None,
            "plans": sorted({run["plan"] for run in ordered_runs if run["plan"]}),
            "sprints": sorted(
                {str(run["sprint"]) for run in ordered_runs if run.get("sprint")}
            ),
            "state": (
                "dispatching"
                if any(_is_dispatching(run) for run in ordered_runs)
                else "all-terminal"
            ),
            "live_node_count": len(ordered_runs),
            "runs": ordered_runs,
        }
        if transports:
            coordinator["transports"] = [transports[key] for key in sorted(transports)]
        coordinators.append(coordinator)

    result: dict[str, Any] = {
        "ok": True,
        "view": "directory",
        "project": project,
        "coordinator_count": len(coordinators),
        "live_node_count": sum(row["live_node_count"] for row in coordinators),
        "coordinators": coordinators,
        "unowned_run_count": len(unowned_runs),
        "unowned_runs": sorted(unowned_runs, key=lambda row: row["run_id"]),
    }
    if selected_by:
        owned = bool(coordinators)
        run = coordinators[0]["runs"][0] if owned else unowned_runs[0]
        result["resolved"] = {
            "by": selected_by,
            "value": selected_value,
            "run_id": run["run_id"],
            "node": run["node"],
            "session": coordinators[0]["session"] if owned else None,
        }
    return result
