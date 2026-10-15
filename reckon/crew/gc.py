import os
import shutil
import time

from reckon.crew.routing import (
    STREAM_DERIVED_FIELDS,
    UTC,
    WORKTREE_TRASH_DIRNAME,
    Any,
    CrewError,
    GcSweepError,
    Path,
    _clear_run_lock,
    _extraction_report,
    _figure_retention,
    _git,
    _inspect_workspace,
    _iso_ctime,
    _ledger_index_locked,
    _ledgered_records,
    _ledgered_run_ids,
    _live_pointer_worktrees,
    _pin_archived_commits,
    _pinnable_unique_commits,
    _plan_review_raw_reports,
    _pointer_lock,
    _registered_worktrees,
    _releasable_roots,
    _release_worktree_whole,
    _residue_run_record,
    _run_directory_extractions,
    _run_report_trees,
    _run_worktree_path,
    _save_and_release_worktree,
    _shadow_patch_retained,
    _shadow_worktree_records,
    _stale_run_locks,
    _unledgered_run_directories,
    _workspace_roots,
    datetime,
    ledger,
    list_live,
    runs_dir,
    timedelta,
    timezone,
)
from reckon.crew.runs import (
    pointer_path,
    read_pointer,
    record_process_alive,
)

# The scratch a harness plants in every worktree it provisions: the agent home
# a worker runs against, and the directory carrying the run's own harness
# record. Both are untracked by construction and neither is part of what a run
# delivered, so a tree holding nothing else is still integrated.
_HARNESS_SCRATCH_DIRS: frozenset[str] = frozenset({"codex-home", "harness"})


def _harness_scratch_entry(code: str, path: str) -> bool:
    """Whether one status entry names harness scratch rather than tree content.

    Only an untracked entry at the worktree root qualifies: the harness creates
    these directories there itself, while a nested path merely carrying the
    same name, or a tracked file a run modified, is the run's own work.
    """
    return code == "??" and path.partition("/")[0] in _HARNESS_SCRATCH_DIRS


def _gc_projects(repo: Path, project: str | None) -> list[str]:
    """Return the project names whose ledgers a gc pass reads."""
    if project:
        return [project]
    state_root = repo / "docs" / "state"
    if state_root.is_dir():
        return sorted(path.name for path in state_root.iterdir() if path.is_dir())
    return []


# What `--apply` removes, and why each of the rest stays. Kept beside the removal
# branch so the report and the behaviour cannot drift: a classification named
# here as reclaimable must be one that branch acts on.
RECLAIMABLE_CLASSES = ("integrated", "disposable", "dirty-integrated")


WITHHELD_REASONS = {
    "dirty": (
        "uncommitted changes in the worktree; commit or discard them, and "
        "nothing reclaims a worktree holding work that exists nowhere else"
    ),
    "unintegrated": (
        "its HEAD is not reachable from the integration revision, so removing "
        "it would destroy the only copy of that commit; merge it or discard it "
        "deliberately"
    ),
    "live-referenced": (
        "a live run pointer still claims this worktree; reconcile or stop that "
        "run first"
    ),
}


# Where a release with the archive option keeps a worktree's commits. One ref
# per worktree directory name; the ref is what keeps the commits reachable
# after the tree is gone, so it must be created before the removal, not after.
ARCHIVE_REF_PREFIX = "refs/reckon/archive/"


def _sweep_worktree_trash(repo: Path, *, apply: bool) -> list[dict[str, Any]]:
    """Remove leftover worktree trash a release's deletion step did not finish.

    Trash is written only after a tree has been moved off its registered path
    and deregistered, so removing it cannot strand a registered tree; a gc pass
    that runs with ``apply`` deletes each directory and prints it.
    """
    rows: list[dict[str, Any]] = []
    for root in _releasable_roots(repo):
        trash_root = root / WORKTREE_TRASH_DIRNAME
        if not trash_root.is_dir():
            continue
        for entry in sorted(trash_root.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            row: dict[str, Any] = {"path": str(entry), "removed": False}
            if apply:
                try:
                    shutil.rmtree(entry)
                except OSError as exc:
                    row["error"] = str(exc)
                else:
                    row["removed"] = True
                    print(f"removed worktree trash {entry}")
            rows.append(row)
    return rows


# The extraction's reason is held in its own constant rather than joining
# WITHHELD_REASONS: that mapping is asserted to name exactly the worktree-rule
# vocabulary, and an extraction is not a worktree rule — it has no commit to
# judge, only a kind to report.
RUN_DIRECTORY_EXTRACTION_REASON = (
    "a plain extraction with no git directory, so it has no commit to judge "
    "by containment; it is never removed, only reported"
)


# A registered worktree whose directory is gone is reported and left alone. The
# registration is another session's record of a tree it may still be reasoning
# about, so gc neither prunes it nor counts it reclaimed.
UNAVAILABLE_WORKTREE_REASON = (
    "git still registers this worktree but its directory is gone, so there is "
    "no commit or working tree to judge; it is reported and left in place, "
    "because removing its registration would discard another session's record"
)


# A registration whose directory still exists but cannot be read as a git
# working tree — a plain directory left where the tree was, or a damaged
# registration. It is reported with git's own reason and left in place for the
# same purpose as a vanished directory: the registration is another session's
# record of a tree it may still be reasoning about.
UNREADABLE_WORKTREE_REASON = (
    "git still registers this worktree and its directory exists, but it is not "
    "a git working tree, so there is no commit or status to judge; it is "
    "reported and left in place, because removing it would discard another "
    "session's record"
)


# A failure of this family is a defect in the sweep's own code rather than a
# refusal of the tree it happened on. Reported as an ordinary refusal it would
# read to the caller as gc declining a worktree, so it is re-raised with its
# traceback and names the line that is wrong.
PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError)


