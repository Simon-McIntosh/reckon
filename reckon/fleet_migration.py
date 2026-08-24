"""Transactional migration of every repository in the active mount registry.

The fleet layer deliberately separates three concerns:

* discovery and preflight are read-only against mounted repositories;
* every repository receives a content-bearing snapshot before any mutation;
* only explicitly selected projects are staged, verified, and installed.

Repository commits and pushes remain an orchestrator responsibility.  The
machine ledger records the verified working-tree paths first and can be amended
with the resulting commit and push ref after repository policy has been
honoured.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from reckon import _plan_html
from reckon._schema import PlanState
from reckon._store import _config_home, _mounts_path
from reckon.capability import from_legacy_tier
from reckon.project_state import (
    ProjectStateError,
    migrate_project_state,
    project_state_mode,
    read_resource,
    write_resource,
)
from reckon.resources import (
    INFRA_DIRS,
    NON_RESOURCE_FILES,
    ROOT_TYPES,
    ResourceCollision,
    build_migration_manifest,
    canonical_type,
    iter_resources,
    migrate_typed_layout,
)

FLEET_FORMAT = 1
MIGRATION_VERSION = "1"
TERMINAL_STATES = frozenset({"deferred", "verified", "rolled-back"})
SNAPSHOT_CONTENT_SUFFIXES = frozenset({".html", ".json"})


class FleetMigrationError(RuntimeError):
    """A repository cannot complete the reviewed migration transaction."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(encoded)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_git(repo_root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise FleetMigrationError(
            f"git {' '.join(args)} failed in {repo_root}: {detail}"
        )
    return completed.stdout.rstrip("\n")


