from __future__ import annotations

import hashlib
import json
import locale
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from reckon import (
    _backends,
    _plan_html,
    agent_context,
    capabilities,
    flight,
    ledger,
    velocity,
)
from reckon._plan_html import plan_headings, section_id_candidates, section_record_id
from reckon._timestamps import parse_utc
from reckon.calibration import calibration_configuration_key
from reckon.capability import (
    CAPABILITY_CLASSES,
    REQUIREMENT_LEVELS,
    _effective_request,
    _rank,
)
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_MEMBER_IDLE_WINDOW,
    CrewError,
    PlanReviewMissingError,
    PlanVisibilityError,
    TaskNode,
    parse_duration,
)
from reckon.crew.refusals import format_refusal
from reckon.crew.runs import (
    _pointer_lock,
    _process_start_time,
    delivery_roots,
    list_live,
    pointer_path,
    read_pointer,
    record_process_alive,
    runs_dir,
)
from reckon.crew.runs import (
    run_dir as _run_dir,
)

if TYPE_CHECKING:
    from reckon.crew.dispatch import DispatchPlan

# ── Routing ─────────────────────────────────────────────────────────────────

_CONTEXT_BYTES_PER_TOKEN = 3.5
_MEASURED_LAUNCH_CONTEXT_FLOOR_TOKENS = 50_392
_NAMED_REPOSITORY_FILE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:/?(?:[A-Za-z0-9_.-]+/)+"
    r"[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]*|"
    r"[A-Za-z0-9_-]+\.[A-Za-z][A-Za-z0-9]*)"
    r"(?=[^A-Za-z0-9_./-]|$)"
)
# A brief keeps a worker off a large file by naming it in an exclusion
# ("Exclude ``pkg/large.py`` from the declared inputs"), and the same brief may
# declare one path and exclude another in one clause ("Declare ``pkg/a.py`` as
# an input and avoid ``pkg/b.py``"). The two verbs govern different paths, so
# the exclusion is read per path: each verb governs the paths that follow it up
# to the next verb, and a path with no verb before it is charged, because the
# clause reached this point by declaring an input.
_GOVERNING_VERB = re.compile(
    r"\b(?:(?P<exclude>exclud\w*|omit\w*|avoid\w*|ignor\w*|unread"
    r"|(?:do not|don't|never|not)\s+(?:be\s+)?read)"
    r"|(?P<charge>declar\w*|treat\w*|read\w*|charg\w*|count\w*|includ\w*))\b",
    re.IGNORECASE,
)


