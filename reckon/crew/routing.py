from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from reckon import _plan_html, capabilities, flight, ledger
from reckon.calibration import agent_configuration_key

from reckon.crew.node import (
    CrewError,
    DEFAULT_MEMBER_IDLE_WINDOW,
    PlanVisibilityError,
    TaskNode,
    _TERMINAL_RUN_PHASES,
    parse_duration,
)
from reckon.crew.runs import (
    _live_worktree_claims,
    _pointer_lock,
    _process_start_time,
    crew_home,
    list_live,
    pointer_path,
    process_alive,
    read_pointer,
    reports_dir,
    runs_dir,
)

if TYPE_CHECKING:
    from reckon.crew.dispatch import DispatchPlan

# ── Routing ─────────────────────────────────────────────────────────────────


def resolve_role(
    config: Mapping[str, Any], role: str, spec_level: str = ""
) -> tuple[str, dict[str, Any]]:
    """Resolve a role to its backend name and the effective backend settings.

    A role overlays only the keys it names; everything else falls through to the
    backend it dispatches to. That is what lets a review role drop to a
    read-only tier without restating a backend.
    """
    roles = config.get("roles") or {}
    overlay = roles.get(role)
    if overlay is None:
        known = ", ".join(sorted(roles)) or "none"
        raise CrewError(f"role {role!r} is not configured (configured roles: {known})")
    if not isinstance(overlay, Mapping):
        overlay = {}
    backends = config.get("backends") or {}
    routing_by_level = overlay.get("by_spec_level") or {}
    level_overlay = (
        routing_by_level.get(spec_level, {})
        if spec_level and isinstance(routing_by_level, Mapping)
        else {}
    )
    if not isinstance(level_overlay, Mapping):
        level_overlay = {}
    backend_name = (
        level_overlay.get("backend")
        or overlay.get("backend")
        or config.get("default_backend")
    )
    if not backend_name:
        raise CrewError(
            f"role {role!r} selects no backend and no default_backend is set"
        )
    backend = backends.get(backend_name)
    if not isinstance(backend, Mapping):
        known = ", ".join(sorted(backends)) or "none"
        raise CrewError(
            f"role {role!r} routes to backend {backend_name!r}, which no layer "
            f"defines (defined backends: {known})"
        )
    effective = dict(backend)
    for key, value in overlay.items():
        if key in ("name", "backend", "by_spec_level"):
            continue
        effective[key] = value
    for key, value in level_overlay.items():
        if key == "backend":
            continue
        effective[key] = value
    return str(backend_name), effective


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
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CrewError(f"git {' '.join(args)} failed: {detail}")
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


def _tree_state(path: Path) -> dict[str, Any]:
    """Return the commit and working-tree state needed for a boundary check."""
    if not path.is_dir():
        return {
            "path": str(path),
            "available": False,
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
            "detail": detail,
        }
    entries = []
    for raw in (item for item in status.stdout.split(b"\0") if item):
        if len(raw) < 4 or raw[2:3] != b" ":
            continue
        entries.append({"code": os.fsdecode(raw[:2]), "path": os.fsdecode(raw[3:])})
    return {
        "path": str(path),
        "available": True,
        "head": os.fsdecode(head.stdout).strip(),
        "status_digest": "sha256:" + hashlib.sha256(status.stdout).hexdigest(),
        "status_entries": entries,
    }


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