def _safe_name(value: str, label: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise FleetMigrationError(f"{label} must be one safe path segment")
    return value


def _registry(path: Path | None = None) -> tuple[Path, bytes, dict[str, Path]]:
    registry_path = (path or _mounts_path()).expanduser().resolve()
    try:
        raw = registry_path.read_bytes()
        decoded = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetMigrationError(
            f"mount registry is unreadable: {registry_path}: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise FleetMigrationError("mount registry must be a project-to-path object")
    mounts: dict[str, Path] = {}
    resolved_targets: dict[Path, str] = {}
    for project, raw_path in sorted(decoded.items()):
        _safe_name(str(project), "project")
        if not isinstance(raw_path, str) or not raw_path:
            raise FleetMigrationError(f"mount {project!r} has no docs path")
        docs_dir = Path(raw_path).expanduser().resolve()
        previous = resolved_targets.get(docs_dir)
        if previous is not None:
            raise FleetMigrationError(
                f"duplicate mounted docs path {docs_dir}: {previous!r}, {project!r}"
            )
        resolved_targets[docs_dir] = str(project)
        mounts[str(project)] = docs_dir
    return registry_path, raw, mounts


def discover_registry(path: Path | None = None) -> dict[str, Any]:
    """Return the authoritative runtime registry and its immutable run digest."""
    registry_path, raw, mounts = _registry(path)
    return {
        "path": str(registry_path),
        "sha256": _sha256_bytes(raw),
        "projects": [
            {"project": project, "docs_dir": str(docs_dir)}
            for project, docs_dir in mounts.items()
        ],
    }


def _tree_inventory(docs_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(docs_dir.rglob("*")):
        relative = path.relative_to(docs_dir).as_posix()
        if path.is_symlink():
            rows.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": os.readlink(path),
                }
            )
        elif path.is_file():
            rows.append(
                {
                    "path": relative,
                    "kind": "file",
                    "size": path.stat().st_size,
                    "mode": stat.S_IMODE(path.stat().st_mode),
                    "sha256": _sha256_path(path),
                }
            )
    return rows


def _snapshot_content_path(relative: str) -> str:
    return f"contents/{PurePosixPath(relative).as_posix()}"


def _content_bearing(relative: str) -> bool:
    path = PurePosixPath(relative)
    return path.suffix.lower() in SNAPSHOT_CONTENT_SUFFIXES


def create_snapshot(
    docs_dir: Path,
    destination: Path,
    *,
    project: str,
    repository: dict[str, Any],
    registry_sha256: str,
) -> dict[str, Any]:
    """Create an idempotent, content-bearing before snapshot."""
    docs_dir = docs_dir.resolve()
    destination = destination.resolve()
    inventory = _tree_inventory(docs_dir)
    manifest = {
        "format": FLEET_FORMAT,
        "migration_version": MIGRATION_VERSION,
        "project": project,
        "docs_dir": str(docs_dir),
        "registry_sha256": registry_sha256,
        "repository": repository,
        "inventory": inventory,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        with zipfile.ZipFile(destination) as archive:
            existing = archive.read("manifest.json")
        if existing != manifest_bytes:
            raise FleetMigrationError(f"snapshot collision: {destination}")
        return {
            "path": str(destination),
            "sha256": _sha256_path(destination),
            "files": len(inventory),
            "content_files": sum(
                row["kind"] == "file" and _content_bearing(row["path"])
                for row in inventory
            ),
        }

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            for row in inventory:
                if row["kind"] != "file" or not _content_bearing(row["path"]):
                    continue
                archive.write(
                    docs_dir / row["path"],
                    arcname=_snapshot_content_path(row["path"]),
                )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(destination),
        "sha256": _sha256_path(destination),
        "files": len(inventory),
        "content_files": sum(
            row["kind"] == "file" and _content_bearing(row["path"]) for row in inventory
        ),
    }


def repository_inventory(docs_dir: Path, project: str) -> dict[str, Any]:
    """Return migration-relevant before/after counts without strict discovery."""
    docs_dir = docs_dir.resolve()
    counts = {
        "plan": 0,
        "research": 0,
        "evidence": 0,
        "sprint": 0,
        "milestone": 0,
        "blocker": 0,
        "timeline": 0,
        "project": 0,
    }
    typed = legacy = legacy_capabilities = 0
    html_files = 0
    for path in sorted(docs_dir.rglob("*.html")):
        relative = path.relative_to(docs_dir)
        if not relative.parts:
            continue
        root_type = ROOT_TYPES.get(relative.parts[0])
        if root_type is not None:
            counts[root_type] += 1
            typed += 1
            if root_type not in {"plan", "research", "evidence"}:
                continue
        elif relative.parts[0] == "state":
            if path.name == "timeline.html":
                counts["timeline"] += 1
            continue
        elif relative.parts[0] in INFRA_DIRS or path.name in NON_RESOURCE_FILES:
            continue
        else:
            legacy += 1
        html_files += 1
        try:
            state = _plan_html.read_state(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if root_type is None:
            artifact_type = canonical_type(state.get("type"))
            if artifact_type in {"plan", "research", "evidence"}:
                counts[artifact_type] += 1
        legacy_capabilities += int(bool(state.get("tier")))
        legacy_capabilities += sum(
            bool(item.get("tier")) for item in state.get("followups", [])
        )

    mode = project_state_mode(docs_dir)
    if mode.format == "distributed":
        project_path = docs_dir / "state" / project / "project.json"
        counts["project"] = int(project_path.is_file())
        for sprint_path in sorted((docs_dir / "sprints").glob("*.html")):
            try:
                sprint, _ = read_resource(docs_dir, project, "sprint", sprint_path.stem)
            except (OSError, ValueError, ProjectStateError):
                continue
            legacy_capabilities += sum(
                bool(item.get("tier")) for item in sprint.get("items", [])
            )
    else:
        index_path = docs_dir / "state" / project / "index.json"
        try:
            envelope = json.loads(index_path.read_text())
            index = envelope.get("data", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            index = {}
        for sprint in index.get("sprints", []) if isinstance(index, dict) else []:
            if not isinstance(sprint, dict):
                continue
            legacy_capabilities += sum(
                isinstance(item, dict) and bool(item.get("tier"))
                for item in sprint.get("items", [])
            )
    return {
        "html_files": html_files,
        "resources": counts,
        "layout": {"typed": typed, "legacy": legacy},
        "legacy_capabilities": legacy_capabilities,
        "project_state": mode.format,
    }


def snapshot_inventory(snapshot: Path) -> dict[str, Any]:
    """Reconstruct inventory evidence from a content-bearing snapshot."""
    manifest = _snapshot_manifest(snapshot)
    holder = tempfile.TemporaryDirectory(prefix="reckon-snapshot-inventory-")
    docs_dir = Path(holder.name) / "docs"
    docs_dir.mkdir()
    try:
        with zipfile.ZipFile(snapshot) as archive:
            for name in archive.namelist():
                if not name.startswith("contents/"):
                    continue
                relative = PurePosixPath(name).relative_to("contents")
                if (
                    not relative.parts
                    or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    raise FleetMigrationError(
                        f"snapshot contains an unsafe content path: {name}"
                    )
                destination = docs_dir.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
        return repository_inventory(
            docs_dir,
            str(manifest.get("project") or ""),
        )
    finally:
        holder.cleanup()


def _snapshot_manifest(snapshot: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(snapshot) as archive:
            value = json.loads(archive.read("manifest.json"))
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise FleetMigrationError(f"snapshot is unreadable: {snapshot}: {exc}") from exc
    if not isinstance(value, dict) or value.get("format") != FLEET_FORMAT:
        raise FleetMigrationError(f"snapshot format is unsupported: {snapshot}")
    return value


def _repository_state(docs_dir: Path) -> dict[str, Any]:
    if not docs_dir.is_dir():
        raise FleetMigrationError(f"mounted docs directory does not exist: {docs_dir}")
    repo_root = (
        Path(_run_git(docs_dir, "rev-parse", "--show-toplevel")).expanduser().resolve()
    )
    branch = _run_git(repo_root, "branch", "--show-current")
    head = _run_git(repo_root, "rev-parse", "HEAD")
    status = _run_git(
        repo_root, "status", "--porcelain=v1", "--untracked-files=all"
    ).splitlines()
    worktree_rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _run_git(repo_root, "worktree", "list", "--porcelain").splitlines():
        if not line:
            if current:
                worktree_rows.append(current)
                current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                worktree_rows.append(current)
            current = {"path": value}
        elif current is not None:
            current[key] = value or True
    if current:
        worktree_rows.append(current)
    upstream = _run_git(
        repo_root,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        check=False,
    )
    ahead = behind = None
    if upstream:
        counts = _run_git(
            repo_root,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...{upstream}",
        ).split()
        if len(counts) == 2:
            ahead, behind = (int(counts[0]), int(counts[1]))
    return {
        "root": str(repo_root),
        "branch": branch,
        "head": head,
        "upstream": upstream or None,
        "ahead": ahead,
        "behind": behind,
        "status": status,
        "worktrees": worktree_rows,
    }


def _status_path(line: str) -> str:
    value = line[3:] if len(line) >= 4 else line
    if " -> " in value:
        value = value.split(" -> ", 1)[1]
    return value.strip().strip('"')


def _overlaps_migration_path(repo_root: Path, docs_dir: Path, value: str) -> bool:
    try:
        docs_relative = docs_dir.relative_to(repo_root)
    except ValueError:
        return True
    path = PurePosixPath(value)
    prefix = PurePosixPath(docs_relative.as_posix())
    try:
        relative = path.relative_to(prefix)
    except ValueError:
        return False
    if not relative.parts:
        return True
    if relative.suffix.lower() == ".html":
        return True
    return relative.parts[0] in {"state", ".reckon"}


def preflight_repository(docs_dir: Path, project: str) -> dict[str, Any]:
    """Inspect one repository and return exact blockers without mutation."""
    docs_dir = docs_dir.resolve()
    blockers: list[dict[str, Any]] = []
    try:
        repository = _repository_state(docs_dir)
    except FleetMigrationError as exc:
        return {
            "ok": False,
            "repository": {},
            "blockers": [{"code": "repository-unreadable", "detail": str(exc)}],
        }
    repo_root = Path(repository["root"])
    if not repository["branch"]:
        blockers.append(
            {
                "code": "detached-mounted-checkout",
                "detail": "mounted checkout has no current branch",
            }
        )
    alternate_worktree_blockers: list[dict[str, Any]] = []
    for row in repository["worktrees"]:
        alternate = Path(str(row["path"])).resolve()
        if alternate == repo_root:
            continue
        alternate_docs = alternate / "docs"
        if not alternate_docs.is_dir():
            continue
        try:
            alternate_status = _run_git(
                alternate,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ).splitlines()
        except FleetMigrationError:
            continue
        overlap = [
            _status_path(line)
            for line in alternate_status
            if _overlaps_migration_path(
                alternate, alternate_docs, _status_path(line)
            )
        ]
        if overlap:
            alternate_worktree_blockers.append(
                {"path": str(alternate), "paths": sorted(set(overlap))}
            )
    if alternate_worktree_blockers:
        blockers.append(
            {
                "code": "dirty-alternate-worktrees",
                "worktrees": alternate_worktree_blockers,
                "detail": "alternate worktrees have migration-relevant uncommitted changes",
            }
        )
    overlap = [
        _status_path(line)
        for line in repository["status"]
        if _overlaps_migration_path(repo_root, docs_dir, _status_path(line))
    ]
    if overlap:
        blockers.append(
            {
                "code": "dirty-migration-paths",
                "paths": sorted(overlap),
                "detail": "migration-relevant docs paths contain uncommitted changes",
            }
        )
    if repository.get("behind"):
        blockers.append(
            {
                "code": "upstream-behind",
                "detail": f"mounted branch is {repository['behind']} commit(s) behind",
            }
        )
    try:
        manifest = build_migration_manifest(docs_dir, project)
        layout_moves = len(manifest.get("moves", []))
    except (OSError, ValueError, ResourceCollision) as exc:
        layout_moves = 0
        blockers.append({"code": "layout-preflight", "detail": str(exc)})
    mode = project_state_mode(docs_dir)
    index_path = docs_dir / "state" / project / "index.json"
    if mode.format != "distributed" and not index_path.is_file():
        blockers.append(
            {
                "code": "missing-project-index",
                "detail": str(index_path),
            }
        )
    return {
        "ok": not blockers,
        "repository": repository,
        "blockers": blockers,
        "layout_moves": layout_moves,
        "project_state": mode.format,
    }


def _atomic_text(path: Path, value: str) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _map_capability(
    record: dict[str, Any], *, context: str
) -> tuple[dict[str, Any], bool]:
    changed = False
    tier = str(record.get("tier") or "").strip()
    if tier:
        if not record.get("capability"):
            capability, diagnostic = from_legacy_tier(tier)
            if capability is None:
                raise FleetMigrationError(
                    f"{context}: cannot migrate legacy tier {tier!r}: {diagnostic}"
                )
            record["capability"] = capability
        record.pop("tier", None)
        changed = True
    return record, changed


def migrate_capabilities(repo_root: Path, project: str) -> dict[str, Any]:
    """Persist neutral plan/followup/sprint capability state."""
    docs_dir = repo_root / "docs"
    changed_paths: list[str] = []
    converted = 0
    for resource in iter_resources(docs_dir, project, include_archived=True):
        if resource.type not in {"plan", "research", "evidence"}:
            continue
        text = resource.path.read_text(encoding="utf-8")
        state = _plan_html.read_state(text)
        state, changed = _map_capability(state, context=f"{resource.identity.key} plan")
        followups: list[dict[str, Any]] = []
        for followup in state.get("followups", []):
            mapped, followup_changed = _map_capability(
                dict(followup),
                context=f"{resource.identity.key} followup {followup.get('id')}",
            )
            changed = changed or followup_changed
            converted += int(followup_changed)
            followups.append(mapped)
        if "followups" in state:
            state["followups"] = followups
        if not changed:
            continue
        state["version"] = int(state.get("version", 0) or 0) + 1
        state["modified"] = date.today().isoformat()
        typed = PlanState.model_validate(state)
        typed.validate_for_write()
        _atomic_text(resource.path, _plan_html.write_state(text, state))
        changed_paths.append(resource.relative_path.as_posix())
        converted += 1

    if project_state_mode(docs_dir).format == "distributed":
        for sprint_path in sorted((docs_dir / "sprints").glob("*.html")):
            sprint_id = sprint_path.stem
            sprint, version = read_resource(docs_dir, project, "sprint", sprint_id)
            sprint_changed = False
            items: list[dict[str, Any]] = []
            for item in sprint.get("items", []):
                mapped, item_changed = _map_capability(
                    dict(item), context=f"sprint {sprint_id} item {item.get('slug')}"
                )
                sprint_changed = sprint_changed or item_changed
                converted += int(item_changed)
                items.append(mapped)
            if not sprint_changed:
                continue
            sprint["items"] = items
            write_resource(
                docs_dir,
                project,
                "sprint",
                sprint_id,
                sprint,
                version,
            )
            changed_paths.append(sprint_path.relative_to(docs_dir).as_posix())
    return {
        "converted": converted,
        "paths": sorted(set(changed_paths)),
    }


def _tree_hashes(root: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if relative.parts[:2] == (".reckon", "locks"):
            continue
        if path.name.endswith(".tmp"):
            continue
        rows[relative.as_posix()] = _sha256_path(path)
    return rows


def _tree_diff(before: Path, after: Path) -> dict[str, list[str]]:
    before_hashes = _tree_hashes(before)
    after_hashes = _tree_hashes(after)
    before_paths = set(before_hashes)
    after_paths = set(after_hashes)
    return {
        "created": sorted(after_paths - before_paths),
        "modified": sorted(
            path
            for path in before_paths & after_paths
            if before_hashes[path] != after_hashes[path]
        ),
        "deleted": sorted(before_paths - after_paths),
    }


def _verify_repository(stage_root: Path, project: str) -> dict[str, Any]:
    from reckon.doccheck import audit_file, audit_links
    from reckon.mcp import _audit, _read_plan

    docs_dir = stage_root / "docs"
    resources = list(iter_resources(docs_dir, project, include_archived=True))
    html_paths = [
        resource.path
        for resource in resources
        if resource.type in {"plan", "research", "evidence"}
    ]
    document_findings: list[dict[str, Any]] = []
    link_findings = audit_links(html_paths, docs_dir, project=project)
    for path in html_paths:
        for finding in [
            *audit_file(path, project=project),
            *link_findings.get(path, []),
        ]:
            if finding.severity == "error":
                document_findings.append(
                    {
                        "path": path.relative_to(docs_dir).as_posix(),
                        "code": finding.code,
                        "message": finding.message,
                    }
                )
    audit = _audit(project, checkout_path=str(stage_root))
    audit_errors = [
        finding
        for finding in audit.get("findings", [])
        if finding.get("severity") == "error"
    ]
    legacy_capability_count = repository_inventory(docs_dir, project)[
        "legacy_capabilities"
    ]
    if (
        document_findings
        or audit.get("violations")
        or audit_errors
        or legacy_capability_count
    ):
        raise FleetMigrationError(
            "verification failed: "
            f"document_errors={len(document_findings)}, "
            f"schema_violations={len(audit.get('violations', []))}, "
            f"audit_errors={len(audit_errors)}, "
            f"legacy_capabilities={legacy_capability_count}; "
            f"documents={document_findings[:5]!r}"
        )
    summary = _read_plan(
        project=project,
        checkout_path=str(stage_root),
        view="summary",
        limit=25,
        include_followups=False,
        include_questions=False,
    )
    if summary.get("ok") is False or summary.get("view") != "summary":
        raise FleetMigrationError(
            f"progressive discovery verification failed: {summary}"
        )

    build_parent = Path(tempfile.mkdtemp(prefix="reckon-static-build-"))
    build_docs = build_parent / "docs"
    try:
        shutil.copytree(docs_dir, build_docs, symlinks=True)
        from reckon.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            main.main(
                args=["build", str(build_docs), "--project", project],
                standalone_mode=False,
            )
        build_result = {
            "ok": True,
            "output_tail": output.getvalue().splitlines()[-3:],
        }
    except Exception as exc:  # noqa: BLE001 - preserve verification evidence
        raise FleetMigrationError(f"static build failed: {exc}") from exc
    finally:
        shutil.rmtree(build_parent)
    return {
        "document_errors": 0,
        "schema_checked": audit.get("checked", 0),
        "schema_violations": 0,
        "audit_errors": 0,
        "legacy_capabilities": 0,
        "progressive_resources": summary.get("pagination", {}).get("total", 0),
        "static_build": build_result,
    }


def _restore_changed_paths(
    snapshot: Path,
    docs_dir: Path,
    changed_paths: Iterable[str],
) -> None:
    manifest = _snapshot_manifest(snapshot)
    inventory = {
        row["path"]: row
        for row in manifest.get("inventory", [])
        if isinstance(row, dict) and row.get("kind") == "file"
    }
    with zipfile.ZipFile(snapshot) as archive:
        for relative in sorted(set(changed_paths)):
            target = docs_dir / PurePosixPath(relative)
            before = inventory.get(relative)
            if before is None:
                target.unlink(missing_ok=True)
                continue
            if not _content_bearing(relative):
                raise FleetMigrationError(
                    f"snapshot lacks content required to restore {relative}"
                )
            content = archive.read(_snapshot_content_path(relative))
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                temporary.write_bytes(content)
                os.chmod(temporary, int(before.get("mode", 0o644)))
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)


def rollback_repository(
    snapshot: Path,
    docs_dir: Path,
    changed_paths: Iterable[str],
) -> dict[str, Any]:
    """Restore exactly the migration paths recorded for one repository."""
    snapshot = snapshot.resolve()
    docs_dir = docs_dir.resolve()
    manifest = _snapshot_manifest(snapshot)
    if Path(str(manifest.get("docs_dir"))).resolve() != docs_dir:
        raise FleetMigrationError("snapshot docs path does not match rollback target")
    selected = sorted(set(changed_paths))
    _restore_changed_paths(snapshot, docs_dir, selected)
    return {"ok": True, "restored": selected}


def _install_stage(
    original_docs: Path,
    staged_docs: Path,
    diff: dict[str, list[str]],
    snapshot: Path,
    install_hook: Any | None = None,
) -> None:
    changed = [*diff["created"], *diff["modified"], *diff["deleted"]]
    manifest = _snapshot_manifest(snapshot)
    before = {
        row["path"]: row
        for row in manifest.get("inventory", [])
        if isinstance(row, dict) and row.get("kind") == "file"
    }
    for relative in [*diff["modified"], *diff["deleted"]]:
        current = original_docs / relative
        expected = before.get(relative, {}).get("sha256")
        if not current.is_file() or _sha256_path(current) != expected:
            raise FleetMigrationError(f"source changed after snapshot: {relative}")
    try:
        for position, relative in enumerate([*diff["created"], *diff["modified"]]):
            if install_hook:
                install_hook(position, relative)
            source = staged_docs / relative
            destination = original_docs / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(source, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        for relative in diff["deleted"]:
            (original_docs / relative).unlink()
    except Exception:
        _restore_changed_paths(snapshot, original_docs, changed)
        raise


def migrate_repository(
    docs_dir: Path,
    project: str,
    snapshot: Path,
    *,
    staging_parent: Path,
    install_hook: Any | None = None,
) -> dict[str, Any]:
    """Stage, fully verify, then atomically install one repository migration."""
    docs_dir = docs_dir.resolve()
    repo_root = Path(_run_git(docs_dir, "rev-parse", "--show-toplevel")).resolve()
    staging_parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=f"{project}-", dir=staging_parent))
    stage_docs = stage_root / "docs"
    try:
        shutil.copytree(docs_dir, stage_docs, symlinks=True)
        capability = migrate_capabilities(stage_root, project)
        layout = migrate_typed_layout(stage_docs, project)
        project_state = migrate_project_state(stage_docs, project)
        distributed_capability = migrate_capabilities(stage_root, project)
        verification = _verify_repository(stage_root, project)
        diff = _tree_diff(docs_dir, stage_docs)
        _install_stage(
            docs_dir,
            stage_docs,
            diff,
            snapshot,
            install_hook=install_hook,
        )
        return {
            "ok": True,
            "repository_root": str(repo_root),
            "working_tree": str(repo_root),
            "changes": diff,
            "capabilities": {
                "converted": capability["converted"]
                + distributed_capability["converted"],
                "paths": sorted(
                    set(capability["paths"]) | set(distributed_capability["paths"])
                ),
            },
            "typed_layout": {
                "moves": len(layout.get("moves", [])),
                "rewrites": len(layout.get("rewrites", [])),
                "manifest": ".reckon/typed-resource-manifest.json",
            },
            "project_state": {
                "changed": bool(project_state.get("changed")),
                "source_sha256": project_state.get("source_sha256"),
                "parity_sha256": project_state.get("parity_sha256"),
                "marker": ".reckon/project-state-migration.json",
            },
            "verification": verification,
        }
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    rows = ledger.get("repositories", [])
    ledger["complete"] = bool(rows) and all(
        row.get("state") in TERMINAL_STATES for row in rows
    )
    _atomic_json(path, ledger)


def _snapshot_matches(snapshot: Path, docs_dir: Path) -> tuple[bool, list[str]]:
    manifest = _snapshot_manifest(snapshot)
    before = {
        row["path"]: row.get("sha256")
        for row in manifest.get("inventory", [])
        if isinstance(row, dict) and row.get("kind") == "file"
    }
    current = _tree_hashes(docs_dir)
    differences = sorted(
        {
            *set(before).symmetric_difference(current),
            *{
                path
                for path in set(before) & set(current)
                if before[path] != current[path]
            },
        }
    )
    return not differences, differences


def run_fleet_migration(
    *,
    mounts_path: Path | None = None,
    output_dir: Path | None = None,
    run_id: str | None = None,
    apply_projects: Iterable[str] = (),
) -> dict[str, Any]:
    """Snapshot all mounts and migrate only the explicitly selected projects."""
    registry_path, registry_bytes, mounts = _registry(mounts_path)
    selected = {_safe_name(value, "selected project") for value in apply_projects}
    unknown = sorted(selected - set(mounts))
    if unknown:
        raise FleetMigrationError(
            "selected projects are not registered: " + ", ".join(unknown)
        )
    identifier = _safe_name(run_id or _new_run_id(), "run id")
    root = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else _config_home() / "migrations" / identifier
    )
    root.mkdir(parents=True, exist_ok=True)
    ledger_path = root / "ledger.json"
    registry_sha = _sha256_bytes(registry_bytes)
    if ledger_path.is_file():
        try:
            ledger = json.loads(ledger_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise FleetMigrationError(
                f"existing ledger is unreadable: {ledger_path}: {exc}"
            ) from exc
        if (
            ledger.get("format") != FLEET_FORMAT
            or ledger.get("migration_version") != MIGRATION_VERSION
            or ledger.get("run_id") != identifier
            or ledger.get("registry", {}).get("sha256") != registry_sha
            or ledger.get("selected_projects") != sorted(selected)
        ):
            raise FleetMigrationError(
                "existing run identity, registry, or selected wave does not match"
            )
        if ledger.get("complete"):
            ledger["ledger_path"] = str(ledger_path)
            return ledger
    else:
        ledger = {
            "format": FLEET_FORMAT,
            "migration_version": MIGRATION_VERSION,
            "run_id": identifier,
            "registry": {
                "path": str(registry_path),
                "sha256": registry_sha,
                "projects": len(mounts),
            },
            "selected_projects": sorted(selected),
            "repositories": [],
            "complete": False,
        }
        _write_ledger(ledger_path, ledger)
    existing_rows = {
        row.get("project"): row
        for row in ledger.get("repositories", [])
        if isinstance(row, dict) and row.get("project")
    }
    promotion_stopped = False
    for project, docs_dir in mounts.items():
        row = existing_rows.get(project)
        if row is not None and row.get("state") in TERMINAL_STATES:
            promotion_stopped = promotion_stopped or row.get("state") == "rolled-back"
            continue
        if row is None:
            row = {
                "project": project,
                "resource": f"project:{project}",
                "docs_dir": str(docs_dir),
                "state": "discovered",
                "migration_version": MIGRATION_VERSION,
            }
            ledger["repositories"].append(row)
            existing_rows[project] = row
            _write_ledger(ledger_path, ledger)
        elif row.get("state") == "preflight-passed" and row.get("snapshot"):
            matches, differences = _snapshot_matches(
                Path(row["snapshot"]["path"]), docs_dir
            )
            if not matches:
                row["state"] = "deferred"
                row["error"] = "interrupted transaction changed the mounted docs tree"
                row["interrupted_paths"] = differences
                row["required_action"] = (
                    "inspect the exact paths against the content-bearing snapshot "
                    "before a reviewed retry or rollback"
                )
                _write_ledger(ledger_path, ledger)
                continue
        try:
            repository = _repository_state(docs_dir)
            row["source_commit"] = repository["head"]
            row["repository"] = repository
            snapshot_path = root / "snapshots" / f"{project}.zip"
            row["snapshot"] = create_snapshot(
                docs_dir,
                snapshot_path,
                project=project,
                repository=repository,
                registry_sha256=registry_sha,
            )
            row["before"] = repository_inventory(docs_dir, project)
            preflight = preflight_repository(docs_dir, project)
            row["preflight"] = preflight
            if not preflight["ok"]:
                row["state"] = "deferred"
                row["required_action"] = (
                    "clear the exact preflight blockers, then select this project "
                    "in a new reviewed migration wave"
                )
            elif project not in selected:
                row["state"] = "deferred"
                row["required_action"] = (
                    "authorize this repository write scope and select it in a "
                    "reviewed migration wave"
                )
            elif promotion_stopped:
                row["state"] = "deferred"
                row["required_action"] = (
                    "retry after the earlier selected repository failure is resolved"
                )
            else:
                row["state"] = "preflight-passed"
                _write_ledger(ledger_path, ledger)
                try:
                    result = migrate_repository(
                        docs_dir,
                        project,
                        Path(row["snapshot"]["path"]),
                        staging_parent=root / "staging",
                    )
                except Exception as exc:  # noqa: BLE001 - terminal ledger evidence
                    promotion_stopped = True
                    row["state"] = "rolled-back"
                    row["error"] = str(exc)
                    row["required_action"] = (
                        "inspect the recorded error and snapshot before retrying"
                    )
                else:
                    row["state"] = "verified"
                    row["result"] = result
                    row["after"] = repository_inventory(docs_dir, project)
                    row["output_commit"] = None
                    row["push_ref"] = None
        except Exception as exc:  # noqa: BLE001 - no registered mount is omitted
            row["state"] = "deferred"
            row["error"] = str(exc)
            row["required_action"] = (
                "repair repository discovery or snapshot creation before retrying"
            )
        _write_ledger(ledger_path, ledger)
    ledger["ledger_path"] = str(ledger_path)
    _write_ledger(ledger_path, ledger)
    return ledger


def enrich_ledger_inventories(ledger_path: Path) -> dict[str, Any]:
    """Backfill before/after counts without rerunning a migration."""
    ledger_path = ledger_path.expanduser().resolve()
    try:
        ledger = json.loads(ledger_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetMigrationError(
            f"ledger is unreadable: {ledger_path}: {exc}"
        ) from exc
    for row in ledger.get("repositories", []):
        snapshot = row.get("snapshot", {}).get("path")
        if snapshot:
            row["before"] = snapshot_inventory(Path(snapshot))
        if row.get("state") == "verified":
            row["after"] = repository_inventory(
                Path(str(row["docs_dir"])),
                str(row["project"]),
            )
    _write_ledger(ledger_path, ledger)
    return ledger


def record_repository_commit(
    ledger_path: Path,
    project: str,
    commit: str,
    push_ref: str,
) -> dict[str, Any]:
    """Attach repository commit and push evidence to one verified ledger row."""
    ledger_path = ledger_path.expanduser().resolve()
    try:
        ledger = json.loads(ledger_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetMigrationError(
            f"ledger is unreadable: {ledger_path}: {exc}"
        ) from exc
    rows = [
        row for row in ledger.get("repositories", []) if row.get("project") == project
    ]
    if len(rows) != 1:
        raise FleetMigrationError(f"ledger has no unique project row for {project!r}")
    row = rows[0]
    if row.get("state") != "verified":
        raise FleetMigrationError(
            f"cannot attach commit to non-verified row {project!r}"
        )
    row["output_commit"] = commit
    row["push_ref"] = push_ref
    _write_ledger(ledger_path, ledger)
    return row
