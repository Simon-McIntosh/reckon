from __future__ import annotations

import html
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from reckon import _backends, _store, ledger
from reckon.crew.dispatch import _backend_settings, _capture_member_session
from reckon.crew.node import CrewError, STALL_BUDGET_MULTIPLE, parse_duration
from reckon.crew.reports import parse_manifest
from reckon.crew.routing import (
    RECLAIMABLE_CLASSES,
    WITHHELD_REASONS,
    _git,
    _inspect_workspace,
    _repository_tree_snapshot,
    _signal_process_group,
)
from reckon.crew.runs import (
    _live_worktree_claims,
    _manifest_freshness,
    _pointer_lock,
    _utc_now,
    _write_json,
    pointer_path,
    process_alive,
    read_pointer,
    run_dir,
)

# ── Promotion: the transient record becomes committed evidence ──────────────


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


def _commit_resolves_in(root: Path, revision: str) -> bool:
    """Report whether one revision names a commit object in one repository."""
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
    return not probe.returncode and bool(probe.stdout.strip())


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
) -> None:
    """Refuse a passing gate that leaves the run's own commits uncited.

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
    """
    if verdict != "passed" or commits or str(no_commit_reason).strip():
        return

    # First ask the worker, because it already answered. The manifest's
    # `commits:` line is delivered evidence that Reckon holds and, until now,
    # discarded: a coordinator that omitted one flag produced a ledger saying
    # the node succeeded with nothing pointing at the work. Naming the exact
    # revisions is more use than describing the condition, so this runs before
    # the repository check below.
    tree = Path(str(record.get("worktree") or ""))
    manifest_present, fresh = _manifest_freshness(record)
    if manifest_present and fresh and tree.is_dir():
        try:
            delivered = parse_manifest(
                Path(str(record["manifest_path"])).read_text(encoding="utf-8")
            )
        except (OSError, KeyError, ValueError):
            delivered = {}
        # Only an entry that resolves to a real commit means Reckon is holding
        # something. The line is free text a worker wrote: a report-only node
        # writes `commits: none (repository worktree remained clean)`, which is
        # neither a revision nor an omission, and matching a literal "none"
        # would refuse it. Resolving instead of pattern-matching cannot make
        # that mistake.
        stated = [
            candidate
            for candidate in (
                str(sha).strip() for sha in (delivered.get("commits") or [])
            )
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
        return
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    tip = head.stdout.strip()
    if head.returncode or not tip or tip == base:
        return
    raise CrewError(
        f"run {run_id!r} has a passing gate and cites no commit, but its "
        f"worktree is at {tip[:12]} rather than its base {base[:12]}: it "
        "committed, and nothing on the record says what. Work left only in a "
        "worktree is discarded the moment someone believes the ledger. Cite the "
        "commit, or pass --no-commit '<why>' to record deliberately that this "
        "node's commits are not being registered"
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
        declared_paths, worktree=tree, repository=repository
    )
    outside = []
    for changed in changed_paths:
        path = Path(changed)
        if not any(path == root or path.is_relative_to(root) for root in roots):
            outside.append(changed)
    return tuple(outside)


def _snapshot_entries(tree: Mapping[str, Any]) -> set[tuple[str, str]]:
    entries = tree.get("status_entries") or ()
    return {
        (str(entry.get("code") or ""), str(entry.get("path") or ""))
        for entry in entries
        if isinstance(entry, Mapping) and str(entry.get("path") or "")
    }


def _repository_tree_boundary_violations(
    run_id: str, record: Mapping[str, Any]
) -> list[str]:
    """Return the stray uncommitted edits found in another dispatch-visible tree."""
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


@dataclass(frozen=True)
class StreamMeasures:
    """Measurements recoverable from a run's ordered event streams."""

    completed_at: str | None
    completion_source: str | None
    worker_seconds: int | None
    budget: dict[str, Any]
    session_id: str | None


def _section_anchor(section: Any) -> str:
    """Map a numbered section reference to its semantic HTML anchor."""
    normalized = ledger.normalize_section(section)
    numbered = re.fullmatch(r"§(\d+(?:\.\d+)*)", normalized)
    if numbered:
        return f"s{numbered.group(1).replace('.', '-')}"
    return normalized.removeprefix("#") or "_top"


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
) -> dict[str, Any]:
    """Append one idempotent section comment for a promoted run."""
    narrative = str(narrative).strip()
    if not narrative or not plan:
        return {"recorded": False, "reason": "empty_narrative"}
    comment_id = f"c-run-{re.sub(r'[^A-Za-z0-9._-]+', '-', run_id)}"
    anchor = _section_anchor(section)
    for _attempt in range(4):
        state, version = _store.read_plan(project, plan, root, artifact_type="plan")
        if not state or state.get("type") != "plan":
            return {"recorded": False, "reason": "plan_unavailable"}
        comments = {
            key: list(items) for key, items in (state.get("comments") or {}).items()
        }
        items = comments.setdefault(anchor, [])
        if any(str(item.get("id") or "") == comment_id for item in items):
            return {
                "recorded": True,
                "comment_id": comment_id,
                "section": anchor,
                "already_recorded": True,
            }
        items.append(
            {
                "id": comment_id,
                "who": author,
                "when": when,
                "body": f"<p>{html.escape(narrative)}</p>",
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
    for candidate in paths:
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=candidate,
        )
        if observation.terminal:
            budget = dict(observation.budget)
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
        )

    newest = max(candidate.stat().st_mtime for candidate in paths)
    completed = (
        datetime.fromtimestamp(newest, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return StreamMeasures(completed, "stream_mtime", None, budget, session_id)


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
        if _is_shadow(record) and commit_list:
            raise CrewError(
                f"shadow run {run_id!r} is commitless evidence; --commit is refused"
            )
        _require_gate_evidence(
            run_id,
            record,
            verdict=verdict,
            commits=commit_list,
            no_commit_reason=no_commit,
        )
        suite_delta = _evaluate_suite_delta(
            run_id,
            record,
            waiver_reason=suite_delta_waiver,
        )
        return _complete_locked(
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
        )


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
    added = sorted(
        set(normalized_after["failure_ids"]) - set(normalized_baseline["failure_ids"])
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


def _release_run_workspace(record: Mapping[str, Any]) -> dict[str, Any]:
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
    if worktree is None:
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
    elif process_alive(pid) is not True:
        result["process_withheld"] = "process is not alive"
    else:
        try:
            _signal_process_group(int(pid), record.get("pid_start_time"))
        except (ProcessLookupError, PermissionError, OSError, CrewError) as exc:
            result["process_withheld"] = f"could not signal pid {pid} — {exc}"
        else:
            result["process_signalled"] = True

    return result


def _release_after_promotion(run_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Release what promotion made transient, never at the cost of the ledger.

    A failure here — a git command that raises, a permission error signalling
    a process — must never read as a failed promotion: the ledger row and the
    pointer deletion that precede this call have already succeeded, and this
    step is strictly additional cleanup on top of them.
    """
    try:
        return _release_run_workspace(record)
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask promotion
        return {
            "worktree_released": False,
            "process_signalled": False,
            "worktree_withheld": f"run {run_id!r} release step raised: {exc}",
        }


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
) -> dict[str, Any]:
    """Promote a finished run into the owning repository's committed ledger.

    The plan comment and ledger append both happen before the pointer is
    deleted. The comment uses a stable run-derived id, so a retry after an
    interruption cannot duplicate the narrative.

    Worker-time spans the first and last timestamped stream events. A healthy
    timestamp-less stream falls back to wall duration with an explicit source;
    a stalled run keeps that duration absent. Promotion time remains an
    explicit completion fallback when no stream survives.
    """
    record = read_pointer(run_id)
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    shadow = _is_shadow(record)
    ledger_root = root if root is not None else record.get("repo")
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
        comment = (
            {"recorded": False, "reason": "shadow evidence does not land code"}
            if shadow
            else _record_landing_comment(
                project=project,
                plan=str(node.get("plan") or ""),
                section=str(node.get("section") or ""),
                run_id=run_id,
                narrative=outcome,
                author=str(record.get("member") or record.get("role") or "reckon"),
                when=str(existing.get("completed_at") or _utc_now()),
                root=ledger_root,
            )
        )
        capture = _capture_member_session(record)
        path = pointer_path(run_id)
        path.unlink(missing_ok=True)
        release = _release_after_promotion(run_id, record)
        return {
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

    stream = _terminal_stream_data(record)
    if completed_at:
        finished = _assume_utc_if_naive(completed_at)
        completion_source = "provided"
    elif stream.completed_at:
        finished = stream.completed_at
        completion_source = stream.completion_source or "terminal_event"
    else:
        finished = _utc_now()
        completion_source = "promotion_time"
    commit_list = [str(sha) for sha in commits if str(sha).strip()]
    if shadow and commit_list:
        raise CrewError(
            f"shadow run {run_id!r} is commitless evidence; --commit is refused"
        )
    worktree = Path(str(record.get("worktree") or ""))
    tree = worktree if worktree.is_dir() else Path(str(record.get("repo") or "."))
    if commit_list:
        commit_list = _resolve_commits(cwd=tree, revisions=commit_list, run_id=run_id)
    shadow_patch = ""
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
                raise CrewError(
                    f"run {run_id!r} changed paths outside its declared "
                    f"write scope: {', '.join(outside)}"
                )
        changed_lines = cumulative.changed_lines
    else:
        changed_lines = None
    boundary_waived = _require_repository_tree_boundary(
        run_id, record, waiver_reason=boundary_waiver
    )

    session_id = record.get("session_id") or stream.session_id
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

    comment = (
        {"recorded": False, "reason": "shadow evidence does not land code"}
        if shadow
        else _record_landing_comment(
            project=project,
            plan=str(node.get("plan") or ""),
            section=str(node.get("section") or ""),
            run_id=run_id,
            narrative=outcome,
            author=str(record.get("member") or record.get("role") or "reckon"),
            when=finished,
            root=ledger_root,
        )
    )
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
        outcome="" if comment.get("recorded") else outcome,
        manifest_path=str(record.get("manifest_path") or ""),
        scope_changed=scope_changed,
        session_id=session_id,
        budget=measured_budget,
        lineage=record.get("lineage"),
        shadow_patch=shadow_patch,
        unreconciled_override=record.get("unreconciled_override"),
        gate_check=gate_check,
        require_gate_check=require_gate_check,
        suite_delta=suite_delta,
    )
    run["attempt"] = int(record.get("attempt") or 1)
    run["attempt_kind"] = str(record.get("attempt_kind") or "dispatch")
    # A deliberate commitless promotion survives on the record with its reason,
    # so a later reader can tell it from one that recorded nothing by accident.
    if str(no_commit).strip():
        run["no_commit"] = str(no_commit).strip()
    if boundary_waived is not None:
        run["boundary_waiver"] = boundary_waived
    watch_override = record.get("watch_override")
    if isinstance(watch_override, Mapping):
        run["watch_override"] = dict(watch_override)
    execution_fit = record.get("execution_fit")
    if isinstance(execution_fit, Mapping):
        run["execution_fit"] = dict(execution_fit)
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

    # The session id lives only in the pointer until it reaches the roster, so
    # it has to be captured before the pointer goes.
    capture = _capture_member_session(record)
    pointer_path(run_id).unlink(missing_ok=True)
    release = _release_after_promotion(run_id, record)
    return {
        "run_id": run_id,
        "project": project,
        "ledger_path": written["path"],
        "ledger_version": written["version"],
        "pointer_removed": not pointer_path(run_id).exists(),
        "record": written["run"],
        "already_promoted": already_promoted,
        "session_capture": capture,
        "plan_comment": comment,
        "release": release,
    }


def discard(run_id: str) -> dict[str, Any]:
    """Remove a stopped or abandoned pointer without promoting it."""
    with _pointer_lock(run_id):
        record = read_pointer(run_id)
        pid = record.get("pid")
        if process_alive(pid) is True:
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
