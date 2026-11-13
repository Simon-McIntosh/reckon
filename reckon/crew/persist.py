"""Persist a report item: a verified copy outside the crew home and its record.

A report item a run wrote is working material and expires with the run. The
one way it outlives that is a deliberate persist: the item is copied into the
project's own persist root, each copy is checked against its source's byte
count and content hash, and one record per copy is appended to the project's
persisted-items resource.

Reckon carries no default persist root and no project name, so a project that
has not declared ``reports.persist_root`` in its flight configuration cannot
persist anything, and the refusal names that setting. The records live in the
project's mounted ``docs`` tree, read and written through
:mod:`reckon.project_state` so their version check and atomic write are the
same ones every other typed resource uses.
"""

from __future__ import annotations

import getpass
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import flight
from reckon._store import _config_home
from reckon.project_state import (
    _sha256_path,
    read_resource,
    resource_path,
    write_resource,
)

RESOURCE_TYPE = "persisted_items"
RESOURCE_ID = "persisted-items"
PERSIST_ROOT_SETTING = f"{flight.REPORTS_KEY}.{flight.PERSIST_ROOT_KEY}"

KIND_FIGURE = "figure"
KIND_DATA = "data"
RECORD_KINDS = (KIND_FIGURE, KIND_DATA)


class PersistError(RuntimeError):
    """A persist request refused, either before copying or at verification."""


def persisted_items_path(docs_dir: Path, project: str) -> Path:
    """Canonical path of a project's persisted-items resource."""
    return resource_path(docs_dir, project, RESOURCE_TYPE, RESOURCE_ID)


def _mounted_docs(project: str) -> Path:
    """The project's mounted docs directory, refusing an unmounted project."""
    docs_dir = flight.mounted_project_docs().get(project)
    if docs_dir is None:
        raise PersistError(
            f"project {project!r} is not mounted, so it has no docs tree to "
            "record a persisted item in"
        )
    return docs_dir


def persist_root(project: str) -> Path:
    """Resolve the project's declared persist root, refusing an unset one.

    The root must lie outside the crew home: a persisted item is precisely what
    the crew home's retention rules must never remove, so a root inside it
    would defer that decision rather than make it.
    """
    resolved = flight.resolve(project=project).config
    declared = flight.reports_setting(resolved, flight.PERSIST_ROOT_KEY)
    if declared is None:
        raise PersistError(
            f"project {project!r} declares no {PERSIST_ROOT_SETTING} in its "
            "flight configuration, so there is no root to persist into"
        )
    root = Path(declared).expanduser()
    crew_home = (_config_home() / "crew").resolve()
    if Path(root).resolve().is_relative_to(crew_home):
        raise PersistError(
            f"{PERSIST_ROOT_SETTING} for project {project!r} resolves inside "
            f"the crew home ({crew_home}); a persist root must lie outside it"
        )
    return Path(root)


