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

Dry run by default; ``--write`` acts. Every file that carries review material
and has a subject — a plan or a run the body names, or, when the body names
neither, the run the filename names under the store's
``<reviewed-run-id>[.at-<head>].json`` grammar — is committed through
:func:`review.store_committed_review`, so its dispatch and completion times are
the run's own, resolved from its review run id, rather than the moment the file
was stored. Two host files that resolve the same committed path import once; a
record already committed is left untouched, so a second immediate pass imports
zero.

Everything else under the store — a scratch directory, a cache, a probe file, a
``.keep`` — is listed with its path, size and modification time and, under
``--write``, moved to a quarantine directory outside the store, never deleted.
Deleting quarantined content is a later decision taken on the printed inventory.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reckon.crew import plan_review  # noqa: E402
from reckon.crew import review as review_store  # noqa: E402
from reckon.crew.runs import reports_dir  # noqa: E402


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


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _explicit_review_run_id(body: Mapping[str, Any]) -> str:
    """Return the review run id the record names, whichever field carries it.

    Current records name ``review_run_id``; an older vintage names the same run
    as ``reviewer_run_id`` and leaves ``review_run_id`` absent, so both are read
    and the committed file keeps whatever key the record supplied.
    """
    return str(body.get("review_run_id") or body.get("reviewer_run_id") or "").strip()


def _subject_kind(body: Mapping[str, Any]) -> str | None:
    """Return ``"plan"``, ``"run"`` or ``None`` for the subject a body names.

    A body that names the run it reviews is a run review, whatever plan the run
    carried: a run review records its reviewed run's ``plan_slug`` and no
    ``plan_version``, so the reviewed run is tested first and a plan is a
    subject only when it carries an integer ``plan_version``. A plan slug with
    no usable version and no reviewed run names no subject.
    """
    if str(body.get("reviewed_run_id") or "").strip():
        return "run"
    if str(body.get("plan_slug") or "").strip():
        try:
            int(body.get("plan_version"))
        except (TypeError, ValueError):
            return None
        return "plan"
    return None


# The staging store names a run review by the run it reviewed:
# ``<reviewed-run-id>.json``, with a ``.at-<head>`` sibling when the reviewed
# head is named. A record whose body omits the reviewed run still has it in the
# filename, so the store's own grammar recovers the subject the body dropped.
_AT_SIBLING = re.compile(r"^(?P<id>.+)\.at-[0-9a-f]{7,40}$")


def _filename_run_subject(name: str | None) -> str | None:
    """Return the reviewed run id a staging filename names, or ``None``.

    A run review is staged as ``<reviewed-run-id>.json`` or its
    ``<reviewed-run-id>.at-<head>.json`` sibling, so the stem before the
    optional ``.at-<head>`` suffix is the reviewed run. A name that is not a
    JSON file, or whose stem is empty, names nothing.
    """
    if not name or not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    match = _AT_SIBLING.match(stem)
    if match:
        stem = match.group("id")
    return stem.strip() or None


def _review_identity(
    raw: bytes, body: Any, filename: str | None = None
) -> tuple[str, str, str, bool] | None:
    """Return ``(kind, subject, review_run_id, derived)`` for a review, else ``None``.

    A file is a review record when it carries review material and has a subject:
    a plan or a run its body names, or — when the body names neither — the run
    the filename names under the store's ``<reviewed-run-id>[.at-<head>].json``
    grammar. A review run id is not required; one that names none is filed under
    a derived, stable legacy id.
    """
    if not isinstance(body, Mapping) or not review_store.carries_review_material(body):
        return None
    kind = _subject_kind(body)
    if kind == "plan":
        subject = str(body["plan_slug"]).strip()
    elif kind == "run":
        subject = str(body["reviewed_run_id"]).strip()
    else:
        # The body named no usable subject; a body that named one it could not
        # use (a plan with no version) is refused rather than misread as a run.
        if (
            str(body.get("plan_slug") or "").strip()
            or str(body.get("reviewed_run_id") or "").strip()
        ):
            return None
        subject = _filename_run_subject(filename)
        if not subject:
            return None
        kind = "run"
    explicit = _explicit_review_run_id(body)
    if explicit:
        return kind, subject, explicit, False
    return kind, subject, review_store.derived_legacy_review_run_id(raw), True


