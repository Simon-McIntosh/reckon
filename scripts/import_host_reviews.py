#!/usr/bin/env python3
"""Import a project's host review store into its committed review tree.

    uv run python scripts/import_host_reviews.py --project <project> [--root <path>]
    uv run python scripts/import_host_reviews.py --project <project> --write

The source is the host staging store (``<config home>/crew/reviews/<project>/``)
where a live review is written for the gate to read; the target is the project's
committed tree (``docs/state/<project>/reviews/``), where a record travels
with the plan, the ledger and the evidence. The two differ, which is why this is
a separate script from ``import_runs_into_store.py`` rather than one more command
on the surface: that one copies the committed ledger into the shadow run store,
this one moves staged reviews into the repository.

Dry run by default; ``--write`` acts. Every file whose body is a plan review
(a record naming a plan) or a run review (a record naming the run it reviewed and
the run that produced it) is committed through
:func:`review.store_committed_review`, so its dispatch and completion times are
the run's own, resolved from its review run id, rather than the moment the file
was stored. Two host files that resolve the same committed path — the primary
path and its ``.at-<blob>`` sibling name one review run — import once; a record
already committed is left untouched, so a second immediate pass imports zero.

Everything else under the store — a scratch directory, a cache, a probe file, a
``.keep`` — is listed with its path, size and modification time and, under
``--write``, moved to a quarantine directory outside the store, never deleted.
Deleting quarantined content is a later decision taken on the printed inventory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reckon.crew import review as review_store  # noqa: E402


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a project's host review store into its committed tree"
    )
    parser.add_argument(
        "--project", required=True, help="project whose reviews to import"
    )
    parser.add_argument(
        "--root",
        default=None,
        help="checkout root holding the committed tree when config-home routing is not wanted",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="commit recognised records and quarantine the rest (default: dry run)",
    )
    parser.add_argument(
        "--quarantine",
        default=None,
        help="quarantine directory; defaults beside the store, outside it",
    )
    return parser.parse_args(argv)


def _quarantine_root(store_root: Path, override: str | None) -> Path:
    """Return the quarantine directory, outside the store by construction."""
    if override:
        return Path(override).expanduser().resolve()
    return store_root.parent / "reviews-quarantine"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _review_run_id(body: Mapping[str, Any]) -> str:
    """Return the review run id the record names, whichever field carries it.

    Current records name ``review_run_id``; an older vintage names the same run
    as ``reviewer_run_id`` and leaves ``review_run_id`` absent, so both are read
    and the newer field wins when both are present.
    """
    return str(body.get("review_run_id") or body.get("reviewer_run_id") or "").strip()


def _classify(body: Any) -> str | None:
    """Return ``"plan"``, ``"run"`` or ``None`` for a parsed review body.

    A plan review names the plan it read (``plan_slug`` plus ``plan_version``); a
    run review names the run it reviewed (``reviewed_run_id``). Both carry the
    review run id that keys their committed file, so a body missing it is not a
    filable record and is quarantined like any other file rather than retried.
    """
    if not isinstance(body, Mapping):
        return None
    if not _review_run_id(body):
        return None
    if str(body.get("plan_slug") or "").strip():
        try:
            int(body.get("plan_version"))
        except (TypeError, ValueError):
            return None
        return "plan"
    if str(body.get("reviewed_run_id") or "").strip():
        return "run"
    return None


def _committed_path(
    project: str, body: Mapping[str, Any], committed_root: Path
) -> Path:
    """Return the committed path a recognised record resolves, without writing.

    The path comes from the two store owners rather than being spelled again
    here, so the importer and the writer that lands the record agree on where it
    goes by construction.
    """
    review_run_id = _review_run_id(body)
    plan_slug = str(body.get("plan_slug") or "").strip()
    if plan_slug:
        from reckon.crew import plan_review

        return plan_review.plan_review_path(
            project,
            plan_slug,
            int(body["plan_version"]),
            committed_root=committed_root,
            review_run_id=review_run_id,
        )
    return review_store.review_path(
        project,
        str(body["reviewed_run_id"]).strip(),
        committed_root=committed_root,
        review_run_id=review_run_id,
    )


def _entry_size(path: Path) -> int:
    """Size of a file, or the summed size of a directory's files."""
    try:
        if not path.is_dir():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _iso_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        return ""