def _gc_partial_report(
    repo_root: Path,
    integrated_into: str,
    apply: bool,
    removed: list[str],
    refused: list[str],
    worktrees: list[dict[str, Any]],
    failed_path: str,
) -> dict[str, Any]:
    """The report a sweep stopped mid-pass can still make.

    ``refused`` is separate from ``removed``: a tree whose removal was
    attempted and failed is neither gone nor still just an unreached row, and a
    caller must not have to infer which rows those were by walking the
    classifications.
    """
    return {
        "repo": str(repo_root),
        "integrated_into": integrated_into,
        "dry_run": not apply,
        "removed_worktrees": list(removed),
        "refused_worktrees": list(refused),
        "worktrees": list(worktrees),
        "failed_path": failed_path,
    }


# An index that records no ledger path cannot be tied to a checkout, so it is
# offered for removal only once no reader has opened it for this long. Seven
# days is well past any live reader's refresh cadence yet far short of the
# multi-month accumulation this sweep exists to reclaim.
LEDGER_INDEX_UNRECORDED_MAX_AGE_DAYS = 7


def _orphan_ledger_indexes(
    *, apply: bool, now: datetime | None
) -> list[dict[str, Any]]:
    """Ledger caches whose checkout is gone, or stale and carrying no path.

    Each index is keyed per checkout, so a worktree that is later removed
    leaves a full-size index behind that nothing resolves again. An index
    recording a ledger path that no longer exists is an orphan outright; one
    recording no path at all is offered only once it has aged past
    :data:`LEDGER_INDEX_UNRECORDED_MAX_AGE_DAYS`, so a reader between opening
    and rebuilding an index is never swept. A cache file a live process holds
    is left alone whatever the age, and only ``ledger-*.sqlite`` files are
    matched, so the picker's ``picker-*.json`` inputs in the same root are
    untouched. Every row states its path and size, and removal happens only
    under ``--apply``.
    """
    from reckon import _store
    from reckon import ledger as ledger_module

    root = _store.cache_root("pick-input")
    if not root.is_dir():
        return []
    current = now or datetime.now(tz=UTC)
    stale_before = current - timedelta(days=LEDGER_INDEX_UNRECORDED_MAX_AGE_DAYS)
    entries: list[dict[str, Any]] = []
    for index in sorted(path for path in root.glob("ledger-*.sqlite") if path.is_file()):
        recorded = ledger_module.recorded_ledger_path(index)
        if recorded is not None:
            if Path(recorded).exists():
                continue
            reason = "recorded-checkout-gone"
        else:
            modified = datetime.fromtimestamp(index.stat().st_mtime, tz=UTC)
            if modified > stale_before:
                continue
            reason = "unrecorded-stale"
        entry = {
            "path": str(index),
            "size": index.stat().st_size,
            "recorded_ledger_path": recorded,
            "reason": reason,
            "action": "prune",
            "removed": False,
        }
        if apply and not _ledger_index_locked(index):
            index.unlink()
            entry["removed"] = True
        entries.append(entry)
    return entries


