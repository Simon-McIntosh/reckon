"""Preview and apply age-based document archival."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reckon import _plan_html
from reckon.doccheck import derived_plan_age
from reckon.resources import ResourceCollision, iter_resources


ARCHIVABLE_STATUSES = frozenset({"done", "superseded"})
ARCHIVABLE_TYPES = frozenset({"plan", "research", "evidence"})


class ArchiveError(RuntimeError):
    """Raised when an archive pass cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    """Caller-supplied policy for one archive pass."""

    older_than_days: int

    def __post_init__(self) -> None:
        if isinstance(self.older_than_days, bool) or self.older_than_days < 0:
            raise ValueError("older_than_days must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ArchiveCandidate:
    """One document eligible for archival at scan time."""

    slug: str
    status: str
    age_days: int
    age_source: str
    path: Path
    relative_path: str
    source_digest: str


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    """The candidates and mutations produced by one archive pass."""

    candidates: tuple[ArchiveCandidate, ...]
    archived: tuple[Path, ...]
    applied: bool


CandidateReporter = Callable[[Sequence[ArchiveCandidate]], None]


def _archived_flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def find_archive_candidates(
    docs_dir: Path,
    project: str,
    config: ArchiveConfig,
    *,
    today: date | None = None,
) -> tuple[ArchiveCandidate, ...]:
    """Return every eligible live document in deterministic path order."""

    docs_dir = docs_dir.resolve()
    candidates: list[ArchiveCandidate] = []
    try:
        resources = iter_resources(docs_dir, project, include_archived=True)
    except ResourceCollision as exc:
        raise ArchiveError(str(exc)) from exc

    for resource in resources:
        if resource.archived or resource.type not in ARCHIVABLE_TYPES:
            continue
        state = _plan_html.parse_meta(resource.path, slug=resource.slug)
        status = str(state.get("status") or "").strip().lower()
        if status not in ARCHIVABLE_STATUSES or _archived_flag(state.get("archived")):
            continue
        age_days, age_source = derived_plan_age(
            state.get("modified"),
            fallback_path=resource.path,
            today=today,
        )
        if age_days is None or age_days <= config.older_than_days:
            continue
        try:
            source = resource.path.read_bytes()
        except OSError as exc:
            raise ArchiveError(f"cannot read {resource.path}: {exc}") from exc
        candidates.append(
            ArchiveCandidate(
                slug=resource.slug,
                status=status,
                age_days=age_days,
                age_source=age_source,
                path=resource.path,
                relative_path=resource.relative_path.as_posix(),
                source_digest=hashlib.sha256(source).hexdigest(),
            )
        )
    return tuple(candidates)


def apply_archive_candidates(
    candidates: Sequence[ArchiveCandidate],
) -> tuple[Path, ...]:
    """Set the archived scalar after verifying every candidate is unchanged."""

    rendered: list[tuple[Path, str]] = []
    for candidate in candidates:
        try:
            source = candidate.path.read_bytes()
        except OSError as exc:
            raise ArchiveError(f"cannot read {candidate.path}: {exc}") from exc
        digest = hashlib.sha256(source).hexdigest()
        if digest != candidate.source_digest:
            raise ArchiveError(
                f"archive candidate changed after reporting: {candidate.path}"
            )
        text = source.decode("utf-8")
        rendered.append(
            (candidate.path, _plan_html.write_state(text, {"archived": "1"}))
        )

    archived: list[Path] = []
    for path, text in rendered:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ArchiveError(f"cannot archive {path}: {exc}") from exc
        archived.append(path)
    return tuple(archived)


def run_archive_pass(
    docs_dir: Path,
    project: str,
    config: ArchiveConfig,
    *,
    apply: bool = False,
    reporter: CandidateReporter | None = None,
    today: date | None = None,
) -> ArchiveReport:
    """Report all candidates, then optionally apply their archive flags."""

    candidates = find_archive_candidates(docs_dir, project, config, today=today)
    if reporter is not None:
        reporter(candidates)
    archived = apply_archive_candidates(candidates) if apply else ()
    return ArchiveReport(candidates=candidates, archived=archived, applied=apply)
