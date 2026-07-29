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
    "milestone": "milestones",
    "blocker": "blockers",
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
    ResourceIdentity(
        project="path-validation",
        type=artifact_type,
        slug=slug,
        archived=archived,
    ).validate_for_write()
    root = TYPE_ROOTS[artifact_type]
    parts = [root]
    if archived:
        parts.append("archive")
    parts.append(f"{slug}.html")
    return PurePosixPath(*parts)


def canonical_href(
    project: str, artifact_type: str, slug: str, *, archived: bool = False
) -> str:
    """Return the extensionless canonical live route."""
    identity = ResourceIdentity(
        project=project,
        type=artifact_type,
        slug=slug,
        archived=archived,
    ).validate_for_write()
    archive_part = "/archive" if identity.archived else ""
    return (
        f"/{identity.project}/{TYPE_ROOTS[identity.type]}{archive_part}/{identity.slug}"
    )


def _path_context(relative_path: PurePosixPath) -> tuple[str | None, bool, bool]:
    parts = relative_path.parts
    if not parts:
        return None, False, True
    typed = ROOT_TYPES.get(parts[0])
    if typed is not None and not (
        len(parts) == 2 or (len(parts) == 3 and parts[1] == "archive")
    ):
        raise ResourceCollision(
            f"{relative_path}: typed resource path must be "
            "<root>/<slug>.html or <root>/archive/<slug>.html"
        )
    archived = (typed is not None and len(parts) == 3 and parts[1] == "archive") or (
        typed is None and parts[0] == "archive"
    )
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
    if location_type in {"sprint", "milestone", "blocker"}:
        artifact_type = location_type
        if location_type == "sprint":
            slug = _sprint_slug(path)
        else:
            _, meta = _read_head(path)
            slug = (
                meta.get(f"{location_type}-id")
                or meta.get("reckon-id")
                or path.stem
            )
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
        canonical_href=canonical_href(project, artifact_type, slug, archived=archived),
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
    stripped = route.strip("/")
    html_alias = stripped.endswith(".html")
    clean = stripped.removesuffix(".html")
    parts = clean.split("/")
    if parts[0] in ROOT_TYPES:
        if len(parts) not in {2, 3}:
            raise ResourceCollision(f"invalid typed resource route: {route!r}")
        archived = len(parts) == 3 and parts[1] == "archive"
        if len(parts) == 3 and not archived:
            raise ResourceCollision(f"invalid typed resource route: {route!r}")
        slug = parts[-1]
        try:
            ResourceIdentity(
                project=project,
                type=ROOT_TYPES[parts[0]],
                slug=slug,
                archived=archived,
            ).validate_for_write()
        except ValueError as exc:
            raise ResourceCollision(f"invalid typed resource route: {route!r}") from exc
        resource = resolve_resource(
            docs_dir,
            project,
            slug,
            ROOT_TYPES[parts[0]],
            include_archived=archived,
        )
        if resource is not None and resource.archived != archived:
            return None, False
        return resource, html_alias
    if "/" not in clean:
        return resolve_resource(docs_dir, project, clean), True
    return None, False


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _contained_path(docs_dir: Path, relative: PurePosixPath, *, label: str) -> Path:
    """Resolve one manifest path and prove it remains below the docs root."""
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ResourceCollision(f"{label} is not a contained relative path: {relative}")
    root = docs_dir.resolve()
    candidate = root.joinpath(*relative.parts).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ResourceCollision(
            f"{label} escapes the docs directory: {relative}"
        ) from exc
    return candidate