def read_persisted(
    project: str,
    *,
    plan: str | None = None,
    kind: str | None = None,
    docs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the project's persisted-item records, optionally filtered.

    ``plan`` keeps only the records attributed to that plan; ``kind`` keeps
    only figures or only the data files behind a figure.
    """
    docs = docs_dir if docs_dir is not None else _mounted_docs(project)
    if kind is not None and kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {', '.join(RECORD_KINDS)}")
    if not persisted_items_path(docs, project).exists():
        return []
    data, _version = read_resource(docs, project, RESOURCE_TYPE, RESOURCE_ID)
    records = list(data.get("records", []))
    if plan is not None:
        records = [record for record in records if record.get("plan") == plan]
    if kind is not None:
        records = [record for record in records if record.get("kind") == kind]
    return records


def _copy_bytes(source: Path, destination: Path) -> None:
    """Copy one file's bytes; separate so a test can intervene at the copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _verify_copy(source: Path, destination: Path) -> tuple[int, str]:
    """Check a copy's byte count and content hash against its source."""
    source_bytes = source.stat().st_size
    copied_bytes = destination.stat().st_size
    if copied_bytes != source_bytes:
        raise PersistError(
            f"copy of {source} is {copied_bytes} bytes against the source's "
            f"{source_bytes}"
        )
    source_hash = _sha256_path(source)
    copied_hash = _sha256_path(destination)
    if copied_hash != source_hash:
        raise PersistError(
            f"copy of {source} hashes {copied_hash} against the source's {source_hash}"
        )
    return copied_bytes, copied_hash


def _destination(
    root: Path, project: str, kind: str, source: Path, digest: str
) -> Path:
    """Where one copy lives inside the root, fixed by its own content."""
    return root / project / kind / f"{digest[:12]}-{source.name}"


def _attribute_plan(docs_dir: Path, project: str, source: Path) -> str | None:
    """The plan a source path can be attributed to, when one is identifiable.

    A report tree is named for the plan that produced it, so a path component
    equal to one of the project's plan slugs attributes the item to that plan.
    """
    from reckon.resources import resource_map

    slugs = {
        resource.slug
        for resource in resource_map(
            docs_dir, project, include_archived=False, ignore_invalid=True
        ).values()
        if resource.type == "plan"
    }
    for part in reversed(source.parts[:-1]):
        if part in slugs:
            return part
    return None


def _existing_record(
    docs_dir: Path, project: str, source: Path, digest: str
) -> dict[str, Any] | None:
    """The record already holding this source at this content hash."""
    source_path = str(source.resolve())
    for record in read_persisted(project, docs_dir=docs_dir):
        if record.get("source") == source_path and record.get("sha256") == digest:
            return record
    return None


def _append_records(
    docs_dir: Path, project: str, records: list[dict[str, Any]]
) -> None:
    """Append records to the resource, creating it on the first persist."""
    path = persisted_items_path(docs_dir, project)
    if path.exists():
        current, version = read_resource(docs_dir, project, RESOURCE_TYPE, RESOURCE_ID)
        create = False
    else:
        current, version, create = {}, 0, True
    write_resource(
        docs_dir,
        project,
        RESOURCE_TYPE,
        RESOURCE_ID,
        {**current, "records": [*current.get("records", []), *records]},
        version,
        create=create,
    )


def persist_item(
    figure: str | Path,
    data_files: list[str | Path] | tuple[str | Path, ...] = (),
    *,
    project: str,
    plan: str | None = None,
    section: str | None = None,
    persisted_by: str | None = None,
) -> list[dict[str, Any]]:
    docs = _mounted_docs(project)
    return persist_into(
        figure,
        data_files,
        project=project,
        docs_dir=docs,
        plan=plan,
        section=section,
        persisted_by=persisted_by,
    )


def persist_into(
    figure: str | Path,
    data_files: list[str | Path] | tuple[str | Path, ...] = (),
    *,
    project: str,
    docs_dir: Path,
    plan: str | None = None,
    section: str | None = None,
    persisted_by: str | None = None,
) -> list[dict[str, Any]]:
    """Persist one figure and any named data files, returning their records.

    Every copy is verified before any record is written, so a copy that does
    not match its source is refused and leaves no record behind. A repeat
    persist of a source whose bytes are unchanged returns the record already
    held for it and writes nothing.
    """
    root = persist_root(project)
    who = persisted_by or getpass.getuser()
    when = datetime.now(UTC).isoformat(timespec="seconds")

    figure_path = Path(figure)
    sources: list[tuple[Path, str]] = [(figure_path, KIND_FIGURE)]
    sources.extend((Path(extra), KIND_DATA) for extra in data_files)
    missing = [str(source) for source, _kind in sources if not source.is_file()]
    if missing:
        raise PersistError("nothing to persist at " + ", ".join(missing))

    attributed_plan = (
        plan if plan is not None else _attribute_plan(docs_dir, project, figure_path)
    )

    records: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    created: list[Path] = []
    try:
        for source, kind in sources:
            digest = _sha256_path(source)
            existing = _existing_record(docs_dir, project, source, digest)
            if existing is not None:
                records.append(existing)
                continue
            destination = _destination(root, project, kind, source, digest)
            destination.unlink(missing_ok=True)
            _copy_bytes(source, destination)
            created.append(destination)
            size, copied_hash = _verify_copy(source, destination)
            record = {
                "source": str(source.resolve()),
                "persisted": str(destination),
                "kind": kind,
                "figure": (None if kind == KIND_FIGURE else str(figure_path.resolve())),
                "plan": attributed_plan,
                "section": section,
                "persisted_by": who,
                "persisted_at": when,
                "bytes": size,
                "sha256": copied_hash,
            }
            records.append(record)
            fresh.append(record)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise

    if fresh:
        _append_records(docs_dir, project, fresh)
    return records