def _committed_path(
    project: str,
    kind: str,
    body: Mapping[str, Any],
    subject: str,
    review_run_id: str,
    committed_root: Path,
) -> Path:
    """Return the committed path a recognised record resolves, without writing.

    The path comes from the two store owners rather than being spelled again
    here, so the importer and the writer that lands the record agree on where it
    goes by construction. The subject is the plan slug or the reviewed run id,
    which may come from the body or from the staging filename.
    """
    if kind == "plan":
        from reckon.crew import plan_review

        return plan_review.plan_review_path(
            project,
            subject,
            int(body["plan_version"]),
            committed_root=committed_root,
            review_run_id=review_run_id,
        )
    return review_store.review_path(
        project,
        subject,
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

    Only the store's top-level entries are considered: a record is a file that
    carries review material and names a subject (in its body or, for a run
    review, in its filename), and a directory is one non-record however many files it
    holds, because it is the directory that is quarantined. Two entries that
    resolve one committed path are duplicates and the first sorted path wins.
    """
    records: list[tuple[Path, Mapping[str, Any], Path, str, str, bool, str]] = []
    non_records: list[Path] = []
    duplicates: list[tuple[Path, Path]] = []
    seen: dict[Path, Path] = {}

    directory = store_root / project
    entries = sorted(directory.iterdir()) if directory.is_dir() else []
    for entry in entries:
        if not entry.is_file():
            non_records.append(entry)
            continue
        raw = _read_bytes(entry)
        body = _parse_json(raw)
        identity = _review_identity(raw, body, entry.name)
        if identity is None:
            non_records.append(entry)
            continue
        kind, subject, review_run_id, derived = identity
        committed = _committed_path(
            project, kind, body, subject, review_run_id, committed_root
        )
        if committed in seen:
            duplicates.append((entry, seen[committed]))
            continue
        seen[committed] = entry
        records.append((entry, body, committed, subject, review_run_id, derived, kind))
    return {"records": records, "non_records": non_records, "duplicates": duplicates}


def _print_inventory(project: str, store_root: Path, plan: dict[str, Any]) -> None:
    records = plan["records"]
    non_records = plan["non_records"]
    duplicates = plan["duplicates"]
    plan_count = sum(1 for *_rest, kind in records if kind == "plan")
    run_count = len(records) - plan_count
    derived_count = sum(1 for *_rest, derived, _kind in records if derived)
    record_bytes = sum(_entry_size(rec[0]) for rec in records)
    non_record_bytes = sum(_entry_size(src) for src in non_records)
    print(f"project: {project}")
    print(f"store: {store_root / project}")
    print(
        f"recognised records: {len(records)} "
        f"(plan reviews: {plan_count}, run reviews: {run_count}, "
        f"derived review run ids: {derived_count}, bytes: {record_bytes})"
    )
    for rec in records:
        print(f"  record\t{rec[0]}\t{_entry_size(rec[0])}\t{_iso_mtime(rec[0])}")
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


def _commit(
    project: str,
    body: Mapping[str, Any],
    kind: str,
    subject: str,
    review_run_id: str,
    derived: bool,
    root: str | None,
) -> Path:
    """Commit one recognised record through the shared committed writer.

    The committed writer keys the file by ``review_run_id`` and files a run
    review under the reviewed run named in the body, so both the identity this
    import resolved and — when the body omitted it and the staging filename
    supplied it — the reviewed run are written in. A record that named no review
    run of its own carries the derived legacy id and is marked
    ``review_run_id_source: derived``, so a later reader knows the id was
    synthesised rather than read from the record.
    """
    stored = dict(body)
    stored["review_run_id"] = review_run_id
    if kind == "run":
        stored["reviewed_run_id"] = subject
    if derived:
        stored["review_run_id_source"] = "derived"
    return review_store.store_committed_review(stored, project=project, root=root)


def _import_records(
    project: str,
    plan: dict[str, Any],
    root: str | None,
) -> dict[str, int]:
    """Commit recognised records; a path already committed is not imported again."""
    imported = 0
    already_present = 0
    refused: list[str] = []
    for _src, body, committed, subject, review_run_id, derived, kind in plan["records"]:
        if committed.is_file():
            already_present += 1
            continue
        try:
            _commit(project, body, kind, subject, review_run_id, derived, root)
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


def _delivered_plan_slugs(project: str) -> list[str]:
    """Return the plan slugs a project's report root holds delivered reviews for.

    The report root is ``reports/<project>/plan-review/<slug>/`` and the plans
    are its directories, so the enumeration reads the same layout
    :func:`plan_review.delivered_reports` builds on rather than walking for a
    ``report.md`` by hand.
    """
    root = reports_dir() / project / "plan-review"
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def _committed_plan_records(committed_root: Path) -> list[Mapping[str, Any]]:
    """Return the plan-review record bodies committed under a project's tree."""
    plan_root = committed_root / review_store.COMMITTED_PLAN_DIRNAME
    if not plan_root.is_dir():
        return []
    records: list[Mapping[str, Any]] = []
    for path in sorted(plan_root.glob("*/*.json")):
        body = _parse_json(_read_bytes(path))
        if isinstance(body, Mapping):
            records.append(body)
    return records


def _carried_reports(
    project: str, committed_root: Path
) -> tuple[set[str], set[tuple[str, str, str]]]:
    """The review run ids and (plan, fingerprint, rubric) triples records carry.

    A record carries a delivered report when its ``review_run_id`` equals the
    report directory's name, or when its ``plan_slug``, ``plan_fingerprint``
    and ``rubric`` equal the sidecar's. Both trees are read: the committed tree
    the import targets, and the staging store whose records the import commits
    in the same pass, so a report whose content is already staged is not
    imported twice.
    """
    run_ids: set[str] = set()
    triples: set[tuple[str, str, str]] = set()
    records: list[Mapping[str, Any]] = [
        *_committed_plan_records(committed_root),
        *plan_review.list_plan_reviews(project),
    ]
    for record in records:
        run_id = str(record.get("review_run_id") or "").strip()
        if run_id:
            run_ids.add(run_id)
        slug = str(record.get("plan_slug") or "").strip()
        if slug:
            triples.add(
                (
                    slug,
                    str(record.get("plan_fingerprint") or ""),
                    str(record.get("rubric") or ""),
                )
            )
    return run_ids, triples


def _report_is_review(report_path: Path) -> bool:
    """Whether a report file carries a RUBRIC or FINDING line, so it is a review."""
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return plan_review.report_carries_review_lines(text)


def _delivered_report_candidates(
    project: str, committed_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Delivered reports no record carries, split into importable and refused.

    Returns ``(candidates, refused)``. Each candidate is a sidecar-and-record
    pair; each refused entry is a sidecar and the reason its record could not be
    built, so a report that is a review but cannot be parsed is listed and
    counted rather than silently dropped. A report a record already carries — by
    review run id or by plan, fingerprint and rubric — is skipped, so a second
    pass imports zero. A directory whose report carries no ``RUBRIC`` or
    ``FINDING`` line is not a review and is neither returned nor counted.
    """
    run_ids, triples = _carried_reports(project, committed_root)
    candidates: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for slug in _delivered_plan_slugs(project):
        for sidecar in plan_review.delivered_reports(project, slug):
            run_id = str(sidecar.get("review_run_id") or "").strip()
            triple = (
                str(sidecar.get("plan_slug") or ""),
                str(sidecar.get("plan_fingerprint") or ""),
                str(sidecar.get("rubric") or ""),
            )
            if run_id in run_ids or triple in triples:
                continue
            if not _report_is_review(Path(str(sidecar.get("report_path") or ""))):
                continue
            try:
                record = plan_review.delivered_report_record(sidecar)
            except (OSError, ValueError) as exc:
                refused.append(
                    {"sidecar": sidecar, "reason": f"{type(exc).__name__}: {exc}"}
                )
                continue
            candidates.append({"sidecar": sidecar, "record": record})
    return candidates, refused


def _print_delivered(
    candidates: list[dict[str, Any]], refused: list[dict[str, Any]]
) -> None:
    """List each unrecorded and each refused delivered report, with a count line."""
    print(
        f"delivered reports without a record: {len(candidates)}, "
        f"refused: {len(refused)}"
    )
    for item in candidates:
        report_path = str(item["sidecar"].get("report_path") or "")
        print(f"  delivered\t{report_path}")
    for item in refused:
        report_path = str(item["sidecar"].get("report_path") or "")
        print(f"  refused\t{report_path}\t{item['reason']}")


def _import_delivered(
    project: str,
    candidates: list[dict[str, Any]],
    refused: list[dict[str, Any]],
    root: str | None,
) -> dict[str, int]:
    """Commit each unrecorded delivered report; record each refused report's reason.

    A report the store commits has any stale ``store_error`` cleared from its
    sidecar, so the file never contradicts the record beside it; a report whose
    record could not be built carries the reason under ``store_error`` on its
    sidecar, the same way the staging store records a refusal.
    """
    for item in refused:
        plan_review._record_store_error(item["sidecar"], item["reason"])
    imported = 0
    refused_store: list[str] = []
    for item in candidates:
        record = item["record"]
        try:
            review_store.store_committed_review(record, project=project, root=root)
        except (OSError, ValueError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            refused_store.append(f"{record.get('review_run_id')}: {reason}")
            plan_review._record_store_error(item["sidecar"], reason)
            continue
        plan_review._clear_store_error(item["sidecar"])
        imported += 1
    for message in refused_store:
        print(f"refused: {message}")
    return {"imported": imported, "refused": len(refused) + len(refused_store)}


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

    delivered, delivered_refused = _delivered_report_candidates(
        args.project, committed_root
    )
    _print_delivered(delivered, delivered_refused)

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
    delivered_counts = _import_delivered(
        args.project, delivered, delivered_refused, args.root
    )
    print(f"delivered imported: {delivered_counts['imported']}")
    print(f"delivered refused: {delivered_counts['refused']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
