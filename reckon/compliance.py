"""Check every document against the render contract and keep the verdicts.

``reckon audit-doc`` checks a document when someone runs it, and nothing runs it
on its own. Discovery meanwhile tolerates a malformed document without saying
so: a truncated file lists as if whole, and an empty or binary one drops out of
the listing. This module runs the same check (:func:`reckon.doccheck.audit_file`)
for every live document in a project and keeps each verdict beside the stat
identity of every file the check read — the document, and for a landed evidence
record its fragment directory and fragments. A verdict whose files have moved
is never served; it is pending until it is recomputed.

A whole project is checked out of process, by ``python -m reckon.compliance
refresh``, because one check costs about a tenth of a second on a shared
filesystem and a large project holds hundreds of documents: the served process
must not spend that time holding requests. Verdicts persist under the
configuration home's cache directory, so a restart reuses them.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from reckon import doccheck, resources
from reckon._store import _config_home, write_json_atomically
from reckon.file_memo import file_signature

LOGGER = logging.getLogger("reckon.compliance")

_SCHEMA = "reckon.compliance-verdicts"
_VERSION = 1
#: The resource types a project lists as documents.
_DOC_TYPES = frozenset({"plan", "research", "evidence"})
#: A verdict keeps at most this many findings; the counts stay exact.
_MAX_FINDINGS = 20
#: A refresh persists its progress this often, so a reader sees verdicts
#: arrive while a large project is still being checked.
_BATCH = 25


def check_document(path: Path, project: str) -> dict:
    """Return one document's verdict: error and warning counts and findings."""

    try:
        findings = doccheck.audit_file(Path(path), project=project)
    except Exception as exc:  # noqa: BLE001 — a failed check is itself a verdict
        return {
            "errors": 1,
            "warnings": 0,
            "findings": [
                {
                    "severity": "error",
                    "code": "check-failed",
                    "message": f"the check itself failed: {type(exc).__name__}: {exc}",
                }
            ],
        }
    kept = [f for f in findings if f.severity in ("error", "warn")]
    return {
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warn"),
        "findings": [
            {"severity": f.severity, "code": f.code, "message": f.message}
            for f in kept[:_MAX_FINDINGS]
        ],
    }


def _signature_or_none(path: Path) -> list[int] | None:
    """Return a file's identity without its device number, or None if absent.

    A shared filesystem reports a different device number for the same file on
    each host — measured 41 on a compute node and 65 on a login node — while
    the inode, size and both nanosecond timestamps agree. The served process
    and a command run on another node share one store, so the identity a
    verdict is kept against leaves the device number out.
    """

    try:
        return list(file_signature(path))[1:]
    except OSError:
        return None


def _dependencies(path: Path) -> list[Path]:
    """Return the files besides ``path`` that its check reads.

    A landed evidence record is checked in its composed form, so its fragment
    directory (whose identity moves when a fragment is added or removed) and
    each fragment in it are part of what the verdict was computed from.
    """

    from reckon.evidence import evidence_record_plan

    plan = evidence_record_plan(path)
    if plan is None:
        return []
    fragment_dir = doccheck._record_fragment_dir(path, plan)
    fragments = sorted(fragment_dir.glob("*.html")) if fragment_dir.is_dir() else []
    return [fragment_dir, *fragments]


def _identity(path: Path) -> dict:
    return {
        "self": _signature_or_none(path),
        "deps": [[str(dep), _signature_or_none(dep)] for dep in _dependencies(path)],
    }


def _is_current(path: Path, identity: object) -> bool:
    if not isinstance(identity, dict):
        return False
    own = identity.get("self")
    if own is None or _signature_or_none(path) != own:
        return False
    deps = identity.get("deps")
    if not isinstance(deps, list):
        return False
    return all(
        isinstance(dep, list)
        and len(dep) == 2
        and _signature_or_none(Path(dep[0])) == dep[1]
        for dep in deps
    )


# ── Persisted store ────────────────────────────────────────────────────────


def _store_path(docs_dir: Path, project: str) -> Path:
    identity = f"{project}\0{Path(docs_dir).resolve()}".encode()
    digest = hashlib.sha256(identity).hexdigest()
    return _config_home() / "cache" / "compliance" / f"{digest}.json"


@contextlib.contextmanager
def _locked(docs_dir: Path, project: str) -> Iterator[None]:
    """Serialise writers — the server, a refresh process, the CLI — on one store."""

    lock_path = _store_path(docs_dir, project).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def stored_verdicts(docs_dir: Path, project: str) -> dict[str, dict]:
    """Return the persisted entries by docs-relative path, current or not."""

    path = _store_path(docs_dir, project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable compliance store %s: %s", path, exc)
        return {}
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != _SCHEMA
        or raw.get("version") != _VERSION
        or raw.get("project") != project
        or raw.get("docs_dir") != str(Path(docs_dir).resolve())
        or not isinstance(raw.get("entries"), dict)
    ):
        LOGGER.warning("Ignoring incompatible compliance store %s", path)
        return {}
    return {
        key: entry
        for key, entry in raw["entries"].items()
        if isinstance(entry, dict) and isinstance(entry.get("check"), dict)
    }


def _write_entries(
    docs_dir: Path,
    project: str,
    updates: dict[str, dict],
    *,
    keep: set[str] | None = None,
) -> None:
    """Merge ``updates`` into the store; with ``keep``, drop every other path."""

    with _locked(docs_dir, project):
        entries = stored_verdicts(docs_dir, project)
        entries.update(updates)
        if keep is not None:
            entries = {key: entry for key, entry in entries.items() if key in keep}
        payload = {
            "schema": _SCHEMA,
            "version": _VERSION,
            "project": project,
            "docs_dir": str(Path(docs_dir).resolve()),
            "entries": entries,
        }
        try:
            write_json_atomically(
                _store_path(docs_dir, project),
                payload,
                fsync=False,
                indent=None,
                ensure_ascii=False,
            )
        except OSError as exc:
            LOGGER.warning(
                "Could not persist compliance verdicts for %s: %s", project, exc
            )


