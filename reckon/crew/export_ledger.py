"""Periodic ledger snapshots outside git and explicit store reconstruction."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon import _store, run_store
from reckon.crew.run_paths import crew_home


def export_path(project: str) -> Path:
    """Resolve a project export under the crew home, refusing a git checkout."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project):
        raise ValueError(f"unsafe export project {project!r}")
    target = (crew_home() / "exports" / project / "crew.json").resolve()
    if any((parent / ".git").exists() for parent in target.parents):
        raise ValueError(f"ledger export must live outside git: {target}")
    return target


def _validate(snapshot: Any, project: str) -> dict[str, Any]:
    """Refuse empty, foreign or malformed history before writing anything."""
    if not isinstance(snapshot, dict) or snapshot.get("project") != project:
        raise ValueError(f"ledger export does not name project {project!r}")
    data = snapshot.get("data")
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), list) for key in ("runs", "members", "holds")
    ):
        raise ValueError("ledger export must carry runs, members and holds lists")
    runs = data["runs"]
    if not runs:
        raise ValueError(f"refusing an empty ledger export for {project!r}")
    ids = []
    for row in runs:
        if not isinstance(row, dict) or not row.get("run_id"):
            raise ValueError("ledger export has a run without its identity")
        if row.get("project") != project:
            raise ValueError("ledger export has a run from another project")
        ids.append(row["run_id"])
    if len(set(ids)) != len(ids):
        raise ValueError("ledger export has duplicate run identities")
    version = data.get("_version")
    if type(version) is not int or version < 0:
        raise ValueError("ledger export has no valid mutable-state version")
    for key, identity in (("members", "id"), ("holds", "hold_id")):
        entries = data[key]
        if any(not isinstance(row, dict) or not row.get(identity) for row in entries):
            raise ValueError(f"ledger export has an invalid {key} identity")
        if len({row[identity] for row in entries}) != len(entries):
            raise ValueError(f"ledger export has duplicate {key} identities")
    revisions = snapshot.get("run_revisions")
    if not isinstance(revisions, dict) or any(
        key not in ids or not isinstance(value, str) or not value
        for key, value in revisions.items()
    ):
        raise ValueError("ledger export has invalid run revisions")
    return data


def export_project(project: str) -> dict[str, Any]:
    """Publish the complete readable record; no detail is discarded here."""
    target = export_path(project)
    snapshot = run_store.project_snapshot(project)
    data = _validate(snapshot, project)
    _store.write_json_atomically(target, snapshot, fsync_directory=True)
    return {"project": project, "path": str(target), "rows": len(data["runs"])}


def export_projects(
    projects: Mapping[str, Any], *, apply: bool
) -> list[dict[str, Any]]:
    """Export on the retention cadence, reporting each project's refusal."""
    if not apply:
        return []
    reports = []
    for project in sorted(projects):
        try:
            reports.append({"status": "written", **export_project(project)})
        except (OSError, ValueError, run_store.UnreadableRunStoreError) as error:
            reports.append(
                {"project": project, "status": "failed", "error": str(error)}
            )
    return reports


def rebuild_store(
    project: str, source: str | Path, destination: str | Path
) -> dict[str, Any]:
    """Reconstruct a fresh store without replacing any existing database."""
    snapshot = json.loads(Path(source).read_text(encoding="utf-8"))
    data = _validate(snapshot, project)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an existing store, including a concurrent
    # reconstruction. An interrupted import remains visible for inspection.
    with target.open("xb"):
        pass
    with run_store.RunStore(target) as store:
        store.auxiliary(project, lambda: (data, data["_version"]))
        for row in data["runs"]:
            store.append(project, row)
        store.stamp_revisions(project, snapshot["run_revisions"])
    return {"project": project, "path": str(target), "rows": len(data["runs"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--rebuild-from", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    if bool(args.rebuild_from) != bool(args.destination):
        parser.error("--rebuild-from and --destination are required together")
    report = (
        rebuild_store(args.project, args.rebuild_from, args.destination)
        if args.rebuild_from
        else export_project(args.project)
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
