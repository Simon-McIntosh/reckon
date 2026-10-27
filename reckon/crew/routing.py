from __future__ import annotations

import hashlib
import json
import locale
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

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
from reckon._store import CorruptEnvelopeError
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
    repository_identity,
)
from reckon.crew.refusals import format_refusal
from reckon.crew.runs import (
    _pointer_lock,
    _process_start_time,  # noqa: F401 - re-exported for callers that read it from here
    delivery_roots,
    list_live,
    runs_dir,
)
from reckon.crew.runs import (
    run_dir as _run_dir,
)

if TYPE_CHECKING:
    from reckon.crew.dispatch import DispatchPlan

# ── Routing ─────────────────────────────────────────────────────────────────

_CONTEXT_BYTES_PER_TOKEN = 3.5
# A regenerated write path is overwritten wholesale rather than read, so the
# census charges it one fixed reference cost instead of its full size. The
# figure is the byte charge of a small module (a few kilobytes), which keeps a
# regenerated node's estimate independent of the generated record's size while
# still pricing the brief that names the work. The value is flat by design: a
# size-dependent charge would reintroduce the cost this path exists to remove.
_REGENERATED_PATH_REFERENCE_TOKENS = 4_000
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


def _lane_model_settings(
    config: Mapping[str, Any], backend: Mapping[str, Any]
) -> Mapping[str, Any]:
    """The keys the selected lane model declares, for re-application.

    A backend that is not a lane model carries no ``lane`` or ``model_key``, so
    nothing is re-applied and the role overlay stands.
    """
    lane_name = backend.get("lane")
    model_key = backend.get("model_key")
    if not lane_name or not model_key:
        return {}
    lane = (config.get("lanes") or {}).get(str(lane_name)) or {}
    models = lane.get("models") or {}
    if not isinstance(models, Mapping):
        return {}
    model = models.get(str(model_key))
    return model if isinstance(model, Mapping) else {}


