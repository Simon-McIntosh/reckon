"""Canonical identities for resource tags."""

from __future__ import annotations

import re
from pathlib import Path
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
