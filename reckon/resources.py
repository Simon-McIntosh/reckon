"""Typed HTML resource identity, discovery, routing, and explicit migration.

One resolver owns the mapping between semantic artifact type and filesystem
location. Callers may read mixed repositories containing flat compatibility
files and canonical typed roots. Migration is always an explicit command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from reckon import _plan_html
from reckon._schema import RESOURCE_TYPE_ENUM, ResourceIdentity

TYPE_ROOTS = {
    "plan": "plans",
    "research": "research",
    "evidence": "evidence",
    "sprint": "sprints",
}
ROOT_TYPES = {root: artifact_type for artifact_type, root in TYPE_ROOTS.items()}
INFRA_DIRS = frozenset(
    {
        "_shared",
        "_ui",
        "ui",
        "state",
        "assets",
        "images",
        "figures",
        ".reckon",
        "milestones",
    }
)
NON_RESOURCE_FILES = frozenset(
    {
        "index.html",
        "sprint.html",
        "sprints.html",
        "milestones.html",
        "decisions.html",
        "inventory.html",
        "blockers.html",
        "implementation.html",
        "questions.html",
        "home.html",
        "project.html",
        "plan.html",
        "README.html",
    }
)
MANIFEST_PATH = PurePosixPath(".reckon/typed-resource-manifest.json")
_LINK_ATTR_RE = re.compile(
    r"""(?P<prefix>\b(?:href|src)\s*=\s*)(?P<quote>["'])(?P<url>.*?)(?P=quote)""",
    re.IGNORECASE,
)


class ResourceCollision(ValueError):
    """Raised when a typed resource key or migration destination is ambiguous."""


@dataclass(frozen=True)
class Resource:
    """Resolved HTML resource with stable identity and compatibility metadata."""

    identity: ResourceIdentity
    path: Path
    relative_path: PurePosixPath
    canonical_relative_path: PurePosixPath
    canonical_href: str
    legacy: bool

    @property
    def slug(self) -> str:
        return self.identity.slug

    @property
    def type(self) -> str:
        return self.identity.type

    @property
    def archived(self) -> bool:
        return self.identity.archived


@dataclass(frozen=True)
class MigrationMove:
    source: PurePosixPath
    destination: PurePosixPath
    identity: ResourceIdentity
    sha256: str


def canonical_type(value: str | None) -> str:
    """Normalise a semantic artifact type."""
    artifact_type = (value or "plan").strip().lower()
    if artifact_type == "doc":
        return "research"
    return artifact_type


def canonical_relative_path(
    artifact_type: str, slug: str, *, archived: bool = False
) -> PurePosixPath:
    """Return the canonical path below a docs root."""
    root = TYPE_ROOTS[artifact_type]
    parts = [root]
    if archived:
        parts.append("archive")
    parts.append(f"{slug}.html")
    return PurePosixPath(*parts)


def canonical_href(project: str, artifact_type: str, slug: str) -> str:
    """Return the extensionless canonical live route."""
    return f"/{project}/{TYPE_ROOTS[artifact_type]}/{slug}"


def _path_context(relative_path: PurePosixPath) -> tuple[str | None, bool, bool]:
    parts = relative_path.parts
    if not parts:
        return None, False, True
    typed = ROOT_TYPES.get(parts[0])
    archived = "archive" in parts[:-1]
    legacy = typed is None or (typed != "sprint" and parts[0] == "archive")
    return typed, archived, legacy


def _sprint_slug(path: Path) -> str:
    title, meta = _read_head(path)
    del title
    return meta.get("sprint-id") or path.stem


def _read_head(path: Path) -> tuple[str, dict[str, str]]:
    """Read title and meta through the server's bounded head parser."""
    from reckon.serve import _read_head_meta

    return _read_head_meta(path)


def identify_resource(docs_dir: Path, path: Path, project: str) -> Resource | None:
    """Classify one HTML file using typed location plus semantic metadata."""
    try:
        relative = PurePosixPath(path.relative_to(docs_dir).as_posix())
    except ValueError:
        return None
    if path.name in NON_RESOURCE_FILES:
        return None
    if any(part in INFRA_DIRS for part in relative.parts[:-1]):
        return None

    location_type, archived, legacy = _path_context(relative)
    if location_type == "sprint":
        artifact_type = "sprint"
        slug = _sprint_slug(path)
    else:
        meta = _plan_html.parse_meta(path)
        artifact_type = canonical_type(meta.get("type"))
        slug = meta.get("slug") or path.stem
        if location_type and artifact_type != location_type:
            raise ResourceCollision(
                f"{relative}: location type {location_type!r} conflicts with "
                f"reckon-type {artifact_type!r}"
            )
    if artifact_type not in RESOURCE_TYPE_ENUM:
        return None
    identity = ResourceIdentity(
        project=project,
        type=artifact_type,
        slug=slug,
        archived=archived,
    ).validate_for_write()
    canonical = canonical_relative_path(artifact_type, slug, archived=archived)
    return Resource(
        identity=identity,
        path=path,
        relative_path=relative,
        canonical_relative_path=canonical,
        canonical_href=canonical_href(project, artifact_type, slug),
        legacy=legacy,
    )


def iter_resources(
    docs_dir: Path,
    project: str,
    *,
    include_archived: bool = True,
    include_legacy: bool = True,
) -> list[Resource]:
    """Discover resources in typed roots and bounded flat compatibility paths."""
    resources: list[Resource] = []
    for path in sorted(docs_dir.rglob("*.html")):
        resource = identify_resource(docs_dir, path, project)
        if resource is None:
            continue
        if resource.archived and not include_archived:
            continue
        if resource.legacy and not include_legacy:
            continue
        resources.append(resource)
    return resources


def resource_map(
    docs_dir: Path,
    project: str,
    *,
    include_archived: bool = True,
    include_legacy: bool = True,
) -> dict[tuple[str, str, bool], Resource]:
    """Return resources keyed by type, slug, and archive state."""
    indexed: dict[tuple[str, str, bool], Resource] = {}
    for resource in iter_resources(
        docs_dir,
        project,
        include_archived=include_archived,
        include_legacy=include_legacy,
    ):
        key = (resource.type, resource.slug, resource.archived)
        existing = indexed.get(key)
        if existing is not None:
            preferred = _preferred_resource(existing, resource)
            if preferred is None:
                raise ResourceCollision(
                    f"duplicate resource {resource.identity.key}: "
                    f"{existing.relative_path}, {resource.relative_path}"
                )
            indexed[key] = preferred
        else:
            indexed[key] = resource
    return indexed


def _preferred_resource(left: Resource, right: Resource) -> Resource | None:
    if left.legacy != right.legacy:
        return right if left.legacy else left
    return None


def resolve_resource(
    docs_dir: Path,
    project: str,
    slug: str,
    artifact_type: str | None = None,
    *,
    include_archived: bool = False,
) -> Resource | None:
    """Resolve by typed identity; untyped compatibility reads require uniqueness."""
    requested_type = canonical_type(artifact_type) if artifact_type else None
    matches = [
        resource
        for resource in resource_map(
            docs_dir,
            project,
            include_archived=include_archived,
        ).values()
        if resource.slug == slug
        and (requested_type is None or resource.type == requested_type)
        and (include_archived or not resource.archived)
    ]
    if not matches:
        return None
    if requested_type is None:
        plan_matches = [resource for resource in matches if resource.type == "plan"]
        if len(plan_matches) == 1:
            return plan_matches[0]
    if len(matches) != 1:
        kinds = ", ".join(sorted(resource.type for resource in matches))
        raise ResourceCollision(
            f"resource slug {slug!r} is ambiguous across types: {kinds}; "
            "supply artifact_type"
        )
    return matches[0]


def resolve_route(
    docs_dir: Path, project: str, route: str
) -> tuple[Resource | None, bool]:
    """Resolve a project-relative route and report whether it is a legacy alias."""
    clean = route.strip("/").removesuffix(".html")
    parts = clean.split("/", 1)
    if len(parts) == 2 and parts[0] in ROOT_TYPES:
        resource = resolve_resource(docs_dir, project, parts[1], ROOT_TYPES[parts[0]])
        return resource, False
    if "/" not in clean:
        return resolve_resource(docs_dir, project, clean), True
    return None, False


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _migration_candidates(docs_dir: Path, project: str) -> list[Resource]:
    return [
        resource
        for resource in iter_resources(docs_dir, project, include_archived=True)
        if resource.legacy
    ]


def _rewrite_url(
    raw_url: str,
    *,
    project: str,
    original_document: PurePosixPath,
    migrated_document: PurePosixPath,
    moves: dict[PurePosixPath, PurePosixPath],
    known_paths: set[PurePosixPath],
) -> str:
    parsed = urlsplit(raw_url)
    if parsed.scheme or parsed.netloc or raw_url.startswith(("#", "mailto:", "data:")):
        return raw_url

    target: PurePosixPath | None = None
    extensionless = not PurePosixPath(parsed.path).suffix
    prefix = f"/{project}/"
    if parsed.path.startswith(prefix):
        candidate = parsed.path[len(prefix) :].lstrip("/")
        target = PurePosixPath(candidate)
        if extensionless:
            target = target.with_suffix(".html")
    elif parsed.path and not parsed.path.startswith("/"):
        candidate = original_document.parent / parsed.path
        target = PurePosixPath(os.path.normpath(str(candidate)))

    if target in moves:
        destination = moves[target]
    elif target in known_paths and migrated_document != original_document:
        destination = target
    else:
        return raw_url
    if parsed.path.startswith(prefix):
        new_path = f"/{project}/{destination.with_suffix('')}"
    else:
        new_path = os.path.relpath(destination, migrated_document.parent)
        if extensionless:
            new_path = str(PurePosixPath(new_path).with_suffix(""))
    return urlunsplit(("", "", new_path, parsed.query, parsed.fragment))


def _rewrite_links(
    content: str,
    *,
    project: str,
    original_document: PurePosixPath,
    migrated_document: PurePosixPath,
    moves: dict[PurePosixPath, PurePosixPath],
    known_paths: set[PurePosixPath],
) -> str:
    def replace(match: re.Match[str]) -> str:
        rewritten = _rewrite_url(
            match.group("url"),
            project=project,
            original_document=original_document,
            migrated_document=migrated_document,
            moves=moves,
            known_paths=known_paths,
        )
        return (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{rewritten}{match.group('quote')}"
        )

    return _LINK_ATTR_RE.sub(replace, content)


def build_migration_manifest(docs_dir: Path, project: str) -> dict:
    """Preflight an explicit typed-root migration without modifying files."""
    candidates = _migration_candidates(docs_dir, project)
    moves: list[MigrationMove] = []
    destinations: dict[PurePosixPath, PurePosixPath] = {}
    for resource in candidates:
        destination = resource.canonical_relative_path
        existing_source = destinations.get(destination)
        if existing_source is not None:
            raise ResourceCollision(
                f"migration destination collision at {destination}: "
                f"{existing_source}, {resource.relative_path}"
            )
        target = docs_dir / destination
        if target.exists() and target.resolve() != resource.path.resolve():
            raise ResourceCollision(
                f"migration destination already exists: {destination}"
            )
        destinations[destination] = resource.relative_path
        content = resource.path.read_bytes()
        moves.append(
            MigrationMove(
                source=resource.relative_path,
                destination=destination,
                identity=resource.identity,
                sha256=_sha256(content),
            )
        )

    return {
        "format": 1,
        "project": project,
        "moves": [
            {
                "resource": move.identity.key,
                "type": move.identity.type,
                "slug": move.identity.slug,
                "archived": move.identity.archived,
                "from": str(move.source),
                "to": str(move.destination),
                "sha256": move.sha256,
            }
            for move in sorted(moves, key=lambda item: str(item.destination))
        ],
    }


def migrate_typed_layout(docs_dir: Path, project: str) -> dict:
    """Execute the preflighted migration transaction and emit its manifest."""
    docs_dir = docs_dir.resolve()
    manifest_path = docs_dir / MANIFEST_PATH
    manifest = build_migration_manifest(docs_dir, project)
    if not manifest["moves"]:
        if manifest_path.is_file():
            return json.loads(manifest_path.read_text())
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return manifest

    moves = {
        PurePosixPath(item["from"]): PurePosixPath(item["to"])
        for item in manifest["moves"]
    }
    transformed: dict[PurePosixPath, bytes] = {}
    originals: dict[PurePosixPath, bytes] = {}
    known_paths = {
        PurePosixPath(path.relative_to(docs_dir).as_posix())
        for path in docs_dir.rglob("*")
        if path.is_file()
    }
    for path in sorted(docs_dir.rglob("*.html")):
        relative = PurePosixPath(path.relative_to(docs_dir).as_posix())
        destination = moves.get(relative, relative)
        content = path.read_bytes()
        originals[relative] = content
        rewritten = _rewrite_links(
            content.decode("utf-8"),
            project=project,
            original_document=relative,
            migrated_document=destination,
            moves=moves,
            known_paths=known_paths,
        ).encode("utf-8")
        if relative in moves or rewritten != content:
            transformed[destination] = rewritten

    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    transformed[MANIFEST_PATH] = manifest_bytes
    prior_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
    staging = Path(tempfile.mkdtemp(prefix=".reckon-layout-", dir=docs_dir))
    installed: list[PurePosixPath] = []
    try:
        for relative, content in transformed.items():
            staged = staging / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
        for relative in sorted(transformed, key=str):
            destination = docs_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / relative, destination)
            installed.append(relative)
        for source in sorted(moves, key=str):
            if source not in transformed and (docs_dir / source).exists():
                (docs_dir / source).unlink()
            elif source != moves[source] and (docs_dir / source).exists():
                (docs_dir / source).unlink()
    except Exception:
        for relative in installed:
            path = docs_dir / relative
            if relative not in originals and path.exists():
                path.unlink()
        for relative, content in originals.items():
            path = docs_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        if prior_manifest is not None:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(prior_manifest)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return manifest


def migration_paths(manifest: dict) -> Iterable[tuple[str, str]]:
    """Yield source/destination pairs for CLI reporting."""
    for item in manifest.get("moves", []):
        yield str(item["from"]), str(item["to"])