def _plan(project: str, store_root: Path, committed_root: Path) -> dict[str, Any]:
    """Sort the store's entries into recognised records and non-records.

    Only the store's top-level entries are considered: a record is a file whose
    body is a review, and a directory is one non-record however many files it
    holds, because it is the directory that is quarantined. Two entries that
    resolve one committed path are duplicates and the first sorted path wins.
    """
    records: list[tuple[Path, Mapping[str, Any], Path]] = []
    non_records: list[Path] = []
    duplicates: list[tuple[Path, Path]] = []
    seen: dict[Path, Path] = {}

    directory = store_root / project
    entries = sorted(directory.iterdir()) if directory.is_dir() else []
    for entry in entries:
        body = _read_json(entry) if entry.is_file() else None
        if _classify(body) is None:
            non_records.append(entry)
            continue
        committed = _committed_path(project, body, committed_root)
        if committed in seen:
            duplicates.append((entry, seen[committed]))
            continue
        seen[committed] = entry
        records.append((entry, body, committed))
    return {"records": records, "non_records": non_records, "duplicates": duplicates}


def _print_inventory(project: str, store_root: Path, plan: dict[str, Any]) -> None:
    records = plan["records"]
    non_records = plan["non_records"]
    duplicates = plan["duplicates"]
    plan_count = sum(1 for _src, body, _path in records if _classify(body) == "plan")
    run_count = len(records) - plan_count
    record_bytes = sum(_entry_size(src) for src, _body, _path in records)
    non_record_bytes = sum(_entry_size(src) for src in non_records)
    print(f"project: {project}")
    print(f"store: {store_root / project}")
    print(
        f"recognised records: {len(records)} "
        f"(plan reviews: {plan_count}, run reviews: {run_count}, bytes: {record_bytes})"
    )
    for src, _body, _path in records:
        print(f"  record\t{src}\t{_entry_size(src)}\t{_iso_mtime(src)}")
    print(f"non-records: {len(non_records)} (bytes: {non_record_bytes})")
    for src in non_records:
        print(f"  non-record\t{src}\t{_entry_size(src)}\t{_iso_mtime(src)}")
    print(f"duplicates: {len(duplicates)}")
    for src, first in duplicates:
        print(
            f"  duplicate\t{src}\t{_entry_size(src)}\t{_iso_mtime(src)}\tfirst: {first.name}"
        )


def _quarantine(
    src: Path, project_store: Path, quarantine_root: Path, project: str
) -> Path:
    """Move one non-record outside the store, preserving its relative path.

    The move targets ``<quarantine_root>/<project>/<relative path>`` so two
    projects' same-named scratch never collide. Nothing is deleted: the source
    leaves the store by being moved, and a destination already holding a file of
    that name is left alone rather than overwritten.
    """
    relative = src.relative_to(project_store)
    dest = quarantine_root / project / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    shutil.move(str(src), str(dest))
    return dest


def _commit(project: str, record: Mapping[str, Any], root: str | None) -> Path:
    """Commit one recognised record through the shared committed writer.

    An older record that named its review run only as ``reviewer_run_id`` is
    normalised first, because the committed writer keys the file by
    ``review_run_id`` and refuses a record that does not carry it.
    """
    body = dict(record)
    body.setdefault("review_run_id", _review_run_id(record))
    return review_store.store_committed_review(body, project=project, root=root)


def _import_records(
    project: str,
    plan: dict[str, Any],
    root: str | None,
) -> dict[str, int]:
    """Commit recognised records; a path already committed is not imported again."""
    imported = 0
    already_present = 0
    refused: list[str] = []
    for _src, body, committed in plan["records"]:
        if committed.is_file():
            already_present += 1
            continue
        try:
            _commit(project, body, root)
        except (OSError, ValueError) as exc:
            refused.append(f"{committed.name}: {type(exc).__name__}: {exc}")
            continue
        imported += 1
    for message in refused:
        print(f"refused: {message}")
    return {
        "imported": imported,
        "already_present": already_present,
        "duplicates": len(plan["duplicates"]),
        "refused": len(refused),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse(list(argv) if argv is not None else sys.argv[1:])
    store_root = review_store.review_store_root()
    project_store = store_root / args.project
    committed_root = review_store.committed_review_root(args.project, root=args.root)
    if committed_root is None:
        print(f"no committed reviews tree resolves for project {args.project!r}")
        return 1

    plan = _plan(args.project, store_root, committed_root)
    _print_inventory(args.project, store_root, plan)

    if not args.write:
        print("dry run: nothing written")
        return 0

    if project_store.is_dir():
        quarantine_root = _quarantine_root(store_root, args.quarantine)
        moved = 0
        for src in plan["non_records"]:
            _quarantine(src, project_store, quarantine_root, args.project)
            moved += 1
        print(f"quarantined: {moved} -> {quarantine_root / args.project}")

    counts = _import_records(args.project, plan, args.root)
    print(f"imported: {counts['imported']}")
    print(f"already present: {counts['already_present']}")
    print(f"refused: {counts['refused']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