def _effective_backend(
    config: Mapping[str, Any],
    backend_name: str,
    overlay: Mapping[str, Any],
    level_overlay: Mapping[str, Any],
    class_overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a role's overlay onto one named backend's own settings.

    A selected lane model's own declared keys are re-applied last, so the effort
    and time budget a model declares outrank the role, spec-level and class
    overlays. A backend that is not a lane model keeps the overlay's value.
    """
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
    declared = _lane_model_settings(config, backend)
    for key in flight.LANE_MODEL_KEYS:
        value = declared.get(key)
        if value is not None:
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


def _config_with_effective_budget(
    config: Mapping[str, Any] | None,
    group: str | None,
    *,
    readings: Sequence[Mapping[str, Any]] | None,
    now: datetime | None,
    session: str | None,
) -> Mapping[str, Any] | None:
    """A configuration whose budget block is the one in force for ``group``.

    :func:`reckon.budget.policy` reads ``config["budget"]`` itself, so a lift's
    released reserves reach it only through a substituted block; a group the
    resolver names ``None`` has no wallet, so the configuration is returned
    unchanged.
    """
    if group is None:
        return config
    from reckon.crew import budget_group as budget_group_module

    block = budget_group_module.effective_block(
        config, str(group), readings=readings, now=now, session=session
    )
    return {**(config or {}), "budget": block}


def _budget_verdict(
    *,
    project: str,
    root: str | Path | None,
    config: Mapping[str, Any] | None,
    backend_name: str,
    backend: Mapping[str, Any],
    purpose: str,
    budget_state: Mapping[str, Any] | None = None,
    group: str | None = None,
    readings: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Judge one backend's headroom for one purpose.

    Imported here rather than at module scope because the budget module reads run
    records through this one; deferring the import to call time keeps that a
    one-way dependency instead of a cycle.

    The judged backend's declared wallet is resolved here, and the group's
    effective budget block — a lift in force releases the resume and
    coordinator reserves — is handed to :func:`reckon.budget.policy`, which
    reads ``config["budget"]`` itself and so must receive the substituted
    block. A group no lift covers is handed the configuration unchanged.
    """
    from reckon import budget as budget_module

    from reckon.crew import budget_group as budget_group_module

    wallet = group or budget_group_module.declared_group_for(config, backend_name)
    judged_config = _config_with_effective_budget(
        config, wallet, readings=readings, now=now, session=session
    )
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
    verdict = budget_module.decide(
        state, budget_module.policy(judged_config), purpose=purpose
    )
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


def resolved_grace_multiple(config: Mapping[str, Any] | None) -> float | None:
    """Return the grace multiple past a budget, or nothing when unusable.

    The multiple is the ratio between an attempt's deadline and the hard stop
    the watchdog enforces. An absent key resolves to ``1.0`` — the hard stop is
    the deadline — and a non-numeric value resolves to nothing, which leaves
    the watchdog inert and the prompt offering no continuation. Both readers
    call this one owner, so the instant they agree on cannot drift.
    """
    value = ((config or {}).get("fences") or {}).get("budget_grace_multiple")
    if value is None:
        return 1.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _worktree_registrations(repo: Path) -> list[dict[str, Any]]:
    """One record per registered worktree, from a single porcelain read.

    Each record carries the worktree's resolved path and whether git holds a
    lock on it, so a caller that needs either does not parse the listing again.
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            current = {
                "path": Path(line.removeprefix("worktree ")).resolve(),
                "locked": False,
            }
            records.append(current)
        elif current is not None and line.startswith("locked"):
            current["locked"] = True
    return records


def _registered_worktrees(repo: Path) -> list[Path]:
    return [record["path"] for record in _worktree_registrations(repo)]


def _run_directory_extractions(
    runs_root: Path, run_id: str | None = None
) -> list[Path]:
    """Return the git-less trees a worker left under the crew runs root.

    Workers keep two kinds of source tree in a run directory: registered
    worktrees, and plain extractions made with ``git archive`` that carry no
    git directory. The shapes and names both appear in are stated once, by
    :func:`reckon.crew.fleet_supervisor.run_directory_source_trees`, so the
    extraction report and the gc run-file walk cannot drift apart. A candidate
    that holds a git directory is a real worktree that the worktree registry
    already reports, so only the git-less ones are returned here — they are
    exactly the trees gc would otherwise never see. Each run directory is
    scanned directly; the walk never descends a whole run tree.

    A caller that names one run scans only that run's own directory, so a
    pass confined to a single run does not walk every run directory under the
    store. A caller that names no run keeps scanning them all.
    """
    from reckon.crew.fleet_supervisor import (
        carries_git_directory,
        run_directory_source_trees,
    )

    if not runs_root.is_dir():
        return []
    if run_id:
        run_dirs = [runs_root / run_id]
    else:
        run_dirs = sorted(
            path
            for path in runs_root.iterdir()
            if path.is_dir() and path.name != WORKTREE_TRASH_DIRNAME
        )
    found: list[Path] = []
    for run_dir in run_dirs:
        for candidate in run_directory_source_trees(run_dir):
            if carries_git_directory(candidate) or candidate in found:
                continue
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
    an ancestor, or the comparison could not run — and the sibling
    ``commits_unmeasured_reason`` names which. A measured row keeps both lists
    even when they are empty. A clean tree whose head is unreachable is
    measured as well as a dirty one: the commits a deliberately abandoned tree
    carries are what an explicit pin archives, and that list cannot be read off
    the working-tree status.
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
    if release_residue and not reachable:
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

    The token names the candidate backend and the run's recorded unique
    component, so repeated arms on one backend retain separate worktrees, and
    never the primary run id: this token is the worktree path shown in the
    shadow's composed prompt, and naming the primary there would hand the shadow
    the identity of the run it is measuring. ``primary_run_id`` is kept for
    signature compatibility with the dispatch caller and is deliberately unused;
    records written before the token dropped the primary are resolved by
    :func:`_shadow_worktree_records` under their own legacy naming.
    """
    stem = f"shadow-{candidate_backend}" if candidate_backend else "shadow"
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
        # A shadow's worktree session no longer names its primary: a current run
        # builds its token from the candidate backend and its component alone. A
        # record committed before that change still carries the primary in its
        # path, and the ledger row does not record which naming produced it, so
        # every naming the record could have used is resolved. A record that
        # predates the candidate-named path (or never named one) still resolves
        # its single legacy worktree; only one candidate could have produced a
        # shadow before the candidate entered the path.
        sessions = {shadow_worktree_session(primary_run_id, candidate, component)}
        if candidate:
            legacy = f"shadow-{primary_run_id}-{candidate}"
            sessions.add(f"{legacy}-{component}" if component else legacy)
        else:
            sessions.add(f"shadow-{primary_run_id}")
        for session in sessions:
            for root in roots:
                result[(root / session / node).resolve()] = record
    return result


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


def _retained_worktree(record: Mapping[str, Any]) -> Path | None:
    """The resolved path a record's retention names, or None when it names none."""
    retention = record.get("worktree_retention")
    if isinstance(retention, Mapping):
        value = str(retention.get("worktree") or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return None


def _node_worktree_identity(record: Mapping[str, Any]) -> tuple[str, str]:
    """The node id and session a record names a tree by."""
    node = record.get("node")
    node_id = (
        str(node.get("id") or "") if isinstance(node, Mapping) else str(node or "")
    )
    return node_id, str(record.get("session") or "").strip()


def record_names_worktree(record: Mapping[str, Any], worktree: Path) -> bool:
    """Whether a run's record names this retained tree.

    Two fields name a tree: the path a promotion retained, and the node id the
    tree's directory is named for. The obligations view and ``crew gc --run``
    resolve a run's tree from this one rule, so every tree an obligation names
    is one its remedy can reach. A tree lives at ``<root>/<session>/<node id>``,
    so the node id names a tree only within the run's own session directory; a
    record that carries no session keeps the node-id-only rule. Without the
    session the node id alone would resolve to whichever same-named tree
    registers first, which may be a peer session's.
    """
    retained = _retained_worktree(record)
    if retained is not None and retained == worktree:
        return True
    node_id, session = _node_worktree_identity(record)
    if not node_id or worktree.name != node_id:
        return False
    return not session or worktree.parent.name == session


class WorktreeRecordIndex:
    """Run records filed by the trees they name, so each tree is one lookup.

    ``record_names_worktree`` is the one rule for what names a tree, and this
    index is built from the same key derivation rather than restating it, so an
    index answer and the rule cannot disagree. Each record is filed once under
    the retained path it carries and once under the node id it names, and a
    tree is matched by consulting those buckets instead of comparing every
    record against it: the work is proportional to records plus trees rather
    than to their product. A retained path is resolved once, when its record is
    filed.
    """

    def __init__(self) -> None:
        self._by_path: dict[Path, list[tuple[int, Mapping[str, Any]]]] = {}
        self._by_session_node: dict[
            tuple[str, str], list[tuple[int, Mapping[str, Any]]]
        ] = {}
        self._by_node: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
        self._filed = 0

    def add(self, record: Mapping[str, Any]) -> None:
        """File one record under every key by which it names a tree."""
        position = self._filed
        self._filed += 1
        retained = _retained_worktree(record)
        if retained is not None:
            self._by_path.setdefault(retained, []).append((position, record))
        node_id, session = _node_worktree_identity(record)
        if not node_id:
            return
        if session:
            self._by_session_node.setdefault((session, node_id), []).append(
                (position, record)
            )
        else:
            self._by_node.setdefault(node_id, []).append((position, record))

    def records_naming(self, worktree: Path) -> list[Mapping[str, Any]]:
        """The filed records that name ``worktree``, each once, in filing order.

        ``worktree`` is the resolved path, as every caller resolves one; a path
        the record retained is compared resolved, and the node id is matched
        against the tree's own directories. A record naming a tree by both keys
        is filed under both, so the buckets are merged on each record's filing
        position: a tree two records name is attributed to the one filed later,
        which is the order the reader that gathered the records would give.
        """
        buckets = (
            self._by_path.get(worktree),
            self._by_session_node.get((worktree.parent.name, worktree.name)),
            self._by_node.get(worktree.name),
        )
        found: dict[int, Mapping[str, Any]] = {}
        for bucket in buckets:
            for position, record in bucket or ():
                found.setdefault(position, record)
        return [found[position] for position in sorted(found)]

    def names(self, record: Mapping[str, Any], worktree: Path) -> bool:
        """Whether ``record`` names ``worktree``, by the rule's own keys."""
        return any(record is existing for existing in self.records_naming(worktree))


def _run_worktree_path(repo: Path, project: str | None, run_id: str) -> Path:
    """The worktree a run's own records name, preferring its live pointer.

    A retained tree is named by the run's ledger row once its live pointer has
    been promoted away, so both sources are read. A withheld run's row carries
    no retained path and no worktree field — only the node id its tree is named
    for. A tree the record names by that node id is resolved from the
    registered trees, exactly as the obligations view resolves it, so the
    remedy reaches the tree the obligation reports. A run no record names has
    no tree to confine a sweep to and is refused.
    """
    for record in list_live():
        if str(record.get("run_id") or "") != run_id:
            continue
        value = str(record.get("worktree") or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    registered = [path for path in _registered_worktrees(repo) if path != repo]
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
        for tree in registered:
            if record_names_worktree(record, tree):
                return tree
    raise CrewError(f"no record names a worktree for run {run_id}")


# A tree is released whole or not at all. The registered path is vacated in one
# rename onto a trash directory on the tree's own filesystem, so a deadline or a
# failure while the copy is deleted can only leave trash, never a registered
# tree holding a fraction of its files. `git worktree remove` is not the
# deletion step: it deletes the files while deregistering the tree under a single
# deadline, and on a loaded filesystem that deadline has expired partway,
# leaving a husk that is neither whole nor gone.
WORKTREE_TRASH_DIRNAME = "_trash"


def _releasable_roots(repo: Path) -> list[Path]:
    """The roots whose trees this node owns and may release.

    Two places hold a disposable tree: the managed worktree roots a dispatch
    creates trees in, and the crew runs root, where a worker's own ``git
    worktree add`` puts a source tree inside its run directory. Both are
    managed, and `crew gc` already walks both, so a tree under either is
    releasable; anything else — the checkout, its ancestors, a tree someone
    registered by hand elsewhere — is not.
    """
    roots = list(_workspace_roots(repo))
    runs = runs_dir()
    if runs not in roots:
        roots.append(runs)
    return roots


def _worktree_trash_root(repo: Path, path: Path) -> Path:
    """The trash directory a tree is moved into, on the tree's own filesystem.

    Trash sits inside the root that holds the tree, so the rename stays on one
    filesystem and cannot fail with a cross-device error; a tree outside every
    releasable root falls back to the primary root's trash.
    """
    resolved = path.resolve()
    roots = _releasable_roots(repo)
    for root in roots:
        if resolved.is_relative_to(root):
            return root / WORKTREE_TRASH_DIRNAME
    return roots[0] / WORKTREE_TRASH_DIRNAME


def _worktree_registration(repo: Path, tree: Path) -> dict[str, Any] | None:
    """The registration record for ``tree``, or ``None`` when git holds none."""
    resolved = tree.resolve()
    for record in _worktree_registrations(repo):
        if record["path"] == resolved:
            return record
    return None


def _worktree_admin_dir(repo: Path, tree: Path) -> Path | None:
    """The administrative directory whose deletion deregisters this one tree.

    ``git worktree list --porcelain`` omits ``gitdir`` for a worktree at the
    standard location, so it is read from the tree itself. Only a directory
    directly under a ``worktrees`` parent is returned, so the main repository's
    git directory can never be named here.
    """
    probe = _git(tree, "rev-parse", "--absolute-git-dir", check=False)
    if probe.returncode or not probe.stdout.strip():
        return None
    admin = Path(probe.stdout.strip()).resolve()
    return admin if admin.parent.name == "worktrees" else None


def _worktree_is_locked(repo: Path, tree: Path) -> bool:
    """Whether git holds a lock on ``tree``, which forbids its removal.

    ``git worktree remove`` refuses a locked tree, and the rename that replaces
    it would otherwise ignore the lock, so the lock is read and honoured here
    before the tree is moved rather than silently overridden.
    """
    record = _worktree_registration(repo, tree)
    return bool(record and record["locked"])


def _release_withhold_reason(repo: Path, tree: Path) -> str:
    """Why ``tree`` must not be vacated, or the empty string when it may be.

    Only a linked worktree that this repository registers and that is neither
    the repository, one of its ancestors nor its main checkout is a release
    candidate. Without these the release would move something other than a
    disposable tree: a run pointer naming the main checkout would take the
    checkout itself, and the directory it moved to would be swept as trash.
    Where a tree sits is not one of these: which trees are offered at all is
    the caller's decision, and `crew gc` already narrows its candidates to the
    roots it manages, so refusing by location here would only withhold a tree a
    caller legitimately owns.
    """
    resolved = tree.resolve()
    repository = repo.resolve()
    if repository.is_relative_to(resolved):
        return "worktree names the repository root or one of its ancestors"
    if resolved == repository_identity(repo):
        return "worktree is not a linked worktree"
    if resolved not in _registered_worktrees(repo):
        return "worktree is not registered with the repository"
    return ""


def _delete_worktree_copy(trash: Path) -> None:
    """Delete a moved-aside tree copy. No deadline: a failure leaves trash.

    Kept separate from the move so the deletion step — the one place a deadline
    can interrupt — is exercised or substituted on its own.
    """
    shutil.rmtree(trash, ignore_errors=True)


def _release_worktree_whole(
    repo: Path,
    path: str | Path,
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """Vacate a worktree's registered path in one rename, then delete the copy.

    The tree is moved aside before git is told to forget it, so the registered
    path is emptied in a single step no deadline can interrupt partway. The
    moved tree's own administrative directory is then deleted, which deregisters
    that tree and no other — a global ``git worktree prune`` would also drop the
    registration of a peer tree whose directory vanished outside git, which is
    another session's record and must be left in place. The trash copy is
    deleted with no deadline; a deletion that fails or does not finish leaves
    trash — which ``crew gc`` sweeps — rather than a half-removed registered
    tree.

    A path that is not a disposable linked worktree is refused before anything
    moves, and the refusal comes back as a withheld release rather than an
    exception, so a caller holding a bad pointer leaves everything where it is.
    """
    tree = Path(path)
    withheld = _release_withhold_reason(repo, tree)
    if withheld:
        return {"path": str(tree), "released": False, "withheld": withheld}
    if _worktree_is_locked(repo, tree):
        raise CrewError(f"refusing to release locked worktree {tree}")
    admin = _worktree_admin_dir(repo, tree)
    trash_root = _worktree_trash_root(repo, tree)
    trash_root.mkdir(parents=True, exist_ok=True)
    label = run_id or tree.name
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
    trash = trash_root / f"{label}-{stamp}"
    os.rename(tree, trash)
    if admin is not None and admin.is_dir():
        shutil.rmtree(admin, ignore_errors=True)
    result: dict[str, Any] = {
        "path": str(tree),
        "released": True,
        "trash": str(trash),
        "trash_removed": False,
    }
    try:
        _delete_worktree_copy(trash)
    except Exception as exc:  # noqa: BLE001 - any failure leaves trash, not a husk
        result["trash_removal_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["trash_removed"] = not trash.exists()
    return result


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
    released = _release_worktree_whole(repo, path)
    if not released.get("released", True):
        raise CrewError(f"refusing to release worktree {path}: {released['withheld']}")
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

    Only a row carrying measured non-equivalent commits qualifies, and that
    covers both a dirty tree and a clean one whose head is unreachable — the
    two states a deliberately abandoned tree is left in. The archive ref is
    what makes its removal safe, so a row whose commits were never measured is
    not released on the strength of an unread comparison.
    """
    return bool(
        pin_unique_commits
        and item["classification"] in ("dirty", "unintegrated")
        and item.get("non_equivalent_commits")
    )


# The reason a raw review report is held back: the committed record is the
# durable copy of a review, and until it exists the raw text is the only copy,
# so a report whose record is absent is reported rather than removed.
PLAN_REVIEW_RAW_REPORT_REASON = (
    "the review's committed record is absent, so the raw report is the only "
    "copy of the review and is never removed"
)


def _newest_mtime(directory: Path) -> datetime:
    """The newest file mtime beneath a tree, or the tree directory's own."""
    newest = directory.stat().st_mtime
    for child in directory.rglob("*"):
        try:
            stamp = child.stat().st_mtime
        except OSError:
            continue
        newest = max(newest, stamp)
    return datetime.fromtimestamp(newest, tz=UTC)


def _live_run_ids() -> set[str]:
    """The run ids a live pointer currently names, read fresh for a re-check."""
    return {str(record.get("run_id") or "") for record in list_live()}


def _run_has_live_pointer(
    run_id: str, live_ids: set[str], confined_to: str | None
) -> bool:
    """Whether a run's own artifacts are held by a live pointer.

    A live pointer names a run that may still be writing into its tree, so a
    tree whose run is live is not offered for removal. A pass confined with
    ``--run`` owns that run's artifacts — the operator named it, as the release
    door names a tree its own record carries — so the confined run's own live
    pointer does not withhold it.
    """
    if confined_to and confined_to == run_id:
        return False
    return run_id in live_ids


def _run_report_trees(
    *,
    project: str | None,
    apply: bool,
    now: datetime | None,
    run_id: str | None,
    live_ids: set[str],
    ledgered: set[str],
    retention_days: int,
) -> list[dict[str, Any]]:
    """Run-keyed report trees outside the plan-review family, past retention.

    A report tree here is a top-level ``reports/<project>/<run-id>/`` directory
    named for a run gc knows, live or already in the ledger; a topic-named
    directory filed by hand carries no run name and is never matched. A tree
    whose run is live is left alone, and the live set is re-read immediately
    before each removal.
    """
    from reckon.crew.dispatch import tree_size_bytes
    from reckon.crew.runs import reports_dir

    reports_root = reports_dir()
    if not reports_root.is_dir():
        return []
    current = now or datetime.now(tz=UTC)
    cutoff = current - timedelta(days=retention_days)
    entries: list[dict[str, Any]] = []
    for project_dir in sorted(path for path in reports_root.iterdir() if path.is_dir()):
        name = project_dir.name
        if project and name != project:
            continue
        for tree in sorted(path for path in project_dir.iterdir() if path.is_dir()):
            report_run_id = tree.name
            if report_run_id == "plan-review":
                continue
            if report_run_id not in live_ids and report_run_id not in ledgered:
                continue
            if run_id and report_run_id != run_id:
                continue
            modified = _newest_mtime(tree)
            if modified > cutoff:
                continue
            if _run_has_live_pointer(report_run_id, live_ids, run_id):
                continue
            entry = {
                "family": "run",
                "project": name,
                "run_id": report_run_id,
                "path": str(tree),
                "tree": str(tree),
                "size": tree_size_bytes(tree),
                "age_days": (current - modified).days,
                "action": "prune",
                "removed": False,
            }
            if apply and not _run_has_live_pointer(
                report_run_id, _live_run_ids(), run_id
            ):
                shutil.rmtree(tree)
                entry["removed"] = True
            entries.append(entry)
    return entries


def _stale_run_locks(
    *,
    apply: bool,
    now: datetime | None,
    run_id: str | None,
    live_ids: set[str],
    ledgered: set[str],
    retention_days: int,
) -> list[dict[str, Any]]:
    """Lock files for runs already in the ledger, past retention and not live.

    ``_pointer_lock`` creates ``locks/<run-id>.lock`` per run and nothing
    removes it. A run still live may take that lock again, so only a lock whose
    run is in the ledger — the run has finished — and has no live pointer is
    offered. The queue's own ``queued-dispatch.lock`` is not a run's and is
    never matched. Before a lock is removed the live set is re-read, so a
    pointer that appears mid-sweep still withholds its run's lock.
    """
    from reckon.crew.runs import crew_home

    locks_root = crew_home() / "locks"
    if not locks_root.is_dir():
        return []
    current = now or datetime.now(tz=UTC)
    cutoff = current - timedelta(days=retention_days)
    entries: list[dict[str, Any]] = []
    for lock in sorted(path for path in locks_root.glob("*.lock") if path.is_file()):
        if lock.name == "queued-dispatch.lock":
            continue
        lock_run_id = lock.stem
        if run_id and lock_run_id != run_id:
            continue
        if lock_run_id not in ledgered or lock_run_id in live_ids:
            continue
        modified = datetime.fromtimestamp(lock.stat().st_mtime, tz=UTC)
        if modified > cutoff:
            continue
        entry = {
            "run_id": lock_run_id,
            "path": str(lock),
            "size": lock.stat().st_size,
            "age_days": (current - modified).days,
            "action": "prune",
            "removed": False,
        }
        if apply and lock_run_id not in _live_run_ids():
            lock.unlink()
            entry["removed"] = True
        entries.append(entry)
    return entries


def _clear_run_lock(run_id: str, *, apply: bool) -> dict[str, Any]:
    """Remove one run's own lock file, the one ``_pointer_lock`` leaves behind.

    A run whose recorded worktree is already gone has finished, so an explicit
    run-scoped sweep clears its lock rather than waiting for the retention
    window the age of a lock is otherwise judged against. The queue's own
    ``queued-dispatch.lock`` is not a run's and is never matched.
    """
    from reckon.crew.runs import crew_home

    lock = crew_home() / "locks" / f"{run_id}.lock"
    entry: dict[str, Any] = {
        "run_id": run_id,
        "path": str(lock),
        "action": "prune",
        "removed": False,
    }
    if lock.is_file() and apply:
        lock.unlink()
        entry["removed"] = True
    return entry


def _mounted_ledgered_run_ids() -> tuple[set[str], list[str]]:
    """Run ids held by every registered project's ledger, and unreadable names.

    The runs directory is shared by every registered project, so a run id is
    unledgered only when no registered project's ledger holds it. A project
    whose ledger cannot be read leaves the set incomplete, so its name is
    returned rather than its silence read as ``no rows``: the caller then
    withholds the whole sweep rather than offering a peer's run for removal.
    An absent ledger is a project that has run nothing yet, not an unreadable
    one, and reads as empty. The mounted docs directory is the same authority
    ``resolve_dispatch_authority`` reads through, and its parent is the
    checkout whose ledger the reads below resolve against.
    """
    try:
        mounts = flight.mounted_project_docs()
    except flight.FlightConfigError as exc:
        return set(), [f"the mount registry could not be read: {exc}"]
    ids: set[str] = set()
    unreadable: list[str] = []
    for name, docs in sorted(mounts.items()):
        try:
            records = ledger.runs(str(name), root=docs.parent)
        except (ledger.LedgerError, OSError, ValueError, CorruptEnvelopeError):
            unreadable.append(str(name))
            continue
        for record in records:
            record_run_id = str(record.get("run_id") or "")
            if record_run_id:
                ids.add(record_run_id)
    return ids, unreadable


def _unledgered_run_directories(
    *,
    apply: bool,
    now: datetime | None,
    run_id: str | None,
    live_ids: set[str],
    ledgered: set[str],
    retention_days: int,
) -> list[dict[str, Any]]:
    """Run directories with no ledger row and no live pointer, past retention.

    The ledgered-directory sweep withholds these when their figures are not
    recorded: without a row there is nothing to check, and reaping would
    destroy a stream that is still the only copy of its figures. A directory
    with neither a row nor a live pointer is covered by nothing durable, so
    past retention it is offered whole. The live set is re-read before each
    removal.

    The run directory is shared with every registered project, so the known
    set is the union of the gc repository's own ``ledgered`` rows with every
    registered mount's. The mounts are read because a run this checkout never
    ledgers is still a peer project's; the repository's own rows are carried
    because a repository can run workers without being a registered mount, and
    its own ledgered directories must survive its gc. If any registered
    project's ledger cannot be read, the sweep fails closed: every would-be
    row is reported withheld with a reason naming the unreadable project and
    none of them is removed, so a gc run from one checkout cannot delete a
    peer project's run directories.
    """
    from reckon.crew.dispatch import tree_size_bytes

    runs_root = runs_dir()
    if not runs_root.is_dir():
        return []
    mounted, unreadable = _mounted_ledgered_run_ids()
    known = set(ledgered) | mounted
    current = now or datetime.now(tz=UTC)
    cutoff = current - timedelta(days=retention_days)
    entries: list[dict[str, Any]] = []
    for directory in sorted(
        path
        for path in runs_root.iterdir()
        if path.is_dir() and path.name != WORKTREE_TRASH_DIRNAME
    ):
        directory_run_id = directory.name
        if run_id and directory_run_id != run_id:
            continue
        if directory_run_id in known or directory_run_id in live_ids:
            continue
        modified = datetime.fromtimestamp(directory.stat().st_mtime, tz=UTC)
        if modified > cutoff:
            continue
        entry = {
            "run_id": directory_run_id,
            "path": str(directory),
            "size": tree_size_bytes(directory),
            "age_days": (current - modified).days,
            "action": "prune",
            "removed": False,
        }
        if unreadable:
            entry["action"] = "withheld"
            entry["withheld"] = (
                "the ledger of registered project "
                f"{', '.join(repr(name) for name in unreadable)} could not be "
                "read, so no run directory can be shown unledgered"
            )
        elif apply and directory_run_id not in _live_run_ids():
            shutil.rmtree(directory)
            entry["removed"] = True
        entries.append(entry)
    return entries


def _plan_review_raw_reports(
    *,
    repo_root: Path,
    project: str | None,
    apply: bool,
    now: datetime | None,
    run_id: str | None = None,
    live_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Prune a plan review's raw report text past its retention, printing each.

    A tree whose review run still holds a live pointer is left alone, because
    that run may be writing into it; the live set is re-read immediately before
    each removal so a pointer that appears mid-sweep still withholds its tree.

    The committed record filed under the project's docs tree is what survives a
    clone, and coverage reads the sidecar and the snapshot beside the raw text,
    so the raw text is the one part of a delivered review the record already
    distils. It is removed after ``review.raw_report_retention_days`` on the
    host — but only once the committed record exists, because until then the raw
    text is the only copy. Every candidate appears in the returned rows with its
    path, age in days and action, so a dry run prints the same set ``--apply``
    would act on. A ``run_id`` confines the pass to the one report, exactly as
    the rest of the sweep is confined to one run, so ``crew gc --run`` cannot
    reach another run's raw report.
    """
    from reckon.crew import plan_review
    from reckon.crew import review as _review_store
    from reckon.crew.runs import reports_dir

    reports_root = reports_dir()
    if not reports_root.is_dir():
        return []
    current = now or datetime.now(tz=UTC)
    retention: dict[str, int] = {}

    def _days_for(name: str) -> int:
        if name not in retention:
            config = flight.resolve(name, checkout_path=repo_root).config
            retention[name] = flight.raw_report_retention_days(config)
        return retention[name]

    entries: list[dict[str, Any]] = []
    for project_dir in sorted(path for path in reports_root.iterdir() if path.is_dir()):
        name = project_dir.name
        if project and name != project:
            continue
        plan_root = project_dir / "plan-review"
        if not plan_root.is_dir():
            continue
        cutoff = current - timedelta(days=_days_for(name))
        committed_root = _review_store.committed_review_root(name, root=repo_root)
        for slug_dir in sorted(path for path in plan_root.iterdir() if path.is_dir()):
            for run_dir in sorted(path for path in slug_dir.iterdir() if path.is_dir()):
                if run_id and run_dir.name != run_id:
                    continue
                report = run_dir / plan_review._REVIEW_REPORT_NAME
                if not report.is_file():
                    continue
                modified = datetime.fromtimestamp(report.stat().st_mtime, tz=UTC)
                if modified > cutoff:
                    continue
                if _run_has_live_pointer(run_dir.name, live_ids or set(), run_id):
                    continue
                record = (
                    plan_review.plan_review_path(
                        name,
                        slug_dir.name,
                        0,
                        committed_root=committed_root,
                        review_run_id=run_dir.name,
                    )
                    if committed_root is not None
                    else None
                )
                entry = {
                    "family": "plan-review",
                    "project": name,
                    "plan_slug": slug_dir.name,
                    "run_id": run_dir.name,
                    "path": str(report),
                    "tree": str(run_dir),
                    "size": report.stat().st_size,
                    "age_days": (current - modified).days,
                    "removed": False,
                }
                if record is not None and record.is_file():
                    entry["action"] = "prune"
                    if apply and not _run_has_live_pointer(
                        run_dir.name, _live_run_ids(), run_id
                    ):
                        report.unlink()
                        entry["removed"] = True
                else:
                    entry["action"] = "withheld"
                    entry["withheld"] = "missing-committed-record"
                    entry["reason"] = PLAN_REVIEW_RAW_REPORT_REASON
                entries.append(entry)
    return entries


def _ledger_index_locked(index: Path) -> bool:
    """Whether a live process holds the index, so it is never removed.

    A reader mid-refresh holds a write lock, and SQLite refuses a second
    writer: that refusal is the signal. A file that cannot be opened at all is
    not a running reader, so it is not treated as locked.
    """
    import contextlib
    import sqlite3

    try:
        with contextlib.closing(sqlite3.connect(index, timeout=0.0)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                return True
            connection.rollback()
    except sqlite3.Error:
        return False
    return False


def _iso_ctime(path: Path) -> str | None:
    """The directory's ctime as an ISO timestamp, or ``None`` if unreadable."""
    try:
        return datetime.fromtimestamp(path.stat().st_ctime, tz=UTC).isoformat()
    except OSError:
        return None


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


def _launch_worktree_path(repo: Path, session: str, worker: str) -> Path:
    """The path a launch for ``session``/``worker`` creates its tree at.

    A node's worktree path is named for its session and node, so every attempt
    to launch that node in that session reaches for the same directory. The
    path is the fleet script's own first managed root, so a tree found here is
    exactly the one a fresh creation would collide with.
    """
    return _workspace_roots(repo)[0] / session / worker


def _launch_attempt_record(repo: Path, path: Path) -> Mapping[str, Any] | None:
    """The record naming the tree at ``path``, preferring a live pointer.

    A leftover launch path may hold a tree left by an earlier attempt, which
    names itself through the same sources a finished run does: its live pointer
    while one survives, and a ledger row once one is written. Whichever names
    the path lets the refusal name the attempt that left it.
    """
    resolved = path.resolve()
    for record in list_live():
        value = str(record.get("worktree") or "").strip()
        if value and Path(value).expanduser().resolve() == resolved:
            return record
    return _residue_run_record(resolved, _ledgered_records(repo, None))


def _launch_tree_verdict(
    repo: Path, path: Path, base: str
) -> tuple[dict[str, Any], str]:
    """Classify a tree left at a launch path, with the label for its attempt.

    The classification comes from the one workspace inspector, called with the
    launch's base as the integration revision and the live claims as the
    references, so a launch and a promotion judge a tree by the same rules. The
    label names the attempt that left the tree — a live run, or the ledger row
    a finished one wrote — so a refusal can report which attempt owns it.
    """
    record = _launch_attempt_record(repo, path)
    attempt = str(record.get("run_id") or "") if record else ""
    label = f"run {attempt}" if attempt else "an earlier attempt"
    claims = _live_pointer_worktrees().get(path.resolve(), [])
    verdict = _inspect_workspace(repo, path, base, claims, raise_on_unavailable=False)
    return verdict, label


def _tree_is_disposable_for_launch(repo: Path, path: Path, base: str) -> bool:
    """Whether a tree left at a launch path may be released for a new launch.

    Disposable is the inspector's ``integrated``: the tree is clean and its
    head is an ancestor of the base the launch is cut from, so it carries no
    commit beyond it. Any other classification keeps the tree, because the
    content it holds may exist nowhere else.
    """
    verdict, _ = _launch_tree_verdict(repo, path, base)
    return verdict["classification"] == "integrated"


def _stale_launch_tree_refusal(repo: Path, path: Path, base: str) -> str:
    """Why the tree at a launch path must be kept, or ``""`` when it may go.

    A tree a live run still holds is never removed, and neither is one whose
    head carries a commit or whose working copy holds changes — that content
    may exist nowhere else. Only a tree the inspector classifies as integrated
    is disposable, and it is released through the whole-tree release so its
    registered path is vacated in one step.
    """
    verdict, label = _launch_tree_verdict(repo, path, base)

    claims = verdict["claimed_by_live_runs"]
    if claims:
        return (
            f"{label} still holds live run(s) {', '.join(claims)}; the "
            f"worktree at {path} is kept"
        )

    if verdict["classification"] == "unavailable":
        return (
            f"{label} left an unreadable tree at {path}: "
            f"{verdict.get('detail') or 'unavailable'}; kept"
        )
    if verdict["dirty"]:
        return f"{label} left uncommitted changes in the worktree at {path}; kept"
    if not _tree_is_disposable_for_launch(repo, path, base):
        head = str(verdict.get("head") or "")
        return (
            f"{label} left commit {head} on the worktree at {path}, beyond its "
            f"base {base}; kept"
        )
    return ""


def _create_worktree(
    repo: Path, session: str, worker: str, base: str
) -> dict[str, Any]:
    """Create a detached worktree through the fleet script, or raise.

    A path still holding a tree from an earlier attempt is released first when
    that tree is disposable, so a launch that once failed at its streamless
    start does not block every later retry of its node on the leftover path.
    """
    target = _launch_worktree_path(repo, session, worker)
    if target.exists():
        refusal = _stale_launch_tree_refusal(repo, target, base)
        if refusal:
            raise CrewError(f"worktree creation failed: {refusal}")
        _remove_worktree(repo, str(target))
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
    released = _release_worktree_whole(repo, path)
    if not released.get("released", True):
        raise CrewError(f"refusing to remove worktree {path}: {released['withheld']}")


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


#: The plan units a queued request reads, published by the admission pass for
#: the length of the one launch it is making. The launch path reaches this gate
#: through the ordinary dispatch call, which has no argument of its own for the
#: narrowing, so the units travel beside it rather than being restated as a
#: second gate. A dispatch that publishes nothing — every direct dispatch —
#: keeps the whole-plan check.
ADMISSION_REVIEW_UNITS: ContextVar[Iterable[str] | None] = ContextVar(
    "admission-review-units", default=None
)


def plan_review_units(section: str) -> set[str]:
    """The plan units one dispatch of ``section`` reads.

    The unit is the section itself and the document unit, which carries the
    plan's decisions, gates and dependencies: a change to either is a change
    this dispatch must have been reviewed against, while a section elsewhere
    in the plan is another dispatch's business. Every spelling of a section
    resolves to one identity through :func:`reckon._plan_html.section_record_id`,
    which is the identity a stored review records its coverage under. A
    dispatch naming no section reads the plan as a whole, so its caller asks
    for no narrowing.
    """
    from reckon._plan_html import DOCUMENT_UNIT, section_record_id

    units = {DOCUMENT_UNIT}
    if str(section).strip():
        units.add(section_record_id(section))
    return units


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

    Report-only is the shipped default, so a repository adopting reckon is not
    stopped before its authors have composed a first review; a host or project
    layer selects enforcement. In report-only mode the dispatch proceeds and the
    same sentence an enforced refusal would carry is recorded on the dispatch
    result as a warning, so the fact is visible without being fatal and the
    wording does not drift between the two modes.
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
    units: Iterable[str] | None = None,
) -> str | None:
    """Judge whether a building dispatch's plan carries an answered review.

    A plan is reviewed before it is built: the gate joins a stored review to the
    plan content by fingerprint rather than by the plan's version integer, so a
    metadata-only write neither demands a review nor orphans one, and an
    authored edit demands a fresh one. Every finding of the review must
    be answered — acted on or declined with a reason — because the findings are
    advisory and the answer is the record; an unanswered finding is an unread
    one, and the gate refuses a plan whose review nobody read.

    Only a hot review's findings refuse: the gate reads each review's derived
    lifecycle (:mod:`reckon.crew.review_lifecycle`) and counts a finding only
    when the review is open or answered. A landed review — its reviewed sections
    all declared done, or the plan shipped — or one superseded by a later review
    of the same rubric is the archive, so its unanswered finding no longer
    blocks a build of content a different review now covers. A design review at
    any version and in any lifecycle state satisfies its requirement unless it is
    marked :data:`INCOMPLETE_STATUS` — a delivered design report that left out an
    item of its own rubric covers nothing, so it is refused here as though no
    design review existed, naming the design-review command. The coverage check
    is otherwise unchanged.

    ``allow_unreviewed`` is the operational waiver: a broken local review lane
    must not stop every build, so the caller may waive the gate and the waiver
    is recorded on the run that carries it. ``enforce`` selects the mode: when
    false (the default) a plan's missing or unanswered review returns the
    warning to record and the dispatch proceeds; when true the same condition
    raises :class:`PlanReviewMissingError`. ``None`` is returned when there is
    nothing to report.

    ``units`` narrows the coverage judgment to the units the caller's request
    actually reads — the request's own section and the document unit that
    carries the plan's decisions, gates and dependencies. A plan edit elsewhere
    then no longer strands that request. A dispatch of a section narrows to its
    own units even when the caller names none, so another section's uncovered
    edit does not refuse it. A dispatch naming no section keeps the whole-plan
    check, because it reads the plan as a whole. When no stored review covers
    any unit of the plan, the narrowing is set aside and the refusal names the
    whole content the plan is owed a review of.
    """

    if units is None:
        units = ADMISSION_REVIEW_UNITS.get()
    if units is None and str(getattr(node, "section", "") or "").strip():
        # A dispatch of one section reads that section and the document unit,
        # so an authored edit to a section it does not read no longer refuses
        # content a review does cover. The units a caller names win over this
        # default, and a dispatch naming no section keeps the whole-plan check.
        units = plan_review_units(node.section)
    scope = None if units is None else {str(unit) for unit in units}
    if allow_unreviewed or _plan_review_exempt(node):
        return None

    from reckon.crew import plan_review, review_lifecycle
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
    design = plan_review.newest_design_review(project, node.plan)
    if design is None or plan_review._review_is_incomplete(design):
        # The design review is the plan's one search of the codebase for
        # machinery it could reuse, so it is owed once, at any version; a design
        # review also covers the content it read, so composing it first answers
        # both requirements for a plan that has neither. A design review that
        # left out an item of its own rubric is marked incomplete and covers
        # nothing, so it is refused here as though none existed.
        design_invocation = plan_review.review_invocation(
            project, node.plan, rubric=plan_review.DESIGN_RUBRIC
        )
        if design is None:
            missing = "carries no stored review under the design rubric"
        else:
            missing = (
                "carries a design review that left out an item of its rubric"
            )
        return _plan_review_verdict(
            enforce,
            f"plan {node.plan!r} in project {project!r} {missing}; "
            f"{_refused_store_detail(refusals)}"
            "a plan is searched against the codebase for machinery it could reuse "
            "once, before its first implementation node; compose that review with "
            f"`{design_invocation}`",
        )
    _records, uncovered, changes = plan_review.review_coverage(
        project, node.plan, plan=resource.path
    )
    record = plan_review.read_plan_review(project, node.plan, plan=resource.path)
    if scope is not None and not _records:
        # No stored review covers any of the plan, so the review this dispatch
        # is owed is a review of all of it, and the refusal names every unit
        # uncovered rather than only the ones this request reads.
        scope = None
    if scope is not None:
        # Only the units this caller reads decide its hold. A unit the plan
        # carries and the review never read is another request's business.
        uncovered = {unit for unit in uncovered if unit in scope}
    if uncovered or (record is None and scope is None):
        return _plan_review_verdict(
            enforce,
            f"plan {node.plan!r} in project {project!r} carries no stored review "
            f"of the content about to be built; uncovered units: {', '.join(sorted(uncovered))}; "
            f"{_uncovered_change_detail(uncovered, changes)}"
            f"{_refused_store_detail(refusals)}"
            "a plan is reviewed before it is built; compose one with "
            f"`{plan_review.review_invocation(project, node.plan)}`",
        )
    plan_state = _plan_html.read_state_file(resource.path)
    plan_records = plan_review.list_plan_reviews(project)
    unanswered = (
        plan_review.unanswered_findings(record)
        if review_lifecycle.is_hot(
            record, plan_state=plan_state, later_records=plan_records
        )
        else []
    )
    if design.get("review_path") != record.get("review_path") and (
        review_lifecycle.is_hot(
            design, plan_state=plan_state, later_records=plan_records
        )
    ):
        unanswered += [
            finding
            for finding in plan_review.unanswered_findings(design)
            if finding not in unanswered
        ]
    # A finding's identity names no unit, so an unanswered one cannot be
    # attributed to the caller's scope: narrowing the coverage judgment to the
    # units a request reads says nothing about whether the review that carried
    # the finding was read, so the refusal stands whatever the scope.
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
    work_projects = list(repository_projects(work_repo))
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


def repository_projects(path: str | Path) -> tuple[str, ...]:
    """Return the mounted projects that own the repository a path belongs to.

    Resolution is by repository identity rather than by resolved path, so a
    linked worktree names the same projects as the main checkout it shares an
    object store with. A path outside every mounted repository has none.
    """
    identity = repository_identity(path)
    if identity is None:
        return ()
    return mounted_repository_projects().get(identity, ())


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
        # The decision reads the union — a member's last dispatch may sit in a
        # completed run's own file — while the write carries the aggregate's own
        # rows. Writing the union back would copy every per-run row into the
        # tracked aggregate beside the file that already holds it, which the
        # writer refuses.
        aggregate, _aggregate_version = ledger.load_aggregate(project, root)
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
        # The write carries the aggregate's own rows with the roster removal
        # applied; the union's run rows stay in the files that already hold them.
        data["runs"] = aggregate["runs"]
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

    def describe(
        raw: str, *, declared_read: bool, regenerated: bool = False
    ) -> dict[str, Any]:
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
            "provenance": "regenerated" if regenerated else "declared",
            "status": "missing",
            "reference_tokens": (
                _REGENERATED_PATH_REFERENCE_TOKENS if regenerated else 0
            ),
        }
        if not regenerated and not declared_read and resolved in granted_paths:
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
        tokens = (
            _REGENERATED_PATH_REFERENCE_TOKENS
            if regenerated
            else _tokens_for_bytes(byte_count)
        )
        chargeable = record["provenance"] in ("declared", "regenerated")
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

    regenerated_declared = {str(path) for path in node.regenerate_paths}
    write_paths = [
        describe(
            str(path),
            declared_read=False,
            regenerated=str(path) in regenerated_declared,
        )
        for path in node.write_paths
    ]
    named_files = [
        describe(path, declared_read=True) for path in _declared_input_files(node)
    ]
    all_inputs = [*write_paths, *named_files]
    file_tokens = sum(
        int(item["estimated_tokens"]) for item in all_inputs if item["counted"]
    )
    return file_tokens, {"write_paths": write_paths, "named_files": named_files}


def pick_lane_document(
    backend: Mapping[str, Any], cache: dict[str, Any]
) -> Any:
    """Parse one backend's published lane document once per pick.

    ``run_time_profile.local_lane_load`` reads the same published document for
    the lane's live load. A pick that also needs the parsed document itself --
    for the admission and slot fields that reader does not expose -- reads it
    here, and stores the parse in ``cache``, which the caller scopes to one
    pick, so a document is read at most once per pick however many backends
    resolve to it. An unreadable or malformed document reads as ``None``, which
    the lane readers turn into their all-unknown report rather than raising.
    """

    path = backend.get("lane_document")
    if not path:
        return None
    key = str(path)
    if key not in cache:
        try:
            cache[key] = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache[key] = None
    return cache[key]


def _census_file_paths(
    standing: Mapping[str, Any], files: Mapping[str, Any]
) -> list[str]:
    """Every file one context census read, deduplicated for its stamp."""

    paths: list[str] = []
    for item in standing.get("files") or ():
        path = str((item or {}).get("path") or "")
        if path:
            paths.append(path)
    for key in ("write_paths", "named_files"):
        for item in files.get(key) or ():
            path = str((item or {}).get("path") or "")
            if path:
                paths.append(path)
    return sorted(set(paths))


def _census_request_key(
    node: TaskNode,
    repo: Path,
    backend_settings: Mapping[str, Any] | None,
    authority: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The selecting fields of one context census, hashed into its cache name.

    Two calls sharing every field read the same files and share one entry; a
    changed node, agent layout or dispatch authority selects a different entry
    rather than serving a figure measured for another request.
    """

    authority_key = ""
    if isinstance(authority, Mapping):
        authority_key = hashlib.sha256(
            json.dumps(authority, sort_keys=True, default=str).encode()
        ).hexdigest()
    return {
        "repo": str(repo.resolve()),
        "goal": node.goal,
        "done_when": node.done_when,
        "role": node.role,
        "spec_level": node.spec_level,
        "plan": node.plan,
        # The regenerated set selects which declared write paths are charged at
        # the reference cost rather than their size, so two nodes whose scopes
        # differ only there must not share one cached figure.
        "regenerate_paths": sorted(str(path) for path in node.regenerate_paths),
        "agent": _context_agent(backend_settings or {}),
        "authority": authority_key,
    }


def context_census(
    node: TaskNode,
    repo: Path,
    *,
    backend_settings: Mapping[str, Any] | None = None,
    authority: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
) -> int:
    """One node's deterministic input estimate, cached on the files it reads.

    The estimate is the standing instruction chain plus the repository files the
    node's brief loads, so it is a pure function of those files. A second census
    of unchanged files reads none of them: the figure built from them is reused
    while every file it recorded still carries the stamp it was built from, and
    a file whose stamp moved makes the census run again over the files. Because
    the reused figure is the figure the same files produced, the cached value
    equals an uncached census of the same files.
    """

    def build() -> dict[str, Any]:
        standing_tokens, standing = _standing_context_input(repo, backend_settings or {})
        file_tokens, files = _context_file_inputs(repo, node, authority)
        return {
            "tokens": standing_tokens + file_tokens,
            "paths": _census_file_paths(standing, files),
        }

    def stamp_of(value: Mapping[str, Any]) -> list[Any]:
        return [
            [path, capabilities.file_stamp(path)]
            for path in (value.get("paths") or [])
        ]

    request_key = _census_request_key(node, repo, backend_settings, authority)
    name = "context-census-" + hashlib.sha256(
        json.dumps(request_key, sort_keys=True, default=str).encode()
    ).hexdigest()
    value = capabilities.cached_pick_input_stamped(
        name, request_key, stamp_of, build, root=root
    )
    return int(value["tokens"])


def _context_fit_verdict(
    *, resolution: DispatchPlan, repo: Path
) -> dict[str, Any] | None:
    """Compare one node's deterministic context estimate with its backend window.

    The window that gates the refusal is the smaller of the declared lane window
    and the effective boundary the backend records for its lane. The two are
    different quantities: the declared window is what the configuration claims
    the endpoint accepts, while the effective boundary is the lowest input a
    recorded refusal shows the endpoint actually rejected. A lane whose endpoint
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
    """Return the counted inputs that contribute the most estimated tokens.

    Every record carries how its input arrived -- a path the node must write, a
    path its brief names in prose, or a standing instruction file -- because the
    remedy differs: a declared write path cannot leave the brief, since the node
    edits it, while a mention can be cut or replaced by a reference.
    """

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
                    "origin": "standing instruction",
                }
            )
    repository = inputs.get("repository_files")
    repository = repository if isinstance(repository, Mapping) else {}
    for group in ("write_paths", "named_files"):
        # The two repository groups are the origin: a write path is charged
        # because the node must edit it, a named file only because the brief
        # names it.
        origin = "declared write path" if group == "write_paths" else "mention"
        for item in repository.get(group) or ():
            if isinstance(item, Mapping) and item.get("counted"):
                records.append(
                    {
                        "path": str(item.get("path") or ""),
                        "estimated_tokens": int(item.get("estimated_tokens") or 0),
                        "origin": origin,
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
    to act -- the estimate, the window it exceeded, the inputs that
    dominated the estimate, and how the largest of them arrived -- at the level
    the refusal answers on. The origin matters because a declared write path
    cannot leave the brief while a mention can be cut or made a reference, so
    the two shapes send the reader to different levers."""

    contributors = _context_contributors(context_fit)
    if contributors:
        rendered = ", ".join(
            f"{record['path']} ({record['estimated_tokens']} tokens)"
            for record in contributors
        )
        dominant = f"largest contributing files: {rendered}"
        lead = contributors[0]
        dominant_term = (
            f"; dominant term: {lead['path']} ({lead['estimated_tokens']} tokens), "
            f"which arrived as a {lead['origin']}"
        )
    else:
        dominant = (
            "no repository file dominates the estimate; the standing instruction "
            "context alone exceeds the window"
        )
        dominant_term = ""
    return (
        f"context-window-exceeded: estimated {context_fit['estimated_tokens']} "
        f"usable input tokens against a {context_fit['window_tokens']} token "
        f"window (shortfall {context_fit['shortfall_tokens']} tokens); {dominant}"
        f"{dominant_term}"
    )


def _docs_scan_inputs(docs: Path) -> tuple[list[str], list[str]]:
    """Return the docs-relative directories and files a resource scan reads.

    A scan walks every directory below the docs root before it identifies a
    file, so the listing of each directory a resource can be resolved from is
    part of the scan's input, and the metadata of every candidate file it walks
    past is read while it decides which file a slug names. Both are returned
    from the one walk, so a stamp over them sees a resource added, moved or
    removed wherever one could be found, and a sibling plan's metadata edit --
    which is an input to the resolution even when the named plan is untouched.
    The subtrees the resolver never identifies from -- the infra directories
    and the evidence fragment store -- are pruned rather than tracked.
    """

    from pathlib import PurePosixPath

    from reckon import resources

    directories: list[str] = []
    files: list[str] = []
    for current, subdirs, names in os.walk(docs):
        relative = PurePosixPath(Path(current).relative_to(docs).as_posix())
        parts = relative.parts
        if parts[:2] == resources.EVIDENCE_FRAGMENTS_SUBTREE or any(
            part in resources.INFRA_DIRS for part in parts
        ):
            subdirs[:] = []
            continue
        directories.append(relative.as_posix())
        subdirs[:] = [name for name in subdirs if name not in resources.INFRA_DIRS]
        for name in names:
            if name.endswith(".html"):
                files.append(str(relative / name))
    return sorted(directories), sorted(files)


def _docs_scan_directories(docs: Path) -> list[str]:
    """Return the docs-relative directories a resource scan reads.

    A caller that stamps a resolve by the paths it walked uses the directory
    listings alone; the candidate plan files a resolve reads are taken from
    :func:`_docs_scan_inputs` by the estimate stage, which stamps them too.
    """

    return _docs_scan_inputs(docs)[0]


def _estimated_hours(
    repo: Path, project: str, node: TaskNode
) -> tuple[float | None, str]:
    """Return neutral hours and whether the node or plan supplied them.

    The figure is a pure function of the node's own estimate and the plans the
    resolve reads, so a repeated call for an unchanged tree reads neither the
    plans' metadata nor the docs tree: the directory listings a resolve would
    read, every candidate plan file it would stamp and the resolved plan file
    carry the stamps the cached figure was built from, and the resolve runs
    again only when one of those stamps has moved. A sibling plan's metadata is
    part of that input even when the named plan is untouched, because an edited
    sibling changes which file a slug names; stamping every candidate keeps the
    cached figure no older than the tree it summarised. A plan named by a node
    is resolved once however often the pick asks for its estimate, and its
    source is unchanged whether the node, the plan or neither supplied the
    figure.
    """

    try:
        node_hours = float(node.estimated_hours)
    except (TypeError, ValueError):
        node_hours = 0.0
    if math.isfinite(node_hours) and node_hours > 0:
        return node_hours, "node"

    if not node.plan.strip():
        return None, "unavailable"

    from reckon import resources

    docs = repo / "docs"
    identity = hashlib.sha256(
        f"{repo.resolve()}:{project}:{node.plan}".encode()
    ).hexdigest()

    def build() -> dict[str, Any]:
        directories, files = _docs_scan_inputs(docs)
        resource = resources.resolve_resource(
            docs, project, node.plan, "plan", include_archived=False
        )
        hours: float | None = None
        source = "unavailable"
        if resource is not None:
            value = _plan_html.parse_meta(resource.path).get("effort_hours")
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = 0.0
            if math.isfinite(parsed) and parsed > 0:
                hours, source = parsed, "plan-fallback"
        return {
            "hours": hours,
            "source": source,
            "path": str(resource.path) if resource is not None else None,
            "directories": directories,
            "files": files,
        }

    def stamp_of(value: Mapping[str, Any]) -> list[Any]:
        stamp: list[Any] = [
            [relative, capabilities.file_stamp(docs / relative)]
            for relative in value["directories"]
        ]
        # Every candidate plan the resolve walked past is stamped, not only the
        # one it selected: a sibling's metadata edit changes which file a slug
        # names and what the selected plan declares, so a stamp that saw only
        # the resolved file would answer from a cache. The directory listings
        # already catch a file added, moved or removed; these catch an in-place
        # edit, which moves no directory stamp.
        stamp.extend(
            [relative, capabilities.file_stamp(docs / relative)]
            for relative in value.get("files") or ()
        )
        path = value.get("path")
        if path:
            stamp.append([path, ledger._file_identity(Path(path))])
        return stamp

    value = capabilities.cached_pick_input_stamped(
        f"plan-estimate-{identity}", identity, stamp_of, build
    )
    return value["hours"], value["source"]


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
        # ``CompetenceLimit`` reads its message from ``reason``: a token refusal
        # names the window it exceeded, so no neutral-hour placeholder is
        # written here. A horizon nobody measured stays absent from the verdict
        # rather than appearing as a measured zero.
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


# The gc/scratch and process-signalling families were moved out of this module
# into their own concern modules. Every name below is re-exported so a caller
# that does not move keeps importing it from here; the two modules import their
# helpers back from this module, so the re-export sits last, after every
# definition is bound.

from reckon.crew.gc import (  # noqa: E402, F401
    _HARNESS_SCRATCH_DIRS,
    _UNATTRIBUTED_SIZE_LIMIT,
    ARCHIVE_REF_PREFIX,
    LEDGER_INDEX_UNRECORDED_MAX_AGE_DAYS,
    PROGRAMMING_ERRORS,
    RECLAIMABLE_CLASSES,
    RUN_DIRECTORY_EXTRACTION_REASON,
    SCRATCH_GRACE_SECONDS,
    SCRATCH_LIVE,
    SCRATCH_TERMINAL,
    SCRATCH_UNATTRIBUTED,
    UNAVAILABLE_WORKTREE_REASON,
    UNREADABLE_WORKTREE_REASON,
    WITHHELD_REASONS,
    _gc_partial_report,
    _gc_projects,
    _harness_scratch_entry,
    _orphan_ledger_indexes,
    _processes_holding,
    _scratch_ctime,
    _scratch_disposition,
    _scratch_process_holders,
    _sweep_worktree_trash,
    garbage_collect,
    garbage_collect_orphan_scratch,
    garbage_collect_scratch,
)
from reckon.crew.process_signal import (  # noqa: E402, F401
    _RUN_WORKER_RECORD,
    RUN_ID_ENV,
    SENDER_RECORD_NAME,
    _process_environment,
    _processes_owning_run,
    _recorded_worker_pid,
    _root_names_the_recorded_process,
    _run_launch_floor,
    _run_process_pids,
    _running_descendants,
    _signal_one,
    _signal_process_group,
    _start_tick,
    _worker_record,
    _write_sender_record,
    signal_worker,
)