# ── Readers ────────────────────────────────────────────────────────────────


def _documents(docs_dir: Path, project: str) -> list[resources.Resource]:
    resolved = resources.resource_map(
        Path(docs_dir), project, include_archived=False, ignore_invalid=True
    )
    return sorted(
        (r for r in resolved.values() if r.type in _DOC_TYPES),
        key=lambda r: r.relative_path.as_posix(),
    )


def relative_path(docs_dir: Path, path: Path) -> str:
    """Return ``path`` relative to the docs root, as the store keys it."""

    try:
        return path.relative_to(docs_dir).as_posix()
    except ValueError:
        return path.resolve().relative_to(docs_dir.resolve()).as_posix()


def _relative(docs_dir: Path, resource: resources.Resource) -> str:
    return relative_path(docs_dir, resource.path)


def project_checks(docs_dir: Path, project: str) -> dict:
    """Return the project's failing documents and how many are still pending.

    A document is pending when it has no stored verdict or when any file its
    verdict was computed from has moved since. Only documents with at least one
    error are listed; warnings are counted on each listed document but do not
    list one on their own.
    """

    docs_dir = Path(docs_dir)
    stored = stored_verdicts(docs_dir, project)
    listed: list[dict] = []
    checked = pending = 0
    for resource in _documents(docs_dir, project):
        relative = _relative(docs_dir, resource)
        entry = stored.get(relative)
        if entry is None or not _is_current(resource.path, entry.get("identity")):
            pending += 1
            continue
        checked += 1
        verdict = entry["check"]
        if verdict.get("errors"):
            listed.append(
                {
                    "type": resource.type,
                    "slug": resource.slug,
                    "archived": resource.archived,
                    "path": relative,
                    **verdict,
                }
            )
    return {
        "project": project,
        "checked": checked,
        "pending": pending,
        "failing": len(listed),
        "documents": listed,
    }


def document_check(docs_dir: Path, project: str, path: Path) -> dict:
    """Return one document's verdict, computing and storing it when stale."""

    docs_dir = Path(docs_dir)
    path = Path(path)
    relative = relative_path(Path(docs_dir), path)
    entry = stored_verdicts(docs_dir, project).get(relative)
    if entry is not None and _is_current(path, entry.get("identity")):
        return dict(entry["check"])
    identity = _identity(path)
    verdict = check_document(path, project)
    if _is_current(path, identity):
        # A write that landed mid-check leaves the verdict unstored, so the
        # next reader checks the new bytes rather than serving a torn verdict.
        _write_entries(
            docs_dir, project, {relative: {"identity": identity, "check": verdict}}
        )
    return verdict


@dataclass
class RefreshResult:
    project: str
    checked: int = 0
    reused: int = 0
    removed: int = 0
    failing: int = 0
    seconds: float = 0.0


def refresh(docs_dir: Path, project: str) -> RefreshResult:
    """Check every live document whose verdict is missing or stale."""

    started = time.monotonic()
    docs_dir = Path(docs_dir)
    result = RefreshResult(project=project)
    stored = stored_verdicts(docs_dir, project)
    present: set[str] = set()
    batch: dict[str, dict] = {}
    for resource in _documents(docs_dir, project):
        relative = _relative(docs_dir, resource)
        present.add(relative)
        entry = stored.get(relative)
        if entry is not None and _is_current(resource.path, entry.get("identity")):
            result.reused += 1
            continue
        identity = _identity(resource.path)
        verdict = check_document(resource.path, project)
        result.checked += 1
        if _is_current(resource.path, identity):
            batch[relative] = {"identity": identity, "check": verdict}
        if len(batch) >= _BATCH:
            _write_entries(docs_dir, project, batch)
            batch = {}
    result.removed = len(set(stored) - present)
    if batch or result.removed or not _store_path(docs_dir, project).exists():
        _write_entries(docs_dir, project, batch, keep=present)
    result.failing = project_checks(docs_dir, project)["failing"]
    result.seconds = round(time.monotonic() - started, 2)
    return result


# ── Command line ───────────────────────────────────────────────────────────


def _mounts(mounts_file: Path | None) -> dict[str, Path]:
    if mounts_file is None:
        return doccheck._load_mounts()
    raw = json.loads(Path(mounts_file).read_text(encoding="utf-8"))
    return {name: Path(path).expanduser() for name, path in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reckon.compliance")
    commands = parser.add_subparsers(dest="command", required=True)
    refresh_parser = commands.add_parser(
        "refresh", help="check every document whose verdict is missing or stale"
    )
    refresh_parser.add_argument("--project", action="append", default=[])
    refresh_parser.add_argument("--mounts", type=Path, default=None)
    args = parser.parse_args(argv)

    mounts = _mounts(args.mounts)
    names = args.project or sorted(mounts)
    unknown = [name for name in names if name not in mounts]
    if unknown:
        print(f"compliance: unknown project(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    for name in names:
        docs_dir = mounts[name]
        if not docs_dir.is_dir():
            print(f"compliance: {name}: {docs_dir} is not a directory", file=sys.stderr)
            continue
        result = refresh(docs_dir, name)
        print(
            f"compliance: {name}: checked {result.checked}, reused {result.reused}, "
            f"removed {result.removed}, failing {result.failing} "
            f"in {result.seconds} s",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