def _inspect_workspace(
    repo: Path,
    path: Path,
    integrated_into: str,
    claimed_by: Iterable[str],
    shadow_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    dirty = _git(path, "status", "--porcelain").stdout.splitlines()
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
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
    if claims:
        classification = "live-referenced"
    elif shadow_record is not None and _shadow_patch_retained(shadow_record):
        classification = "disposable"
    elif dirty:
        classification = "dirty"
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


def shadow_worktree_session(primary_run_id: str, candidate_backend: str) -> str:
    """Return the session token a shadow worktree lives under.

    Named by its primary run and its candidate backend so several candidates can
    shadow one primary concurrently; the candidate is what stops a second shadow
    from colliding with the first. The dispatcher passes this same token when it
    provisions the worktree, so routing can reconstruct the location from a
    committed record without re-deriving the format by inspection.
    """
    return f"shadow-{primary_run_id}-{candidate_backend}"


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
        # A record that predates the candidate-named path (or never named one)
        # still resolves its single legacy worktree; only one candidate could
        # have produced a shadow before the candidate entered the path.
        session = (
            shadow_worktree_session(primary_run_id, candidate)
            if candidate
            else f"shadow-{primary_run_id}"
        )
        for root in roots:
            result[(root / session / node).resolve()] = record
    return result


# What `--apply` removes, and why each of the rest stays. Kept beside the removal
# branch so the report and the behaviour cannot drift: a classification named
# here as reclaimable must be one that branch acts on.
RECLAIMABLE_CLASSES = ("integrated", "disposable")
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


def garbage_collect(
    *,
    repo: str | Path,
    project: str | None = None,
    integrated_into: str = "HEAD",
    retention_days: int = 30,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect or remove disposable workspaces and promoted transient state."""
    if retention_days < 0:
        raise CrewError("retention days cannot be negative")
    repo_root = Path(repo).resolve()
    _git(repo_root, "rev-parse", "--verify", f"{integrated_into}^{{commit}}")
    roots = _workspace_roots(repo_root)
    claims = _live_worktree_claims()
    shadow_records = _shadow_worktree_records(repo_root, project)
    candidates = [
        path
        for path in _registered_worktrees(repo_root)
        if path != repo_root and any(path.is_relative_to(root) for root in roots)
    ]
    worktrees = [
        _inspect_workspace(
            repo_root,
            path,
            integrated_into,
            claims.get(path.resolve(), ()),
            shadow_records.get(path.resolve()),
        )
        for path in sorted(candidates)
    ]
    removed: list[str] = []
    if apply:
        for item in worktrees:
            if item["classification"] not in ("integrated", "disposable"):
                continue
            path = Path(item["path"])
            current_claims = _live_worktree_claims().get(path.resolve(), [])
            if current_claims:
                item["classification"] = "live-referenced"
                item["claimed_by_live_runs"] = sorted(current_claims)
                continue
            if item["classification"] == "disposable":
                shadow_record = shadow_records.get(path.resolve())
                if shadow_record is None or not _shadow_patch_retained(shadow_record):
                    item["classification"] = "unintegrated"
                    continue
                _git(repo_root, "worktree", "remove", "--force", str(path))
            else:
                _git(repo_root, "worktree", "remove", str(path))
            removed.append(str(path))
        _git(repo_root, "worktree", "prune")

    ledgered = {
        str(record.get("run_id") or "")
        for record in _ledgered_records(repo_root, project)
        if record.get("run_id")
    }
    pointer_reports: list[dict[str, Any]] = []
    for record in list_live():
        run_id = str(record.get("run_id") or "")
        if run_id not in ledgered or process_alive(record.get("pid")) is not False:
            continue
        report = {"run_id": run_id, "action": "reap", "removed": False}
        if apply:
            with _pointer_lock(run_id):
                current = read_pointer(run_id)
                if run_id in ledgered and process_alive(current.get("pid")) is False:
                    pointer_path(run_id).unlink()
                    report["removed"] = True
        pointer_reports.append(report)

    cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(days=retention_days)
    live_ids = {str(record.get("run_id") or "") for record in list_live()}
    run_reports: list[dict[str, Any]] = []
    runs_root = crew_home() / "runs"
    if runs_root.is_dir():
        for directory in sorted(path for path in runs_root.iterdir() if path.is_dir()):
            if directory.name not in ledgered or directory.name in live_ids:
                continue
            modified = datetime.fromtimestamp(
                directory.stat().st_mtime, tz=timezone.utc
            )
            if modified > cutoff:
                continue
            report = {
                "run_id": directory.name,
                "path": str(directory),
                "action": "prune",
                "removed": False,
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

    # `--apply` removes the integrated and the disposable, so a report whose
    # headline figure is `disposable` says 0 while it would in fact reclaim
    # dozens. A caller reading that concludes nothing is reclaimable and the
    # accumulation grows — measured at 46 worktrees in one project, 40 of them
    # integrated. So every row states whether it would be reclaimed, and a row
    # that would not says which condition holds it back.
    for item in worktrees:
        classification = str(item["classification"])
        item["reclaimable"] = classification in RECLAIMABLE_CLASSES
        if not item["reclaimable"]:
            item["withheld"] = WITHHELD_REASONS.get(
                classification, "unrecognised classification"
            )

    counts = {
        name: sum(item["classification"] == name for item in worktrees)
        for name in (
            "integrated",
            "disposable",
            "dirty",
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
    return {
        "dry_run": not apply,
        "repo": str(repo_root),
        "ledger": ledgers,
        "integrated_into": integrated_into,
        "counts": counts,
        "worktrees": worktrees,
        "removed_worktrees": removed,
        "pointers": pointer_reports,
        "run_directories": run_reports,
    }


def _fleet_script() -> Path:
    """Resolve the worktree fleet script from the running reckon installation.

    The script is repository-agnostic: it derives every path it touches from
    the ``--repo`` it is handed and from reckon's config home, and reads
    nothing relative to its own location. Requiring a copy inside each
    dispatched repository therefore bought no isolation and made dispatch
    depend on a per-repository file that nothing installs — so a repository
    that had never been hand-provisioned could not be dispatched into at all,
    which is the whole failure mode for a write repository named separately
    from the plan's. One resolved copy also keeps the script and the reckon
    that invokes it at the same version.
    """
    package_dir = Path(__file__).resolve().parent.parent
    candidates = (package_dir.parent / "skills", package_dir / "_skills")
    for candidate in candidates:
        script = candidate / "reckon-ship" / "scripts" / "worktree_fleet.py"
        if script.is_file():
            return script
    searched = ", ".join(str(path) for path in candidates)
    raise CrewError(
        "the reckon installation is missing its worktree fleet script; "
        f"searched: {searched}; reinstall reckon"
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
    """Undo a worktree created for a dispatch that then failed."""
    claims = _live_worktree_claims().get(Path(path).resolve(), [])
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


def _signal_process_group(pid: int, expected_start_time: str | None) -> None:
    """Signal a worker only while its pid still names the spawned process.

    A recorded pid is data from a run record, not a fact about who is calling
    this function — a test double, a stale carry-forward, or any record whose
    pid happens to equal the caller's own is otherwise indistinguishable from
    a genuine spawned worker. os.killpg signals the whole process group, so
    signalling one's own group takes the caller down with it. Refuse before
    that lookup rather than let the OS enforce it as a self-inflicted SIGTERM.
    """
    own_pid = os.getpid()
    if pid == own_pid or os.getpgid(pid) == os.getpgid(own_pid):
        raise CrewError(
            f"refusing to signal pid {pid}: it is this process's own pid or "
            "shares this process's own process group, and killpg would "
            "terminate the caller doing the releasing"
        )
    actual_start_time = _process_start_time(pid)
    if not expected_start_time or actual_start_time != expected_start_time:
        raise CrewError(
            f"refusing to signal pid {pid}: process identity changed "
            f"from {expected_start_time!r} to {actual_start_time!r}"
        )
    os.killpg(os.getpgid(pid), signal.SIGTERM)


def _base_commit(repo: Path, base: str) -> str:
    """Resolve a worktree base to a commit without accepting option-like refs."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise PlanVisibilityError(
            f"worktree base {base!r} is not a readable commit; commit the plan "
            "before dispatching"
        )
    return result.stdout.strip()


def _contains_plan_section(html_text: str, section: str) -> bool:
    """Return whether authored HTML exposes the requested section."""
    from bs4 import BeautifulSoup

    requested = re.sub(r"\s+", " ", section.strip())
    if not requested:
        return True
    requested_folded = requested.casefold()
    ids = {requested_folded.removeprefix("#")}
    numbered = re.fullmatch(r"§\s*([A-Za-z0-9._-]+)", requested)
    if numbered:
        ids.add(f"s{numbered.group(1)}".casefold())

    soup = BeautifulSoup(html_text, "html.parser")
    if any(
        str(tag.get("id") or "").casefold() in ids for tag in soup.find_all(id=True)
    ):
        return True
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).casefold()
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
            "inappropriate — hand-compose the delegation per reckon-ship "
            "references/sprint-orchestration.md §6, which keeps the worktree, "
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
    delivery_roots = (runs_dir().resolve(), reports_dir().resolve())
    for declared in node.write_paths:
        raw = Path(declared).expanduser()
        resolved = (raw if raw.is_absolute() else work_repo / raw).resolve()
        if not resolved.is_relative_to(work_repo) and not any(
            resolved.is_relative_to(root) for root in delivery_roots
        ):
            raise CrewError(
                f"write path {declared!r} resolves outside the authorised work "
                f"repository {work_repo} and Reckon delivery directories "
                f"{delivery_roots[0]} and {delivery_roots[1]}; declare a path "
                "inside one of them"
            )


