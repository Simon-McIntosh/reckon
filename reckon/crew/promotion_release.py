from __future__ import annotations

# Imports below the definitions resolve sibling cycles after names are bound.
# ruff: noqa: E402
import json
import re
import shutil
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon import (
    ledger,
)
from reckon.crew import review as review_module
from reckon.crew import rollout
from reckon.crew.dispatch import (
    WORKER_SCRATCH_BUDGET_BYTES,
    remove_worker_scratch,
    tree_size_bytes,
    worker_scratch_root,
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
    _git,
    _inspect_workspace,
    _live_pointer_worktrees,
    _signal_process_group,
    run_directory_of,
)
from reckon.crew.runs import (
    _drain_row,
    _utc_now,
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    list_live,
    process_alive,
    record_process_alive,
)

_CITED_ABSOLUTE_PATH = re.compile(r"/(?:[^\s\"'`()\[\]{}<>,;]+)")


_CITED_PATH_EDGE_PUNCTUATION = ".,:;)]}\"'"


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



from reckon.crew.promotion_evidence import (
    _manifest_text,
)
from reckon.crew.promotion_records import (
    _manifest_reads_blocked,
    _release_scratch_when_release_raised,
    _release_terminal_manifest,
    _worktree_audit,
)
from reckon.crew.promotion_scope import (
    _is_shadow,
    _landing_refusal,
    _promoted_revision,
    _restore_landing_writes,
)
