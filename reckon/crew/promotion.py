from __future__ import annotations

import html
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from reckon import (
    _backends,
    _store,
    capabilities,
    clones,
    flight,
    ledger,
    review_tiers,
)
from reckon._plan_html import section_anchor, section_record_id
from reckon._schema import is_implementable_section
from reckon._timestamps import parse_iso, parse_utc
from reckon.crew import review as review_module
from reckon.crew import rollout
from reckon.crew.dispatch import (
    WORKER_SCRATCH_BUDGET_BYTES,
    _backend_settings,
    _capture_member_session,
    project_mount_repository,
    remove_worker_scratch,
    resolve_project_repository,
    tree_size_bytes,
    worker_scratch_root,
)
from reckon.crew.node import (
    NEGATIVE_CONTROL_FIELD,
    NEGATIVE_CONTROL_NONE,
    STALL_BUDGET_MULTIPLE,
    CrewError,
    is_test_path,
    negative_control_is_none,
    negative_control_reason,
    parse_duration,
    role_may_write_repository_paths,
)
from reckon.crew.plan_review import RUN_COMMENT_PREFIX, _is_run_comment
from reckon.crew.recovery import _resolve_commit
from reckon.crew.reports import (
    _NONE_VALUES as _MANIFEST_NOTHING,
)
from reckon.crew.reports import (
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    parse_manifest,
    path_within_declared_scope,
)
from reckon.crew.routing import (
    RECLAIMABLE_CLASSES,
    WITHHELD_REASONS,
    _boundary_tree_roots,
    _disposable_member_id,
    _git,
    _inspect_workspace,
    _live_pointer_worktrees,
    _repository_tree_snapshot,
    _shadow_patch_retained,
    _shadow_worktree_records,
    _signal_process_group,
    mounted_repository_projects,
    run_directory_of,
)
from reckon.crew.runs import (
    _drain_row,
    _live_worktree_claims,
    _manifest_freshness,
    _pointer_lock,
    _shared_write_paths,
    _utc_now,
    _write_json,
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    list_live,
    pointer_path,
    process_alive,
    read_pointer,
    record_process_alive,
    run_dir,
)
from reckon.evidence import EXECUTABLE_SECTION_ROLES

# ── Promotion: the transient record becomes committed evidence ──────────────

LOGGER = logging.getLogger(__name__)

_COORDINATOR_LANDING_AUTHOR = "reckon-build"

# The run-directory records a launch leaves beside its pointer. Named here
# rather than imported from the dispatch module so the reconstruction below
# reads the same filenames the supervisor writes without a circular import.
_WORKER_RECORD_NAME = "worker.json"
_ATTEMPT_RECORD_NAME = "attempt.json"


def scoped_diff_stat(
    *,
    cwd: str | Path,
    base: str,
    head: str = "HEAD",
    paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Count the lines a run changed inside its own write scope.

    Measured against the node's exclusive paths rather than the whole diff, so
    the number describes the node rather than whatever else the branch carried.
    An unmeasurable diff is an explicit absence. Command diagnostics are not
    measurements and must never enter the durable numeric field.
    """
    if not base:
        return {"available": False, "reason": "missing_base"}
    for revision in (base, head):
        if not _resolve_commit(Path(cwd), revision):
            return {"available": False, "reason": "unresolvable_revision"}
    argv = ["git", "diff", "--numstat", f"{base}..{head}"]
    if paths:
        argv += ["--", *[str(path) for path in paths]]
    result = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode:
        return {"available": False, "reason": "diff_unavailable"}
    added = removed = files = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # A binary file reports "-" for both counts; it changed, but no lines did.
        added += int(parts[0]) if parts[0].isdigit() else 0
        removed += int(parts[1]) if parts[1].isdigit() else 0
    return {"added": added, "removed": removed, "files": files}


@dataclass(frozen=True)
class _CumulativeDiff:
    paths: tuple[str, ...]
    changed_lines: dict[str, Any]


def _committed_scope(
    *, cwd: Path, commits: Sequence[str], run_id: str = ""
) -> _CumulativeDiff:
    """Return paths and counts from the cited commits' own diffs.

    A tree diff from the first cited commit's parent to the run's tip is a diff
    of a span, not of a run: a head that merged the integration branch carries
    every path that branch changed, and the span charges them to the run. The
    paths are therefore read from each cited commit's diff against its first
    parent, and a cited merge is skipped rather than resolved, because what a
    merge brought in belongs to the branch it came from — not to the run that
    merged it.

    A citation list of merges alone carries no such commit, and a run's work
    can reach the record inside a merge: each cited merge is then measured
    against its first parent, which is the content the merge brought, so the
    run is charged that content rather than recorded as changing nothing.

    The counts are one net diff over the measured commits, restricted to the
    paths those commits touched and headed at the last of them, so a trailing
    merge is not charged the content it resolved to. Adding each commit's own
    numstat counts churn the run netted out for itself — a path rewritten
    across two cited commits contributes both revisions — so the row would
    describe the run's keystrokes rather than its effect.

    A citation list that measures no path is refused rather than recorded as
    zero: a row of zeros reads as a measured run that changed nothing, and a
    citation list whose commits change no path contains no deliverable at all.
    """
    if not commits:
        return _CumulativeDiff((), {"available": False, "reason": "missing_base"})
    merges = set(_merge_revisions(cwd, commits))
    measured = [commit for commit in commits if commit not in merges]
    if not measured:
        # Every cited commit is a merge, so the run's content is what each
        # merge brought relative to its first parent: that is what gets
        # charged. _merge_revisions preserves citation order.
        measured = [commit for commit in commits if commit in merges]
    paths: list[str] = []
    seen: set[str] = set()
    for commit in measured:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--numstat",
                "--no-renames",
                "-z",
                f"{commit}^1",
                commit,
                "--",
            ],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            return _CumulativeDiff(
                (), {"available": False, "reason": "diff_unavailable"}
            )
        for raw_line in (item for item in result.stdout.split(b"\0") if item):
            fields = raw_line.split(b"\t", 2)
            if len(fields) != 3:
                continue
            path = os.fsdecode(fields[2])
            if path not in seen:
                seen.add(path)
                paths.append(path)
    if not paths:
        raise CrewError(
            f"run {run_id!r} cites "
            + ", ".join(str(commit) for commit in commits)
            + ", and none of them changes a path, so this citation list "
            "measures no path. Cite the commit(s) whose diff is the work, or "
            "pass --no-commit '<why>' when the run produced none"
        )
    counts = scoped_diff_stat(
        cwd=cwd, base=f"{measured[0]}^1", head=measured[-1], paths=paths
    )
    if not counts.get("available", True):
        return _CumulativeDiff(
            (),
            {"available": False, "reason": counts.get("reason") or "diff_unavailable"},
        )
    return _CumulativeDiff(
        tuple(paths),
        {
            "added": counts["added"],
            "removed": counts["removed"],
            "files": len(paths),
        },
    )


def _uncommitted_paths(tree: Path) -> list[str]:
    """Return the tree's own report of the paths it holds uncommitted.

    Untracked paths count: a deliverable written but never staged is exactly
    the work a promotion must not record as landed, so the reading is what the
    working tree holds rather than what the index knows about it. A tree git
    cannot read reports nothing, and the caller treats that as unmeasured
    rather than as clean.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return []
    paths: list[str] = []
    for line in result.stdout.splitlines():
        entry = line[3:].strip()
        if not entry:
            continue
        if " -> " in entry:
            entry = entry.rsplit(" -> ", 1)[1]
        paths.append(entry.strip('"'))
    return paths


def _declared_repository_roots(
    record: Mapping[str, Any], *, tree: Path
) -> tuple[Path, ...]:
    """Return the paths this run declared it would write or change.

    A promotion may only charge a run with dirt it declared: a shared checkout
    and a fleet worktree both carry other sessions' uncommitted work, so an
    undeclared stray path is not this run's evidence and not this run's
    deliverable. The node's declared write paths and a fresh manifest's changed
    paths are the two declarations a run makes, and a declaration outside the
    repository contributes no root here.
    """
    declared = [
        str(path) for path in ((record.get("node") or {}).get("write_paths") or ())
    ]
    manifest_present, fresh = _manifest_freshness(record)
    if manifest_present and fresh:
        try:
            manifest = parse_manifest(
                Path(str(record.get("manifest_path") or "")).read_text(encoding="utf-8")
            )
        except (OSError, KeyError, ValueError):
            manifest = {}
        declared += [
            str(path) for path in _changed_paths_inside_repository(manifest, record)
        ]
    return _repository_scope_paths(
        declared,
        worktree=_scope_worktree(record, tree),
        repository=Path(str(record.get("repo") or tree)),
    )


def _require_commits_beyond_base(
    run_id: str,
    record: Mapping[str, Any],
    commits: Sequence[str],
) -> None:
    """Refuse a promotion whose citations are not work this run made.

    A ledger row asserts what a run produced beyond the base it was dispatched
    against, and two states make that assertion false while still looking true.
    A citation of the base itself, or of any commit behind it, satisfies every
    ancestry question a later sweep asks — so a run reads as landed while
    nothing it did is in the repository, which is the same
    true-statement-for-the-needed-fact shape a promoted revision exists to
    avoid. And a commitless promotion over a tree still holding the run's
    declared work records the run as complete while its deliverable exists in
    no commit at all, which the release step then takes with the worktree.

    The comparison is made in the run's own tree, so a citation that resolves
    to an abbreviated revision still equals the base it names, and a citation
    that resolves nowhere is left to the guard whose refusal names the
    repository it consulted rather than answered here with this one. The tree
    is the one the record names, read the way every other reader reads it: a
    record that names no readable directory is not measured here rather than
    measured against whatever repository the promotion happens to run in. A
    tree git cannot read, or a base that does not resolve in it, leaves the
    guard silent: an unmeasured citation is not a defect in the citation.
    """
    base = str(record.get("base_sha") or "").strip()
    tree = _record_tree(record)
    if not base or tree is None:
        return
    canonical_base = _commit_canonical_id(tree, base)
    if canonical_base is None:
        return
    for revision in commits:
        cited = str(revision).strip()
        if not cited:
            continue
        canonical = _commit_canonical_id(tree, cited)
        if canonical is None:
            continue
        if canonical == canonical_base:
            raise CrewError(
                f"run {run_id!r} cites {canonical} as its own work, but that is "
                f"the base it was dispatched against ({base}). A promotion "
                "records what the run made beyond its base, and a citation of "
                "the base itself would read as landed work that predates the "
                "run. Cite the commit the run committed on top of the base, or "
                "pass --no-commit '<why>' when it produced none"
            )
        if not _revision_is_ancestor(tree, canonical_base, canonical):
            raise CrewError(
                f"run {run_id!r} cites {canonical}, which is not a commit this "
                f"run made beyond its base {base}: the run's work is what it "
                "committed on top of the base, so this citation belongs to "
                "another branch or predates the run. Cite the run's own commit, "
                "or pass --no-commit '<why>' when it produced none"
            )
    if commits:
        return
    uncommitted = _uncommitted_paths(tree)
    if not uncommitted:
        return
    roots = _declared_repository_roots(record, tree=tree)
    if not roots:
        return
    held = [
        path
        for path in uncommitted
        if any(Path(path) == root or Path(path).is_relative_to(root) for root in roots)
    ]
    if not held:
        return
    raise CrewError(
        f"run {run_id!r} cites no commit beyond its base {base} while its "
        "worktree holds its own declared work uncommitted: "
        + ", ".join(sorted(held))
        + ". Promoting it would record the run as landed with that work in no "
        "commit, and the release step takes the worktree with it. Commit the "
        "work and cite the commit, so the row points at something the run made"
    )


def _registered_repository_roots() -> list[Path]:
    """Return the repository root of every registered project mount."""
    path = _store._mounts_path()
    if not path.exists():
        return []
    try:
        mounts = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    roots: list[Path] = []
    for raw in (mounts or {}).values():
        try:
            docs = Path(str(raw)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        root = docs.parent
        if root not in roots and (root / ".git").exists():
            roots.append(root)
    return roots


def _run_commit_directory(record: Mapping[str, Any]) -> Path | None:
    """The directory a run's commit objects are read through, or None.

    A run commits in its own worktree, and a worktree is a checkout of a
    repository that shares its object store, so the same revisions resolve
    through the repository as well. The worktree is released once the run ends,
    and the path it named is then not a directory at all: a git call whose
    working directory has been removed raises rather than reporting, which left
    a run whose worktree was already reclaimed impossible to promote. Reading
    its commits through the repository instead keeps resolution, ancestry and
    citation checks answering for it as they did while the worktree was there.
    A record that names no readable directory names none at all: an empty field
    is absent rather than the current directory, and a caller reads the absence
    as a citation that cannot be measured rather than one that is absent.
    """
    return _record_tree(record)


def _commit_canonical_id(root: Path, revision: str) -> str | None:
    """Return the canonical object id one revision names, or None.

    The same resolution the scope paths use, returning the canonical id rather
    than a boolean so an abbreviated or branch-named revision can be compared
    against another spelling of the same commit. None is an unresolvable
    revision, never evidence of absence. A root that is not a readable
    directory is unmeasurable in the same way — a git invocation whose working
    directory has been removed raises rather than reporting — and a caller
    holding a run's record reads through ``_run_commit_directory`` so a
    released worktree is never the directory this call runs in.
    """
    if not root.is_dir():
        return None
    return _resolve_commit(root, revision) or None


def _commit_resolves_in(root: Path, revision: str) -> bool:
    """Report whether one revision names a commit object in one repository."""
    return _commit_canonical_id(root, revision) is not None


def _declares_absent_commits(entry: str) -> bool:
    """Whether one ``commits:`` entry declares absence rather than citing an
    id.

    A report-only node writes ``commits: none (repository worktree remained
    clean)``: the record stating it has no commit to cite, not a citation that
    fails to resolve. Read as the declaration it is, so no store is asked about
    it, and a manifest that names no commit is unaffected by the citation check.
    The vocabulary itself is reports' — one statement of what an explicit
    nothing looks like in a manifest field — so a word added there is honoured
    here rather than shadowed by a second list.
    """
    head = entry.split("(", 1)[0].strip().lower()
    return head in _MANIFEST_NOTHING


# Roles whose run carries no repository work to commit: a review delivers a
# report and an investigation delivers findings, so a ``commits:`` line that
# declares an absence is the honest answer and ``none`` is the word for it.
_COMMITLESS_ROLES: frozenset[str] = frozenset({"review", "investigate"})

# What may follow an absence word for the word to stand alone: the end of the
# value, or any character that is not a letter, digit or underscore. A word
# character after the letters means a longer word that merely begins with them.
_ABSENCE_BOUNDARY = r"(?!\w)"

_FIELD_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def _raw_manifest_field(text: str, key: str) -> str | None:
    """The value one ``key: value`` field carries, as the node wrote it.

    The parsed manifest cannot answer this question. Its list reader empties a
    field that holds only an absence word, so ``commits: none, no repository
    change`` loses the word ``none`` and arrives as ``['no repository change']``
    — the declaration gone before anything reads it. What the node wrote is the
    text, so the raw line's value is returned here instead. A JSON manifest
    shares no such line and returns ``None``; the caller then falls back to the
    parsed entries, which is all a JSON field ever offered.
    """
    if text.lstrip().startswith(("{", "[")):
        return None
    for line in text.splitlines():
        match = _FIELD_LINE.match(line)
        if match and match.group(1).lower() == key.lower():
            return match.group(2)
    return None


def _opens_with_an_absence_word(value: str) -> bool:
    """Whether a raw field value opens with an absence word and then a boundary.

    The word must stand alone — followed by the end of the value or by any
    character that is not a letter, digit or underscore, so sentence punctuation
    such as a full stop, an en dash, a slash or a closing parenthesis ends the
    word exactly as the end of the value does. ``nonesuch`` begins with the
    letters of ``none`` but continues with a letter, so it is a different word,
    not a declaration, and is read as the citation it looks like.
    """
    stripped = value.strip()
    return any(
        re.match(rf"{re.escape(word)}{_ABSENCE_BOUNDARY}", stripped, re.IGNORECASE)
        for word in _MANIFEST_NOTHING
        if word
    )


def _commitless_changed_paths_declares_absence(
    manifest_text: str, record: Mapping[str, Any]
) -> bool:
    """Whether a commitless run's ``changed_paths`` field claims no change.

    The plain declaration reader, on the same boundary the ``commits`` field
    uses. A value in this field may itself be a path — ``none.txt`` is a
    filename as plausibly as ``none`` is a declaration — so any narrower shape
    rule could only guess which one a value meant, and each guess was answered
    by the next prose shape that spoofed it. The claim is read here and it is
    not evidence: whether the claim holds is settled from the worktree, by
    ``_worktree_repository_changes``.
    """
    raw = _commitless_raw_field(manifest_text, record, "changed_paths")
    return raw is not None and _opens_with_an_absence_word(raw)


def _worktree_git_paths(tree: Path, *arguments: str) -> tuple[str, ...] | None:
    """The path lines one ``git`` call prints in the run's worktree.

    ``None`` when the call cannot be made or fails, which the caller reads as
    "nothing measurable here" rather than as "no change": a run whose worktree
    cannot be read is not evidence of a clean one.
    """
    if not tree.is_dir():
        return None
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        return None
    return tuple(line for line in completed.stdout.splitlines() if line.strip())


# The entries a provisioned worktree always carries and the run did not write.
# The ``.venv`` symlink is the one the dispatch rule plants in every worktree
# and it is usually gitignored, which excludes it already; it is named here so a
# worktree whose ignore rule is a bare ``.venv`` entry is not read as dirty.
_PROVISIONED_WORKTREE_ENTRIES: frozenset[str] = frozenset({".venv"})


def _worktree_repository_changes(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The repository paths a run's worktree changed against its base sha.

    The path-based declaration rules are gone because no shape rule can tell a
    path from a declaration in a free-text field: ``none.txt`` is a filename and
    ``none, no repository change`` opens with the word ``none``, and every
    attempt to separate them was answered by the next prose shape that spoofed
    it. So a commitless role's claim to have changed nothing is checked against
    the worktree itself, which cannot be written to or spoofed by the manifest.
    Three measurements, taken in the run's own worktree, are unioned:

    * the files the worktree's commits touched between the run's recorded
      ``base_sha`` and its ``HEAD``,
    * the tracked files it has modified, staged or not,
    * the untracked repository files it holds, read through
      ``_worktree_untracked_paths`` so the exemption and this check share one
      enumeration rather than two git verbs that can drift.

    An empty result is the only condition under which a commitless role may
    promote with no commits. A run that records no base sha or no readable
    worktree yields the empty tuple, so an unmeasurable run falls through to the
    manifest check rather than being refused on no evidence — the same silence
    ``_require_gate_evidence`` keeps when it cannot read the repository.
    """
    # The worktree is the only tree this measures: the record's repository is a
    # different tree the run did not work in, so a change found there is not
    # this run's, and a blank field names no tree rather than the current
    # directory.
    tree = _record_worktree(record)
    if tree is None:
        return ()
    base = str(record.get("base_sha") or "").strip()
    changed: set[str] = set()
    if base:
        committed = _worktree_git_paths(tree, "diff", "--name-only", base, "HEAD")
        if committed:
            changed.update(committed)
    modified = _worktree_git_paths(tree, "diff", "--name-only")
    if modified:
        changed.update(modified)
    changed.update(_worktree_untracked_paths(tree))
    return tuple(sorted(changed))


def _manifest_declares_no_change(
    record: Mapping[str, Any], manifest: Mapping[str, Any], manifest_text: str
) -> bool:
    """Whether a run's manifest declares no repository work against its base.

    The manifest half of the unchanged-run exemption. A fresh manifest whose
    ``changed_paths`` claims no repository path and which cites no commit other
    than the base the run was dispatched against has declared that it changed
    nothing. Like every other declaration this is read, not believed: whether
    the claim holds is settled from the worktree by
    ``_worktree_unchanged_since_base``.
    """
    if not _changed_paths_declare_no_paths(manifest, record, manifest_text):
        return False
    base = str(record.get("base_sha") or "").strip()
    # The run's own worktree only: the repository is a different tree whose
    # history this run did not write, and a blank field names no tree rather
    # than the current directory. With no tree named, a citation is not settled
    # here at all.
    tree = _record_worktree(record)
    if tree is None:
        return False
    canonical_base = _commit_canonical_id(tree, base) if base else None
    for raw in manifest.get("commits") or ():
        cited = str(raw).strip()
        if not cited:
            continue
        canonical = _commit_canonical_id(tree, cited)
        if canonical is None or canonical_base is None or canonical != canonical_base:
            return False
    return True


def _worktree_untracked_paths(tree: Path) -> tuple[str, ...]:
    """The untracked repository paths a worktree holds, minus provisioned ones.

    The single reader of a worktree's untracked paths: both the unchanged-run
    exemption and the commitless change check take their untracked set from
    here, so the two cannot read the same fact through two git verbs that
    drift. Read from ``git ls-files --others --exclude-standard`` so an
    untracked deliverable is visible at file precision — an untracked file is
    exactly the repository work a commitless run can leave behind while its
    ``HEAD`` still sits on the base. The provisioned ``.venv`` symlink the
    dispatch rule plants in every worktree is not the run's work and is
    dropped.
    """
    listed = _worktree_git_paths(tree, "ls-files", "--others", "--exclude-standard")
    if not listed:
        # A worktree whose directory is gone yields ``None`` rather than an empty
        # list, and the honest reading of an unmeasurable tree is the empty set:
        # a commitless run whose tree has been reclaimed holds no untracked path
        # this check can see, so it reads as no repository change rather than
        # raising on the iteration.
        return ()
    untracked: list[str] = []
    for line in listed:
        path = line.strip().strip('"')
        if not path:
            continue
        if Path(path).parts and Path(path).parts[0] in _PROVISIONED_WORKTREE_ENTRIES:
            continue
        untracked.append(path)
    return tuple(untracked)


def _worktree_unchanged_since_base(record: Mapping[str, Any]) -> bool:
    """Whether the run's worktree still sits on its base with no change.

    The worktree half of the unchanged-run exemption, and the only measurement
    of it: the manifest cannot be written to or spoofed by the worktree. The
    exemption is a claim about a worktree that is still there to be measured,
    so a run whose worktree directory is gone is proved nothing and the suite
    pair is required as before. A worktree that exists must have its ``HEAD``
    equal to the recorded base, no tracked file modified against it staged or
    not, and no untracked path the run left behind beside the provisioned
    symlink — an unstaged deliverable is repository work even when ``HEAD``
    never moved. A run that records no base cannot be proved unchanged against
    one, so an existing worktree with no base is refused the exemption rather
    than allowed on its manifest alone.
    """
    tree = _record_worktree(record)
    if tree is None:
        return False
    base = str(record.get("base_sha") or "").strip()
    if not base:
        return False
    canonical_base = _commit_canonical_id(tree, base)
    head = _worktree_git_paths(tree, "rev-parse", "HEAD")
    if canonical_base is None or not head:
        return False
    if _commit_canonical_id(tree, head[0]) != canonical_base:
        return False
    if _worktree_git_paths(tree, "diff", "--name-only", "HEAD"):
        return False
    return not _worktree_untracked_paths(tree)


def _run_changed_nothing(record: Mapping[str, Any]) -> bool:
    """Whether an armed run's own manifest and worktree show no change.

    The single condition under which an armed promotion may carry a suite delta
    of ``unchanged`` with no baseline and after suite pair: a fresh manifest
    declaring no changed path and no commit beyond the base, beside a worktree
    still sitting on that base. A run whose worktree head moved past the base,
    or whose manifest names a changed path or a commit past the base, is not
    this case and is refused as before.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not (manifest_present and fresh):
        return False
    if not _worktree_unchanged_since_base(record):
        return False
    manifest_text = _manifest_text(record)
    try:
        manifest = parse_manifest(manifest_text)
    except (OSError, KeyError, ValueError):
        return False
    return _manifest_declares_no_change(record, manifest, manifest_text)


def _commitless_raw_field(
    manifest_text: str, record: Mapping[str, Any], key: str
) -> str | None:
    """One field's raw value when a commitless role wrote the manifest.

    A report-only node writes one sentence into a field — ``commits: none
    (review node, no repository change)``, ``changed_paths: none under the
    repository; the sole deliverable is the report``. The list reader splits the
    field on commas and empties a field holding only an absence word, so once
    parsed neither shape still opens with the declaration: the first arrives as
    ``['none (review node', 'no repository change)']`` and the second as
    ``['no repository change']``. Read entry by entry, the surviving tail looks
    like an unresolvable citation and the line has to be blanked by hand — the
    symptom the raw-field reading removes.

    The value is returned only for a role that carries no repository work. A
    role that commits keeps the existing entry-by-entry reading, so a field
    opening with ``none`` beside further text is still resolved and still refused
    when that text names nothing.
    """
    from reckon.crew.recovery import _pointer_role

    if _pointer_role(record) not in _COMMITLESS_ROLES:
        return None
    return _raw_manifest_field(manifest_text, key)


def _commits_field_declares_absence(
    manifest_text: str, record: Mapping[str, Any]
) -> bool:
    """Whether a commitless run's ``commits`` field declares an absence.

    The ``commits`` field follows the permissive boundary — the absence word
    ending at the value edge or at any character that is not a letter, digit or
    underscore. It is safe here because a commit value cannot be a path: a
    revision citation either is the declared absence or names a commit, so
    reading ``none`` before a full stop as the declaration costs nothing, and
    the shapes a node actually writes (``none.``, ``none (review node)``,
    ``none under the repository``) all have to be caught.
    """
    raw = _commitless_raw_field(manifest_text, record, "commits")
    return raw is not None and _opens_with_an_absence_word(raw)


def _manifest_text(record: Mapping[str, Any]) -> str:
    """The run's manifest bytes, or the empty string when it cannot be read."""
    try:
        return Path(str(record["manifest_path"])).read_text(encoding="utf-8")
    except (OSError, KeyError):
        return ""


def _presented_commits_without_a_declaration(
    record: Mapping[str, Any], commits: Iterable[str]
) -> tuple[str, ...]:
    """The commits a promotion presents, with a commitless declaration read as none.

    A review that has no repository work to commit writes the declaration into
    its manifest's ``commits`` field, and the list reader leaves the sentence
    behind where a citation list would be. The declaration parser is the one
    authority on whether that field declares an absence, so it is asked here,
    and a word that only begins with an absence word is not a declaration, so
    such a value is still presented as the citation attempt it looks like.
    """
    presented = tuple(str(sha).strip() for sha in commits if str(sha).strip())
    if presented and _commits_field_declares_absence(_manifest_text(record), record):
        return ()
    return presented


def _unresolved_citations(root: Path, entries: Iterable[str]) -> list[str]:
    """The cited identifiers that resolve to no object in the given store.

    Every identifier a record cites is resolved against the store that owns it,
    and the check asks the store about the value as cited rather than judging
    its spelling. A width or character-class requirement is the trap here:
    measured 2026-09-04 in this repository, a forty-character requirement on a
    sha caused a confabulation and then concealed it, because the producer's
    tooling does not hand it forty characters, so compliance meant guessing. A
    fabricated value passes every shape check — forty hexadecimal characters,
    the right prefix, nothing behind it — and only the store can tell the
    difference.
    """
    return [
        entry
        for entry in entries
        if not _declares_absent_commits(entry) and not _commit_resolves_in(root, entry)
    ]


def _foreign_repository(revision: str, *, exclude: Path) -> Path | None:
    """Name the registered repository a stray revision actually belongs to."""
    for root in _registered_repository_roots():
        if root == exclude:
            continue
        try:
            if _commit_resolves_in(root, revision):
                return root
        except OSError:
            continue
    return None


def _require_gate_evidence(
    run_id: str,
    record: Mapping[str, Any],
    *,
    verdict: str,
    commits: tuple[str, ...],
    no_commit_reason: str,
) -> dict[str, Any] | None:
    """Refuse an uncited commitless gate, and report a presented shortfall.

    Gate correctness and integration completeness are two separate claims, and a
    ledger row must carry both: a gate can be independently defensible — the
    node's externally visible goal met, exit status 0 — while repository
    integration is recorded nowhere at all.

    A commitless promotion is normal: a report-only node produces a manifest and
    no commit, and that is its deliverable. The defect is narrower and it is
    measurable — a worktree whose HEAD has moved off its base *made* commits, so
    a passing gate citing none loses the binding between the ledger row and the
    work. Measured: one run promoted `gate: passed` with `commits: []` while its
    worktree held a commit unreachable from the integration branch. Only the
    workspace collector's `unintegrated` classification saved that work, and it
    would have gone the moment someone believed the ledger.

    Inferring this from the node's role or sandbox tier instead would refuse
    every honest commitless promotion, so it reads the repository rather than
    the metadata, and stays silent whenever it cannot measure.

    The other half of the same silence is a presentation that is short
    of the manifest's list, and it is the warn half of refuse-or-warn: a
    passing promotion whose presented commits are a strict subset of the
    manifest's declared commits is reported — naming how many were presented,
    how many the manifest declared, and which revisions are missing — and never
    refused. The out-of-scope boundary check runs against the commits a
    promotion presents, so an incomplete presentation silently narrows that
    check. Only the strict-subset direction is reported: a presented commit the
    manifest does not declare is the coordinator having declined the worker's
    revision, where the presented list is the one that resolves, so reporting
    it would name the list that resolves and recreate the confusion the counts
    cause. Only declared commits that resolve in the run's repository are
    compared, so a manifest quoting a revision that resolves nowhere is neither
    counted nor named as missing — it is not authoritative. The report is a
    returned value, never an exception: a shortfall must not turn a passing
    promotion into a failed one.

    A presented citation is the other half of that split and is refused rather
    than reported: the presented list is what the boundary check and the ledger
    resolve, so an entry that resolves to no object is a defective citation
    naming nothing, not a count short of the manifest's. Refusing it by value
    where the presentation is read keeps it from being filtered out of the
    comparison, which would leave the report resting on a set nothing can be
    found for. The refusal stays silent when the tree cannot be read, so a run
    whose worktree is already gone is not refused for a citation no store was
    asked about.
    """
    if verdict != "passed" or str(no_commit_reason).strip():
        return None

    # First ask the worker, because it already answered. The manifest's
    # `commits:` line is delivered evidence that Reckon holds and, until now,
    # discarded: a coordinator that omitted one flag produced a ledger saying
    # the node succeeded with nothing pointing at the work. Naming the exact
    # revisions is more use than describing the condition, so the manifest is
    # read before the repository check below. A run whose worktree was
    # reclaimed after it ended is read through its repository, which shares the
    # object store, so its citations resolve rather than going unmeasured.
    tree = _run_commit_directory(record)
    manifest_present, fresh = _manifest_freshness(record)
    delivered: dict[str, Any] = {}
    delivered_text = ""
    if manifest_present and fresh and tree is not None:
        try:
            delivered_text = Path(str(record["manifest_path"])).read_text(
                encoding="utf-8"
            )
            delivered = parse_manifest(delivered_text)
        except (OSError, KeyError, ValueError):
            delivered = {}
    declared = [
        str(sha).strip() for sha in (delivered.get("commits") or []) if str(sha).strip()
    ]
    declared_absent = _commits_field_declares_absence(delivered_text, record)

    presented = [str(sha).strip() for sha in commits if str(sha).strip()]
    if presented:
        # A promotion that presents commits is the case the commitless guard
        # cannot see: its condition returns on any non-empty list, so a
        # presentation naming one commit of the manifest's declared eight
        # passes through untouched and the boundary check runs against the one.
        #
        # A presented identifier must resolve in the run repository like any
        # other citation: the presented list is the one the out-of-scope
        # boundary check and the ledger resolve, so a value that names nothing
        # is refused by value here rather than filtered out of the comparison
        # below, where a count would rest on an entry nothing can be found for.
        # The measurement is taken in a tree that can be read, which for a
        # reclaimed run is its repository rather than the worktree it no longer
        # has.
        if tree is None:
            # With no tree named, none of these citations can be measured, and
            # an unmeasured citation is not a failed one: this branch's subset
            # report is not written.
            return None
        unresolved_presented = [
            candidate
            for candidate in presented
            if _commit_canonical_id(tree, candidate) is None
        ]
        # A commitless declaration the declaration parser recognises means the
        # run presents no commits rather than a value that names nothing: a
        # review writes `commits: none (review node; no repository change)`
        # because a review has no repository work to commit, and the prose the
        # list reader leaves behind is a declaration, not a citation. A word
        # that only begins with an absence word is still not a declaration, so
        # `nonesuch` is refused here as the citation attempt it looks like.
        if unresolved_presented and not declared_absent:
            raise CrewError(
                f"run {run_id!r} presents "
                + ", ".join(repr(entry) for entry in unresolved_presented)
                + " as a commit, but that identifier does not resolve to a "
                f"commit object in the run repository ({tree}). The presented "
                "list is what the boundary check and the ledger resolve, so a "
                "value that names nothing is not evidence and is reported "
                "rather than dropped from the comparison. Cite the commit the "
                "run actually wrote, or leave the value out of the presentation"
            )
        resolving_declared = {
            canonical
            for candidate in declared
            if (canonical := _commit_canonical_id(tree, candidate)) is not None
        }
        resolving_presented = {
            canonical
            for candidate in presented
            if (canonical := _commit_canonical_id(tree, candidate)) is not None
        }
        # A strict subset is the only shape reported. Equality is a full
        # presentation; a superset, or a presented commit outside the declared
        # list, is the coordinator declining the worker's revision, where the
        # presented list resolves and the manifest's does not.
        if resolving_presented and resolving_presented < resolving_declared:
            missing = sorted(resolving_declared - resolving_presented)
            return {
                "kind": "presented_commits_subset_of_manifest",
                "presented": len(presented),
                "declared": len(resolving_declared),
                "missing": missing,
                "message": (
                    f"run {run_id!r} has a passing gate presenting "
                    f"{len(presented)} commit(s) of the {len(resolving_declared)} "
                    f"its manifest declares; missing: {', '.join(missing)}. The "
                    "out-of-scope boundary check runs against the presented "
                    "commits, so an incomplete presentation narrows it. This is "
                    "a report, not a refusal — present the full list, or the "
                    "boundary check may not cover the node's work"
                ),
            }
        return None

    # An identifier a record cites must resolve in the store that owns it, and
    # the manifest's commit citations are the ones nothing else here resolves:
    # the presented list goes through `_resolve_commits`, while a declared one
    # reaches the record as text. A row that reads as evidence and points at
    # nothing is the defect this refusal exists for, so it is raised under the
    # citation's own name before the commitless guard below can answer with the
    # broader complaint that no commit was cited at all.
    unresolved = (
        [] if declared_absent or tree is None else _unresolved_citations(tree, declared)
    )
    if unresolved:
        raise CrewError(
            f"run {run_id!r} cites "
            + ", ".join(repr(entry) for entry in unresolved)
            + " as a commit, but that identifier does not resolve to a commit "
            f"object in the run repository ({tree}). A value assembled rather than "
            "copied passes every shape check and names nothing, so the store is "
            "asked about the value as cited and never about its form. Cite the "
            "commit the run actually wrote — `reckon crew recover` reports it as "
            "the run's next action — or pass --no-commit '<why>' to record "
            "deliberately that the commits are not being registered"
        )

    # Only an entry that resolves to a real commit means Reckon is holding
    # something. The line is free text a worker wrote: a report-only node
    # writes `commits: none (repository worktree remained clean)`, which is
    # neither a revision nor an omission, and matching a literal "none"
    # would refuse it. Resolving instead of pattern-matching cannot make
    # that mistake.
    stated = (
        []
        if declared_absent or tree is None
        else [
            candidate
            for candidate in declared
            if candidate and _commit_resolves_in(tree, candidate)
        ]
    )
    if stated:
        raise CrewError(
            f"run {run_id!r} has a passing gate and cites no commit, but its "
            f"manifest records {len(stated)}: {', '.join(stated)}. Reckon is "
            "holding the answer and would discard it — the ledger row would "
            "say the node succeeded with nothing pointing at the work, and "
            "the commit would survive only as long as its worktree. Pass "
            "--commit for each, or --no-commit '<why>' to record "
            "deliberately that they are not being registered"
        )

    base = str(record.get("base_sha") or "").strip()
    # This one reads the worktree's own state rather than a commit object, so
    # the fallback above does not apply: the repository's HEAD belongs to a
    # tree this run did not work in, and reading it for a run whose worktree is
    # gone would refuse a truthful promotion on another tree's movement. With
    # no worktree there is nothing to ask, so the guard stays silent: a blank
    # field names no worktree rather than the current directory.
    worktree = _record_worktree(record)
    if not base or worktree is None:
        return None
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    tip = head.stdout.strip()
    if head.returncode or not tip or tip == base:
        return None
    raise CrewError(
        f"run {run_id!r} has a passing gate and cites no commit, but its "
        f"worktree is at {tip[:12]} rather than its base {base[:12]}: it "
        "committed, and nothing on the record says what. Work left only in a "
        "worktree is discarded the moment someone believes the ledger. Cite the "
        "commit, or pass --no-commit '<why>' to record deliberately that this "
        "node's commits are not being registered"
    )


_EXIT_RECORD = re.compile(r"EXIT=(-?\d+)")

# A test runner's result-count token ("518 passed", "3 failed", "1 warning"),
# which is direct evidence the runner itself executed. The scan below reads the
# log as text and parses no runner's schema, so this is a conservative marker of
# a result summary rather than a full pytest tail parse.
_RUNNER_SUMMARY = re.compile(
    r"\b\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b"
)

# Exit statuses that assert the shell could not execute the command at all:
# 127 for a command it could not find, 126 for one it found but could not run.
# A capture shell recording either has not evidenced that the cited command
# ran, so such a sentinel is not the positive record a quoted diagnostic needs.
_UNEXECUTED_EXIT_CODES = frozenset({126, 127})


def _log_shows_the_command_ran(log_text: str, recorded: int | None) -> bool:
    """Whether the log carries positive evidence the cited command executed.

    Two shapes count, and both are what a real capture produces: the runner's
    own result-count summary, and a terminal ``EXIT=<n>`` record from the
    capture shell, since writing that line means the shell returned from the
    command. An exit in ``_UNEXECUTED_EXIT_CODES`` is excluded, because that
    shell status is itself a statement that the command never started.
    """
    if _RUNNER_SUMMARY.search(log_text) is not None:
        return True
    return recorded is not None and recorded not in _UNEXECUTED_EXIT_CODES


def _first_command_not_found_line(log_text: str) -> tuple[int, str] | None:
    """The first line carrying the shell's own command-not-found diagnostic.

    Returns the one-based line number and the stripped line text, so the refusal
    can name exactly which line it matched rather than leaving the author to
    search a long log for the phrase.
    """
    for number, raw_line in enumerate(log_text.splitlines(), start=1):
        if re.search(r"\bcommand not found\b", raw_line):
            return number, raw_line.strip()
    return None


def _recorded_exit_status(log_text: str) -> int | None:
    """The status a shell capture recorder wrote, read from the log's tail.

    The capture convention this fleet documents is ``> log 2>&1; echo EXIT=$?``,
    which writes the command's own status as a final ``EXIT=<n>`` line. Only an
    end-of-log record is read: a status marker buried inside a runner's own
    output is not this check's evidence, so a passing log that merely mentions
    the token is left alone.
    """
    for raw_line in reversed(log_text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        match = _EXIT_RECORD.fullmatch(line)
        return int(match.group(1)) if match else None
    return None


def _require_gate_log_agrees(
    run_id: str,
    gate_check: Mapping[str, Any] | None,
    *,
    verdict: str,
) -> None:
    """Refuse a passing gate whose cited log contradicts the asserted check.

    A promotion cites three pieces of evidence for a passing gate: the check
    command, its asserted exit status, and its captured log. A contradiction is
    refused before the ledger stores a verdict that downstream calibration and
    routing would otherwise treat as evidence.

    The check reads the log as text and parses no runner's result format: it
    never re-runs the command and it recognises no test-output schema, so a
    log that happens to print a runner's summary is untouched. It refuses on
    three observable shapes and names which it found:

    * an empty log — a cited log file that resolves to no content evidences
      nothing, and the empty log itself is the contradiction. A path that
      cannot be read at promotion time is skipped rather than refused, because
      promotion may run from a machine the worker's log never reached; an
      absent log is the unfalsifiable-gate refusal's subject, not this one's.
    * a recorded exit status that contradicts the asserted one — a log whose
      terminal ``EXIT=<n>`` capture record differs from the asserted status
      has filed contradictory evidence.
    * no evidence the command ran — a line carrying the shell's own
      command-not-found diagnostic, when the log carries nothing that shows a
      command did run, states the command never executed. A runner log may
      quote that phrase in fixture or assertion output, so the diagnostic is
      read only in the absence of a positive record: a terminal ``EXIT=<n>``
      capture line other than the two statuses that say the shell could not
      execute the command, or a runner's own result-count summary (``518
      passed, 20 failed``), either of which shows the command executed and its
      output was captured. When the diagnostic is the only evidence, the
      refusal names the matched line and its one-based line number.
    """
    if verdict != "passed" or not isinstance(gate_check, Mapping):
        return
    log_path = str(gate_check.get("log_path") or "").strip()
    if not log_path:
        # Only a digest was cited; there is no text to contradict the claim.
        return
    try:
        log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not log_text.strip():
        raise CrewError(
            f"run {run_id!r} asserts gate 'passed' but its cited log "
            f"{log_path!r} is empty: a check that captured nothing evidences "
            "nothing, and the empty log contradicts the passing verdict. "
            "Found: an empty log. Re-run the check and cite its full captured "
            "output with --gate-log-path, or re-promote with the verdict the "
            "evidence actually records"
        )
    recorded = _recorded_exit_status(log_text)
    asserted = gate_check.get("exit_status")
    if (
        recorded is not None
        and isinstance(asserted, int)
        and not isinstance(asserted, bool)
        and recorded != asserted
    ):
        raise CrewError(
            f"run {run_id!r} asserts gate 'passed' with exit status {asserted} "
            f"but its cited log {log_path!r} records EXIT={recorded}: the log "
            "contradicts the asserted status. Found: a recorded exit status "
            "that contradicts the asserted one. Re-run the check and cite its "
            "log, or re-promote with the verdict the evidence actually shows"
        )
    not_found = _first_command_not_found_line(log_text)
    if not_found is not None and not _log_shows_the_command_ran(log_text, recorded):
        number, line = not_found
        raise CrewError(
            f"run {run_id!r} asserts gate 'passed' but its cited log "
            f"{log_path!r} carries the shell's 'command not found' "
            f"diagnostic at line {number}: {line!r}, and shows nothing else "
            "that a command ran. The command never ran, so the log is no "
            "evidence of the pass being asserted. Found: no evidence the "
            "command ran. Re-run the check and cite its log, or re-promote "
            "with the verdict the evidence actually shows"
        )


def _head_arm_log_failure_ids(gate_check: Mapping[str, Any] | None) -> set[str] | None:
    """The failing ids the head arm's log reports, or ``None`` when it is unreadable.

    The head arm of a gated measurement is the run's own check, so its log is
    the one the promotion cites: the ids are read from that log's own
    ``FAILED``/``ERROR`` summary lines, canonicalised the way every other arm
    reader canonicalises them. A citation that names no path, or one that
    cannot be read from here, reports ``None`` rather than an empty set — an
    unread log is not a log that failed nothing, and a comparison taken over an
    empty set would claim exactly that.
    """
    if not isinstance(gate_check, Mapping):
        return None
    raw = str(gate_check.get("log_path") or "").strip()
    if not raw:
        return None
    try:
        log_text = Path(raw).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _control_failure_ids(log_text)


def _zero_added_against_a_red_base(
    record: Mapping[str, Any], gate_check: Mapping[str, Any] | None
) -> bool:
    """Whether the run's own manifest shows a red base the head did not worsen.

    A gate is judged here by its delta against its base, so a nonzero exit
    beside a passing verdict is admitted only when the manifest records a
    baseline observation that is itself red — a nonzero exit status — whose
    recorded failure ids include every id the head arm's log reports, and when
    both arms declare their runs complete. That is zero added against a red
    base, which is what the passing verdict then states; anything less is
    refused by the caller.

    The head ids are read from the cited gate log's own ``FAILED``/``ERROR``
    lines, never from the manifest's ``after_suite`` list: the manifest is the
    run's own record, so a failure left out of it would read as zero added
    while the log beside it names the id. Completion is asked of both arms as a
    literal ``True``, never a truthy stand-in, the way the strict arm validator
    asks it. An interrupted arm lists only the failures it reached before it
    stopped: a short baseline then covers every id an unfinished head recorded
    while the comparison itself says nothing about what the head added. Every
    condition is asked of the run's own records, never inferred: a manifest
    that records no arm, an arm that does not declare completion, a baseline
    with no readable status or no readable list of failure ids, and a head log
    that cannot be read each leave the delta unmeasured, and an unmeasured
    delta cannot license the pair.
    """
    manifest = _fresh_manifest(record)
    if manifest is None:
        return False
    baseline = manifest.get("baseline_suite")
    after = manifest.get("after_suite")
    if not isinstance(baseline, Mapping) or not isinstance(after, Mapping):
        return False
    for arm in (baseline, after):
        if arm.get("completed") is not True:
            return False
    base_exit = baseline.get("exit_status")
    if isinstance(base_exit, bool) or not isinstance(base_exit, int) or base_exit == 0:
        return False
    base_ids = baseline.get("failure_ids")
    if not isinstance(base_ids, list) or any(
        not isinstance(test_id, str) or not test_id.strip() for test_id in base_ids
    ):
        return False
    canonical_base = {
        review_module.canonical_node_id(test_id.strip()) for test_id in base_ids
    }
    head_ids = _head_arm_log_failure_ids(gate_check)
    if head_ids is None:
        return False
    return head_ids <= canonical_base


def _arm_without_completion(manifest: Mapping[str, Any] | None) -> str | None:
    """Name the first recorded suite arm that does not declare its run complete.

    The refusal beside a nonzero exit status names the arm whose withheld
    completion is what the admission turned on, so a reader knows which
    observation to finish and record. Only an arm the manifest actually
    records can be named: an arm that is absent is not an arm that omitted the
    key, and calling it uncompleted would state a fact its absence does not.
    """
    if manifest is None:
        return None
    for name in ("baseline_suite", "after_suite"):
        observation = manifest.get(name)
        if not isinstance(observation, Mapping):
            continue
        if observation.get("completed") is not True:
            return name
    return None


def _require_verdict_matches_exit_status(
    run_id: str,
    record: Mapping[str, Any],
    gate_check: Mapping[str, Any] | None,
    *,
    verdict: str,
) -> None:
    """Refuse a passing verdict recorded beside a nonzero exit status.

    The verdict and the exit status are two statements about one run, and a
    check that reads only the log's terminal ``EXIT=<n>`` record cannot compare
    them: a log whose command wrote no such line records no status at all, so
    the comparison short-circuits on the missing record and a passing verdict
    sits beside a nonzero status unreported. The asserted status is therefore
    compared with the verdict directly, whether or not the log carries an exit
    record of its own.

    The one pair admitted is the repository's own delta rule: an armed run
    whose manifest records a red baseline covering every id the head arm's log
    reports — both arms declaring their runs complete — measures zero added
    against that base, so its passing verdict is the delta verdict and not a
    contradiction. The admission is read from the run's records by
    ``_zero_added_against_a_red_base``; everything else refuses, naming both
    the verdict and the status, and naming the arm whose withheld completion is
    what the admission turned on when there is one.
    """
    if verdict != "passed" or not isinstance(gate_check, Mapping):
        return
    asserted = gate_check.get("exit_status")
    if isinstance(asserted, bool) or not isinstance(asserted, int) or asserted == 0:
        return
    if _zero_added_against_a_red_base(record, gate_check):
        return
    incomplete = _arm_without_completion(_fresh_manifest(record))
    withheld = (
        ""
        if incomplete is None
        else (
            f", and the {incomplete} arm it records does not declare "
            "completed: true, so that arm never reached its suite's summary "
            "line and its list of failures is not the set it measured"
        )
    )
    raise CrewError(
        f"run {run_id!r} asserts gate 'passed' beside exit status {asserted}: a "
        "passed verdict states the check succeeded and a nonzero exit status "
        "states it did not, so the row would record two contradictory readings "
        f"of one run. Found: gate 'passed' with a nonzero exit status, "
        f"'{asserted}'{withheld}. Re-promote with the verdict the evidence "
        "shows, or — when the base this check measures against is itself red, "
        "the head adds no failure to it, and both suite arms record "
        "completed: true — the passing verdict states the zero added against "
        "that base"
    )


def _promoted_worker_exit(run_id: str) -> dict[str, Any] | None:
    """The worker's exit record, read verbatim from the run directory.

    A CLI dispatch's supervisor writes ``exit.json`` beside the worker it spawned.
    Promotion releases the run's live pointer and its worktree, but not the run
    directory: that survives, ``exit.json`` with it, until ``crew gc`` prunes it
    on a retention window, so the ledger row is the durable copy once gc runs.
    The row carries this promotion's copy under ``worker_exit`` when the file
    exists, and carries no such key when it does not: an empty key would read as
    the supervisor having run and recorded nothing, which is the opposite of a run
    whose supervisor never wrote a record.

    A file that exists but cannot be read or parsed as a JSON object is treated
    as absent rather than propagated as a failure: a corrupt file is not
    promoted into the durable row, and a run whose exit record is damaged still
    promotes on its gate evidence rather than being refused by an instrument.
    """
    try:
        data = json.loads((run_dir(run_id) / "exit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(data) if isinstance(data, Mapping) else None


def _recorded_disposition(record: Mapping[str, Any]) -> str:
    """The disposition verb a live pointer carries, empty when it carries none.

    ``record_run_disposition`` writes the verb a coordinator recorded for why a
    pointer may outlive its session, and the pointer is the only copy: this
    promotion deletes it, so a row that does not read the verb here has lost
    the distinction between a run that was deliberately disposed of and one
    that simply disappeared. A pointer with no such block, or one whose kind is
    empty, is a run that carried no recorded disposition rather than a defect —
    the verb is not inferred from anything else.
    """
    recorded = record.get("closure_disposition")
    if not isinstance(recorded, Mapping):
        return ""
    return str(recorded.get("kind") or "").strip()


_PRESERVED_GATE_LOG_NAME = "gate.log"

# The re-run's own capture, written beside the preserved gate log rather than
# over it: one file records what the worker's gate did at its base, the other
# what the replay of it did at the integrated tree, and a reader comparing the
# two must be able to open both.
_REPLAY_GATE_LOG_NAME = "verify-gate.log"

# The bound a gate re-run runs under before it is reported as timed out. A run
# whose own gate log records a duration was measured running the same command
# this replay executes, so the bound that fits it is derived from that duration
# rather than fixed: measured 2026-09-29, a gate whose log recorded 378 s was
# replayed under the fixed 300 s bound and reported not-run on every attempt,
# so a gate that legitimately outran the constant could not be verified at the
# merged head at all. Headroom covers the replay running slower than the run
# it is compared with; the ceiling caps a mis-recorded duration from mounting
# a bound that holds a fleet slot for an hour; the floor is the historical
# constant, so the derivation can only loosen the bound, never tighten it.
_REPLAY_BOUND_DEFAULT_SECONDS = 300.0
_REPLAY_BOUND_HEADROOM = 2.0
_REPLAY_BOUND_CEILING_SECONDS = 1800.0

# A test runner's own duration record ("213 passed in 378.29s (0:06:18)"): the
# elapsed-seconds token of a summary line. Read as text and parsed on no
# runner's schema, like the result-count marker above; the last match wins,
# because the final summary line is the whole gate's own duration.
_GATE_LOG_DURATION = re.compile(r"\bin (\d+(?:\.\d+)?)s\b")


def _replay_log_header(
    *,
    replay_command: str,
    checkout: Path,
    checkout_revision: str,
    integrated_revision: str,
    rewritten_roots: Sequence[str] = (),
) -> list[str]:
    """The header lines a re-run log opens with, in the gate log's own shape.

    The header is what makes the log self-describing for a reader who has only
    the file: which revision was measured, in which tree, and the exact text
    the shell was handed — which differs from the stored command whenever a
    recorded worktree root was rewritten.
    """
    lines = [
        (
            f"# replayed revision: {checkout_revision or 'unknown'} "
            f"(integrated {integrated_revision or 'unknown'})"
        ),
        f"# cwd of the replay: {checkout}",
        f"# command: {replay_command}",
    ]
    lines.extend(
        f"# worktree root rewritten: {root} -> {checkout}" for root in rewritten_roots
    )
    return lines


def _write_replay_log(
    log_path: str | Path | None,
    *,
    header: Sequence[str],
    output: str,
    exit_status: int | None,
    cut_short: str | None = None,
) -> Path | None:
    """Write a re-run's captured output under the run directory, or None.

    The text lands in the capture convention the fleet's gate logs use — header
    lines, the command's own output, and a terminal ``EXIT=<n>`` record — so
    the gate-log readers parse a re-run exactly as they parse the log it was
    replayed from. A re-run killed by the bound has no status to record, so no
    ``EXIT=`` line is written and the absence is the fact; the ``cut_short``
    statement says outright that it was stopped, so the log's three bare header
    lines are not left to read as a gate that simply printed nothing.

    Best-effort, like the cited-log preservation beside it: a log that cannot
    be written leaves the verdict untouched and reports no path.
    """
    if log_path is None:
        return None
    path = Path(log_path).expanduser()
    parts = [str(line) for line in header]
    body = str(output or "").rstrip("\n")
    if body.strip():
        parts.append(body)
    if cut_short:
        parts.append(f"# cut short: {cut_short}")
    if exit_status is not None:
        parts.append(f"EXIT={exit_status}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def _preserve_cited_gate_log(
    run_id: str,
    gate_check: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Copy a cited gate log into the run directory so the row cites a durable path.

    ``crew complete --gate-log-path`` records where a log *lies*, and a worker's
    log routinely lies somewhere that will not survive: ``/tmp`` is reaped, and a
    worktree's gitignored output directory goes with the worktree the promotion
    releases. The ledger row then cites a path that resolves to nothing — every
    check a reader runs on the row passes, and the evidence is gone.
    Compensating by hand does not scale: two coordinators independently copied 27
    gate logs into a reports directory before citing them.

    The run directory outlives the worktree and is pruned only by ``crew gc`` on
    a retention window, so a copy placed there is the durable form of the cited
    log. A cited log already inside the run directory is therefore returned
    unchanged — nothing needs copying, and a second copy would only duplicate it.

    A check that cites a digest with no log path has nothing to copy, so it is
    returned as given rather than dropped: the digest is the citation, and only
    the path is ever rewritten here.

    Preservation is best-effort and changes no verdict. Promotion may run from a
    machine the worker's log never reached, so a cited path that does not resolve
    has no text to contradict the verdict and must not turn a promotion into a
    refusal; a copy that cannot be written leaves the citation exactly as given,
    for the same reason. The gate verdict and the exit status are the caller's to
    decide, and this step reads neither.
    """
    if not isinstance(gate_check, Mapping):
        return None
    raw = str(gate_check.get("log_path") or "").strip()
    if not raw:
        return dict(gate_check)
    source = Path(raw).expanduser()
    if not source.is_file():
        return dict(gate_check)
    directory = run_dir(run_id)
    try:
        inside = source.resolve().is_relative_to(directory.resolve())
    except (OSError, RuntimeError, ValueError):
        inside = False
    if inside:
        return dict(gate_check)
    destination = directory / _PRESERVED_GATE_LOG_NAME
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    except OSError:
        return dict(gate_check)
    return {**dict(gate_check), "log_path": str(destination)}


# ── A recorded gate command is re-executed, so it has to be a command ───────

# Every shape below marks a description of a check rather than the check. The
# integration re-run executes the recorded text through a shell at the merged
# head, so a description exits non-zero without running anything and the row
# then reports a failure the merge did not cause. Measured 2026-09-20: a gate
# command whose file set was written as an angle-bracket description re-ran
# pytest against a path that does not exist, and the ledger recorded a finding
# against a clean node.
#
# Each shape is spelled so that a runnable command does not match it, because a
# refusal here blocks a promotion:
#
# * an angle-bracket placeholder requires a non-space at both inner edges, so
#   the shell's own `< file` and `> file` redirections, which carry a space in
#   the pair, are left alone;
# * an ellipsis is a whole token of its own, so `cd ..` and a quoted `'...'`
#   pass, while a bare `...` standing in for the rest of a file list does not;
# * a parenthetical group is judged only when it is whitespace-delimited on
#   both sides and reads as words — no shell operator and more than one word
#   inside — so `python -c "print(a, b)"`, a quoted `-k "(a or b)"` and the
#   genuine subshell `(cd sub && pytest)` all pass, while a parenthetical
#   selection written in prose does not.
_GATE_COMMAND_PLACEHOLDER = re.compile(r"<[^\s<>][^<>]*[^\s<>]>")
_GATE_COMMAND_ELLIPSIS = re.compile(r"(?:^|(?<=\s))\.\.\.(?=\s|$)|…")
_GATE_COMMAND_PARENTHETICAL = re.compile(r"(?:^|(?<=\s))\([^()]*\)(?=\s|$)")
_GATE_COMMAND_SHELL_OPERATORS = frozenset("&|;$><=*?!`\"'")


def gate_command_prose(command: str) -> tuple[str, str] | None:
    """The shape in a gate command that a shell cannot execute, or None.

    Returns the offending token class beside the token itself, so a refusal can
    name both. The classes are the three a description is written in: an
    angle-bracket placeholder, an ellipsis standing for the rest of a list, and
    a parenthetical selection written as prose. Anything else is admitted: this
    judges only the shapes that cannot run, never the spelling of a command
    that can, because a false refusal costs a coordinator a promotion.
    """
    text = str(command or "").strip()
    if not text:
        return None
    found = _GATE_COMMAND_PLACEHOLDER.search(text)
    if found is not None:
        return "angle-bracket placeholder", found.group(0)
    found = _GATE_COMMAND_ELLIPSIS.search(text)
    if found is not None:
        return "ellipsis", found.group(0).strip()
    for found in _GATE_COMMAND_PARENTHETICAL.finditer(text):
        group = found.group(0)
        inner = group[1:-1]
        if len(inner.split()) < 2:
            continue
        if any(character in inner for character in _GATE_COMMAND_SHELL_OPERATORS):
            continue
        return "parenthetical prose selection", group
    return None


def _require_runnable_gate_command(
    run_id: str,
    gate_check: Mapping[str, Any] | None,
) -> None:
    """Refuse a promotion that records a gate command nothing can re-execute.

    The command a promotion records is not narrative: the integration re-run
    executes it verbatim through a shell, whatever the verdict on the row, so a
    text that describes the check rather than running it makes the re-run
    report an invented failure. Refused here, before any store is written, so
    the record never holds a command that reads as evidence and executes as
    gibberish. A readable summary of the check belongs in the log header or in
    the outcome line, both of which are free text.
    """
    if not isinstance(gate_check, Mapping):
        return
    command = str(gate_check.get("command") or "").strip()
    prose = gate_command_prose(command)
    if prose is None:
        return
    token_class, token = prose
    raise CrewError(
        f"run {run_id!r} records the gate command {command!r}, which carries a "
        f"{token_class} ({token!r}): that describes the check rather than "
        "running it, so the integration re-run would execute the description, "
        "exit non-zero and record a failure the merge did not cause. Record the "
        "command itself — the literal file list, not a description of it — and "
        "put the readable summary in the gate log header or in --outcome"
    )


# A leading ``NAME=value`` token is a shell assignment rather than the program
# name, so the executable lookup starts at the first token that is not one.
_SHELL_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _require_executable_gate_command(
    run_id: str,
    gate_check: Mapping[str, Any] | None,
) -> None:
    """Refuse a recorded gate command whose first token names no executable.

    The recorded command is what the integration re-run executes, so it has to
    name a program that can start. The command is tokenised the way a shell
    would tokenise it, leading ``NAME=value`` assignments are skipped, and the
    first remaining token either carries a path separator — and must then be an
    existing executable file — or is looked up on ``PATH``. A command written
    as prose can satisfy every shape check that refuses placeholders, ellipses
    and prose parentheticals and still name no program at all, so the token
    itself is resolved here and a promotion recording such a command is refused
    before the row is written.

    Refusal reaches only a command whose program cannot be resolved from here:
    the lookup is the same ``PATH`` the promotion process runs under, and a
    command that resolves still promotes on whatever the other gate checks
    make of its evidence.
    """
    if not isinstance(gate_check, Mapping):
        return
    command = str(gate_check.get("command") or "").strip()
    if not command:
        return
    try:
        argv = shlex.split(command)
    except ValueError as unparseable:
        raise CrewError(
            f"run {run_id!r} records the gate command {command!r}, which does "
            f"not parse as a shell command ({unparseable}): the integration "
            "re-run would execute the text a shell cannot tokenise. Found: a "
            "command that does not parse. Record the literal command that ran, "
            "quoting its arguments, and put any readable summary in the gate "
            "log header or in --outcome"
        ) from unparseable
    while argv and _SHELL_ASSIGNMENT.match(argv[0]):
        argv = argv[1:]
    if not argv:
        raise CrewError(
            f"run {run_id!r} records the gate command {command!r}, which names "
            "no program: it carries only shell variable assignments. Found: a "
            "command whose first token names no executable. Record the literal "
            "command that ran, and put any readable summary in the gate log "
            "header or in --outcome"
        )
    program = argv[0]
    if "/" in program:
        candidate = Path(program).expanduser()
        try:
            runnable = candidate.is_file() and os.access(candidate, os.X_OK)
        except OSError:
            runnable = False
    else:
        runnable = shutil.which(program) is not None
    if runnable:
        return
    raise CrewError(
        f"run {run_id!r} records the gate command {command!r}, whose program "
        f"{program!r} names no executable — it is neither an existing "
        "executable file nor a command found on PATH, so the integration "
        "re-run would fail before running the check. Found: a first token that "
        "names no executable. Record the literal command that ran — the real "
        "program the check was started with — and put any readable summary in "
        "the gate log header or in --outcome"
    )


def _paths_differing_between(
    repository: Path,
    integrated: str,
    checkout: str,
    changed_paths: Sequence[str] | None,
) -> list[str] | None:
    """Return the run's changed paths whose content differs between two revisions.

    ``None`` reports the paths as unknown, from either route: a caller that
    states no paths cannot establish that the commits after the merge leave
    that work untouched, and a comparison that cannot be taken reports the same
    unknown rather than an empty result, because a failed probe establishes
    nothing. An empty list means the comparison ran and every stated path is
    identical between the two revisions, the condition under which a checkout
    past the merge may still be measured.
    """
    if changed_paths is None:
        return None
    paths = [str(path) for path in changed_paths if str(path).strip()]
    if not paths:
        return []
    probe = _git(
        repository,
        "diff",
        "--name-only",
        integrated,
        checkout,
        "--",
        *paths,
        check=False,
    )
    if probe.returncode:
        return None
    return [line.strip() for line in probe.stdout.splitlines() if line.strip()]


def _cited_changed_paths(repository: Path, commits: Any) -> tuple[str, ...] | None:
    """The repository paths a run's own cited commits changed, or None.

    Read from each cited commit's own diff against its first parent, in the
    checkout the re-run will measure, so a merge charges the paths it resolved
    rather than the branch's whole span. A citation that does not resolve
    leaves the paths unknown — the caller then refuses a checkout past the
    integrated revision rather than guessing which paths are safe to ignore.
    """
    revisions = [str(commit) for commit in (commits or ()) if str(commit).strip()]
    if not revisions:
        return None
    paths: list[str] = []
    seen: set[str] = set()
    for revision in revisions:
        probe = _git(
            repository, "diff", "--name-only", f"{revision}^", revision, check=False
        )
        if probe.returncode:
            return None
        for line in probe.stdout.splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    return tuple(paths)


def _merged_gate_finding(
    base_verdict: str,
    integrated_verdict: str,
    *,
    integrated_revision: str,
    exit_status: int | None,
    reason: str | None,
    failure_ids: Sequence[str] = (),
    timed_out: bool = False,
    replay_elapsed_seconds: float | None = None,
) -> dict[str, Any] | None:
    """The divergence a merge-time re-run exists to surface, or None.

    A finding exists exactly when a gate that passed at the worker's base is
    not passed by the tree that ships. A base that was not already green is
    never a finding — the merge cannot turn a red gate red — so the check
    cannot manufacture a merge finding where the worker's own gate was
    already failing. Anything other than ``passed`` at the integrated tree
    (failed, or a re-run that could not establish a pass) is surfaced, because
    a base-green gate the merged tree does not re-confirm is the silent gap
    this mechanism exists to close.

    ``failure_ids`` are the failing tests the replay's own log enumerated. They
    are carried beside the verdict when the log named any, so the coordinator
    the finding reaches can act on the divergence without reopening the log;
    an empty sequence writes no key, because a re-run whose output named no
    test id measured no id rather than a zero.

    A re-run stopped by its bound carries ``timed_out`` and the seconds it ran
    before it was stopped, so the finding states the timeout and its duration
    in fields rather than only inside the reason sentence.
    """
    if base_verdict != "passed" or integrated_verdict == "passed":
        return None
    finding: dict[str, Any] = {
        "base_verdict": "passed",
        "integrated_verdict": integrated_verdict,
        "integrated_revision": integrated_revision,
        "exit_status": exit_status,
        "reason": reason,
        "message": (
            f"the gate passed at the worker's base but reports "
            f"{integrated_verdict} on the integrated revision "
            f"{integrated_revision[:12]}: the merge changes what this node's "
            "gate proves, so a base-green run alone is not enough to push. "
            "Re-run the node's gate on the merged tree, land the code the "
            "merged tree requires, or record explicitly why this finding is "
            "accepted"
        ),
    }
    if failure_ids:
        finding["failure_ids"] = list(failure_ids)
    if timed_out:
        finding["timed_out"] = True
        if replay_elapsed_seconds is not None:
            finding["replay_elapsed_seconds"] = round(replay_elapsed_seconds, 2)
    return finding


def _recorded_worktree_roots(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The worker worktree roots a promoted row records, in the order written.

    Promotion releases the run's worktree but keeps the audit of what it
    released, so ``release.worktree_audit.worktrees[].path`` is the durable
    record of the directory a stored gate command was authored against. A row
    written before the audit existed carries no path at all, and a top-level
    ``worktree`` is read too so either shape reaches the rewrite.
    """
    roots: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in roots:
            roots.append(text)

    release = row.get("release")
    audit = release.get("worktree_audit") if isinstance(release, Mapping) else None
    if isinstance(audit, Mapping):
        for entry in audit.get("worktrees") or ():
            if isinstance(entry, Mapping):
                add(entry.get("path"))
    add(row.get("worktree"))
    return tuple(roots)


def _rewrite_worktree_roots(
    command: str,
    *,
    roots: Sequence[str | Path],
    checkout: Path,
) -> tuple[str, tuple[str, ...]]:
    """Rewrite recorded worker worktree roots in a command to the checkout.

    A stored gate command routinely pins the worker's own worktree: ``env -C
    <worktree>`` decides where the gate runs, and its test files are named by
    absolute path under that tree. Promotion releases the worktree, so a
    replay of the recorded text either cannot start — ``env`` reports a removed
    directory with status 125, which a reader takes for the gate failing — or,
    while the tree survives, measures a stale copy of the repository instead of
    the tree that ships. Both are the wrong tree, so every recorded root is
    rewritten to the checkout being replayed.

    A match is taken only at a whole path segment: the character after the root
    must end the token or be a separator, so a root that is a string prefix of
    a longer path is left alone. A root that already resolves to the checkout
    is skipped, so a re-run against the tree the gate ran in is byte-identical
    to the recorded text. Returns the command to execute beside the roots the
    rewrite actually replaced.
    """
    rewritten: list[str] = []
    text = str(command or "")
    checkout_text = str(checkout)
    for raw in roots:
        candidate = str(raw or "").strip().rstrip("/")
        if not candidate:
            continue
        try:
            already = Path(candidate).resolve() == checkout
        except (OSError, RuntimeError, ValueError):
            already = False
        if already:
            continue
        pattern = re.compile(re.escape(candidate) + r"(?=$|[/\s'\"])")
        text, replacements = pattern.subn(checkout_text, text)
        if replacements:
            rewritten.append(candidate)
    return text, tuple(rewritten)


def _recorded_gate_seconds(
    run_id: str, gate_check: Mapping[str, Any] | None
) -> float | None:
    """The duration the promoted run's own gate log records, in seconds, or None.

    The log is read from the row's cited path when that resolves, and from the
    copy promotion preserves under the run directory beside it otherwise, so a
    run promoted on a machine that released the worktree still carries a
    duration. A log that is absent, unreadable, or carries no runner duration
    reports None rather than a number, and None keeps the fixed default bound:
    an unmeasured duration is not a zero, and a zero would derive a bound no
    gate could meet.
    """
    candidates: list[Path] = []
    if isinstance(gate_check, Mapping):
        raw = str(gate_check.get("log_path") or "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    candidates.append(run_dir(run_id) / _PRESERVED_GATE_LOG_NAME)
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches = _GATE_LOG_DURATION.findall(text)
        if matches:
            try:
                return float(matches[-1])
            except ValueError:
                continue
    return None


def _replay_bound(
    timeout_seconds: float | None,
    recorded_gate_seconds: float | None,
) -> tuple[float, str]:
    """The bound a re-run executes under, and which input set it.

    An explicit timeout is taken as given: a caller who names a bound has
    judged the gate for itself. Otherwise a recorded gate duration derives one
    with headroom, held between the historical default and the ceiling, and a
    run whose log records no duration keeps the default. The source is
    returned beside the value so the recorded report can say which of the
    three applied: a bound a reader cannot trace to an input is a number they
    cannot act on when a replay times out.
    """
    if timeout_seconds is not None:
        return float(timeout_seconds), "explicit"
    if recorded_gate_seconds is not None and recorded_gate_seconds > 0:
        derived = recorded_gate_seconds * _REPLAY_BOUND_HEADROOM
        clamped = min(
            max(derived, _REPLAY_BOUND_DEFAULT_SECONDS),
            _REPLAY_BOUND_CEILING_SECONDS,
        )
        return clamped, "derived"
    return _REPLAY_BOUND_DEFAULT_SECONDS, "default"


def _replay_cut_short_reason(
    *,
    bound_seconds: float,
    bound_source: str,
    elapsed_seconds: float,
) -> str:
    """The statement a re-run stopped by its bound leaves behind.

    It names the bound, which input set it, and the seconds the replay ran
    before it was stopped, so the same sentence serves the report's ``reason``,
    the finding, and the log's cut-short line: a reader who has only the log can
    tell a stopped replay from one that ran and printed nothing.
    """
    origin = {
        "explicit": "named by the caller",
        "derived": "derived from the run's recorded gate duration",
        "default": "the default, as no gate duration is recorded",
    }[bound_source]
    return (
        f"the gate did not finish within the {bound_seconds:g}s re-run bound "
        f"({origin}) and was stopped after {elapsed_seconds:.2f}s"
    )


def rerun_gate_at_integrated_revision(
    *,
    repository: Path,
    gate_check: Mapping[str, Any] | None,
    base_verdict: str = "passed",
    integrated_revision: str = "HEAD",
    timeout_seconds: float | None = None,
    command: str | None = None,
    changed_paths: Sequence[str] | None = None,
    worktree_roots: Sequence[str | Path] = (),
    replay_log_path: str | Path | None = None,
    recorded_gate_seconds: float | None = None,
) -> dict[str, Any]:
    """Re-run one gate against the tree that ships, and compare its verdict.

    A worker's gate runs against the base revision its worktree branched from,
    so a coordinator may name the merged revision while the checkout has moved
    on past it: bookkeeping commits land on the branch, and refusing every
    checkout that is not exactly the integrated revision sends the coordinator
    to re-check-out a tree the gate does not depend on. The re-run therefore
    also accepts a checkout the integrated revision is an ancestor of, but only
    when none of the run's own changed paths differs between the two — the
    extra commits are then unable to change what the gate measures. A checkout
    whose extra commits touch one of those paths is refused, and a checkout
    whose paths cannot be compared at all is refused too — whether the run
    states no paths or the comparison itself fails — because an unknown scope
    is not an empty one. ``changed_paths_differing`` reports the comparison's
    result: the differing paths, an empty list for a comparison that ran and
    found none, and null for one that was never taken.

    The command is executed only when the repository's tree is acceptable: a
    run against any other tree verifies the wrong tree, so it never executes
    and the reason is stated. The base verdict is taken as given — it is the
    gate the run already recorded, which this check tests rather than
    re-creates.

    A gate that did not run, or did not finish within the bound, is reported
    as ``not-run`` with its reason, never as passed: an unmeasured re-run must
    not read as a verified one.

    ``ok`` is true only when the re-run finished and passed. A re-run that
    exits by timeout, or otherwise ends without a status, reports ``ok: false``
    with a finding naming the timeout and the seconds it ran before it was cut
    short, so the one field a caller reads first cannot call an unmeasured
    re-run a success. Its log carries a ``# cut short:`` line saying the bound
    stopped it rather than ending on header lines a reader takes for a gate
    that printed nothing. A re-run that completes keeps its exit status,
    verdict and finding unchanged, and reports ``ok: false`` when that verdict
    is failed, so the first field a caller reads answers whether the merged
    tree passed rather than only whether the command returned.

    The bound the re-run executes under resolves from three inputs, and the
    report names which applied and its value: an explicit ``timeout_seconds``
    as given (``explicit``); otherwise the duration in
    ``recorded_gate_seconds``, the run's own gate log read by the caller, with
    headroom and held between the historical default and a ceiling
    (``derived``); otherwise the historical default (``default``). A run whose
    gate legitimately took longer than the default is therefore replayed
    under a bound that fits it rather than reported ``not-run`` forever.

    An explicit ``command`` is run in place of the run's stored gate command,
    so a caller can measure a wider suite — a whole-repository one — than the
    node's own gate, and can do so on a run that stored none. The report names
    the command actually executed and whether it came from the option or the
    stored row, so a reader can tell a supplied command from the recorded one.

    A recorded command routinely pins the worker's own worktree — ``env -C
    <worktree>`` decides where the gate runs and its test files are named by
    absolute path under that tree — and promotion releases that worktree, so
    replaying the text verbatim either cannot start (``env`` reports a removed
    directory with status 125, which a reader takes for the gate failing) or
    measures a stale copy of the repository instead of the tree that ships.
    Each root in ``worktree_roots`` is therefore rewritten to the checkout
    being replayed before the command executes, and ``worktree_roots_rewritten``
    names the ones the rewrite replaced.

    The re-run's own output is kept when ``replay_log_path`` is given: the
    command that ran, the captured stdout and stderr, and the exit status go to
    that path under a header naming the revision, the tree, and the command,
    and ``log_path`` cites the file the text landed in. A replay whose output
    was discarded could only be read as an exit status — measured 2026-09-28, a
    replayed gate recorded exit 1 and neither the failing test ids nor the
    runner's stderr survived anywhere.
    """
    base = str(base_verdict).strip().lower()
    if base not in ledger.GATE_VERDICTS:
        raise CrewError(
            f"base gate verdict {base_verdict!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; the base verdict is the gate "
            "the run already recorded, which this re-run is compared against"
        )
    stored_command = str((gate_check or {}).get("command") or "").strip()
    bound_seconds, bound_source = _replay_bound(timeout_seconds, recorded_gate_seconds)
    supplied = str(command or "").strip()
    command = supplied or stored_command
    command_source = "option" if supplied else ("stored" if stored_command else None)
    integrated = _commit_canonical_id(repository, str(integrated_revision))
    checkout = _commit_canonical_id(repository, "HEAD")
    on_integrated = bool(integrated and checkout and integrated == checkout)
    descends = bool(
        not on_integrated
        and integrated
        and checkout
        and _revision_is_ancestor(repository, integrated, checkout)
    )
    differing = (
        _paths_differing_between(repository, integrated, checkout, changed_paths)
        if descends
        else None
    )
    report: dict[str, Any] = {
        "base_verdict": base,
        "integrated_verdict": "not-run",
        "integrated_revision": integrated or str(integrated_revision),
        "checkout_revision": checkout or "",
        "checkout_on_integrated_revision": on_integrated,
        "checkout_descends_from_integrated_revision": descends,
        "changed_paths_differing": list(differing) if differing is not None else None,
        "gate_command": command or None,
        "gate_command_source": command_source,
        "replay_bound_source": bound_source,
        "replay_bound_seconds": bound_seconds,
        "recorded_gate_seconds": recorded_gate_seconds,
        "ran": False,
        "exit_status": None,
        "timed_out": False,
        "replay_elapsed_seconds": None,
        "ok": True,
        "log_path": None,
        "worktree_roots_rewritten": [],
        "reason": None,
        "finding": None,
    }
    replay_text: str | None = None
    prose = gate_command_prose(command)
    if not command:
        reason = "no gate command is stored to re-run"
    elif prose is not None:
        token_class, token = prose
        reason = (
            f"the gate command {command!r} carries a {token_class} ({token!r}), "
            "so it describes the check rather than running it. A shell cannot "
            "execute the description, and executing it would report a failure "
            "the integrated revision did not cause"
        )
    elif integrated is None:
        reason = (
            f"integrated revision {integrated_revision!r} does not resolve to "
            "a commit in the repository"
        )
    elif not checkout:
        reason = "the repository has no resolvable HEAD to run the gate against"
    elif not on_integrated and not descends:
        reason = (
            f"the checkout is at {checkout[:12]}, not the integrated revision "
            f"{integrated[:12]}, and does not descend from it: a gate run here "
            "would verify the wrong tree. Check the integrated revision out, "
            "or name the revision the checkout actually carries"
        )
    elif not on_integrated and differing is None:
        reason = (
            f"the checkout is at {checkout[:12]}, past the integrated revision "
            f"{integrated[:12]}, and the run's changed paths could not be "
            "compared between the two: either the run does not state what it "
            "changed or the comparison could not be taken, and an unknown "
            "comparison is not an empty one, so nothing establishes that the "
            "commits after the merge leave what the gate measures untouched. A "
            "gate run here would verify the wrong tree. Check the integrated "
            "revision out, or state the paths the run changed"
        )
    elif not on_integrated and differing:
        reason = (
            f"the checkout is at {checkout[:12]}, past the integrated revision "
            f"{integrated[:12]}, and the commits between them change "
            + ", ".join(differing[:5])
            + ": a gate run here would verify the wrong tree, because this "
            "run's merge moved past paths the gate measures. Check the "
            "integrated revision out, or re-run the gate on a checkout whose "
            "changed paths are unchanged"
        )
    else:
        reason = None
        checkout_root = Path(repository).expanduser().resolve()
        replayed, rewritten_roots = _rewrite_worktree_roots(
            command, roots=worktree_roots, checkout=checkout_root
        )
        report["worktree_roots_rewritten"] = list(rewritten_roots)
        header = _replay_log_header(
            replay_command=replayed,
            checkout=checkout_root,
            checkout_revision=report["checkout_revision"],
            integrated_revision=report["integrated_revision"],
            rewritten_roots=rewritten_roots,
        )
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["sh", "-c", replayed],
                cwd=str(repository),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=bound_seconds,
            )
        except subprocess.TimeoutExpired as expired:
            elapsed = time.monotonic() - started
            report.update(ran=True, timed_out=True, replay_elapsed_seconds=elapsed)
            partial = expired.output if isinstance(expired.output, str) else ""
            written = _write_replay_log(
                replay_log_path,
                header=header,
                output=partial,
                exit_status=None,
                cut_short=_replay_cut_short_reason(
                    bound_seconds=bound_seconds,
                    bound_source=bound_source,
                    elapsed_seconds=elapsed,
                ),
            )
            if written is not None:
                report["log_path"] = str(written)
            replay_text = partial
        else:
            report["ran"] = True
            report["exit_status"] = result.returncode
            report["integrated_verdict"] = (
                "passed" if result.returncode == 0 else "failed"
            )
            replay_text = result.stdout or ""
            written = _write_replay_log(
                replay_log_path,
                header=header,
                output=replay_text,
                exit_status=result.returncode,
            )
            if written is not None:
                report["log_path"] = str(written)
    if reason is not None:
        report["reason"] = reason
    elif report["timed_out"]:
        report["reason"] = _replay_cut_short_reason(
            bound_seconds=bound_seconds,
            bound_source=bound_source,
            elapsed_seconds=report["replay_elapsed_seconds"],
        )
    report["finding"] = _merged_gate_finding(
        report["base_verdict"],
        report["integrated_verdict"],
        integrated_revision=report["integrated_revision"],
        exit_status=report["exit_status"],
        reason=report["reason"],
        failure_ids=tuple(sorted(_control_failure_ids(replay_text)))
        if replay_text
        else (),
        timed_out=report["timed_out"],
        replay_elapsed_seconds=report["replay_elapsed_seconds"],
    )
    # ``ok`` reads the verdict, not the measurement: only a replay that finished
    # and passed is a success. A replay cut short by the bound, or one that never
    # started, produced no status; one that completed failing carries a red
    # verdict. A caller taking ok at its word on either would conclude the
    # opposite of the fact.
    report["ok"] = report["integrated_verdict"] == "passed"
    return report


def record_gate_rerun_at_integrated_revision(
    *,
    project: str,
    run_id: str,
    repository: Path,
    integrated_revision: str = "HEAD",
    timeout_seconds: float | None = None,
    root: str | Path | None = None,
    command: str | None = None,
) -> dict[str, Any]:
    """Production caller: re-run a run's gate at the integrated revision and record it.

    A run's gate is measured at the base its worktree branched from, so a
    contract that lands after that base never binds the run; only the merged
    tree can tell whether a base-green gate still holds. This is the surface a
    coordinator reaches after merging: it reads the run's stored gate command
    and base verdict from its committed ledger row, re-runs that command
    against the merged checkout, writes the full re-run report back onto the
    run's ledger row (finding present or absent, so a reader sees the merged
    tree was re-checked either way), keeps the shadow store in agreement, and
    commits the edit in one landing.

    A ``command`` supplied here replaces the stored gate command for the
    re-run, so a coordinator can measure a suite wider than the node's own
    gate — or measure a run that stored no gate command at all — against the
    merged head. The report records which command ran and whether it came from
    the option or the stored row. An omitted ``timeout_seconds`` derives the
    re-run bound from the duration the run's own gate log records, so a gate
    that legitimately ran longer than the fixed default is replayed under a
    bound that fits it; an explicit value is taken as given. Either way the
    report records which bound applied and its value. A stored command that pins the worker's
    released worktree has that root rewritten to the checkout being replayed,
    so a gate that ran correctly in its worker's tree is not recorded as
    failing here because the directory it named is gone. The re-run's captured
    output is written under this run's directory beside the preserved gate log
    and its path is recorded on the report, so a failure carries the text that
    explains it rather than only an exit status. The payload's ``ok`` reports
    whether the re-run finished and passed, so a replay cut short by its bound
    and a gate the merged tree fails both reach a coordinator as failures
    rather than as successes.
    """
    checkout = Path(repository).expanduser().resolve()
    ledger_root = root if root is not None else checkout
    probe = _git(checkout, "rev-parse", "--is-inside-work-tree", check=False)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise CrewError(
            f"run {run_id!r} gate cannot be re-run at the integrated revision: "
            f"{checkout} is not a git worktree, so the recorded report could not "
            "be committed; run `reckon crew verify-gate` against a checkout that "
            "is a git worktree"
        )
    data, version = ledger.load(project, root=ledger_root)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id") or "") == run_id),
        None,
    )
    if row is None:
        raise CrewError(
            f"run {run_id!r} has no row in the {project!r} ledger, so its gate "
            "cannot be re-run at the integrated revision; a promoted run's gate "
            "comes from its committed row. Run `reckon crew complete --run "
            f"{run_id}` to promote it first, then rerun this"
        )
    stored_gate_check = row.get("gate_check")
    recorded_gate_seconds = _recorded_gate_seconds(
        run_id,
        stored_gate_check if isinstance(stored_gate_check, Mapping) else None,
    )
    report = rerun_gate_at_integrated_revision(
        repository=checkout,
        gate_check=stored_gate_check if isinstance(stored_gate_check, Mapping) else None,
        base_verdict=str(row.get("gate") or "passed"),
        integrated_revision=integrated_revision,
        timeout_seconds=timeout_seconds,
        command=command,
        changed_paths=_cited_changed_paths(checkout, row.get("commits")),
        worktree_roots=_recorded_worktree_roots(row),
        replay_log_path=run_dir(run_id) / _REPLAY_GATE_LOG_NAME,
        recorded_gate_seconds=recorded_gate_seconds,
    )
    record_path = ledger.run_path(project, run_id, ledger_root)
    if record_path.is_file():
        _update_run_record(record_path, run_id, {"integrated_gate_check": report})
        new_version = None
    else:
        record_path = ledger.ledger_path(project, ledger_root)
        patched = [dict(item) for item in data["runs"]]
        for index, item in enumerate(patched):
            if str(item.get("run_id") or "") == run_id:
                patched[index]["integrated_gate_check"] = report
                break
        new_version = ledger.write(
            project, {**data, "runs": patched}, version, ledger_root
        )
    from reckon import run_store

    store_synopsis = run_store.import_ledger(project, root=ledger_root)
    landing = _commit_landing_writes(
        run_id=run_id,
        verdict=str(report.get("integrated_verdict") or "not-run"),
        checkout=checkout,
        paths=[record_path],
        subject=f"record({run_id}): re-run gate at integrated {integrated_revision}",
        body=(
            "Re-run the run's stored gate command against the merged tree and "
            "record the report on its ledger row, so a gate the integrated "
            "revision no longer satisfies is recorded against the run rather "
            "than only printed."
        ),
    )
    return {
        "run_id": run_id,
        "project": project,
        "ledger_path": str(record_path),
        "ledger_version": new_version,
        "checkout_on_integrated_revision": report.get(
            "checkout_on_integrated_revision"
        ),
        "checkout_revision": report.get("checkout_revision"),
        "report": report,
        # The CLI publishes this payload behind its own success flag, so a
        # replay that measured nothing must carry the failure here or the
        # caller reads ok on a check that never produced a status.
        "ok": report["ok"],
        "finding": report.get("finding"),
        "landing": landing,
        "store_synopsis": store_synopsis,
    }


# A value of nothing but zeros names the null object id, which resolves to no
# commit in any store, so it states the count of commits rather than citing one.
_ZERO_COUNT = re.compile(r"0+")


def _manifest_cites_a_commit(
    manifest: Mapping[str, Any], record: Mapping[str, Any], manifest_text: str
) -> bool:
    """Whether a manifest's ``commits`` field cites at least one commit.

    The field is free text a worker wrote, and a run with nothing to commit
    writes a sentence that opens with the declaration word ``none`` — the shape
    a review delivers. Such a line is not a citation, so it must not answer the
    commit-for-changed-manifest guard's question: a manifest that names an
    in-repository path needs the commit that contains it, and a declared absence
    over one is a contradiction the guard must read as the missing commit it is.
    An entry that names a commit is a citation; a declared absence is not, and
    any other value counts as a citation, so an unrecognised spelling is refused
    rather than silently dropped. A commitless role's raw-field declaration is
    honoured first, so a sentence whose commas split it into several entries — or
    whose declaration word the list reader empties — is still read as the single
    absence it is rather than as a citation list.

    This is the one reader of the question: the write-time audit and the guard
    both ask it here, so the field cannot answer "cites" at the gate and "does
    not" at check-manifest. An entry of nothing but zeros is the count of
    commits written into a citation field, and the null object id it spells
    resolves to no commit in any store, so it cites nothing wherever this is
    asked. A value that reaches this reader structured rather than as a list
    carries no entry and cites nothing.
    """
    if _commits_field_declares_absence(manifest_text, record):
        return False
    commits = manifest.get("commits") or ()
    if not isinstance(commits, (list, tuple)):
        return False
    return any(
        entry
        and not _declares_absent_commits(entry)
        and not _ZERO_COUNT.fullmatch(entry)
        for entry in (str(item).strip() for item in commits)
    )


def _prose_changed_paths_name_no_paths(manifest: Mapping[str, Any]) -> bool:
    """True when the manifest's changed_paths declare none in prose.

    A report-only manifest states ``changed_paths: none under the repository;
    the sole deliverable is the report`` as free text, and the manifest parser
    keeps the whole sentence as the field's single item. Like the bare token
    ``none``, which the parser's none-values set already empties, prose that
    opens with the word ``none`` and continues declares no repository paths —
    so the commit-for-changed-manifest guard must not read a path out of it.
    Only the word ``none`` followed by more text counts: a real path that
    merely begins with those letters is untouched, and a list that still names
    a real path is not prose-none.
    """
    items = [str(item).strip() for item in (manifest.get("changed_paths") or ())]
    return bool(items) and all(
        bool(re.match(r"none(?:\s|$)", item, re.IGNORECASE)) for item in items
    )


def _changed_paths_declare_no_paths(
    manifest: Mapping[str, Any], record: Mapping[str, Any], manifest_text: str
) -> bool:
    """Whether a manifest's ``changed_paths`` field claims no repository paths.

    One rule, and it reads the claim rather than deciding the question. A field
    the reader finds empty claims nothing: no value at all, or the bare ``none``
    the parser's none-values set strips. A prose sentence that opens with the
    declaration word claims none for any role, which is the shape a report-only
    node writes. A commitless role's raw value is read beside the parsed list, on
    the same word boundary the ``commits`` field uses, so a value whose commas the
    list reader splits still declares what it opens with.

    This says only what the run says about itself. Whether the claim is true is
    not a matter of prose shape -- ``none.txt`` is a filename as plausibly as
    ``none`` is a declaration, and each narrower boundary tried here was answered
    by the next prose shape that spoofed it -- so for a commitless role it is
    settled from the worktree by ``_worktree_repository_changes`` before this is
    consulted.
    """
    if not manifest.get("changed_paths"):
        return True
    if _prose_changed_paths_name_no_paths(manifest):
        return True
    return _commitless_changed_paths_declares_absence(manifest_text, record)


def _changed_paths_inside_repository(
    manifest: Mapping[str, Any], record: Mapping[str, Any]
) -> tuple[str, ...]:
    """The manifest's changed paths that resolve under the run's own repository.

    A report-only run delivers its artefact outside every repository — a parsed
    review JSON under the crew reviews directory, a scratch file beside it — and
    its manifest names those paths while citing no commit, because there is
    nothing inside a repository to commit. So the commit-for-changed-manifest
    guard's real question is not whether ``changed_paths`` names anything, but
    whether any named path lies in *this run's* repository and so needs the
    commit that contains it. Asking the narrower question is what lets a
    finished run that has no commit to give be reconciled.

    A relative path is repository-relative by the manifest's own convention, so
    a relative path resolves against the repository root; an absolute path
    resolves as written. A path anywhere else — a store directory, a scratch
    path under the crew home — is outside and requires no commit here. When the
    run records no repository to resolve against, every named path is returned,
    which is the guard's prior behaviour.
    """
    items = [str(item).strip() for item in (manifest.get("changed_paths") or ())]
    root = str(record.get("repo") or record.get("worktree") or "").strip()
    if not root:
        return tuple(items)
    try:
        base = Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        return tuple(items)
    inside: list[str] = []
    for item in items:
        raw = Path(item).expanduser()
        if raw.is_absolute():
            try:
                resolved = raw.resolve()
            except (OSError, RuntimeError):
                resolved = raw
            try:
                resolved.relative_to(base)
            except ValueError:
                continue
            inside.append(item)
        elif ".." in raw.parts:
            continue
        else:
            inside.append(item)
    return tuple(inside)


def _require_commit_for_changed_manifest(
    run_id: str, record: Mapping[str, Any], *, no_commit_reason: str = ""
) -> tuple[str, ...]:
    """Refuse a run whose claim and worktree disagree about a repository change.

    Two questions, one rule each. For a role that carries no repository work —
    review or investigate — the declaring field is not evidence, because no shape
    in a free-text field separates a filename from a declaration: ``none.txt`` is
    a path and ``none, no repository change`` opens with the declaration word, and
    a guard that reads the prose can be walked around by writing the next shape
    that looks like both. So a commitless role may promote with no commits only
    when its own worktree shows no repository change against the run's recorded
    base sha, measured by ``_worktree_repository_changes``; a worktree holding
    work and a manifest that cites no commit is refused, and the refusal names
    the paths git found. The declared fields still say whether the run *claims*
    no change, and that claim is what the second question reads.

    A reason passed as ``no_commit_reason`` is the one thing that overrides the
    measurement: it is a coordinator's deliberate, auditable declaration that
    the worktree's changes are not being recorded as a commit, and the paths it
    covers are returned for the caller to record on the ledger beside the
    reason. No text a worker writes can do this — the manifest is never the
    authority — so a worktree read without that reason is refused.

    The second question is the older one and is unchanged: the manifest names a
    changed path that resolves under the run's own repository, so it needs the
    commit that contains it, and the field is empty or declares an absence. A run
    whose changed_paths lie entirely outside the repository — a report delivered
    to the crew reviews directory — promotes without a commit, which is its
    correct disposition, and a ``commits`` line that declares an absence is not a
    citation, so it does not answer the question either.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return ()
    try:
        manifest_text = Path(str(record["manifest_path"])).read_text(encoding="utf-8")
        manifest = parse_manifest(manifest_text)
    except (OSError, KeyError, ValueError):
        return ()
    if str(manifest.get("status") or "").strip().lower() != "complete":
        return ()
    cites_commit = _manifest_cites_a_commit(manifest, record, manifest_text)
    from reckon.crew.recovery import _pointer_role

    role = _pointer_role(record)
    if role in _COMMITLESS_ROLES and not cites_commit:
        changed = _worktree_repository_changes(record)
        if changed and str(no_commit_reason).strip():
            return changed
        if changed:
            raise CrewError(
                f"run {run_id!r} is a {role} run whose worktree has repository "
                "changes against its base, but its manifest cites no commit. "
                "Changed paths: " + ", ".join(changed) + ". A commitless role may "
                "promote with no commits only when its worktree shows no "
                "repository change: no commits beyond the base, no tracked "
                "modification, and no untracked repository file apart from the "
                "provisioned .venv symlink. Cite the commit that contains these "
                "paths, or pass --no-commit '<why>' to record deliberately that "
                "they are not being registered"
            )
    if (
        _changed_paths_declare_no_paths(manifest, record, manifest_text)
        or cites_commit
        or not _changed_paths_inside_repository(manifest, record)
    ):
        return ()
    raise CrewError(
        f"run {run_id!r} has a complete manifest with changed_paths, but the "
        "manifest field 'commits' is missing. Promotion cannot verify changed "
        "repository paths without the commit that contains them"
    )


def _resolve_commits(*, cwd: Path, revisions: Iterable[str], run_id: str) -> list[str]:
    """Resolve every recorded revision to its canonical commit object id."""
    commits = []
    for revision in revisions:
        commit = _resolve_commit(cwd, revision)
        if not commit:
            # A commit that resolves somewhere else is not a bad sha, it is a
            # node whose write target was not its run repository — dispatched
            # without --repo and pointed at a foreign checkout by prose. Naming
            # the repository it does belong to turns a correct-but-opaque
            # refusal into the instruction for the next dispatch.
            elsewhere = _foreign_repository(revision, exclude=cwd)
            remedy = (
                f"it resolves in {elsewhere} instead, so that node belongs to "
                f"that repository: redispatch it with `--repo {elsewhere}` "
                "rather than granting a foreign checkout in its prose"
                if elsewhere is not None
                else "check that the worker committed rather than only staging, "
                "and that it committed in its own worktree"
            )
            raise CrewError(
                f"run {run_id!r} commit {revision!r} does not resolve to a "
                f"commit object in the run repository ({cwd}); {remedy}"
            )
        commits.append(commit)
    return commits


def _promoted_revision(run_tree: Path, commit_list: Sequence[str]) -> str:
    """Resolve the revision a promotion asserts landed, or the empty string.

    The revision is the tip of the run's own work, so a later sweep can ask two
    questions of the branch it was promoted into: whether this commit is an
    ancestor of it, and whether a marker the change introduced survives there.
    A promotion also commits its own ledger row and plan comment into that
    branch, so recording the commit the promotion makes instead would make the
    ancestry question true by construction and unable to fail for any reason.

    The revision is the presented commit that descends from all the others, so
    the order a manifest lists its commits in never decides which revision the
    run's work reached. The cited commits are preferred over the worktree's
    ``HEAD`` because a shared checkout can advance under other runs between the
    commit and the promotion, and a cited commit is the revision whose diff
    promotion already measured.
    """
    if commit_list:
        return _descendant_commit(run_tree, commit_list)
    head = _commit_canonical_id(run_tree, "HEAD")
    return head or ""


def _descendant_commit(run_tree: Path, presented: Sequence[str]) -> str:
    """The presented commit that descends from all the presented commits.

    A run's manifest lists the commits it made in whatever order its worker
    wrote them, and a promotion that read a position out of that list promoted
    a revision the run never reached: the newest-first order prints the tip
    first, so the last position named the run's first commit and a promotion
    following the printed order was refused against a mis-selected revision.

    Each entry is resolved to the canonical object id its spelling names, so an
    abbreviation, a full sha and a branch name for one commit collapse to one
    candidate. An entry naming no commit cannot be placed in the history, so it
    takes no part in the comparison and is left for the citation check that
    reports it. The remaining candidates are compared with git, and the one
    that every other candidate is an ancestor of is the run's tip. A set whose
    commits have no such single member is refused, naming every presented
    commit, because the revision the run's work reached cannot be told from the
    list and any pick would be a guess recorded as the promoted revision.
    """
    entries: list[str] = []
    resolvable: list[str] = []
    seen: set[str] = set()
    for entry in presented:
        text = str(entry).strip()
        if not text:
            continue
        canonical = _commit_canonical_id(run_tree, text)
        value = canonical or text
        if value in seen:
            continue
        seen.add(value)
        entries.append(value)
        if canonical:
            resolvable.append(value)
    if len(entries) == 1:
        return entries[0]
    if not resolvable:
        return entries[-1]
    descendants = [
        candidate
        for candidate in resolvable
        if all(
            other == candidate or _revision_is_ancestor(run_tree, other, candidate)
            for other in resolvable
        )
    ]
    if len(descendants) == 1:
        return descendants[0]
    listed = ", ".join(entries)
    raise CrewError(
        "the commits presented for this promotion have no single descendant: "
        f"{listed}; none of them descends from all the others, so the revision "
        "the run's work reached cannot be told from the list. Present the "
        "commit the run's work ended at"
    )


def _repository_scope_paths(
    declared_paths: Iterable[str], *, worktree: Path, repository: Path
) -> tuple[Path, ...]:
    """Map declared paths into repository-relative roots when possible."""
    roots: list[Path] = []
    for declared in declared_paths:
        raw = Path(str(declared)).expanduser()
        if raw.is_absolute():
            relative = None
            for base in (worktree, repository):
                try:
                    relative = raw.resolve().relative_to(base.resolve())
                    break
                except ValueError:
                    continue
            if relative is None:
                continue
        else:
            relative = raw
        if relative.is_absolute() or ".." in relative.parts:
            continue
        roots.append(relative)
    return tuple(roots)


def _merge_revisions(cwd: Path, revisions: Iterable[str]) -> list[str]:
    """Return the cited revisions that are merges rather than a worker's commit."""
    merges = []
    for revision in revisions:
        parents = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", revision],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if parents.returncode:
            continue
        if len(parents.stdout.split()) > 2:
            merges.append(revision)
    return merges


def _scope_worktree(record: Mapping[str, Any], tree: Path) -> Path:
    """Return the worktree a declared write path resolves against.

    ``tree`` is the caller's readable tree, which falls back to the repository
    once the run's worktree directory has been reclaimed — correct for reading
    a tree, wrong for resolving a declaration. An absolute write path dispatch
    granted under that worktree still names a location inside the repository,
    and the write-time audit resolves it against the recorded worktree, which
    never falls back. Resolving against the repository instead loses the
    mapping, so the same declaration reads as stray at promotion and in scope
    at the write-time check. The recorded worktree is the authority here
    whether or not it still exists on disk.
    """
    return Path(str(record.get("worktree") or tree))


def _outside_declared_scope(
    changed_paths: Iterable[str],
    declared_paths: Iterable[str],
    *,
    record: Mapping[str, Any],
    tree: Path,
) -> tuple[str, ...]:
    """Return changed paths the run's declared write scope does not contain.

    One contract, one implementation: this delegates to the write-time audit's
    own test rather than repeating its containment rule, so a manifest a worker
    was told passes is a manifest promotion accepts. Delegating also carries two
    declarations the containment rule alone dropped. A declaration naming a
    location outside the repository resolves to no repository-relative root, and
    is compared as the absolute path it already is, so it still decides what it
    grants — a review's store record is the real case, since its deliverable is
    written beside the run it read rather than inside the repository. And the
    revision-keyed record written beside a declared store path is that same
    deliverable under another name.

    The worktree the declaration is resolved against is the run's recorded one,
    not the caller's readable tree: a reclaimed worktree leaves the record as
    the only place the resolution can be made, and resolving against the
    repository instead loses the mapping for an absolute grant made under it.
    """
    worktree = _scope_worktree(record, tree)
    repository = Path(str(record.get("repo") or tree))
    declarations = tuple(str(path) for path in declared_paths)
    return tuple(
        str(changed)
        for changed in changed_paths
        if not path_within_declared_scope(
            changed, declarations, worktree=worktree, repository=repository
        )
    )


def _path_change_reaches_integration_head(
    repository: Path,
    commits: Sequence[str],
    path: str,
) -> bool:
    """Report whether the run's change to ``path`` is behind the integration head.

    The integration head is the repository's own HEAD — the branch the landing
    checkout carries, the same reading recovery uses when it asks whether a
    run's commits are on the primary branch. The run's change to the path is
    read from each cited commit's own diff against its first parent, the
    instrument ``_committed_scope`` charges the run by, so a cited merge counts
    the content it brought rather than what it resolved to. Every cited commit
    that changed the path must be strictly behind the head: a cited commit that
    is the head itself was integrated by no later commit, so the tip a live
    peer's claim races is the run's own, and the claim still refuses. A path no
    cited commit changed admits nothing here, because the run's change to it
    cannot be read from the citations the promotion presents.
    """
    touched = False
    for commit in commits:
        changed = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--no-renames",
                f"{commit}^1",
                commit,
                "--",
                path,
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        if changed.returncode:
            raise CrewError(
                f"git could not read the change {commit} made to {path} in "
                f"{repository}: "
                f"{changed.stderr.strip() or changed.stdout.strip() or changed.returncode}"
            )
        if not changed.stdout.strip():
            continue
        touched = True
        if not _revision_is_ancestor(repository, commit, "HEAD"):
            return False
        if _revision_is_ancestor(repository, "HEAD", commit):
            return False
    return touched


def _accepted_scope_exceptions(
    run_id: str,
    outside: Iterable[str],
    accepted_paths: Mapping[str, str] | None,
    *,
    record: Mapping[str, Any],
    tree: Path,
    commits: Sequence[str],
) -> list[dict[str, Any]]:
    """Validate deliberate companion paths and return their durable account.

    A live run's claim refuses an acceptance only while it protects something:
    a path whose change by this run is already behind the integration head is
    admitted despite the claim, because what the refusal blocks there is the
    ledger record of a landing that has happened, not a concurrent edit. The
    peer's claim and pointer are left untouched.
    """
    outside_paths = tuple(str(path) for path in outside)
    supplied = accepted_paths or {}
    if not supplied:
        raise CrewError(
            f"run {run_id!r} changed paths outside its declared "
            f"write scope: {', '.join(outside_paths)}"
        )
    if not isinstance(supplied, Mapping):
        raise CrewError("accepted paths must map each repository path to its reason")

    repository = Path(str(record.get("repo") or tree)).resolve()
    normalized: dict[str, str] = {}
    for raw_path, raw_reason in supplied.items():
        roots = _repository_scope_paths(
            (str(raw_path),),
            worktree=_scope_worktree(record, tree),
            repository=repository,
        )
        if len(roots) != 1:
            raise CrewError(
                f"accepted path {raw_path!r} is not inside the run repository"
            )
        path = roots[0].as_posix()
        reason = str(raw_reason).strip()
        if not reason:
            raise CrewError(f"accepted path {path!r} requires a stated reason")
        if path in normalized:
            raise CrewError(f"accepted path {path!r} was named more than once")
        normalized[path] = reason

    outside_set = set(outside_paths)
    unaccepted = sorted(outside_set - normalized.keys())
    if unaccepted:
        raise CrewError(
            f"run {run_id!r} changed paths outside its declared "
            f"write scope: {', '.join(unaccepted)}"
        )
    unchanged = sorted(normalized.keys() - outside_set)
    if unchanged:
        raise CrewError(
            "accepted paths must name changed paths outside the declared write "
            f"scope; these do not: {', '.join(unchanged)}"
        )

    shared_files = _shared_write_paths(str(record.get("project") or ""), repository)
    integrated_claims: dict[str, list[dict[str, str]]] = {}
    path_integrated: dict[str, bool] = {}
    for pointer in list_live():
        peer_run = str(pointer.get("run_id") or "")
        if not peer_run or peer_run == run_id:
            continue
        peer_repository = Path(str(pointer.get("repo") or ".")).resolve()
        if peer_repository != repository:
            continue
        peer_node = pointer.get("node")
        if not isinstance(peer_node, Mapping):
            continue
        peer_tree = Path(str(pointer.get("worktree") or peer_repository))
        claims = _repository_scope_paths(
            peer_node.get("write_paths") or (),
            worktree=peer_tree,
            repository=peer_repository,
        )
        for path in sorted(normalized):
            candidate = Path(path)
            for claim in claims:
                if (
                    candidate == claim
                    or candidate.is_relative_to(claim)
                    or claim.is_relative_to(candidate)
                ):
                    # A file the project declares shareable admits a second
                    # claimant editing a different region: worktrees isolate the
                    # in-flight work and merging is the orchestrator's job, so a
                    # whole-file refusal here serialises nodes that do not
                    # actually collide. Only the exact named file is shareable;
                    # a directory claim that merely contains it is a different
                    # path, and paths under such a claim stay refused. Dispatch
                    # resolves the same list through this same helper.
                    if candidate == claim and path in shared_files:
                        continue
                    if path not in path_integrated:
                        path_integrated[path] = _path_change_reaches_integration_head(
                            repository, commits, path
                        )
                    if path_integrated[path]:
                        # The refusal this claim would raise protects two runs
                        # from editing one file concurrently; a change already
                        # behind the integration head is not at risk from the
                        # peer's live edit, so the claim is noted on the
                        # acceptance and left untouched.
                        integrated_claims.setdefault(path, []).append(
                            {"run_id": peer_run, "claim": claim.as_posix()}
                        )
                        continue
                    raise CrewError(
                        f"run {run_id!r} cannot accept {path}: live run "
                        f"{peer_run!r} claims {claim.as_posix()}"
                    )

    accepted: list[dict[str, Any]] = []
    for path in sorted(normalized):
        entry: dict[str, Any] = {"path": path, "reason": normalized[path]}
        if path in integrated_claims:
            peers = sorted(
                {(claim["run_id"], claim["claim"]) for claim in integrated_claims[path]}
            )
            entry["already_integrated"] = True
            entry["peer_claims"] = [
                {"run_id": peer_run, "claim": claim} for peer_run, claim in peers
            ]
        accepted.append(entry)
    return accepted


def _snapshot_entries(tree: Mapping[str, Any]) -> set[tuple[str, str]]:
    entries = tree.get("status_entries") or ()
    return {
        (str(entry.get("code") or ""), str(entry.get("path") or ""))
        for entry in entries
        if isinstance(entry, Mapping) and str(entry.get("path") or "")
    }


def _run_directory_tree_snapshot(run_id: str) -> Mapping[str, Any] | None:
    """Read the boundary snapshot a per-run supervisor left in the run directory.

    The snapshot is taken by the supervisor after dispatch's writes and before
    the worker's spawn, and its cost grows with the repository's worktree count,
    so it is written to the run directory rather than carried on the pointer
    dispatch returns. A run dispatched before the supervisor existed keeps its
    snapshot on the pointer, which the caller falls back to.
    """
    try:
        data = json.loads((run_dir(run_id) / "tree-snapshot.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def _repository_tree_boundary_violations(
    run_id: str, record: Mapping[str, Any]
) -> list[str]:
    """Return the stray uncommitted edits found in another dispatch-visible tree.

    A declared path on the project's shared-write list is not one of them: the
    list is resolved through the same helper the accepted-path check reads, and
    dispatch admits a concurrent claim there, so a peer's in-flight edit says
    nothing about this run's boundary. A dirty path in a live peer's worktree is
    not one either where that peer's own declaration covers it: it is the peer's
    in-flight work, charged at the peer's own completion, so a shared directory
    grant does not make each holder refuse the other's file. A path the peer does
    not declare is a stray edit this check exists to catch, so it stays charged
    here even inside a live peer's worktree.
    """
    snapshot = _run_directory_tree_snapshot(run_id)
    if snapshot is None:
        snapshot = record.get("repository_tree_snapshot")
    if not isinstance(snapshot, Mapping):
        return []
    before_trees = snapshot.get("trees")
    if not isinstance(before_trees, list):
        return []
    repository = Path(str(record.get("repo") or ".")).resolve()
    # The own tree is the worktree only: the repository is not a fallback here,
    # because the main checkout is a tree this run did not work in and
    # excluding it would drop the very edits this scan exists to report. A
    # blank field names no tree rather than the current directory.
    own_tree = _record_worktree(record)
    worktree_field = str(record.get("worktree") or "").strip()
    # A fenced run's boundary check reads only its own worktree and the main
    # checkout: every other tree is a write the operating system already
    # refused. The recorded fact is read rather than the current default, so a
    # later change to the default cannot redefine what a run already dispatched
    # is checked against. A record written before the field existed carries
    # neither value and keeps the full scan.
    if record.get("fenced") is True and worktree_field:
        roots: list[str | Path] | None = _boundary_tree_roots(
            repository, worktree_field
        )
    else:
        roots = [
            str(tree.get("path") or "")
            for tree in before_trees
            if isinstance(tree, Mapping) and str(tree.get("path") or "")
        ]
    current = _repository_tree_snapshot(repository, roots=roots)
    after_by_path = {
        str(tree.get("path") or ""): tree
        for tree in current["trees"]
        if isinstance(tree, Mapping)
    }
    declared = (record.get("node") or {}).get("write_paths") or ()
    declared_roots = _repository_scope_paths(
        declared,
        worktree=own_tree if own_tree is not None else repository,
        repository=repository,
    )
    shared_files = _shared_write_paths(str(record.get("project") or ""), repository)
    # The paths each live peer holds its worktree for, resolved through the same
    # helper this run's own declaration reads. A dirty path in a peer's worktree
    # is that peer's own work only where the peer's declaration covers it; a path
    # the peer does not declare is a stray edit this check exists to catch.
    held_trees = {tree for tree in _live_worktree_claims() if tree != own_tree}
    peer_grants: dict[Path, list[Path]] = {}
    # A pointer naming no worktree names no tree: a blank field resolved as
    # Path("") is the directory this promotion started in, and a live claim on
    # that tree would read this pointer's declaration onto it.
    for pointer in list_live():
        peer_tree = _record_worktree(pointer)
        if peer_tree is None or peer_tree not in held_trees:
            continue
        if Path(str(pointer.get("repo") or ".")).resolve() != repository:
            continue
        peer_node = pointer.get("node")
        if not isinstance(peer_node, Mapping):
            continue
        peer_grants.setdefault(peer_tree, []).extend(
            _repository_scope_paths(
                peer_node.get("write_paths") or (),
                worktree=peer_tree,
                repository=repository,
            )
        )
    terminal_shadows = _shadow_worktree_records(
        repository, str(record.get("project") or "") or None
    )
    violations: list[str] = []
    for before in before_trees:
        if not isinstance(before, Mapping):
            continue
        raw_path = str(before.get("path") or "")
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if own_tree is not None and path == own_tree:
            continue
        peer_roots = peer_grants.get(path, ())
        shadow_record = terminal_shadows.get(path)
        if (
            shadow_record is not None
            and _is_shadow(shadow_record)
            and _shadow_patch_retained(shadow_record)
        ):
            # Committed shadow records are terminal, and their retained patch
            # is the durable form of the worktree changes. The worktree may
            # therefore keep that evidence without impersonating a live peer
            # edit at the same declared path.
            continue
        after = after_by_path.get(str(path))
        if after is None or not after.get("available", False):
            continue
        status_changed = str(before.get("status_digest") or "") != str(
            after.get("status_digest") or ""
        )
        if not status_changed:
            continue
        # An uncommitted edit on a declared path the project publishes as
        # shareable admits a concurrent editor, so it says nothing about this
        # run's boundary; only a declared path off that list can violate it.
        # A path a live peer holds under its own declaration is the peer's work,
        # not this run's, so it is exempt too; a path the peer does not declare
        # stays charged here.
        changed_paths = {
            changed
            for _, changed in _snapshot_entries(after) - _snapshot_entries(before)
            if changed not in shared_files
            and any(
                Path(changed) == root or Path(changed).is_relative_to(root)
                for root in declared_roots
            )
            and not any(
                Path(changed) == root or Path(changed).is_relative_to(root)
                for root in peer_roots
            )
        }
        label = "main checkout" if path == repository else "peer worktree"
        if changed_paths:
            violations.extend(
                f"{changed} in {label} {path}" for changed in sorted(changed_paths)
            )
    return violations


# A worktree older than this cannot separate its own edits from every other
# run's, so a boundary walk against it reports changes it cannot attribute.
BOUNDARY_REFERENT_MAX_AGE_SECONDS = 7 * 24 * 3600


def _boundary_has_no_referent(record: Mapping[str, Any]) -> str:
    """State why a boundary check has no worktree to attribute changes to.

    Two states make the walk meaningless, and each is reported in the terms a
    reader can act on. A record whose worktree is gone has nothing to compare
    against: the walk then measures today's trees against a baseline from
    dispatch, so every edit made since reads as this run's. A surviving
    worktree whose dispatch predates the stated bound has the same defect, since
    a week of everyone's work sits between its baseline and now. Returns an
    empty string when a referent survives, which keeps the ordinary per-path
    check in force.
    """
    worktree = str(record.get("worktree") or "").strip()
    if not worktree:
        return "its record names no worktree to check against"
    if not Path(worktree).is_dir():
        return f"its worktree {worktree} is gone"
    age = _elapsed_seconds(record.get("created_at"), _utc_now())
    if age is not None and age > BOUNDARY_REFERENT_MAX_AGE_SECONDS:
        days = BOUNDARY_REFERENT_MAX_AGE_SECONDS // 86400
        return f"its base is older than the {days}-day bound this check can attribute"
    return ""


def _require_repository_tree_boundary(
    run_id: str, record: Mapping[str, Any], *, waiver_reason: str = ""
) -> dict[str, Any] | None:
    """Refuse a stray uncommitted edit in another dispatch-visible tree.

    A genuine violation may be waived with a required reason, which is
    recorded on the promoted run rather than erased. A waiver offered against
    a run with nothing to waive is itself refused, naming that nothing was
    waived — an unconditional waiver would stop meaning anything.
    """
    reason = str(waiver_reason).strip()
    violations = _repository_tree_boundary_violations(run_id, record)
    if violations:
        # A run whose own worktree no longer survives cannot be attributed the
        # changes the walk finds: with the tree gone and its base older than the
        # diff it would be measured against, every edit another run made since
        # reads as this run's. Enumerating them asks the operator for one false
        # claim per path; the honest report is that the check has no referent,
        # resolved by a single acknowledgement.
        no_referent = _boundary_has_no_referent(record)
        if no_referent:
            if not reason:
                raise CrewError(
                    f"run {run_id!r} cannot have its repository-tree boundary "
                    f"checked: {no_referent}. The changes the walk found "
                    f"({', '.join(violations)}) cannot be attributed to this run. "
                    "A run whose worktree survives is checked per path; this one "
                    "takes a single acknowledgement — pass "
                    "--waive-boundary-refusal REASON to record that you accept "
                    "the boundary could not be checked"
                )
            return {
                "reason": reason,
                "no_referent": no_referent,
                "waived_paths": list(violations),
            }
        if not reason:
            raise CrewError(
                f"run {run_id!r} has uncommitted changes at its declared paths "
                f"outside its own worktree: {', '.join(violations)}"
            )
        return {"reason": reason, "waived_paths": list(violations)}
    if reason:
        raise CrewError(
            f"run {run_id!r} has no repository-tree boundary violation for "
            f"--waive-boundary-refusal {reason!r} to waive"
        )
    return None


def _is_shadow(record: Mapping[str, Any]) -> bool:
    """Return whether a live or committed record is shadow evidence."""
    lineage = record.get("lineage")
    return isinstance(lineage, Mapping) and lineage.get("kind") == "shadow"


def _write_shadow_patch(record: Mapping[str, Any]) -> Path:
    """Persist the complete diff from a shadow's fixed base, including new files."""
    run_id = str(record.get("run_id") or "")
    worktree = _record_worktree(record)
    base = str(record.get("base_sha") or "")
    if worktree is None:
        raise CrewError(
            f"shadow run {run_id!r} has no readable worktree; its patch cannot be preserved"
        )
    if not _resolve_commit(worktree, base):
        raise CrewError(
            f"shadow run {run_id!r} base {base!r} is not reachable in its worktree"
        )

    tracked = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", base, "--"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if tracked.returncode:
        raise CrewError(f"shadow run {run_id!r} could not produce its tracked diff")
    patch = bytearray(tracked.stdout)

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if untracked.returncode:
        raise CrewError(f"shadow run {run_id!r} could not enumerate new files")
    for raw_path in (item for item in untracked.stdout.split(b"\0") if item):
        path = os.fsdecode(raw_path)
        addition = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", path],
            cwd=worktree,
            capture_output=True,
            check=False,
        )
        if addition.returncode not in (0, 1):
            raise CrewError(
                f"shadow run {run_id!r} could not preserve new file {path!r}"
            )
        if patch and not patch.endswith(b"\n"):
            patch.extend(b"\n")
        patch.extend(addition.stdout)

    artifact = run_dir(run_id) / "shadow.patch"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(bytes(patch))
    return artifact


def _shadow_patch_stat(path: Path, *, cwd: Path) -> dict[str, int]:
    """Derive line and file counts from the retained patch artifact."""
    if not path.read_bytes():
        return {"added": 0, "removed": 0, "files": 0}
    result = subprocess.run(
        ["git", "apply", "--numstat", str(path)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise CrewError(f"shadow patch {path} is not a measurable git patch")
    added = removed = files = 0
    for line in result.stdout.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        files += 1
        added += int(fields[0]) if fields[0].isdigit() else 0
        removed += int(fields[1]) if fields[1].isdigit() else 0
    return {"added": added, "removed": removed, "files": files}


def _elapsed_seconds(start: Any, end: Any) -> int | None:
    """Return whole seconds between two ISO-8601 stamps, or None."""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    first = parse_utc(start)
    last = parse_utc(end)
    if first is None or last is None:
        return None
    return max(0, int((last - first).total_seconds()))


def _assume_utc_if_naive(value: str) -> str:
    """Attach UTC to a completion stamp that carries no timezone."""
    parsed = parse_iso(value)
    if parsed is None or parsed.tzinfo is not None:
        return value
    return parsed.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _wall_exceeded_budget(wall_seconds: int | None, time_budget: Any) -> bool:
    """Flag wall time beyond the bounded multiple used to identify stalls."""
    if wall_seconds is None:
        return False
    try:
        budget_seconds = parse_duration(str(time_budget))
    except CrewError:
        return False
    return wall_seconds > STALL_BUDGET_MULTIPLE * budget_seconds


def _run_streams(path: Path) -> list[Path]:
    """Return the original stream followed by resumes and lane changes."""
    from reckon.crew import metering

    return metering.run_streams(path)


def _record_stream_paths(record: Mapping[str, Any]) -> list[Path]:
    """Every surviving stream path a run's tool calls appear in.

    ``log_path`` is the stream file the launcher wrote and resumes live beside
    it; the run directory is the fallback when a pointer never carried one, so
    a record whose launcher recorded nothing still reaches whatever stream
    survived.
    """
    candidates: list[Path] = []
    gathered: set[Path] = set()
    for candidate in _run_streams(Path(str(record.get("log_path") or ""))):
        if candidate not in gathered:
            candidates.append(candidate)
            gathered.add(candidate)
    run_stream = run_dir(str(record.get("run_id") or "")) / "stream.jsonl"
    for candidate in _run_streams(run_stream):
        if candidate not in gathered:
            candidates.append(candidate)
            gathered.add(candidate)
    return candidates


def _primary_read_targets(
    primary: Mapping[str, Any], tree: Path | None
) -> tuple[str, tuple[str, ...]]:
    """The landed commit and changed paths a shadow could have read.

    The commit is the primary row's own cited sha, resolved at its promotion,
    and the paths are the files that sha changed, read back from the shared
    object store so no manifest or fixture needs to have named them. A primary
    with no cited commit (a shadow of a shadow, which does not land code) has
    nothing a run could read as an answer, and a run whose record names no
    readable tree has nowhere to read the paths back from: both answer with no
    targets rather than consulting a tree the run was never dispatched for.
    """
    commits = [str(sha) for sha in (primary.get("commits") or ()) if str(sha).strip()]
    commit = commits[-1] if commits else ""
    if not commit or tree is None:
        return "", ()
    canonical = _resolve_commit(tree, commit)
    if not canonical:
        return commit, ()
    result = subprocess.run(
        [
            "git",
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            canonical,
        ],
        cwd=str(tree),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return commit, ()
    return commit, tuple(line for line in result.stdout.splitlines() if line.strip())


def _shadow_stream_contamination(
    record: Mapping[str, Any],
    ledger_runs: Iterable[Mapping[str, Any]],
    tree: Path | None,
) -> str | None:
    """Scan a shadow's streams for reads of its primary's landed answer.

    Detection runs at promotion, where both halves are already in hand: the
    primary's row in the ledger this promotion loaded and the run's own stream.
    A run that cannot see the primary's commit — its row absent, its stream
    gone, or its primary never cited one — answers clean rather than refusing,
    because an instrument that fails must not become a refusal of its own.
    """
    lineage = record.get("lineage")
    primary_run_id = str((lineage or {}).get("primary_run_id") or "")
    if not primary_run_id:
        return None
    primary = next(
        (row for row in ledger_runs if str(row.get("run_id")) == primary_run_id),
        None,
    )
    if primary is None:
        return None
    commit, paths = _primary_read_targets(primary, tree)
    if not commit:
        return None
    stream_paths = _record_stream_paths(record)
    if not stream_paths:
        return None
    calls = ledger.stream_tool_calls(stream_paths)
    return ledger.shadow_primary_read(
        calls,
        primary_commit=commit,
        primary_paths=paths,
        repo_root=str(record.get("repo") or ""),
        worktree=str(record.get("worktree") or ""),
    )


@dataclass(frozen=True)
class StreamMeasures:
    """Measurements recoverable from a run's ordered event streams."""

    completed_at: str | None
    completion_source: str | None
    worker_seconds: int | None
    budget: dict[str, Any]
    session_id: str | None
    # The rate the run generated at, read from the same terminal observation the
    # budget comes from. It has to be carried out of here because the streams it
    # is derived from are the run's, and nothing downstream re-parses them.
    throughput: dict[str, Any] = field(default_factory=dict)


def _declared_manifest_commits(record: Mapping[str, Any]) -> list[str]:
    """Commits a run's durable manifest declares, for landing-record detection."""
    manifest_path = str(record.get("manifest_path") or "")
    if not manifest_path:
        return []
    try:
        declared = parse_manifest(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, KeyError, ValueError):
        return []
    return [str(sha).strip() for sha in (declared.get("commits") or []) if str(sha).strip()]


def _declared_manifest_landing(record: Mapping[str, Any]) -> str:
    """The ``landing:`` line a run's durable manifest declares, if any.

    A run records its landing in this line rather than editing the plan, so
    promotion can land it through the plan's own versioned write instead of
    depending on a merge of the plan HTML. A manifest that cannot be read
    yields no landing line, which keeps every other run on the existing path.
    """
    manifest_path = str(record.get("manifest_path") or "")
    if not manifest_path:
        return ""
    try:
        declared = parse_manifest(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, KeyError, ValueError):
        return ""
    return str(declared.get("landing") or "").strip()


def _worker_authored_landing_record(
    *,
    tree: Path,
    commits: Iterable[str],
    project: str,
    plan: str,
    comment_id: str,
) -> bool:
    """True when the run's own committed plan already carries the comment id.

    A worker that followed the landing contract wrote its record under this
    same run-derived comment id into its own tree, and the merge that later
    brings that record in resolves in the worker's favour. Reading the plan at
    the run's submitted commit detects the record before the merge, so
    promotion leaves the plan file untouched here and the merge cannot collide
    on a duplicate comment id.
    """
    revisions = [str(sha).strip() for sha in commits if str(sha).strip()]
    if not revisions:
        return False
    plan_file = _store._resolve_html_file(project, plan, root=tree, artifact_type="plan")
    if plan_file is None:
        return False
    try:
        relative = plan_file.resolve().relative_to(tree.resolve())
    except ValueError:
        return False
    shown = _git(tree, "show", f"{revisions[-1]}:{'/'.join(relative.parts)}", check=False)
    return shown.returncode == 0 and f'data-id="{comment_id}"' in shown.stdout


def _record_landing_comment(
    *,
    project: str,
    plan: str,
    section: str,
    run_id: str,
    narrative: str,
    author: str,
    when: str,
    root: str | Path | None,
    worker_tree: Path | None = None,
    worker_commits: Iterable[str] = (),
    landing: str = "",
) -> dict[str, Any]:
    """Append one idempotent section comment for a promoted run.

    A run whose manifest declares a ``landing:`` line lands that line as the
    comment body through the versioned write below, so its record reaches the
    plan as an ordinary versioned edit and never depends on a git merge of the
    plan HTML. The worker-authored deferral then does not apply: a worker that
    writes a ``landing:`` line does not edit the plan, so there is no
    merge-borne duplicate for promotion to leave alone.

    When the run's own worker already wrote the landing record — under this
    same run-derived comment id, in its own committed plan — nothing is
    appended, so promotion leaves the plan file untouched and the merge that
    brings the worker's record in does not collide on the duplicate id.

    The append is also where a plan's first landing moves it out of ``draft``
    or ``pending``: that status flip rides in the same versioned write, so a
    plan that was never started stops reading as unstarted the moment work
    lands on it. ``status`` is a metadata scalar, so the flip never re-stales
    a design review.
    """
    landing = str(landing).strip()
    narrative = str(narrative).strip()
    body_text = landing or narrative
    if not body_text or not plan:
        return {"recorded": False, "reason": "empty_narrative"}
    comment_id = f"{RUN_COMMENT_PREFIX}{re.sub(r'[^A-Za-z0-9._-]+', '-', run_id)}"
    anchor = section_anchor(section)
    desired_body = f"<p>{html.escape(body_text)}</p>"
    worker_recorded = bool(
        not landing
        and worker_tree
        and _worker_authored_landing_record(
            tree=worker_tree,
            commits=worker_commits,
            project=project,
            plan=plan,
            comment_id=comment_id,
        )
    )
    # A plan's status is set by hand, so nothing moves it when work starts. The
    # first landing is the moment work has demonstrably begun, and this append
    # is already a versioned read-modify-write of the plan, so the flip rides in
    # the same write rather than costing a second one. Only a plan that still
    # reads ``draft`` or ``pending`` moves; every other status — ``active``,
    # ``blocked``, the terminal states — is authored and left alone.
    in_progress_states = frozenset({"draft", "pending"})
    for _attempt in range(4):
        state, version = _store.read_plan(project, plan, root, artifact_type="plan")
        if not state or state.get("type") != "plan":
            return {"recorded": False, "reason": "plan_unavailable"}
        started = str(state.get("status") or "").strip().lower() in in_progress_states
        comments = {
            key: list(items) for key, items in (state.get("comments") or {}).items()
        }
        items = comments.setdefault(anchor, [])
        existing = next(
            (item for item in items if str(item.get("id") or "") == comment_id),
            None,
        )
        if existing is not None:
            if worker_recorded:
                return {
                    "recorded": False,
                    "comment_id": comment_id,
                    "section": anchor,
                    "reason": "worker_authored_landing_record",
                }
            # The store round-trips the body through HTML, so the text read back
            # has its entities decoded (``run&apos;s`` returns as ``run's``)
            # while ``desired_body`` is the escaped form. Comparing the raw
            # strings makes an identical narrative look different the moment it
            # carries an apostrophe, which makes an interrupted landing
            # unretryable: the row is rolled back but the plan comment is not,
            # and every re-promotion then refuses a narrative it already wrote.
            # Compare the unescaped text on both sides so the same narrative
            # always matches however the store happens to encode it.
            if html.unescape(str(existing.get("body") or "")) != html.unescape(
                desired_body
            ):
                raise CrewError(
                    f"landing comment {comment_id!r} for plan {plan!r} already "
                    "contains a different narrative; refusing to report the "
                    "corrected outcome as recorded"
                )
            return {
                "recorded": True,
                "comment_id": comment_id,
                "section": anchor,
                "already_recorded": True,
            }
        if worker_recorded:
            return {
                "recorded": False,
                "comment_id": comment_id,
                "section": anchor,
                "reason": "worker_authored_landing_record",
            }
        items.append(
            {
                "id": comment_id,
                "who": author,
                "when": when,
                "body": desired_body,
            }
        )
        payload: dict[str, Any] = {**state, "comments": comments}
        if started:
            payload["status"] = "in-progress"
        try:
            _store.write_plan(
                project,
                plan,
                payload,
                version,
                root,
                artifact_type="plan",
            )
        except _store.VersionConflict:
            continue
        return {
            "recorded": True,
            "comment_id": comment_id,
            "section": anchor,
            "already_recorded": False,
            "status": payload.get("status"),
        }
    raise CrewError(
        f"could not record landing comment for plan {plan!r}: "
        "the plan changed during four consecutive write attempts"
    )


# The git state files whose presence means another operation owns the index.
# Each is resolved through ``git rev-parse --git-path`` rather than a literal
# ``.git/`` join, so a linked worktree — whose ``.git`` is a file pointing at
# its private directory — resolves the marker where git actually keeps it.
_OPEN_OPERATION_MARKERS: tuple[tuple[str, str], ...] = (
    ("MERGE_HEAD", "merge"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)


def _open_operation_state(checkout: Path) -> str | None:
    """The git operation the checkout has open, or ``None`` when none has.

    Promotion commits the stores it writes into the checkout's index. An open
    merge, rebase, cherry-pick or revert means another session is mid-operation there:
    a landing commit would move that operation's first parent under it and a
    whole-index commit could take its staged work. The marker path is resolved
    with ``git rev-parse --git-path`` so a linked worktree, whose ``.git`` is a
    file, is read at its own git directory rather than a ``.git/`` that does
    not exist.
    """
    for marker, description in _OPEN_OPERATION_MARKERS:
        resolved = _worktree_git_paths(checkout, "rev-parse", "--git-path", marker)
        if not resolved:
            continue
        candidate = Path(resolved[0])
        if not candidate.is_absolute():
            candidate = checkout / candidate
        if candidate.exists():
            return description
    return None


def _require_committable_checkout(checkout: Path | None, run_id: str) -> None:
    """Refuse before writing when the checkout cannot host the landing commit.

    Promotion writes two tracked stores (the ledger row and the plan landing
    comment) and commits them as one landing. A checkout that is not a git
    worktree cannot host that commit, and one with an open merge, rebase,
    cherry-pick or revert is owned by another operation, so promotion refuses here,
    before either store is written, rather than writing stores it could not
    commit or committing into a peer's operation.
    """
    if checkout is None:
        raise CrewError(
            f"run {run_id!r} cannot be promoted: no checkout is known for it, "
            "so the landing commit would have nowhere to land; promotion "
            "refuses before writing either store"
        )
    probe = _git(checkout, "rev-parse", "--is-inside-work-tree", check=False)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise CrewError(
            f"run {run_id!r} cannot be promoted: the checkout {checkout} is not "
            "a git worktree, so the ledger row and plan comment it would write "
            "could not be committed in one landing; promotion refuses before "
            "writing either store"
        )
    open_state = _open_operation_state(checkout)
    if open_state is not None:
        raise CrewError(
            f"run {run_id!r} cannot be promoted: the checkout {checkout} has an "
            f"open {open_state} in progress, so the landing commit would write "
            "into an operation another session may be concluding; promotion "
            "refuses before writing either store"
        )


def _path_differs_from_head(checkout: Path, path: Path) -> bool:
    """Whether a tracked path's working-tree content differs from HEAD.

    ``git diff --quiet`` exits 0 when the path matches HEAD and 1 when it
    differs. Any other exit is a git failure, which counts as differing so the
    path is carried into the landing commit and the failure surfaces there
    rather than silently dropping a write; a path outside the checkout cannot
    be compared and likewise counts as differing.
    """
    try:
        relative = path.resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError:
        return True
    diff = _git(checkout, "diff", "--quiet", "HEAD", "--", relative, check=False)
    return diff.returncode != 0


def _plan_differs_only_by_the_stores_own_writes(
    checkout: Path, plan_file: Path
) -> bool:
    """Whether the plan file's working copy differs from HEAD only by writes
    the plan store itself makes.

    The store is the sole writer of a plan's reckon-owned content: the
    ``plan-*`` scalars (``impl``, ``status``, the version stamps and the rest)
    and every element carrying ``data-reckon``. That marker declares the store's
    own region whatever the tag, so the admitted class is every ``data-reckon``
    element the store writes — the section records, the sections such as gates,
    decisions, followups, questions, research and comments, and the landed and
    landing notes are examples rather than an exhaustive list. Every one of
    those is a plan-state write the store makes, so a change confined to them —
    an impl or status move, a resolved followup, an appended landing comment, a
    collapsed section's landed note, a re-encoded entity — is the run's own
    bookkeeping and is admitted. The comparison therefore reads each side as
    parsed HTML and keeps only the authored content outside those store-owned
    regions; an authored prose edit, which the store never regenerates, survives
    on both sides and is the only thing that refuses.

    Parsing both sides through the same HTML reader also normalises a
    re-encoded entity, so the store's canonical re-encoding does not read as an
    authored change. A body-resident comment the working copy holds and HEAD
    does not is a landing record the store appended outside its comments
    section and is dropped before comparing, while one HEAD already carries
    survives on both sides.
    """
    from bs4 import BeautifulSoup

    try:
        relative = plan_file.resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError:
        return False
    head = _git(checkout, "show", f"HEAD:{relative}", check=False)
    if head.returncode != 0:
        return False
    try:
        head_text = head.stdout
        disk_text = plan_file.read_text(encoding="utf-8", errors="replace")
        head_soup = BeautifulSoup(head_text, "html.parser")
        disk_soup = BeautifulSoup(disk_text, "html.parser")
    except Exception:  # noqa: BLE001 - an unreadable plan is not provably clean
        return False
    head_ids = {
        str(element.get("data-id") or "") for element in head_soup.select(".r-comment")
    }
    # A body-resident comment the store appended for this landing is not
    # authored, so drop disk elements HEAD does not already carry.
    for element in disk_soup.select(".r-comment"):
        if str(element.get("data-id") or "") not in head_ids:
            element.decompose()
    for soup in (head_soup, disk_soup):
        _strip_store_owned_content(soup)
    # Removing the store's records leaves the whitespace between the tags that
    # held them, which is not authorship either side carried. Collapse
    # whitespace between adjacent tags so the removed records do not read as a
    # change; text inside a tag is untouched, so prose edits still differ.
    left = _collapse_inter_tag(str(head_soup))
    right = _collapse_inter_tag(str(disk_soup))
    return left == right


_RECORD_ATTRIBUTES = frozenset(
    {
        "data-effort-hours",
        "data-attempts",
        "data-status",
        "data-links",
    }
)


def _strip_store_owned_content(soup) -> None:
    """Remove the plan store's regenerate-from-state content in place.

    Leaves only authored prose: the ``plan-*`` scalars and every element the
    store marks as its own region are detached, so a difference that survives
    is one the store does not own. Every element carrying ``data-reckon`` is
    such a region, whatever its tag — the section records, the sections such
    as gates, decisions, followups, questions, research and comments, and the
    landed and landing notes are examples rather than an exhaustive list.
    """
    for meta in soup.find_all("meta"):
        if (meta.get("name") or "").lower().startswith("plan-"):
            meta.decompose()
    for element in soup.select("[data-reckon]"):
        element.decompose()
    for element in soup.find_all(True):
        for attribute in list(element.attrs):
            if attribute in _RECORD_ATTRIBUTES or attribute.startswith(
                "data-capability-"
            ):
                del element[attribute]


def _collapse_inter_tag(text: str) -> str:
    """Drop whitespace that sits directly between a closing and an opening tag."""
    return re.sub(r"(?<=>)\s+(?=<)", "", text)


def _refuse_unrelated_plan_edit(
    *,
    project: str,
    plan: str,
    root: str | Path | None,
    checkout: Path,
) -> None:
    """Refuse a landing while the plan file carries an unrelated uncommitted
    change that the landing commit would sweep in.

    Runs before the landing writes either store, so a refused promotion leaves
    neither a ledger row nor a plan comment for the next promotion to read as
    an unrelated edit. A plan whose working copy matches HEAD, or differs only
    by the store's own writes, passes untouched.
    """
    if not str(plan):
        return
    plan_file = _store._resolve_html_file(
        project, str(plan), root, artifact_type="plan"
    )
    if plan_file is None or not _path_differs_from_head(checkout, plan_file):
        return
    if _plan_differs_only_by_the_stores_own_writes(checkout, plan_file):
        return
    raise CrewError(
        f"the plan file {plan_file} carries an uncommitted change that is not a "
        "plan-state write the store made (an impl or status move, a resolved "
        "followup or other section record, an appended landing comment, a "
        "version stamp or the store's own re-encoding); the refused difference "
        "is authored content outside those store-owned regions, so refusing to "
        "sweep it into the landing commit. Commit or discard the unrelated "
        "edit, then re-promote."
    )


def _plan_comment_store_path(
    *,
    project: str,
    plan: str,
    comment: Mapping[str, Any],
    root: str | Path | None,
    checkout: Path,
) -> list[Path]:
    """The tracked plan path a landing comment carries into the commit.

    The ledger path is never returned here: a caller that appended a newly
    recorded row adds it explicitly, while the already-landed branch rewrote
    no ledger of its own.

    A newly recorded comment always wrote the plan file, so the file is
    returned. An idempotent retry usually leaves the plan file unchanged and
    returns empty, but not when an earlier attempt recorded the comment and
    then failed to commit it: the comment reads as already recorded from the
    plan on disk while the file still differs from HEAD, so leaving it out
    would strand the landing's own write as uncommitted state. The fact that
    decides inclusion is therefore whether the file differs from HEAD, not
    whether this call recorded it.
    """
    if not str(plan) or not comment.get("recorded"):
        return []
    plan_file = _store._resolve_html_file(
        project, str(plan), root, artifact_type="plan"
    )
    if plan_file is None:
        return []
    if comment.get("already_recorded") and not _path_differs_from_head(
        checkout, plan_file
    ):
        return []
    return [plan_file]


def _restore_landing_writes(
    checkout: Path, paths: Sequence[Path]
) -> dict[str, bool]:
    """Best-effort reversal of a refused landing's uncommitted store writes.

    Each path promotion wrote returns to its committed state: tracked paths
    are restored from HEAD; a path absent from HEAD (created by this
    promotion) is dropped from the index and, once its entry is gone, from the
    working tree. Recovery is best-effort because the refusal that triggers it
    (a stuck index or other git failure) can itself block these git calls, and
    a tracked path whose restore is refused keeps its working-tree write — the
    retry commits that write, so taking it back would discard the recorded
    comment. What a refusal does not leave behind is a staged entry the run's
    own commit never covered.

    The result is a per-path report keyed by the path as written: True for a
    path that no longer carries the landing write — restored from HEAD, or
    dropped because HEAD never had it, with nothing of it staged — and False
    for one whose write survives. A caller reporting a store it wrote reads
    this to tell a row that survived the rollback from one the rollback
    reverted.
    """
    report: dict[str, bool] = {}
    for path in paths:
        target = str(path)
        restored = _git(
            checkout,
            "restore",
            "--source=HEAD",
            "--staged",
            "--worktree",
            "--",
            target,
            check=False,
        )
        if restored.returncode == 0:
            report[target] = True
            continue
        report[target] = _restore_one_landing_write(checkout, path)
    return report


def _restore_one_landing_write(checkout: Path, path: Path) -> bool:
    """Settle one landing write as far as a held index lock allows.

    The whole-path restore is a single index-writing call, so a lock another
    process holds refuses it and leaves the working-tree write in place. The
    working tree is left as the attempt wrote it on purpose: the retry commits
    that write, so a plan file taken back from HEAD would lose the comment the
    retry is recording. What must not survive is a staged entry, because a
    peer's next commit could take it without this run ever having committed.

    A tracked path therefore keeps its write and is reported as surviving. A
    path HEAD never carried is dropped from the index, and its file is removed
    only once the entry is confirmed gone: a lock that refuses the drop would
    otherwise leave the index staging a path no longer on disk, which is a
    commit of content a reader cannot see in the working tree.
    """
    try:
        relative = Path(path).resolve().relative_to(checkout.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    committed = _git(checkout, "cat-file", "-e", f"HEAD:{relative}", check=False)
    if committed.returncode == 0:
        return False
    _git(checkout, "rm", "--cached", "--force", "--", str(path), check=False)
    if _path_has_a_staged_change(checkout, path):
        return False
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _path_has_a_staged_change(checkout: Path, path: Path) -> bool:
    """Whether the index carries a change at ``path`` against HEAD.

    A read, so it answers while another process holds the index lock. A probe
    git refuses to answer counts as a staged change, because a restore the
    rollback cannot show is not one it may claim.
    """
    diff = _git(
        checkout, "diff", "--cached", "--name-only", "--", str(path), check=False
    )
    if diff.returncode != 0:
        return True
    return bool(diff.stdout.strip())


# A refused landing commit carries its rollback's per-path report here, so the
# receipt composed around it can tell a reverted row from one that survived.
LANDING_ROLLBACK_ATTRIBUTE = "landing_rollback"


@contextmanager
def _report_written_ledger_row(
    run_id: str,
    *,
    ledger_path: str | Path | None = None,
    row_present: Callable[[], bool] | None = None,
):
    """Keep the append receipt visible when a later landing operation fails.

    The append returning is not the row being present: a landing that fails
    commits, then rolls its own writes back, reverts the ledger to HEAD and
    takes the appended row with it. So the receipt states which of the two
    states the row is in, and the branch is decided by what the rollback
    reported for the ledger path together with reading the row back from the
    ledger this promotion resolved — never by the fact that the append
    returned. The already-written wording warns against re-promotion, which is
    correct only while the row survives it; when the rollback reverted the
    row, re-promotion is the recovery once the landing failure is resolved.

    Exceptions escaping this span lose their type, which is safe only while no
    typed exception handler can be reached by an exception raised within it.
    """
    try:
        yield
    except Exception as exc:
        if _ledger_row_was_rolled_back(exc, ledger_path, row_present):
            raise CrewError(
                f"the ledger row for run {run_id!r} was written and has been "
                f"rolled back with {ledger_path}; re-promote once the landing "
                f"failure is resolved. Landing or cleanup failed: {exc}"
            ) from exc
        raise CrewError(
            f"the ledger row for run {run_id!r} is already written; "
            f"do not re-promote. Landing or cleanup failed: {exc}"
        ) from exc


def _ledger_row_was_rolled_back(
    exc: BaseException,
    ledger_path: str | Path | None,
    row_present: Callable[[], bool] | None,
) -> bool:
    """Whether a failing landing reverted the ledger row it had appended.

    Two facts, and neither alone is enough. The rollback's own per-path report
    says whether it returned this ledger path to HEAD, which is why the append
    returning proves nothing; the row read back from the ledger the promotion
    resolved says whether the row a reader will open is actually there. A
    report that preserved the path, or a read-back that finds the row, keeps
    the already-written wording. Anything unreadable counts as present, so the
    receipt never claims a rollback it cannot show.
    """
    if ledger_path is None or row_present is None:
        return False
    report = getattr(exc, LANDING_ROLLBACK_ATTRIBUTE, None)
    if not isinstance(report, Mapping):
        return False
    target = _resolved_path_key(ledger_path)
    reverted = next(
        (
            bool(state)
            for path, state in report.items()
            if _resolved_path_key(path) == target
        ),
        False,
    )
    if not reverted:
        return False
    return row_present() is False


def _resolved_path_key(path: str | Path) -> str:
    """A path's absolute form, so a report key and a lookup resolve together."""
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, ValueError):
        return str(path)


def _landing_refusal(message: str, rollback: Mapping[str, bool]) -> CrewError:
    """A refused landing commit that carries its rollback's per-path report.

    The receipt around the landing decides which state the appended row is in
    from this report, so it must travel with the refusal rather than be
    recomputed: only the rollback knows which restore calls git refused.
    """
    error = CrewError(message)
    setattr(error, LANDING_ROLLBACK_ATTRIBUTE, dict(rollback))
    return error


def _ledger_holds_row(project: str, root: str | Path | None, run_id: str) -> bool:
    """Whether the ledger this promotion resolved carries ``run_id``.

    The row is read back from the same store the append targeted rather than
    inferred from the append's return: a rollback can have re-read the file
    since. An unreadable ledger answers present, so a receipt never claims a
    rollback the reader cannot confirm.
    """
    try:
        data, _version = ledger.load(project, root=root)
    except (OSError, ValueError, ledger.LedgerError):
        return True
    return any(
        str(item.get("run_id") or "") == run_id for item in data.get("runs", [])
    )


def _discard_run_store_row(run_id: str) -> bool:
    """Drop the run store row a refused landing inserted, so a retry can write it.

    The store indexes the committed ledger, so a run whose landing never
    committed has no row to keep — and leaving one behind makes the retry's
    insert collide with the row the refused attempt wrote instead of
    re-recording the run. The index has no delete of its own, so the row and
    its detail row are removed directly against the store's declared tables.
    Best-effort: the refusal that triggers this is the caller's outcome, and a
    store this cannot reach must not replace it.
    """
    import sqlite3

    from reckon import run_store

    store = run_store.store_path()
    if not store.is_file():
        return False
    try:
        connection = sqlite3.connect(str(store))
    except sqlite3.Error:
        return False
    try:
        with connection:
            connection.execute(
                'DELETE FROM "run_details" WHERE "run_id" = ?', (run_id,)
            )
            removed = connection.execute(
                'DELETE FROM "runs" WHERE "run_id" = ?', (run_id,)
            )
        return bool(removed.rowcount)
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _commit_landing_writes(
    *,
    run_id: str,
    verdict: str,
    checkout: Path,
    paths: Sequence[Path],
    subject: str | None = None,
    body: str | None = None,
    store_row_written: bool = False,
) -> dict[str, Any]:
    """Commit promotion's own store writes in one landing commit.

    Stages exactly the given paths (never a whole-tree add) and commits them
    under a subject naming the promoted run and its gate verdict, so a landing
    leaves the checkout with no uncommitted change at the paths promotion
    wrote. A write that cannot be staged or committed attempts to restore
    those paths and refuses. A blocked restore preserves paths held by HEAD;
    callers that already appended a ledger row must report that append.

    ``store_row_written`` states that this attempt's own append inserted the
    run's row in the rebuildable store. A refused landing then removes it,
    because the store indexes a committed ledger and this run's row did not
    commit: the retry must be able to insert it rather than collide with it.
    An append that only found the row already present leaves it alone.

    ``subject`` and ``body`` override the promotion-flavoured defaults; a
    caller that records a landing that is not a promotion (a gate re-run at
    the integrated revision) passes its own subject naming what it did.
    """
    targets = sorted(
        {Path(p).expanduser().resolve() for p in paths if Path(p).is_file()}
    )
    if not targets:
        return {"committed": False, "reason": "no_write"}
    staged = _git(checkout, "add", "--", *(str(p) for p in targets), check=False)
    if staged.returncode != 0:
        rollback = _restore_landing_writes(checkout, targets)
        if store_row_written:
            _discard_run_store_row(run_id)
        raise _landing_refusal(
            f"could not stage the landing writes for run {run_id!r} in "
            f"{checkout}: {staged.stderr.strip() or staged.stdout.strip()}",
            rollback,
        )
    subject = subject or f"promote({run_id}): {verdict}"
    body = body or (
        "Record the landing: append the run to the project ledger and its "
        "plan comment in one commit, so a promotion leaves the checkout "
        "without uncommitted state at the paths it wrote."
    )
    committed = _git(
        checkout,
        "commit",
        "-m",
        subject,
        "-m",
        body,
        check=False,
    )
    if committed.returncode != 0:
        rollback = _restore_landing_writes(checkout, targets)
        if store_row_written:
            _discard_run_store_row(run_id)
        raise _landing_refusal(
            f"could not commit the landing writes for run {run_id!r} in "
            f"{checkout}: {committed.stderr.strip() or committed.stdout.strip()}",
            rollback,
        )
    return {"committed": True, "subject": subject, "paths": [str(p) for p in targets]}


def _zone_aware_stream_timestamp(timestamp: object) -> datetime | None:
    """The moment a stream event states, kept only when it names its zone.

    A stamp that carries a ``Z`` suffix or a numeric offset is a moment the
    event placed; one that names no zone is dropped, so the span this feeds is
    measured only from moments that stated where they were rather than from an
    assumption of UTC.
    """
    parsed = parse_iso(timestamp)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed


def _terminal_stream_data(
    record: Mapping[str, Any],
) -> StreamMeasures:
    """Resolve completion from events, then stream mtimes, across all turns."""
    budget = dict(record.get("budget") or {})
    if record.get("launch") != "cli":
        return StreamMeasures(None, None, None, budget, None)

    backend_name = str(record.get("backend") or "")
    backend = _backend_settings(record, None)
    path = Path(str(record.get("log_path") or ""))
    paths = _run_streams(path)
    if not paths:
        return StreamMeasures(None, None, None, budget, None)

    timestamps: list[tuple[datetime, str]] = []
    session_id = None
    throughput: dict[str, Any] = {}
    # The client-owned rollout receipt, read by the same authority
    # _harvest_lane_receipt uses, joins this observe_log call to the model span
    # it already measures. The exec stream cannot separate inference from tool
    # wait, so without the join the stored throughput block carries no span for
    # a codex run whose rollout does. Only a receipt that actually measured the
    # span is folded in: one that measured nothing must not relabel the stream's
    # own report, so an absent rollout leaves the span explicitly unmeasured
    # rather than claimed.
    record_session = str(record.get("session_id") or "").strip()
    receipt = None
    if record_session:
        candidate = rollout.read_rollout_receipt(record_session)
        if isinstance(
            getattr(candidate, "generation_seconds", None), float
        ) and isinstance(getattr(candidate, "machine_seconds", None), float):
            receipt = candidate
    for candidate in paths:
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=candidate,
            receipt=receipt,
        )
        if observation.terminal:
            budget = dict(observation.budget)
            # The last turn that finished, not a fold across turns: the spans a
            # resume reports are its own, and adding them to an earlier turn's
            # would rate tokens against a clock that never ran for them.
            throughput = dict(observation.throughput)
        session_id = observation.session_id or session_id
        first, last = _backends.cached_stream_timestamp_bounds(
            candidate, _zone_aware_stream_timestamp
        )
        if first is not None:
            timestamps.append(first)
        if last is not None:
            timestamps.append(last)
    if timestamps:
        first = min(timestamps, key=lambda item: item[0])
        last = max(timestamps, key=lambda item: item[0])
        return StreamMeasures(
            last[1],
            "terminal_event",
            max(0, int((last[0] - first[0]).total_seconds())),
            budget,
            session_id,
            throughput,
        )

    newest = max(candidate.stat().st_mtime for candidate in paths)
    completed = (
        datetime.fromtimestamp(newest, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return StreamMeasures(
        completed, "stream_mtime", None, budget, session_id, throughput
    )


# A prompt promotion can read a finished run's stream in the gap between the
# manifest write and the harness' final turn record, folding a completion time
# taken from a file mtime and losing the run's own token figures. Promotion
# waits out that tail once the writer has exited, bounded by the values below.
# The wait is a quiescence poll rather than a fixed sleep: an already-quiet
# stream returns without waiting, and a writer still appending keeps the window
# open until its tail lands or the ceiling elapses.
_STREAM_SETTLE_POLL_SECONDS = 0.05
_STREAM_SETTLE_QUIESCENCE_SECONDS = 0.2
_STREAM_SETTLE_MAX_SECONDS = 2.0


def _newest_stream_mtime(paths: Iterable[Path]) -> float:
    """Return the newest modification time across a run's stream files."""
    mtimes = [candidate.stat().st_mtime for candidate in paths if candidate.is_file()]
    return max(mtimes) if mtimes else 0.0


def _wait_out_stream_tail(paths: Iterable[Path]) -> None:
    """Boundedly wait for a closed writer's stream tail, returning once quiet.

    The stream's newest mtime is polled until it has not advanced for the
    quiescence window, or the hard ceiling elapses, whichever comes first. A
    stream whose newest write already predates the window is already quiet and
    returns without any wait. The ceiling guarantees a truncated stream — a
    writer that died mid-tail, or one whose terminal record never lands —
    cannot hold a promotion past the bound.
    """
    candidates = [path for path in paths if path.is_file()]
    if not candidates:
        return
    newest = _newest_stream_mtime(candidates)
    if time.time() - newest >= _STREAM_SETTLE_QUIESCENCE_SECONDS:
        return
    deadline = time.monotonic() + _STREAM_SETTLE_MAX_SECONDS
    last_mtime = newest
    stable_since: float | None = None
    while True:
        observed = time.monotonic()
        if (
            stable_since is not None
            and observed - stable_since >= _STREAM_SETTLE_QUIESCENCE_SECONDS
        ):
            return
        if observed >= deadline:
            return
        time.sleep(_STREAM_SETTLE_POLL_SECONDS)
        current = _newest_stream_mtime(candidates)
        if current != last_mtime:
            last_mtime = current
            stable_since = None
        elif stable_since is None:
            stable_since = time.monotonic()


def _end_live_writer_for_settle(record: Mapping[str, Any]) -> bool:
    """End a still-writing run before the fold, so its terminal tail is readable.

    A prompt promotion that finds the run's process alive is about to end that
    same process in the release step after the fold; it ends it here instead,
    before the observation, so the bounded settle can fold the terminal record
    the writer flushes on shutdown rather than a file mtime. Gated exactly like
    the release's own signal — a fresh terminal manifest and a live process —
    so a promotion that would not have signalled it (a run with no manifest, or
    a launch this settle never applies to) leaves the writer untouched and the
    observation takes its pre-existing live-process shape. Returns whether the
    writer was ended and the observation should therefore settle regardless.
    """
    if str(record.get("launch") or "") != "cli":
        return False
    if not _release_terminal_manifest(record):
        return False
    pid = record.get("pid")
    if record_process_alive(record, process_alive) is not True:
        return False
    try:
        _signal_process_group(
            int(pid),
            record.get("pid_start_time"),
            run_dir=run_directory_of(record),
            reason="promotion-settle",
        )
    except (ProcessLookupError, PermissionError, OSError, CrewError):
        return False
    return True


def _promotion_terminal_observation(
    record: Mapping[str, Any], *, settle_even_if_alive: bool = False
) -> StreamMeasures:
    """Read a finished run's stream after a bounded settle for its terminal tail.

    A worker writes its manifest before the harness reaches its terminal turn
    record, so a prompt promotion can read the stream in that gap and fold a
    completion taken from a file mtime — losing the run's own timing and token
    figures from the ledger. Once the run's process has exited the writer can
    only be flushing, so promotion waits out a short quiescence of the stream
    and re-reads it. A process still alive is normally never waited on: its
    stream is legitimately mid-write, and its behaviour here is unchanged. The
    exception is a writer this promotion has already ended (settle_even_if_alive)
    — signalling it means its tail is on its way, so waiting is both safe and
    bounded. A stream that never receives a terminal record still folds the
    mtime fallback, and the bounded wait guarantees a truncated stream cannot
    hang a promotion.
    """
    if str(record.get("launch") or "") != "cli":
        return _terminal_stream_data(record)
    if record_process_alive(record, process_alive) is True and not settle_even_if_alive:
        return _terminal_stream_data(record)
    path = Path(str(record.get("log_path") or ""))
    _wait_out_stream_tail(_run_streams(path))
    return _terminal_stream_data(record)


def _recoverable_session(record: Mapping[str, Any]) -> dict[str, str] | None:
    """The session a resume could still continue, and where it was found.

    The shared resolution consults the pointer, stream and promoted ledger.
    A pointer carrying no id cannot establish that the run is unresumable.
    """
    from reckon.crew.resumption import resolve_session

    resolution = resolve_session(
        str(record.get("run_id") or ""),
        record=record,
        project=str(record.get("project") or ""),
        root=record.get("repo"),
    )
    if not resolution["resolved"]:
        return None
    return {
        "session_id": str(resolution["session_id"]),
        "source": str(resolution["source"]),
    }


def _require_resume_waiver(
    run_id: str,
    *,
    verdict: str,
    waiver_reason: str,
    classification: str,
    recoverable_session: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Refuse a promotion that would delete a resume path, unless it is stated.

    Promotion removes the pointer, and the pointer is where a resume finds the
    session it continues. A run that stopped without finishing is exactly the
    one whose session is worth keeping — a provider refusal classifies as
    blocked and leaves a session holding every turn of the worker's
    orientation, which promotion then discards while reporting success.

    So the two facts are checked together, before anything irreversible runs: a
    blocked classification and a session either source can still reach. A
    passing gate is never touched, and neither is any other terminal state. A
    caller who genuinely wants the discard states why, and the reason lands on
    the ledger row so a deliberate one is afterwards distinguishable from an
    accident.
    """
    if verdict == "passed":
        return None
    if classification != "blocked":
        return None
    found = recoverable_session
    if found is None:
        return None
    reason = str(waiver_reason).strip()
    if reason:
        return {**found, "reason": reason}
    raise CrewError(
        f"run {run_id!r} is classified blocked and its session "
        f"{found['session_id']} is still recoverable from the {found['source']}, "
        "so promoting it would delete the only record a resume needs. Continue "
        f"it with `reckon crew resume --run {run_id} --advice <answer>`, or "
        "promote anyway with --waive-resume-path REASON stating why the "
        "session is being discarded"
    )


def _fresh_manifest(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Parse a run's manifest when it is present and fresh, else None.

    The two guards below key on what the worker wrote, so both must read the
    same file. A run with no manifest, or one whose manifest postdates the
    reason the run is being judged, is left to the arms that read its absence;
    an unparseable file is a delivery defect with its own refusal.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return None
    try:
        parsed = parse_manifest(
            Path(str(record["manifest_path"])).read_text(encoding="utf-8")
        )
    except (OSError, KeyError, ValueError):
        return None
    return dict(parsed)


def _manifest_repository_paths(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The paths a run's manifest declares inside the run's own repository.

    This is the question the review gate asks — did the run change the
    repository a reviewer would have to read? — answered from the same field
    the commit-for-changed-manifest guard reads, and by the same resolution
    rule, so the two refusals cannot disagree about what a run wrote. A
    manifest that names no path, or only paths outside the repository, is a
    run that changed nothing there.
    """
    manifest = _fresh_manifest(record)
    if manifest is None:
        return ()
    if _changed_paths_declare_no_paths(manifest, record, _manifest_text(record)):
        return ()
    return _changed_paths_inside_repository(manifest, record)


def _require_recognised_manifest_status(run_id: str, record: Mapping[str, Any]) -> None:
    """Refuse a promotion whose manifest carries no status the reader accepts.

    The status vocabulary is the reader's, not the worker's, and the review
    gate reaches a completed run only through the exact word ``complete``. So a
    worker that writes a plausible synonym — ``awaiting-orchestrator-review``,
    ``implemented-not-closed``, a bare ``done`` — exempts its own run from that
    review without any signal it has done so: the reader refuses the file, the
    classifier falls through to a reading keyed on a dead process, and the run
    then promotes as though its status had said something the reader accepts.

    The reader's own refusal already names the rejected word and the recognised
    vocabulary, so it is carried forward here rather than re-derived, and the
    run id is put in front of it so the refusal names the run it is about. A
    manifest that is absent or not fresh has no verdict to judge and is left to
    the arms that read its absence.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return
    try:
        text = Path(str(record["manifest_path"])).read_text(encoding="utf-8")
    except (OSError, KeyError):
        return
    try:
        parse_manifest(text)
    except ManifestParseError as refusal:
        raise ManifestParseError(
            f"run {run_id!r} cannot be promoted: {refusal}"
        ) from refusal


def _require_worker_stopped_before_promotion(
    run_id: str,
    record: Mapping[str, Any],
    *,
    waiver_reason: str,
) -> dict[str, str] | None:
    """Refuse promoting a run whose worker is still alive and not finished.

    Promotion deletes the live pointer, so a run promoted while its own process
    is still running carries on with no pointer, no follower row and no
    obligation to any coordinator — an orphaned process whose worktree cannot be
    reclaimed until it exits. The guard fires on the conjunction that makes that
    harm real: the recorded process is alive *and* this attempt has delivered no
    finished verdict of its own — it has written no manifest, its manifest
    states a status that is not terminal, or the terminal status on file belongs
    to an attempt the live one superseded. A run whose process has exited, or
    whose manifest is this attempt's own and reads complete, blocked or failed,
    promotes as before.

    A resumed attempt reuses its run directory, so the manifest beside the
    pointer may be the verdict a superseded turn left. Its own launch time is
    the fact that separates the two: a worker that started after the manifest
    was last written cannot have written it, so a terminal status still on the
    file states nothing about the attempt now running and the guard reads that
    attempt as unfinished. A worker started before the manifest keeps the
    reading its record already earns. The comparison is recovery's own, so the
    classifier that defers such a run and the gate that refuses to promote it
    cannot disagree about which attempt a manifest belongs to.

    A manifest that is absent, or older than the baseline this attempt began
    from, is no verdict on the work running now: the attempt has written
    nothing, so the guard reads it as unfinished for the same reason it reads a
    non-terminal status that way, and the live-run waiver is the only way to
    land it. A resumed attempt is the ordinary shape of that — its baseline is
    the inherited manifest's own mtime, so an inherited status is never fresh
    for it — and refusing there is the harm this guard exists for, because
    promotion would delete the live pointer under the worker the resume just
    started.

    ``waiver_reason`` is the operator's own statement of why the run may be
    promoted anyway, and it is recorded on the promoted row rather than erased.
    An unconditional waiver would stop meaning anything, so a waiver offered
    against a run with nothing to waive is itself refused.
    """
    from reckon.crew.recovery import _worker_launched_after_manifest

    manifest = _fresh_manifest(record)
    status = (
        "" if manifest is None else str(manifest.get("status") or "").strip().lower()
    )
    reason = str(waiver_reason).strip()
    superseded = (
        manifest is not None
        and status in TERMINAL_MANIFEST_STATUSES
        and _worker_launched_after_manifest(record, Path(str(record["manifest_path"])))
    )
    live = record_process_alive(record, process_alive) is True and (
        manifest is None or status not in TERMINAL_MANIFEST_STATUSES or superseded
    )
    if not live:
        if reason:
            raise CrewError(
                f"run {run_id!r} has no live, in-progress worker to waive for "
                f"--waive-live-run {reason!r}"
            )
        return None
    if reason:
        return {"reason": reason, "pid": str(record.get("pid")), "status": status}
    if manifest is None:
        reading = "no manifest written by this attempt is on file"
    elif superseded:
        reading = (
            f"its manifest's {status!r} status was written before this attempt was "
            "launched, so it reads a superseded attempt rather than the work running "
            "now"
        )
    else:
        reading = f"its manifest status is {status!r}"
    raise CrewError(
        f"run {run_id!r} cannot be promoted: its recorded worker process "
        f"{record.get('pid')} is still alive and {reading}. Promotion would "
        "delete the live pointer and orphan the worker. Wait for the process to "
        "exit, or state why it may land anyway with --waive-live-run REASON"
    )


def _capability_risk_of(capability: Any) -> str:
    """The risk a plan or section record declares, or empty when none does."""
    if not isinstance(capability, Mapping):
        return ""
    requirements = capability.get("requirements")
    if not isinstance(requirements, Mapping):
        return ""
    return str(requirements.get("risk") or "").strip()


def _run_capability_risk(
    record: Mapping[str, Any], *, root: str | Path | None
) -> str:
    """The capability risk the run's plan or its section declares.

    A section's own declaration is read first because a plan can carry a
    moderate risk overall while the one section a run lands against is where a
    guard or a fence lives, and a run's review is sized to the risk it actually
    touched. An elevated declaration at either level forces the fuller review,
    so the two are not averaged: whichever names an elevated risk wins.
    """
    state = _plan_state_for_run(record, fallback_root=root)
    if not state:
        return ""
    section_risk = ""
    wanted = section_record_id((record.get("node") or {}).get("section"))
    sections = state.get("sections")
    if isinstance(sections, (list, tuple)):
        for section in sections:
            if not isinstance(section, Mapping):
                continue
            if str(section.get("id") or "") == wanted:
                section_risk = _capability_risk_of(section.get("capability"))
                break
    plan_risk = _capability_risk_of(state.get("capability"))
    for risk in (section_risk, plan_risk):
        if review_tiers.elevated_risk(risk):
            return risk
    return section_risk or plan_risk


def _light_changed_line_ceiling(project: str, root: str | Path | None) -> int:
    """The light tier's changed-line ceiling from the resolved flight config.

    The threshold rides the ``review.tiers`` flight key, so a host or project
    layer retunes it without a code change. A config that cannot be resolved —
    a malformed host layer, an unreadable shipped default — falls back to the
    shipped ceiling rather than failing a promotion over a lookup.
    """
    config: Mapping[str, Any] | None
    try:
        config = flight.resolve(project or None, checkout_path=root).config
    except (flight.FlightConfigError, OSError, ValueError):
        config = None
    ceiling, _budget = flight.review_tier_thresholds(config)
    return ceiling


def _review_changed_scope(
    run_id: str,
    record: Mapping[str, Any],
    commit_list: Sequence[str],
) -> tuple[tuple[str, ...], int | None, bool]:
    """The paths a run changed, their changed-line count, and whether measured.

    Read the same way the ledger row's own scope is read: from each cited
    commit's own diff, so a head that merged the integration branch is not
    charged the branch's paths. A run that cites no commit — a report-only or
    review run — falls back to the repository paths its manifest declares, and
    its line count is left unmeasured, which the resolver reads as over the
    ceiling and so as the fuller review.

    The third element says whether the run's own declarations gave the tier
    anything to judge at all. A run that cites no commit and declares no path
    whatever has not said it changed nothing — it has said nothing, which is a
    different statement, and a reviewer cannot read a diff the run never named.
    Such a silent scope is reported unmeasured so the caller grants the fuller
    review rather than the lighter one. A record that names no readable tree
    measures no commit either: the diff belongs to the run's own tree, and the
    directory the promotion happens to run in cannot supply it.
    """
    tree = _record_tree(record)
    if commit_list:
        if tree is None:
            return (), None, False
        resolved = _resolve_commits(cwd=tree, revisions=commit_list, run_id=run_id)
        cumulative = _committed_scope(cwd=tree, commits=resolved, run_id=run_id)
        lines = cumulative.changed_lines
        changed_lines = (
            int(lines["added"]) + int(lines["removed"])
            if lines.get("available", True)
            else None
        )
        return cumulative.paths, changed_lines, True
    declared = _fresh_manifest(record)
    declares_paths = bool(
        declared
        and declared.get("changed_paths")
        and not _changed_paths_declare_no_paths(
            declared, record, _manifest_text(record)
        )
    )
    return _manifest_repository_paths(record), None, declares_paths


def _run_review_tier(
    run_id: str,
    record: Mapping[str, Any],
    *,
    commit_list: Sequence[str],
    root: str | Path | None,
) -> str:
    """Resolve this run's review tier from what it actually changed.

    The four inputs the tier is decided from are read here rather than passed
    in: the run's changed paths and their changed-line count at the promoted
    head, the specification level its node declares, and the capability risk
    its plan or section declares. The light ceiling is the resolved flight
    value, so the threshold is not a literal in this module.

    A run whose own declarations measure nothing is granted the fuller review
    rather than the lighter one: silence about what changed is not evidence that
    what changed was safe, and the tier resolver would otherwise read an empty
    path list as a run that touched no runtime source.
    """
    changed_paths, changed_lines, measured = _review_changed_scope(
        run_id, record, commit_list
    )
    if not measured:
        return review_tiers.FULL
    node = record.get("node") or {}
    return review_tiers.review_tier(
        changed_paths,
        changed_lines,
        str(node.get("spec_level") or ""),
        _run_capability_risk(record, root=root),
        light_changed_lines=_light_changed_line_ceiling(
            str(record.get("project") or ""), root
        ),
    )


def _require_review_waiver(
    run_id: str,
    record: Mapping[str, Any],
    *,
    verdict: str,
    classification: str,
    review: Mapping[str, Any] | None,
    review_action: str,
    waiver_reason: str,
    review_tier: str = "",
    promoted_head: str = "",
    stale_head: str = "",
    manifest_commits: Sequence[str] = (),
) -> dict[str, str] | None:
    """Refuse an unreviewed promotion of a run that owes a review.

    The gate follows what the run changed, not the role name: a passing run
    whose changes include runtime source has produced work a reviewer must
    read, whatever role carried it. The tier is computed from the run's own
    changed paths, changed-line count, declared spec level and declared
    capability risk, so a node that changes no runtime source — a test, plan,
    evidence, research-data or figure node — promotes unreviewed with its tier
    recorded on the row as the reason no review exists, while a runtime-source
    node is refused until a review is stored or a waiver states why it may land.
    The implement role is no longer singled out: a source-touching test or
    documentation node earns the same review the implement role does, and the
    tier is what separates them. The review role is exempt, because the review
    it wrote for another run is its own deliverable; requiring another review
    would recurse without a stopping point.

    The obligation is read from the delivery this promotion is proceeding on —
    a terminal manifest written for this attempt — and not from the live-pointer
    classification. The classifier's ``running`` arm means only that a live
    process could still supersede the manifest, so it defers the run's outcome;
    a delivered run whose worker has not stopped yet therefore classifies as
    neither ``scoring`` nor ``promotable``, and a gate that read the obligation
    off that classification disarmed itself for exactly those runs: they
    promoted unreviewed with no waiver, and a waiver offered for one was refused
    as a waiver of nothing.

    ``review`` is the record whose own comment says it read ``promoted_head``; a
    record of a different revision does not satisfy the gate. When such a record
    exists, its head arrives as ``stale_head`` so the refusal can name both
    revisions: an operator told only that no review is stored looks for a record
    that is already on disk, and one told which two revisions disagree knows the
    review must be recomposed against the new head.

    The classification alone cannot carry the decision, because the classifier
    reads the store without naming a revision and so counts a review of an
    earlier head as a complete review of the run. A parsed record at a different
    head is therefore promotable and unreviewed at once, and the head comparison
    — which only this gate makes — is what separates them.
    """
    from reckon.crew.recovery import (
        REVIEW_ROLE,
        _pointer_role,
        _review_dispatch_action,
    )

    role = _pointer_role(record)
    reason = str(waiver_reason).strip()
    delivered = _release_terminal_manifest(record)
    review_required = (
        classification == "scoring"
        or (classification == "promotable" and bool(stale_head))
        or delivered
    )
    # The tier, not the role, decides whether the run changed work a reviewer
    # owes. An unmeasured or unknown tier is treated as the fuller review, so a
    # caller that could not resolve one never opens a lighter path by silence.
    tier = str(review_tier or review_tiers.FULL)
    unreviewed = (
        verdict == "passed"
        and review_required
        and role != REVIEW_ROLE
        and tier != review_tiers.NONE
        and not (review and review.get("status") == "parsed")
    )
    if unreviewed:
        if reason:
            return {"reason": reason}
        if delivered and classification not in ("scoring", "promotable"):
            # A deferred delivery's classification names an action for the run's
            # own lifecycle — observe it, answer its blocker — rather than one
            # that produces a review. The refusal asks for a review, so it names
            # the review dispatch rather than sending the operator to watch a
            # run that has already delivered.
            review_action = _review_dispatch_action(record)
        # The operator who cites the revisions their manifest names is refused
        # because the stored review read a revision the list does not carry. The
        # refusal names that reviewed head as the value to cite, so the way to a
        # promotion is one flag rather than a search for which of two revisions
        # the store meant.
        cite_reviewed_head = ""
        if stale_head and promoted_head and not any(
            str(candidate).strip()
            and (
                str(candidate).strip() == stale_head
                or stale_head.startswith(str(candidate).strip())
                or str(candidate).strip().startswith(stale_head)
            )
            for candidate in manifest_commits
        ):
            cite_reviewed_head = stale_head
        raise CrewError(
            _unreviewed_refusal(
                run_id,
                review_action,
                promoted_head,
                stale_head,
                classification=classification,
                cite_reviewed_head=cite_reviewed_head,
            )
        )
    if reason:
        raise CrewError(
            f"run {run_id!r} has no unreviewed promotion for "
            f"--waive-unreviewed-promotion {reason!r} to waive"
        )
    return None


def _require_standing_suite(
    project: str,
    review_tier: str,
    root: str | Path | None,
) -> None:
    """Refuse a lighter promotion while the project's declared suite is held.

    A per-node gate runs only the tests a node's brief names, so a project whose
    default command stopped at collection can keep promoting unseen; the
    project's own suite is the check that sees the whole tree, and the lighter
    tiers wait on it. The tier is the one promotion has already resolved from
    what the run changed, so a ``full`` review -- which reads the run for
    itself -- is never held, and the tier is neither re-derived nor copied here.

    The reason comes from the project's recorded suite runs: the latest one
    failed to collect or overran its budget and no later waiver has lifted it.
    The refusal names that reason together with both ways out, so an operator is
    not left to guess which command answers the node. A run whose record names
    no project owns no declared suite and is not held.
    """
    if not project:
        return
    from reckon.crew import standing_suite

    reason = standing_suite.hold_reason(root, review_tier, project)
    if reason is None:
        return
    raise CrewError(
        f"standing suite holds this promotion: {reason}; record a passing run "
        f"with `reckon crew suite run --project {project}` or record a lead "
        f"waiver with `reckon crew suite waive --project {project} --reason TEXT`"
    )


class _PromotionRefusalError(CrewError):
    """One refusal carrying several failed preconditions.

    A sweep inside a nested helper raises this so an enclosing sweep can flatten
    the parts into its own ordered list: the message a caller reads is composed
    once, from every failure in the order the promotion checks them.
    """

    def __init__(self, refusals: Sequence[BaseException]) -> None:
        super().__init__(_combined_refusal_text(refusals))
        self.refusals = list(refusals)


def _combined_refusal_text(refusals: Sequence[BaseException]) -> str:
    """Render every failure, keeping the first one's wording verbatim first.

    A caller that matches on the leading refusal keeps matching, and the rest
    follow in the order the promotion checks them so the operator reads them in
    the sequence the code runs them.
    """
    first, *rest = refusals
    if not rest:
        return str(first)
    listed = "\n".join(f"  - {refusal}" for refusal in rest)
    return (
        f"{first}\n\nThis promotion also fails {len(rest)} further "
        f"precondition(s), in the order the promotion checks them:\n{listed}"
    )


def _require_independently(checks: Sequence[Callable[[], Any]]) -> list[Any]:
    """Run independent preconditions together, refusing once with every failure.

    Each check returns its product; every check runs even after an earlier one
    has refused, because the facts behind them are independent, and then one
    refusal carries all of them in the order given. A check whose inputs come
    from an earlier check's success is not listed as independent — the caller
    keeps it after this sweep, where it is judged only once its inputs hold.
    """
    refusals: list[BaseException] = []
    products: list[Any] = []
    for check in checks:
        try:
            products.append(check())
        except (CrewError, ledger.LedgerError) as refusal:
            products.append(None)
            if isinstance(refusal, _PromotionRefusalError):
                refusals.extend(refusal.refusals)
            else:
                refusals.append(refusal)
    if refusals:
        if len(refusals) == 1:
            raise refusals[0]
        raise _PromotionRefusalError(refusals)
    return products


def _require_gate_check_precondition(
    gate_check: Mapping[str, Any] | None,
    *,
    gate: str,
    require_gate_check: bool,
) -> None:
    """Refuse a passing gate with no check before the landing path runs.

    ``ledger.build_record`` enforces the same requirement as the backstop for
    every caller that assembles a record, so the wording is taken from that
    check and raised here as the same error: a promotion that fails only this
    precondition reports exactly what it reported before.
    """
    if not require_gate_check or str(gate).strip().lower() != "passed":
        return
    missing = ledger.gate_check_missing_fields(gate_check)
    if missing:
        raise ledger.LedgerError(
            "a passing gate requires the check that produced it; missing "
            + ", ".join(missing)
        )


def _unreviewed_refusal(
    run_id: str,
    review_action: str,
    promoted_head: str,
    stale_head: str,
    *,
    classification: str = "scoring",
    cite_reviewed_head: str = "",
) -> str:
    """State why an unreviewed promotion is refused, naming both revisions.

    A run promoted on the strength of a review of an earlier revision is the
    failure this gate exists for, and an operator who reads only "no review is
    stored" goes looking for a record that is already on disk. Naming the
    revision the promotion asserts beside the one the stored record read makes
    the repair obvious: the review must be recomposed against the new head.

    The classification is the one the refusal was reached under, so a delivery
    that owes a review while its process is still running is reported as the
    deferred run it is, rather than under the scoring word the gate's other arm
    usually reaches. It is stated beside the absent review rather than as its
    cause: a run is classified from its own record, so "classified running
    because no review is stored" would read as a claim that producing a review
    changes the classification the run already holds.
    """
    revision = (
        f"the stored review read revision {stale_head[:12]} and this promotion "
        f"asserts {promoted_head[:12]}: no review of the promoted revision is "
        "stored"
        if stale_head and promoted_head
        else "no complete independent review is stored"
    )
    citation = (
        " The manifest's own commit list predates the revision the review read: "
        f"pass --commit {cite_reviewed_head} to cite {cite_reviewed_head[:12]} "
        f"as the promoted revision, or recompose the review against "
        f"{promoted_head[:12]} and cite that"
        if cite_reviewed_head
        else ""
    )
    return (
        f"run {run_id!r} is classified {classification}; {revision}.{citation} "
        f"Produce it with `{review_action}`, or promote anyway with "
        "--waive-unreviewed-promotion REASON stating why this run may land "
        "without review"
    )


def _review_outcome_summary(stored: Mapping[str, Any]) -> str:
    """Summarise a stored review as the outcome a promotion records.

    The two figures are the ones a review run's own deliverable carries — the
    total all dimensions sum to and the count of findings — so the summary
    is derived from the record rather than restated by hand. A review whose
    score is withheld (an unparsed or dimension-incomplete record) has no total
    to name, and a summary that invented one would read as a measured score.
    """
    total = stored.get("total")
    findings = stored.get("findings")
    count = len(findings) if isinstance(findings, Sequence) else 0
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return f"review stored with no total score; {count} finding(s)"
    return f"review scored {int(total)}, {count} finding(s)"


def _resolve_promotion_outcome(
    run_id: str,
    record: Mapping[str, Any],
    *,
    verdict: str,
    outcome: str = "",
) -> str:
    """Return the outcome text a promotion records, defaulting a review run's.

    A non-passing gate must land with a summary of what failed or why the
    evidence could not be produced, so an empty outcome is refused. A review
    run's summary already exists in its deliverable, so the demand is met from
    the stored review's total score and finding count instead of requiring the
    operator to restate a figure the review store holds. Every other run still
    refuses, and a review run with no readable stored review refuses too,
    because a summary composed from nothing would read as evidence.
    """
    supplied = str(outcome).strip()
    if verdict == "passed" or supplied:
        return supplied
    from reckon.crew import recovery

    if not recovery._is_review_run(record):
        raise CrewError(
            "a non-passing gate requires --outcome; write what failed or why "
            "the evidence could not be produced"
        )
    project = str(record.get("project") or "")
    delivered = recovery._delivered_review_record(record, project)
    if delivered is None:
        raise CrewError(
            f"run {run_id!r} is a review run whose stored review cannot be "
            "read, so --outcome has no default to take; store the review or "
            "write what failed or why the evidence could not be produced"
        )
    _, path = delivered
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stored = None
    if not isinstance(stored, Mapping):
        raise CrewError(
            f"run {run_id!r} is a review run whose stored review at {path} "
            "does not parse, so --outcome has no default to take; store the "
            "review or write what failed or why the evidence could not be "
            "produced"
        )
    return _review_outcome_summary(stored)


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read one small JSON object from the run directory, or return nothing.

    The run directory is written by several processes and may be removed under
    promotion at any moment, so every read here is best-effort: a file that is
    absent, unparsable, or not an object yields an empty mapping rather than an
    exception. The reconstruction below only ever defaults a field from what
    survives, so a missing record degrades to the same empty field a pointer
    that never carried the value would.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _project_for_repository(repository: Path) -> str:
    """The mounted project whose docs tree lives in this repository, if one does.

    A run rebuilt from its directory never carried the project on a pointer, so
    the project is resolved from the same mounted-project map every other scope
    lookup consults, by repository identity so a linked worktree resolves to the
    same project as its main checkout.
    """
    target = repository.resolve()
    try:
        mounted = mounted_repository_projects()
    except (OSError, ValueError):
        return ""
    for repository_root, projects in mounted.items():
        if repository_root.resolve() == target and projects:
            return str(projects[0])
    return ""


def _rebuild_record_from_run_directory(
    run_id: str, *, root: str | Path | None
) -> dict[str, Any] | None:
    """Reconstruct a run's record from its surviving run directory.

    A run whose live pointer was removed — or never written — keeps its run
    directory: ``prompt.txt``, ``stderr.log`` and ``stream.jsonl`` survive
    beside the supervisor, worker and attempt records the launch wrote, and the
    durable manifest the worker delivered. That is enough to complete the run:
    the directory names the repository and worktree the supervisor was given,
    the manifest names the delivery, and the stream supplies the measurement.
    Everything the directory cannot supply is left empty rather than guessed,
    so the guards downstream read an absent field the way they read a pointer
    that never carried it.

    Returns ``None`` when no run directory survives, which is the caller's own
    "no live run" case rather than a reconstruction failure.
    """
    directory = run_dir(run_id)
    if not directory.is_dir():
        return None
    supervisor = _read_json_object(directory / "supervisor.json")
    worker = _read_json_object(directory / _WORKER_RECORD_NAME)
    attempt_record = _read_json_object(directory / _ATTEMPT_RECORD_NAME)
    repo = str(supervisor.get("repo") or (root if root is not None else "") or "")
    worktree = str(supervisor.get("worktree") or repo)
    manifest = directory / "manifest.md"
    manifest_path = str(manifest) if manifest.is_file() else ""
    project = _project_for_repository(Path(repo)) if repo else ""
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": project,
        "repo": repo,
        "worktree": worktree,
        "base_sha": "",
        "role": "",
        "launch": "",
        "created_at": str(attempt_record.get("attempt_started_at") or ""),
        "manifest_path": manifest_path,
        "node": {},
        "fenced": supervisor.get("fenced") is True,
        "rebuilt_from_run_directory": True,
    }
    # The pointer carries the run's assigned write scope, and a discard removes
    # it with the pointer. The durable manifest is the one scope declaration
    # that survives that removal: its ``changed_paths`` names the paths the run
    # says it delivered, so a rebuilt record presents that declaration in place
    # of the lost assignment. Without it a run citing its own commit is refused
    # for changing paths no surviving declaration contains, which is exactly the
    # discarded run that reached main through a follow-on.
    if manifest.is_file():
        try:
            manifest_data = parse_manifest(manifest.read_text(encoding="utf-8"))
        except (OSError, KeyError, ValueError):
            manifest_data = {}
        declared = list(_changed_paths_inside_repository(manifest_data, record))
        if declared:
            record["node"] = {"write_paths": declared}
    attempt = attempt_record.get("attempt") or worker.get("attempt")
    if attempt is not None:
        record["attempt"] = attempt
    attempt_kind = attempt_record.get("attempt_kind")
    if attempt_kind:
        record["attempt_kind"] = attempt_kind
    if "backend" in worker:
        record["backend"] = worker.get("backend")
    return record


def _read_pointer_or_rebuild(run_id: str, *, root: str | Path | None) -> dict[str, Any]:
    """A run's pointer when one survives, else its record rebuilt from disk.

    Promotion is the one command that must complete a run from whatever
    classification of it survives: a run whose pointer has been removed is
    exactly the run ``crew complete`` was refusing, and its run directory still
    holds the delivery. The pointer keeps first claim — it is the launch's own
    record — and the run directory is read only in its absence.
    """
    if pointer_path(run_id).exists():
        return read_pointer(run_id)
    rebuilt = _rebuild_record_from_run_directory(run_id, root=root)
    if rebuilt is None:
        raise CrewError(
            f"no live run {run_id!r} (looked in {pointer_path(run_id)}) and no "
            "run directory survives to rebuild it from"
        )
    return rebuilt


def _complete_withdrawn_run(run_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Report and retire a run that names no project.

    A dispatch refused before a project is resolved leaves a run with an empty
    project: the pointer never carried one, or the reconstruction of a
    pointerless run directory finds no supervisor record to read one from.
    There is no project ledger to append a row to, so ``crew complete`` reports
    the withdrawal and writes no row rather than failing validation on the empty
    project name.

    The withdrawal still retires what a promotion retires. A run whose pointer
    survives is otherwise left reading as in flight, and nothing reconciles it
    — so the pointer is removed and the workspace released through the same
    release path a promotion uses, without the ledger row a promotion's release
    receipt would need a project to hold. The run's own record is returned so a
    reader sees what survived of the launch; ``status`` and ``withdrawn`` both
    state the word the fleet vocabulary already uses for a departure that
    records no landing.
    """
    capture = _capture_member_session(record)
    pointer_existed = pointer_path(run_id).exists()
    pointer_path(run_id).unlink(missing_ok=True)
    release = _release_after_promotion(run_id, record)
    release.update(_retire_disposable_identity(record))
    return {
        "run_id": run_id,
        "project": "",
        "withdrawn": True,
        "status": "withdrawn",
        "promoted": False,
        "ledger_row_written": False,
        "pointer_removed": pointer_existed and not pointer_path(run_id).exists(),
        "reason": (
            "the run names no project, so its dispatch was refused before a "
            "project was resolved; the run is withdrawn, its pointer retired "
            "and its workspace released, and no ledger row is written"
        ),
        "release": release,
        "session_capture": capture,
        "record": dict(record),
    }


def _manifest_relative_path(value: Any, *, manifest_path: str) -> Path:
    """Resolve one manifest-cited path under the manifest's own directory.

    A worker writes its log paths relative to the run directory the manifest
    lives in, so a relative citation is anchored there rather than to whichever
    directory the promoting process happens to run in. An absolute path is
    taken as written.
    """
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and manifest_path:
        return Path(manifest_path).expanduser().parent / path
    return path


def _default_gate_evidence_from_manifest(
    record: Mapping[str, Any],
    *,
    verdict: str,
    gate_check: Mapping[str, Any] | None,
    commits: Sequence[str],
    no_commit_reason: str = "",
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Fill a passing promotion's gate flags from the worker's own manifest.

    A passing gate must carry the command that produced it, its exit status and
    its log, and a ledger row loses the work when a coordinator drops a commit
    the manifest already named. Reckon holds all of that the moment the worker
    delivers: the manifest states the gate command in ``tests``, the log path in
    ``test_logs``, its exit status on the log's own ``EXIT=`` line, and the
    commits in ``commits``. Each is taken only where the operator's flag left the
    value absent, so an explicit --gate-command or --commit always wins, and a
    manifest that states nothing leaves the guard's ordinary refusal untouched.
    Returns the gate-check mapping and the commit list unchanged for a
    non-passing gate, since neither is required there.
    """
    resolved_gate = dict(gate_check) if isinstance(gate_check, Mapping) else {}
    resolved_commits = tuple(str(sha).strip() for sha in commits if str(sha).strip())
    if str(verdict).strip().lower() != "passed":
        return resolved_gate, resolved_commits
    manifest = _fresh_manifest(record)
    if manifest is None:
        return resolved_gate, resolved_commits
    manifest_path = str(record.get("manifest_path") or "")
    if not str(resolved_gate.get("command") or "").strip():
        command = str(manifest.get("tests") or "").strip()
        if command:
            resolved_gate["command"] = command
    if not str(resolved_gate.get("log_path") or "").strip():
        logs = [
            str(entry).strip()
            for entry in (manifest.get("test_logs") or [])
            if str(entry).strip()
        ]
        if logs:
            resolved_gate["log_path"] = str(
                _manifest_relative_path(logs[0], manifest_path=manifest_path)
            )
    if resolved_gate.get("exit_status") is None:
        log_path = str(resolved_gate.get("log_path") or "").strip()
        recorded = None
        if log_path:
            try:
                log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                log_text = ""
            recorded = _recorded_exit_status(log_text)
        if recorded is not None:
            resolved_gate["exit_status"] = recorded
    if not resolved_commits and not str(no_commit_reason).strip():
        declared = [
            str(sha).strip()
            for sha in (manifest.get("commits") or [])
            if str(sha).strip()
        ]
        if declared:
            resolved_commits = tuple(declared)
    return resolved_gate, resolved_commits


def complete(
    run_id: str,
    *,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
    no_commit: str = "",
    suite_delta_waiver: str = "",
    boundary_waiver: str = "",
    resume_waiver: str = "",
    review_waiver: str = "",
    negative_control_waiver: str | None = None,
    discard_resume_worktree: bool = False,
    accepted_paths: Mapping[str, str] | None = None,
    no_impl_change: str = "",
    live_run_waiver: str = "",
    plan_link: str = "",
    unplanned_reason: str = "",
    promoted_by: str = "",
) -> dict[str, Any]:
    """Promote a run, or finish cleanup when its record already landed."""
    verdict = str(gate).strip().lower()
    if verdict not in ledger.GATE_VERDICTS:
        raise ledger.LedgerError(
            f"gate verdict {gate!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; a gate whose evidence could "
            "not be produced is 'not-run'"
        )
    classification = str(failure_classification).strip().lower()
    if verdict == "failed" and classification not in ledger.FAILURE_CLASSIFICATIONS:
        raise CrewError(
            "a failing gate requires --failure-classification from: "
            + ", ".join(ledger.FAILURE_CLASSIFICATIONS)
        )
    if verdict != "failed" and classification:
        raise CrewError("--failure-classification is valid only when --gate failed")
    commit_list = tuple(str(sha) for sha in commits if str(sha).strip())
    with _pointer_lock(run_id):
        record = _read_pointer_or_rebuild(run_id, root=root)
        # A dispatch whose launch was refused leaves a run that names no
        # project: the refusal removed the pointer or never let it carry a
        # project, and the run directory it left behind holds no supervisor
        # record either, so reconstruction resolves no project from it. There
        # is nothing to promote and no project ledger to hold a row, so the run
        # is reported as the withdrawal it is rather than failing validation on
        # the empty project name further down.
        if not str(record.get("project") or "").strip():
            return _complete_withdrawn_run(run_id, record)
        # A review run's outcome is the review it stored, so the operator's
        # hand is not the only source for a non-passing gate's summary; the
        # refusal below stands for every run with no stored review to read.
        outcome = _resolve_promotion_outcome(
            run_id, record, verdict=verdict, outcome=outcome
        )
        # A promotion writes the ledger row into the project's own mount, so
        # the repository is resolved from that mount before anything is
        # written: a checkout named from elsewhere is refused, and a run whose
        # record names none is given the mount rather than the caller's
        # enclosing repository. A project with no mount keeps the caller's
        # checkout, which is the only root available to it. Judged ahead of the
        # per-run evidence so a repository defect is reported as itself rather
        # than as a missing commit further down.
        landing_project = str(record.get("project") or "")
        # Gate command, exit status, log path and commits default to what the
        # worker's manifest already states, so a passing run whose evidence is
        # on disk promotes without the coordinator retyping figures the manifest
        # holds. Only absent values are filled: an operator's own flag always
        # wins, and a manifest that states nothing leaves the guard's ordinary
        # refusal in place.
        gate_check, commit_list = _default_gate_evidence_from_manifest(
            record,
            verdict=verdict,
            gate_check=gate_check,
            commits=commit_list,
            no_commit_reason=no_commit,
        )
        # Every precondition that can be judged on its own is judged here, in
        # the order the promotion checks them, and reported in one refusal: a
        # coordinator learns every way the record falls short from one call
        # rather than one per attempt. The resolutions a gate needs are read
        # inside that gate's own check, so a refusal never leaves a later check
        # reading a half-built value.
        resolved: dict[str, Any] = {}

        def _repository_root() -> str | Path | None:
            if landing_project and project_mount_repository(landing_project) is not None:
                resolved["root"] = resolve_project_repository(
                    landing_project, root, flag="--checkout-path"
                )
            else:
                resolved["root"] = root
            return resolved["root"]

        def _review_gate() -> dict[str, str] | None:
            from reckon.crew.recovery import classify_pointer

            classified = classify_pointer(record)
            resolved["classified"] = classified
            resolved["classification"] = str(classified.get("classification") or "")
            # The tier and the promoted revision read the commits the run
            # presents, so a commitless declaration is read as none here too:
            # the run changed nothing in the repository, and its tier is
            # resolved from what its manifest declares rather than from a
            # sentence nothing can resolve.
            gate_commits = _presented_commits_without_a_declaration(record, commit_list)
            # The revision this promotion asserts, resolved before the gate
            # reads the store, so a review of an earlier revision is refused
            # rather than accepted as evidence about code the repair has
            # already moved past.
            resolved["promoted_revision"] = _run_promoted_revision(record, gate_commits)
            reviewed, stale_review_head = _review_for_promotion(
                landing_project,
                run_id,
                promoted_revision=resolved["promoted_revision"],
                tree=_record_tree(record),
            )
            resolved["reviewed"] = reviewed
            # The tier is computed before the gate reads it, from what the run
            # actually changed at the promoted head, so the refusal and the row
            # it would have written agree about which review the run owes.
            resolved["review_tier"] = _run_review_tier(
                run_id,
                record,
                commit_list=gate_commits,
                root=resolved.get("root", root),
            )
            return _require_review_waiver(
                run_id,
                record,
                verdict=verdict,
                classification=resolved["classification"],
                review=reviewed,
                review_action=str(classified.get("next_action") or ""),
                waiver_reason=review_waiver,
                review_tier=resolved["review_tier"],
                promoted_head=resolved["promoted_revision"],
                stale_head=stale_review_head,
                manifest_commits=commit_list,
            )

        def _resume_gate() -> dict[str, str] | None:
            classified = resolved.get("classified")
            if not isinstance(classified, Mapping):
                classified = {}
            classification_name = str(resolved.get("classification") or "")
            candidate_remedy = classified.get("resume_remedy")
            resume_remedy = (
                dict(candidate_remedy) if isinstance(candidate_remedy, Mapping) else None
            )
            candidate_resolution = classified.get("session_resolution")
            if classification_name == "blocked" and isinstance(
                candidate_resolution, Mapping
            ):
                recoverable_session = (
                    {
                        "session_id": str(candidate_resolution["session_id"]),
                        "source": str(candidate_resolution["source"]),
                    }
                    if candidate_resolution.get("resolved")
                    else None
                )
            elif resume_remedy is not None:
                recoverable_session = {
                    "session_id": str(resume_remedy["session_id"]),
                    "source": str(resume_remedy["source"]),
                }
            else:
                recoverable_session = _recoverable_session(record)
            resolved["resume_remedy"] = resume_remedy
            resolved["recoverable_session"] = recoverable_session
            waived = _require_resume_waiver(
                run_id,
                verdict=verdict,
                waiver_reason=resume_waiver,
                classification=classification_name,
                recoverable_session=recoverable_session,
            )
            if discard_resume_worktree and waived is None:
                raise CrewError(
                    "discard_resume_worktree requires a reasoned resume waiver "
                    "for a recoverable non-passing run"
                )
            return waived

        def _landing_gate() -> dict[str, Any]:
            landing_root = resolved.get("root", root)
            if landing_root is None:
                landing_root = record.get("repo")
            checkout = (
                Path(landing_root).expanduser().resolve()
                if landing_root is not None
                else None
            )
            return _landing_preconditions(
                run_id,
                record,
                checkout=checkout,
                ledger_root=landing_root,
                commits=commit_list,
                gate=gate,
                failure_classification=classification,
                no_impl_change=no_impl_change,
                plan_link=plan_link,
                unplanned_reason=unplanned_reason,
                boundary_waiver=boundary_waiver,
                negative_control_waiver=negative_control_waiver,
                accepted_paths=accepted_paths,
                gate_check=gate_check,
                require_gate_check=require_gate_check,
            )

        (
            root,
            overridden_worktree_changes,
            _manifest_status,
            live_run_waived,
            _shadow_commit,
            commit_list_shortfall,
            _commits_beyond_base,
            _gate_log_agrees,
            _verdict_matches_exit_status,
            _runnable_gate_command,
            _executable_gate_command,
            review_waived,
            _standing_suite,
            resume_waived,
            landing,
        ) = _require_independently(
            [
                _repository_root,
                lambda: _require_commit_for_changed_manifest(
                    run_id, record, no_commit_reason=no_commit
                ),
                lambda: _require_recognised_manifest_status(run_id, record),
                # A run whose own worker is still alive and not finished is
                # refused before any store is written: deleting its live
                # pointer now would orphan the process until it exits on its
                # own. The waiver names the reason and rides the promoted row.
                lambda: _require_worker_stopped_before_promotion(
                    run_id, record, waiver_reason=live_run_waiver
                ),
                lambda: _refuse_commits_for_a_shadow(run_id, record, commit_list),
                lambda: _require_gate_evidence(
                    run_id,
                    record,
                    verdict=verdict,
                    commits=commit_list,
                    no_commit_reason=no_commit,
                ),
                # The citations themselves must be the run's own work, judged
                # before any store is written so a run that cannot be asserted
                # truthfully is refused with nothing landed and nothing to
                # unwind.
                lambda: _require_commits_beyond_base(run_id, record, commit_list),
                lambda: _require_gate_log_agrees(run_id, gate_check, verdict=verdict),
                # The verdict and the asserted status are two statements about
                # one run: a passing verdict beside a nonzero exit status is
                # refused from those two facts alone, whether or not the log
                # carries a terminal EXIT record of its own for the comparison
                # above to read.
                lambda: _require_verdict_matches_exit_status(
                    run_id, record, gate_check, verdict=verdict
                ),
                # The recorded command is what the integration re-run executes,
                # so a text that describes the command or names no program must
                # be refused here rather than land on a row that later reports a
                # failure the merge did not cause.
                lambda: _require_runnable_gate_command(run_id, gate_check),
                lambda: _require_executable_gate_command(run_id, gate_check),
                _review_gate,
                # The project's declared suite is the gate that sees the whole
                # tree, and the lighter promotions wait on it. The tier is the
                # one just resolved, so a full review -- which reads the run for
                # itself -- is never held.
                lambda: _require_standing_suite(
                    landing_project,
                    resolved.get("review_tier") or review_tiers.FULL,
                    resolved.get("root", root),
                ),
                _resume_gate,
                _landing_gate,
            ]
        )
        suite_delta = _evaluate_suite_delta(
            run_id,
            record,
            waiver_reason=suite_delta_waiver,
        )
        result = _complete_locked(
            run_id,
            record=record,
            gate=gate,
            failure_classification=classification,
            commits=commit_list,
            no_commit=no_commit,
            no_commit_uncommitted=overridden_worktree_changes,
            outcome=outcome,
            tests_added=tests_added,
            scope_changed=scope_changed,
            changed_lines=changed_lines,
            completed_at=completed_at,
            root=root,
            gate_check=gate_check,
            require_gate_check=require_gate_check,
            suite_delta=suite_delta,
            boundary_waiver=boundary_waiver,
            resume_remedy=resolved.get("resume_remedy"),
            resume_waived=resume_waived,
            reviewed=resolved.get("reviewed"),
            review_waived=review_waived,
            review_tier=resolved.get("review_tier") or "",
            negative_control_waiver=negative_control_waiver,
            recoverable_session=resolved.get("recoverable_session"),
            discard_resume_worktree=discard_resume_worktree,
            accepted_paths=accepted_paths,
            commit_list_shortfall=commit_list_shortfall,
            no_impl_change=no_impl_change,
            live_run_waived=live_run_waived,
            plan_link=plan_link,
            unplanned_reason=unplanned_reason,
            promoted_by=promoted_by,
            landing=landing,
        )
        if commit_list_shortfall is not None:
            result["commit_list_shortfall"] = dict(commit_list_shortfall)
        return result


def _normalized_command(value: Any) -> str:
    """Return a command with runs of whitespace collapsed to single spaces.

    Two arms that differ only in spacing exercise the same command, so the
    comparison against the record's armed command is made on the normalised
    spelling rather than the raw one.
    """
    return " ".join(str(value or "").split())


def _evaluate_suite_delta(
    run_id: str,
    record: Mapping[str, Any],
    *,
    waiver_reason: str,
) -> dict[str, Any] | None:
    """Validate an armed run's paired suite evidence and calculate its delta.

    An armed run that changed nothing against its base — a fresh manifest
    declaring no changed path and no commit beyond the base, over a worktree
    still sitting on that base — carries a delta of ``unchanged`` and needs no
    baseline and after pair. Every other run with missing suite evidence is
    refused as before, and an arm whose recorded command differs from the
    command the run was armed with measures a different suite, so it is
    refused as missing evidence too.
    """
    suite_command = str(record.get("suite_command") or "").strip()
    if not suite_command:
        return None
    manifest_present, fresh = _manifest_freshness(record)
    manifest: dict[str, Any] = {}
    if manifest_present and fresh:
        try:
            manifest = parse_manifest(
                Path(str(record["manifest_path"])).read_text(encoding="utf-8")
            )
        except (OSError, KeyError, ValueError):
            manifest = {}
    baseline = manifest.get("baseline_suite")
    after = manifest.get("after_suite")
    missing: list[str] = []
    if not manifest_present:
        missing.append("manifest")
    elif not fresh:
        missing.append("fresh_manifest")
    missing.extend(
        ledger.suite_observation_missing_fields(baseline, name="baseline_suite")
    )
    missing.extend(ledger.suite_observation_missing_fields(after, name="after_suite"))
    armed_command = _normalized_command(suite_command)
    for arm_name, observation in (
        ("baseline_suite", baseline),
        ("after_suite", after),
    ):
        if not isinstance(observation, Mapping):
            continue
        arm_command = str(observation.get("command") or "").strip()
        if arm_command and _normalized_command(arm_command) != armed_command:
            missing.append(f"{arm_name}.command_matches_suite_command")
    base_sha = str(record.get("base_sha") or "").strip()
    if (
        isinstance(baseline, Mapping)
        and str(baseline.get("revision") or "").strip()
        and str(baseline["revision"]).strip() != base_sha
    ):
        missing.append("baseline_suite.revision_matches_base_sha")
    if missing:
        if _run_changed_nothing(record):
            # An armed run that changed nothing has nothing for a suite pair to
            # measure: no baseline and after suite can differ over an empty
            # delta, so requiring the pair would refuse a run whose own tree
            # proves it is unchanged. The refusal below still fires for a run
            # that moved its head or declared a changed path.
            return {
                "status": "unchanged",
                "suite_command": suite_command,
                "added_failure_ids": [],
                "reason": (
                    "run changed nothing against its base: its fresh manifest "
                    "declares no changed path and no commit beyond the base, and "
                    "its worktree sits on the base with no tracked modification, "
                    "so no baseline and after suite pair is required"
                ),
            }
        refusal = {
            "status": "refused",
            "suite_command": suite_command,
            "missing_fields": missing,
            "added_failure_ids": [],
        }
        updated = dict(record)
        updated["suite_delta_refusal"] = refusal
        _write_json(pointer_path(run_id), updated)
        raise ledger.SuiteDeltaError(
            "armed promotion requires complete baseline and after suite evidence; "
            "missing " + ", ".join(missing),
            missing_fields=missing,
        )
    normalized_baseline = ledger.normalized_suite_observation(baseline)
    normalized_after = ledger.normalized_suite_observation(after)
    # "New" is defined exactly here: the single set-difference lives in
    # ledger.added_failure_ids, so the refusal and the per-failure attribution
    # share one notion of a newly added failure rather than a second expression
    # that could drift from it.
    added = ledger.added_failure_ids(normalized_baseline, normalized_after)
    # Surface the worker's attribution on the stored record: each added
    # failure id beside the candidate merged commit that introduced it.
    # An entry naming a pre-existing failure is dropped rather than stored.
    attribution = ledger.new_failure_attribution(
        baseline, after, manifest.get("failure_attribution")
    )
    if str(record.get("role") or "").strip() == "test":
        unattributed = sorted(set(added) - set(attribution))
        if unattributed:
            missing_attribution = [
                f"failure_attribution[{failure_id}]" for failure_id in unattributed
            ]
            refusal = {
                "status": "refused",
                "suite_command": suite_command,
                "baseline_suite": normalized_baseline,
                "after_suite": normalized_after,
                "missing_fields": missing_attribution,
                "added_failure_ids": added,
                "failure_attribution": attribution,
            }
            updated = dict(record)
            updated["suite_delta_refusal"] = refusal
            _write_json(pointer_path(run_id), updated)
            raise ledger.SuiteDeltaError(
                "test-role promotion requires a candidate commit for every "
                "added suite failure; missing " + ", ".join(missing_attribution),
                missing_fields=missing_attribution,
                added_failure_ids=added,
            )
    waiver = str(waiver_reason).strip()
    if added and not waiver:
        refusal = {
            "status": "refused",
            "suite_command": suite_command,
            "baseline_suite": normalized_baseline,
            "after_suite": normalized_after,
            "missing_fields": [],
            "added_failure_ids": added,
            "failure_attribution": attribution,
        }
        updated = dict(record)
        updated["suite_delta_refusal"] = refusal
        _write_json(pointer_path(run_id), updated)
        raise ledger.SuiteDeltaError(
            "armed promotion added suite failures: " + ", ".join(added),
            added_failure_ids=added,
        )
    return {
        "status": "waived" if added else "clean",
        "suite_command": suite_command,
        "baseline_suite": normalized_baseline,
        "after_suite": normalized_after,
        "added_failure_ids": added,
        "failure_attribution": attribution,
        "waiver_reason": waiver or None,
    }


def _release_terminal_manifest(record: Mapping[str, Any]) -> bool:
    """Return whether a terminal manifest was delivered for this attempt.

    A live process with no manifest at all is a recovery case, not a cleanup
    one, so signalling it here would race whatever is meant to observe it.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return False
    try:
        parsed = parse_manifest(
            Path(str(record["manifest_path"])).read_text(encoding="utf-8")
        )
    except (OSError, KeyError, ValueError):
        return False
    return str(parsed.get("status") or "").strip().lower() in {
        "complete",
        "blocked",
        "failed",
    }


def _resume_worktree_retention(
    record: Mapping[str, Any],
    recoverable_session: Mapping[str, str] | None,
    *,
    retained_at: str,
    discard: bool,
    gate: str,
) -> dict[str, str] | None:
    """Describe a worktree deliberately kept as a session's working directory.

    A completed run has closed its work and can release an integrated tree even
    when its session is still resolvable. Retention is for a promotion that did
    not close the work, or for a manifest that is not complete.
    """
    worktree = str(record.get("worktree") or "").strip()
    if recoverable_session is None or discard or not worktree:
        return None
    manifest = _fresh_manifest(record)
    complete_manifest = (
        manifest is not None
        and str(manifest.get("status") or "").strip().lower() == "complete"
    )
    if str(gate).strip().lower() not in {"blocked", "failed"} and complete_manifest:
        return None
    if not Path(worktree).is_dir():
        return None
    return {
        "classification": "retained-for-resume",
        "worktree": str(Path(worktree).resolve()),
        "session_id": str(recoverable_session["session_id"]),
        "session_source": str(recoverable_session["source"]),
        "retained_at": retained_at,
    }


def _worktree_audit(
    record: Mapping[str, Any],
    retention: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Audit the promoted run's worktree, preserving retention as its own state.

    Artifact safety and session recoverability answer different questions. A
    clean reachable tree is normally reclaimable, and an unintegrated tree is
    normally withheld for its commit. When the tree is a recoverable session's
    working directory, neither label states why it is present, so the audit
    carries the artifact classification separately and presents the retention
    as the operative classification.
    """
    repo_value = str(record.get("repo") or "").strip()
    worktree_value = str(record.get("worktree") or "").strip()
    if not repo_value or not Path(repo_value).is_dir() or not worktree_value:
        return {"counts": {}, "worktrees": []}
    repo = Path(repo_value).resolve()
    worktree = Path(worktree_value).resolve()
    claims = _live_worktree_claims().get(worktree, ())
    snapshot = _repository_tree_snapshot(repo, roots=(worktree,))
    retained_path = (
        Path(str(retention["worktree"])).resolve() if retention is not None else None
    )
    rows: list[dict[str, Any]] = []
    for tree_state in snapshot.get("trees") or ():
        if not tree_state.get("available"):
            rows.append(dict(tree_state))
            continue
        path = Path(str(tree_state["path"])).resolve()
        if path == repo:
            continue
        shadow_record = record if path == retained_path and _is_shadow(record) else None
        inspected = _inspect_workspace(
            repo,
            path,
            "HEAD",
            claims,
            shadow_record,
        )
        row = {**tree_state, **inspected}
        if retention is not None and path == retained_path:
            row["artifact_classification"] = row["classification"]
            row["classification"] = "retained-for-resume"
            row["retention"] = dict(retention)
            row["reclaimable"] = False
            row["withheld"] = (
                "this worktree is the working directory of recoverable session "
                f"{retention['session_id']}"
            )
        else:
            classification = str(row["classification"])
            row["reclaimable"] = classification in RECLAIMABLE_CLASSES
            if not row["reclaimable"]:
                row["withheld"] = WITHHELD_REASONS.get(
                    classification, "unrecognised classification"
                )
        rows.append(row)
    names = {
        "integrated",
        "disposable",
        "dirty",
        "unintegrated",
        "live-referenced",
        "retained-for-resume",
    }
    return {
        "counts": {
            name: sum(row.get("classification") == name for row in rows)
            for name in sorted(names)
        },
        "worktrees": rows,
    }


def _release_scratch_when_release_raised(record: Mapping[str, Any]) -> dict[str, Any]:
    """The scratch outcome when the rest of a release step raised.

    Scratch removal does not depend on the worktree, the process or the
    repository, so it is attempted even when the surrounding release raised:
    leaving the directory to leak because a git command failed is the very
    outcome this release step exists to prevent. A release that raises must
    still tell a reader, because a result carrying no scratch field cannot
    distinguish a directory that was removed from one that was left behind.
    The fallback never raises: it reports the reason instead.
    """
    try:
        return remove_worker_scratch(
            str(record.get("run_id") or ""),
            recorded_path=record.get("scratch"),
            budget_bytes=WORKER_SCRATCH_BUDGET_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 - the fallback must itself never raise
        return {
            "scratch_removed": False,
            "scratch_path": str(record.get("scratch") or "") or None,
            "scratch_withheld": f"scratch removal raised in the release fallback: {exc}",
            "scratch_bytes": None,
        }


def _manifest_reads_blocked(record: Mapping[str, Any]) -> bool:
    """Whether the run's manifest is a blocked delivery kept for resume.

    A blocked run stopped without finishing, so a resume may still continue the
    work. Its process group and its per-run identity are therefore part of what
    a resume finds, and the release keeps both rather than reclaiming them. The
    status is read from the delivered manifest, which is the same source the
    release already consults to decide whether a writer may be signalled.
    """
    manifest = _fresh_manifest(record)
    return (
        manifest is not None
        and str(manifest.get("status") or "").strip().lower() == "blocked"
    )


def _retire_disposable_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    """Remove a run's own disposable roster identity once its work is accepted.

    A dispatch that names no member carries a per-run identity and registers no
    roster row, so the ordinary run retires nothing here: there is no row to
    remove. The row is removed when one exists — a run hand-registered, or
    dispatched by an earlier revision — so acceptance leaves no disposable
    identity standing in the committed roster, which is the same guarantee the
    dispatch side gives by registering none.

    Only the run's own disposable identity is eligible. A run that names a
    member explicitly carries no disposable identity, so the release reports
    nothing about one and adds no key a reader could mistake for a retirement
    that was considered. A blocked run is kept: it may still be resumed, and its
    identity is part of what a resume finds.

    The removal runs after the release receipt is committed, so no later write
    amends a run file and leaves the aggregate behind; the roster write's own
    union read keeps the run-count guard satisfied and re-encodes every per-run
    file it copies, so the two copies stay identical.
    """
    run_id = str(record.get("run_id") or "")
    member_id = str(record.get("member") or "").strip()
    if not member_id or member_id != _disposable_member_id(run_id):
        return {}
    if _manifest_reads_blocked(record):
        return {
            "identity_retired": False,
            "identity_withheld": "manifest is blocked; identity kept for resume",
        }
    project = str(record.get("project") or "").strip()
    root = str(record.get("repo") or "").strip() or None
    if not project:
        return {
            "identity_retired": False,
            "identity_withheld": "the run names no project to retire the identity from",
        }
    try:
        for _attempt in range(8):
            data, version = ledger.load(project, root)
            if not any(str(entry.get("id")) == member_id for entry in data["members"]):
                return {
                    "identity_retired": False,
                    "identity_absent": True,
                    "identity_absent_member": member_id,
                }
            data["members"] = [
                entry
                for entry in data["members"]
                if str(entry.get("id")) != member_id
            ]
            try:
                ledger.write(
                    project,
                    data,
                    version,
                    root=root,
                    allow_member_removal=True,
                    commit=True,
                )
            except ledger.LedgerError:
                continue
            return {"identity_retired": True, "identity_member": member_id}
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask promotion
        return {
            "identity_retired": False,
            "identity_withheld": f"identity retirement raised: {exc}",
        }
    return {
        "identity_retired": False,
        "identity_withheld": "could not retire the disposable identity after retries",
    }


# ── Cited arm directories ───────────────────────────────────────────────────
#
# A worker's arms do not always land under its own scratch directory: a
# basetemp, a control tree or an extraction goes to whatever path its brief
# named, and the only record that knows the run owns it is the manifest that
# cites it. The release reads those citations, bounded to the node-local temp
# root the scratch root sits beneath, so a cited directory outside that root —
# or one another live run's pointer or manifest also cites — is reported and
# left in place rather than reached for.

_CITED_ABSOLUTE_PATH = re.compile(r"/(?:[^\s\"'`()\[\]{}<>,;]+)")
_CITED_PATH_EDGE_PUNCTUATION = ".,:;)]}\"'"
# The suite fields carry exactly one path each that the run owns: the log the
# suite wrote and the basetemp its own command was given. Every other token in
# a command — the interpreter, the repository, the test paths — belongs to
# somebody else's tree, and the basetemp is the only one a suite creates.
_BASETEMP_IN_COMMAND = re.compile(r"(?:--basetemp(?:=|\s+)|basetemp=)(\S+)")
_ARM_LOG_FIELDS = ("test_logs", "negative_control_log")
_ARM_SUITE_FIELDS = ("baseline_suite", "after_suite")
_ARM_ARTIFACT_FIELD = "artifacts"


def _string_leaves(value: Any) -> Iterable[str]:
    """Every string anywhere inside one manifest-shaped value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _string_leaves(item)


def _absolute_paths_in(text: str) -> list[Path]:
    """Every absolute path spelled in one field's value.

    Trailing sentence punctuation is stripped — a path cited inside a sentence
    ends at the path, not at the full stop after it — and a token that strips
    to the root is dropped, because removing / is never a reading of a citation.
    """
    found: list[Path] = []
    for match in _CITED_ABSOLUTE_PATH.findall(text):
        token = match.rstrip(_CITED_PATH_EDGE_PUNCTUATION)
        if len(token) > 1:
            found.append(Path(token))
    return found


def _suite_record(value: Any) -> Mapping[str, Any]:
    """A suite field as a mapping, however the manifest spelled it."""
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                return {}
            if isinstance(loaded, Mapping):
                return loaded
    return {}


def _artifact_candidates(artifacts: Any) -> list[Path]:
    """The paths an artifacts field declares, never the prose beside them.

    A manifest maps each artifact path to a free-text description of what the
    run did with it, and that description is prose: it can name another tree,
    another run's directory, or where a file was copied to. So a mapping
    contributes its keys — the declared paths — and never its values, and a
    list contributes its items, which are paths rather than descriptions.
    """
    found: list[Path] = []
    if isinstance(artifacts, Mapping):
        for key in artifacts:
            found.extend(_absolute_paths_in(str(key)))
        return found
    for text in _string_leaves(artifacts):
        found.extend(_absolute_paths_in(text.split(":", 1)[0]))
    return found


def _declared_arm_paths(declared: Mapping[str, Any]) -> list[Path]:
    """The paths a manifest declares as its own arms, basetemps and controls.

    Only the structured fields that carry a declared path are read: the two log
    fields, the suite records' own log and basetemp, and the artifacts field. A
    path that merely appears in prose is not a declaration of ownership, so a
    landing line, a checkpoint or a follow-on naming another session's
    directory is never a removal candidate. The run's own scratch directory and
    its timestamp-stemmed siblings are not read here either; the scratch
    removal that runs immediately before this step owns them.
    """
    found: list[Path] = []
    for field_name in _ARM_LOG_FIELDS:
        for text in _string_leaves(declared.get(field_name)):
            found.extend(_absolute_paths_in(text))
    found.extend(_artifact_candidates(declared.get(_ARM_ARTIFACT_FIELD)))
    for field_name in _ARM_SUITE_FIELDS:
        value = declared.get(field_name)
        for text in _string_leaves(_suite_record(value).get("log_path")):
            found.extend(_absolute_paths_in(text))
        # The basetemp is read from the field's whole text as well as from a
        # spelled-out record, because a manifest that writes its suite as one
        # line keeps the flag inside the command and names it nowhere else.
        for text in _string_leaves(value):
            for match in _BASETEMP_IN_COMMAND.finditer(text):
                found.extend(_absolute_paths_in(match.group(1)))
    return found


def _declared_manifest(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """A run's manifest as parsed fields, or an empty mapping when unreadable."""
    text = _manifest_text(record)
    if not text.strip():
        return {}
    try:
        declared = parse_manifest(text)
    except (ValueError, KeyError):
        return {}
    return declared if isinstance(declared, Mapping) else {}


def _arm_citations_of(pointer: Mapping[str, Any]) -> list[Path]:
    """Every path a live run's own record declares as one of its arms.

    Both halves of the record are read, because either may cite a tree: the
    pointer carries the scratch directory the launch was given, while the arms
    live in the manifest the pointer names. Nothing else on a pointer is read —
    its node brief and its prose fields are not declarations of ownership.
    """
    found = _declared_arm_paths(_declared_manifest(pointer))
    scratch = str(pointer.get("scratch") or "").strip()
    if scratch:
        found.extend(_absolute_paths_in(scratch))
    return found


def _other_live_run_citations(run_id: str) -> dict[Path, str]:
    """The paths every other live run cites, with the run that cites each."""
    citations: dict[Path, str] = {}
    for other in list_live():
        other_id = str(other.get("run_id") or "")
        if not other_id or other_id == run_id:
            continue
        for path in _arm_citations_of(other):
            citations.setdefault(path.resolve(), other_id)
    return citations


def remove_cited_arms(record: Mapping[str, Any], *, gate: str = "") -> dict[str, Any]:
    """Remove the directories a passing run's manifest declares as its arms.

    Only a run whose gate verdict passed has its arms cleared: a blocked or
    failed run's arms are the evidence a repair or a reviewer reads, so they
    are retained and the withheld verdict is recorded instead. The reach is
    bounded as well: a directory is removed only when it resolves under the
    node-local temp root, which is the parent of the run's scratch root — never
    when it is, contains, or lies inside the repository, the run directory or
    the scratch root, nor when any other live run's pointer or manifest cites
    it. A cited log inside a directory that is about to go is copied into
    ``<run directory>/cleared-arms`` first, so the evidence outlives the tree
    that held it; a log that cannot be copied holds its directory in place,
    because a removal that destroys evidence is worse than a tree left for a
    later sweep. Every removal and every keep is enumerated on the result;
    nothing is selected by a name pattern.
    """
    result: dict[str, Any] = {
        "arms_removed_paths": [],
        "arms_kept": [],
        "arms_logs_copied": [],
        "arms_withheld": "",
    }
    verdict = str(gate).strip().lower()
    if verdict != "passed":
        result["arms_withheld"] = (
            f"gate verdict {verdict or 'none'!r} is not passing; cited arms "
            "retained as evidence"
        )
        return result
    citations = _declared_arm_paths(_declared_manifest(record))
    scratch_root = worker_scratch_root().resolve()
    temp_root = scratch_root.parent
    run_dir_path = run_directory_of(record)
    protections: list[tuple[Path, str]] = []
    for label, value in (
        ("the run's repository", record.get("repo")),
        ("the run's worktree", record.get("worktree")),
    ):
        text = str(value or "").strip()
        if text:
            protections.append((Path(text).resolve(), label))
    if run_dir_path is not None:
        protections.append((Path(run_dir_path).resolve(), "the run directory"))

    cited_logs = [
        path for path in citations if not path.is_symlink() and path.is_file()
    ]
    live_citations = _other_live_run_citations(str(record.get("run_id") or ""))
    targets: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for path in citations:
        if path.is_symlink() or not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved == temp_root or not resolved.is_relative_to(temp_root):
            result["arms_kept"].append(
                {
                    "path": str(resolved),
                    "reason": f"outside the node-local temp root {temp_root}",
                }
            )
            continue
        if resolved.is_relative_to(scratch_root):
            # The run's own scratch subtree is cleared and recorded by the
            # release step that owns it, so its accounting stays in one place.
            continue
        protected_by = next(
            (
                label
                for root, label in protections
                if resolved == root or resolved.is_relative_to(root)
            ),
            "",
        )
        if protected_by:
            result["arms_kept"].append(
                {"path": str(resolved), "reason": f"inside {protected_by}"}
            )
            continue
        contains = next(
            (label for root, label in protections if root.is_relative_to(resolved)),
            "",
        )
        holder = next(
            (
                other_id
                for cited, other_id in live_citations.items()
                if resolved == cited or cited.is_relative_to(resolved)
            ),
            "",
        )
        if contains or holder:
            reason = f"cited by live run {holder}" if holder else f"contains {contains}"
            result["arms_kept"].append({"path": str(resolved), "reason": reason})
            continue
        targets.append((path, resolved))

    # Deepest first, so a cited arm nested inside another is copied and removed
    # on its own account before its parent's removal can take it silently.
    targets.sort(key=lambda item: len(item[1].parts), reverse=True)
    for path, resolved in targets:
        size = tree_size_bytes(path)
        copied: list[dict[str, str]] = []
        copy_failure = ""
        for cited in cited_logs:
            try:
                relative = cited.resolve().relative_to(resolved)
            except (OSError, ValueError):
                continue
            if run_dir_path is None:
                copy_failure = f"cited log {cited} has no run directory to survive in"
                break
            destination = run_dir_path / "cleared-arms" / resolved.name / relative
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cited, destination)
            except OSError as exc:
                copy_failure = (
                    f"cited log {cited} could not be copied into the run "
                    f"directory: {exc}"
                )
                break
            copied.append({"log": str(cited), "path": str(destination)})
        if copy_failure:
            result["arms_kept"].append({"path": str(resolved), "reason": copy_failure})
            continue
        print(f"removing cited arm directory {path} ({size} bytes)")
        try:
            shutil.rmtree(path)
        except OSError as exc:
            result["arms_kept"].append(
                {"path": str(resolved), "reason": f"removal failed: {exc}"}
            )
            continue
        result["arms_removed_paths"].append({"path": str(resolved), "bytes": size})
        result["arms_logs_copied"].extend(copied)
        print(f"removed cited arm directory {path}")
    return result


def _release_run_workspace(
    record: Mapping[str, Any],
    retention: Mapping[str, str] | None = None,
    *,
    process_already_ended: bool = False,
    release_worktree: bool = True,
    worktree_withheld: str = "",
    keep_process: bool = False,
    gate: str = "",
) -> dict[str, Any]:
    """Release a promoted run's own worktree, process, and scratch directory.

    Scratch is removed unconditionally, because it is node-local and ephemeral
    and must not outlive the run that owns it; a worktree, by contrast, is only
    released when it is safe to remove. Reuses the classification `crew gc`
    already applies rather than writing a
    second policy: a worktree is released only when it is clean and its HEAD
    is an ancestor of the repository's integration branch, or when it is a
    shadow whose patch was already retained. Everything else is left in place
    and named with the condition that withheld it. Liveness is judged by the
    wide claim — every worktree a live pointer names, apart from the released
    run itself — so a peer parked on an external wait keeps the share of the
    tree its pointer still names. Called only after the
    ledger append and pointer delete already succeeded; any exception raised
    here is caught by the caller and folded into the result instead of being
    allowed to obscure those two writes.
    """
    result: dict[str, Any] = {"worktree_released": False, "process_signalled": False}

    worktree_value = str(record.get("worktree") or "")
    repo_value = str(record.get("repo") or "")
    worktree = Path(worktree_value) if worktree_value else None
    repo = Path(repo_value) if repo_value else None
    if not release_worktree:
        result["worktree_withheld"] = worktree_withheld or (
            "promotion did not pass; worktree retained for recovery"
        )
    elif retention is not None:
        result["worktree_withheld"] = (
            "retained as the working directory of recoverable session "
            f"{retention['session_id']}"
        )
        result["worktree_retention"] = dict(retention)
    elif worktree is None:
        result["worktree_withheld"] = "no worktree recorded for this run"
    elif not worktree.is_dir():
        result["worktree_withheld"] = "tree is no longer available"
    elif repo is None or not repo.is_dir():
        result["worktree_withheld"] = "repository root is unavailable"
    else:
        run_id = str(record.get("run_id") or "")
        # The wide claim, not the phase-gated one: a peer parked on an external
        # wait carries a terminal-looking phase while its pointer still names
        # this tree, and a tree another live pointer names is not released.
        claims = [
            claim
            for claim in _live_pointer_worktrees().get(worktree.resolve(), [])
            if claim != run_id
        ]
        shadow_record = record if _is_shadow(record) else None
        inspected = _inspect_workspace(repo, worktree, "HEAD", claims, shadow_record)
        classification = str(inspected["classification"])
        if classification not in RECLAIMABLE_CLASSES:
            result["worktree_withheld"] = WITHHELD_REASONS.get(
                classification, "unrecognised classification"
            )
        else:
            force = classification == "disposable"
            removal = _git(
                repo,
                "worktree",
                "remove",
                *(("--force",) if force else ()),
                str(worktree),
                check=False,
            )
            if removal.returncode or worktree.is_dir():
                result["worktree_withheld"] = (
                    removal.stderr.strip()
                    or removal.stdout.strip()
                    or "worktree remove did not report success"
                )
            else:
                _git(repo, "worktree", "prune", check=False)
                result["worktree_released"] = True

    pid = record.get("pid")
    if keep_process:
        # A blocked run may still be resumed, so its process group is left
        # standing rather than reclaimed: the resume continues the process the
        # block interrupted, and signalling it here would end what the resume
        # was going to continue.
        result["process_withheld"] = "manifest is blocked; process kept for resume"
    elif not _release_terminal_manifest(record):
        result["process_withheld"] = "no terminal manifest was delivered"
    elif record_process_alive(record, process_alive) is not True:
        if process_already_ended:
            # The promotion ended this writer before the fold so its stream
            # could settle; the release reports that it signalled, and when,
            # rather than that the process is now merely absent.
            result["process_signalled"] = True
            result["process_withheld"] = "process ended by promotion before the fold"
        else:
            result["process_withheld"] = "process is not alive"
    else:
        try:
            _signal_process_group(
                int(pid),
                record.get("pid_start_time"),
                run_dir=run_directory_of(record),
                reason="promotion-release",
            )
        except (ProcessLookupError, PermissionError, OSError, CrewError) as exc:
            result["process_withheld"] = f"could not signal pid {pid} — {exc}"
        else:
            result["process_signalled"] = True
            # The pid rides the release result so the record names the process
            # that was stopped rather than only that some process was signalled.
            result["process_stopped_pid"] = int(pid)

    result["worktree_audit"] = _worktree_audit(record, retention)
    # The scratch directory a run owned dies with it. Both promotion and
    # discard funnel through this release step, so the removal is wired once
    # here and the two cannot disagree about whether it happens. It is removed
    # whatever the worktree verdict: scratch is node-local and ephemeral by
    # design, and a run that kept its scratch after its worktree was withheld
    # would leak the very entries this step exists to reclaim.
    result.update(
        remove_worker_scratch(
            str(record.get("run_id") or ""),
            recorded_path=record.get("scratch"),
            budget_bytes=WORKER_SCRATCH_BUDGET_BYTES,
        )
    )
    # Cited arms outside the scratch directory are cleared after that step, so
    # what the scratch removal already took — and recorded — is not reported a
    # second time as a cited arm that is no longer present. Only a passing gate
    # clears them: a blocked or failed run's arms are the evidence a repair or
    # a reviewer reads, and the scratch removal above still runs, because
    # scratch is node-local space rather than evidence.
    result.update(remove_cited_arms(record, gate=gate))
    return result


def _release_after_promotion(
    run_id: str,
    record: Mapping[str, Any],
    retention: Mapping[str, str] | None = None,
    *,
    process_already_ended: bool = False,
    gate: str = "",
) -> dict[str, Any]:
    """Release what promotion made transient, never at the cost of the ledger.

    A failure here — a git command that raises, a permission error signalling
    a process — must never read as a failed promotion: the ledger row and the
    pointer deletion that precede this call have already succeeded, and this
    step is strictly additional cleanup on top of them. process_already_ended
    records a writer the promotion ended before the fold, so the release can
    report that outcome instead of a process that is merely absent.
    """
    verdict = str(gate).strip().lower()
    blocked_for_resume = _manifest_reads_blocked(record)
    if verdict in {"blocked", "failed"}:
        try:
            return _release_run_workspace(
                record,
                retention,
                process_already_ended=process_already_ended,
                release_worktree=False,
                worktree_withheld=(
                    f"gate verdict {verdict!r} is not passing; worktree retained "
                    "for recovery"
                ),
                keep_process=blocked_for_resume,
                gate=verdict,
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask promotion
            fallback = {
                "worktree_released": False,
                "process_signalled": False,
                "worktree_withheld": f"run {run_id!r} release step raised: {exc}",
            }
            fallback.update(_release_scratch_when_release_raised(record))
            return fallback
    try:
        return _release_run_workspace(
            record,
            retention,
            process_already_ended=process_already_ended,
            keep_process=blocked_for_resume,
            gate=verdict,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask promotion
        fallback = {
            "worktree_released": False,
            "process_signalled": False,
            "worktree_withheld": f"run {run_id!r} release step raised: {exc}",
        }
        fallback.update(_release_scratch_when_release_raised(record))
        return fallback


def _update_run_record(
    path: Path, run_id: str, changes: Mapping[str, Any]
) -> dict[str, Any]:
    """Amend only this run's record through the canonical serialiser."""
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("run_id") != run_id:
        raise ledger.LedgerError(f"run {run_id!r} does not match {path}")
    updated = {**record, **changes}
    from reckon._store import write_atomically

    write_atomically(
        path, lambda stream: stream.write(ledger.serialize_run(updated)), mode=0o600
    )
    return updated


def _record_release_on_ledger(
    *,
    project: str,
    root: str | Path | None,
    run_id: str,
    release: Mapping[str, Any],
    checkout: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Persist the release decision beside the promoted run.

    Cleanup happens after the first landing commit so a removal failure cannot
    erase the evidence. This second, narrow ledger write makes the cleanup
    outcome durable as well: later readers can distinguish a removed tree from
    one retained for a dirty, unintegrated, live-referenced, or failed run.
    Its separate commit preserves the landing's identity even if a peer has
    already published it or committed other work before release finishes.
    """
    path = ledger.run_path(project, run_id, root)
    per_run = path.is_file()
    if not per_run:
        path = ledger.ledger_path(project, root)
    last_error: Exception | None = None
    for _attempt in range(12):
        try:
            if per_run:
                updated = _update_run_record(path, run_id, {"release": dict(release)})
            else:
                data, version = ledger.load(project, root=root)
                updated = None
                replaced = []
                for row in data.get("runs") or []:
                    candidate = dict(row)
                    if str(candidate.get("run_id") or "") == run_id:
                        candidate["release"] = dict(release)
                        updated = candidate
                    replaced.append(candidate)
                if updated is None:
                    return dict(release), None
                data["runs"] = replaced
                ledger.write(project, data, version, root=root)
            if checkout is not None:
                staged = _git(checkout, "add", "--", str(path), check=False)
                if staged.returncode:
                    rollback = _restore_landing_writes(checkout, [path])
                    raise _landing_refusal(
                        f"could not stage the release outcome for run {run_id!r}: "
                        f"{staged.stderr.strip() or staged.stdout.strip()}",
                        rollback,
                    )
                unchanged = _git(
                    checkout, "diff", "--cached", "--quiet", "--", str(path), check=False
                )
                if unchanged.returncode == 0:
                    return dict(release), updated
                committed = _git(
                    checkout,
                    "commit",
                    "--only",
                    "-m",
                    f"release({run_id}): record workspace outcome",
                    "-m",
                    "Record the worktree and process release receipt after promotion "
                    "without rewriting a commit another session may have published.",
                    "--",
                    str(path),
                    check=False,
                )
                if committed.returncode:
                    rollback = _restore_landing_writes(checkout, [path])
                    raise _landing_refusal(
                        f"could not commit the release outcome for run {run_id!r}: "
                        f"{committed.stderr.strip() or committed.stdout.strip()}",
                        rollback,
                    )
        except (ledger.LedgerError, CrewError, OSError, ValueError) as exc:
            last_error = exc
            continue
        return dict(release), updated

    recorded = dict(release)
    recorded["ledger_recorded"] = False
    if last_error is not None:
        recorded["ledger_error"] = str(last_error)
    return recorded, None


def _coordinator_supplied_predecessor(
    record: Mapping[str, Any],
) -> str | None:
    """Return the predecessor run id a coordinator attached to the record.

    The node definition is the coordinator-authored surface, so its
    ``predecessor`` value is read first; a top-level pointer field is the
    fallback a dispatch call could populate directly. Absent either, promotion
    derives the link from the repository base instead of asking a later reader
    to guess.
    """
    node = record.get("node")
    authored = node.get("predecessor") if isinstance(node, Mapping) else None
    value = authored if str(authored or "").strip() else record.get("predecessor")
    clean = str(value or "").strip()
    return clean or None


def _receipt_unmeasured_reason(value: object) -> str | None:
    """Return the stable reason carried by an unmeasured receipt value."""
    return value.value if isinstance(value, rollout.Unmeasured) else None


def _agent_model_identifier(record: Mapping[str, Any]) -> str | None:
    """Return the served model from the agent configuration the record holds."""
    agent = record.get("agent")
    if not isinstance(agent, Mapping):
        return None
    model = str(agent.get("model") or "").strip()
    return model or None


def _serialize_notional_figure(receipt: object) -> tuple[object, str | None]:
    """Serialize the receipt's notional figure and its unmeasured reason.

    The figure is the receipt's own, derived from declared rates in
    ``rollout._notional_price``; promotion never recomputes it.  A marked
    figure keeps the readable absence string in its field and the marker's
    reason in the unmeasured map, so a lane that never priced is
    distinguishable from a run that genuinely cost nothing.
    """
    value = getattr(
        receipt, "notional_cost_usd", rollout.Unmeasured.NO_MODEL_IDENTIFIER
    )
    reason = _receipt_unmeasured_reason(value)
    if reason is not None:
        return "unmeasured", reason
    return value, None


def _serialize_rate_basis(receipt: object) -> tuple[object, str | None]:
    """Serialize the receipt's rate basis, its date rendered as ISO text."""
    value = getattr(receipt, "rate_basis", rollout.Unmeasured.NO_MODEL_IDENTIFIER)
    reason = _receipt_unmeasured_reason(value)
    if reason is not None:
        return "unmeasured", reason
    return {
        "model_identifier": value.model_identifier,
        "input_per_million": value.input_per_million,
        "output_per_million": value.output_per_million,
        "as_of": value.as_of.isoformat(),
    }, None


def _harvest_lane_receipt(
    record: Mapping[str, Any],
    *,
    session_id: object,
    observed_at: str,
) -> dict[str, Any]:
    """Serialize the client-owned quota receipt at the promotion boundary."""
    # Do not source this from the delivered manifest: a run cannot independently
    # attest its own quota use. The harness-owned client receipt is evidence the
    # run did not author, even though adding a manifest field would look simpler.
    #
    # The served model flows through from the agent configuration the record
    # already holds, so the notional figure resolves without any new identity
    # lookup.  The receipt prices a lane the model names; a record carrying no
    # model keeps the explicit no-model marker rather than a fabricated figure.
    receipt = rollout.read_rollout_receipt(
        str(session_id or ""), model_identifier=_agent_model_identifier(record)
    )
    unmeasured: dict[str, str] = {}
    context_reason = _receipt_unmeasured_reason(receipt.model_context_window)
    if context_reason is None:
        effective_context_window: object = receipt.model_context_window
    else:
        effective_context_window = "unmeasured"
        unmeasured["effective_context_window"] = context_reason
    notional_cost, notional_reason = _serialize_notional_figure(receipt)
    basis, basis_reason = _serialize_rate_basis(receipt)

    result: dict[str, Any] = {
        "quota_state": "unmeasured",
        "observed_at": observed_at,
        "effective_context_window": effective_context_window,
        "quota_windows": [],
        "notional_cost_usd": notional_cost,
        "rate_basis": basis,
    }
    for reason_key, reason in (
        ("effective_context_window", context_reason),
        ("notional_cost_usd", notional_reason),
        ("rate_basis", basis_reason),
    ):
        if reason is not None:
            unmeasured[reason_key] = reason

    agent = record.get("agent")
    agent_backend = agent.get("backend") if isinstance(agent, Mapping) else ""
    backend = str(record.get("backend") or agent_backend or "")
    if ledger.is_unmetered_backend(backend):
        unmeasured["quota_windows"] = "unmetered"
        result["unmeasured"] = unmeasured
        return result

    readings = receipt.quota_readings
    readings_reason = _receipt_unmeasured_reason(readings)
    if readings_reason is not None:
        unmeasured["quota_windows"] = readings_reason
        result["unmeasured"] = unmeasured
        return result
    if not isinstance(readings, Mapping) or not readings:
        unmeasured["quota_windows"] = rollout.Unmeasured.NO_RATE_LIMIT_VALUE.value
        result["unmeasured"] = unmeasured
        return result

    windows: list[dict[str, Any]] = []
    for raw_window, reading in sorted(readings.items(), key=lambda item: int(item[0])):
        window_minutes = getattr(reading, "window_minutes", raw_window)
        used_percent = getattr(
            reading, "used_percent", rollout.Unmeasured.NO_RATE_LIMIT_VALUE
        )
        resets_at = getattr(
            reading, "resets_at", rollout.Unmeasured.NO_RATE_LIMIT_VALUE
        )
        row: dict[str, Any] = {
            "window_minutes": int(window_minutes),
            "used_percent": (
                "unmeasured"
                if _receipt_unmeasured_reason(used_percent) is not None
                else used_percent
            ),
            "resets_at": (
                "unmeasured"
                if _receipt_unmeasured_reason(resets_at) is not None
                else resets_at
            ),
            "observed_at": observed_at,
        }
        row_unmeasured = {
            key: reason
            for key, reason in (
                ("used_percent", _receipt_unmeasured_reason(used_percent)),
                ("resets_at", _receipt_unmeasured_reason(resets_at)),
            )
            if reason is not None
        }
        if row_unmeasured:
            row["unmeasured"] = row_unmeasured
        windows.append(row)

    result["quota_state"] = "measured"
    result["quota_windows"] = windows
    if unmeasured:
        result["unmeasured"] = unmeasured
    return result


def _unreconciled_live_runs(pointers: Iterable[Mapping[str, Any]]) -> int:
    """Count the live pointers no closure disposition excuses.

    Each pointer is classified by the closure drain's own per-pointer step,
    :func:`reckon.crew.runs._drain_row`, so this agrees with the drain's
    ``unreconciled_runs`` by construction: the drain builds its rows with the
    same function, and a change to the composition reaches both. Nothing is
    recomputed here, and the drain's plan inventory is not read — a promotion
    stamps a reading on its row and never consumes the drain's closure count or
    plan remainder.
    """
    return sum(1 for pointer in pointers if _drain_row(pointer)["unreconciled"])


# An unmeasured fleet reading names the exception that produced it, so a stale
# fixture or a real composition break is readable from the promotion result
# rather than swallowed into an absence with no cause. The bound keeps the
# stamped reading small: the promotion result lands on the committed row, and a
# traceback-length string would bloat it without adding signal.
_FLEET_UNMEASURED_CAUSE_LIMIT = 200


def _fleet_state_reading(project: str) -> dict[str, Any]:
    """Return a bounded current reading of the project's fleet state.

    The live-pointer projection owns the pointer classification, and the
    unreconciled count is derived from those pointers alone; promotion composes
    the already-derived facts into the result that an orchestrator is about to
    read. The reading deliberately stays outside the ledger because it describes
    the fleet at this moment, not this run.

    When the reading cannot be composed, at the per-pointer step that derives the
    unreconciled count or earlier when the pointers cannot be listed at all, the
    result is an unmeasured reading that still states the exception type and
    message under one named key, bounded in length; the exception never blocks
    landing, but it leaves a cause a reader can act on instead of a bare absence.
    Every unmeasured reading names the exception that produced it, whatever step
    failed, so a stale fixture and a real composition break are distinguishable
    from the promotion result alone.
    """
    observed_at = _utc_now()
    try:
        from reckon.crew import recovery

        pointers = list_live(project=project)
        classified = [recovery.classify_pointer(pointer) for pointer in pointers]
        actionable = [
            str(row.get("recovery_classification") or "")
            for row in classified
            if str(row.get("recovery_classification") or "")
            in recovery.ACTIONABLE_RECOVERY_CLASSIFICATIONS
        ]
        lanes = {
            str(pointer.get("backend") or "").strip()
            for pointer in pointers
            if str(pointer.get("backend") or "").strip()
        }
        try:
            unreconciled = _unreconciled_live_runs(pointers)
        except Exception as exc:  # noqa: BLE001 - a named cause never blocks landing
            return _unavailable_fleet_reading(observed_at, cause=exc)
        return {
            "fleet_state": "measured",
            "observed_at": observed_at,
            "live_runs": len(pointers),
            "unreconciled_runs": unreconciled,
            "actionable_runs": len(actionable),
            "actionable_classifications": sorted(set(actionable)),
            "occupied_lanes": len(lanes),
        }
    except Exception as exc:  # noqa: BLE001 - a named cause never blocks landing
        return _unavailable_fleet_reading(observed_at, cause=exc)


def _unavailable_fleet_reading(
    observed_at: str, cause: Exception | None = None
) -> dict[str, Any]:
    """Return the unmeasured fleet state reading, naming a cause when there is one.

    The reading states only what promotion observed, so a caller's reader sees
    the same unmeasured state whether or not a cause is carried; the cause, when
    given, is the exception type and message bounded to a readable length.
    """
    unmeasured = {"fleet_state": "unavailable"}
    if cause is not None:
        unmeasured["cause"] = f"{type(cause).__name__}: {cause}"[
            :_FLEET_UNMEASURED_CAUSE_LIMIT
        ]
    return {
        "fleet_state": "unmeasured",
        "observed_at": observed_at,
        "unmeasured": unmeasured,
    }


def _review_for_promotion(
    project: str,
    run_id: str,
    *,
    promoted_revision: str = "",
    tree: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Return this run's review of the promoted revision, and any other head.

    A review is evidence about a diff, and landing work invalidates it: once a
    repair lands, the stored verdict describes code that no longer exists, so a
    reader that takes the presence of a review as evidence about the code being
    promoted is reading a true statement that stopped being the one required.
    The record is therefore selected by the revision it recorded reading, not by
    which file the store happens to hold newest — and by the same selection the
    classifier uses, so a run does not read promotable and then refuse, or read
    scoring while a matching record sits on disk.

    The first element is the ledger-row block for that record — the shape that
    lands on the committed row, so the dimensions survive the loss of the crew
    configuration home. The second is the head a non-matching record did name,
    empty when none exists, so a refusal can name both revisions rather than
    report an absence. A legacy record naming no revision is reconstructed to
    the head its tree carried when it was written, and accepted only when that
    reconstructs to the promoted revision. A store that cannot be read yields an
    ``unreadable`` block — a distinct third state — so a later reader can tell
    an anomaly from a run that was simply promoted unreviewed, and from a parsed
    review whose dimensions measure zero.
    """
    from reckon.crew.recovery import same_revision, select_review_for_head

    try:
        stored, stale = select_review_for_head(
            project, run_id, promoted_revision, tree=tree
        )
    except (OSError, ValueError):
        return review_module.ledger_block({"status": "unreadable"}), ""
    # A refusal that names one revision for both the stored head and the
    # asserted head reads as no disagreement at all, so a head equal to the
    # promoted revision is never carried as the stale one.
    if stale and same_revision(stale, promoted_revision):
        stale = ""
    return review_module.ledger_block(stored), stale


def _record_tree(record: Mapping[str, Any]) -> Path | None:
    """The run's own checkout as this record names it, or None when it names none.

    A record names its worktree first and its repository second, because a
    released worktree is still readable through the repository that shares its
    object store. An empty field is absent: built naively, ``Path("")`` is
    ``Path(".")``, which is a directory, so a blank worktree field would resolve
    a promotion's revision and a review's reconstructed head from whatever
    repository the process happened to start in — a confident wrong answer
    rather than a missing one. A record that names no existing directory
    resolves no tree, and its reader reports the absence.
    """
    for key in ("worktree", "repo"):
        value = str(record.get(key) or "").strip()
        if value and (tree := Path(value)).is_dir():
            return tree
    return None


def _record_worktree(record: Mapping[str, Any]) -> Path | None:
    """The run's own worktree as this record names it, or None.

    The worktree-only sibling of :func:`_record_tree`. The repository is
    deliberately not a fallback here: a reader of this shape measures the tree
    the run *worked in*, and the repository is a different tree whose HEAD
    follows the integration branch, so a measurement taken there answers about
    work the run never did. An empty field is absent rather than the current
    directory, and a record naming no readable worktree resolves no tree at
    all, so its reader reports the absence.
    """
    value = str(record.get("worktree") or "").strip()
    if not value:
        return None
    tree = Path(value)
    return tree.resolve() if tree.is_dir() else None


def _tree_for_measurement(record: Mapping[str, Any], run_id: str) -> Path:
    """The run's own tree as its record names it, or a refusal naming absence.

    A citation is resolved, a commit diffed and a shadow patch measured in the
    tree the run's record names. A record that names no readable directory
    names no instrument at all, and the directory this promotion happens to run
    in cannot stand in for it: measuring against that ambient checkout would
    answer about a repository the run was never dispatched for, which is a
    confident wrong answer rather than a missing one.
    """
    tree = _record_tree(record)
    if tree is None:
        raise CrewError(
            f"run {run_id!r} names no readable worktree or repository, so the "
            "work it presents cannot be measured in the run's own tree"
        )
    return tree


def _run_promoted_revision(
    record: Mapping[str, Any], commit_list: Sequence[str]
) -> str:
    """Resolve the revision a promotion of this run asserts, from its own tree.

    The same reading the promoted row records, taken before the review gate
    reads the store so the gate compares against the revision this promotion
    will name rather than against whatever the store holds newest. Every cited
    commit is canonicalised as the row canonicalises it, so a citation that
    names the revision symbolically or in abbreviation still matches the full
    sha a review recorded reading, and the tip is selected by descent rather
    than by the position the citation was written in.

    A record that names no readable tree resolves no revision, and the empty
    string is returned: the revision a promotion asserts is a claim about the
    run's own work, and the directory this promotion happens to run in cannot
    make that claim.
    """
    tree = _record_tree(record)
    if tree is None:
        return ""
    return _promoted_revision(tree, commit_list)


def plan_impl_at(
    project: str,
    plan: str,
    root: str | Path | None,
) -> float | None:
    """Return a plan's persisted impl, or None when unset or unreadable.

    A plan that has never carried a ``plan-impl`` scalar reads as unset rather
    than as zero, so a promotion can tell "the plan never moved" from "the plan
    has no impl to compare".
    """
    if not project or not plan:
        return None
    try:
        state, _version = _store.read_plan(project, plan, root, artifact_type="plan")
    except (OSError, ValueError, _store.OpError):
        return None
    return _plan_impl_from_state(state)


def _plan_impl_from_state(state: Any) -> float | None:
    if not isinstance(state, Mapping) or state.get("type") != "plan":
        return None
    value = state.get("impl")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plan_state_for_run(
    record: Mapping[str, Any], *, fallback_root: str | Path | None
) -> dict[str, Any]:
    """Read the run's plan from the repository its dispatch authority names.

    The impl comparison must read the plan the run was dispatched against, so
    the authority's plan repository is preferred over the ledger checkout — a
    caller may point ``--checkout-path`` at another tree.
    """
    project = str(record.get("project") or "")
    plan = str((record.get("node") or {}).get("plan") or "")
    if not project or not plan:
        return {}
    root: str | Path | None = fallback_root
    authority = record.get("authority")
    if isinstance(authority, Mapping):
        plan_authority = authority.get("plan")
        if isinstance(plan_authority, Mapping) and plan_authority.get("repository"):
            root = str(plan_authority["repository"])
    try:
        state, _version = _store.read_plan(project, plan, root, artifact_type="plan")
    except (OSError, ValueError, _store.OpError):
        return {}
    return state if isinstance(state, Mapping) else {}


def _landed_sections(state: Mapping[str, Any]) -> set[str]:
    """Return the sections a landing comment already records.

    A promoted run appends one section comment under a run-derived id, so the
    presence of that id is the plan's own record that work landed against the
    section. A comment written for any other reason carries another id and
    says nothing about a landing. This records run outcomes, not document-card
    shape or section closure.
    """
    comments = state.get("comments")
    if not isinstance(comments, Mapping):
        return set()
    landed: set[str] = set()
    for raw_section, entries in comments.items():
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and _is_run_comment(entry.get("id")):
                landed.add(str(raw_section).strip())
                break
    return landed


def _plan_remaining_sections(state: Mapping[str, Any]) -> list[str]:
    """Return the plan's sections that still have work to land.

    A landing already recorded on a section is subtracted, so the refusal
    names work a reader can still pick up rather than a section that has been
    delivered. The schema predicate first selects declared work, which remains
    outstanding until reclassification regardless of landings; subtraction
    applies only to this pickup list. A plan that has not persisted a
    classification falls back to the section identities its gates and comment
    anchors name.
    """
    landed = _landed_sections(state)
    declarations = state.get("section_declarations")
    if isinstance(declarations, Mapping):
        return sorted(
            section
            for section, classification in declarations.items()
            if is_implementable_section(classification)
            and str(section).strip() not in landed
        )
    from reckon._schema import plan_section_anchors

    return sorted(plan_section_anchors(state) - landed)


_IMPL_MOVE_EXEMPT_CLASSIFICATIONS = frozenset({"negative-result", "correct-refusal"})
# A run that continues earlier work inherits that work's plan movement: the
# impl the plan gained belongs to the dispatch the retry corrects, so demanding
# it move again charges a second node for one advance.
_IMPL_MOVE_CORRECTIVE_ATTEMPT_KINDS = frozenset({"resume", "redispatch"})


def _require_brief_owner(
    run_id: str,
    record: Mapping[str, Any],
    *,
    plan_link: str,
    unplanned_reason: str,
) -> dict[str, Any] | None:
    """Refuse an implement-role brief run that names no owner for its change.

    A brief run has no plan section to move, so nothing joins a product change
    to the plan it belongs to unless the promotion says so. An implement-role
    brief run may land only with one of two discharges: ``--plan-link <slug>``
    naming the plan whose product it changed, or ``--unplanned-reason <text>``
    stating why it changed no plan. A non-implementing role needs neither, and a
    plan run records its owner through the plan itself. Both discharges name the
    flag a reader would pass, so the refusal is answered in one word of work.
    """
    node = record.get("node") or {}
    if not str(node.get("brief") or "").strip():
        return None
    if str(node.get("plan") or "").strip():
        # A brief beside a plan section is briefed plan work: the plan it
        # names is its owner, so no discharge is needed.
        return None
    role = str(record.get("role") or "")
    if role not in EXECUTABLE_SECTION_ROLES:
        return None
    link = str(plan_link).strip()
    reason = str(unplanned_reason).strip()
    if link or reason:
        return {"plan_link": link, "unplanned_reason": reason}
    raise CrewError(
        f"implement-role brief run {run_id!r} names neither a plan link nor an "
        "unplanned reason; pass --plan-link <slug> naming the plan whose "
        "product it changed, or --unplanned-reason <text> stating why it "
        "changed no plan"
    )


def _require_impl_moved(
    run_id: str,
    record: Mapping[str, Any],
    *,
    gate: str,
    failure_classification: str,
    no_impl_change: str,
    plan_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the plan's impl at promotion against the value at dispatch.

    Landing work is supposed to advance the plan it lands against, and nothing
    in the landing path made the plan move: plans sat at zero percent while
    nodes landed against them one after another, so this converts the habit
    into a check. Every exemption is named in the returned record, and the
    refusal names the flag that waives it so recording a reason is one word of
    work.
    """

    role = str(record.get("role") or "")
    node = record.get("node") or {}
    plan = str(node.get("plan") or "")
    recorded = record.get("plan_impl_at_dispatch")
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        recorded_value = float(recorded)
    else:
        recorded_value = None
    check: dict[str, Any] = {
        "plan": plan,
        "at_dispatch": recorded_value,
        "at_complete": None,
    }
    # A brief-only run names no plan section, so there is no plan impl to move
    # and nothing for this guard to read. The skip is named here rather than
    # left to the empty-plan branch so a reader of the row sees the run's
    # carrier as the reason, not an absent plan that might read as a defect. A
    # brief beside a plan section is plan work and its impl is expected to move.
    if str(node.get("brief") or "").strip() and not plan:
        check["verdict"] = "exempt"
        check["reason"] = "brief-names-no-plan"
        return check
    if role not in EXECUTABLE_SECTION_ROLES:
        check["verdict"] = "exempt"
        check["reason"] = f"role-not-enforced:{role or 'unknown'}"
        return check
    if str(failure_classification).strip().lower() in _IMPL_MOVE_EXEMPT_CLASSIFICATIONS:
        check["verdict"] = "exempt"
        check["reason"] = f"failure-classification:{failure_classification}"
        return check
    attempt_kind = str(record.get("attempt_kind") or "").strip().lower()
    if attempt_kind in _IMPL_MOVE_CORRECTIVE_ATTEMPT_KINDS:
        check["verdict"] = "exempt"
        check["reason"] = f"corrective-run:{attempt_kind}"
        return check
    # A dispatch may name the promoted run it repairs with ``--repairs``. The
    # movement that run produced belongs to the run being repaired, so the
    # same exemption the attempt-kind corrective forms carry applies, and the
    # repaired run is named so a reader can follow the chain.
    repaired_run = str(record.get("repairs") or "").strip()
    if repaired_run:
        check["verdict"] = "exempt"
        check["reason"] = f"corrective-run:repairs:{repaired_run}"
        return check

    if str(gate).strip().lower() != "passed":
        check["verdict"] = "exempt"
        check["reason"] = "gate-not-passing"
        return check
    if not plan:
        check["verdict"] = "exempt"
        check["reason"] = "node-names-no-plan"
        return check
    if not plan_state:
        check["verdict"] = "exempt"
        check["reason"] = "plan-unreadable"
        return check
    current = _plan_impl_from_state(plan_state)
    check["at_complete"] = current
    if recorded_value is None:
        # A run dispatched before this check existed carries no value to
        # compare; it is exempt rather than treated as a plan that never moved.
        check["verdict"] = "exempt"
        check["reason"] = "no-impl-recorded-at-dispatch"
        return check
    if current is None:
        check["verdict"] = "exempt"
        check["reason"] = "plan-records-no-impl"
        return check
    if current != recorded_value:
        check["verdict"] = "moved"
        return check
    if str(no_impl_change).strip():
        check["verdict"] = "waived"
        check["reason"] = str(no_impl_change).strip()
        return check
    remaining = _plan_remaining_sections(plan_state)
    listed = ", ".join(remaining) if remaining else "(none declared)"
    raise CrewError(
        f"run {run_id!r} promotes a passing {role} run, but plan {plan!r} impl "
        f"did not move: {recorded_value:g} at dispatch and {current:g} at "
        f"completion. Sections still to land: {listed}. Advance the plan's impl "
        f"as the work lands, or record why it did not move with "
        f"`reckon crew complete --run {run_id} --gate passed "
        f"--no-impl-change REASON` (the reason lands on the ledger row); if the "
        f"plan's impl is not this run's to move, state that reason"
    )


def _negative_control_log_text(
    log_path: str, *, manifest_path: str
) -> tuple[str | None, str]:
    """Read the red log a declaration names, resolving it against the manifest.

    A worker writes a path relative to the manifest it delivered, so a relative
    value is resolved there rather than against the promoting process's working
    directory, which would read as a missing file for every manifest on disk.
    """
    raw = str(log_path or "").strip()
    if not raw:
        return None, ""
    path = Path(raw).expanduser()
    if not path.is_absolute() and manifest_path:
        path = Path(manifest_path).expanduser().parent / path
    try:
        return path.read_text(encoding="utf-8"), str(path)
    except (OSError, UnicodeError):
        return None, str(path)


def _control_failure_ids(log_text: str) -> set[str]:
    """The failing tests a control log names, as canonical node ids.

    Only the ids the log enumerates are a fact: a runner's summary saying how
    many tests failed states that something did fail, not which test, and the
    comparison needs the identity rather than the tally. The log's own short
    summary marks them FAILED or ERROR, read through the review module's reader
    so both sides of the comparison are canonical.
    """
    return review_module._pytest_failure_ids(log_text)


def _baseline_suite_failure_ids(manifest: Mapping[str, Any] | None) -> set[str] | None:
    """The failing tests the manifest records for the baseline, or ``None``.

    The baseline is the fallback arm a control is compared against when the
    manifest records no readable head arm. A manifest that recorded no
    ``baseline_suite`` at all observed nothing, so every failure its control
    names is one the baseline does not.

    An arm that is recorded but does not declare its run complete is a
    different fact: the run may have been interrupted, so the ids it lists are
    not the set it failed and the arm says nothing about what it passed.
    Completion is asked the way the head arm asks it — a literal ``True``,
    never a truthy stand-in — so an arm that omits ``completed`` or carries
    null is unreadable here, and ``None`` says so. Reading such an arm as
    complete would either trust the ids of a half-run or, read as an empty set,
    admit every control it was meant to refuse. ``None`` therefore decides no
    control: the caller falls through to the head arm when one is readable, and
    refuses when neither is.

    The ``failure_ids`` the arm lists are read the way the head reader reads
    them: a list of non-empty strings. An unreadable shape is unreadable here
    too, for the same reason the completion key is — a value read as a set of
    ids but that is not one says nothing. A ``failure_ids`` recorded as a JSON
    string would otherwise iterate as its characters and admit a control the
    same manifest in list form refuses, and a non-iterable value would raise
    out of the gate; both return ``None``.
    """
    observation = None if manifest is None else manifest.get("baseline_suite")
    if not isinstance(observation, Mapping):
        return set()
    if observation.get("completed") is not True:
        return None
    failure_ids = observation.get("failure_ids")
    if not isinstance(failure_ids, list) or any(
        not isinstance(test_id, str) or not test_id.strip() for test_id in failure_ids
    ):
        return None
    return {review_module.canonical_node_id(test_id.strip()) for test_id in failure_ids}


def _head_suite_failure_ids(manifest: Mapping[str, Any] | None) -> set[str] | None:
    """The failing tests the manifest records for the head arm, or ``None``.

    The head arm is the run's own after measurement: the same suite over the
    tree the change landed in. It is the arm a control has to be compared
    against, because it answers the question the control exists to ask — does
    the mutation redden a test the changed code passes? The baseline answers a
    different one. A node whose tests were written before the repair has every
    new case failing at the base by design, so a baseline comparison refuses
    exactly the sound controls that redden those cases.

    ``None`` means no readable head arm is recorded: an absent ``after_suite``,
    an observation that does not declare its run complete, or one whose
    ``failure_ids`` cannot be read as a list of ids says nothing about what the
    head arm passed. Reading such an arm as failing nothing would admit every
    control, so the caller falls back to the baseline comparison instead.

    Completion is asked the way the strict arm validator and the manifest report
    ask it — a literal ``True``, never a truthy stand-in — so an arm that omits
    the key or carries null is unreadable here and decides nothing.
    """
    observation = None if manifest is None else manifest.get("after_suite")
    if not isinstance(observation, Mapping):
        return None
    if observation.get("completed") is not True:
        return None
    failure_ids = observation.get("failure_ids")
    if not isinstance(failure_ids, list) or any(
        not isinstance(test_id, str) or not test_id.strip() for test_id in failure_ids
    ):
        return None
    return {review_module.canonical_node_id(test_id.strip()) for test_id in failure_ids}


def _require_declared_negative_control(
    run_id: str,
    record: Mapping[str, Any],
    *,
    gate: str,
    manifest: Mapping[str, Any] | None,
    manifest_path: str,
    waiver_reason: str = "",
) -> dict[str, Any]:
    """Refuse a passing gate on a check whose red log shows nothing break.

    A node whose write paths include a test file declares the mutation that
    check must fail against. The declaration is discharged at promotion by a
    manifest that carries the path to the log that mutation produced: the pair
    of logs is the positive and negative control of one measurement.

    The log is judged on two facts about the run it captured, not on how it is
    worded: the run's terminal record exited non-zero, and the log names at
    least one failing test id the head arm does not fail. The head arm is the
    run's own after measurement, so a control that reddens a case written
    before the repair is admitted even though the baseline, taken over the
    unfixed tree, fails that case too; the baseline comparison decides only
    when the manifest records no readable head arm. An arm is readable only when
    it declares its run complete — a literal ``True``, never a truthy stand-in —
    so neither an ``after_suite`` nor a ``baseline_suite`` that omits the key or
    carries null decides anything, and when both are unreadable the control is
    refused rather than admitted against a comparison that was never made.
    Neither fact is inferred —
    a log with no exit record and a log naming no failing test id each state
    too little to admit the control, and a bare failing count is not
    evidence that either fact holds. Wording cannot carry either fact, so a
    declaration pasted into a log whose run exited zero is refused, and neither
    can a log whose run merely repeated the failures the compared arm already had.
    The declaration stays on the node record and on the verdict row beside the
    log path, so a person compares the two — the instrument for *is this the
    right mutation*, which no comparison of text can be. A declaration of
    ``none`` with its reason is an explicit escape rather than a silent one, so
    it is recorded on the row rather than refused.
    """

    check: dict[str, Any] = {"verdict": "exempt"}
    node = record.get("node") or {}
    if not isinstance(node, Mapping):
        check["reason"] = "node-writes-no-test-path"
        return check
    test_paths = sorted(
        str(path) for path in node.get("write_paths") or () if is_test_path(str(path))
    )
    if not test_paths:
        check["reason"] = "node-writes-no-test-path"
        return check
    check["test_paths"] = test_paths
    declaration = str(node.get(NEGATIVE_CONTROL_FIELD) or "").strip()
    if not declaration:
        # A run dispatched before this check existed carries no field to read;
        # it is exempt rather than treated as a node that declared nothing.
        check["verdict"] = "exempt"
        check["reason"] = "no-negative-control-declared"
        return check
    if str(gate).strip().lower() != "passed":
        check["verdict"] = "exempt"
        check["reason"] = "gate-not-passing"
        return check
    if negative_control_is_none(declaration):
        reason = negative_control_reason(declaration)
        if not reason:
            raise CrewError(
                f"run {run_id!r} declares its negative control as "
                f"{NEGATIVE_CONTROL_NONE!r} in the {NEGATIVE_CONTROL_FIELD} field "
                "without the reason it applies. A check that admits no applicable "
                f"mutation states so as `{NEGATIVE_CONTROL_NONE}: <reason>`, and "
                "the reason is what a later reader has to judge"
            )
        check["verdict"] = "none-recorded"
        check["declaration"] = declaration
        check["reason"] = reason
        return check

    check["declaration"] = declaration
    delivered = (
        "" if manifest is None else str(manifest.get("negative_control_log") or "")
    )
    check["log"] = delivered
    if not delivered:
        raise CrewError(
            f"run {run_id!r} writes a check ({', '.join(test_paths)}) and declares "
            f"the mutation {declaration!r} in its {NEGATIVE_CONTROL_FIELD} field, "
            "but its manifest carries no negative_control_log path. Promotion "
            "refuses a passing gate whose negative control was never run: keep the "
            "log that mutation produced beside the passing one and name its path "
            "in the manifest as `negative_control_log: <path>`"
        )
    text, resolved = _negative_control_log_text(delivered, manifest_path=manifest_path)
    check["resolved_log"] = resolved
    if text is None:
        raise CrewError(
            f"run {run_id!r} names negative_control_log {delivered!r}, which cannot "
            "be read, so the mutation it was to evidence was never shown to fail. "
            "Write the red log where the manifest can be read alongside it and "
            "name that path"
        )
    control_ids = _control_failure_ids(text)
    head_ids = _head_suite_failure_ids(manifest)
    baseline_ids = _baseline_suite_failure_ids(manifest)
    # The head arm decides the control whenever it is readable; the baseline is
    # the fallback only when it is not. ``None`` from either reader means the arm
    # is unreadable, so when both are unreadable nothing is left to compare
    # against and the control cannot be admitted on a comparison that was never
    # made: ``reference_ids`` stays ``None`` and the refusal below carries it.
    if head_ids is not None:
        reference_ids: set[str] | None = head_ids
    else:
        reference_ids = baseline_ids
    added = [] if reference_ids is None else sorted(control_ids - reference_ids)
    recorded_exit = _recorded_exit_status(text)
    check["control_exit_status"] = recorded_exit
    check["control_failure_ids"] = sorted(control_ids)
    if baseline_ids is not None:
        check["baseline_failure_ids"] = sorted(baseline_ids)
    if head_ids is not None:
        check["head_failure_ids"] = sorted(head_ids)
    if reference_ids is None:
        check["comparison_arm"] = "none"
    elif head_ids is None:
        check["comparison_arm"] = "baseline_suite"
    else:
        check["comparison_arm"] = "after_suite"
    check["added_failure_ids"] = added
    # Both facts have to come from something only the run could have written: a
    # log with no EXIT record says nothing about whether its command failed,
    # and one naming no failing test id says nothing about what broke. Neither
    # is inferred — a failing count and a repeated declaration together are
    # exactly the shape the removed wording rule could not tell from a
    # measurement — so an unrecorded fact refuses the declaration rather than
    # admitting it.
    if recorded_exit is None:
        unexplained = "it records no EXIT status, so whether its run failed is unknown"
    elif recorded_exit == 0:
        unexplained = "it records EXIT=0, so its run did not fail"
    elif not control_ids:
        unexplained = "it names no failing test id, so what broke is unknown"
    elif reference_ids is None:
        unexplained = (
            "no readable comparison arm is recorded — neither after_suite nor "
            "baseline_suite declares the run complete — so it adds no failure "
            "against an arm the gate can read, and the failure it names "
            f"({', '.join(sorted(control_ids))}) is one nothing compares"
        )
    elif head_ids is not None and not added:
        unexplained = (
            "it adds no failure to the head arm's: every test it names "
            f"({', '.join(sorted(control_ids))}) is one the head arm also fails"
        )
    elif not added:
        unexplained = (
            "it adds no failure to the baseline's: every test it names "
            f"({', '.join(sorted(control_ids))}) is one the baseline already fails"
        )
    else:
        unexplained = ""
    if unexplained:
        refusal = (
            f"run {run_id!r} declares the mutation {declaration!r} but the log at "
            f"{resolved!r} shows no failed control run: {unexplained}. A control "
            "is admitted on its facts alone — a non-zero exit record and at least "
            "one failing test id the head arm does not fail, falling back to the "
            "baseline's failing ids only when the manifest records no readable "
            "head arm, an arm being readable only when it declares its run "
            "complete — and neither fact is "
            "inferred from the log's wording or from a bare failing count. Re-run "
            "the declared mutation and keep the log it produced, with the "
            "capture's EXIT=<n> record and the runner's own list of which tests "
            "failed, or use --waive-negative-control REASON to record why the "
            "control may be accepted without it"
        )
        if waiver_reason:
            check["verdict"] = "waived"
            check["reason"] = waiver_reason
            return check
        raise CrewError(refusal)
    check["verdict"] = "matched"
    return check


def _refuse_commits_for_a_shadow(
    run_id: str, record: Mapping[str, Any], commits: Sequence[str]
) -> None:
    """Refuse a shadow run presenting commits: its evidence is a patch, not code."""
    if _is_shadow(record) and any(str(sha).strip() for sha in commits):
        raise CrewError(
            f"shadow run {run_id!r} is commitless evidence; --commit is refused"
        )


def _landing_scope_products(
    run_id: str,
    record: Mapping[str, Any],
    *,
    shadow: bool,
    node: Mapping[str, Any],
    commits: Sequence[str],
    accepted_paths: Mapping[str, str] | None,
) -> dict[str, Any]:
    """The scope facts a landing row records, refusing an out-of-role commit.

    A verifier may read the repository it grades but writes only its manifest,
    report and logs, so a cited commit that changes repository paths under a
    non-writing role is refused here rather than recorded as the verifier's
    work. A shadow asserts no code, so its scope is its patch. Everything the
    row carries is returned rather than recomputed, so the refusal and the row
    it would have written cannot disagree. The tree the scope is measured in is
    the run's own, resolved through :func:`_tree_for_measurement`: a patch or a
    citation belongs to the run's tree, never to the directory the promotion
    happens to run in.
    """
    if shadow:
        artifact = _write_shadow_patch(record)
        return {
            "shadow_patch": str(artifact),
            "changed_lines": _shadow_patch_stat(
                artifact, cwd=_tree_for_measurement(record, run_id)
            ),
            "scope_acceptances": [],
        }
    if not commits:
        return {"shadow_patch": "", "changed_lines": None, "scope_acceptances": []}
    tree = _tree_for_measurement(record, run_id)
    cumulative = _committed_scope(cwd=tree, commits=commits, run_id=run_id)
    acceptances: list[dict[str, str]] = []
    if cumulative.changed_lines.get("available", True):
        if (
            not role_may_write_repository_paths(str(record.get("role") or ""))
            and cumulative.paths
        ):
            raise CrewError(
                f"run {run_id!r} has role 'test', but its cited commit "
                "changes repository paths: "
                + ", ".join(cumulative.paths)
                + ". A verifier may read the repository it grades, but "
                "writes only its manifest, report, and logs outside the "
                "repository; dispatch an implement node for source edits"
            )
        outside = _outside_declared_scope(
            cumulative.paths,
            node.get("write_paths") or (),
            record=record,
            tree=tree,
        )
        if outside:
            acceptances = _accepted_scope_exceptions(
                run_id,
                outside,
                accepted_paths,
                record=record,
                tree=tree,
                commits=commits,
            )
    return {
        "shadow_patch": "",
        "changed_lines": cumulative.changed_lines,
        "scope_acceptances": acceptances,
    }


def _landing_preconditions(
    run_id: str,
    record: Mapping[str, Any],
    *,
    checkout: Path | None,
    ledger_root: str | Path | None,
    commits: Sequence[str],
    gate: str,
    failure_classification: str,
    no_impl_change: str,
    plan_link: str,
    unplanned_reason: str,
    boundary_waiver: str,
    negative_control_waiver: str | None,
    accepted_paths: Mapping[str, str] | None,
    gate_check: Mapping[str, Any] | None,
    require_gate_check: bool,
) -> dict[str, Any]:
    """Judge every precondition the landing checks, refusing once with all.

    The checks are independent of one another — each reads the record, the
    run's tree or its plan — so a promotion failing several reports them
    together rather than one per call, in the order the landing path checks
    them today. The one dependency is the citations: the changed-scope and
    role checks diff the commits a promotion presents, so a citation that does
    not resolve leaves them unjudged and they run only once it does. A run
    whose row is already in the ledger re-promotes through the already-promoted
    path, which checks none of this, so the probe below returns before any of
    them run.
    """
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    shadow = _is_shadow(record)
    existing = next(
        (
            item
            for item in ledger.load(project, root=ledger_root)[0]["runs"]
            if str(item.get("run_id") or "") == run_id
        ),
        None,
    )
    if existing is not None:
        return {"already_landed": True}

    refusals: list[BaseException] = []

    def attempt(check: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, check()
        except (CrewError, ledger.LedgerError) as refusal:
            refusals.append(refusal)
            return False, None

    attempt(lambda: _require_committable_checkout(checkout, run_id))

    commit_list = list(_presented_commits_without_a_declaration(record, commits))
    shadow_ok, _ = attempt(
        lambda: _refuse_commits_for_a_shadow(run_id, record, commit_list)
    )
    resolved_ok = True
    resolved: Sequence[str] = []
    if commit_list:
        resolved_ok, resolved = attempt(
            lambda: _resolve_commits(
                cwd=_tree_for_measurement(record, run_id),
                revisions=commit_list,
                run_id=run_id,
            )
        )
    if not resolved_ok:
        # The changed-scope and role checks diff the cited commits, so an
        # unresolvable citation leaves them unjudged: this pair stays
        # sequential, after the citation itself is judged.
        commit_list = []
        resolved = []
    elif resolved:
        # The row records the canonical revisions the citations resolved to —
        # an abbreviated sha or a tag is admitted as a citation, never as the
        # value a later reader resolves again.
        commit_list = [str(canonical) for canonical in resolved]

    scope_products: dict[str, Any] = {
        "shadow_patch": "",
        "changed_lines": None,
        "scope_acceptances": [],
    }
    if shadow_ok and resolved_ok:
        _ok, measured = attempt(
            lambda: _landing_scope_products(
                run_id,
                record,
                shadow=shadow,
                node=node,
                commits=tuple(resolved),
                accepted_paths=accepted_paths,
            )
        )
        if measured:
            scope_products = measured

    _ok, boundary_waived = attempt(
        lambda: _require_repository_tree_boundary(
            run_id, record, waiver_reason=boundary_waiver
        )
    )
    plan_state = _plan_state_for_run(record, fallback_root=ledger_root)
    _ok, brief_owner = attempt(
        lambda: _require_brief_owner(
            run_id, record, plan_link=plan_link, unplanned_reason=unplanned_reason
        )
    )
    _ok, impl_move = attempt(
        lambda: _require_impl_moved(
            run_id,
            record,
            gate=gate,
            failure_classification=failure_classification,
            no_impl_change=no_impl_change,
            plan_state=plan_state,
        )
    )

    manifest_path = str(record.get("manifest_path") or "")
    manifest_text: str | None = None
    manifest: Mapping[str, Any] | None = None
    if manifest_path:
        try:
            manifest_path_text = Path(manifest_path).read_text(encoding="utf-8")
        except OSError:
            manifest_text = None
        else:
            manifest_text = manifest_path_text
    if manifest_text is not None:
        try:
            manifest = parse_manifest(manifest_text)
        except (KeyError, ValueError, OSError):
            manifest = None
    waiver_reason = (
        "" if negative_control_waiver is None else str(negative_control_waiver).strip()
    )

    def _negative_control_check() -> dict[str, Any]:
        if negative_control_waiver is not None and not waiver_reason:
            raise CrewError("--waive-negative-control requires a non-empty reason")
        control = _require_declared_negative_control(
            run_id,
            record,
            gate=gate,
            manifest=manifest,
            manifest_path=manifest_path,
            waiver_reason=waiver_reason,
        )
        if negative_control_waiver is not None and control["verdict"] != "waived":
            raise CrewError(
                f"run {run_id!r} has no negative-control match refusal for "
                f"--waive-negative-control {waiver_reason!r} to waive"
            )
        return control

    _ok, negative_control = attempt(_negative_control_check)
    attempt(
        lambda: _require_gate_check_precondition(
            gate_check, gate=gate, require_gate_check=require_gate_check
        )
    )

    if refusals:
        if len(refusals) == 1:
            raise refusals[0]
        raise _PromotionRefusalError(refusals)

    return {
        "already_landed": False,
        "commits": commit_list,
        "shadow_patch": scope_products["shadow_patch"],
        "changed_lines": scope_products["changed_lines"],
        "scope_acceptances": scope_products["scope_acceptances"],
        "boundary_waived": boundary_waived,
        "brief_owner": brief_owner,
        "impl_move": impl_move,
        "manifest": manifest,
        "manifest_text": manifest_text,
        "negative_control": negative_control,
    }


def _staging_review_record_by_run(project: str, run_id: str) -> dict[str, Any] | None:
    """The staged review record a review run delivered, found by its own id.

    A plan review is stored by the plan-review store rather than as a scored run
    review, so the run-store lookup the run-review path uses does not find it.
    Both kinds name the review run that produced them, which is the one stable
    key they share, so the lookup is served by the review store's own index for
    that key rather than by walking the project directory — a whole-store pass
    per promotion is the cost the index removes.
    """
    found = review_module.record_for_review_run(project, run_id)
    return None if found is None else found[1]


def _delivered_review_payloads_for_commit(
    record: Mapping[str, Any], project: str, run_id: str
) -> list[dict[str, Any]]:
    """Every stored review record a promoting review run should commit.

    A review run's deliverable is the record it stored beside the subject it
    read, and that record is one round of that subject's review: the run makes
    no repository commit of its own, so landing it is the moment the round can
    be landed. A subject reviewed more than once — a first review, then a
    repair round and its re-review at a new head — leaves one round per head in
    the host staging store, and every one of them is the subject's review. So
    the promotion commits every stored round of the subject, across all heads
    and all review run ids, not only the round this run selected for the head
    it promotes; the earlier rounds would otherwise survive only in the staging
    store and nowhere that travels with the plan and the ledger.

    The subject is resolved from the run's node id (a plan review reviews a
    plan and names no reviewed run), and its rounds are enumerated from the
    staging store. The round this run delivered is included whether or not the
    store index already lists it, so a record filed off the run's own path is
    still committed. A plan review, whose subject is a plan, contributes the
    single round this run produced. A run that is not a review contributes
    nothing and lands as before. Rounds are deduplicated by review run id, the
    key the committed file is filed under, so the delivered round and the same
    round read from the index commit once.

    The round this run delivered is filed under the promoting run's id. A
    record a reviewer wrote by hand can lose its own ``review_run_id``, and
    since the promoting run is exactly the run that produced that round its id
    is the correct key: refusing would abandon the round the run was minted to
    deliver, and filing it under the reviewed run would collide across two
    review runs of one subject. A round the index returns that is neither this
    run's own delivered round nor carries a review run id cannot be keyed at
    all, so it is skipped with a note naming the file rather than refusing the
    whole promotion — the other rounds it sits beside are still the subject's
    review and should land.

    The store refuses a record it cannot key or time; this function does not
    filter such a record out, because the refusal must reach the caller as a
    rolled-back landing rather than as a silently dropped round.
    """
    from reckon.crew import recovery

    if not recovery._is_review_run(record):
        return []
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()

    def include(payload: Mapping[str, Any] | None) -> None:
        if not payload:
            return
        key = str(payload.get("review_run_id") or "").strip()
        if key:
            if key in seen:
                return
            seen.add(key)
        payloads.append(dict(payload))

    delivered = recovery._delivered_review_record(
        record, str(record.get("project") or "")
    )
    delivered_path = delivered[1] if delivered is not None else None
    if delivered_path is not None:
        delivered_payload = _read_json_object(delivered_path)
        if delivered_payload and not str(
            delivered_payload.get("review_run_id") or ""
        ).strip():
            delivered_payload = {**delivered_payload, "review_run_id": run_id}
        include(delivered_payload)
    reviewed_run_id = recovery._resolved_reviewed_run_id(record, project)
    if reviewed_run_id:
        for path, stored in review_module.stored_records_for_run(
            project, reviewed_run_id
        ):
            if delivered_path is not None and Path(path) == Path(delivered_path):
                continue
            if not str(stored.get("review_run_id") or "").strip():
                LOGGER.warning(
                    "skipping stored review round %s of run %r: it carries no "
                    "review run id and is not the round this promotion "
                    "delivered, so it cannot be filed under a committed path",
                    path,
                    reviewed_run_id,
                )
                continue
            include(stored)
    if not payloads:
        # A plan review names no reviewed run and its own round was not found
        # through the delivered lookup, so the store index for the review run
        # id is the remaining way to reach it.
        include(_staging_review_record_by_run(project, run_id))
    return payloads


def _complete_locked(
    run_id: str,
    *,
    record: Mapping[str, Any] | None = None,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    no_commit: str = "",
    no_commit_uncommitted: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
    gate_check: Mapping[str, Any] | None = None,
    require_gate_check: bool = False,
    suite_delta: Mapping[str, Any] | None = None,
    boundary_waiver: str = "",
    resume_remedy: Mapping[str, str] | None = None,
    resume_waived: Mapping[str, str] | None = None,
    reviewed: Mapping[str, Any] | None = None,
    review_waived: Mapping[str, str] | None = None,
    review_tier: str = "",
    negative_control_waiver: str | None = None,
    recoverable_session: Mapping[str, str] | None = None,
    discard_resume_worktree: bool = False,
    accepted_paths: Mapping[str, str] | None = None,
    commit_list_shortfall: Mapping[str, Any] | None = None,
    no_impl_change: str = "",
    live_run_waived: Mapping[str, str] | None = None,
    plan_link: str = "",
    unplanned_reason: str = "",
    landing: Mapping[str, Any] | None = None,
    promoted_by: str = "",
) -> dict[str, Any]:
    """Promote a finished run into the owning repository's committed ledger.

    The plan comment and ledger append both happen before the pointer is
    deleted. The comment uses a stable run-derived id, so a retry after an
    interruption cannot duplicate the narrative.

    Worker-time spans the first and last timestamped stream events. The stream
    is read after a bounded settle, so the terminal record the worker's harness
    writes after the manifest is not missed. A live process is normally never
    waited on — but when promotion is about to end that process in its release
    step anyway, it ends it first and then settles the tail the shutdown writes,
    folding the run's own record instead of a file mtime; a promotion that will
    not end the process keeps the live-process behaviour. A healthy
    timestamp-less stream falls back to wall duration with an explicit source;
    a stalled run keeps that duration absent. Promotion time remains an
    explicit completion fallback when no stream survives.

    ``record`` is the run's own record, passed by :func:`complete` so a run
    rebuilt from its directory and one read from its live pointer reach the
    same body. A caller that omits it has the pointer read here, which keeps
    the direct callers that predate the reconcile path working unchanged.
    """
    if record is None:
        record = read_pointer(run_id)
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    shadow = _is_shadow(record)
    ledger_root = root if root is not None else record.get("repo")
    checkout = (
        Path(ledger_root).expanduser().resolve() if ledger_root is not None else None
    )
    # Promotion commits the stores it writes, so a checkout that cannot host
    # that commit refuses before either store is written rather than leaving a
    # half-landed, uncommitted state behind.
    _require_committable_checkout(checkout, run_id)
    tree = _record_tree(record)
    # A landing that will carry the plan file must not sweep an unrelated
    # uncommitted edit into its commit. The check runs here, before either
    # store is written, so a refusal leaves no ledger row and no plan comment
    # for the next promotion to read as an unrelated edit.
    if not shadow:
        _refuse_unrelated_plan_edit(
            project=project,
            plan=str(node.get("plan") or ""),
            root=ledger_root,
            checkout=checkout,
        )
    ledger_data, ledger_version = ledger.load(project, root=ledger_root)
    existing = next(
        (
            item
            for item in ledger_data["runs"]
            if str(item.get("run_id") or "") == run_id
        ),
        None,
    )
    if existing is not None:
        existing_path = ledger.run_path(project, run_id, ledger_root)
        if not existing_path.is_file():
            existing_path = ledger.ledger_path(project, ledger_root)
        with _report_written_ledger_row(
            run_id,
            ledger_path=existing_path,
            row_present=lambda: _ledger_holds_row(project, ledger_root, run_id),
        ):
            comment = (
                {"recorded": False, "reason": "shadow evidence does not land code"}
                if shadow
                else _record_landing_comment(
                    project=project,
                    plan=str(node.get("plan") or ""),
                    section=str(node.get("section") or ""),
                    run_id=run_id,
                    narrative=outcome,
                    author=_COORDINATOR_LANDING_AUTHOR,
                    when=str(existing.get("completed_at") or _utc_now()),
                    root=ledger_root,
                    worker_tree=tree,
                    worker_commits=_declared_manifest_commits(record),
                    landing=_declared_manifest_landing(record),
                )
            )
            _commit_landing_writes(
                run_id=run_id,
                verdict=str(gate).strip().lower(),
                checkout=checkout,
                paths=_plan_comment_store_path(
                    project=project,
                    plan=str(node.get("plan") or ""),
                    comment=comment,
                    root=ledger_root,
                    checkout=checkout,
                ),
            )
            capture = _capture_member_session(record)
            path = pointer_path(run_id)
            path.unlink(missing_ok=True)
            retention = existing.get("worktree_retention")
            release = _release_after_promotion(
                run_id,
                record,
                retention if isinstance(retention, Mapping) else None,
                gate=str(gate),
            )
            release, recorded = _record_release_on_ledger(
                project=project,
                root=ledger_root,
                run_id=run_id,
                release=release,
                checkout=checkout,
            )
            release.update(_retire_disposable_identity(record))
            if recorded is not None:
                existing = recorded
            result = {
                "run_id": run_id,
                "project": project,
                "ledger_path": str(existing_path),
                "ledger_version": ledger_version,
                "pointer_removed": not path.exists(),
                "record": dict(existing),
                "already_promoted": True,
                "session_capture": capture,
                "plan_comment": comment,
                "release": release,
            }
            lane_receipt = existing.get("lane_receipt")
            if isinstance(lane_receipt, Mapping):
                result["lane_receipt"] = dict(lane_receipt)
            # This is a bounded fleet reading, not a readiness recommendation: the
            # result states only what promotion observed, and the orchestrator owns
            # every decision about what to do next.
            result["fleet_state"] = _fleet_state_reading(project)
            return result

    # A writer still alive when a prompt promotion arrives is about to be ended
    # by this promotion's own release step; end it before the observation so the
    # bounded settle can fold the terminal record its shutdown writes. A run the
    # release would not signal — no terminal manifest, or a non-cli launch —
    # keeps the existing live-process behaviour, and nothing waits on a process
    # this promotion is not going to end.
    ended_writer = _end_live_writer_for_settle(record)
    stream = _promotion_terminal_observation(record, settle_even_if_alive=ended_writer)
    if completed_at:
        finished = _assume_utc_if_naive(completed_at)
        completion_source = "provided"
    elif stream.completed_at:
        finished = stream.completed_at
        completion_source = stream.completion_source or "terminal_event"
    else:
        finished = _utc_now()
        completion_source = "promotion_time"
    worktree_retention = _resume_worktree_retention(
        record,
        recoverable_session,
        retained_at=finished,
        discard=discard_resume_worktree,
        gate=str(gate),
    )
    if landing is None:
        landing = _landing_preconditions(
            run_id,
            record,
            checkout=checkout,
            ledger_root=ledger_root,
            commits=commits,
            gate=gate,
            failure_classification=failure_classification,
            no_impl_change=no_impl_change,
            plan_link=plan_link,
            unplanned_reason=unplanned_reason,
            boundary_waiver=boundary_waiver,
            negative_control_waiver=negative_control_waiver,
            accepted_paths=accepted_paths,
            gate_check=gate_check,
            require_gate_check=require_gate_check,
        )
    commit_list = list(landing["commits"])
    shadow_patch = str(landing["shadow_patch"] or "")
    changed_lines = landing["changed_lines"]
    scope_acceptances = list(landing["scope_acceptances"] or [])
    boundary_waived = landing["boundary_waived"]
    brief_owner = landing["brief_owner"]
    impl_move = landing["impl_move"]
    manifest_text = landing["manifest_text"]
    manifest = landing["manifest"]
    negative_control = landing["negative_control"]

    from reckon.crew.resumption import resolve_session

    session_id = resolve_session(
        run_id,
        record=record,
        project=str(record.get("project") or ""),
        root=record.get("repo"),
    )["session_id"]
    lane_receipt = _harvest_lane_receipt(
        record,
        session_id=session_id,
        observed_at=finished,
    )
    previous = next(
        (
            item
            for item in reversed(ledger_data["runs"])
            if session_id and item.get("session_id") == session_id
        ),
        None,
    )
    measured_budget = ledger.per_run_budget(stream.budget, previous)
    wall_seconds = _elapsed_seconds(record.get("created_at"), finished)
    stalled = _wall_exceeded_budget(wall_seconds, node.get("time_budget"))
    worker_seconds = stream.worker_seconds
    if worker_seconds is not None:
        worker_seconds_source = "stream_events"
    elif stream.completion_source == "stream_mtime" and stalled:
        worker_seconds_source = "stalled"
    elif stream.completion_source == "stream_mtime" and wall_seconds is not None:
        worker_seconds = wall_seconds
        worker_seconds_source = "wall_fallback"
    else:
        worker_seconds_source = "unavailable"

    # Routing evidence is read from the delivered manifest at promotion, so a
    # later measure can separate a followup touch from a defect on the row
    # alone; the run directory survives, pruned only later by crew gc. An
    # unreadable manifest
    # leaves follow-on paths unmeasured (key absent) and the dispute count
    # "unknown" — a node never measured is not one that measured zero.
    follow_on_paths = (
        None if manifest is None else ledger.follow_on_paths(manifest.get("follow_ons"))
    )
    predecessor = ledger.predecessor_run_id(
        supplied=_coordinator_supplied_predecessor(record),
        base_sha=str(record.get("base_sha") or record.get("base") or ""),
        runs=ledger_data["runs"],
        exclude_run_id=run_id,
    )
    dispute_count = (
        "unknown"
        if manifest_text is None
        else ledger.stated_correction_count(manifest_text)
    )
    # An attached review is copied onto the committed row at promotion, so its
    # dimension scores and their total survive the loss of the crew
    # configuration home. The store keeps the verbatim text and findings; the
    # row keeps the compact block that joins to the run which earned it.
    # The cited gate log is copied into the row's own run directory before the
    # record is built, so the row names a path that outlives the worktree and the
    # reaper rather than one that was true only when it was written. The step is
    # best-effort: a log already in the run directory, a citation that does not
    # resolve on this machine, and a copy that cannot be read or written all
    # leave the check as given, because preservation must never decide the verdict.
    gate_check = _preserve_cited_gate_log(run_id, gate_check)
    # The worker's own exit record is copied onto the row before promotion
    # releases the live pointer and the worktree. The run directory survives
    # until crew gc prunes it, but the row must carry the record regardless: the
    # facts a census reads (the terminating signal and whether the exit landed
    # mid-work either way) then outlive the pointer. The row's ``exit_status`` is
    # left as the gate command's status; the worker's exit is a separate fact
    # under its own key, so a run with no exit record carries no ``worker_exit``
    # key rather than an empty one.
    worker_exit = _promoted_worker_exit(run_id)
    # The revision this promotion asserts landed, resolved while the run's tree
    # is still present. A shadow asserts no code, so it records none.
    promoted_revision = "" if shadow else _run_promoted_revision(record, commit_list)
    # The six-line clone detector reports each function this run added or
    # modified whose normalised window duplicates another function already in
    # reckon/ or tests/ at the promoted revision. It is a review signal: a
    # readable revision yields the list of matches (empty when none), and a
    # case no revision pair can be measured for records the unmeasured marker
    # rather than the empty list, so the two never read alike. Neither fails
    # the promotion.
    if shadow:
        clone_matches: Any = ledger.unmeasured_clone_report("shadow run lands no code")
    elif not (commit_list and promoted_revision):
        clone_matches = ledger.unmeasured_clone_report(
            "the run asserted no revision pair to compare"
        )
    else:
        measured = clones.promotion_clone_matches(
            tree,
            base_sha=str(record.get("base_sha") or record.get("base") or ""),
            tip=promoted_revision,
        )
        clone_matches = (
            measured
            if measured is not None
            else ledger.unmeasured_clone_report("the revision pair could not be read")
        )
    # A brief run carries the digest and stored path of the brief it read in
    # place of a plan section, so a promoted row still points at the exact text
    # the worker saw. The block is present only for a brief run, which is what
    # makes the row's ``plan`` null rather than empty.
    brief_run = str(node.get("brief") or "").strip()
    brief_block = (
        {
            "sha256": str(node.get("brief_sha256") or ""),
            "path": str(node.get("brief_path") or ""),
        }
        if brief_run
        else None
    )
    # The crew session that dispatched this run rides the committed row beside
    # the run it belongs to, because this promotion deletes the live pointer
    # that carried it while ``session_id`` names the worker's own harness
    # session rather than the coordinator's. It is null rather than absent when
    # no pointer named one, because every key in ``RECORD_FIELDS`` is present on
    # every promoted row.
    pointer_session = str(record.get("session") or "").strip() or None
    # The disposition verb rides the committed row for the same reason: the
    # live pointer that carries it is deleted by this promotion, so a row that
    # does not read it here can never recover it. A run with no recorded
    # disposition records null, never an inferred verb.
    disposition = _recorded_disposition(record)
    # A review run's deliverable is the record it stored for the subject it
    # read, so landing the run lands the record with it — and every earlier
    # round of that subject too, so a review of another head is not left only in
    # the host staging store. The rounds are read here, before the ledger row is
    # assembled, so the round this run produced can name the row and every
    # round's committed write can join the row's commit below. A run that is not
    # a review, or a review run that delivered no readable round, leaves the
    # row's review block as the review gate resolved it.
    committed_review_payloads: list[dict[str, Any]] = []
    if not shadow:
        committed_review_payloads = _delivered_review_payloads_for_commit(
            record, project, run_id
        )
    committed_review_payload = next(
        (
            payload
            for payload in committed_review_payloads
            if str(payload.get("review_run_id") or "").strip() == run_id
        ),
        None,
    )
    if committed_review_payload is None and committed_review_payloads:
        # A round whose own body carried no review run id is filed under this
        # run's id above, so it is normally the selected one; if none names the
        # row, the first round still stands in so the row's review block is not
        # left absent.
        committed_review_payload = committed_review_payloads[0]
    if committed_review_payload is not None:
        committed_block = review_module.ledger_block(committed_review_payload)
        if committed_block is not None:
            committed_block["id"] = run_id
            reviewed = committed_block
    run = ledger.build_record(
        run_id=run_id,
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
        brief=brief_block,
        plan_link=str((brief_owner or {}).get("plan_link") or ""),
        unplanned_reason=str((brief_owner or {}).get("unplanned_reason") or ""),
        node=str(node.get("id") or ""),
        node_definition=node,
        role=str(record.get("role") or ""),
        spec_level=str(node.get("spec_level") or ""),
        member_id=str(record.get("member") or ""),
        backend=str(
            record.get("backend") or (record.get("agent") or {}).get("backend") or ""
        ),
        agent=record.get("agent") or {},
        dispatched_at=str(record.get("created_at") or ""),
        completed_at=finished,
        completed_at_source=completion_source,
        worker_seconds=worker_seconds,
        worker_seconds_source=worker_seconds_source,
        wall_seconds=wall_seconds,
        stalled=stalled,
        time_budget=str(node.get("time_budget") or ""),
        base_sha=str(record.get("base_sha") or ""),
        commits=commit_list,
        changed_lines=changed_lines,
        tests_added=tests_added,
        gate=gate,
        failure_classification=failure_classification,
        outcome=outcome,
        disposition=disposition,
        manifest_path=str(record.get("manifest_path") or ""),
        scope_changed=scope_changed,
        session=pointer_session,
        session_id=session_id,
        session_harness=(record.get("session_harness") or record.get("dialect"))
        if session_id
        else None,
        session_model=(
            record.get("session_model") or (record.get("agent") or {}).get("model")
        )
        if session_id
        else None,
        budget=measured_budget,
        lane_receipt=lane_receipt,
        throughput=stream.throughput,
        budget_fallback=record.get("budget_fallback"),
        lineage=record.get("lineage"),
        shadow_patch=shadow_patch,
        unreconciled_override=record.get("unreconciled_override"),
        orchestrator_lane_override=record.get("orchestrator_lane_override"),
        gate_check=gate_check,
        require_gate_check=require_gate_check,
        suite_delta=suite_delta,
        resume_remedy=resume_remedy,
        follow_on_paths=follow_on_paths,
        predecessor_run=predecessor,
        dispute_count=dispute_count,
        review=reviewed,
        clone_matches=clone_matches,
        promoted_by=promoted_by,
    )
    run["attempt"] = int(record.get("attempt") or 1)
    run["attempt_kind"] = str(record.get("attempt_kind") or "dispatch")
    # The worker's exit record rides the row verbatim, and the key is written
    # only when the run directory held one: a present-but-empty key would read
    # as a supervisor that ran and recorded nothing.
    if worker_exit is not None:
        run["worker_exit"] = worker_exit
    # The revision the promotion asserts landed rides the row so a later sweep
    # can ask about it without the worktree, which promotion is about to
    # release. A run that asserted no code leaves the key absent rather than
    # recording a base it never touched, so an absent key never reads as a
    # promotion whose work was verified as landed.
    if promoted_revision:
        run["promoted_revision"] = promoted_revision
    # The job a placed launch was charged to rides the committed row beside the
    # run it belongs to, because the ledger row is the durable record a later
    # attribution reads; the live pointer it was first written on is removed by
    # this promotion. A run that declared no placement records the key absent
    # rather than zero, so an unplaced run is distinguishable from a placed one
    # whose scheduler never answered.
    job_id = record.get("job_id")
    if job_id is not None:
        run["job_id"] = str(job_id)
    # A deliberate commitless promotion survives on the record with its reason,
    # so a later reader can tell it from one that recorded nothing by accident.
    if str(no_commit).strip():
        run["no_commit"] = str(no_commit).strip()
    # A deliberate override of the worktree-evidence guard records the paths it
    # covered, so the ledger says which uncommitted repository work was declined
    # a commit beside the reason given, rather than only that a commit was
    # declined. No manifest field can put a path here.
    uncommitted = sorted(
        {str(path).strip() for path in no_commit_uncommitted if str(path).strip()}
    )
    if uncommitted:
        run["no_commit_uncommitted_paths"] = uncommitted
    # The impl comparison and its outcome ride the row, so a later audit can
    # separate a plan that moved from one promoted against a recorded waiver.
    if impl_move.get("at_dispatch") is not None:
        run["plan_impl_at_dispatch"] = impl_move["at_dispatch"]
    run["impl_move"] = dict(impl_move)
    # The negative-control verdict rides the row, so an audit can separate a
    # red log that was delivered and matched from a declaration of none that was
    # recorded with its reason rather than refused.
    run["negative_control"] = dict(negative_control)
    # A presented-list shortfall survives on the record so a reader of the
    # ledger sees the boundary check may have been under-scoped, not only the
    # coordinator that was looking at the immediate report.
    if commit_list_shortfall:
        run["commit_list_shortfall"] = dict(commit_list_shortfall)
    if boundary_waived is not None:
        run["boundary_waiver"] = boundary_waived
    if scope_acceptances:
        run["scope_acceptances"] = scope_acceptances
    # A discarded resume path survives on the row with the session it discarded
    # and the reason given, so an audit can tell a deliberate discard from the
    # accidental one this refusal exists to prevent.
    if resume_waived is not None:
        run["resume_waiver"] = dict(resume_waived)
        if discard_resume_worktree:
            run["resume_waiver"]["worktree_discarded"] = True
    if review_waived is not None:
        run["review_waiver"] = dict(review_waived)
    # The tier rides the row on every promotion. For a run that changed no
    # runtime source it is the recorded reason no review exists — the merged-head
    # gate re-run and the node's negative control are what checked it instead —
    # so a later reader can tell a deliberate no-review tier from a review that
    # was simply never produced.
    if review_tier:
        run["review_tier"] = review_tier
    # A run promoted while its own worker was still alive survives on the row
    # with the reason given, so an audit can tell a deliberate promotion of a
    # live worker from the accidental orphan this refusal exists to prevent.
    if live_run_waived is not None:
        run["live_run_waiver"] = dict(live_run_waived)
    if worktree_retention is not None:
        run["worktree_retention"] = dict(worktree_retention)
    watch_override = record.get("watch_override")
    if isinstance(watch_override, Mapping):
        run["watch_override"] = dict(watch_override)
    execution_fit = record.get("execution_fit")
    if isinstance(execution_fit, Mapping):
        run["execution_fit"] = dict(execution_fit)
    # A run launched without the fence carries the reason it was waived onto the
    # committed row, so the ledger says which runs ran unprotected and why. The
    # pointer holds it only until promotion deletes it, and a reader asking why
    # a worker could write outside its grant needs it in the durable row. An
    # ordinary fenced dispatch records no such key.
    fence_waiver = record.get("fence_waiver")
    if isinstance(fence_waiver, Mapping):
        run["fence_waiver"] = dict(fence_waiver)
    # The defaults a layer left writable ride the row beside the waiver, for the
    # same reason: the pointer that held them is deleted at promotion, and a
    # reader asking whether a run could write outside its fence's own grant needs
    # them in the durable row. A run whose fence removed nothing records no such
    # key, so the row never carries an empty list that would read as a fence
    # built and found whole.
    fence_unprotected = record.get("fence_unprotected_paths")
    if fence_unprotected:
        run["fence_unprotected_paths"] = [str(path) for path in fence_unprotected]
    # The advisory a dispatch computed rides the committed row the same way it
    # rode the live pointer, together with the lane declaration and reading it
    # was derived from. Promotion deletes that pointer, so an advisory reaching
    # only the pointer is readable exactly until the run becomes evidence, and
    # the row is where a later reader asks whether the advice was taken --
    # ``backend`` already names the lane that was chosen, so the pair on the row
    # is what tells taking the advice apart from declining it. Each key is
    # written only when the pointer carried it: a row holding a null advisory
    # would read as a lane that was assessed and found quiet, which is the
    # opposite of a run whose dispatch never emitted one.
    for lane_key in ("lane_advisory", "lane_declaration", "lane_reading"):
        lane_value = record.get(lane_key)
        if lane_value is None:
            continue
        run[lane_key] = dict(lane_value) if isinstance(lane_value, Mapping) else lane_value
    # A shadow whose stream read its primary's landed answer is void as
    # calibration evidence; the stream is still on disk at this point (the run
    # directory survives until crew gc) so promotion scans it rather than
    # trusting a worker's self-report. Contamination recreates the primary's
    # answer from the object store, never from the shadow's own patch.
    if shadow:
        contamination = _shadow_stream_contamination(record, ledger_data["runs"], tree)
        if contamination:
            run["shadow_contaminated"] = contamination
    # A terminal run's stream is immutable, so its two figures are computed once
    # here, from the run's own stream, and recorded on the row; a later derive
    # then reads the ledger instead of reopening the stream. A stream that is
    # already gone records each figure as absent rather than as zero, so a
    # missing measurement never reads as a free run.
    run.update(capabilities.derive_run_figures(run))
    # The landing comment is written last of the plan-facing steps: the record
    # is assembled, and every refusal it raises is raised, before anything is
    # written to the plan. A promotion that refuses after writing the comment
    # leaves the plan mutated for the caller that retries it, and the first
    # attempt's narrative pins the outcome every later attempt must repeat
    # byte-identical to avoid the narrative-conflict refusal. None of the
    # checks above needs the comment to exist, so none of them follows it.
    comment = (
        {"recorded": False, "reason": "shadow evidence does not land code"}
        if shadow
        else _record_landing_comment(
            project=project,
            plan=str(node.get("plan") or ""),
            section=str(node.get("section") or ""),
            run_id=run_id,
            narrative=outcome,
            author=_COORDINATOR_LANDING_AUTHOR,
            when=finished,
            root=ledger_root,
            worker_tree=tree,
            worker_commits=commit_list or _declared_manifest_commits(record),
            landing=_declared_manifest_landing(record),
        )
    )
    # The narrative the comment recorded lives in the plan, so the row carries
    # its outcome empty rather than twice.
    if comment.get("recorded"):
        run["outcome"] = ""
    already_promoted = False
    try:
        written = ledger.append_run(project, run, root=ledger_root)
    except ledger.LedgerError:
        # Another completion can land after the read above. Treat only an
        # observed matching record as success; every other ledger error is
        # still a refusal.
        refreshed, ledger_version = ledger.load(project, root=ledger_root)
        existing = next(
            (
                item
                for item in refreshed["runs"]
                if str(item.get("run_id") or "") == run_id
            ),
            None,
        )
        if existing is None:
            raise
        already_promoted = True
        existing_path = ledger.run_path(project, run_id, ledger_root)
        if not existing_path.is_file():
            existing_path = ledger.ledger_path(project, ledger_root)
        written = {
            "path": str(existing_path),
            "version": ledger_version,
            "run": dict(existing),
        }
    committed_review_paths: list[Path] = []
    with _report_written_ledger_row(
        run_id,
        ledger_path=written["path"],
        row_present=lambda: _ledger_holds_row(project, ledger_root, run_id),
    ):
        # The shadow store outcome rides on the ordinary payload, not a flag or a
        # log stream: a promotion that otherwise succeeded is the exact consumer
        # that must see a silently failing shadow. When this call did not perform
        # the append, the outcome is the one already recorded on the committed row.
        store_outcome = written.get("store")
        if store_outcome is None:
            recorded = written["run"].get("store_write")
            store_outcome = dict(recorded) if isinstance(recorded, Mapping) else None
        store_row_written = (
            not already_promoted
            and isinstance(store_outcome, Mapping)
            and store_outcome.get("status") == "written"
        )
        # A promoting review run's stored rounds are written into the committed
        # reviews tree now, before the landing commit: the run's own per-run file
        # was just written, so it supplies the records' dispatch and completion
        # stamps, and the records then join the ledger row's commit rather than
        # adding one. Every round of the subject is written, not only the round
        # this run selected. The store refuses a record it cannot key or time,
        # and that refusal takes back every record already written this attempt
        # and the row it appended, so neither a record nor the ledger row is
        # committed and the retry re-promotes cleanly.
        if committed_review_payloads and not already_promoted:
            try:
                committed_review_paths.extend(
                    review_module.store_committed_review(
                        payload, project=project, root=ledger_root
                    )
                    for payload in committed_review_payloads
                )
            except ValueError as exc:
                rollback = _restore_landing_writes(
                    checkout, [Path(written["path"]), *committed_review_paths]
                )
                if store_row_written:
                    _discard_run_store_row(run_id)
                raise _landing_refusal(
                    f"cannot store the review record for run {run_id!r} in the "
                    f"committed reviews tree, so the landing was rolled back: {exc}",
                    rollback,
                ) from exc

        # The two tracked stores this promotion wrote (the ledger row and, when a
        # narrative landed, the plan comment) are committed as one landing, so the
        # checkout carries no uncommitted state the next reader would trip on. A
        # refused landing then takes back every write it made, including the store
        # row this attempt's own append inserted, so a retry promotes cleanly.
        _commit_landing_writes(
            run_id=run_id,
            verdict=str(gate).strip().lower(),
            checkout=checkout,
            paths=[
                Path(written["path"]),
                *_plan_comment_store_path(
                    project=project,
                    plan=str(node.get("plan") or ""),
                    comment=comment,
                    root=ledger_root,
                    checkout=checkout,
                ),
                *committed_review_paths,
            ],
            store_row_written=store_row_written,
        )

        # Return capture metadata while the pointer still exists. Session
        # ownership has already been copied onto the committed ledger row.
        capture = _capture_member_session(record)
        pointer_path(run_id).unlink(missing_ok=True)
        release = _release_after_promotion(
            run_id,
            record,
            worktree_retention,
            process_already_ended=ended_writer,
            gate=str(gate),
        )
        release, recorded = _record_release_on_ledger(
            project=project,
            root=ledger_root,
            run_id=run_id,
            release=release,
            checkout=checkout,
        )
        release.update(_retire_disposable_identity(record))
        if recorded is not None:
            written["run"] = recorded
        # This is a bounded fleet reading, not a readiness recommendation: the
        # result states only what promotion observed, and the orchestrator owns
        # every decision about what to do next.
        return {
            "run_id": run_id,
            "project": project,
            "ledger_path": written["path"],
            "ledger_version": written["version"],
            "pointer_removed": not pointer_path(run_id).exists(),
            "record": written["run"],
            "lane_receipt": dict(written["run"]["lane_receipt"]),
            "fleet_state": _fleet_state_reading(project),
            "already_promoted": already_promoted,
            "session_capture": capture,
            "plan_comment": comment,
            "release": release,
            "store": store_outcome,
            "impl_move": dict(impl_move),
            "negative_control": dict(negative_control),
        }


# The marker a deliberate discard leaves in the run directory. A pointer that
# vanishes and a pointer removed on purpose look identical from the fleet's
# side, so without it a reader cannot tell finished work from abandoned work.
DISCARD_RECORD_NAME = "discard.json"


def discard_record_path(run_id: str) -> Path:
    """Path of the marker a discard leaves in one run's directory."""
    return run_dir(run_id) / DISCARD_RECORD_NAME


def discard(run_id: str) -> dict[str, Any]:
    """Remove a stopped or abandoned pointer without promoting it.

    The departure is recorded in the run directory before the pointer goes, so
    a reader sees a discard rather than the same absence a reaped or
    hand-removed pointer produces. The record is never written into a directory
    that does not already exist: the run directory is the run's own home, and a
    write that recreated it would bring a discarded run back into existence.

    The run's own worktree is released through the same audit promotion applies
    on release. A discarded run whose tree is clean and whose head adds no
    commit the integration branch lacks holds nothing to preserve, and leaving
    it on disk refuses a redispatch of the same node because the worktree path
    already exists. A dirty or unintegrated tree is withheld with the audit's
    own reason.
    """
    with _pointer_lock(run_id):
        record = read_pointer(run_id)
        pid = record.get("pid")
        if record_process_alive(record, process_alive) is True:
            raise CrewError(
                f"cannot discard live run {run_id!r}: recorded pid {pid} is alive"
            )
        path = pointer_path(run_id)
        # Before the pointer goes: a reader that sees the absence must find the
        # record behind it, and a record written afterwards leaves a window in
        # which the departure is indistinguishable from a vanished pointer.
        written = _write_discard_record(run_id, record)
        path.unlink()
        return {
            "run_id": run_id,
            "pointer_path": str(path),
            "pointer_removed": not path.exists(),
            "removed": record,
            "discard_record": written,
            **_remove_discarded_worktree(record),
        }


def _remove_discarded_worktree(record: Mapping[str, Any]) -> dict[str, Any]:
    """Release a discarded run's worktree through the release audit.

    Reuses ``_release_run_workspace`` — the same classification and withheld
    reasons ``crew complete`` applies on release — so a discard and a promotion
    cannot disagree about which trees are safe to remove. Worktree and scratch
    fields are surfaced; process fields cannot matter here, because discard has
    already refused a run whose recorded process is alive.
    """
    try:
        release = _release_run_workspace(record)
    except Exception as exc:  # noqa: BLE001 - the pointer is already gone
        fallback = {
            "worktree_released": False,
            "worktree_withheld": (
                f"run {record.get('run_id')!r} release step raised: {exc}"
            ),
        }
        fallback.update(_release_scratch_when_release_raised(record))
        return fallback
    return {
        key: release[key]
        for key in (
            "worktree_released",
            "worktree_withheld",
            "worktree_audit",
            "scratch_removed",
            "scratch_path",
            "scratch_withheld",
            "scratch_bytes",
            "scratch_removed_paths",
            "scratch_warning",
            "arms_removed_paths",
            "arms_kept",
            "arms_logs_copied",
            "arms_withheld",
        )
        if key in release
    }


def _write_discard_record(
    run_id: str, record: Mapping[str, Any]
) -> str | None:
    """Record a deliberate discard in the run directory, or report its absence.

    Returns the record's path when the run directory was there to hold it, and
    ``None`` when the directory had already gone — a run whose home is gone has
    nothing left to mark, and creating the directory here would resurrect it.
    """
    path = discard_record_path(run_id)
    if not path.parent.is_dir():
        return None
    _write_json(
        path,
        {
            "run_id": run_id,
            "discarded_at": datetime.now(UTC).isoformat(),
            "phase": record.get("phase"),
            "node": record.get("node"),
            "pointer_path": str(pointer_path(run_id)),
        },
    )
    return str(path)


def sweep_promoted_revisions(
    project: str,
    *,
    target_head: str = "HEAD",
    markers: Iterable[Mapping[str, Any]] = (),
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Report every promoted run whose asserted work is not in the target head.

    A promotion records the revision it asserts landed; nothing at promotion
    time can know whether the merge carrying it into the integration branch
    happens later, or happens and then drops the content. This sweep answers
    both questions per promoted run:

    * is the recorded revision an ancestor of ``target_head``; and
    * does a marker the change introduced survive in ``target_head`` — or, for a
      change whose purpose was a removal, is the removed marker still gone?

    ``markers`` carries what only a caller can know about a change: the literal
    that proves the content landed and whether its passing answer is presence or
    absence. Each entry is a mapping with ``run_id``, ``marker``, an optional
    ``path`` to search, and ``expect`` of ``"present"`` (the default) or
    ``"absent"``.

    Neither half subsumes the other. A run that was never merged fails ancestry
    and, in the ordinary case, the marker too. A run whose work was merged and
    then reverted — a merge conclusion committing an index that dropped it —
    passes ancestry and fails only the marker. A run that removed something
    passes both unless the marker's absence is required, which is why a removal
    assertion exists at all: presence and absence are not one measurement.
    Nothing is reported for a run whose work landed intact, because a check that
    fires on the healthy case is one nobody keeps.

    The sweep reads the ledger and the branch and writes neither: a promotion
    does not merge, and this is a later read, not a read side of landing.
    """
    checkout = (
        resolve_project_repository(project, root, flag="root")
        if root is not None
        else project_mount_repository(project)
    )
    if checkout is None:
        raise CrewError(
            f"the promoted-revision sweep for {project!r} has no repository to "
            "read: pass root=<checkout> or register the project's mount"
        )
    target = _commit_canonical_id(checkout, target_head)
    if target is None:
        raise CrewError(
            f"target head {target_head!r} does not resolve to a commit in "
            f"{checkout}, so no promotion can be compared against it"
        )
    wanted: dict[str, list[dict[str, str]]] = {}
    for entry in markers:
        run_id = str(entry.get("run_id") or "").strip()
        marker = str(entry.get("marker") or "")
        if not run_id or not marker:
            raise CrewError(
                "a sweep marker needs both a run_id and a marker; one without "
                "either names nothing to search for or nothing to search"
            )
        expect = str(entry.get("expect") or "present").strip().lower()
        if expect not in ("present", "absent"):
            raise CrewError(
                f"sweep marker for {run_id!r} has expect={expect!r}; it must be "
                "'present' or 'absent'"
            )
        wanted.setdefault(run_id, []).append(
            {"marker": marker, "path": str(entry.get("path") or ""), "expect": expect}
        )
    records = ledger.runs(project, root=root)
    findings: list[dict[str, Any]] = []
    matched: set[str] = set()
    checked = 0
    for record in records:
        revision = str(record.get("promoted_revision") or "").strip()
        if not revision:
            # Rows promoted before the revision was recorded, and runs that
            # asserted no code, carry no claim to test. An absent key is not a
            # passing revision; it is a row this sweep cannot speak for.
            continue
        checked += 1
        run_id = str(record.get("run_id") or "")
        finding: dict[str, Any] = {
            "run_id": run_id,
            "node": str(record.get("node") or ""),
            "promoted_revision": revision,
            "reasons": [],
            "markers": [],
        }
        if not _commit_resolves_in(checkout, revision):
            finding["reasons"].append("unresolvable-revision")
        elif not _revision_is_ancestor(checkout, revision, target):
            finding["reasons"].append("not-an-ancestor")
        for spec in wanted.get(run_id, ()):
            matched.add(run_id)
            present = _marker_present_in(
                checkout,
                target,
                marker=spec["marker"],
                path=spec["path"],
            )
            finding["markers"].append(
                {
                    "marker": spec["marker"],
                    "path": spec["path"],
                    "expect": spec["expect"],
                    "present": present,
                }
            )
            if spec["expect"] == "present" and not present:
                finding["reasons"].append("marker-absent")
            elif spec["expect"] == "absent" and present:
                finding["reasons"].append("removed-marker-still-present")
        if finding["reasons"]:
            findings.append(finding)
    unresolved = sorted(run_id for run_id in wanted if run_id not in matched)
    return {
        "project": project,
        "checkout": str(checkout),
        "target_head": target,
        "checked": checked,
        "findings": findings,
        "unresolved_markers": unresolved,
    }


def _revision_is_ancestor(checkout: Path, revision: str, target: str) -> bool:
    """Report whether ``revision`` is an ancestor of ``target`` in ``checkout``.

    A git failure here is an instrument fault, not a verdict: reporting it as
    "not an ancestor" would turn a broken probe into a finding about a run.
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, target],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise CrewError(
        f"git could not compare {revision} against {target} in {checkout}: "
        f"{result.stderr.strip() or result.stdout.strip() or result.returncode}"
    )


def _marker_present_in(
    checkout: Path, target: str, *, marker: str, path: str = ""
) -> bool:
    """Report whether ``marker`` occurs in ``target``'s tree, optionally at ``path``.

    The search is fixed-string and anchored to the target revision, so a marker
    that survives only in a worktree file or in another branch is not counted. A
    git failure raises rather than answering absent: an unreadable tree and a
    genuinely absent marker otherwise return the same empty result.
    """
    argv = ["git", "grep", "-q", "-F", "-e", marker, target]
    if path:
        argv += ["--", path]
    result = subprocess.run(
        argv, cwd=checkout, capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise CrewError(
        f"git could not search {target} in {checkout} for the marker: "
        f"{result.stderr.strip() or result.stdout.strip() or result.returncode}"
    )