def _load_prior_manifest(docs_dir: Path, project: str) -> dict:
    """Load and validate cumulative migration provenance."""
    manifest_path = _contained_path(
        docs_dir, MANIFEST_PATH, label="migration manifest path"
    )
    if not manifest_path.is_file():
        return {"format": 1, "project": project, "moves": [], "rewrites": []}
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceCollision(f"migration manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ResourceCollision("migration manifest must be an object")
    if manifest.get("format") != 1:
        raise ResourceCollision("migration manifest format must be 1")
    if manifest.get("project") != project:
        raise ResourceCollision(
            "migration manifest project does not match the requested project"
        )
    moves = manifest.get("moves")
    rewrites = manifest.get("rewrites", [])
    if not isinstance(moves, list) or not isinstance(rewrites, list):
        raise ResourceCollision("migration manifest moves and rewrites must be lists")

    destinations: dict[str, str] = {}
    sources: dict[str, str] = {}
    normalised_moves: list[dict] = []
    for item in moves:
        if not isinstance(item, dict):
            raise ResourceCollision("migration manifest move must be an object")
        required = {
            "resource",
            "type",
            "slug",
            "archived",
            "from",
            "to",
            "sha256",
        }
        if not required.issubset(item):
            raise ResourceCollision("migration manifest move is missing fields")
        source = PurePosixPath(str(item["from"]))
        destination = PurePosixPath(str(item["to"]))
        _contained_path(docs_dir, source, label="migration source")
        _contained_path(docs_dir, destination, label="migration destination")
        identity = ResourceIdentity(
            project=project,
            type=str(item["type"]),
            slug=str(item["slug"]),
            archived=bool(item["archived"]),
        ).validate_for_write()
        if item["resource"] != identity.key:
            raise ResourceCollision(
                "migration manifest resource identity is inconsistent"
            )
        expected = canonical_relative_path(
            identity.type, identity.slug, archived=identity.archived
        )
        if destination != expected:
            raise ResourceCollision(
                "migration manifest destination contradicts resource identity"
            )
        if str(source) in sources and sources[str(source)] != str(destination):
            raise ResourceCollision("migration manifest source is contradictory")
        if str(destination) in destinations and destinations[str(destination)] != str(
            source
        ):
            raise ResourceCollision("migration manifest destination is contradictory")
        sources[str(source)] = str(destination)
        destinations[str(destination)] = str(source)
        normalised_moves.append(dict(item))

    normalised_rewrites: list[dict] = []
    for item in rewrites:
        if not isinstance(item, dict) or not {
            "from",
            "to",
            "from_sha256",
            "to_sha256",
        }.issubset(item):
            raise ResourceCollision("migration manifest rewrite is malformed")
        _contained_path(
            docs_dir, PurePosixPath(str(item["from"])), label="rewrite source"
        )
        _contained_path(
            docs_dir, PurePosixPath(str(item["to"])), label="rewrite destination"
        )
        normalised_rewrites.append(dict(item))
    return {
        "format": 1,
        "project": project,
        "moves": sorted(normalised_moves, key=lambda item: str(item["to"])),
        "rewrites": sorted(
            normalised_rewrites,
            key=lambda item: (
                str(item["to"]),
                str(item["from"]),
                str(item["from_sha256"]),
                str(item["to_sha256"]),
            ),
        ),
    }


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
    is_html_resource = destination.suffix == ".html"
    if parsed.path.startswith(prefix):
        rendered_destination = (
            destination.with_suffix("") if is_html_resource else destination
        )
        new_path = f"/{project}/{rendered_destination}"
    else:
        new_path = os.path.relpath(destination, migrated_document.parent)
        if extensionless and is_html_resource:
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
    docs_dir = docs_dir.resolve()
    ResourceIdentity(
        project=project, type="plan", slug="identity-validation"
    ).validate_for_write()
    prior = _load_prior_manifest(docs_dir, project)
    prior_by_source = {item["from"]: item for item in prior["moves"]}
    prior_by_destination = {item["to"]: item for item in prior["moves"]}
    candidates = _migration_candidates(docs_dir, project)
    moves: list[MigrationMove] = []
    destinations: dict[PurePosixPath, PurePosixPath] = {}
    for resource in candidates:
        destination = resource.canonical_relative_path
        _contained_path(docs_dir, resource.relative_path, label="migration source")
        _contained_path(docs_dir, destination, label="migration destination")
        if resource.relative_path.as_posix() in prior_by_source:
            raise ResourceCollision(
                f"{resource.relative_path}: source contradicts prior manifest"
            )
        prior_destination = prior_by_destination.get(destination.as_posix())
        if prior_destination is not None:
            raise ResourceCollision(
                f"{destination}: destination contradicts prior manifest"
            )
        existing_source = destinations.get(destination)
        if existing_source is not None:
            raise ResourceCollision(
                f"migration destination collision at {destination}: "
                f"{existing_source}, {resource.relative_path}"
            )
        target = _contained_path(docs_dir, destination, label="migration destination")
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

    new_moves = [
        {
            "resource": move.identity.key,
            "type": move.identity.type,
            "slug": move.identity.slug,
            "archived": move.identity.archived,
            "from": str(move.source),
            "to": str(move.destination),
            "sha256": move.sha256,
        }
        for move in moves
    ]
    return {
        "format": 1,
        "project": project,
        "moves": sorted(
            [*prior["moves"], *new_moves], key=lambda item: str(item["to"])
        ),
        "rewrites": prior["rewrites"],
    }


def migrate_typed_layout(docs_dir: Path, project: str) -> dict:
    """Execute migration and emit cumulative provenance.

    Process-level failures roll back installed files. The sequence is not
    crash-atomic because portable filesystems do not provide a directory-wide
    transaction; the cumulative manifest makes an interrupted run auditable and
    safely repeatable after filesystem inspection.
    """
    docs_dir = docs_dir.resolve()
    manifest_path = _contained_path(
        docs_dir, MANIFEST_PATH, label="migration manifest path"
    )
    prior = _load_prior_manifest(docs_dir, project)
    manifest = build_migration_manifest(docs_dir, project)
    prior_sources = {item["from"] for item in prior["moves"]}
    active_items = [
        item for item in manifest["moves"] if item["from"] not in prior_sources
    ]
    if not active_items:
        if manifest_path.is_file():
            return prior
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return manifest

    moves = {
        PurePosixPath(item["from"]): PurePosixPath(item["to"]) for item in active_items
    }
    rewrite_moves = {
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
        _contained_path(docs_dir, relative, label="migration document")
        destination = moves.get(relative, relative)
        _contained_path(docs_dir, destination, label="migration document destination")
        content = path.read_bytes()
        originals[relative] = content
        rewritten = _rewrite_links(
            content.decode("utf-8"),
            project=project,
            original_document=relative,
            migrated_document=destination,
            moves=rewrite_moves,
            known_paths=known_paths,
        ).encode("utf-8")
        if relative in moves or rewritten != content:
            transformed[destination] = rewritten

    rewrite_records = list(prior["rewrites"])
    for destination, content in sorted(
        transformed.items(), key=lambda item: str(item[0])
    ):
        source = next(
            (candidate for candidate, target in moves.items() if target == destination),
            destination,
        )
        original = originals[source]
        if content == original:
            continue
        record = {
            "from": str(source),
            "to": str(destination),
            "from_sha256": _sha256(original),
            "to_sha256": _sha256(content),
        }
        if record not in rewrite_records:
            rewrite_records.append(record)
    manifest["rewrites"] = sorted(
        rewrite_records,
        key=lambda item: (
            str(item["to"]),
            str(item["from"]),
            str(item["from_sha256"]),
            str(item["to_sha256"]),
        ),
    )
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    transformed[MANIFEST_PATH] = manifest_bytes
    prior_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
    staging = Path(tempfile.mkdtemp(prefix=".reckon-layout-", dir=docs_dir))
    installed: list[PurePosixPath] = []
    try:
        for relative, content in transformed.items():
            staged = _contained_path(staging, relative, label="staged migration path")
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
        for relative in sorted(transformed, key=str):
            destination = _contained_path(
                docs_dir, relative, label="migration install destination"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / relative, destination)
            installed.append(relative)
        for source in sorted(moves, key=str):
            source_path = _contained_path(
                docs_dir, source, label="migration removal source"
            )
            if source not in transformed and source_path.exists():
                source_path.unlink()
            elif source != moves[source] and source_path.exists():
                source_path.unlink()
    except Exception:
        for relative in installed:
            path = _contained_path(docs_dir, relative, label="rollback path")
            if relative not in originals and path.exists():
                path.unlink()
        for relative, content in originals.items():
            path = _contained_path(docs_dir, relative, label="rollback source")
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
