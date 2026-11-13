"""Durable end-of-life cleanup, with a bounded crash-recovery queue."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon.crew import runs


def archive_dir(run_id: str) -> Path:
    """An ended run's durable home, outside the live directory traversal."""
    return runs.crew_home() / "archive" / "runs" / run_id


def cleanup_path(run_id: str) -> Path:
    return runs.crew_home() / "cleanup" / f"{run_id}.json"


def queue_cleanup(record: Mapping[str, Any], *, kind: str, gate: str = "") -> None:
    """Persist recovery inputs before the operation retires the live pointer."""
    if gate and gate.strip().lower() not in {"passed", "not-run"}:
        return
    run_id = str(record.get("run_id") or "")
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
        raise runs.CrewError("cleanup requires a single run directory name")
    if not record.get("repo"):
        return
    runs._write_json(
        cleanup_path(run_id),
        {"run_id": run_id, "kind": kind, "gate": gate, "record": dict(record)},
    )


def archive_ended_run(
    record: Mapping[str, Any],
    release: Mapping[str, Any],
    *,
    report_removals: bool = False,
) -> dict[str, Any]:
    """Move a released run intact, preserving its evidence and a terminal receipt.

    A live pointer, a withheld worktree, or an unfinished scratch removal keeps
    the directory where it is. The pending receipt survives every such refusal
    and every crash; it is retired only after the archive is readable.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id:
        raise runs.CrewError("cleanup requires a single run directory name")
    if runs.pointer_path(run_id).exists():
        return {}
    if not record.get("repo"):
        return {"run_archive_withheld": "repository ownership is unavailable"}
    tree = str(record.get("worktree") or "")
    if tree and Path(tree).exists():
        return {}
    if release.get("scratch_withheld") and not release.get("scratch_absent"):
        return {}
    source = runs.runs_dir() / run_id
    destination = archive_dir(run_id)
    if source.is_symlink() or destination.is_symlink():
        raise runs.CrewError("cleanup refuses a symlinked run directory")
    pending = cleanup_path(run_id)
    if not source.is_dir() and not destination.is_dir():
        if pending.exists():
            pending.unlink()
            if report_removals:
                print(f"removed cleanup receipt {pending}", file=sys.stderr, flush=True)
        return {}
    try:
        intent = json.loads(pending.read_text())
    except FileNotFoundError:
        intent = {"run_id": run_id, "kind": "landed", "record": dict(record)}
        runs._write_json(pending, intent)
    if source.is_dir():
        if destination.exists():
            raise runs.CrewError(
                f"both live and archived run directories exist: {run_id}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
    else:
        destination.mkdir(parents=True, exist_ok=True)
    receipt = destination / "end.json"
    if not receipt.is_file():
        runs._write_json(receipt, {**intent, "release": dict(release)})
    if report_removals:
        print(
            f"archived run directory {source} to {destination}",
            file=sys.stderr,
            flush=True,
        )
        for path in release.get("scratch_removed_paths", ()):
            print(f"removed scratch {path}", file=sys.stderr, flush=True)
        if release.get("worktree_released"):
            print(f"removed worktree {tree}", file=sys.stderr, flush=True)
    pending.unlink(missing_ok=True)
    if report_removals:
        print(f"removed cleanup receipt {pending}", file=sys.stderr, flush=True)
    return {"run_archived": str(destination)}


def _read_cleanup_intent(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    run_id = str(payload["run_id"])
    if path != cleanup_path(run_id) or payload["record"].get("run_id") != run_id:
        raise runs.CrewError("cleanup receipt does not name its own run")
    return payload


def sweep_ended_runs(
    *, limit: int = 20, repo: Path | None = None
) -> list[dict[str, Any]]:
    """Retry at most ``limit`` interrupted terminal operations, never live runs.

    Workspace release reuses the gc classification and its conservative
    refusals. A nonblocking run lock keeps a sweep from delaying an ending
    operation, and a live pointer always wins over an older cleanup intent.
    """
    from reckon.crew.promotion_release import _release_run_workspace

    root = runs.crew_home() / "cleanup"
    if limit < 1:
        raise runs.CrewError("cleanup slice must have a positive limit")
    if not root.is_dir():
        return []
    result = []
    cursor = runs.crew_home() / "cleanup-cursor.json"
    try:
        after = str(json.loads(cursor.read_text()).get("after") or "")
    except (OSError, ValueError):
        after = ""
    paths = sorted(root.glob("*.json"))
    candidates = [path for path in paths if path.name > after] or paths
    # Only the pending queue is listed; archived run directories and the
    # historical ledger are outside this traversal. The cursor gives a
    # withheld operation no power to starve later cleanup intents.
    for path in candidates[:limit]:
        try:
            payload = _read_cleanup_intent(path)
            record = payload["record"]
            if (
                repo is not None
                and Path(str(record.get("repo") or "")).resolve() != repo.resolve()
            ):
                continue
            run_id = str(payload["run_id"])
            with runs._pointer_lock(run_id, blocking=False):
                if runs.pointer_path(run_id).exists():
                    result.append({"run_id": run_id, "withheld": "live pointer"})
                    continue
                gate = str(payload.get("gate") or "passed")
                if payload.get("kind") == "discarded":
                    gate = ""
                retention = record.get("worktree_retention")
                release = _release_run_workspace(
                    record,
                    retention if isinstance(retention, Mapping) else None,
                    gate=gate,
                    report_removals=True,
                )
                result.append({"run_id": run_id, **release})
        except (OSError, ValueError, KeyError, runs.CrewError) as exc:
            result.append({"path": str(path), "withheld": str(exc)})
    if candidates:
        last = candidates[min(limit, len(candidates)) - 1]
        runs._write_json(
            cursor, {"after": last.name if len(candidates) > limit else ""}
        )
    return result