def _agent_configuration(
    backend_name: str, launch_kind: str, backend: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the exact worker configuration persisted on a run record."""

    return {
        "backend": backend_name,
        "launch": launch_kind,
        "model": backend.get("model"),
        "effort": backend.get("effort"),
        "sandbox": backend.get("sandbox"),
    }


def _session_member_id(session: str) -> str:
    """Derive the private roster identity owned by one dispatching session."""
    digest = hashlib.sha256(str(session).encode()).hexdigest()[:20]
    return f"session-{digest}"


def _register_session_member(
    project: str,
    member_id: str,
    *,
    backend: str,
    role: str,
    root: Path,
    attempts: int = 12,
) -> dict[str, Any]:
    """Provision a session-owned member without losing a concurrent write."""
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
            )
        except ledger.LedgerError as exc:
            last = exc
    raise CrewError(
        f"could not provision session member {member_id!r} after {attempts} "
        f"attempts: {last}"
    )


def _parse_utc_timestamp(value: Any) -> datetime | None:
    """Return an aware UTC timestamp, or None for missing or malformed input."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
            ledger.write(project, data, version, root=root)
        except ledger.LedgerError:
            if attempt + 1 >= max(1, attempts):
                raise CrewError(
                    f"could not reap idle session members after {attempts} attempts"
                )
            continue
        return {"reaped": reaped, "idle_window": idle_window}
    return {"reaped": [], "idle_window": idle_window}


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

    from reckon.resources import resolve_resource

    resource = resolve_resource(
        repo / "docs", project, node.plan, "plan", include_archived=False
    )
    if resource is None:
        return None, "unavailable"
    value = _plan_html.parse_meta(resource.path).get("effort_hours")
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None, "unavailable"
    return (
        (hours, "plan-fallback")
        if math.isfinite(hours) and hours > 0
        else (None, "unavailable")
    )


def _competence_verdict(
    *,
    resolution: DispatchPlan,
    project: str,
    repo: Path,
) -> dict[str, Any]:
    """Compare a neutral node estimate with a neutral-size success horizon."""

    agent = _agent_configuration(
        resolution.backend, resolution.launch, resolution.backend_settings
    )
    key = agent_configuration_key({"agent": agent})
    plan_repo = repo
    if resolution.authority is not None:
        plan_repo = Path(resolution.authority["plan"]["repository"])
    estimated_hours, estimate_provenance = _estimated_hours(
        plan_repo, project, resolution.node
    )
    cache = capabilities.load_capabilities()
    cache_status = capabilities.project_cache_status(cache, project, root=repo)
    configuration = next(
        (
            item
            for item in cache.get("configurations", [])
            if isinstance(item, Mapping) and item.get("key") == key
        ),
        None,
    )
    horizon = configuration.get("competence_horizon_hours") if configuration else None
    try:
        horizon_hours = float(horizon)
    except (TypeError, ValueError):
        horizon_hours = 0.0

    verdict: dict[str, Any] = {
        "allowed": True,
        "agent_key": key,
        "estimated_hours": estimated_hours,
        "estimate_provenance": estimate_provenance,
        "cache_status": cache_status,
        "reason": "no-measured-horizon",
    }
    if cache_status == "stale":
        verdict["reason"] = "stale-capability-cache"
        return verdict
    if not math.isfinite(horizon_hours) or horizon_hours <= 0:
        return verdict
    if estimated_hours is None:
        verdict["reason"] = "no-estimated-hours"
        return verdict

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
        verdict["recommendation"] = (
            f"split into nodes no larger than {verdict['target_size_hours']} "
            "worker-hours for this agent configuration"
        )
    return verdict
