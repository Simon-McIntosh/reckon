"""A landing that was promoted but never merged is finished by the next pass.

``crew complete --land`` promotes first, pushing its promote and release
commits, and merges the reviewed head afterwards. A process killed between the
two leaves a run that reads as promoted, and that a monitor draws as landed,
while its change is on no branch. Nothing else would ever notice.

So ``--land`` writes one small record per run under the crew home before it
promotes, and removes it once the merge is on the branch. The landing pass reads
that directory every pass. A record whose run has a promotion row in the ledger
and whose reviewed head is not on the primary branch is finished with the same
``merge.land`` call ``--land`` makes. A record whose run never promoted is left
while its run is live and dropped once it is old and its run is gone. A merge
that fails is recorded on the record and retried on later passes, up to a bound,
so a conflicting head stops costing a pass and stays visible.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon.crew import runs

#: The directory under the crew home holding one record per landing in flight.
PENDING_DIRNAME = "pending-landings"

#: How many failed finishing attempts a record carries before the pass stops
#: retrying it and leaves it for a coordinator.
MAX_FINISH_ATTEMPTS = 3

#: How long a record whose run never promoted, and whose run is gone, is kept
#: before it is dropped as a landing that was refused or abandoned.
UNPROMOTED_RECORD_SECONDS = 2 * 3600


def pending_dir() -> Path:
    return runs.crew_home() / PENDING_DIRNAME


def _record_path(run_id: str) -> Path:
    return pending_dir() / f"{run_id}.json"


def write_pending(run_id: str, project: str, *, now: float | None = None) -> None:
    """Record that ``run_id`` is about to be promoted and landed. Best-effort."""
    path = _record_path(run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project": project,
                    "written_at": time.time() if now is None else now,
                    "attempts": 0,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def clear_pending(run_id: str) -> None:
    """Remove a run's record once its head is on the branch. Best-effort."""
    try:
        _record_path(run_id).unlink(missing_ok=True)
    except OSError:
        return


def _read(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _promoted_row(project: str, run_id: str) -> Mapping[str, Any] | None:
    """The run's ledger row in the project's mount, or None when it has none."""
    from reckon import ledger
    from reckon.crew import merge

    try:
        repository = merge.project_repository(project)
        path = ledger.run_path(project, run_id, repository)
        if not path.is_file():
            return None
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ledger.LedgerError, merge.MergeError):
        return None
    return row if isinstance(row, Mapping) else None


def _reviewed_head(row: Mapping[str, Any]) -> str:
    promoted = str(row.get("promoted_revision") or "").strip()
    if promoted:
        return promoted
    commits = [str(sha).strip() for sha in (row.get("commits") or ())]
    return next((sha for sha in reversed(commits) if sha), "")


def finish_pending_landings(
    *, now: float | None = None, land=None, live_run_ids=None
) -> list[dict[str, Any]]:
    """Finish every promoted-but-unmerged landing, returning one row per record.

    ``land`` is ``merge.land``-shaped and ``live_run_ids`` the ids with a live
    pointer; both are parameters so a test can drive the pass.
    """
    from reckon.crew import merge

    moment = time.time() if now is None else now
    lander = land or merge.land
    directory = pending_dir()
    if not directory.is_dir():
        return []
    live = (
        set(live_run_ids)
        if live_run_ids is not None
        else {str(pointer.get("run_id") or "") for pointer in runs.list_live()}
    )
    results: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        record = _read(path)
        if record is None:
            continue
        run_id = str(record.get("run_id") or "")
        project = str(record.get("project") or "")
        if not run_id or not project:
            continue
        if int(record.get("attempts") or 0) >= MAX_FINISH_ATTEMPTS:
            results.append({"run_id": run_id, "status": "gave-up"})
            continue
        row = _promoted_row(project, run_id)
        if row is None:
            age = moment - float(record.get("written_at") or moment)
            if run_id not in live and age > UNPROMOTED_RECORD_SECONDS:
                clear_pending(run_id)
                results.append({"run_id": run_id, "status": "dropped-unpromoted"})
            continue
        if run_id in live:
            # The promotion row exists but the --land that wrote it may still be
            # merging: its pointer is removed by the promotion, so a live pointer
            # here is a run being promoted again. Leave it to that process.
            continue
        head = _reviewed_head(row)
        if not head:
            continue
        try:
            repository = merge.project_repository(project)
            landed = lander(
                repository,
                head=head,
                message=(
                    f"land({run_id}): finish a landing interrupted after its promotion"
                ),
                body=(
                    f"Run {run_id} was promoted with --land, but its process "
                    "ended before the reviewed head was merged. The landing "
                    f"pass finishes that merge of {head}."
                ),
            )
        except merge.MergeError as exc:
            record["attempts"] = int(record.get("attempts") or 0) + 1
            record["last_error"] = str(exc)
            with contextlib.suppress(OSError):
                path.write_text(json.dumps(record), encoding="utf-8")
            results.append({"run_id": run_id, "status": "failed", "error": str(exc)})
            continue
        clear_pending(run_id)
        results.append(
            {
                "run_id": run_id,
                "status": "finished",
                "head": head,
                "commit": str((landed or {}).get("commit") or ""),
            }
        )
    return results