def _role_overlay(
    config: Mapping[str, Any], role: str, spec_level: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Return a role's overlay and the level overlay selected by spec_level."""
    roles = config.get("roles") or {}
    overlay = roles.get(role)
    if overlay is None:
        known = ", ".join(sorted(roles)) or "none"
        raise CrewError(f"role {role!r} is not configured (configured roles: {known})")
    if not isinstance(overlay, Mapping):
        overlay = {}
    routing_by_level = overlay.get("by_spec_level") or {}
    level_overlay = (
        routing_by_level.get(spec_level, {})
        if spec_level and isinstance(routing_by_level, Mapping)
        else {}
    )
    if not isinstance(level_overlay, Mapping):
        level_overlay = {}
    return overlay, level_overlay


def _capability_overlay(
    overlay: Mapping[str, Any], capability_class: str
) -> Mapping[str, Any]:
    """Return the role overlay selected by a node's effective capability class.

    A class the role declares nothing for applies no overlay, so the raise is
    additive: it can only move a node whose class has routing declared for it.
    """
    if not capability_class:
        return {}
    by_class = overlay.get("by_capability_class") or {}
    if not isinstance(by_class, Mapping):
        return {}
    selected = by_class.get(capability_class) or {}
    return selected if isinstance(selected, Mapping) else {}


def _effective_backend(
    config: Mapping[str, Any],
    backend_name: str,
    overlay: Mapping[str, Any],
    level_overlay: Mapping[str, Any],
    class_overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a role's overlay onto one named backend's own settings."""
    backends = config.get("backends") or {}
    backend = backends.get(backend_name)
    if not isinstance(backend, Mapping):
        known = ", ".join(sorted(backends)) or "none"
        raise CrewError(
            f"backend {backend_name!r} is not defined (defined backends: {known})"
        )
    effective = dict(backend)
    for key, value in overlay.items():
        if key in ("name", "backend", "by_spec_level", "by_capability_class"):
            continue
        effective[key] = value
    for key, value in level_overlay.items():
        if key == "backend":
            continue
        effective[key] = value
    for key, value in (class_overlay or {}).items():
        if key == "backend":
            continue
        effective[key] = value
    return effective


def resolve_role(
    config: Mapping[str, Any],
    role: str,
    spec_level: str = "",
    *,
    capability_class: str = "",
) -> tuple[str, dict[str, Any]]:
    """Resolve a role to its backend name and the effective backend settings.

    A role overlays only the keys it names; everything else falls through to the
    backend it dispatches to. That is what lets a review role drop to a
    read-only tier without restating a backend. A named capability class
    applies its own overlay last, so the class a node resolves at outranks the
    specification level that would otherwise have selected its lane.
    """
    overlay, level_overlay = _role_overlay(config, role, spec_level)
    class_overlay = _capability_overlay(overlay, capability_class)
    backend_name = (
        class_overlay.get("backend")
        or level_overlay.get("backend")
        or overlay.get("backend")
        or config.get("default_backend")
    )
    if not backend_name:
        raise CrewError(
            f"role {role!r} selects no backend and no default_backend is set"
        )
    try:
        effective = _effective_backend(
            config, str(backend_name), overlay, level_overlay, class_overlay
        )
    except CrewError as exc:
        raise CrewError(
            f"role {role!r} routes to backend {backend_name!r}, which no layer defines"
        ) from exc
    return str(backend_name), effective


def resolve_role_override(
    config: Mapping[str, Any],
    role: str,
    spec_level: str,
    backend_name: str,
    *,
    capability_class: str = "",
) -> tuple[str, dict[str, Any]]:
    """Resolve a role's settings against an explicitly named backend.

    Used to re-resolve a role onto a budget fallback: the role's own overlay
    (an effort or sandbox override, say) still applies, only the concrete
    backend it lands on changes. The named backend wins over any capability
    class overlay, because the caller has already chosen a lane.
    """
    overlay, level_overlay = _role_overlay(config, role, spec_level)
    class_overlay = _capability_overlay(overlay, capability_class)
    effective = _effective_backend(
        config, backend_name, overlay, level_overlay, class_overlay
    )
    return str(backend_name), effective


def _section_record(
    plan_path: str | Path, section: str
) -> tuple[Mapping[str, Any], str]:
    """Return one typed record and its owning project."""
    wanted = section_record_id(section)
    if not wanted:
        return {}, ""
    state = _plan_html.read_state_file(Path(plan_path))
    project = str(state.get("project") or "")
    for record in state.get("sections") or ():
        if not isinstance(record, Mapping):
            continue
        if section_record_id(str(record.get("id") or "")) == wanted:
            return record, project
    return {}, project


def capability_raise(
    config: Mapping[str, Any],
    *,
    attempts: int | None,
    capability: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the raise a section's attempt count triggers, or None.

    The raise is one-way: each level it names moves a node up only, so a
    section that already declared more than the rule asks for records no move
    and stays on the lane its own capability selected. A config with no rule,
    or a threshold no count has reached, raises nothing.
    """
    rule = config.get("capability_raise") or {}
    if not isinstance(rule, Mapping):
        return None
    threshold = rule.get("attempts_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        return None
    if attempts is None or attempts < threshold:
        return None
    before = _effective_request(capability or {})
    after = _effective_request(capability or {})
    changes: list[dict[str, Any]] = []
    raised_class = str(rule.get("raised_class") or "")
    if raised_class and _rank(raised_class, CAPABILITY_CLASSES) > _rank(
        str(after["class"]), CAPABILITY_CLASSES
    ):
        changes.append({"field": "class", "from": after["class"], "to": raised_class})
        after["class"] = raised_class
    for field in ("reasoning", "verification"):
        wanted = str(rule.get(f"raised_{field}") or "")
        levels = REQUIREMENT_LEVELS[field]
        current = str(after["requirements"].get(field) or "")
        if wanted and _rank(wanted, levels) > _rank(current, levels):
            changes.append({"field": field, "from": current or None, "to": wanted})
            after["requirements"][field] = wanted
    return {
        "attempts": attempts,
        "threshold": threshold,
        "before": before,
        "after": after,
        "changes": changes,
    }


def resolve_section_routing(
    config: Mapping[str, Any],
    *,
    node: TaskNode,
    plan_path: str | Path,
) -> dict[str, Any]:
    """Resolve a node's routing at the capability its section's attempts earn.

    The typed record declares capability. Distinct executable runs provide the
    count that may raise it; an authored legacy count never steers the lane.
    """
    from reckon.mcp_views import section_attempt_count

    record, project = _section_record(plan_path, node.section)
    path = Path(plan_path).resolve()
    docs = path.parent.parent if path.parent.name == "plans" else path.parent
    root = docs.parent if docs.name == "docs" else None
    attempts = (
        section_attempt_count(project, node.plan, node.section, root)
        if record
        else None
    )
    declared = record.get("capability")
    if not isinstance(declared, Mapping):
        declared = None
    raise_record = capability_raise(config, attempts=attempts, capability=declared)
    effective = (
        raise_record["after"]
        if raise_record is not None
        else _effective_request(declared or {})
    )
    backend_name, backend = resolve_role(
        config,
        node.role,
        node.spec_level,
        capability_class=str(effective.get("class") or ""),
    )
    label = str(node.section or "section")
    if raise_record is None:
        summary = f"{label}: capability {effective['class']}; backend {backend_name}"
    else:
        moves = ", ".join(
            f"{change['field']} {change['from']}→{change['to']}"
            for change in raise_record["changes"]
        )
        detail = f"raised {moves}" if moves else f"already at {effective['class']}"
        summary = (
            f"{label} attempt {attempts} reached the raise threshold "
            f"{raise_record['threshold']}: {detail}; backend {backend_name}"
        )
    return {
        "role": node.role,
        "spec_level": node.spec_level,
        "section": node.section,
        "attempts": attempts,
        "capability": effective,
        "backend": backend_name,
        "backend_settings": backend,
        "raise": raise_record,
        "summary": summary,
    }


def resolve_budget_fallback(
    config: Mapping[str, Any],
    role: str,
    spec_level: str,
    held_backend_name: str,
    held_backend: Mapping[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Return a held backend's declared fallback, resolved for the same role.

    ``None`` when the held backend declares no fallback — a caller must then
    still refuse rather than guess a substitute. A fallback is backend-level
    data (declared on the backend that is spent), never inferred from the
    role or from what else happens to be configured.
    """
    fallback_name = held_backend.get("fallback")
    if not fallback_name:
        return None
    fallback_name = str(fallback_name)
    if fallback_name == held_backend_name:
        raise CrewError(
            f"backend {held_backend_name!r} declares itself as its own fallback"
        )
    backends = config.get("backends") or {}
    if fallback_name not in backends:
        known = ", ".join(sorted(backends)) or "none"
        raise CrewError(
            f"backend {held_backend_name!r} declares fallback {fallback_name!r}, "
            f"which no layer defines (defined backends: {known})"
        )
    return resolve_role_override(config, role, spec_level, fallback_name)


def _budget_verdict(
    *,
    project: str,
    root: str | Path | None,
    config: Mapping[str, Any] | None,
    backend_name: str,
    backend: Mapping[str, Any],
    purpose: str,
    budget_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Judge one backend's headroom for one purpose.

    Imported here rather than at module scope because the budget module reads run
    records through this one; deferring the import to call time keeps that a
    one-way dependency instead of a cycle.
    """
    from reckon import budget as budget_module

    if budget_state is None:
        recorded = budget_module.latest_recorded(project, root=root, config=config)
        state = budget_module.state_for(
            backend_name,
            backend,
            recorded=recorded.get(backend_name),
            unattributed=recorded.unattributed,
        )
    else:
        state = budget_module.BudgetState(**dict(budget_state))
    verdict = budget_module.decide(state, budget_module.policy(config), purpose=purpose)
    try:
        budget_module.record_checks(
            project,
            [verdict],
            root=root,
            resumption_fired=False,
        )
    except (ledger.LedgerError, OSError) as exc:
        verdict.setdefault("warnings", []).append(
            f"budget check passed but its ledger history was not recorded: {exc}"
        )
    return verdict


def resolved_time_budget(config: Mapping[str, Any], backend: Mapping[str, Any]) -> str:
    """Return a node's default time budget: role overlay first, fence fallback."""
    for candidate in (
        backend.get("time_budget"),
        (config.get("fences") or {}).get("time_budget"),
    ):
        if candidate:
            return str(candidate)
    return ""


def resolved_time_ceiling(config: Mapping[str, Any]) -> str:
    """Return the independent hard ceiling for an explicitly declared budget."""
    return str((config.get("fences") or {}).get("time_budget") or "")


# ── Dispatch ────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = velocity.run_git(repo, *args)
    encoding = locale.getpreferredencoding(False)
    result.stdout, result.stderr = (
        value.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        for value in (result.stdout, result.stderr)
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CrewError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return result


def _workspace_roots(repo: Path) -> list[Path]:
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    digest = hashlib.sha256(str(git_dir.resolve()).encode()).hexdigest()[:12]
    stem = f"{repo.name}-{digest}"
    override = os.environ.get("RECKON_WORKTREE_ROOT")
    preferred = (
        Path(override).expanduser().resolve()
        if override
        else repo.parent / ".reckon-worktrees"
    )
    if sum(part.lstrip(".") == "reckon-worktrees" for part in preferred.parts) > 1:
        raise CrewError(
            "refusing to nest another reckon-worktrees root; dispatch from the "
            "owning checkout or set RECKON_WORKTREE_ROOT outside the current root"
        )
    legacy = Path(tempfile.gettempdir()) / "reckon-worktrees" / stem
    roots = [preferred / stem]
    if legacy != roots[0]:
        roots.append(legacy)
    return roots


def _registered_worktrees(repo: Path) -> list[Path]:
    return [
        Path(line.removeprefix("worktree ")).resolve()
        for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
        if line.startswith("worktree ")
    ]


def _run_directory_extractions(runs_root: Path) -> list[Path]:
    """Return the git-less trees a worker left under the crew runs root.

    Workers keep two kinds of source tree in a run directory: registered
    worktrees, and plain extractions made with ``git archive`` that carry no
    git directory. Both appear in the same places — the children of a
    ``checkouts`` directory, and directories named for a base revision
    (``base-source``, ``base-tree``, ``base-wt`` ...) at the run-directory
    level or under ``artifacts``. A candidate that holds a git directory is a
    real worktree that the worktree registry already reports, so only the
    git-less ones are returned here — they are exactly the trees gc would
    otherwise never see. Each run directory is scanned directly; the walk never
    descends a whole run tree.
    """
    if not runs_root.is_dir():
        return []
    found: list[Path] = []
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        candidates: list[Path] = []
        for pattern in ("base-*", "artifacts/base-*"):
            candidates.extend(path for path in run_dir.glob(pattern) if path.is_dir())
        checkouts = run_dir / "checkouts"
        if checkouts.is_dir():
            candidates.extend(path for path in checkouts.iterdir() if path.is_dir())
        for candidate in candidates:
            git_dir = candidate / ".git"
            if git_dir.is_file() or git_dir.is_dir():
                continue
            if candidate not in found:
                found.append(candidate)
    return sorted(found)


def _extraction_report(path: Path) -> dict[str, Any]:
    """Report one git-less tree under the runs root as its own kind.

    A plain extraction has no commit, so it cannot be judged by containment and
    is never removable. Reporting it under one of the classifications the
    worktree rules own would mislabel it, so it states its kind instead. The
    caller's report loop adds the reclaimable and withheld fields like every
    other row.
    """
    return {
        "path": str(path),
        "kind": "extraction",
        "classification": "extraction",
    }


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


def _tree_state(path: Path) -> dict[str, Any]:
    """Return the commit and working-tree state needed for a boundary check.

    Harness scratch is left out of both the entries and the digest, so the
    bookkeeping the harness writes into every worktree never reads as a run's
    uncommitted work.
    """
    if not path.is_dir():
        return {
            "path": str(path),
            "available": False,
            "unavailable_state": "missing",
            "detail": "tree is no longer available",
        }
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=path,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        ],
        cwd=path,
        capture_output=True,
        check=False,
    )
    if head.returncode or status.returncode:
        detail = (
            os.fsdecode(head.stderr or b"").strip()
            or os.fsdecode(status.stderr or b"").strip()
            or "tree is unavailable"
        )
        return {
            "path": str(path),
            "available": False,
            "unavailable_state": "unreadable",
            "detail": detail,
        }
    entries = []
    kept: list[bytes] = []
    for raw in (item for item in status.stdout.split(b"\0") if item):
        if len(raw) < 4 or raw[2:3] != b" ":
            continue
        code = os.fsdecode(raw[:2])
        entry_path = os.fsdecode(raw[3:])
        if _harness_scratch_entry(code, entry_path):
            continue
        kept.append(raw)
        entries.append({"code": code, "path": entry_path})
    return {
        "path": str(path),
        "available": True,
        "head": os.fsdecode(head.stdout).strip(),
        "status_digest": "sha256:" + hashlib.sha256(b"\0".join(kept)).hexdigest(),
        "status_entries": entries,
    }


def _boundary_tree_roots(repository: str | Path, worktree: str | Path) -> list[Path]:
    """The two trees a fenced run's boundary check reads.

    A fenced worker cannot write outside its own worktree, so a boundary check
    over the rest of the worktree registry looks for a write the operating
    system already refused. The main checkout stays in the set as a second line
    behind the fence, because a write there goes live for every session on the
    repository.
    """
    return sorted({Path(repository).resolve(), Path(worktree).resolve()}, key=str)


def _repository_tree_snapshot(
    repo: Path, *, roots: Iterable[str | Path] | None = None
) -> dict[str, Any]:
    """Capture one deterministic snapshot of the repository's selected trees.

    With no explicit roots, the worktree registry is enumerated exactly once.
    Promotion supplies the persisted root set, so worktrees registered after
    dispatch cannot be charged to an earlier run.
    """
    selected = (
        _registered_worktrees(repo) if roots is None else [Path(p) for p in roots]
    )
    resolved = sorted({path.resolve() for path in selected}, key=str)
    return {
        "version": 1,
        "status_digest": "sha256",
        "trees": [_tree_state(path) for path in resolved],
    }


def _commits_beyond_merge_base(
    repo: Path, path: Path, integrated_into: str
) -> list[dict[str, str]] | None:
    """List the commits a worktree carries beyond the integration head.

    ``git cherry`` compares each commit's patch against the integration head and
    marks the ones whose change is already there, so a commit that landed as
    part of a squash or a rebase is recognised even though its sha is not an
    ancestor. ``None`` means the comparison could not run, which a caller
    reports as an unmeasured list rather than as an empty one, so an unreadable
    tree is kept rather than released.
    """
    from reckon.crew.recovery import _resolve_commit

    integration = _resolve_commit(repo, integrated_into)
    if not integration:
        return None
    result = subprocess.run(
        ["git", "cherry", "-v", integration, "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    commits: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        mark, _, remainder = line.partition(" ")
        sha, _, subject = remainder.partition(" ")
        if mark in {"+", "-"} and sha:
            commits.append({"sha": sha, "subject": subject, "equivalent": mark == "-"})
    return commits


def _inspect_workspace(
    repo: Path,
    path: Path,
    integrated_into: str,
    claimed_by: Iterable[str],
    shadow_record: Mapping[str, Any] | None = None,
    *,
    raise_on_unavailable: bool = True,
    release_residue: bool = False,
) -> dict[str, Any]:
    """Inspect one worktree's commits, residue and classification.

    The two commit-list fields are null exactly when the commits beyond the
    integration head were not measured — this caller did not ask, the head is
    an ancestor, the tree is clean, or the comparison could not run — and the
    sibling ``commits_unmeasured_reason`` names which. A measured row keeps
    both lists even when they are empty.
    """
    state = _tree_state(path)
    if not state.get("available"):
        detail = state.get("detail") or "tree is unavailable"
        if raise_on_unavailable:
            raise CrewError(f"worktree {path} is unavailable: {detail}")
        # A caller sweeping a whole registry (gc) cannot abort on one tree whose
        # directory is gone while git still lists it: one such registration
        # would otherwise stop the pass from reclaiming every other tree. It is
        # reported as its own classification and left for a reader, never
        # judged by the worktree rules — there is no working tree to read a
        # commit or a status from.
        return {
            "path": str(path),
            "head": "",
            "classification": "unavailable",
            "unavailable_state": state.get("unavailable_state") or "missing",
            "detail": detail,
            "dirty": [],
            "integrated_into": integrated_into,
            "claimed_by_live_runs": sorted(claimed_by),
            "non_equivalent_commits": None,
            "patch_equivalent_commits": None,
            "commits_unmeasured_reason": (
                "the worktree is unavailable, so there is no tree to measure"
            ),
            "shadow_run_id": "",
            "shadow_patch": "",
        }
    dirty = [f"{entry['code']} {entry['path']}" for entry in state["status_entries"]]
    head = str(state.get("head") or "")
    reachable = (
        _git(
            repo,
            "merge-base",
            "--is-ancestor",
            head,
            integrated_into,
            check=False,
        ).returncode
        == 0
    )
    claims = sorted(claimed_by)
    commits: list[dict[str, str]] | None = None
    if release_residue and not reachable and dirty:
        commits = _commits_beyond_merge_base(repo, path, integrated_into)
    if commits is None:
        if not release_residue:
            unmeasured_reason = (
                "release_residue is false: this caller did not ask for the "
                "commits beyond the integration head to be measured"
            )
        elif reachable:
            unmeasured_reason = (
                "the worktree head is an ancestor of the integration head, so "
                "it carries no commits beyond it"
            )
        elif not dirty:
            unmeasured_reason = (
                "the worktree is clean, so no residue release reads its commits"
            )
        else:
            unmeasured_reason = (
                "the comparison against the integration head could not run"
            )
    else:
        unmeasured_reason = ""
    if claims:
        classification = "live-referenced"
    elif shadow_record is not None and _shadow_patch_retained(shadow_record):
        classification = "disposable"
    elif dirty:
        landed_elsewhere = bool(commits) and all(
            commit["equivalent"] for commit in (commits or [])
        )
        classification = (
            "dirty-integrated"
            if release_residue and (reachable or landed_elsewhere)
            else "dirty"
        )
    elif reachable:
        classification = "integrated"
    else:
        classification = "unintegrated"
    return {
        "path": str(path),
        "head": head,
        "classification": classification,
        "dirty": dirty,
        "integrated_into": integrated_into,
        "claimed_by_live_runs": claims,
        "non_equivalent_commits": (
            None
            if commits is None
            else [commit for commit in commits if not commit["equivalent"]]
        ),
        "patch_equivalent_commits": (
            None
            if commits is None
            else [commit for commit in commits if commit["equivalent"]]
        ),
        "commits_unmeasured_reason": unmeasured_reason,
        "shadow_run_id": (
            str(shadow_record.get("run_id") or "") if shadow_record else ""
        ),
        "shadow_patch": (
            str(shadow_record.get("shadow_patch") or "") if shadow_record else ""
        ),
    }


def _gc_projects(repo: Path, project: str | None) -> list[str]:
    """Return the project names whose ledgers a gc pass reads."""
    if project:
        return [project]
    state_root = repo / "docs" / "state"
    if state_root.is_dir():
        return sorted(path.name for path in state_root.iterdir() if path.is_dir())
    return []


def _ledgered_records(repo: Path, project: str | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in _gc_projects(repo, project):
        result.extend(ledger.runs(str(name), root=repo))
    return result


def _ledgered_run_ids(repo: Path, project: str | None) -> set[str]:
    return {
        str(record.get("run_id") or "")
        for record in _ledgered_records(repo, project)
        if record.get("run_id")
    }


def shadow_worktree_session(
    primary_run_id: str, candidate_backend: str, component: str = ""
) -> str:
    """Return the session token a shadow worktree lives under.

    New shadows add a recorded unique component beyond the primary run and
    candidate backend, allowing repeated arms on one backend to retain separate
    worktrees. A missing component preserves the single legacy location for
    records written before repeated arms were supported.
    """
    stem = f"shadow-{primary_run_id}-{candidate_backend}"
    return f"{stem}-{component}" if component else stem


def _shadow_patch_retained(record: Mapping[str, Any]) -> bool:
    run_id = str(record.get("run_id") or "")
    artifact = Path(str(record.get("shadow_patch") or ""))
    expected = runs_dir() / run_id / "shadow.patch"
    return (
        bool(run_id) and artifact.resolve() == expected.resolve() and artifact.is_file()
    )


def _shadow_worktree_records(
    repo: Path, project: str | None
) -> dict[Path, dict[str, Any]]:
    roots = _workspace_roots(repo)
    result: dict[Path, dict[str, Any]] = {}
    for record in _ledgered_records(repo, project):
        lineage = record.get("lineage")
        if not isinstance(lineage, Mapping) or lineage.get("kind") != "shadow":
            continue
        primary_run_id = str(lineage.get("primary_run_id") or "")
        node = str(record.get("node") or "")
        if not primary_run_id or not node:
            continue
        # The candidate backend comes from the committed record, not current
        # flight config: the record is what says which candidate actually ran.
        candidate = str(record.get("backend") or "").strip()
        component = str(lineage.get("worktree_component") or "").strip()
        # A record that predates the candidate-named path (or never named one)
        # still resolves its single legacy worktree; only one candidate could
        # have produced a shadow before the candidate entered the path.
        session = (
            shadow_worktree_session(primary_run_id, candidate, component)
            if candidate
            else f"shadow-{primary_run_id}"
        )
        for root in roots:
            result[(root / session / node).resolve()] = record
    return result


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


def _residue_run_record(
    path: Path, records: Iterable[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """Attribute a finished tree only when one ledger row identifies it."""
    matches = [
        record
        for record in records
        if str(record.get("run_id") or "")
        and (
            str(record.get("worktree") or "") == str(path)
            or str(record.get("run_id")) in path.parts
            or (
                str(record.get("session") or "") == path.parent.name
                and str(record.get("node") or "") == path.name
            )
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _residue_file_bytes(path: Path) -> bytes | None:
    if path.is_symlink():
        return os.fsencode(os.readlink(path))
    return path.read_bytes() if path.is_file() else None


def _git_blob(repo: Path, revision: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _residue_digests() -> set[tuple[str, str]]:
    """Find equal path contents already preserved by another release."""
    roots = (runs_dir(), runs_dir().parent / "worktree-residue")
    found: set[tuple[str, str]] = set()
    for root in roots:
        for record in (
            root.glob("*/worktree-residue/*/classification.json")
            if root == roots[0]
            else root.glob("*/classification.json")
        ):
            try:
                rows = json.loads(record.read_text()).get("paths", {})
            except (OSError, ValueError):
                continue
            for path, detail in rows.items():
                if isinstance(detail, Mapping) and detail.get("digest"):
                    found.add((path, str(detail["digest"])))
    return found


# Where a release with the archive option keeps a worktree's commits. One ref
# per worktree directory name; the ref is what keeps the commits reachable
# after the tree is gone, so it must be created before the removal, not after.
ARCHIVE_REF_PREFIX = "refs/reckon/archive/"


def _pin_archived_commits(repo: Path, ref: str, head: str) -> None:
    """Point ``ref`` at ``head`` so its commits survive the worktree's removal.

    An existing ref is refused unless it already points at the same commit:
    overwriting it would drop an earlier worktree's pinned commits out of reach,
    which is the very loss the archive exists to prevent.
    """
    existing = _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
    if existing.returncode == 0:
        if existing.stdout.strip() == head:
            return
        raise CrewError(
            f"archive ref {ref} already resolves to {existing.stdout.strip()}; "
            "refusing to overwrite the earlier pin"
        )
    zeros = "0" * 40
    _git(repo, "update-ref", ref, head, zeros)


def _live_pointer_worktrees() -> dict[Path, list[str]]:
    """Every worktree a live pointer names, whatever state that pointer is in.

    The phase-gated claim keeps a tree only while the pointer reads as
    in-flight, and a parked run's pointer is left in a terminal-looking phase
    while its worker is between turns: the tree then reads as reclaimable in
    the window from dispatch to first commit, which is exactly where a
    measure-first node parks on a long job. A tree stays a run's until
    promotion or discard removes the pointer, so every removal path reads this
    wider claim rather than the phase it happens to carry.
    """
    claims: dict[Path, list[str]] = {}
    for record in list_live():
        worktree = record.get("worktree")
        if not worktree:
            continue
        path = Path(str(worktree)).resolve()
        claims.setdefault(path, []).append(str(record.get("run_id") or "unknown"))
    return claims


def _run_worktree_path(repo: Path, project: str | None, run_id: str) -> Path:
    """The worktree a run's own records name, preferring its live pointer.

    A retained tree is named by the run's ledger row once its live pointer has
    been promoted away, so both sources are read; a run neither source names
    has no tree to confine a sweep to and is refused.
    """
    for record in list_live():
        if str(record.get("run_id") or "") != run_id:
            continue
        value = str(record.get("worktree") or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    for record in _ledgered_records(repo, project):
        if str(record.get("run_id") or "") != run_id:
            continue
        retention = record.get("worktree_retention")
        values: list[str] = []
        if isinstance(retention, Mapping):
            values.append(str(retention.get("worktree") or ""))
        values.append(str(record.get("worktree") or ""))
        for value in values:
            if value.strip():
                return Path(value.strip()).expanduser().resolve()
    raise CrewError(f"no record names a worktree for run {run_id}")


def _save_and_release_worktree(
    repo: Path,
    path: Path,
    integrated_into: str,
    record: Mapping[str, Any] | None = None,
    *,
    archive_ref: str = "",
) -> dict[str, Any]:
    """Save, verify, and clear a finished tree before a non-forced removal.

    ``archive_ref`` pins the tree's commits into that ref before the removal
    clears the working copy, so a tree holding commits with no patch-equivalent
    on the integration head can be released without losing them; the ref is
    recorded beside the residue patch. Without it the tree must be integrated
    as before.
    """
    if _live_pointer_worktrees().get(path.resolve()):
        raise CrewError(f"refusing residue release of live worktree {path}")
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
    if archive_ref:
        _pin_archived_commits(repo, archive_ref, head)
    elif _git(
        repo, "merge-base", "--is-ancestor", head, integrated_into, check=False
    ).returncode:
        landed = _commits_beyond_merge_base(repo, path, integrated_into)
        if not landed or not all(commit["equivalent"] for commit in landed):
            raise CrewError(f"worktree {path} is not integrated into {integrated_into}")
    status_before = _tree_state(path)["status_digest"]
    patch = subprocess.run(
        ["git", "diff", "HEAD", "--binary", "--no-ext-diff", "--no-renames", "--"],
        cwd=path,
        capture_output=True,
        check=True,
    ).stdout
    tracked = [
        os.fsdecode(name)
        for name in subprocess.run(
            ["git", "diff", "HEAD", "--name-only", "--no-renames", "-z", "--"],
            cwd=path,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        if name
    ]
    untracked = [
        os.fsdecode(name)
        for name in subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=path,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        if name
    ]
    if not patch and not untracked:
        raise CrewError(f"worktree {path} has no residue to save")
    run_id = str(record.get("run_id") or "") if record else ""
    parent = (
        (runs_dir() / run_id / "worktree-residue")
        if run_id
        else (runs_dir().parent / "worktree-residue")
    )
    parent.mkdir(parents=True, exist_ok=True)
    name = f"{path.name}-{hashlib.sha256(os.fsencode(path)).hexdigest()[:12]}-"
    destination = Path(tempfile.mkdtemp(prefix=name, dir=parent))
    patch_path = destination / "residue.patch"
    tar_path = destination / "untracked.tar"
    class_path = destination / "classification.json"
    patch_path.write_bytes(patch)
    with tarfile.open(tar_path, "w") as archive:
        for relative in untracked:
            archive.add(path / relative, arcname=relative, recursive=False)
    with tarfile.open(tar_path) as archive:
        if sorted(archive.getnames()) != sorted(untracked):
            raise CrewError(f"saved untracked archive for {path} is incomplete")
        for relative in untracked:
            member = archive.getmember(relative)
            source = path / relative
            if member.issym():
                if not source.is_symlink() or member.linkname != os.readlink(source):
                    raise CrewError(f"saved symlink {relative} does not match {path}")
            else:
                saved = archive.extractfile(member)
                if saved is None or saved.read() != source.read_bytes():
                    raise CrewError(f"saved file {relative} does not match {path}")
    if patch:
        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
            subprocess.run(
                ["git", "read-tree", head],
                cwd=path,
                env=env,
                check=True,
                capture_output=True,
            )
            verified = subprocess.run(
                ["git", "apply", "--cached", "--check", str(patch_path)],
                cwd=path,
                env=env,
                capture_output=True,
                check=False,
            )
            if verified.returncode:
                raise CrewError(
                    f"saved patch for {path} does not apply to its HEAD: {os.fsdecode(verified.stderr).strip()}"
                )
    already_saved = _residue_digests()
    # The worktree's base is the merge base with the integration head when the
    # record names none: for an ancestor head that is the head itself, and for a
    # head whose commits landed elsewhere it is where the two histories parted,
    # so "superseded" asks whether the integration head moved the path since.
    base = (str(record.get("base_sha") or "") if record else "") or _git(
        repo, "merge-base", integrated_into, head, check=False
    ).stdout.strip()
    base = base or head
    classes: dict[str, str] = {}
    detail: dict[str, dict[str, str]] = {}
    for relative in sorted(set(tracked + untracked)):
        content = _residue_file_bytes(path / relative)
        digest = hashlib.sha256(
            b"absent\0" if content is None else b"present\0" + content
        ).hexdigest()
        if (
            content == _git_blob(repo, integrated_into, relative)
            or (relative, digest) in already_saved
        ):
            category = "subsumed"
        elif _git_blob(repo, base, relative) != _git_blob(
            repo, integrated_into, relative
        ):
            category = "superseded"
        else:
            category = "unique"
        classes[relative] = category
        detail[relative] = {"class": category, "digest": digest}
    class_payload: dict[str, Any] = {
        "worktree": str(path),
        "head": head,
        "paths": detail,
    }
    if archive_ref:
        class_payload["archive_ref"] = archive_ref
    class_path.write_text(json.dumps(class_payload, indent=2) + "\n")
    if _tree_state(path)["status_digest"] != status_before:
        raise CrewError(
            f"worktree {path} changed while residue was saved; preserved copy at {destination}"
        )
    _git(path, "read-tree", head)
    for relative in tracked:
        target = path / relative
        if (
            _git(repo, "cat-file", "-e", f"{head}:{relative}", check=False).returncode
            == 0
        ):
            _git(path, "checkout-index", "--force", "--", relative)
        elif target.is_file() or target.is_symlink():
            target.unlink()
    for relative in untracked:
        target = path / relative
        if target.is_file() or target.is_symlink():
            target.unlink()
    if _git(path, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise CrewError(
            f"worktree {path} remains dirty after residue was saved at {destination}"
        )
    _git(repo, "worktree", "remove", str(path))
    saved: dict[str, Any] = {
        "residue_patch": str(patch_path),
        "residue_tar": str(tar_path),
        "residue_classification": str(class_path),
        "residue_classes": classes,
        "head": head,
    }
    if archive_ref:
        saved["archive_ref"] = archive_ref
    return saved


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
# Figures the routing derivation reads from raw run streams when the ledger
# row carries no recorded measurement. A run source may be reaped only once
# every such figure is recorded on its row, because a reaped stream is the
# last copy of the unrecorded figure. The check reads this declaration rather
# than a hardcoded pair, so a figure added to the derivation later is
# protected by the same rule without editing the reaper: extending this tuple
# automatically extends what must be recorded before a source is removed.
STREAM_DERIVED_FIELDS = ("orientation_input_tokens", "tool_steps")


def _recorded_figure_blocks(
    record: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """The places a stream-derived figure may already be recorded on a row.

    Order matches the preference order the routing derivation reads in
    ``capabilities`` — the row itself first, then the measurement blocks the
    warm functions consult for a recorded value — so the reaper and the
    derivation agree on when a figure exists without either re-walking the
    stream.
    """
    blocks: list[Mapping[str, Any]] = [record]
    for name in ("throughput", "budget"):
        block = record.get(name)
        if isinstance(block, Mapping):
            blocks.append(block)
    return tuple(blocks)


def _measured_number(value: Any) -> float | None:
    """Return a finite non-negative measurement, without treating a bool as one."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    measured = float(value)
    return measured if math.isfinite(measured) and measured >= 0 else None


def _figure_retention(record: Mapping[str, Any], field: str) -> str:
    """How a declared stream-derived figure already stands on a ledger row.

    One of ``"recorded"`` — a measured number sits where the derivation reads
    it, so the stream is no longer needed; ``"explicitly-absent"`` — the row
    itself carries the key, but without a measured value, meaning the
    derivation ran when the stream was already gone and recorded the figure as
    unrecoverable, so a surviving run directory holds nothing that could
    produce it; or ``"missing"`` — the key is absent everywhere, so the
    derivation has never run and the stream is the last copy of an unrecorded
    figure. The last two are told apart by key presence rather than value
    truthiness, so a recorded zero is a measurement and still permits reaping
    while an absent key does not.
    """
    if any(
        _measured_number(block.get(field)) is not None
        for block in _recorded_figure_blocks(record)
    ):
        return "recorded"
    if field in record:
        return "explicitly-absent"
    return "missing"


def _explicitly_absent_figures(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The declared stream-derived figures recorded as unrecoverable."""

    return tuple(
        field
        for field in STREAM_DERIVED_FIELDS
        if _figure_retention(record, field) == "explicitly-absent"
    )


# A failure of this family is a defect in the sweep's own code rather than a
# refusal of the tree it happened on. Reported as an ordinary refusal it would
# read to the caller as gc declining a worktree, so it is re-raised with its
# traceback and names the line that is wrong.
PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError)


class GcSweepError(CrewError):
    """A sweep that stopped mid-pass, carrying the steps it already applied.

    A pass removes and refuses worktrees one at a time, so a failure after the
    first removal would otherwise reach the caller as a bare message: the tree
    already gone from disk, and nothing naming it. ``partial`` holds the report
    the pass had built — the removed list, the rows already judged and the path
    the failure was reached through — so a caller that never sees a return
    value can still report what was done.
    """

    def __init__(self, message: str, partial: dict[str, Any]) -> None:
        super().__init__(message)
        self.partial = partial


def _pinnable_unique_commits(item: Mapping[str, Any], pin_unique_commits: bool) -> bool:
    """Whether an explicit pin request makes this row reclaimable.

    Only a dirty row carrying measured non-equivalent commits qualifies: the
    archive ref is what makes its removal safe, so a row whose commits were
    never measured is not released on the strength of an unread comparison.
    """
    return bool(
        pin_unique_commits
        and item["classification"] == "dirty"
        and item.get("non_equivalent_commits")
    )


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

    ``pin_unique_commits`` releases a dirty worktree whose commits have no
    patch-equivalent on the integration head by first pinning those commits
    into an archive ref under ``refs/reckon/archive/``; without it such a tree
    is kept, as before. ``run_id`` confines the pass to that run's own tree and
    to that run's pointer and run directory, so clearing one held tree can
    never reach a peer's.
    """
    if retention_days < 0:
        raise CrewError("retention days cannot be negative")
    repo_root = Path(repo).resolve()
    from reckon.crew.recovery import _resolve_commit

    if not _resolve_commit(repo_root, integrated_into):
        raise CrewError(f"integration revision {integrated_into!r} is not a commit")
    confined_to: Path | None = None
    if run_id:
        confined_to = _run_worktree_path(repo_root, project, run_id)
        registered = {
            path.resolve()
            for path in _registered_worktrees(repo_root)
            if path != repo_root
        }
        if confined_to not in registered:
            raise CrewError(
                f"run {run_id} names worktree {confined_to}, which is not a "
                "worktree this repository registers"
            )
    worktrees: list[dict[str, Any]] = []
    removed: list[str] = []
    refused: list[str] = []
    residue_report: list[dict[str, Any]] = []
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
            and (confined_to is None or path.resolve() == confined_to)
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
                    elif item["classification"] == "disposable":
                        shadow_record = shadow_records.get(path.resolve())
                        if shadow_record is None or not _shadow_patch_retained(
                            shadow_record
                        ):
                            item["classification"] = "unintegrated"
                            continue
                        _git(repo_root, "worktree", "remove", "--force", str(path))
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
                        _git(repo_root, "worktree", "remove", str(path))
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
        if runs_root.is_dir():
            for directory in sorted(
                path for path in runs_root.iterdir() if path.is_dir()
            ):
                if run_id and directory.name != run_id:
                    continue
                if directory.name not in ledgered or directory.name in live_ids:
                    continue
                modified = datetime.fromtimestamp(
                    directory.stat().st_mtime, tz=timezone.utc
                )
                if modified > cutoff:
                    continue
                record = ledgered_records.get(directory.name, {})
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
        "residue_report": residue_report,
        "pointers": pointer_reports,
        "run_directories": run_reports,
        "run_directories_withheld": run_directories_withheld,
        "run_directories_reaped_with_explicitly_absent_figures": (
            run_directories_reaped_with_explicitly_absent_figures
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
# the node owns but no run can be attributed to is reported and left alone: it
# may hold evidence no record points at, so a machine that cannot name its owner
# must not delete it. This is the same rule promotion's release step already
# applies to a run's own scratch, exposed as a survey over the whole root.

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


def _iso_ctime(path: Path) -> str | None:
    """The directory's ctime as an ISO timestamp, or ``None`` if unreadable."""
    try:
        return datetime.fromtimestamp(path.stat().st_ctime, tz=UTC).isoformat()
    except OSError:
        return None


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

    A run's scratch directory is removed only when the run has no live pointer
    and its terminal record exists — a ledger row or a discard marker — and the
    decision is never made by age. Every path and its size are printed before
    anything is deleted, and the whole survey is a dry run unless ``apply`` is
    given. A directory no run can be attributed to, whether beneath the scratch
    root or a stray tree beside it, is reported with its ctime, its size and any
    process holding it, and is never removed.
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
        if state == SCRATCH_TERMINAL:
            if apply:
                print(f"removing worker scratch directory {child} ({size} bytes)")
                shutil.rmtree(child)
                report["removed"] = True
                removed.append(str(child))
            else:
                print(f"would remove worker scratch directory {child} ({size} bytes)")
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


def _fleet_script() -> Path:
    """Resolve the worktree fleet script from the running reckon installation.

    The script is repository-agnostic: it derives every path it touches from
    the ``--repo`` it is handed and from reckon's config home, and reads
    nothing relative to its own location. Requiring a copy inside each
    dispatched repository therefore bought no isolation and made dispatch
    depend on a per-repository file that nothing installs — so a repository
    that had never been hand-provisioned could not be dispatched into at all,
    which is the whole failure mode when the write repository and authority
    repository differ. One resolved copy also keeps the script and the reckon
    that invokes it at the same version.
    """
    package_dir = Path(__file__).resolve().parent.parent
    candidates = (package_dir.parent / "skills", package_dir / "_skills")
    for candidate in candidates:
        script = candidate / "reckon-build" / "scripts" / "worktree_fleet.py"
        if script.is_file():
            return script
    searched = ", ".join(str(path) for path in candidates)
    raise CrewError(
        format_refusal(
            "D17",
            "the reckon installation is missing its worktree fleet script; "
            f"searched: {searched}; reinstall reckon",
        )
    )


def _create_worktree(
    repo: Path, session: str, worker: str, base: str
) -> dict[str, Any]:
    """Create a detached worktree through the fleet script, or raise."""
    script = _fleet_script()
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "create",
            "--repo",
            str(repo),
            "--session",
            session,
            "--worker",
            worker,
            "--base",
            base,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        payload = {}
    if result.returncode or not payload.get("ok"):
        detail = payload.get("error") or result.stderr.strip() or result.stdout.strip()
        raise CrewError(f"worktree creation failed: {detail}")
    return payload


def _remove_worktree(repo: Path, path: str) -> None:
    """Undo a worktree created for a dispatch that then failed.

    Liveness is read through the wide claim, so a run parked between turns —
    its pointer carrying a terminal-looking phase while its worker is gone —
    keeps its tree: the phase-gated claim would read that pointer as finished
    and this force-removal would take the tree of a live run.
    """
    claims = _live_pointer_worktrees().get(Path(path).resolve(), [])
    if claims:
        raise CrewError(
            f"refusing to remove worktree {path}: claimed by live runs "
            f"{', '.join(sorted(claims))}"
        )
    subprocess.run(
        ["git", "worktree", "remove", "--force", path],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "worktree", "prune"], cwd=str(repo), capture_output=True, check=False
    )


# The run directory's own record of who signalled it. One line per signal,
# appended rather than rewritten, because a run may be signalled more than once
# and the order is part of the fact. The name is fixed so a reader — and the
# attribution scan — looks in one place.
SENDER_RECORD_NAME = "senders.jsonl"


def run_directory_of(record: Mapping[str, Any] | None) -> Path | None:
    """The directory a run's sender records land in, from its id or its log.

    Returns ``None`` when the record names neither, so a caller that signals
    something which is not a run (a session-start copy, a standing suite) can
    still use the shared writer without inventing a location for it.
    """
    if not record:
        return None
    run_id = str(record.get("run_id") or "")
    if run_id:
        return Path(_run_dir(run_id))
    log_path = str(record.get("log_path") or "")
    if log_path:
        return Path(log_path).parent
    return None


def _write_sender_record(
    run_dir: str | Path | None,
    *,
    target_pid: int,
    reason: str,
    sig: int | None = None,
    target_pgid: int | None = None,
    outcome: str | None = None,
    detail: str | None = None,
    project: str | None = None,
) -> Path | None:
    """Append one sender record into the target run's directory.

    This is the one writer every signalling path funnels through, so the next
    unattributed SIGTERM is read from the run's own directory rather than
    reconstructed from the survivors. A signal leaves exactly one attribution
    record and one outcome record: the attribution record is written BEFORE the
    signal is delivered, because a signal that ends the sender too must still
    leave the attribution behind, which a write ordered after the signal cannot
    promise. The outcome of the attempt is appended as a second record once the
    attempt returns, because before it runs the outcome is unknowable and a
    record that names a SIGTERM that was never sent is worse than none.

    ``outcome`` is ``"delivered"``, ``"refused"`` or ``"failed"``; ``detail``
    carries the guard's reason for a refusal or the operating system's message
    for a failure. A record written before the attempt leaves both unset.

    ``project`` is the watched project a sender record's target belongs to. It
    is written as its own field rather than folded into ``detail`` so a reader
    can act on the project without parsing a free-text message; a watcher's
    record, which lands in a shared watch directory rather than a run
    directory, is the case it exists for.

    The target's process group is recorded as well as its pid, because the two
    answer different questions — a group signal that reached unrelated work is
    only visible from the group id. A path that cannot be written (a run
    directory already removed by a rollback) is not raised: the signal is the
    safety mechanism and the record is the attribution, so a failed write must
    never withhold the signal. Returns the path written, or ``None``.
    """
    if run_dir is None:
        return None
    try:
        pid = int(target_pid)
    except (TypeError, ValueError):
        return None
    if target_pgid is None:
        try:
            target_pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            target_pgid = None
    if sig is None:
        sig = signal.SIGTERM
    try:
        signal_name = signal.Signals(sig).name
    except (ValueError, TypeError):
        signal_name = str(sig)
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    record = {
        "sender_pid": os.getpid(),
        "sender_argv0": sys.argv[0] if sys.argv else "",
        "target_pid": pid,
        "target_pgid": int(target_pgid) if target_pgid is not None else None,
        "reason": str(reason or ""),
        "signal": signal_name,
        "time": now,
    }
    if outcome is not None:
        record["outcome"] = str(outcome)
    if detail is not None:
        record["outcome_detail"] = str(detail)
    if project is not None:
        record["project"] = str(project)
    path = Path(run_dir) / SENDER_RECORD_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return None
    return path


def signal_worker(
    pid: int,
    sig: int = signal.SIGTERM,
    *,
    reason: str = "",
    run_dir: str | Path | None = None,
    project: str | None = None,
) -> bool:
    """Signal one spawned process, never a group it does not lead.

    ``os.killpg`` takes a process GROUP id, and ``killpg(1, ...)`` is
    ``kill(-1, ...)`` — every process the caller is permitted to signal. A pid
    taken from a scan or a run record can report a group it does not lead: one
    reparented to init, or started without a session of its own, shares its
    group with unrelated work. Signalling that group reaches the whole account
    across every control group and session, which no caller here intends, and
    the reach is invisible at the call site because the argument looks like one
    process.

    The group is therefore signalled only when the process leads it, which is
    true of anything started detached on purpose and is the case the group
    signal exists for. Otherwise the process alone is signalled and its children
    are left running, because ending one worker is never worth the risk of
    ending everything. Returns whether a signal was delivered.

    This is the only function in the crew that calls ``os.kill`` or
    ``os.killpg``, so the attribution scan has one home to point at. A caller
    that names the target's run directory gets a sender record written before
    the signal goes out, and a matching outcome record after; a caller
    signalling something that is not a run passes no directory and writes
    nothing.
    """
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    target_is_own_group_leader = group == pid and group > 1
    signalled_as_group = target_is_own_group_leader and group != os.getpgid(0)
    if run_dir is not None:
        _write_sender_record(
            run_dir,
            target_pid=pid,
            target_pgid=group,
            reason=reason,
            sig=sig,
            project=project,
        )
    try:
        if signalled_as_group:
            os.killpg(group, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError) as exc:
        if run_dir is not None:
            _write_sender_record(
                run_dir,
                target_pid=pid,
                target_pgid=group,
                reason=reason,
                sig=sig,
                outcome="failed",
                detail=str(exc) or type(exc).__name__,
                project=project,
            )
        return False
    if run_dir is not None:
        _write_sender_record(
            run_dir,
            target_pid=pid,
            target_pgid=group,
            reason=reason,
            sig=sig,
            outcome="delivered",
            project=project,
        )
    return True


def _signal_process_group(
    pid: int,
    expected_start_time: str | None,
    *,
    reason: str = "",
    run_dir: str | Path | None = None,
    project: str | None = None,
) -> None:
    """Signal a worker only while its pid still names the spawned process.

    A recorded pid is data from a run record, not a fact about who is calling
    this function — a test double, a stale carry-forward, or any record whose
    pid happens to equal the caller's own is otherwise indistinguishable from
    a genuine spawned worker. os.killpg signals the whole process group, so
    signalling one's own group takes the caller down with it. Refuse before
    that lookup rather than let the OS enforce it as a self-inflicted SIGTERM.

    A caller that names the target's run directory and reason gets its
    attribution and outcome records written by this one writer through
    :func:`signal_worker`, so a kill read from that run's own directory names
    the caller rather than only the victim. Callers do not write an attribution
    of their own: a pre-write beside this one would leave two attribution
    records for one signal.

    A guard that refuses the signal writes its own record carrying the
    ``refused`` outcome and the guard's reason, so a refusal is not read later
    as a SIGTERM that went out.
    """
    own_pid = os.getpid()
    if pid == own_pid or os.getpgid(pid) == os.getpgid(own_pid):
        detail = (
            f"refusing to signal pid {pid}: it is this process's own pid or "
            "shares this process's own process group, and killpg would "
            "terminate the caller doing the releasing"
        )
        _write_sender_record(
            run_dir,
            target_pid=pid,
            reason=reason,
            sig=signal.SIGTERM,
            outcome="refused",
            detail=detail,
            project=project,
        )
        raise CrewError(detail)
    actual_start_time = _process_start_time(pid)
    if not expected_start_time or actual_start_time != expected_start_time:
        detail = (
            f"refusing to signal pid {pid}: process identity changed "
            f"from {expected_start_time!r} to {actual_start_time!r}"
        )
        _write_sender_record(
            run_dir,
            target_pid=pid,
            reason=reason,
            sig=signal.SIGTERM,
            outcome="refused",
            detail=detail,
            project=project,
        )
        raise CrewError(detail)
    signal_worker(pid, signal.SIGTERM, reason=reason, run_dir=run_dir, project=project)


def _base_commit(repo: Path, base: str) -> str:
    """Resolve a worktree base to a commit without accepting option-like refs."""
    from reckon.crew.recovery import _resolve_commit

    commit = _resolve_commit(repo, base)
    if not commit:
        raise PlanVisibilityError(
            f"worktree base {base!r} is not a readable commit; commit the plan "
            "before dispatching"
        )
    return commit


def _contains_plan_section(html_text: str, section: str) -> bool:
    """Return whether authored HTML exposes the requested section."""
    from bs4 import BeautifulSoup

    requested = re.sub(r"\s+", " ", section.strip())
    if not requested:
        return True
    requested_folded = requested.casefold()
    ids = section_id_candidates(requested)

    soup = BeautifulSoup(html_text, "html.parser")
    if any(
        str(tag.get("id") or "").casefold() in ids for tag in soup.find_all(id=True)
    ):
        return True
    for heading in plan_headings(html_text):
        text = re.sub(r"\s+", " ", heading.text).casefold()
        if text == requested_folded or re.match(
            rf"^{re.escape(requested_folded)}(?:\s|[-—:])", text
        ):
            return True
    return False


def require_plan_section_visible(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path,
    base: str,
    authority: Mapping[str, Any],
) -> str:
    """Return the plan commit after proving its mounted file is committed."""

    from reckon.resources import ResourceCollision, resolve_resource

    repo_root = Path(repo).resolve()
    plan_data = authority["plan"]
    plan_repo = Path(str(plan_data["repository"])).resolve()
    docs_dir = Path(str(plan_data["docs"])).resolve()
    plan_base = base if plan_repo == repo_root else "HEAD"
    if plan_data.get("source") == "repository" and (
        not docs_dir.is_dir() or not any(docs_dir.rglob("*.html"))
    ):
        # Repositories that have not adopted HTML plans retain the original
        # local dispatch path.  This is not cross-repository authority: both
        # semantic and write roots are the explicitly supplied repository.
        return _base_commit(plan_repo, plan_base)
    try:
        resource = resolve_resource(
            docs_dir, project, node.plan, "plan", include_archived=False
        )
    except ResourceCollision as exc:
        raise PlanVisibilityError(
            f"plan {node.plan!r} cannot be resolved in {docs_dir}: {exc}; "
            "commit one unambiguous plan before dispatching"
        ) from exc
    if resource is None:
        if plan_data.get("source") == "repository":
            raise PlanVisibilityError(
                f"project {project!r} is missing from mounts.json and plan "
                f"{node.plan!r} is not readable in local repository {plan_repo}; "
                "register the plan repository with `reckon sync`"
            )
        raise PlanVisibilityError(
            f"plan {node.plan!r} is not readable through project {project!r} mount "
            f"{docs_dir}; commit the plan and named section before dispatching"
        )

    try:
        relative_path = resource.path.resolve().relative_to(plan_repo)
    except ValueError as exc:
        raise PlanVisibilityError(
            f"project {project!r} mount {docs_dir} is outside its repository "
            f"{plan_repo}"
        ) from exc
    commit = _base_commit(plan_repo, plan_base)
    blob = subprocess.run(
        ["git", "show", f"{commit}:{relative_path.as_posix()}"],
        cwd=str(plan_repo),
        capture_output=True,
        check=False,
    )
    if blob.returncode:
        raise PlanVisibilityError(
            f"plan file {relative_path.as_posix()} is not readable at base "
            f"{plan_base!r}; "
            "commit the plan and named section before dispatching"
        )

    head_commit = _base_commit(plan_repo, "HEAD")
    if commit == head_commit:
        working_bytes = resource.path.read_bytes()
        if working_bytes != blob.stdout:
            raise PlanVisibilityError(
                f"plan file {relative_path.as_posix()} differs from base "
                f"{plan_base!r}; "
                "commit the plan before dispatching"
            )
    base_html = blob.stdout.decode("utf-8", errors="replace")
    if node.section.strip() and not _contains_plan_section(base_html, node.section):
        raise PlanVisibilityError(
            f"plan file {relative_path.as_posix()} does not contain section "
            f"{node.section!r} at base {plan_base!r}; commit the named section "
            "before dispatching"
        )
    return commit


# Roles that read a plan rather than build it. A review of the plan gates only
# the node that changes it, so an investigation, a test and a review itself are
# exempt: demanding a review to write a review is circular, and a test that
# reads a plan changes nothing a review exists to catch.
PLAN_BUILD_EXEMPT_ROLES: frozenset[str] = frozenset({"investigate", "review", "test"})


def _plan_review_exempt(node: TaskNode) -> bool:
    """Whether a node is exempt from the plan-review gate.

    A plan is reviewed before it is *built*, so only the building node is gated.
    The exemption is the same predicate the promotion boundary uses to stop
    reviews of reviews: a node is exempt when its declared role reads rather
    than builds, or when its identity names it the reviewer of some run. The
    composed plan-review dispatch is such a node, and it must be dispatchable
    without a review of its own, so the two conditions share this one predicate.
    """
    if str(node.role or "").strip() in PLAN_BUILD_EXEMPT_ROLES:
        return True
    from reckon.crew.recovery import _is_review_node

    return _is_review_node({"node": {"id": node.id}})


def _plan_review_verdict(enforce: bool, detail: str) -> str | None:
    """Refuse in enforce mode; otherwise return the warning to record.

    Report-only is the shipped default, because the node that dispatches a plan
    review is not built yet and a gate that stopped every build on that account
    would be turned off rather than answered. In report-only mode the dispatch
    proceeds and the same sentence an enforced refusal would carry is recorded
    on the dispatch result as a warning, so the fact is visible without being
    fatal and the wording does not drift between the two modes.
    """
    if enforce:
        raise PlanReviewMissingError(detail)
    return f"plan-review gate in report-only mode — {detail}"


def _uncovered_change_detail(
    uncovered: Iterable[str], changes: Mapping[str, float | None]
) -> str:
    """Name each uncovered unit beside its measured change, for the refusal.

    The measured change is the share of a section's words that differ from the
    review that read it; a unit the measure could not read — a new section, the
    document unit whose digest alone decides — is named without a figure rather
    than given an invented one.
    """
    parts = []
    for unit in sorted(uncovered):
        share = changes.get(unit)
        if share is None:
            parts.append(f"{unit} changed")
        else:
            parts.append(f"{unit} changed by {round(share * 100)}% of its words")
    return f"{'; '.join(parts)}; " if parts else ""


def _refused_store_detail(refusals: Iterable[Mapping[str, Any]]) -> str:
    """Name each delivery the store refused and the reason it recorded.

    A delivery that does not store is otherwise visible only as the gate's
    missing-review symptom; naming it and its reason in the refusal is what
    carries the cause to the reader who meets the symptom.
    """
    parts = [
        f"delivery {item.get('review_run_id') or '?'} did not store: "
        f"{item.get('store_error')}"
        for item in refusals
    ]
    return "; ".join(parts) + "; " if parts else ""


def require_plan_reviewed(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path,
    authority: Mapping[str, Any],
    allow_unreviewed: bool = False,
    enforce: bool = False,
) -> str | None:
    """Judge whether a building dispatch's plan carries an answered review.

    A plan is reviewed before it is built: the gate joins a stored review to the
    plan content by fingerprint rather than by the plan's version integer, so a
    metadata-only write neither demands a review nor orphans one, and an
    authored edit demands a fresh one. Every finding of the review must
    be answered — acted on or declined with a reason — because the findings are
    advisory and the answer is the record; an unanswered finding is an unread
    one, and the gate refuses a plan whose review nobody read.

    ``allow_unreviewed`` is the operational waiver: a broken local review lane
    must not stop every build, so the caller may waive the gate and the waiver
    is recorded on the run that carries it. ``enforce`` selects the mode: when
    false (the default) a plan's missing or unanswered review returns the
    warning to record and the dispatch proceeds; when true the same condition
    raises :class:`PlanReviewMissingError`. ``None`` is returned when there is
    nothing to report.
    """
    if allow_unreviewed or _plan_review_exempt(node):
        return None

    from reckon.crew import plan_review
    from reckon.resources import ResourceCollision, resolve_resource

    plan_data = authority["plan"]
    docs_dir = Path(str(plan_data["docs"])).resolve()
    if plan_data.get("source") == "repository" and (
        not docs_dir.is_dir() or not any(docs_dir.rglob("*.html"))
    ):
        # A repository that has not adopted HTML plans has no reviewable plan
        # document, the same carve-out the visibility check makes.
        return None
    try:
        resource = resolve_resource(
            docs_dir, project, node.plan, "plan", include_archived=False
        )
    except ResourceCollision as exc:
        raise PlanReviewMissingError(
            f"plan {node.plan!r} cannot be resolved in {docs_dir}: {exc}; "
            "commit one unambiguous plan before dispatching"
        ) from exc
    if resource is None:
        # The visibility check refuses an unreadable plan ahead of this gate, so
        # an unresolved resource here has nothing to review.
        return None

    refusals = plan_review.store_delivered_reviews(project, node.plan)
    _records, uncovered, changes = plan_review.review_coverage(
        project, node.plan, plan=resource.path
    )
    record = plan_review.read_plan_review(project, node.plan, plan=resource.path)
    if uncovered or record is None:
        return _plan_review_verdict(
            enforce,
            f"plan {node.plan!r} in project {project!r} carries no stored review "
            f"of the content about to be built; uncovered units: {', '.join(sorted(uncovered))}; "
            f"{_uncovered_change_detail(uncovered, changes)}"
            f"{_refused_store_detail(refusals)}"
            "a plan is reviewed before it is built; compose one with "
            f"`{plan_review.review_invocation(project, node.plan)}`",
        )
    unanswered = plan_review.unanswered_findings(record)
    if unanswered:
        return _plan_review_verdict(
            enforce,
            f"the review of plan {node.plan!r} leaves {len(unanswered)} "
            f"finding(s) unanswered: {', '.join(unanswered)}; answer each by "
            "acting on it or declining it with a reason, as in "
            f"`{plan_review.answer_invocation(project, node.plan, unanswered[0])}`",
        )
    return None


def resolve_dispatch_authority(project: str, repo: str | Path) -> dict[str, Any]:
    """Resolve semantic and write repositories from the registered mounts."""
    try:
        mounts = flight.mounted_project_docs()
    except flight.FlightConfigError as exc:
        raise PlanVisibilityError(str(exc)) from exc
    work_repo = Path(repo).expanduser().resolve()
    if project not in mounts:
        return {
            "plan": {
                "project": project,
                "docs": str(work_repo / "docs"),
                "repository": str(work_repo),
                "source": "repository",
            },
            "write": {
                "projects": [project],
                "repository": str(work_repo),
                "source": "repository",
            },
            "repositories": [str(work_repo)],
        }

    plan_docs = mounts[project]
    if not plan_docs.is_dir():
        raise PlanVisibilityError(
            f"project {project!r} mount {plan_docs} is not a readable directory"
        )
    plan_repo = plan_docs.parent.resolve()
    work_projects = sorted(
        name for name, docs in mounts.items() if docs.parent.resolve() == work_repo
    )
    if not work_projects:
        # Two remedies, because registering is the wrong one for a repository
        # that should not carry Reckon's UI at all — a data-only catalog that is
        # pull-requested to another organisation, say. A refusal naming only the
        # remedy that does not apply pushes the caller out of the pattern
        # entirely, which is how one hand-rolled delegation left an uncommitted
        # edit in a shared repository with nothing recording who made it.
        raise CrewError(
            f"repository {work_repo} is outside the resolved mount authority set. "
            "Either register its project with `reckon sync` before dispatching "
            "writes, or — when carrying Reckon's scaffolding there is "
            "inappropriate — hand-compose the delegation per reckon-build "
            "references/sprint-orchestration.md, whose orchestration contract keeps the worktree, "
            "write fence, manifest and ledger record that a bare subagent has "
            "none of"
        )
    return {
        "plan": {
            "project": project,
            "docs": str(plan_docs),
            "repository": str(plan_repo),
            "source": "mount",
        },
        "write": {
            "projects": work_projects,
            "repository": str(work_repo),
            "source": "mount",
        },
        "repositories": sorted({str(plan_repo), str(work_repo)}),
    }


def resolve_dispatch_ledger_root(authority: Mapping[str, Any]) -> Path:
    """Return the registered repository that owns the dispatch project's ledger."""
    plan = authority.get("plan")
    repository = plan.get("repository") if isinstance(plan, Mapping) else None
    if not repository:
        raise CrewError("dispatch authority does not name a project ledger repository")
    return Path(str(repository)).expanduser().resolve()


def mounted_repository_projects() -> dict[Path, tuple[str, ...]]:
    """Return mounted project identities grouped by repository root."""
    try:
        mounts = flight.mounted_project_docs()
    except flight.FlightConfigError as exc:
        raise PlanVisibilityError(str(exc)) from exc
    grouped: dict[Path, list[str]] = {}
    for project, docs in mounts.items():
        grouped.setdefault(docs.parent.resolve(), []).append(str(project))
    return {
        repository: tuple(sorted(projects)) for repository, projects in grouped.items()
    }


def resolve_scope_repository(
    path: str | Path,
    *,
    base_repository: str | Path,
    repositories: Iterable[str | Path],
) -> Path | None:
    """Resolve a declared path to its most specific containing repository."""
    base = Path(base_repository).expanduser().resolve()
    raw = Path(path).expanduser()
    resolved = (raw if raw.is_absolute() else base / raw).resolve()
    roots = {Path(root).expanduser().resolve() for root in repositories}
    roots.add(base)
    matches = [root for root in roots if resolved.is_relative_to(root)]
    return max(matches, key=lambda root: len(root.parts)) if matches else None


def _require_write_paths_in_repository(
    node: TaskNode, authority: Mapping[str, Any]
) -> None:
    """Confine writes to the worktree or Reckon's durable delivery roots."""
    work_repo = Path(str(authority["write"]["repository"])).resolve()
    roots = delivery_roots()
    for declared in node.write_paths:
        raw = Path(declared).expanduser()
        resolved = (raw if raw.is_absolute() else work_repo / raw).resolve()
        if not resolved.is_relative_to(work_repo) and not any(
            resolved.is_relative_to(root) for root in roots
        ):
            raise CrewError(
                f"write path {declared!r} resolves outside the authorised work "
                f"repository {work_repo} and Reckon delivery directories "
                f"{', '.join(str(root) for root in roots)}; declare a path "
                "inside one of them"
            )


def _agent_configuration(
    backend_name: str, launch_kind: str, backend: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the exact worker configuration persisted on a run record."""

    configuration = {
        "backend": backend_name,
        "launch": launch_kind,
        "model": backend.get("model"),
        "effort": backend.get("effort"),
        "sandbox": backend.get("sandbox"),
    }
    if backend.get("usable_input_window") is not None:
        configuration["usable_input_window"] = backend["usable_input_window"]
    if backend.get("effective_input_window") is not None:
        configuration["effective_input_window"] = backend["effective_input_window"]
    return configuration


def _session_member_id(session: str) -> str:
    """Derive the private roster identity owned by one dispatching session."""
    digest = hashlib.sha256(str(session).encode()).hexdigest()[:20]
    return f"session-{digest}"


def _disposable_member_id(run_id: str) -> str:
    """Derive the per-run identity an unnamed dispatch carries.

    A dispatch that names no member is disposable, so its identity is minted
    from the run it belongs to rather than from the dispatching session: two
    unnamed dispatches of one coordinator are two identities, so neither can
    hold the other in flight.
    """
    return f"disposable-{run_id}"


def _register_session_member(
    project: str,
    member_id: str,
    *,
    backend: str,
    role: str,
    root: Path,
    attempts: int = 12,
) -> dict[str, Any]:
    """Provision a session-owned member, committing the registration it writes.

    A dispatch that names no member registers one under its own session so the
    run has a roster identity. The registration is written here, so the commit
    is requested here rather than left to ride whatever unrelated commit comes
    next: an uncommitted roster row is visible only in the checkout that wrote
    it, which is a state a declared member's registration never sits in.

    A commit that fails is surfaced rather than retried away. The retry would
    find the row its own failed attempt left behind and return it as though the
    registration were recorded. A registration whose commit failed and one
    whose write never happened would otherwise leave the caller without a
    member, and only the first leaves a roster no other checkout can see.
    """
    # Hold one repository lock through both the ledger write and its git commit.
    # Distinct member ids still share the same roster and git index.
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    with _pointer_lock(f"roster-registration-{identity}"):
        last: ledger.LedgerError | None = None
        for _attempt in range(max(1, attempts)):
            existing = ledger.member(project, member_id, root=root)
            if existing is not None:
                return existing
            try:
                return ledger.register_member(
                    project,
                    member_id,
                    harness=backend,
                    role=role,
                    root=root,
                    commit=True,
                )
            except ledger.LedgerError as exc:
                last = exc
                if ledger.member(project, member_id, root=root) is not None:
                    raise CrewError(
                        f"session member {member_id!r} was written to the roster and "
                        f"not committed, so no other checkout can see the "
                        f"registration: {exc}"
                    ) from exc
        raise CrewError(
            f"could not provision session member {member_id!r} after {attempts} "
            f"attempts: {last}"
        )


def _parse_utc_timestamp(value: Any) -> datetime | None:
    """Return an aware UTC timestamp, or None for missing or malformed input."""
    if not value:
        return None
    return parse_utc(str(value))


def reap_idle_session_members(
    project: str,
    *,
    root: str | Path | None = None,
    idle_window: str = DEFAULT_MEMBER_IDLE_WINDOW,
    now: datetime | None = None,
    attempts: int = 12,
) -> dict[str, Any]:
    """Remove idle session-owned roster rows while retaining their run history.

    Completed records are the durable source of worker session ids. The roster
    is only their reusable index, so deleting an idle row must never rewrite a
    run. A non-terminal pointer protects its member regardless of age.
    """
    window_seconds = parse_duration(idle_window)
    observed_at = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    repo_root = Path(root).resolve() if root is not None else None
    pointers = [
        pointer
        for pointer in list_live(project=project)
        if repo_root is None
        or Path(str(pointer.get("repo") or "")).resolve() == repo_root
    ]
    protected = {
        str(pointer.get("member"))
        for pointer in pointers
        if pointer.get("member")
        and str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
    }

    reaped: list[str] = []
    for attempt in range(max(1, attempts)):
        data, version = ledger.load(project, root=root)
        last_dispatch: dict[str, datetime] = {}
        for record in [*data["runs"], *pointers]:
            member_id = str(record.get("member") or "")
            stamp = _parse_utc_timestamp(
                record.get("dispatched_at") or record.get("created_at")
            )
            if (
                member_id
                and stamp
                and (member_id not in last_dispatch or stamp > last_dispatch[member_id])
            ):
                last_dispatch[member_id] = stamp
        candidates = []
        for entry in data["members"]:
            member_id = str(entry.get("id") or "")
            if not member_id.startswith("session-") or member_id in protected:
                continue
            latest = last_dispatch.get(member_id) or _parse_utc_timestamp(
                entry.get("created")
            )
            if latest is None:
                continue
            if (observed_at - latest).total_seconds() >= window_seconds:
                candidates.append(member_id)
        if not candidates:
            return {"reaped": [], "idle_window": idle_window}

        # Close the observation-to-write gap for workers that became live while
        # the versioned roster update was being prepared.
        newly_protected = {
            str(pointer.get("member"))
            for pointer in list_live(project=project)
            if pointer.get("member")
            and str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
            and (
                repo_root is None
                or Path(str(pointer.get("repo") or "")).resolve() == repo_root
            )
        }
        reaped = sorted(set(candidates) - newly_protected)
        if not reaped:
            return {"reaped": [], "idle_window": idle_window}
        data["members"] = [
            entry for entry in data["members"] if str(entry.get("id")) not in reaped
        ]
        try:
            # The reap is the one caller entitled to drop roster members: this
            # function computes `reaped` above and the removal is its whole
            # purpose, so the intent is declared here rather than inferred from
            # a member count. Every other caller must pass no flag and have its
            # unrequested decrease refused.
            #
            # The removal is also committed here, for the same reason: a retire
            # that left the roster dirty would be absorbed by the next roster
            # registration, and a registration that refuses to commit on a
            # dirty roster would then be unable to provision its member at all.
            ledger.write(
                project,
                data,
                version,
                root=root,
                allow_member_removal=True,
                commit=True,
            )
        except ledger.LedgerError:
            if attempt + 1 >= max(1, attempts):
                raise CrewError(
                    f"could not reap idle session members after {attempts} attempts"
                )
            continue
        return {"reaped": reaped, "idle_window": idle_window}
    return {"reaped": [], "idle_window": idle_window}


def _tokens_for_bytes(byte_count: int) -> int:
    """Return the repository's conservative token estimate for UTF-8 bytes."""

    return math.ceil(max(0, byte_count) / _CONTEXT_BYTES_PER_TOKEN)


def _context_agent(backend: Mapping[str, Any]) -> str:
    """Return the instruction layout used by the resolved worker harness."""

    if backend.get("launch") != "cli":
        return "codex"
    try:
        return _backends.dialect_for(backend).name
    except _backends.BackendError:
        return "codex"


def _standing_context_input(
    repo: Path, backend: Mapping[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Measure the instruction files loaded before repository work begins."""

    home = Path(os.environ.get("HOME") or Path.home()).expanduser()
    # The manifest reads the instruction chain, whose repository lookup shells
    # out to git, on every cold process. It is a pure function of the files it
    # names, so the cached manifest is reused across processes while those files
    # stand, and every candidate sharing an agent layout reuses one build.
    manifest = agent_context.cached_context_manifest(
        agent_context.ContextRequest(
            target=repo,
            user_home=home,
            agent=_context_agent(backend),
        )
    )
    records = list(manifest["instructions"]["effective_chain"])
    canonical = manifest.get("canonical_policy") or {}
    loaded = {
        str(item.get("resolved_path") or item.get("path") or "") for item in records
    }
    canonical_path = str(canonical.get("resolved_path") or canonical.get("path") or "")
    if canonical.get("readable") and canonical_path not in loaded:
        records.append({**canonical, "scope": "user", "loaded_via": canonical_path})

    inputs = [
        {
            "path": str(item.get("path") or ""),
            "bytes": int(item.get("bytes") or 0),
            "estimated_tokens": _tokens_for_bytes(int(item.get("bytes") or 0)),
        }
        for item in records
        if item.get("readable")
    ]
    measured_tokens = sum(item["estimated_tokens"] for item in inputs)
    return max(measured_tokens, _MEASURED_LAUNCH_CONTEXT_FLOOR_TOKENS), {
        "calculated_tokens": measured_tokens,
        "floor_tokens": _MEASURED_LAUNCH_CONTEXT_FLOOR_TOKENS,
        "effective_tokens": max(measured_tokens, _MEASURED_LAUNCH_CONTEXT_FLOOR_TOKENS),
        "token_estimator": f"ceil(utf8-bytes/{_CONTEXT_BYTES_PER_TOKEN})",
        "files": inputs,
    }


def _declared_input_files(node: TaskNode) -> list[str]:
    """Extract repository files the node's brief declares as its inputs.

    A path a brief merely names is not a read. The estimate once charged every
    resolvable path in the prose at full file load, so a done-when that named a
    large ledger made a one-worker-hour node estimate millions of tokens and be refused
    against its window. A path counts only when the clause naming it also
    declares it an input; a clause that only mentions the path is a reference,
    not a load. A clause that negates the read -- telling the worker a path is
    excluded -- names the same keywords while asserting the opposite, so the
    paths its own exclusion phrase governs are withheld.
    """

    text = f"{node.goal}\n{node.done_when}"
    declared: set[str] = set()
    for clause in re.split(r"[;\n]|(?<=\.)\s+", text):
        lowered = clause.lower()
        if "input" not in lowered or "declar" not in lowered:
            continue
        declared.update(_paths_a_clause_charges(clause))
    return sorted(declared)


def _paths_a_clause_charges(clause: str) -> list[str]:
    """Return the paths one declaration clause's own verbs put in charge.

    A verb governs the paths that follow it until the next verb, so a mixed
    clause keeps the path its declaration verb names -- the reason the clause
    was read as a declaration at all -- while the path its exclusion verb
    names is withheld.
    """

    verbs = list(_GOVERNING_VERB.finditer(clause))
    charged: list[str] = []
    for named in _NAMED_REPOSITORY_FILE.finditer(clause):
        preceding = [verb for verb in verbs if verb.end() <= named.start()]
        if preceding and preceding[-1].lastgroup == "exclude":
            continue
        charged.append(named.group(0))
    return charged


def _context_file_inputs(
    repo: Path, node: TaskNode, authority: Mapping[str, Any] | None
) -> tuple[int, dict[str, list[dict[str, Any]]]]:
    """Measure unique repository reads, retaining dispatcher-grant provenance."""

    counted: set[Path] = set()
    granted_paths: set[Path] = set()
    plan = authority.get("plan") if isinstance(authority, Mapping) else None
    project = plan.get("project") if isinstance(plan, Mapping) else None
    if project:
        # ``dispatch`` imports this module to resolve a plan, so this stays
        # local to keep the routing import graph acyclic. The set is the same
        # default-scope function dispatch grants from, so a dispatcher-owned
        # exemption cannot drift from the paths dispatch actually declares.
        from reckon.crew.dispatch import _landing_fragment_paths

        granted_paths = _landing_fragment_paths(node, authority=authority)

    def describe(raw: str, *, declared_read: bool) -> dict[str, Any]:
        candidate = Path(raw).expanduser()
        resolved = (
            candidate if candidate.is_absolute() else repo / candidate
        ).resolve()
        record: dict[str, Any] = {
            "declared": raw,
            "path": str(resolved),
            "bytes": 0,
            "estimated_tokens": 0,
            "counted": False,
            "provenance": "declared",
            "status": "missing",
        }
        if not declared_read and resolved in granted_paths:
            record["provenance"] = "granted"
        if not resolved.is_relative_to(repo):
            record["status"] = "outside-repository"
            return record
        try:
            is_file = resolved.is_file()
            byte_count = resolved.stat().st_size if is_file else 0
        except OSError:
            record["status"] = "unreadable"
            return record
        if not is_file:
            record["status"] = "directory" if resolved.is_dir() else "missing"
            return record
        tokens = _tokens_for_bytes(byte_count)
        chargeable = record["provenance"] == "declared"
        record.update(
            {
                "bytes": byte_count,
                "estimated_tokens": tokens,
                "counted": chargeable and resolved not in counted,
                "status": "file",
            }
        )
        if chargeable:
            counted.add(resolved)
        return record

    write_paths = [
        describe(str(path), declared_read=False) for path in node.write_paths
    ]
    named_files = [
        describe(path, declared_read=True) for path in _declared_input_files(node)
    ]
    all_inputs = [*write_paths, *named_files]
    file_tokens = sum(
        int(item["estimated_tokens"]) for item in all_inputs if item["counted"]
    )
    return file_tokens, {"write_paths": write_paths, "named_files": named_files}


def _context_fit_verdict(
    *, resolution: DispatchPlan, repo: Path
) -> dict[str, Any] | None:
    """Compare one node's deterministic context estimate with its backend window.

    The window that gates the refusal is the smaller of the declared lane window
    and the effective boundary the backend records for its lane. The two are
    different quantities: the declared window is what the configuration claims
    the endpoint accepts, while the effective boundary is the lowest input a
    recorded refusal shows the endpoint actually rejected
    (:func:`reckon.crew.context_budget.refusal_census`). A lane whose endpoint
    refuses below its declared figure would otherwise pass this check and then
    die at the endpoint with three records and no deliverable, which is the
    loss this comparison exists to prevent.

    Either figure may be absent and neither absence is a zero. A lane that
    declares no window is unbounded, so its recorded boundary alone gates it; a
    lane with no recorded boundary keeps its declared window.
    """

    declared_window = resolution.backend_settings.get("usable_input_window")
    effective_boundary = resolution.backend_settings.get("effective_input_window")
    if declared_window is None and effective_boundary is None:
        return None
    try:
        window_tokens = int(declared_window) if declared_window is not None else None
    except (TypeError, ValueError) as exc:
        raise CrewError(
            f"backend {resolution.backend!r} declares a non-integer usable input "
            f"window {declared_window!r}"
        ) from exc
    try:
        boundary_tokens = (
            int(effective_boundary) if effective_boundary is not None else window_tokens
        )
    except (TypeError, ValueError) as exc:
        raise CrewError(
            f"backend {resolution.backend!r} declares a non-integer effective input "
            f"window {effective_boundary!r}"
        ) from exc
    if window_tokens is not None and window_tokens <= 0:
        raise CrewError(
            f"backend {resolution.backend!r} declares a non-positive usable input "
            f"window {window_tokens}"
        )
    if boundary_tokens is None or boundary_tokens <= 0:
        raise CrewError(
            f"backend {resolution.backend!r} declares a non-positive effective input "
            f"window {boundary_tokens}"
        )
    # An absent declared window means unbounded, never zero: a lane that states
    # no ceiling keeps whatever boundary its own recordings establish, rather
    # than being refused because the window it never declared reads as nothing.
    gating_tokens = (
        boundary_tokens
        if window_tokens is None
        else min(window_tokens, boundary_tokens)
    )
    narrows = window_tokens is not None and boundary_tokens < window_tokens

    standing_tokens, standing = _standing_context_input(
        repo, resolution.backend_settings
    )
    file_tokens, files = _context_file_inputs(
        repo, resolution.node, resolution.authority
    )
    estimated_tokens = standing_tokens + file_tokens
    shortfall_tokens = max(0, estimated_tokens - gating_tokens)
    if shortfall_tokens == 0:
        reason = "within-context-window"
    elif narrows:
        # Both figures are named, because the remedy differs: an estimate inside
        # this band is refused by reckon for a lane boundary the configuration
        # does not state, so a reader who sees only the declared window would
        # look for the fault in the node size.
        reason = (
            f"context-window-exceeded: estimated {estimated_tokens} tokens is "
            f"above the effective boundary {boundary_tokens} recorded for backend "
            f"{resolution.backend!r}, whose declared window is {window_tokens}"
        )
    elif window_tokens is None:
        reason = (
            f"context-window-exceeded: estimated {estimated_tokens} tokens is "
            f"above the effective boundary {boundary_tokens} recorded for backend "
            f"{resolution.backend!r}, which declares no window of its own"
        )
    else:
        reason = "context-window-exceeded"
    return {
        "allowed": shortfall_tokens == 0,
        "estimated_tokens": estimated_tokens,
        "window_tokens": gating_tokens,
        "declared_window_tokens": window_tokens,
        "effective_boundary_tokens": (
            boundary_tokens if narrows or window_tokens is None else None
        ),
        "shortfall_tokens": shortfall_tokens,
        "reason": reason,
        "inputs": {
            "standing_instructions": standing,
            "repository_files": files,
            "repository_file_tokens": file_tokens,
        },
    }


def _context_contributors(
    context_fit: Mapping[str, Any], limit: int = 3
) -> list[dict[str, Any]]:
    """Return the counted inputs that contribute the most estimated tokens."""

    inputs = context_fit.get("inputs") if isinstance(context_fit, Mapping) else None
    inputs = inputs if isinstance(inputs, Mapping) else {}
    records: list[dict[str, Any]] = []
    standing = inputs.get("standing_instructions")
    standing = standing if isinstance(standing, Mapping) else {}
    for item in standing.get("files") or ():
        if isinstance(item, Mapping):
            records.append(
                {
                    "path": str(item.get("path") or ""),
                    "estimated_tokens": int(item.get("estimated_tokens") or 0),
                }
            )
    repository = inputs.get("repository_files")
    repository = repository if isinstance(repository, Mapping) else {}
    for group in ("write_paths", "named_files"):
        for item in repository.get(group) or ():
            if isinstance(item, Mapping) and item.get("counted"):
                records.append(
                    {
                        "path": str(item.get("path") or ""),
                        "estimated_tokens": int(item.get("estimated_tokens") or 0),
                    }
                )
    records = [record for record in records if record["estimated_tokens"] > 0]
    records.sort(key=lambda record: (-record["estimated_tokens"], record["path"]))
    return records[:limit]


def _context_refusal_detail(context_fit: Mapping[str, Any]) -> str:
    """Render the top-level sentence a context refusal answers with.

    The refusal payload carries the informative reason only under a nested
    ``context`` field, so a caller reading the refusal's own top level sees an
    empty detail and a bare reason code. This names the figures a reader needs
    to act -- the estimate, the window it exceeded and the inputs that
    dominated the estimate -- at the level the refusal answers on."""

    contributors = _context_contributors(context_fit)
    if contributors:
        rendered = ", ".join(
            f"{record['path']} ({record['estimated_tokens']} tokens)"
            for record in contributors
        )
        dominant = f"largest contributing files: {rendered}"
    else:
        dominant = (
            "no repository file dominates the estimate; the standing instruction "
            "context alone exceeds the window"
        )
    return (
        f"context-window-exceeded: estimated {context_fit['estimated_tokens']} "
        f"usable input tokens against a {context_fit['window_tokens']} token "
        f"window (shortfall {context_fit['shortfall_tokens']} tokens); {dominant}"
    )


def _estimated_hours(
    repo: Path, project: str, node: TaskNode
) -> tuple[float | None, str]:
    """Return neutral hours and whether the node or plan supplied them."""

    try:
        node_hours = float(node.estimated_hours)
    except (TypeError, ValueError):
        node_hours = 0.0
    if math.isfinite(node_hours) and node_hours > 0:
        return node_hours, "node"

    if not node.plan.strip():
        return None, "unavailable"

    from concurrent.futures import ThreadPoolExecutor
    from pathlib import PurePosixPath

    from reckon import resources

    docs = repo / "docs"
    paths = []
    for path in docs.rglob("*.html"):
        relative = PurePosixPath(path.relative_to(docs).as_posix())
        if (
            resources._is_evidence_fragment(relative)
            or path.name in resources.NON_RESOURCE_FILES
            or any(part in resources.INFRA_DIRS for part in relative.parts[:-1])
        ):
            continue
        try:
            kind, archived, _legacy = resources._path_context(relative)
        except resources.ResourceCollision:
            continue
        if not archived and kind in {None, "plan"}:
            paths.append(path)
    paths.sort()
    with ThreadPoolExecutor(max_workers=8) as pool:
        stamps = list(pool.map(ledger._file_identity, paths))
    stamp = [
        (str(path), identity) for path, identity in zip(paths, stamps, strict=True)
    ]
    identity = hashlib.sha256(
        f"{repo.resolve()}:{project}:{node.plan}".encode()
    ).hexdigest()

    def build() -> list[Any]:
        resource = resources.resolve_resource(
            docs, project, node.plan, "plan", include_archived=False
        )
        if resource is None:
            return [None, "unavailable"]
        value = _plan_html.parse_meta(resource.path).get("effort_hours")
        try:
            hours = float(value)
        except (TypeError, ValueError):
            return [None, "unavailable"]
        return (
            [hours, "plan-fallback"]
            if math.isfinite(hours) and hours > 0
            else [None, "unavailable"]
        )

    value = capabilities.cached_pick_input(f"plan-estimate-{identity}", stamp, build)
    return value[0], value[1]


def _measured_horizon_hours(value: Any) -> float | None:
    """Return a measured competence horizon, or ``None`` when none was measured.

    A horizon the configuration never recorded is not the number zero. Coercing
    an absent or unreadable figure to ``0.0`` makes an unmeasured configuration
    compare equal to a measured-zero one, so the verdict refuses a node against
    a horizon that was never observed. Keeping ``None`` distinct lets such a
    node through while a genuinely measured horizon — including a measured zero
    — still bounds it.
    """
    if value is None:
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if math.isfinite(hours) else None


def _verdict_input_stamp(project: str, repo: Path) -> dict[str, Any]:
    """File-derived revision shared with the incremental ledger reader."""
    return {
        "capabilities": capabilities.file_stamp(capabilities.capabilities_path()),
        "ledger": ledger.input_stamp(project, repo),
    }


def shared_verdict_inputs(project: str, repo: Path) -> dict[str, Any]:
    """Load the inputs one pick's verdicts share across every candidate.

    The capability cache and the project's ledger freshness key do not depend on
    the candidate, but computing the freshness key reloads the whole project
    ledger. A pick judges every configured backend, so reading it once per pick
    instead of once per candidate turns a per-candidate ledger read into one.

    Both inputs are pure functions of files, so the result is cached across
    processes under a stamp of those files: the second process skips the ledger
    walk, and a changed capability cache or ledger is a miss.
    """

    def build() -> dict[str, Any]:
        cache = capabilities.load_capabilities()
        return {
            "capability_cache": cache,
            "cache_status": capabilities.project_cache_status(
                cache, project, root=repo
            ),
        }

    stamp = _verdict_input_stamp(project, repo)
    if stamp["ledger"] is None:
        return build()
    return capabilities.cached_pick_input(
        f"verdict-inputs-{project}",
        stamp,
        build,
    )


def _competence_verdict(
    *,
    resolution: DispatchPlan,
    project: str,
    repo: Path,
    verdict_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a neutral node estimate with a neutral-size success horizon."""

    agent = _agent_configuration(
        resolution.backend, resolution.launch, resolution.backend_settings
    )
    key = calibration_configuration_key({"agent": agent})
    plan_repo = repo
    if resolution.authority is not None:
        plan_repo = Path(resolution.authority["plan"]["repository"])
    if verdict_inputs is not None and "node_estimate" in verdict_inputs:
        estimated_hours, estimate_provenance = verdict_inputs["node_estimate"]
    else:
        estimated_hours, estimate_provenance = _estimated_hours(
            plan_repo, project, resolution.node
        )
    if verdict_inputs is None:
        cache = capabilities.load_capabilities()
        cache_status = capabilities.project_cache_status(cache, project, root=repo)
    else:
        cache = verdict_inputs["capability_cache"]
        cache_status = verdict_inputs["cache_status"]
    configuration = next(
        (
            item
            for item in cache.get("configurations", [])
            if isinstance(item, Mapping) and item.get("key") == key
        ),
        None,
    )
    horizon = configuration.get("competence_horizon_hours") if configuration else None
    horizon_hours = _measured_horizon_hours(horizon)

    verdict: dict[str, Any] = {
        "allowed": True,
        "agent_key": key,
        "estimated_hours": estimated_hours,
        "estimate_provenance": estimate_provenance,
        "cache_status": cache_status,
        "reason": "no-measured-horizon",
    }

    def with_context_fit() -> dict[str, Any]:
        context_fit = _context_fit_verdict(resolution=resolution, repo=repo)
        if context_fit is None:
            return verdict
        verdict["context"] = context_fit
        if context_fit["allowed"]:
            return verdict
        verdict.update(
            {
                "allowed": False,
                "reason": "context-window-exceeded",
                "detail": _context_refusal_detail(context_fit),
                "estimated_tokens": context_fit["estimated_tokens"],
                "window_tokens": context_fit["window_tokens"],
                "shortfall_tokens": context_fit["shortfall_tokens"],
                "recommendation": format_refusal(
                    "D08",
                    "split the node or route it to a backend with at least "
                    f"{context_fit['estimated_tokens']} usable input tokens",
                ),
            }
        )
        # ``CompetenceLimit`` is the established exit-5 envelope. Its legacy
        # message renders neutral-hour fields, while callers consume this
        # structured verdict; keep the constructor total until that message is
        # made dimension-aware at its owning boundary.
        verdict.setdefault("competence_horizon_hours", 0.0)
        verdict.setdefault("target_size_hours", 0.0)
        return verdict

    if cache_status == "stale":
        verdict["reason"] = "stale-capability-cache"
        return with_context_fit()
    if horizon_hours is None:
        return with_context_fit()
    if estimated_hours is None:
        verdict["reason"] = "no-estimated-hours"
        return with_context_fit()

    speed = configuration.get("speed") if configuration else None
    try:
        speed_factor = float(speed.get("mean")) if isinstance(speed, Mapping) else 1.0
    except (TypeError, ValueError):
        speed_factor = 1.0
    if not math.isfinite(speed_factor) or speed_factor <= 0:
        speed_factor = 1.0

    # Both the node estimate and horizon are neutral estimated hours.  Speed is
    # descriptive here: applying it to only one side recreates the unit defect.
    target_size = horizon_hours
    verdict.update(
        {
            "allowed": estimated_hours <= horizon_hours,
            "compared_hours": round(estimated_hours, 6),
            "comparison_unit": "neutral-estimate-hours",
            "competence_horizon_hours": round(horizon_hours, 6),
            "reason": "within-competence-horizon"
            if estimated_hours <= horizon_hours
            else "competence-horizon-exceeded",
            "speed_factor": round(speed_factor, 6),
            "speed_direction": "neutral-estimate-hours-per-actual-worker-hour",
            "target_size_hours": round(target_size, 6),
        }
    )
    if not verdict["allowed"]:
        verdict["recommendation"] = format_refusal(
            "D08",
            f"split into nodes no larger than {verdict['target_size_hours']} "
            "worker-hours for this agent configuration",
        )
    return with_context_fit()
