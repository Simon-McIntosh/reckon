from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from reckon import _backends, _store, capabilities, ledger
from reckon.crew import review as review_module
from reckon.crew import rollout
from reckon.crew.dispatch import (
    _backend_settings,
    _capture_member_session,
    project_mount_repository,
    resolve_project_repository,
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
from reckon.crew.reports import (
    _NONE_VALUES as _MANIFEST_NOTHING,
)
from reckon.crew.reports import ManifestParseError, parse_manifest
from reckon.crew.routing import (
    RECLAIMABLE_CLASSES,
    WITHHELD_REASONS,
    _git,
    _inspect_workspace,
    _repository_tree_snapshot,
    _shadow_patch_retained,
    _shadow_worktree_records,
    _signal_process_group,
)
from reckon.crew.runs import (
    _live_worktree_claims,
    _manifest_freshness,
    _pointer_lock,
    _utc_now,
    _write_json,
    drain,
    list_live,
    pointer_path,
    process_alive,
    read_pointer,
    record_process_alive,
    run_dir,
)

# ── Promotion: the transient record becomes committed evidence ──────────────

_COORDINATOR_LANDING_AUTHOR = "reckon-build"


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
        resolved = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode:
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


def _cumulative_diff(*, cwd: Path, base: str, head: str) -> _CumulativeDiff:
    """Return paths and counts from one unfiltered base-to-tip diff."""
    if not base:
        return _CumulativeDiff((), {"available": False, "reason": "missing_base"})
    for revision in (base, head):
        resolved = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
        if resolved.returncode:
            return _CumulativeDiff(
                (), {"available": False, "reason": "unresolvable_revision"}
            )
    result = subprocess.run(
        ["git", "diff", "--numstat", "--no-renames", "-z", f"{base}..{head}", "--"],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return _CumulativeDiff((), {"available": False, "reason": "diff_unavailable"})
    added = removed = files = 0
    paths: list[str] = []
    for raw_line in (item for item in result.stdout.split(b"\0") if item):
        fields = raw_line.split(b"\t", 2)
        if len(fields) != 3:
            continue
        files += 1
        added += int(fields[0]) if fields[0].isdigit() else 0
        removed += int(fields[1]) if fields[1].isdigit() else 0
        paths.append(os.fsdecode(fields[2]))
    return _CumulativeDiff(
        tuple(paths), {"added": added, "removed": removed, "files": files}
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


def _commit_canonical_id(root: Path, revision: str) -> str | None:
    """Return the canonical object id one revision names, or None.

    The same resolution the scope paths use, returning the canonical id rather
    than a boolean so an abbreviated or branch-named revision can be compared
    against another spelling of the same commit. None is an unresolvable
    revision, never evidence of absence.
    """
    probe = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    canonical = probe.stdout.strip()
    if probe.returncode or not canonical:
        return None
    return canonical


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
    """
    if verdict != "passed" or str(no_commit_reason).strip():
        return None

    # First ask the worker, because it already answered. The manifest's
    # `commits:` line is delivered evidence that Reckon holds and, until now,
    # discarded: a coordinator that omitted one flag produced a ledger saying
    # the node succeeded with nothing pointing at the work. Naming the exact
    # revisions is more use than describing the condition, so the manifest is
    # read before the repository check below.
    tree = Path(str(record.get("worktree") or ""))
    manifest_present, fresh = _manifest_freshness(record)
    delivered: dict[str, Any] = {}
    if manifest_present and fresh and tree.is_dir():
        try:
            delivered = parse_manifest(
                Path(str(record["manifest_path"])).read_text(encoding="utf-8")
            )
        except (OSError, KeyError, ValueError):
            delivered = {}
    declared = [
        str(sha).strip() for sha in (delivered.get("commits") or []) if str(sha).strip()
    ]

    presented = [str(sha).strip() for sha in commits if str(sha).strip()]
    if presented:
        # A promotion that presents commits is the case the commitless guard
        # cannot see: its condition returns on any non-empty list, so a
        # presentation naming one commit of the manifest's declared eight
        # passes through untouched and the boundary check runs against the one.
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
    unresolved = _unresolved_citations(tree, declared)
    if unresolved:
        raise CrewError(
            f"run {run_id!r} cites "
            + ", ".join(repr(entry) for entry in unresolved)
            + " as a commit, but that identifier does not resolve to an object "
            f"in the run repository ({tree}). A value assembled rather than "
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
    stated = [
        candidate
        for candidate in declared
        if candidate and _commit_resolves_in(tree, candidate)
    ]
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
    if not base or not tree.is_dir():
        return None
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=tree,
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


_PRESERVED_GATE_LOG_NAME = "gate.log"


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


def _merged_gate_finding(
    base_verdict: str,
    integrated_verdict: str,
    *,
    integrated_revision: str,
    exit_status: int | None,
    reason: str | None,
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
    """
    if base_verdict != "passed" or integrated_verdict == "passed":
        return None
    return {
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


def rerun_gate_at_integrated_revision(
    *,
    repository: Path,
    gate_check: Mapping[str, Any] | None,
    base_verdict: str = "passed",
    integrated_revision: str = "HEAD",
    timeout_seconds: float = 300.0,
    command: str | None = None,
) -> dict[str, Any]:
    """Re-run one gate against the tree that ships, and compare its verdict.

    A worker's gate runs against the base revision its worktree branched from,
    so a contract that lands after that base never binds the worker's run: the
    run is legitimately green, nothing re-checks the merged tree, and the merge
    turns the primary branch red. This re-runs the gate's own command against a
    repository on the integrated revision — the tree the coordinator is about
    to push — and reports whether a base-green gate still holds there.

    The command is executed only when the repository actually sits on the
    integrated revision: a run against any other tree verifies the wrong tree,
    so it never executes and the reason is stated. The base verdict is taken as
    given — it is the gate the run already recorded, which this check tests
    rather than re-creates.

    A gate that did not run, or did not finish within the bound, is reported
    as ``not-run`` with its reason, never as passed: an unmeasured re-run must
    not read as a verified one.

    An explicit ``command`` is run in place of the run's stored gate command,
    so a caller can measure a wider suite — a whole-repository one — than the
    node's own gate, and can do so on a run that stored none. The report names
    the command actually executed and whether it came from the option or the
    stored row, so a reader can tell a supplied command from the recorded one.
    """
    base = str(base_verdict).strip().lower()
    if base not in ledger.GATE_VERDICTS:
        raise CrewError(
            f"base gate verdict {base_verdict!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; the base verdict is the gate "
            "the run already recorded, which this re-run is compared against"
        )
    stored_command = str((gate_check or {}).get("command") or "").strip()
    supplied = str(command or "").strip()
    command = supplied or stored_command
    command_source = "option" if supplied else ("stored" if stored_command else None)
    integrated = _commit_canonical_id(repository, str(integrated_revision))
    checkout = _commit_canonical_id(repository, "HEAD")
    report: dict[str, Any] = {
        "base_verdict": base,
        "integrated_verdict": "not-run",
        "integrated_revision": integrated or str(integrated_revision),
        "checkout_revision": checkout or "",
        "checkout_on_integrated_revision": bool(
            integrated and checkout and integrated == checkout
        ),
        "gate_command": command or None,
        "gate_command_source": command_source,
        "ran": False,
        "exit_status": None,
        "timed_out": False,
        "reason": None,
        "finding": None,
    }
    if not command:
        reason = "no gate command is stored to re-run"
    elif integrated is None:
        reason = (
            f"integrated revision {integrated_revision!r} does not resolve to "
            "a commit in the repository"
        )
    elif not checkout:
        reason = "the repository has no resolvable HEAD to run the gate against"
    elif checkout != integrated:
        reason = (
            f"the checkout is at {checkout[:12]}, not the integrated revision "
            f"{integrated[:12]}: a gate run here would verify the wrong tree. "
            "Check the integrated revision out, or name the revision the "
            "checkout actually carries"
        )
    else:
        reason = None
        try:
            result = subprocess.run(
                ["sh", "-c", command],
                cwd=str(repository),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            report.update(ran=True, timed_out=True)
        else:
            report["ran"] = True
            report["exit_status"] = result.returncode
            report["integrated_verdict"] = (
                "passed" if result.returncode == 0 else "failed"
            )
    if reason is not None:
        report["reason"] = reason
    elif report["timed_out"]:
        report["reason"] = (
            f"the gate did not finish within the {timeout_seconds:g}s re-run bound"
        )
    report["finding"] = _merged_gate_finding(
        report["base_verdict"],
        report["integrated_verdict"],
        integrated_revision=report["integrated_revision"],
        exit_status=report["exit_status"],
        reason=report["reason"],
    )
    return report


def record_gate_rerun_at_integrated_revision(
    *,
    project: str,
    run_id: str,
    repository: Path,
    integrated_revision: str = "HEAD",
    timeout_seconds: float = 300.0,
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
    the option or the stored row.
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
    report = rerun_gate_at_integrated_revision(
        repository=checkout,
        gate_check=stored_gate_check if isinstance(stored_gate_check, Mapping) else None,
        base_verdict=str(row.get("gate") or "passed"),
        integrated_revision=integrated_revision,
        timeout_seconds=timeout_seconds,
        command=command,
    )
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
        paths=[ledger.ledger_path(project, ledger_root)],
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
        "ledger_path": str(ledger.ledger_path(project, ledger_root)),
        "ledger_version": new_version,
        "checkout_on_integrated_revision": report.get("checkout_on_integrated_revision"),
        "checkout_revision": report.get("checkout_revision"),
        "report": report,
        "finding": report.get("finding"),
        "landing": landing,
        "store_synopsis": store_synopsis,
    }


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
    run_id: str, record: Mapping[str, Any]
) -> None:
    """Refuse completed repository work whose manifest omits its commit.

    The guard refuses only when the manifest names a changed path that resolves
    under the run's own repository, leaving a run whose changed_paths lie
    entirely outside it — a report-only or review run — to promote without a
    commit, which is its correct disposition. The chain answers the narrower
    question first: only once a path needs a commit does the absent ``commits``
    field become the defect.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return
    try:
        manifest = parse_manifest(
            Path(str(record["manifest_path"])).read_text(encoding="utf-8")
        )
    except (OSError, KeyError, ValueError):
        return
    if (
        str(manifest.get("status") or "").strip().lower() != "complete"
        or not manifest.get("changed_paths")
        or _prose_changed_paths_name_no_paths(manifest)
        or manifest.get("commits")
        or not _changed_paths_inside_repository(manifest, record)
    ):
        return
    raise CrewError(
        f"run {run_id!r} has a complete manifest with changed_paths, but the "
        "manifest field 'commits' is missing. Promotion cannot verify changed "
        "repository paths without the commit that contains them"
    )


def _resolve_commits(*, cwd: Path, revisions: Iterable[str], run_id: str) -> list[str]:
    """Resolve every recorded revision to its canonical commit object id."""
    commits = []
    for revision in revisions:
        resolved = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        commit = resolved.stdout.strip()
        if resolved.returncode or not commit:
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

    The worker's cited tip is preferred over the worktree's ``HEAD`` because a
    shared checkout can advance under other runs between the commit and the
    promotion, and the cited tip is the revision whose diff promotion already
    measured.
    """
    if commit_list:
        return str(commit_list[-1])
    head = _commit_canonical_id(run_tree, "HEAD")
    return head or ""


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
    """Return changed repository paths not contained by a declared write root."""
    repository = Path(str(record.get("repo") or tree))
    roots = _repository_scope_paths(
        declared_paths, worktree=_scope_worktree(record, tree), repository=repository
    )
    outside = []
    for changed in changed_paths:
        path = Path(changed)
        if not any(path == root or path.is_relative_to(root) for root in roots):
            outside.append(changed)
    return tuple(outside)


def _accepted_scope_exceptions(
    run_id: str,
    outside: Iterable[str],
    accepted_paths: Mapping[str, str] | None,
    *,
    record: Mapping[str, Any],
    tree: Path,
) -> list[dict[str, str]]:
    """Validate deliberate companion paths and return their durable account."""
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
                    raise CrewError(
                        f"run {run_id!r} cannot accept {path}: live run "
                        f"{peer_run!r} claims {claim.as_posix()}"
                    )

    return [{"path": path, "reason": normalized[path]} for path in sorted(normalized)]


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
    """Return the stray uncommitted edits found in another dispatch-visible tree."""
    snapshot = _run_directory_tree_snapshot(run_id)
    if snapshot is None:
        snapshot = record.get("repository_tree_snapshot")
    if not isinstance(snapshot, Mapping):
        return []
    before_trees = snapshot.get("trees")
    if not isinstance(before_trees, list):
        return []
    roots = [
        str(tree.get("path") or "")
        for tree in before_trees
        if isinstance(tree, Mapping) and str(tree.get("path") or "")
    ]
    repository = Path(str(record.get("repo") or ".")).resolve()
    current = _repository_tree_snapshot(repository, roots=roots)
    after_by_path = {
        str(tree.get("path") or ""): tree
        for tree in current["trees"]
        if isinstance(tree, Mapping)
    }
    own_tree = Path(str(record.get("worktree") or "")).resolve()
    declared = (record.get("node") or {}).get("write_paths") or ()
    declared_roots = _repository_scope_paths(
        declared, worktree=own_tree, repository=repository
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
        if path == own_tree:
            continue
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
        changed_paths = {
            changed
            for _, changed in _snapshot_entries(after) - _snapshot_entries(before)
            if any(
                Path(changed) == root or Path(changed).is_relative_to(root)
                for root in declared_roots
            )
        }
        label = "main checkout" if path == repository else "peer worktree"
        if changed_paths:
            violations.extend(
                f"{changed} in {label} {path}" for changed in sorted(changed_paths)
            )
    return violations


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
    worktree = Path(str(record.get("worktree") or ""))
    base = str(record.get("base_sha") or "")
    if not worktree.is_dir():
        raise CrewError(
            f"shadow run {run_id!r} has no readable worktree; its patch cannot be preserved"
        )
    resolved = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{base}^{{commit}}",
        ],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if not base or resolved.returncode:
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
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0, int((last - first).total_seconds()))


def _assume_utc_if_naive(value: str) -> str:
    """Attach UTC to a completion stamp that carries no timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        return value
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


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
    """Return the original stream followed by resumes in numeric turn order."""
    original = path.parent / "stream.jsonl" if path.name.startswith("resume-") else path
    resumes = sorted(
        path.parent.glob("resume-*.jsonl"), key=ledger._resume_stream_order
    )
    return [candidate for candidate in (original, *resumes) if candidate.is_file()]


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
    primary: Mapping[str, Any], tree: Path
) -> tuple[str, tuple[str, ...]]:
    """The landed commit and changed paths a shadow could have read.

    The commit is the primary row's own cited sha, resolved at its promotion,
    and the paths are the files that sha changed, read back from the shared
    object store so no manifest or fixture needs to have named them. A primary
    with no cited commit (a shadow of a shadow, which does not land code) has
    nothing a run could read as an answer.
    """
    commits = [str(sha) for sha in (primary.get("commits") or ()) if str(sha).strip()]
    commit = commits[-1] if commits else ""
    if not commit:
        return "", ()
    result = subprocess.run(
        [
            "git",
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            f"{commit}^{{commit}}",
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
    tree: Path,
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


def _section_anchor(section: Any) -> str:
    """Map a numbered section reference to its semantic HTML anchor."""
    normalized = ledger.normalize_section(section)
    numbered = re.fullmatch(r"§(\d+(?:\.\d+)*)", normalized)
    if numbered:
        return f"s{numbered.group(1).replace('.', '-')}"
    return normalized.removeprefix("#") or "_top"


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
) -> dict[str, Any]:
    """Append one idempotent section comment for a promoted run.

    When the run's own worker already wrote the landing record — under this
    same run-derived comment id, in its own committed plan — nothing is
    appended, so promotion leaves the plan file untouched and the merge that
    brings the worker's record in does not collide on the duplicate id.
    """
    narrative = str(narrative).strip()
    if not narrative or not plan:
        return {"recorded": False, "reason": "empty_narrative"}
    comment_id = f"c-run-{re.sub(r'[^A-Za-z0-9._-]+', '-', run_id)}"
    anchor = _section_anchor(section)
    desired_body = f"<p>{html.escape(narrative)}</p>"
    worker_recorded = bool(
        worker_tree
        and _worker_authored_landing_record(
            tree=worker_tree,
            commits=worker_commits,
            project=project,
            plan=plan,
            comment_id=comment_id,
        )
    )
    for _attempt in range(4):
        state, version = _store.read_plan(project, plan, root, artifact_type="plan")
        if not state or state.get("type") != "plan":
            return {"recorded": False, "reason": "plan_unavailable"}
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
            if str(existing.get("body") or "") != desired_body:
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
        try:
            _store.write_plan(
                project,
                plan,
                {**state, "comments": comments},
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
        }
    raise CrewError(
        f"could not record landing comment for plan {plan!r}: "
        "the plan changed during four consecutive write attempts"
    )


def _require_committable_checkout(checkout: Path | None, run_id: str) -> None:
    """Refuse before writing when the checkout cannot host the landing commit.

    Promotion writes two tracked stores (the ledger row and the plan landing
    comment) and commits them as one landing. A checkout that is not a git
    worktree cannot host that commit, so promotion refuses here, before either
    store is written, rather than writing stores it could not commit.
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


def _plan_comment_store_path(
    *,
    project: str,
    plan: str,
    comment: Mapping[str, Any],
    root: str | Path | None,
) -> list[Path]:
    """The tracked plan path changed by a newly recorded landing comment.

    The ledger path is never returned here: a caller that appended a row adds
    it explicitly, while the already-landed branch rewrote no ledger of its
    own. The plan file is returned only when the comment was newly recorded,
    since an idempotent retry leaves the plan file unchanged.
    """
    if not str(plan) or not comment.get("recorded") or comment.get("already_recorded"):
        return []
    plan_file = _store._resolve_html_file(
        project, str(plan), root, artifact_type="plan"
    )
    return [plan_file] if plan_file is not None else []


def _restore_landing_writes(
    checkout: Path, paths: Sequence[Path]
) -> dict[str, bool]:
    """Best-effort reversal of a refused landing's uncommitted store writes.

    Each path promotion wrote returns to its committed state: tracked paths
    are restored from HEAD; a path absent from HEAD (created by this
    promotion) is dropped from the index and the working tree. Recovery is
    best-effort because the refusal that triggers it (a stuck index or other
    git failure) can itself block these git calls.

    The result is a per-path report keyed by the path as written: True for a
    path that no longer carries the landing write — restored from HEAD, or
    dropped because HEAD never had it — and False for one whose write
    survives, because the restore was refused and HEAD holds a copy the
    caller preserves. A caller reporting a store it wrote reads this to tell
    a row that survived the rollback from one the rollback reverted.
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
        try:
            relative = Path(path).resolve().relative_to(checkout.resolve()).as_posix()
        except ValueError:
            report[target] = False
            continue
        present = _git(checkout, "cat-file", "-e", f"HEAD:{relative}", check=False)
        if present.returncode == 0:
            report[target] = False
            continue
        _git(checkout, "rm", "--cached", "--force", "--", target, check=False)
        Path(path).unlink(missing_ok=True)
        report[target] = True
    return report


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


def _commit_landing_writes(
    *,
    run_id: str,
    verdict: str,
    checkout: Path,
    paths: Sequence[Path],
    subject: str | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    """Commit promotion's own store writes in one landing commit.

    Stages exactly the given paths (never a whole-tree add) and commits them
    under a subject naming the promoted run and its gate verdict, so a landing
    leaves the checkout with no uncommitted change at the paths promotion
    wrote. A write that cannot be staged or committed attempts to restore
    those paths and refuses. A blocked restore preserves paths held by HEAD;
    callers that already appended a ledger row must report that append.

    ``subject`` and ``body`` override the promotion-flavoured defaults; a
    caller that records a non-promotion landing (a gate re-run at the
    integrated revision) passes its own subject naming what it did.
    """
    targets = sorted(
        {Path(p).expanduser().resolve() for p in paths if Path(p).is_file()}
    )
    if not targets:
        return {"committed": False, "reason": "no_write"}
    staged = _git(checkout, "add", "--", *(str(p) for p in targets), check=False)
    if staged.returncode != 0:
        rollback = _restore_landing_writes(checkout, targets)
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
        raise _landing_refusal(
            f"could not commit the landing writes for run {run_id!r} in "
            f"{checkout}: {committed.stderr.strip() or committed.stdout.strip()}",
            rollback,
        )
    return {"committed": True, "subject": subject, "paths": [str(p) for p in targets]}


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
        with candidate.open(encoding="utf-8", errors="replace") as handle:
            events, _malformed = _backends.parse_events(handle)
        for event in events:
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp.strip():
                continue
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                timestamps.append((parsed, timestamp))
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
        _signal_process_group(int(pid), record.get("pid_start_time"))
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

    Two sources, and the second is the load-bearing one: a pointer carrying no
    session id is not evidence of an unresumable run, because a resume that
    finds none on the record re-reads the run's stream for it. Reading only the
    pointer is what makes a recoverable run look dead.

    An unreadable stream answers nothing rather than raising here: this runs
    before the irreversible half of a promotion, and an instrument that fails
    must not become a refusal of its own.
    """
    pointer_session = str(record.get("session_id") or "").strip()
    if pointer_session:
        return {"session_id": pointer_session, "source": "pointer"}
    try:
        stream_session = str(_terminal_stream_data(record).session_id or "").strip()
    except (CrewError, OSError):
        return None
    if stream_session:
        return {"session_id": stream_session, "source": "stream"}
    return None


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
    if manifest is None or _prose_changed_paths_name_no_paths(manifest):
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


def _require_review_waiver(
    run_id: str,
    record: Mapping[str, Any],
    *,
    verdict: str,
    classification: str,
    review: Mapping[str, Any] | None,
    review_action: str,
    waiver_reason: str,
    promoted_head: str = "",
    stale_head: str = "",
) -> dict[str, str] | None:
    """Refuse an unreviewed promotion of a run that changed the repository.

    The gate follows the writing, not the role name: a passing run that
    changed a path inside its own repository has produced work a reviewer must
    read, whatever role carried it — a test node writing test files, a
    documentation node writing docs and an investigate node writing a report
    all leave the repository altered. The implement role stays gated whether or
    not its manifest names a path, so an implement run that declares no change
    is still refused rather than slipping through on its silence. The review
    role is exempt, because the review it wrote for another run is its own
    deliverable; requiring another review would recurse without a stopping point.

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
    from reckon.crew.recovery import REVIEW_ROLE, _pointer_role

    role = _pointer_role(record)
    reason = str(waiver_reason).strip()
    changed_repository = bool(_manifest_repository_paths(record))
    review_required = classification == "scoring" or (
        classification == "promotable" and bool(stale_head)
    )
    unreviewed = (
        verdict == "passed"
        and review_required
        and role != REVIEW_ROLE
        and (role == "implement" or changed_repository)
        and not (review and review.get("status") == "parsed")
    )
    if unreviewed:
        if reason:
            return {"reason": reason}
        raise CrewError(
            _unreviewed_refusal(run_id, review_action, promoted_head, stale_head)
        )
    if reason:
        raise CrewError(
            f"run {run_id!r} has no unreviewed promotion for "
            f"--waive-unreviewed-promotion {reason!r} to waive"
        )
    return None


def _unreviewed_refusal(
    run_id: str,
    review_action: str,
    promoted_head: str,
    stale_head: str,
) -> str:
    """State why an unreviewed promotion is refused, naming both revisions.

    A run promoted on the strength of a review of an earlier revision is the
    failure this gate exists for, and an operator who reads only "no review is
    stored" goes looking for a record that is already on disk. Naming the
    revision the promotion asserts beside the one the stored record read makes
    the repair obvious: the review must be recomposed against the new head.
    """
    revision = (
        f"the stored review read revision {stale_head[:12]} and this promotion "
        f"asserts {promoted_head[:12]}: no review of the promoted revision is "
        "stored"
        if stale_head and promoted_head
        else "no complete independent review is stored"
    )
    return (
        f"run {run_id!r} is classified scoring because {revision}. Produce it "
        f"with `{review_action}`, or promote anyway with "
        "--waive-unreviewed-promotion REASON stating why this run may land "
        "without review"
    )


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
) -> dict[str, Any]:
    """Promote a run, or finish cleanup when its record already landed."""
    verdict = str(gate).strip().lower()
    if verdict not in ledger.GATE_VERDICTS:
        raise ledger.LedgerError(
            f"gate verdict {gate!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; a gate whose evidence could "
            "not be produced is 'not-run'"
        )
    if verdict != "passed" and not str(outcome).strip():
        raise CrewError(
            "a non-passing gate requires --outcome; write what failed or why "
            "the evidence could not be produced"
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
        record = read_pointer(run_id)
        # A promotion writes the ledger row into the project's own mount, so
        # the repository is resolved from that mount before anything is
        # written: a checkout named from elsewhere is refused, and a run whose
        # record names none is given the mount rather than the caller's
        # enclosing repository. A project with no mount keeps the caller's
        # checkout, which is the only root available to it. Judged ahead of the
        # per-run evidence so a repository defect is reported as itself rather
        # than as a missing commit further down.
        landing_project = str(record.get("project") or "")
        if landing_project and project_mount_repository(landing_project) is not None:
            root = resolve_project_repository(
                landing_project, root, flag="--checkout-path"
            )
        _require_commit_for_changed_manifest(run_id, record)
        _require_recognised_manifest_status(run_id, record)
        if _is_shadow(record) and commit_list:
            raise CrewError(
                f"shadow run {run_id!r} is commitless evidence; --commit is refused"
            )
        commit_list_shortfall = _require_gate_evidence(
            run_id,
            record,
            verdict=verdict,
            commits=commit_list,
            no_commit_reason=no_commit,
        )
        _require_gate_log_agrees(run_id, gate_check, verdict=verdict)
        from reckon.crew.recovery import classify_pointer

        classified = classify_pointer(record)
        classification_name = str(classified.get("classification") or "")
        # The revision this promotion asserts, resolved before the gate reads
        # the store, so a review of an earlier revision is refused rather than
        # accepted as evidence about code the repair has already moved past.
        promoted_revision = _run_promoted_revision(record, commit_list)
        review_tree = Path(str(record.get("worktree") or ""))
        if not review_tree.is_dir():
            review_tree = Path(str(record.get("repo") or ""))
        reviewed, stale_review_head = _review_for_promotion(
            landing_project,
            run_id,
            promoted_revision=promoted_revision,
            tree=review_tree if review_tree.is_dir() else None,
        )
        review_waived = _require_review_waiver(
            run_id,
            record,
            verdict=verdict,
            classification=classification_name,
            review=reviewed,
            review_action=str(classified.get("next_action") or ""),
            waiver_reason=review_waiver,
            promoted_head=promoted_revision,
            stale_head=stale_review_head,
        )
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
        resume_waived = _require_resume_waiver(
            run_id,
            verdict=verdict,
            waiver_reason=resume_waiver,
            classification=classification_name,
            recoverable_session=recoverable_session,
        )
        if discard_resume_worktree and resume_waived is None:
            raise CrewError(
                "discard_resume_worktree requires a reasoned resume waiver for "
                "a recoverable non-passing run"
            )
        suite_delta = _evaluate_suite_delta(
            run_id,
            record,
            waiver_reason=suite_delta_waiver,
        )
        result = _complete_locked(
            run_id,
            gate=gate,
            failure_classification=classification,
            commits=commit_list,
            no_commit=no_commit,
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
            resume_remedy=resume_remedy,
            resume_waived=resume_waived,
            reviewed=reviewed,
            review_waived=review_waived,
            negative_control_waiver=negative_control_waiver,
            recoverable_session=recoverable_session,
            discard_resume_worktree=discard_resume_worktree,
            accepted_paths=accepted_paths,
            commit_list_shortfall=commit_list_shortfall,
            no_impl_change=no_impl_change,
        )
        if commit_list_shortfall is not None:
            result["commit_list_shortfall"] = dict(commit_list_shortfall)
        return result


def _evaluate_suite_delta(
    run_id: str,
    record: Mapping[str, Any],
    *,
    waiver_reason: str,
) -> dict[str, Any] | None:
    """Validate an armed run's paired suite evidence and calculate its delta."""
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
    base_sha = str(record.get("base_sha") or "").strip()
    if (
        isinstance(baseline, Mapping)
        and str(baseline.get("revision") or "").strip()
        and str(baseline["revision"]).strip() != base_sha
    ):
        missing.append("baseline_suite.revision_matches_base_sha")
    if missing:
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
) -> dict[str, str] | None:
    """Describe a worktree deliberately kept as a session's working directory."""
    worktree = str(record.get("worktree") or "").strip()
    if recoverable_session is None or discard or not worktree:
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


def _release_run_workspace(
    record: Mapping[str, Any],
    retention: Mapping[str, str] | None = None,
    *,
    process_already_ended: bool = False,
) -> dict[str, Any]:
    """Release a promoted run's own worktree and, if still alive, its process.

    Reuses the classification `crew gc` already applies rather than writing a
    second policy: a worktree is released only when it is clean and its HEAD
    is an ancestor of the repository's integration branch, or when it is a
    shadow whose patch was already retained. Everything else is left in place
    and named with the condition that withheld it. Called only after the
    ledger append and pointer delete already succeeded; any exception raised
    here is caught by the caller and folded into the result instead of being
    allowed to obscure those two writes.
    """
    result: dict[str, Any] = {"worktree_released": False, "process_signalled": False}

    worktree_value = str(record.get("worktree") or "")
    repo_value = str(record.get("repo") or "")
    worktree = Path(worktree_value) if worktree_value else None
    repo = Path(repo_value) if repo_value else None
    if retention is not None:
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
        claims = _live_worktree_claims().get(worktree.resolve(), [])
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
    if not _release_terminal_manifest(record):
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
            _signal_process_group(int(pid), record.get("pid_start_time"))
        except (ProcessLookupError, PermissionError, OSError, CrewError) as exc:
            result["process_withheld"] = f"could not signal pid {pid} — {exc}"
        else:
            result["process_signalled"] = True

    result["worktree_audit"] = _worktree_audit(record, retention)
    return result


def _release_after_promotion(
    run_id: str,
    record: Mapping[str, Any],
    retention: Mapping[str, str] | None = None,
    *,
    process_already_ended: bool = False,
) -> dict[str, Any]:
    """Release what promotion made transient, never at the cost of the ledger.

    A failure here — a git command that raises, a permission error signalling
    a process — must never read as a failed promotion: the ledger row and the
    pointer deletion that precede this call have already succeeded, and this
    step is strictly additional cleanup on top of them. process_already_ended
    records a writer the promotion ended before the fold, so the release can
    report that outcome instead of a process that is merely absent.
    """
    try:
        return _release_run_workspace(
            record, retention, process_already_ended=process_already_ended
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask promotion
        return {
            "worktree_released": False,
            "process_signalled": False,
            "worktree_withheld": f"run {run_id!r} release step raised: {exc}",
        }


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


def _fleet_state_reading(project: str) -> dict[str, Any]:
    """Return a bounded current reading of the project's fleet state.

    The live-pointer and drain projections own their respective derivations;
    promotion only composes their already-derived facts into the result that an
    orchestrator is about to read. The reading deliberately stays outside the
    ledger because it describes the fleet at this moment, not this run.
    """
    observed_at = _utc_now()
    try:
        from reckon.crew import recovery

        pointers = list_live(project=project)
        closure = drain(project)
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
        return {
            "fleet_state": "measured",
            "observed_at": observed_at,
            "live_runs": len(pointers),
            "unreconciled_runs": int(closure["unreconciled_runs"]),
            "actionable_runs": len(actionable),
            "actionable_classifications": sorted(set(actionable)),
            "occupied_lanes": len(lanes),
        }
    except Exception:  # noqa: BLE001 - an unavailable reading never blocks landing
        return {
            "fleet_state": "unmeasured",
            "observed_at": observed_at,
            "unmeasured": {"fleet_state": "unavailable"},
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


def _run_promoted_revision(
    record: Mapping[str, Any], commit_list: Sequence[str]
) -> str:
    """Resolve the revision a promotion of this run asserts, from its own tree.

    The same reading the promoted row records, taken before the review gate
    reads the store so the gate compares against the revision this promotion
    will name rather than against whatever the store holds newest. The cited tip
    is canonicalised as the row canonicalises it, so a citation that names the
    revision symbolically or in abbreviation still matches the full sha a review
    recorded reading.
    """
    worktree = Path(str(record.get("worktree") or ""))
    tree = worktree if worktree.is_dir() else Path(str(record.get("repo") or "."))
    if commit_list:
        tip = str(commit_list[-1])
        return _promoted_revision(tree, [_commit_canonical_id(tree, tip) or tip])
    return _promoted_revision(tree, [])


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


_LANDING_COMMENT_PREFIX = "c-run-"


def _landed_sections(state: Mapping[str, Any]) -> set[str]:
    """Return the sections a landing comment already records.

    A promoted run appends one section comment under a run-derived id, so the
    presence of that id is the plan's own record that work landed against the
    section. A comment written for any other reason carries another id and
    says nothing about a landing.
    """
    comments = state.get("comments")
    if not isinstance(comments, Mapping):
        return set()
    landed: set[str] = set()
    for raw_section, entries in comments.items():
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and str(entry.get("id") or "").startswith(
                _LANDING_COMMENT_PREFIX
            ):
                landed.add(str(raw_section).strip())
                break
    return landed


def _plan_remaining_sections(state: Mapping[str, Any]) -> list[str]:
    """Return the plan's sections that still have work to land.

    A landing already recorded on a section is subtracted, so the refusal
    names work a reader can still pick up rather than a section that has been
    delivered. Beyond that, a declared ``implementable`` section is
    outstanding until it is reclassified, and a plan that has not persisted a
    classification falls back to the section identities its gates and comment
    anchors name.
    """
    landed = _landed_sections(state)
    declarations = state.get("section_declarations")
    if isinstance(declarations, Mapping):
        return sorted(
            section
            for section, classification in declarations.items()
            if str(classification).strip() == "implementable"
            and str(section).strip() not in landed
        )
    from reckon._schema import plan_section_anchors

    return sorted(plan_section_anchors(state) - landed)


_IMPL_MOVE_ENFORCED_ROLES = frozenset({"implement", "test"})
_IMPL_MOVE_EXEMPT_CLASSIFICATIONS = frozenset({"negative-result", "correct-refusal"})


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
    if role not in _IMPL_MOVE_ENFORCED_ROLES:
        check["verdict"] = "exempt"
        check["reason"] = f"role-not-enforced:{role or 'unknown'}"
        return check
    if str(failure_classification).strip().lower() in _IMPL_MOVE_EXEMPT_CLASSIFICATIONS:
        check["verdict"] = "exempt"
        check["reason"] = f"failure-classification:{failure_classification}"
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


def _require_declared_negative_control(
    run_id: str,
    record: Mapping[str, Any],
    *,
    gate: str,
    manifest: Mapping[str, Any] | None,
    manifest_path: str,
    waiver_reason: str = "",
) -> dict[str, Any]:
    """Refuse a passing gate on a check whose red log is not delivered.

    A node whose write paths include a test file declares the mutation that
    check must fail against. The declaration is discharged at promotion by a
    manifest that carries the path to the log that mutation produced: the pair
    of logs is the positive and negative control of one measurement, and the
    red log must name the declared mutation rather than merely be a run that
    failed for something else. A declaration of ``none`` with its reason is an
    explicit escape rather than a silent one, so it is recorded on the row
    rather than refused.
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
    if declaration not in text:
        if waiver_reason:
            check["verdict"] = "waived"
            check["reason"] = waiver_reason
            return check
        raise CrewError(
            f"run {run_id!r} declares the mutation {declaration!r} but the log at "
            f"{resolved!r} does not name it, so the log is a failure for some other "
            "reason and not the negative control of this check. Record the log the "
            "declared mutation produced with its first line repeating that mutation "
            "verbatim, correct the declaration to the mutation the log shows, "
            "or use --waive-negative-control REASON to record why the mismatch "
            "may be accepted"
        )
    check["verdict"] = "matched"
    return check


def _complete_locked(
    run_id: str,
    *,
    gate: str,
    failure_classification: str = "",
    commits: Iterable[str] = (),
    no_commit: str = "",
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
    negative_control_waiver: str | None = None,
    recoverable_session: Mapping[str, str] | None = None,
    discard_resume_worktree: bool = False,
    accepted_paths: Mapping[str, str] | None = None,
    commit_list_shortfall: Mapping[str, Any] | None = None,
    no_impl_change: str = "",
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
    """
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
    worktree = Path(str(record.get("worktree") or ""))
    tree = worktree if worktree.is_dir() else Path(str(record.get("repo") or "."))
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
        with _report_written_ledger_row(
            run_id,
            ledger_path=ledger.ledger_path(project, ledger_root),
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
            )
            result = {
                "run_id": run_id,
                "project": project,
                "ledger_path": str(ledger.ledger_path(project, ledger_root)),
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
    )
    commit_list = [str(sha) for sha in commits if str(sha).strip()]
    if shadow and commit_list:
        raise CrewError(
            f"shadow run {run_id!r} is commitless evidence; --commit is refused"
        )
    if commit_list:
        commit_list = _resolve_commits(cwd=tree, revisions=commit_list, run_id=run_id)
    shadow_patch = ""
    scope_acceptances: list[dict[str, str]] = []
    if shadow:
        artifact = _write_shadow_patch(record)
        changed_lines = _shadow_patch_stat(artifact, cwd=tree)
        shadow_patch = str(artifact)
    elif commit_list:
        cumulative = _cumulative_diff(
            cwd=tree,
            base=f"{commit_list[0]}^",
            head=commit_list[-1],
        )
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
                # A merge's first-parent diff carries everything its other
                # parent brought — the orchestrator's own plan edits included —
                # so citing the merge attributes those to the worker. Same
                # check, but the caller needs to know which of the two it is:
                # a worker that exceeded its fence, or an orchestrator that
                # named the wrong commit.
                merges = _merge_revisions(tree, commit_list)
                if merges:
                    raise CrewError(
                        f"run {run_id!r} cites merge commit "
                        f"{', '.join(merges)}, whose diff includes everything "
                        "its other parent brought — so these paths are outside "
                        f"the node's write scope: {', '.join(outside)}. This is "
                        "the wrong commit rather than a worker that exceeded "
                        "its scope: cite the worker's own commit, which "
                        "`reckon crew recover` reports as the run's next action"
                    )
                scope_acceptances = _accepted_scope_exceptions(
                    run_id,
                    outside,
                    accepted_paths,
                    record=record,
                    tree=tree,
                )
        changed_lines = cumulative.changed_lines
    else:
        changed_lines = None
    boundary_waived = _require_repository_tree_boundary(
        run_id, record, waiver_reason=boundary_waiver
    )
    # A passing implement or test run is refused when the plan it landed against
    # did not move, unless the reason is recorded. The check reads the plan from
    # its own repository, ahead of anything this promotion writes.
    plan_state = _plan_state_for_run(record, fallback_root=ledger_root)
    impl_move = _require_impl_moved(
        run_id,
        record,
        gate=gate,
        failure_classification=failure_classification,
        no_impl_change=no_impl_change,
        plan_state=plan_state,
    )

    session_id = record.get("session_id") or stream.session_id
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
    manifest_text: str | None = None
    manifest: Mapping[str, Any] | None = None
    manifest_path = str(record.get("manifest_path") or "")
    if manifest_path:
        try:
            manifest_text = Path(manifest_path).read_text(encoding="utf-8")
        except OSError:
            manifest_text = None
    if manifest_text is not None:
        try:
            manifest = parse_manifest(manifest_text)
        except (KeyError, ValueError, OSError):
            manifest = None
    # A passing gate on a node that writes a check is refused unless the
    # manifest names the red log the declared mutation produced. The check runs
    # after the manifest is read, because the discharge lives there.
    waiver_reason = (
        "" if negative_control_waiver is None else str(negative_control_waiver).strip()
    )
    if negative_control_waiver is not None and not waiver_reason:
        raise CrewError("--waive-negative-control requires a non-empty reason")
    negative_control = _require_declared_negative_control(
        run_id,
        record,
        gate=gate,
        manifest=manifest,
        manifest_path=manifest_path,
        waiver_reason=waiver_reason,
    )
    if negative_control_waiver is not None and negative_control["verdict"] != "waived":
        raise CrewError(
            f"run {run_id!r} has no negative-control match refusal for "
            f"--waive-negative-control {waiver_reason!r} to waive"
        )
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
    # five dimension scores and their total survive the loss of the crew
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
    run = ledger.build_record(
        run_id=run_id,
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
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
        manifest_path=str(record.get("manifest_path") or ""),
        scope_changed=scope_changed,
        session_id=session_id,
        budget=measured_budget,
        lane_receipt=lane_receipt,
        throughput=stream.throughput,
        budget_fallback=record.get("budget_fallback"),
        lineage=record.get("lineage"),
        shadow_patch=shadow_patch,
        unreconciled_override=record.get("unreconciled_override"),
        gate_check=gate_check,
        require_gate_check=require_gate_check,
        suite_delta=suite_delta,
        resume_remedy=resume_remedy,
        follow_on_paths=follow_on_paths,
        predecessor_run=predecessor,
        dispute_count=dispute_count,
        review=reviewed,
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
    if worktree_retention is not None:
        run["worktree_retention"] = dict(worktree_retention)
    watch_override = record.get("watch_override")
    if isinstance(watch_override, Mapping):
        run["watch_override"] = dict(watch_override)
    execution_fit = record.get("execution_fit")
    if isinstance(execution_fit, Mapping):
        run["execution_fit"] = dict(execution_fit)
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
        written = {
            "path": str(ledger.ledger_path(project, ledger_root)),
            "version": ledger_version,
            "run": dict(existing),
        }
    with _report_written_ledger_row(
        run_id,
        ledger_path=ledger.ledger_path(project, ledger_root),
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

        # The two tracked stores this promotion wrote (the ledger row and, when a
        # narrative landed, the plan comment) are committed as one landing, so the
        # checkout carries no uncommitted state the next reader would trip on.
        _commit_landing_writes(
            run_id=run_id,
            verdict=str(gate).strip().lower(),
            checkout=checkout,
            paths=[
                ledger.ledger_path(project, ledger_root),
                *_plan_comment_store_path(
                    project=project,
                    plan=str(node.get("plan") or ""),
                    comment=comment,
                    root=ledger_root,
                ),
            ],
        )

        # The session id lives only in the pointer until it reaches the roster, so
        # it has to be captured before the pointer goes.
        capture = _capture_member_session(record)
        pointer_path(run_id).unlink(missing_ok=True)
        release = _release_after_promotion(
            run_id,
            record,
            worktree_retention,
            process_already_ended=ended_writer,
        )
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


def discard(run_id: str) -> dict[str, Any]:
    """Remove a stopped or abandoned pointer without promoting it."""
    with _pointer_lock(run_id):
        record = read_pointer(run_id)
        pid = record.get("pid")
        if record_process_alive(record, process_alive) is True:
            raise CrewError(
                f"cannot discard live run {run_id!r}: recorded pid {pid} is alive"
            )
        path = pointer_path(run_id)
        path.unlink()
        return {
            "run_id": run_id,
            "pointer_path": str(path),
            "pointer_removed": not path.exists(),
            "removed": record,
        }


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
