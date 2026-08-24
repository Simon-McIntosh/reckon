"""Canonical identities for resource tags."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any


_SEPARATORS = re.compile(r"[\s_-]+")


def normalise_tag(authored: str) -> str:
    """Derive the canonical stored identity for an authored tag.

    Tag identities are case-insensitive and use a single hyphen between words.
    Other punctuation carries no identity.  A colon is rejected separately so
    it remains available as a future facet separator.
    """

    if not isinstance(authored, str):
        raise TypeError(f"tag must be a string, got {authored!r}")
    if ":" in authored:
        raise ValueError(f"tag {authored!r} contains ':', which is reserved")

    words = _SEPARATORS.split(authored.casefold().strip())
    identity = "-".join(
        cleaned
        for word in words
        if (cleaned := "".join(character for character in word if character.isalnum()))
    )
    if not identity:
        raise ValueError(f"tag {authored!r} normalises to an empty identity")
    return identity


_TAG_CARRIER_TYPES = frozenset({"plan", "research", "evidence", "sprint"})


def _contained_resource_path(docs_dir: Path, relative: PurePosixPath) -> Path:
    """Resolve a mapped resource path without permitting a docs-root escape."""

    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"mapped resource path is not contained: {relative}")
    root = docs_dir.resolve()
    candidate = root.joinpath(*relative.parts).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"mapped resource path escapes docs root: {relative}") from exc
    return candidate


def _parse_layout_moves(preimage: str) -> list[tuple[PurePosixPath, PurePosixPath]]:
    """Parse the saved ``source -> destination`` layout preview."""

    moves: list[tuple[PurePosixPath, PurePosixPath]] = []
    for line_number, line in enumerate(preimage.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(" -> ")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"invalid layout move on line {line_number}: {line!r}")
        moves.append((PurePosixPath(parts[0]), PurePosixPath(parts[1])))
    return moves


def _tag_removed_by_move(
    source: PurePosixPath, destination: PurePosixPath
) -> str | None:
    """Derive the topical directory removed when a resource is flattened."""

    if source.suffix.casefold() != ".html" or destination.suffix.casefold() != ".html":
        return None
    source_parent = source.parent
    if source_parent == PurePosixPath("."):
        return None
    topic = source_parent.name
    if topic in destination.parent.parts:
        return None
    return normalise_tag(topic)


def backfill_tags_from_preimage(
    docs_dir: Path,
    preimage_path: Path,
) -> dict[str, Any]:
    """Backfill topical tags from a saved typed-layout move preview.

    Only moves that flatten an HTML resource out of its immediate source
    directory carry grouping information.  All inputs are validated and all
    resulting HTML is prepared before the first file is changed, preventing a
    malformed later row from leaving a partial backfill.
    """

    docs_dir = Path(docs_dir).resolve()
    moves = _parse_layout_moves(Path(preimage_path).read_text(encoding="utf-8"))

    from reckon._plan_html import read_state, write_state

    prepared: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    seen_sources: set[PurePosixPath] = set()
    for source, destination in moves:
        source_path = _contained_resource_path(docs_dir, source)
        _contained_resource_path(docs_dir, destination)
        if source in seen_sources:
            raise ValueError(f"layout pre-image repeats source path: {source}")
        seen_sources.add(source)

        tag = _tag_removed_by_move(source, destination)
        if tag is None:
            continue
        if not source_path.is_file():
            raise FileNotFoundError(f"mapped source resource does not exist: {source}")

        original = source_path.read_text(encoding="utf-8")
        state = read_state(original)
        tags = list(state.get("tags") or [])
        changed = tag not in tags
        if changed:
            tags.append(tag)
            state["tags"] = tags
            state["version"] = int(state.get("version") or 0) + 1
            rendered = write_state(original, state)
            prepared.append(
                {"path": source_path, "original": original, "rendered": rendered}
            )
        resources.append(
            {
                "from": source.as_posix(),
                "to": destination.as_posix(),
                "tag": tag,
                "tags": tags,
                "changed": changed,
            }
        )

    written: list[dict[str, Any]] = []
    try:
        for item in prepared:
            item["path"].write_text(item["rendered"], encoding="utf-8")
            written.append(item)
    except Exception:
        for item in reversed(written):
            item["path"].write_text(item["original"], encoding="utf-8")
        raise

    lost_resources = [
        item["from"] for item in resources if item["tag"] not in item["tags"]
    ]
    return {
        "moves": len(moves),
        "grouped_resources": len(resources),
        "changed": len(prepared),
        "unchanged": len(resources) - len(prepared),
        "grouping_loss": len(lost_resources),
        "lost_resources": lost_resources,
        "resources": resources,
    }


def _renamed_tags(tags: list[str], source: str, target: str) -> list[str]:
    """Replace one canonical identity while preserving unrelated tag order."""

    if source == target or source not in tags:
        return tags
    target_exists = target in tags
    renamed: list[str] = []
    for tag in tags:
        if tag == source:
            if not target_exists:
                renamed.append(target)
                target_exists = True
        else:
            renamed.append(tag)
    return renamed


def rename_project_tag(
    docs_dir: Path,
    project: str,
    source: str,
    target: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rename a tag on every live typed resource that carries it.

    The complete affected set and its versions are read before the first
    write.  Every mutation then uses that captured version, so a concurrent
    resource edit is refused instead of overwritten.  Renaming to an identity
    already present on a resource removes the source without duplicating the
    target.
    """

    docs_dir = Path(docs_dir).resolve()
    checkout_root = docs_dir.parent
    canonical_source = normalise_tag(source)
    canonical_target = normalise_tag(target)

    # These modules depend on the schema, which itself imports normalise_tag.
    # Keep them lazy so the identity primitive remains cycle-free.
    from reckon import _store
    from reckon.resources import iter_resources

    candidates: list[dict[str, Any]] = []
    for resource in iter_resources(
        docs_dir,
        project,
        include_archived=False,
        include_legacy=True,
    ):
        if resource.type not in _TAG_CARRIER_TYPES:
            continue
        state, version = _store.read_plan(
            project,
            resource.slug,
            root=checkout_root,
            artifact_type=resource.type,
        )
        tags = list(state.get("tags") or [])
        renamed = _renamed_tags(tags, canonical_source, canonical_target)
        if renamed == tags:
            continue
        candidates.append(
            {
                "resource": resource.identity.key,
                "type": resource.type,
                "slug": resource.slug,
                "version": version,
                "tags": renamed,
                "state": state,
            }
        )

    report_resources = [
        {key: item[key] for key in ("resource", "type", "slug", "version", "tags")}
        for item in candidates
    ]
    report = {
        "project": project,
        "source": canonical_source,
        "target": canonical_target,
        "dry_run": dry_run,
        "changed": 0 if dry_run else len(candidates),
        "resources": report_resources,
    }
    if dry_run:
        return report

    # Refuse an already-stale pass before making its first mutation.  The
    # expected version on each write also closes races that begin afterwards.
    for item in candidates:
        current_state, current_version = _store.read_plan(
            project,
            item["slug"],
            root=checkout_root,
            artifact_type=item["type"],
        )
        if current_version != item["version"]:
            from reckon._store import VersionConflict

            raise VersionConflict(item["version"], current_version, current_state)

    for item in candidates:
        _store.write_plan(
            project,
            item["slug"],
            {**item["state"], "tags": item["tags"]},
            item["version"],
            root=checkout_root,
            artifact_type=item["type"],
        )
    return report
