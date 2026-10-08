from __future__ import annotations

# Imports below the definitions resolve sibling cycles after names are bound.
# ruff: noqa: E402
import html
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from typing import Any

from reckon import (
    _store,
    ledger,
)
from reckon._plan_html import section_anchor
from reckon._timestamps import parse_iso, parse_utc
from reckon.crew.node import (
    STALL_BUDGET_MULTIPLE,
    CrewError,
    parse_duration,
)
from reckon.crew.plan_review import RUN_COMMENT_PREFIX
from reckon.crew.recovery import _resolve_commit
from reckon.crew.reports import (
    parse_manifest,
    path_within_declared_scope,
)
from reckon.crew.routing import (
    _boundary_tree_roots,
    _git,
    _repository_tree_snapshot,
    _shadow_patch_retained,
    _shadow_worktree_records,
)
from reckon.crew.runs import (
    _live_worktree_claims,
    _manifest_freshness,
    _shared_write_paths,
    _utc_now,
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    list_live,
    run_dir,
)

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
        # The status is reported only when this write flipped it, so a landing
        # that left an authored status alone returns the result shape every
        # caller already reads.
        return {
            "recorded": True,
            "comment_id": comment_id,
            "section": anchor,
            "already_recorded": False,
            **({"status": "in-progress"} if started else {}),
        }
    raise CrewError(
        f"could not record landing comment for plan {plan!r}: "
        "the plan changed during four consecutive write attempts"
    )


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



from reckon.crew.promotion_evidence import (
    _COMMITLESS_ROLES,
    _commit_canonical_id,
    _commitless_changed_paths_declares_absence,
    _commits_field_declares_absence,
    _declares_absent_commits,
    _foreign_repository,
    _worktree_git_paths,
    _worktree_repository_changes,
)
from reckon.crew.promotion_release import (
    _record_worktree,
)
