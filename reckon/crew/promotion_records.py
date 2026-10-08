from __future__ import annotations

# Imports below the definitions resolve sibling cycles after names are bound.
# ruff: noqa: E402
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon import (
    ledger,
)
from reckon.crew.dispatch import (
    WORKER_SCRATCH_BUDGET_BYTES,
    _capture_member_session,
    remove_worker_scratch,
)
from reckon.crew.node import (
    CrewError,
)
from reckon.crew.reports import (
    parse_manifest,
)
from reckon.crew.routing import (
    RECLAIMABLE_CLASSES,
    WITHHELD_REASONS,
    _disposable_member_id,
    _inspect_workspace,
    _repository_tree_snapshot,
    mounted_repository_projects,
)
from reckon.crew.runs import (
    _live_worktree_claims,
    _manifest_freshness,
    _write_json,
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    pointer_path,
    read_pointer,
    run_dir,
)


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



from reckon.crew.promotion_checks import (
    _fresh_manifest,
)
from reckon.crew.promotion_evidence import (
    _ATTEMPT_RECORD_NAME,
    _WORKER_RECORD_NAME,
    _recorded_exit_status,
    _run_changed_nothing,
)
from reckon.crew.promotion_release import (
    _release_after_promotion,
)
from reckon.crew.promotion_scope import (
    _changed_paths_inside_repository,
    _is_shadow,
)