def garbage_collect(
    *,
    repo: str | Path,
    project: str | None = None,
    integrated_into: str = "HEAD",
    retention_days: int = 30,
    apply: bool = False,
    pin_unique_commits: bool = False,
    now: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Inspect or remove disposable workspaces and promoted transient state.

    ``pin_unique_commits`` releases a worktree — dirty, or clean with an
    unreachable head — whose commits have no patch-equivalent on the integration
    head by first pinning those commits into an archive ref under
    ``refs/reckon/archive/``; without it such a tree is kept, as before.
    ``run_id`` confines the pass to that run's own tree and to that run's
    pointer and run directory, so clearing one held tree can never reach a
    peer's; a recorded tree that is gone or no longer registered is reported as
    already removed rather than refused.
    """
    if retention_days < 0:
        raise CrewError("retention days cannot be negative")
    repo_root = Path(repo).resolve()
    from reckon.crew.recovery import _resolve_commit

    if not _resolve_commit(repo_root, integrated_into):
        raise CrewError(f"integration revision {integrated_into!r} is not a commit")
    confined_to: Path | None = None
    already_removed_run = ""
    already_removed_worktree = ""
    if run_id:
        confined_to = _run_worktree_path(repo_root, project, run_id)
        registered = {
            path.resolve()
            for path in _registered_worktrees(repo_root)
            if path != repo_root
        }
        if confined_to not in registered:
            # The run's recorded tree is no longer a worktree this repository
            # registers: an earlier release already took it. That is the state a
            # caller reaches after pinning the tree's commits and removing it by
            # hand, and it is the ordinary end of the run's life rather than a
            # refusal, so nothing is confined here, the run's remaining leftovers
            # are still cleared below, and the tree is reported as already removed.
            already_removed_run = run_id
            already_removed_worktree = str(confined_to)
            confined_to = None
    worktrees: list[dict[str, Any]] = []
    removed: list[str] = []
    refused: list[str] = []
    withheld: list[dict[str, Any]] = []
    residue_report: list[dict[str, Any]] = []
    trash_rows: list[dict[str, Any]] = []
    failed_path = ""
    try:
        roots = _workspace_roots(repo_root)
        runs_root = runs_dir()
        # A worktree any live pointer names is live-referenced, whatever phase,
        # process liveness or integration state that pointer carries: only
        # promotion or discard, which remove the pointer, release the tree.
        claims = _live_pointer_worktrees()
        shadow_records = _shadow_worktree_records(repo_root, project)
        ledger_records = _ledgered_records(repo_root, project)
        # The managed set is the workspace registry; a tree the promotion boundary
        # already walks must be one gc sees too, and that includes registered
        # worktrees a worker created under a run directory.
        candidates = [
            path
            for path in _registered_worktrees(repo_root)
            if path != repo_root
            and (
                any(path.is_relative_to(root) for root in roots)
                or path.is_relative_to(runs_root)
            )
            and (not run_id or path.resolve() == confined_to)
        ]
        worktrees = []
        for path in sorted(candidates):
            failed_path = str(path)
            worktrees.append(
                _inspect_workspace(
                    repo_root,
                    path,
                    integrated_into,
                    claims.get(path.resolve(), ()),
                    shadow_records.get(path.resolve()),
                    raise_on_unavailable=False,
                    release_residue=True,
                )
            )
        # Extractions carry no git directory, so the worktree registry never sees
        # them; they are reported beside the registry rows under their own kind.
        for path in _run_directory_extractions(runs_root):
            failed_path = str(path)
            worktrees.append(_extraction_report(path))
        if apply:
            for item in worktrees:
                if item["classification"] not in RECLAIMABLE_CLASSES:
                    # An explicit pin is the one thing that reclaims an
                    # otherwise-withheld dirty row; the branch below releases it.
                    pin_reclaims = _pinnable_unique_commits(item, pin_unique_commits)
                    if not pin_reclaims:
                        continue
                path = Path(item["path"])
                failed_path = str(path)
                try:
                    current_claims = _live_pointer_worktrees().get(path.resolve(), [])
                    if current_claims:
                        item["classification"] = "live-referenced"
                        item["claimed_by_live_runs"] = sorted(current_claims)
                        continue
                    release: dict[str, Any] | None = None
                    if item["classification"] == "dirty-integrated":
                        current = _inspect_workspace(
                            repo_root,
                            path,
                            integrated_into,
                            (),
                            raise_on_unavailable=False,
                            release_residue=True,
                        )
                        if (
                            current["classification"] != "dirty-integrated"
                            or current["head"] != item["head"]
                        ):
                            item.update(current)
                            continue
                        saved = _save_and_release_worktree(
                            repo_root,
                            path,
                            integrated_into,
                            _residue_run_record(path, ledger_records),
                        )
                        item.update(saved)
                        if "unique" in saved["residue_classes"].values():
                            residue_report.append({"worktree": str(path), **saved})
                    elif item["classification"] == "dirty":
                        # Pinning was requested for this row: re-read it against
                        # the live tree, then keep its commits in an archive ref
                        # before the residue-preserving release clears the tree.
                        current = _inspect_workspace(
                            repo_root,
                            path,
                            integrated_into,
                            (),
                            raise_on_unavailable=False,
                            release_residue=True,
                        )
                        if (
                            current["classification"] != "dirty"
                            or current["head"] != item["head"]
                        ):
                            item.update(current)
                            continue
                        archive_ref = f"{ARCHIVE_REF_PREFIX}{path.name}"
                        saved = _save_and_release_worktree(
                            repo_root,
                            path,
                            integrated_into,
                            _residue_run_record(path, ledger_records),
                            archive_ref=archive_ref,
                        )
                        item.update(saved)
                        if "unique" in saved["residue_classes"].values():
                            residue_report.append({"worktree": str(path), **saved})
                    elif item["classification"] == "unintegrated":
                        # A clean, unreachable tree carrying commits with no
                        # patch-equivalent on the integration head. There is no
                        # residue to save, so the pin alone keeps them and the
                        # tree is then released whole. Re-read it first, as the
                        # dirty branch does, so a head that moved since the
                        # classification is not released on a stale reading.
                        current = _inspect_workspace(
                            repo_root,
                            path,
                            integrated_into,
                            (),
                            raise_on_unavailable=False,
                            release_residue=True,
                        )
                        if (
                            current["classification"] != "unintegrated"
                            or current["head"] != item["head"]
                        ):
                            item.update(current)
                            continue
                        archive_ref = f"{ARCHIVE_REF_PREFIX}{path.name}"
                        _pin_archived_commits(repo_root, archive_ref, item["head"])
                        item["archive_ref"] = archive_ref
                        release = _release_worktree_whole(repo_root, path)
                    elif item["classification"] == "disposable":
                        shadow_record = shadow_records.get(path.resolve())
                        if shadow_record is None or not _shadow_patch_retained(
                            shadow_record
                        ):
                            item["classification"] = "unintegrated"
                            continue
                        release = _release_worktree_whole(repo_root, path)
                    elif _git(
                        path, "status", "--porcelain", "--untracked-files=all"
                    ).stdout.strip():
                        saved = _save_and_release_worktree(
                            repo_root,
                            path,
                            integrated_into,
                            _residue_run_record(path, ledger_records),
                        )
                        item.update(saved)
                        if "unique" in saved["residue_classes"].values():
                            residue_report.append({"worktree": str(path), **saved})
                    else:
                        release = _release_worktree_whole(repo_root, path)
                    if release is not None and not release.get("released", True):
                        # The helper moved nothing: this tree is not one it may
                        # take. A withheld release is reported as withheld with
                        # its reason, never as a removal that happened.
                        item["worktree_withheld"] = release["withheld"]
                        withheld.append(
                            {"worktree": str(path), "reason": release["withheld"]}
                        )
                    else:
                        removed.append(str(path))
                except Exception:
                    if path.is_dir():
                        # A removal this pass attempted and did not complete:
                        # the tree is neither removed nor an unreached row, so
                        # it is recorded in its own list before the failure
                        # propagates.
                        refused.append(str(path))
                        raise
                    # The directory vanished between classification and this
                    # action — a peer's release, typically. Nothing was pinned,
                    # saved or removed, so the row is reported as gone before
                    # action and the sweep carries on to the remaining trees.
                    # An action that fails while the directory is still there
                    # stays a refused row and a stopped sweep, as above.
                    item.update(
                        _inspect_workspace(
                            repo_root,
                            path,
                            integrated_into,
                            (),
                            raise_on_unavailable=False,
                        )
                    )
                    item["gone_before_action"] = True
                    item["detail"] = (
                        "gone before action: the directory was removed between "
                        "this pass's classification and its action, so no "
                        "commit was pinned, no residue saved and nothing was "
                        "removed"
                    )
            # No blanket `worktree prune` after the pass: each removal above already
            # deregisters its own tree, while a prune would also drop the
            # registration of a tree whose directory vanished outside git — exactly
            # the another-session record this pass reports and leaves in place.

        # Trash left by a release whose deletion step did not finish is swept
        # here: the trees it holds were moved off their registered paths and
        # deregistered before the copy was written, so removing it can never
        # strand a registered tree. Each one removed is printed by name.
        trash_rows = _sweep_worktree_trash(repo_root, apply=apply)

        ledgered_records = {
            str(record.get("run_id") or ""): record
            for record in _ledgered_records(repo_root, project)
            if record.get("run_id")
        }
        ledgered = set(ledgered_records)
        pointer_reports: list[dict[str, Any]] = []
        for record in list_live():
            record_run_id = str(record.get("run_id") or "")
            if run_id and record_run_id != run_id:
                continue
            if (
                record_run_id not in ledgered
                or record_process_alive(record) is not False
            ):
                continue
            report = {"run_id": record_run_id, "action": "reap", "removed": False}
            if apply:
                with _pointer_lock(record_run_id):
                    current = read_pointer(record_run_id)
                    if (
                        record_run_id in ledgered
                        and record_process_alive(current) is False
                    ):
                        pointer_path(record_run_id).unlink()
                        report["removed"] = True
            pointer_reports.append(report)

        cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(days=retention_days)
        live_ids = {str(record.get("run_id") or "") for record in list_live()}
        run_reports: list[dict[str, Any]] = []
        run_file_reports: list[dict[str, Any]] = []
        from reckon.crew.fleet_supervisor import bound_run_files, run_file_limit_bytes

        run_file_limit = run_file_limit_bytes(repo_root, project)
        if runs_root.is_dir():
            for directory in sorted(
                path for path in runs_root.iterdir() if path.is_dir()
            ):
                if run_id and directory.name != run_id:
                    continue
                if directory.name not in ledgered or directory.name in live_ids:
                    continue
                record = ledgered_records.get(directory.name, {})
                run_file_reports.extend(
                    bound_run_files(
                        directory, record, limit_bytes=run_file_limit, apply=apply
                    )
                )
                modified = datetime.fromtimestamp(
                    directory.stat().st_mtime, tz=timezone.utc
                )
                if modified > cutoff:
                    continue
                figure_status = {
                    field: _figure_retention(record, field)
                    for field in STREAM_DERIVED_FIELDS
                }
                missing = tuple(
                    field
                    for field, state in figure_status.items()
                    if state == "missing"
                )
                absent = tuple(
                    field
                    for field, state in figure_status.items()
                    if state == "explicitly-absent"
                )
                if missing:
                    report = {
                        "run_id": directory.name,
                        "path": str(directory),
                        "action": "withheld",
                        "removed": False,
                        "withheld": "missing-derived-figure",
                        "reason": (
                            "the run is past its retention window but the figures "
                            "derived from its stream were never recorded; their "
                            "keys are absent from the ledger row, so the derivation "
                            "has not run and reaping would destroy the only copy of "
                            + ", ".join(missing)
                        ),
                        "missing_figures": list(missing),
                        "figure_status": figure_status,
                    }
                    run_reports.append(report)
                    continue
                report = {
                    "run_id": directory.name,
                    "path": str(directory),
                    "action": "prune",
                    "removed": False,
                    "explicitly_absent_figures": list(absent),
                    "figure_status": figure_status,
                }
                if apply:
                    if any(
                        str(record.get("run_id") or "") == directory.name
                        for record in list_live()
                    ):
                        continue
                    shutil.rmtree(directory)
                    report["removed"] = True
                run_reports.append(report)
        report_tree_rows = _plan_review_raw_reports(
            repo_root=repo_root,
            project=project,
            run_id=run_id,
            apply=apply,
            now=now,
            live_ids=live_ids,
        ) + _run_report_trees(
            project=project,
            apply=apply,
            now=now,
            run_id=run_id,
            live_ids=live_ids,
            ledgered=ledgered,
            retention_days=retention_days,
        )
        raw_report_reports = [
            row for row in report_tree_rows if row["family"] == "plan-review"
        ]
        lock_reports = _stale_run_locks(
            apply=apply,
            now=now,
            run_id=run_id,
            live_ids=live_ids,
            ledgered=ledgered,
            retention_days=retention_days,
        )
        run_lock_reports: list[dict[str, Any]] = []
        if already_removed_run and already_removed_run not in live_ids:
            # An already-removed run's tree is gone, so its lock is cleared
            # regardless of age: the retention window judges a lock left behind
            # by a swept tree, and there is none to judge. It is dropped from
            # the age-based list so the run's lock is reported once.
            run_lock_reports = [_clear_run_lock(run_id, apply=apply)]
            lock_reports = [
                row for row in lock_reports if row["run_id"] != run_id
            ]
        unledgered_run_reports = _unledgered_run_directories(
            apply=apply,
            now=now,
            run_id=run_id,
            live_ids=live_ids,
            ledgered=ledgered,
            retention_days=retention_days,
        )
        # The ledger index cache is global, keyed by checkout rather than by
        # run, so a pass confined to one run's artifacts does not reach it.
        ledger_index_reports = (
            _orphan_ledger_indexes(apply=apply, now=now) if run_id is None else []
        )
    except PROGRAMMING_ERRORS:
        # Re-raised untouched: the traceback names the defective line, where a
        # refusal would have named only the tree the defect happened to hit.
        raise
    except Exception as exc:
        raise GcSweepError(
            str(exc),
            _gc_partial_report(
                repo_root,
                integrated_into,
                apply,
                removed,
                refused,
                worktrees,
                failed_path,
            ),
        ) from exc

    # `--apply` removes the integrated and the disposable, so a report whose
    # headline figure is `disposable` says 0 while it would in fact reclaim
    # dozens. A caller reading that concludes nothing is reclaimable and the
    # accumulation grows — measured at 46 worktrees in one project, 40 of them
    # integrated. So every row states whether it would be reclaimed, and a row
    # that would not says which condition holds it back.
    for item in worktrees:
        classification = str(item["classification"])
        item["reclaimable"] = classification in RECLAIMABLE_CLASSES or (
            _pinnable_unique_commits(item, pin_unique_commits)
        )
        if not item["reclaimable"]:
            if classification == "extraction":
                item["withheld"] = RUN_DIRECTORY_EXTRACTION_REASON
            elif classification == "unavailable":
                item["withheld"] = (
                    UNREADABLE_WORKTREE_REASON
                    if item.get("unavailable_state") == "unreadable"
                    else UNAVAILABLE_WORKTREE_REASON
                )
            else:
                item["withheld"] = WITHHELD_REASONS.get(
                    classification, "unrecognised classification"
                )

    counts = {
        name: sum(item["classification"] == name for item in worktrees)
        for name in (
            "integrated",
            "disposable",
            "dirty",
            "dirty-integrated",
            "unintegrated",
            "live-referenced",
        )
    }
    counts["reclaimable"] = sum(bool(item["reclaimable"]) for item in worktrees)
    ledgers = sorted(
        {
            str(ledger.ledger_path(name, root=repo_root))
            for name in _gc_projects(repo_root, project)
        }
    )
    # The withholding count is a payload number, not only printed text: a
    # corpus that stops shrinking is legible to a caller that never renders
    # prose, and the dry-run/apply split reads the same either way. Beside it
    # sits the number a reaper would now reclaim that the pre-existing rule
    # withheld forever — directories whose figures were derived and recorded as
    # explicitly absent, because the stream those figures would come from is
    # already gone — so the difference this makes is stated rather than
    # inferred by a reader.
    run_directories_withheld = sum(
        1 for item in run_reports if item.get("action") == "withheld"
    )
    run_directories_reaped_with_explicitly_absent_figures = sum(
        1
        for item in run_reports
        if item.get("action") == "prune" and item.get("explicitly_absent_figures")
    )
    return {
        "dry_run": not apply,
        "repo": str(repo_root),
        "ledger": ledgers,
        "integrated_into": integrated_into,
        "counts": counts,
        "worktrees": worktrees,
        "removed_worktrees": removed,
        "withheld_worktrees": withheld,
        "residue_report": residue_report,
        "worktree_trash": trash_rows,
        "worktree_trash_removed": sum(
            1 for item in trash_rows if item.get("removed")
        ),
        "pointers": pointer_reports,
        "run_directories": run_reports,
        "run_files": run_file_reports,
        "run_directories_withheld": run_directories_withheld,
        "run_directories_reaped_with_explicitly_absent_figures": (
            run_directories_reaped_with_explicitly_absent_figures
        ),
        "plan_review_raw_reports": raw_report_reports,
        "plan_review_raw_reports_pruned": sum(
            1 for item in raw_report_reports if item["action"] == "prune"
        ),
        "plan_review_raw_reports_withheld": sum(
            1 for item in raw_report_reports if item["action"] == "withheld"
        ),
        "report_trees": report_tree_rows,
        "report_trees_removed": sum(
            1 for item in report_tree_rows if item.get("removed")
        ),
        "lock_files": lock_reports,
        "lock_files_removed": sum(1 for item in lock_reports if item.get("removed")),
        "already_removed": bool(already_removed_run),
        "already_removed_run": already_removed_run,
        "already_removed_worktree": already_removed_worktree,
        "run_lock": run_lock_reports,
        "unledgered_run_directories": unledgered_run_reports,
        "unledgered_run_directories_removed": sum(
            1 for item in unledgered_run_reports if item.get("removed")
        ),
        "ledger_indexes": ledger_index_reports,
        "ledger_indexes_removed": sum(
            1 for item in ledger_index_reports if item.get("removed")
        ),
        "scratch": (
            garbage_collect_orphan_scratch(apply=apply) if run_id is None else None
        ),
    }


# ── Scratch directories ─────────────────────────────────────────────────────
#
# The disk that fills is not the worktree pool but the node-local temp tree: a
# run's own scratch directory sits beneath a reckon-owned root, but a run before
# this rule made trees at explicit `/tmp/<name>` paths its brief named, which
# nothing that knows the run could find. Two facts settle what may be removed.
# The run's terminal record — a ledger row written by promotion, or a discard
# marker — licenses removal. A live pointer forbids it whatever the directory's
# age, because a complete-but-unpromoted run is revisited hours after its worker
# exits and an age-keyed reaper would delete the scratch a resume needs. A tree
# the node owns but no run can be attributed to is reported by the survey and
# left to the orphan sweep, which removes it by age once its ctime is older than
# SCRATCH_GRACE_SECONDS and no process holds it: the survey cannot name its
# owner, so it does not delete it, and the sweep is the one place that decision
# is made. This is the same rule promotion's release step already applies to a
# run's own scratch, exposed as a survey over the whole root.

SCRATCH_LIVE = "live"


SCRATCH_TERMINAL = "terminal"


SCRATCH_UNATTRIBUTED = "unattributed"


# A stray tree beside the scratch root is measured with a bound, because the
# node's temp tree is shared and a single unattributed directory there can hold
# a whole corpus; the figure names a tree that filled the disk, it is not an
# accounting figure a caller reconciles against.
_UNATTRIBUTED_SIZE_LIMIT = 200_000


SCRATCH_GRACE_SECONDS = 2 * 60 * 60


def _scratch_ctime(path: Path) -> float:
    """Read directory ctime, which archive extraction cannot backdate."""
    return path.stat().st_ctime


def _scratch_process_holders(root: Path) -> dict[str, list[int]]:
    """Read process cwd and fd links once for every scratch child."""
    holders: dict[str, list[int]] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        for link in (entry / "cwd", *(entry / "fd").glob("*")):
            try:
                relative = link.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if relative.parts:
                holders.setdefault(relative.parts[0], []).append(int(entry.name))
    return holders


def garbage_collect_orphan_scratch(
    *, apply: bool = False, now: float | None = None
) -> dict[str, Any]:
    """Report aged, unclaimed scratch; remove it only after rechecking claims.

    The configured root is the only permitted target. Directory ctime is used
    because extracted archives can carry arbitrarily old file mtimes.
    """
    from reckon.crew.dispatch import tree_size_bytes, worker_scratch_root

    root = worker_scratch_root()
    # A private state home cannot prove that the host-wide default root has no
    # live pointers in the operator's state home.
    if os.environ.get("RECKON_HOME") and not os.environ.get(
        "RECKON_WORKER_SCRATCH_ROOT"
    ):
        return {
            "root": str(root),
            "entries": [],
            "removed": [],
            "bytes_freed": 0,
            "withheld": "state and scratch roots differ",
        }
    if root.is_symlink() or root.resolve() != root.absolute():
        raise CrewError(f"scratch sweep refuses noncanonical root {root}")
    if not root.exists():
        return {"root": str(root), "entries": [], "removed": [], "bytes_freed": 0}
    if not root.is_dir() or not Path("/proc").is_dir():
        raise CrewError(f"scratch sweep cannot safely inspect {root} and /proc")
    stamp = time.time() if now is None else now
    live_ids = {str(row.get("run_id") or "") for row in list_live()}
    holders = _scratch_process_holders(root)
    entries: list[dict[str, Any]] = []
    removed: list[str] = []
    bytes_freed = 0
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            entries.append(
                {
                    "path": str(child),
                    "bytes": 0,
                    "removed": False,
                    "withheld": "not a real directory",
                }
            )
            continue
        size = tree_size_bytes(child)
        age = max(0.0, stamp - _scratch_ctime(child))
        reason = ""
        if child.name in live_ids:
            reason = "live pointer"
        elif age < SCRATCH_GRACE_SECONDS:
            reason = f"ctime within {SCRATCH_GRACE_SECONDS} second grace"
        elif holders.get(child.name):
            reason = f"held by process {holders[child.name]}"
        entry: dict[str, Any] = {
            "path": str(child),
            "bytes": size,
            "ctime_age_seconds": age,
            "removed": False,
            "withheld": reason,
        }
        entries.append(entry)
    if apply:
        # A second /proc pass catches holders that appeared during sizing,
        # without rescanning all processes separately for every directory.
        current_holders = _scratch_process_holders(root)
        for entry in entries:
            if entry["withheld"]:
                continue
            child = Path(entry["path"])
            if pointer_path(child.name).exists():
                entry["withheld"] = "live pointer appeared during sweep"
            elif current_holders.get(child.name):
                entry["withheld"] = f"held by process {current_holders[child.name]}"
            elif child.is_symlink() or child.resolve().parent != root:
                entry["withheld"] = "path left configured scratch root"
            elif stamp - _scratch_ctime(child) < SCRATCH_GRACE_SECONDS:
                entry["withheld"] = "ctime refreshed during sweep"
            else:
                shutil.rmtree(child)
                entry["removed"] = True
                removed.append(str(child))
                bytes_freed += entry["bytes"]
    return {
        "root": str(root),
        "entries": entries,
        "removed": removed,
        "bytes_freed": bytes_freed,
        "would_free_bytes": sum(row["bytes"] for row in entries if not row["withheld"]),
    }


def _scratch_disposition(
    path: Path,
    *,
    live_ids: set[str],
    ledgered: set[str],
    discard_recorded: set[str],
) -> str:
    """Classify one scratch directory by the run's own records, never by age.

    The single decision point for removal, kept as its own function so a test
    can replace it: the declared mutation for this survey makes the disposition
    depend on the directory's age instead, under which a complete-but-unpromoted
    run's scratch is deleted and the case keeping it fails.
    """
    run_id = path.name
    if run_id in live_ids:
        return SCRATCH_LIVE
    if run_id in ledgered or run_id in discard_recorded:
        return SCRATCH_TERMINAL
    return SCRATCH_UNATTRIBUTED


def _processes_holding(path: Path) -> list[dict[str, Any]]:
    """Name the processes holding ``path`` as their cwd or as an open file.

    Read-only and best-effort: ``/proc`` may be absent or a process may exit
    mid-scan, and a scan that raised would turn a report into a refusal. A
    holder is only ever reported, never killed — the survey's whole point is to
    let a human decide what a tree nobody owns is doing.
    """
    holders: list[dict[str, Any]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return holders
    target = path.resolve()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        for link in (entry / "cwd", *sorted((entry / "fd").glob("*"))):
            try:
                resolved = link.resolve()
            except OSError:
                continue
            if resolved == target or resolved.is_relative_to(target):
                holders.append({"pid": pid, "via": link.name})
                break
    return holders


def garbage_collect_scratch(
    *,
    repo: str | Path,
    project: str | None = None,
    apply: bool = False,
    scratch_root: str | Path | None = None,
    tmp_root: str | Path | None = None,
) -> dict[str, Any]:
    """Survey the node-local scratch directories and remove the terminal ones.

    A run's scratch directory is removed here only when the run has no live
    pointer and its terminal record exists — a ledger row or a discard marker —
    and this survey's own decision is never made by age. Every path and its
    size are named in the result before anything is deleted, and the whole
    survey is a dry run unless ``apply`` is given. A directory no run can be
    attributed to,
    whether beneath the scratch root or a stray tree beside it, is reported
    here with its ctime, its size and any process holding it, and is left for
    the orphan sweep: :func:`garbage_collect_orphan_scratch` removes such a
    directory by age, once its ctime is older than SCRATCH_GRACE_SECONDS
    (7200 seconds), after re-checking live pointers and holders. So the two
    agree — an unattributed directory is removed by age, and only by that
    sweep.
    """
    from reckon.crew.dispatch import tree_size_bytes, worker_scratch_root
    from reckon.crew.promotion import discard_record_path

    root = Path(scratch_root) if scratch_root is not None else worker_scratch_root()
    root = root.resolve()
    tmp = Path(tmp_root).resolve() if tmp_root is not None else root.parent
    ledgered = _ledgered_run_ids(repo, project)
    live_ids = {str(record.get("run_id") or "") for record in list_live()}

    children: list[Path] = []
    if root.is_dir():
        children = sorted(
            path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
        )
    discard_recorded = {
        path.name for path in children if discard_record_path(path.name).is_file()
    }

    directories: list[dict[str, Any]] = []
    removed: list[str] = []
    for child in children:
        state = _scratch_disposition(
            child,
            live_ids=live_ids,
            ledgered=ledgered,
            discard_recorded=discard_recorded,
        )
        size = tree_size_bytes(child)
        report: dict[str, Any] = {
            "run_id": child.name,
            "path": str(child),
            "bytes": size,
            "state": state,
            "removed": False,
        }
        if state == SCRATCH_UNATTRIBUTED:
            report["ctime"] = _iso_ctime(child)
            report["held_by"] = _processes_holding(child)
        if state == SCRATCH_TERMINAL and apply:
            shutil.rmtree(child)
            report["removed"] = True
            removed.append(str(child))
        directories.append(report)

    unattributed: list[dict[str, Any]] = []
    if tmp.is_dir() and tmp != root:
        for child in sorted(
            path for path in tmp.iterdir() if path.is_dir() and not path.is_symlink()
        ):
            if child.resolve() == root:
                continue
            try:
                if child.stat().st_uid != os.getuid():
                    continue
            except OSError:
                continue
            unattributed.append(
                {
                    "path": str(child),
                    "bytes": tree_size_bytes(child, limit=_UNATTRIBUTED_SIZE_LIMIT),
                    "ctime": _iso_ctime(child),
                    "held_by": _processes_holding(child),
                }
            )

    return {
        "dry_run": not apply,
        "scratch_root": str(root),
        "directories": directories,
        "removed": removed,
        "unattributed": unattributed,
    }
