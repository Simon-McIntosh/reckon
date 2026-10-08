from __future__ import annotations

# Imports below the definitions resolve sibling cycles after names are bound.
# ruff: noqa: E402
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reckon import (
    _store,
)
from reckon.crew import review as review_module
from reckon.crew.node import (
    CrewError,
)
from reckon.crew.recovery import _resolve_commit
from reckon.crew.reports import (
    _NONE_VALUES as _MANIFEST_NOTHING,
)
from reckon.crew.reports import (
    parse_manifest,
)
from reckon.crew.runs import (
    _manifest_freshness,
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    run_dir,
)

_COORDINATOR_LANDING_AUTHOR = "reckon-build"


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


_COMMITLESS_ROLES: frozenset[str] = frozenset({"review", "investigate"})


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


_RUNNER_SUMMARY = re.compile(
    r"\b\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b"
)


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



from reckon.crew.promotion_checks import (
    _control_failure_ids,
    _fresh_manifest,
)
from reckon.crew.promotion_release import (
    _record_tree,
    _record_worktree,
)
from reckon.crew.promotion_scope import (
    _changed_paths_declare_no_paths,
    _changed_paths_inside_repository,
    _merge_revisions,
    _repository_scope_paths,
    _revision_is_ancestor,
    _scope_worktree,
)
