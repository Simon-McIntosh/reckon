"""Uniform worker dispatch — one call that names no harness.

A caller states what it wants worked on; this module resolves how. It validates
that a node is well-formed, resolves routing from flight config, cuts a detached
worktree, composes a fenced prompt, and then branches exactly once — on whether
the backend is a process reckon can spawn or the calling harness's own
delegation primitive, which it cannot. Everything else is shared, so no
execution path can drift from another by wording.

Three things earn their place here rather than in a skill file.

**The task-definition contract.** A worker that thrashes is almost always
executing a malformed task, so well-formedness is checked *before* dispatch
against seven properties (:func:`validate_node`). A node that fails is reshaped
or split; it is never sent in the hope that the worker will work it out. That
leaves the escape hatch handling only the genuine residual.

**Durable delivery.** A background worker can finish its work and end its turn
without delivering a report, at which point the runtime signals idle and the
node looks failed when it is not. So the manifest path is named at dispatch and
the file on disk is the delivery; the reply is a convenience.

**A run record that admits what it does not know.** Whatever budget signal a
backend emits is captured from the first dispatch onward, and a backend that
reports no headroom yields ``headroom: "unknown"`` rather than a guess. Acting
on the signal belongs to later work; having the history does not.

A run's record moves between two homes over its life. In flight it is a pointer
under the config home — pid, worktree, log, phase — which is worthless once the
run ends and is never committed. On completion :func:`complete` promotes it into
the owning repository's committed ledger (:mod:`reckon.ledger`) and deletes the
pointer, in that order, so an interruption leaves a recoverable pointer rather
than a lost record. :func:`recover` is what recovers it.

Atomicity is a contract of :func:`dispatch`: it performs the whole operation —
validate, resolve, worktree, prompt, launch, record — or it undoes what it did
and performs none of it. A half-dispatched node leaves an orphaned worktree
holding write scope, which is the one failure that costs another worker's work.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from reckon import _backends, _plan_html, capabilities, ledger
from reckon._store import _config_home
from reckon.calibration import agent_configuration_key

# The seven properties of the task-definition contract, in the order a reader of
# the plan meets them. Order is part of the contract: a node that is not scoped
# cannot be judged bounded.
NODE_PROPERTIES = (
    "single-goal",
    "fully-specified",
    "demonstrable",
    "closed",
    "scoped",
    "bounded",
    "independently-verifiable",
)

# The four fences a dispatch carries, and nothing else. The live plan stays the
# semantic authority; a copied brief becomes a second source of truth.
FENCES = ("scope", "time", "evidence", "delivery")

# A done-when built from one of these is an opinion, not a measure. The fix is
# to name what would be observed instead.
SUBJECTIVE_TERMS = (
    "clean",
    "robust",
    "better",
    "improved",
    "good",
    "nice",
    "tidy",
    "sensible",
    "reasonable",
    "appropriate",
    "elegant",
    "readable",
    "properly",
    "correctly",
)

# A conjunction starts a second deliverable only when it introduces another
# action. Nouns may be conjoined inside one deliverable without splitting it.
_DELIVERABLE_ACTIONS = frozenset(
    {
        "add",
        "build",
        "create",
        "delete",
        "deploy",
        "document",
        "fix",
        "implement",
        "land",
        "migrate",
        "publish",
        "remove",
        "rename",
        "replace",
        "resolve",
        "run",
        "ship",
        "stop",
        "update",
        "validate",
        "verify",
        "wire",
        "write",
    }
)
_NOUN_OR_ACTION_CONJUNCTIONS = (" and ", " & ", " plus ")
_DELIVERABLE_SEPARATORS = (" then ", ";")

# Text that shows a done-when names something observable rather than a feeling.
_EVIDENCE_SIGNALS = re.compile(
    r"\d|\btests?\b|\bpytest\b|\bexit\b|\breturns?\b|\bpasses\b|\bcommand\b"
    r"|\bgrep\b|\bstat\b|[/\\][\w.-]+",
    re.IGNORECASE,
)

_UNSPECIFIED = re.compile(
    r"\bTBD\b|\bTODO\b|\bFIXME\b|\?\?\?|\bdecide\b|\bfigure out\b|\bsomehow\b"
    r"|\bas appropriate\b|<[a-z-]+>",
    re.IGNORECASE,
)

_DURATION = re.compile(r"^(\d+)([smh])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _deliverable_conjunction(goal: str) -> str | None:
    """Return the separator that introduces another deliverable, if any."""
    lowered = f" {goal.lower()} "
    separator = next(
        (candidate for candidate in _DELIVERABLE_SEPARATORS if candidate in lowered),
        None,
    )
    if separator:
        return separator

    for candidate in _NOUN_OR_ACTION_CONJUNCTIONS:
        for remainder in lowered.split(candidate)[1:]:
            first_word = re.match(r"\s*([a-z]+)", remainder)
            if first_word and first_word.group(1) in _DELIVERABLE_ACTIONS:
                return candidate
    return None


# Recognised on the first line of a worker report. A stuck worker owes a
# decision brief, not a plea, so the four fields below are all required.
NEEDS_HELP_MARKER = "NEEDS-HELP:"
NEEDS_HELP_FIELDS = ("tried", "options", "leaning", "cost-if-wrong")

# The summary reflex: one habit, four axes, used at every occasion.
SUMMARY_AXES = ("WHAT", "WHY", "HOW", "WHEN")

# A resolved followup outcome may close the chain instead of naming the next
# step, but it has to say so — silence is the failure this recognises.
CHAIN_CLOSED_MARKERS = ("no followup", "no follow-up", "no-followup")


class CrewError(Exception):
    """A dispatch cannot proceed, and the message says what to fix."""


class BudgetHold(CrewError):
    """A wave is held on budget rather than failed.

    A distinct type because the two outcomes call for opposite responses: a
    malformed node is reshaped, while a held one is left exactly as it is and
    retried after the reported reset. Nothing was created, nothing failed, and
    the node stays ready — so a caller that cannot tell these apart either
    rewrites work that was fine or abandons work that was only waiting.
    """

    def __init__(self, verdict: Mapping[str, Any]) -> None:
        self.verdict = dict(verdict)
        super().__init__(
            f"wave held on budget for backend {verdict.get('backend')!r} — "
            f"{verdict.get('reason')}"
        )


class CompetenceLimit(CrewError):
    """A node must be split before this worker configuration can run it."""

    def __init__(self, verdict: Mapping[str, Any]) -> None:
        self.verdict = dict(verdict)
        super().__init__(
            f"node estimate {verdict['estimated_hours']} worker-hours exceeds "
            f"the {verdict['competence_horizon_hours']} worker-hour competence "
            f"horizon after speed adjustment; split into nodes no larger than "
            f"{verdict['target_size_hours']} worker-hours"
        )


class PlanVisibilityError(CrewError):
    """The worker's base ref cannot show the named plan section."""


# ── Node definition and the task contract ───────────────────────────────────


@dataclass
class TaskNode:
    """One dispatchable unit of work, before it is judged well-formed.

    The node carries no plan prose. ``plan`` and ``section`` point at the live
    plan, which owns context, decisions, evidence inputs and constraints; the
    node adds only what cannot live there — this run's scope, budget, measure
    and delivery path.
    """

    id: str
    goal: str
    plan: str
    section: str = ""
    role: str = "implement"
    done_when: str = ""
    write_paths: list[str] = field(default_factory=list)
    time_budget: str = ""
    manifest_path: str = ""
    requires_decisions: list[str] = field(default_factory=list)
    peer_scopes: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the node as sorted JSON-ready data."""
        return {
            "done_when": self.done_when,
            "goal": self.goal,
            "id": self.id,
            "manifest_path": self.manifest_path,
            "plan": self.plan,
            "requires_decisions": list(self.requires_decisions),
            "role": self.role,
            "section": self.section,
            "time_budget": self.time_budget,
            "write_paths": list(self.write_paths),
        }


@dataclass
class NodeValidation:
    """The verdict on one node, naming every property it failed."""

    ok: bool
    findings: list[dict[str, str]] = field(default_factory=list)

    @property
    def failed_properties(self) -> list[str]:
        return [finding["property"] for finding in self.findings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "properties": list(NODE_PROPERTIES),
            "findings": [dict(sorted(f.items())) for f in self.findings],
        }


def parse_duration(value: str) -> int:
    """Return a duration in seconds, or raise naming the accepted form."""
    match = _DURATION.match(str(value).strip())
    if not match:
        raise CrewError(f"duration {value!r} must be an integer followed by s, m or h")
    return int(match.group(1)) * _DURATION_SECONDS[match.group(2)]


def validate_node(
    node: TaskNode,
    *,
    locked_decisions: Iterable[str] = (),
    budget_ceiling: str = "",
) -> NodeValidation:
    """Judge a node against all seven properties, reporting every failure.

    Every property is reported rather than stopping at the first, because a
    caller reshaping a node wants the whole list in one pass. The verdict is
    advisory here and binding in :func:`dispatch`, which refuses to send a node
    that fails.
    """
    findings: list[dict[str, str]] = []
    locked = set(locked_decisions)

    def fail(prop: str, detail: str) -> None:
        findings.append({"property": prop, "detail": detail})

    goal = node.goal.strip()
    if not goal:
        fail("single-goal", "the node states no goal")
    else:
        hit = _deliverable_conjunction(goal)
        if hit:
            fail(
                "single-goal",
                f"the goal joins deliverables with {hit.strip()!r}; "
                "split it into one node per deliverable",
            )

    if not node.plan.strip():
        fail("fully-specified", "no plan is named as the semantic authority")
    unspecified = _UNSPECIFIED.search(f"{goal} {node.done_when}")
    if unspecified:
        fail(
            "fully-specified",
            f"{unspecified.group(0)!r} leaves the worker to infer intent; "
            "state the input or add a decision node before this one",
        )

    done_when = node.done_when.strip()
    if not done_when:
        fail("demonstrable", "no done-when measure is stated")
    else:
        subjective = [
            term
            for term in SUBJECTIVE_TERMS
            if re.search(rf"(?<!-)\b{re.escape(term)}\b", done_when, re.I)
        ]
        if subjective:
            fail(
                "demonstrable",
                f"done-when rests on the subjective term(s) "
                f"{', '.join(sorted(subjective))}; name what would be observed",
            )
        if not _EVIDENCE_SIGNALS.search(done_when):
            fail(
                "demonstrable",
                "done-when emits no evidence; name a test, a command output or "
                "a numeric result against a stated bound",
            )

    unlocked = [key for key in node.requires_decisions if key not in locked]
    if unlocked:
        fail(
            "closed",
            f"needs unlocked decision(s) {', '.join(sorted(unlocked))}; "
            "a decision node precedes this one",
        )

    if not node.write_paths:
        fail("scoped", "no exclusive write path is enumerated")
    else:
        mine = set(node.write_paths)
        for peer, paths in sorted(node.peer_scopes.items()):
            shared = sorted(mine.intersection(paths))
            if shared:
                fail(
                    "scoped",
                    f"shares {', '.join(shared)} with concurrent node {peer}; "
                    "serialise the nodes or split the file",
                )

    if not node.time_budget:
        fail("bounded", "no time budget is set")
    else:
        try:
            seconds = parse_duration(node.time_budget)
        except CrewError as exc:
            fail("bounded", str(exc))
        else:
            if budget_ceiling:
                ceiling = parse_duration(budget_ceiling)
                if seconds > ceiling:
                    fail(
                        "bounded",
                        f"budget {node.time_budget} exceeds the resolved fence "
                        f"{budget_ceiling}; split the work rather than overrun it",
                    )

    if not node.manifest_path:
        fail(
            "independently-verifiable",
            "no manifest path is named, so completion could only be judged by "
            "reading the implementation",
        )
    elif not Path(node.manifest_path).is_absolute():
        # A relative manifest path resolves against the worker's cwd, which is
        # its worktree — so the orchestrator, reading from elsewhere, finds
        # nothing, treats a delivered node as silent, and redispatches work that
        # already succeeded. That is the exact failure the delivery fence exists
        # to remove, so the path has to be absolute.
        fail(
            "independently-verifiable",
            f"manifest path {node.manifest_path!r} is relative; it would resolve "
            "against the worker's worktree and be invisible to the orchestrator",
        )

    return NodeValidation(ok=not findings, findings=findings)


# ── Routing ─────────────────────────────────────────────────────────────────


def resolve_role(config: Mapping[str, Any], role: str) -> tuple[str, dict[str, Any]]:
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
    backend_name = overlay.get("backend") or config.get("default_backend")
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
        if key in ("name", "backend"):
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
) -> dict[str, Any]:
    """Judge one backend's headroom for one purpose.

    Imported here rather than at module scope because the budget module reads run
    records through this one; deferring the import to call time keeps that a
    one-way dependency instead of a cycle.
    """
    from reckon import budget as budget_module

    recorded = budget_module.latest_recorded(project, root=root, config=config)
    state = budget_module.state_for(
        backend_name,
        backend,
        recorded=recorded.get(backend_name),
        unattributed=recorded.unattributed,
    )
    verdict = budget_module.decide(state, budget_module.policy(config), purpose=purpose)
    try:
        budget_module.record_checks(
            project,
            [verdict],
            root=root,
            resumption_fired=False,
        )
    except ledger.LedgerError as exc:
        raise CrewError(
            f"cannot record the budget check before opening the wave: {exc}"
        ) from exc
    return verdict


def resolved_time_budget(config: Mapping[str, Any], backend: Mapping[str, Any]) -> str:
    """Return the time budget a node is held to: backend first, fence second."""
    for candidate in (
        backend.get("time_budget"),
        (config.get("fences") or {}).get("time_budget"),
    ):
        if candidate:
            return str(candidate)
    return ""


# ── Run records ─────────────────────────────────────────────────────────────


def crew_home() -> Path:
    """Directory holding transient run state — never committed."""
    return _config_home() / "crew"


def live_dir() -> Path:
    """Directory of live pointers, one JSON file per in-flight run."""
    return crew_home() / "live"


def run_dir(run_id: str) -> Path:
    """Directory holding one run's prompt, event log and default manifest."""
    return crew_home() / "runs" / run_id


def pointer_path(run_id: str) -> Path:
    """Path of one run's live pointer."""
    return live_dir() / f"{run_id}.json"


def _utc_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def new_run_id(node_id: str, *, now: datetime | None = None) -> str:
    """Mint a filesystem-safe run id that sorts by dispatch time."""
    stamp = (now or datetime.now(tz=timezone.utc)).strftime("%Y%m%dT%H%M%S%f")
    token = re.sub(r"[^A-Za-z0-9._-]", "-", node_id).strip("-") or "node"
    return f"r-{stamp}-{token}"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically, so a reader never sees a half-written record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def read_pointer(run_id: str) -> dict[str, Any]:
    """Read one run's live pointer, or say which run is unknown."""
    path = pointer_path(run_id)
    if not path.exists():
        raise CrewError(f"no live run {run_id!r} (looked in {path})")
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise CrewError(f"live pointer for {run_id!r} is not valid JSON — {exc}")
    if not isinstance(data, dict):
        raise CrewError(f"live pointer for {run_id!r} does not hold an object")
    return data


def list_live() -> list[dict[str, Any]]:
    """Return every live pointer, newest run id last."""
    directory = live_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def process_alive(pid: Any) -> bool | None:
    """Report whether a pid is still running; None when there is no pid.

    A dead process with no terminal event in its log is a recoverable orphan
    rather than a completed run, which is why liveness is recorded beside the
    stream rather than inferred from it.
    """
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return None
    return True


# ── Prompt composition ──────────────────────────────────────────────────────


def compose_prompt(
    *,
    node: TaskNode,
    project: str,
    worktree: str,
    working_directory: str,
    manifest_path: str,
    time_budget: str,
    needs_help_after_failures: int,
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
) -> str:
    """Compose a worker prompt from the four fences and a pointer to the plan.

    Deliberately short. Anything the live plan already says is omitted, because
    a copied brief drifts between workers and sessions while the plan does not.
    The worker's first act is to read the plan and section named here.
    """
    peers = peer_scopes or {}
    peer_lines = (
        "\n".join(
            f"  {name} → {', '.join(sorted(paths))}"
            for name, paths in sorted(peers.items())
        )
        or "  none"
    )
    scope_lines = "\n".join(f"  {path}" for path in node.write_paths) or "  none"
    section = f" {node.section}" if node.section else ""
    delivery_directory_note = ""
    if Path(working_directory) != Path(worktree):
        delivery_directory_note = f"""
RUNTIME FILESYSTEM
  The working directory is the delivery directory {working_directory}.
  The repository at the assigned worktree path {worktree} is read-only.
"""
    return f"""You are a worker on one node. Read the live plan first; it is the
semantic authority for context, decisions, evidence inputs and constraints.

NODE     {node.id}
GOAL     {node.goal}
PLAN     {project}:{node.plan}{section}
ROLE     {node.role}
{delivery_directory_note}

FENCE — SCOPE (exclusive write paths; nothing outside them)
{scope_lines}

CONCURRENT NODES (never touch their paths; request a scope change instead)
{peer_lines}

FENCE — TIME
  {time_budget}. Exceeding it means stop and report, never push on.

FENCE — EVIDENCE (this measure is the done-when; state it quantitatively)
  {node.done_when}

FENCE — DELIVERY
  Write your manifest to {manifest_path} BEFORE finishing, then reply with
  that path and a short summary. Your final message is the return value, but
  the file is the delivery: a report that exists only in a message can be
  lost, and the node then looks failed when it is not. Long output belongs in
  the file. Redirect every long-running command to a named on-disk log.

MANIFEST (write exactly these keys)
  node: {node.id}
  status: complete | blocked | failed
  commits: <sha list>
  changed_paths: <explicit list>
  tests: <command and result>
  test_logs: <paths on disk>
  artifacts: <paths plus headline metrics>
  evidence_inputs: <facts the orchestrator needs for writeback>
  follow_ons: <work you found but were fenced out of, or none>
  blockers: <none, or the exact unmet condition>

WORKTREE AND PARALLEL-SAFETY RULES (binding)
  1. Work only in {worktree}. Do not create, checkout or switch branches.
  2. Never use git stash, rebase, clean, reset --hard, or path restoration.
  3. Stage explicit assigned paths only. Never git add -A/./*, commit -a/-am.
  4. Do not edit reckon plan or index state. Return outcome data instead.
  5. Commit locally with a conventional subject AND a body. Do not merge or
     push the primary branch.
  6. No AI attribution, and no plan, sprint or ticket identifiers in commit
     messages, symbol names, filenames or comments.
  7. Stop and report unexpected dirty files or unsafe scope.

IF YOU GET STUCK — stop and emit a report whose first line is
`{NEEDS_HELP_MARKER} <one line>` followed by all four of:
  tried:         what you attempted and the observable result
  options:       two or three concrete paths you can see
  leaning:       which one, and why
  cost-if-wrong: what must be redone if the wrong path is taken
Stop on any of: the same command failed {needs_help_after_failures} times with
different fixes attempted; a decision the plan does not settle is required;
the necessary change exceeds your write scope; the evidence cannot be produced
with the tools or data available; the time budget is spent with the measure
still unmet. Asking costs one turn; thrashing costs the node.
"""


# ── Dispatch ────────────────────────────────────────────────────────────────


def _create_worktree(
    repo: Path, session: str, worker: str, base: str
) -> dict[str, Any]:
    """Create a detached worktree through the fleet script, or raise."""
    script = repo / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py"
    if not script.is_file():
        raise CrewError(f"worktree fleet script is missing: {script}")
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
    *, node: TaskNode, project: str, repo: str | Path, base: str
) -> None:
    """Refuse when a named section is not identical and readable at ``base``."""
    if not node.section.strip():
        return

    from reckon.resources import ResourceCollision, resolve_resource

    repo_root = Path(repo).resolve()
    docs_dir = repo_root / "docs"
    if not docs_dir.is_dir() or not any(docs_dir.rglob("*.html")):
        # Dispatch remains usable before a repository adopts HTML plan authority.
        # Once it does, every named section must be visible from the worker base.
        return
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
        expected = Path("docs") / "plans" / f"{node.plan}.html"
        raise PlanVisibilityError(
            f"plan file {expected.as_posix()} is not readable in the working tree; "
            "commit the plan and named section before dispatching"
        )

    relative_path = resource.path.resolve().relative_to(repo_root)
    commit = _base_commit(repo_root, base)
    blob = subprocess.run(
        ["git", "show", f"{commit}:{relative_path.as_posix()}"],
        cwd=str(repo_root),
        capture_output=True,
        check=False,
    )
    if blob.returncode:
        raise PlanVisibilityError(
            f"plan file {relative_path.as_posix()} is not readable at base {base!r}; "
            "commit the plan and named section before dispatching"
        )

    working_bytes = resource.path.read_bytes()
    if working_bytes != blob.stdout:
        raise PlanVisibilityError(
            f"plan file {relative_path.as_posix()} differs from base {base!r}; "
            "commit the plan before dispatching"
        )
    base_html = blob.stdout.decode("utf-8", errors="replace")
    if not _contains_plan_section(base_html, node.section):
        raise PlanVisibilityError(
            f"plan file {relative_path.as_posix()} does not contain section "
            f"{node.section!r} at base {base!r}; commit the named section before "
            "dispatching"
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


def _plan_estimated_hours(repo: Path, project: str, node: TaskNode) -> float | None:
    """Read the node's neutral-hours estimate from its owning plan."""

    from reckon.resources import resolve_resource

    resource = resolve_resource(
        repo / "docs", project, node.plan, "plan", include_archived=False
    )
    if resource is None:
        return None
    value = _plan_html.parse_meta(resource.path).get("effort_hours")
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if math.isfinite(hours) and hours > 0 else None


def _competence_verdict(
    *,
    resolution: DispatchPlan,
    project: str,
    repo: Path,
) -> dict[str, Any]:
    """Compare plan size with the selected configuration's measured horizon."""

    agent = _agent_configuration(
        resolution.backend, resolution.launch, resolution.backend_settings
    )
    key = agent_configuration_key({"agent": agent})
    estimated_hours = _plan_estimated_hours(repo, project, resolution.node)
    cache = capabilities.load_capabilities()
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
        "reason": "no-measured-horizon",
    }
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

    adjusted_hours = estimated_hours / speed_factor
    target_size = horizon_hours * speed_factor
    verdict.update(
        {
            "allowed": adjusted_hours <= horizon_hours,
            "adjusted_hours": round(adjusted_hours, 6),
            "competence_horizon_hours": round(horizon_hours, 6),
            "reason": "within-competence-horizon"
            if adjusted_hours <= horizon_hours
            else "competence-horizon-exceeded",
            "speed_factor": round(speed_factor, 6),
            "target_size_hours": round(target_size, 6),
        }
    )
    if not verdict["allowed"]:
        verdict["recommendation"] = (
            f"split into nodes no larger than {verdict['target_size_hours']} "
            "worker-hours for this agent configuration"
        )
    return verdict


@dataclass
class DispatchPlan:
    """Everything a dispatch resolved, before anything on disk has changed.

    Separating resolution from effect is what lets a dry run be the *same*
    decision as a real dispatch rather than a second implementation of it: a
    caller can see the routing, the filled-in defaults and the verdict without
    a worktree or a process existing.
    """

    run_id: str
    backend: str
    launch: str
    backend_settings: dict[str, Any]
    node: TaskNode
    budget_ceiling: str
    validation: NodeValidation

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "launch": self.launch,
            "node": self.node.as_dict(),
            "run_id": self.run_id,
            "time_budget": self.node.time_budget,
            "validation": self.validation.as_dict(),
            "write_paths": list(self.node.write_paths),
        }


def plan_dispatch(
    *,
    node: TaskNode,
    config: Mapping[str, Any],
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    run_id: str | None = None,
    project: str = "",
    repo: str | Path | None = None,
    base: str = "HEAD",
) -> DispatchPlan:
    """Resolve routing and defaults for one node and judge it. No side effects.

    Mutates only the node it was handed, filling the defaults a dispatch would
    fill — the time budget from the resolved fence and the manifest path from
    the run directory — so the verdict is the one a real dispatch would reach.
    """
    if not _SAFE_ID.fullmatch(node.id):
        raise CrewError(f"node id {node.id!r} must match {_SAFE_ID.pattern}")
    backend_name, backend = resolve_role(config, node.role)
    launch_kind = backend.get("launch")
    if launch_kind not in ("cli", "in-harness"):
        raise CrewError(
            f"backend {backend_name!r} declares launch {launch_kind!r}; "
            "expected 'cli' or 'in-harness'"
        )
    budget_ceiling = resolved_time_budget(config, backend)
    node.time_budget = node.time_budget or budget_ceiling
    resolved_run_id = run_id or new_run_id(node.id)
    node.manifest_path = node.manifest_path or str(
        run_dir(resolved_run_id) / "manifest.md"
    )
    node.peer_scopes = {
        name: list(paths) for name, paths in (peer_scopes or {}).items()
    }
    verdict = validate_node(
        node, locked_decisions=locked_decisions, budget_ceiling=budget_ceiling
    )
    if verdict.ok and repo is not None:
        require_plan_section_visible(node=node, project=project, repo=repo, base=base)
    return DispatchPlan(
        run_id=resolved_run_id,
        backend=backend_name,
        launch=str(launch_kind),
        backend_settings=backend,
        node=node,
        budget_ceiling=budget_ceiling,
        validation=verdict,
    )


def dispatch(
    *,
    node: TaskNode,
    project: str,
    repo: str | Path,
    config: Mapping[str, Any],
    session: str,
    base: str = "HEAD",
    locked_decisions: Iterable[str] = (),
    peer_scopes: Mapping[str, Iterable[str]] | None = None,
    member: str = "",
    launcher=None,
    check_budget: bool = True,
) -> dict[str, Any]:
    """Validate, prepare and launch one node; return its run record.

    The single branch is on launch kind. A ``cli`` backend is spawned here and
    the caller yields on the returned run id. An ``in-harness`` backend cannot
    be spawned by reckon at all, so everything a worker needs is prepared and
    returned as a directive the calling harness dispatches itself, binding its
    task back with :func:`attach`.

    Naming a roster ``member`` routes the node into that member's long-lived
    session when the backend reuses sessions, so a repository's team accumulates
    context across nodes instead of rebuilding it every dispatch. A member whose
    session is still null gets one captured on its first run.

    A node whose backend has no headroom left is *held* rather than dispatched:
    :class:`BudgetHold` is raised before any worktree exists, so the node stays
    ready and nothing has to be judged or unwound. Holding costs nothing; a wave
    launched into a spent quota costs its whole setup plus half-finished commits.

    Either way the operation is atomic: a failure after the worktree exists
    removes it and writes no pointer, so no orphan is left holding write scope.
    """
    repo_root = Path(repo).resolve()
    resolution = plan_dispatch(
        node=node,
        config=config,
        locked_decisions=locked_decisions,
        peer_scopes=peer_scopes,
        project=project,
        repo=repo_root,
        base=base,
    )
    if not resolution.validation.ok:
        raise CrewError(
            "node is not dispatchable — "
            + "; ".join(
                f"{finding['property']}: {finding['detail']}"
                for finding in resolution.validation.findings
            )
        )

    competence = _competence_verdict(
        resolution=resolution,
        project=project,
        repo=repo_root,
    )
    if not competence["allowed"]:
        raise CompetenceLimit(competence)

    if check_budget:
        # Before the worktree, not after: a hold that had already cut a worktree
        # would leave write scope claimed by a node nobody is running.
        verdict = _budget_verdict(
            project=project,
            root=repo_root,
            config=config,
            backend_name=resolution.backend,
            backend=resolution.backend_settings,
            purpose="dispatch",
        )
        if verdict["held"]:
            raise BudgetHold(verdict)

    backend_name = resolution.backend
    backend = resolution.backend_settings
    launch_kind = resolution.launch
    run_id = resolution.run_id
    directory = run_dir(run_id)
    fences = config.get("fences") or {}
    peers = node.peer_scopes

    roster_member = None
    if member:
        roster_member = ledger.member(project, member, root=repo_root)
        if roster_member is None:
            raise CrewError(
                f"project {project!r} has no crew member {member!r}; register it "
                "with `reckon crew member add` before dispatching to it"
            )
    reuse_session = (
        str(roster_member.get("session_id"))
        if roster_member
        and roster_member.get("session_id")
        and backend.get("session_reuse")
        else None
    )

    worktree = _create_worktree(repo_root, session, node.id, base)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        working_directory = worktree["path"]
        if launch_kind == "cli":
            working_directory = _backends.launch_working_directory(
                backend=backend,
                worktree=worktree["path"],
                manifest_path=node.manifest_path,
            )
        prompt = compose_prompt(
            node=node,
            project=project,
            worktree=worktree["path"],
            working_directory=working_directory,
            manifest_path=node.manifest_path,
            time_budget=node.time_budget,
            needs_help_after_failures=int(fences.get("needs_help_after_failures", 2)),
            peer_scopes=peers,
        )
        prompt_path = directory / "prompt.txt"
        prompt_path.write_text(prompt)
        log_path = directory / "stream.jsonl"
        stderr_path = directory / "stderr.log"
        final_path = directory / "final.txt"

        record: dict[str, Any] = {
            "run_id": run_id,
            "project": project,
            "repo": str(repo_root),
            "session": session,
            "node": node.as_dict(),
            "role": node.role,
            "backend": backend_name,
            "launch": launch_kind,
            "sandbox": backend.get("sandbox"),
            "session_reuse": bool(backend.get("session_reuse")),
            "member": member,
            # The configuration that actually ran the node, recorded now because
            # a later config layer change makes it unreconstructable — and
            # without it a measured duration cannot be attributed to anything.
            "agent": _agent_configuration(backend_name, launch_kind, backend),
            "competence": competence,
            "worktree": worktree["path"],
            "base": worktree["base"],
            "base_sha": worktree["base_sha"],
            "prompt_path": str(prompt_path),
            "log_path": str(log_path),
            "stderr_path": str(stderr_path),
            "final_message_path": str(final_path),
            "manifest_path": node.manifest_path,
            "peer_scopes": {name: sorted(paths) for name, paths in peers.items()},
            "created_at": _utc_now(),
            "phase": "starting",
            "session_id": reuse_session,
            "task": None,
            "pid": None,
            "argv": None,
            "dialect": None,
            "budget": _backends.unknown_budget("no events yet"),
        }

        if launch_kind == "cli":
            plan = _backends.launch_plan(
                backend_name=backend_name,
                backend=backend,
                prompt=prompt,
                worktree=worktree["path"],
                manifest_path=node.manifest_path,
                final_message_path=str(final_path),
                resume_session=reuse_session,
            )
            spawn = launcher or _spawn
            pid = spawn(
                plan,
                log_path=log_path,
                stderr_path=stderr_path,
                prompt_path=prompt_path,
            )
            record.update(
                {"pid": pid, "argv": list(plan.argv), "dialect": plan.dialect}
            )
        else:
            record["directive"] = {
                "attach_with": f"reckon crew attach --run {run_id} --task <task-id>",
                "fences": {
                    "delivery": node.manifest_path,
                    "evidence": node.done_when,
                    "scope": list(node.write_paths),
                    "time": node.time_budget,
                },
                "prompt_path": str(prompt_path),
                "worktree": worktree["path"],
            }

        _write_json(pointer_path(run_id), record)
    except Exception:
        _remove_worktree(repo_root, worktree["path"])
        shutil.rmtree(directory, ignore_errors=True)
        pointer_path(run_id).unlink(missing_ok=True)
        raise
    return record


def _spawn(
    plan: _backends.LaunchPlan,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start the backend detached, with its event stream landing on disk.

    The prompt is fed from a file rather than a pipe so the caller never blocks
    on a full pipe buffer, and so the exact prompt stays recoverable beside the
    stream it produced. ``start_new_session`` detaches the worker from the
    caller's process group: a dispatching agent that ends its turn must not take
    its workers down with it.
    """
    with (
        open(prompt_path, "rb") as stdin,
        open(log_path, "wb") as stdout,
        open(stderr_path, "wb") as stderr,
    ):
        process = subprocess.Popen(
            plan.argv,
            cwd=plan.cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    return process.pid


def attach(run_id: str, task: str) -> dict[str, Any]:
    """Bind an in-harness dispatch to its live pointer.

    Reckon cannot spawn the calling harness's delegation primitive, so the
    harness dispatches its own task and reports the identity back here. That
    binding is what makes an in-harness run observable on the same surface as a
    spawned one.
    """
    record = read_pointer(run_id)
    if record.get("launch") != "in-harness":
        raise CrewError(
            f"run {run_id!r} is a {record.get('launch')!r} launch; attach binds "
            "an in-harness task, and a spawned run already has its pid"
        )
    if record.get("task"):
        raise CrewError(
            f"run {run_id!r} is already attached to task {record['task']!r}; "
            "a second binding would hide which worker holds the write scope"
        )
    if not str(task).strip():
        raise CrewError("attach requires a non-empty task identifier")
    record["task"] = str(task).strip()
    record["attached_at"] = _utc_now()
    record["phase"] = "working"
    _write_json(pointer_path(run_id), record)
    return record


def observe(run_id: str, *, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Fold a run's on-disk evidence back into its pointer and return it.

    Reads the event stream, the manifest path and process liveness, then writes
    the derived phase, session id and budget block into the record. Everything
    it reports is recoverable from disk, so a fresh session can observe a run it
    did not dispatch.
    """
    record = read_pointer(run_id)
    backend_name = str(record.get("backend") or "")
    manifest = Path(record.get("manifest_path") or "")
    record["manifest_present"] = manifest.is_file()
    record["process_alive"] = process_alive(record.get("pid"))
    record["observed_at"] = _utc_now()

    if record.get("launch") == "cli":
        backend = _backend_settings(record, config)
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=record.get("log_path", ""),
        )
        data = observation.as_dict()
        record["budget"] = data["budget"]
        record["events"] = data["events"]
        record["exit_status"] = data["exit_status"]
        record["final_message"] = data["final_message"]
        record["phase"] = data["phase"]
        record["session_id"] = data["session_id"] or record.get("session_id")
        if data["detail"]:
            record["detail"] = data["detail"]
        final_file = Path(record.get("final_message_path") or "")
        if not record["final_message"] and final_file.is_file():
            record["final_message"] = final_file.read_text().strip() or None
        if (
            data["phase"] in ("starting", "working")
            and record["process_alive"] is False
        ):
            # A dead process with no terminal event is a recoverable orphan, not
            # a finished run; saying so is what stops it being read as complete.
            # An empty log counts: a launch that failed on its arguments exits
            # before writing an event, and reporting that as "starting" would
            # leave it waiting forever for a worker that never began.
            record["phase"] = "orphaned"
            record["detail"] = (
                "process exited without a terminal event in its log; "
                f"check {record.get('stderr_path')}"
            )
    elif record.get("task") and record["manifest_present"]:
        record["phase"] = "complete"

    capture = _capture_member_session(record)
    if capture is not None:
        record["session_capture"] = capture

    _write_json(pointer_path(run_id), record)
    return record


def _capture_member_session(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Persist a run's session id onto its roster member, if it has one.

    Observation is where a backend's session id first becomes knowable, so it is
    also where the roster learns it — waiting for completion would leave a second
    node dispatched in the meantime unable to reach the same session.
    """
    member = record.get("member")
    session_id = record.get("session_id")
    if not member or not session_id:
        return None
    try:
        return ledger.capture_session(
            str(record.get("project") or ""),
            str(member),
            str(session_id),
            root=record.get("repo"),
        )
    except (ledger.LedgerError, OSError) as exc:
        # The record being promoted carries the session id anyway, so a roster
        # write that cannot happen must not fail the promotion around it.
        return {"captured": False, "member": None, "detail": str(exc)}


def _backend_settings(
    record: Mapping[str, Any], config: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Recover the backend settings needed to read a run's stream.

    Only the command matters for reading, and the recorded argv already holds
    it, so a run stays observable after its config layer changes — which is the
    difference between a durable record and one that decays.
    """
    argv = record.get("argv")
    if isinstance(argv, list) and argv:
        return {"launch": "cli", "command": argv[0]}
    backends = (config or {}).get("backends") or {}
    backend = backends.get(record.get("backend"))
    if isinstance(backend, Mapping):
        return dict(backend)
    raise CrewError(
        f"run {record.get('run_id')!r} records no argv and its backend is not "
        "in the supplied config, so its stream cannot be read"
    )


def resume_plan(
    run_id: str,
    advice: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> _backends.LaunchPlan:
    """Build the invocation that answers a stuck worker in its own session.

    Session reuse is load-bearing rather than an optimisation: the advice only
    makes sense to a worker that still remembers what it tried, so the resumed
    turn must carry the prior context rather than restate it.

    A resumption is judged against the full ceiling rather than the reserved
    portion, because answering a stuck worker is the expenditure the reserve was
    withheld for. It is still held at a genuinely spent quota — a resume into one
    fails anyway, and reporting the reset time is more use than the rejection.
    """
    record = read_pointer(run_id)
    if record.get("launch") != "cli":
        raise CrewError(f"run {run_id!r} is not a spawned run; resume it in-harness")
    session_id = record.get("session_id")
    if not session_id:
        raise CrewError(
            f"run {run_id!r} has no captured session id yet; observe it first"
        )
    backend = _backend_settings(record, config)
    verdict = _budget_verdict(
        project=str(record.get("project") or ""),
        root=record.get("repo"),
        config=config,
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        purpose="resume",
    )
    if verdict["held"]:
        raise BudgetHold(verdict)
    backend.setdefault("sandbox", record.get("sandbox"))
    return _backends.launch_plan(
        backend_name=str(record.get("backend") or ""),
        backend=backend,
        prompt=advice,
        worktree=str(record.get("worktree") or "."),
        manifest_path=str(record.get("manifest_path") or ""),
        resume_session=str(session_id),
    )


def terminate(run_id: str) -> dict[str, Any]:
    """Signal a spawned run's process group to stop, and record that."""
    record = read_pointer(run_id)
    pid = record.get("pid")
    if not pid:
        raise CrewError(f"run {run_id!r} has no process to stop")
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        record["detail"] = f"could not signal pid {pid} — {exc}"
    else:
        record["detail"] = f"SIGTERM sent to process group of pid {pid}"
    record["phase"] = "stopped"
    record["stopped_at"] = _utc_now()
    _write_json(pointer_path(run_id), record)
    return record


# ── Promotion: the transient record becomes committed evidence ──────────────


def scoped_diff_stat(
    *,
    cwd: str | Path,
    base: str,
    head: str = "HEAD",
    paths: Iterable[str] = (),
) -> dict[str, int] | None:
    """Count the lines a run changed inside its own write scope.

    Measured against the node's exclusive paths rather than the whole diff, so
    the number describes the node rather than whatever else the branch carried.
    An unmeasurable diff is an explicit absence. Command diagnostics are not
    measurements and must never enter the durable numeric field.
    """
    if not base:
        return None
    argv = ["git", "diff", "--numstat", f"{base}..{head}"]
    if paths:
        argv += ["--", *[str(path) for path in paths]]
    result = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode:
        return None
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


def _require_resolvable_commits(cwd: Path, commits: Iterable[str]) -> None:
    """Refuse commit values that Git cannot resolve to a commit object."""
    for revision in commits:
        result = subprocess.run(
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
        if result.returncode:
            raise CrewError(f"commit {revision!r} is not a resolvable revision")


def _elapsed_seconds(start: Any, end: Any) -> int | None:
    """Return whole seconds between two ISO-8601 stamps, or None."""
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0, int((last - first).total_seconds()))


def _terminal_stream_data(
    record: Mapping[str, Any],
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Resolve completion from events, then stream mtimes, across all turns."""
    budget = dict(record.get("budget") or {})
    if record.get("launch") != "cli":
        return None, None, budget

    backend_name = str(record.get("backend") or "")
    backend = _backend_settings(record, None)
    path = Path(str(record.get("log_path") or ""))
    paths = [path, *sorted(path.parent.glob("resume-*.jsonl"))]
    paths = [candidate for candidate in paths if candidate.is_file()]
    if not paths:
        return None, None, budget

    timestamps: list[tuple[datetime, str]] = []
    for candidate in paths:
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=candidate,
        )
        if observation.terminal:
            budget = dict(observation.budget)
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
        return max(timestamps, key=lambda item: item[0])[1], "terminal_event", budget

    newest = max(candidate.stat().st_mtime for candidate in paths)
    completed = (
        datetime.fromtimestamp(newest, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return completed, "stream_mtime", budget


def complete(
    run_id: str,
    *,
    gate: str,
    commits: Iterable[str] = (),
    outcome: str = "",
    tests_added: int | None = None,
    scope_changed: bool = False,
    changed_lines: Mapping[str, Any] | None = None,
    completed_at: str = "",
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Promote a finished run into the owning repository's committed ledger.

    The ledger append happens first and the pointer is deleted second. That
    order is the whole recovery story: an interruption between the two leaves a
    pointer :func:`recover` classifies as completed-but-unpromoted, whereas the
    reverse order would lose the record outright.

    Worker-time is measured from dispatch to the newest event timestamp across
    the run's streams, then their newest last-write time. Promotion time is an
    explicit fallback when no stream survives.
    """
    record = read_pointer(run_id)
    project = str(record.get("project") or "")
    node = record.get("node") or {}
    terminal_time, terminal_source, terminal_budget = _terminal_stream_data(record)
    if completed_at:
        finished = completed_at
        completion_source = "provided"
    elif terminal_time:
        finished = terminal_time
        completion_source = terminal_source or "terminal_event"
    else:
        finished = _utc_now()
        completion_source = "promotion_time"
    ledger_root = root if root is not None else record.get("repo")

    commit_list = [str(sha) for sha in commits if str(sha).strip()]
    worktree = Path(str(record.get("worktree") or ""))
    tree = worktree if worktree.is_dir() else Path(str(record.get("repo") or "."))
    _require_resolvable_commits(tree, commit_list)
    if changed_lines is None:
        changed_lines = (
            scoped_diff_stat(
                cwd=tree,
                base=str(record.get("base_sha") or ""),
                head=commit_list[-1],
                paths=node.get("write_paths") or (),
            )
            if commit_list
            else None
        )

    run = ledger.build_record(
        run_id=run_id,
        plan=str(node.get("plan") or ""),
        section=str(node.get("section") or ""),
        node=str(node.get("id") or ""),
        role=str(record.get("role") or ""),
        member_id=str(record.get("member") or ""),
        backend=str(
            record.get("backend") or (record.get("agent") or {}).get("backend") or ""
        ),
        agent=record.get("agent") or {},
        dispatched_at=str(record.get("created_at") or ""),
        completed_at=finished,
        completed_at_source=completion_source,
        worker_seconds=_elapsed_seconds(record.get("created_at"), finished),
        time_budget=str(node.get("time_budget") or ""),
        base_sha=str(record.get("base_sha") or ""),
        commits=commit_list,
        changed_lines=changed_lines,
        tests_added=tests_added,
        gate=gate,
        outcome=outcome,
        manifest_path=str(record.get("manifest_path") or ""),
        scope_changed=scope_changed,
        session_id=record.get("session_id"),
        budget=terminal_budget,
    )
    written = ledger.append_run(project, run, root=ledger_root)

    # The session id lives only in the pointer until it reaches the roster, so
    # it has to be captured before the pointer goes.
    capture = _capture_member_session(record)
    pointer_path(run_id).unlink(missing_ok=True)
    return {
        "run_id": run_id,
        "project": project,
        "ledger_path": written["path"],
        "ledger_version": written["version"],
        "pointer_removed": not pointer_path(run_id).exists(),
        "record": run,
        "session_capture": capture,
    }


# ── Recovery: what an interrupted orchestrator left behind ───────────────────

# What a live pointer can be once nobody is watching it. Worker-reported
# blocked and failed outcomes remain distinct so neither can be mistaken for a
# completed delivery that is eligible for promotion.
RECOVERY_CLASSES = (
    "running",
    "completed_unpromoted",
    "blocked",
    "failed",
    "abandoned",
)

# How long a log may go quiet before its freshness is reported as stale. Only
# ever reported beside the classification — a slow worker is not a dead one.
LOG_STALE_AFTER_SECONDS = 900


def classify_pointer(
    record: Mapping[str, Any],
    *,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Classify one live pointer, without touching it.

    Pure and read-only, so the same judgement serves an MCP read and
    :func:`recover`. Liveness comes from the process table and delivery from the
    manifest's status, because a terminal stream event only says the worker's
    turn ended. It does not say the node completed successfully.
    """
    run_id = str(record.get("run_id") or "")
    phase = str(record.get("phase") or "")
    manifest = Path(str(record.get("manifest_path") or ""))
    manifest_present = manifest.is_file()
    manifest_data: dict[str, Any] = {}
    manifest_error = ""
    if manifest_present:
        try:
            manifest_data = parse_manifest(manifest.read_text())
        except OSError as exc:
            manifest_error = str(exc)
    manifest_status = str(manifest_data.get("status") or "").strip().lower()
    manifest_commits = list(manifest_data.get("commits") or [])
    manifest_blockers = list(manifest_data.get("blockers") or [])
    needs_help = manifest_data.get("needs_help")
    alive = record.get("process_alive")
    if alive is None and record.get("pid"):
        alive = process_alive(record.get("pid"))
    log = Path(str(record.get("log_path") or ""))
    age = None
    if log.is_file():
        age = max(0, int(_utc_seconds() - log.stat().st_mtime))
    terminal = phase in ("complete", "failed")

    if manifest_status == "complete":
        classification = "completed_unpromoted"
        detail = (
            "the worker manifest reports completion and the run is still a "
            "pointer; promoting it moves the delivered record into the repository ledger"
        )
        action = f"reckon crew complete --run {run_id} --gate <verdict>"
        action += "".join(f" --commit {commit}" for commit in manifest_commits)
    elif manifest_status == "blocked":
        classification = "blocked"
        blocker = "; ".join(manifest_blockers) or "the manifest reports a blocker"
        detail = f"the worker manifest reports blocked: {blocker}"
        if isinstance(needs_help, Mapping) and needs_help.get("complete"):
            action = f"reckon crew resume --run {run_id} --advice <answer>"
        else:
            action = f"read {manifest}; resolve the blocker before resuming the run"
    elif manifest_status == "failed":
        classification = "failed"
        failure = "; ".join(manifest_blockers) or "the worker manifest reports failure"
        detail = f"the worker manifest reports failed: {failure}"
        action = (
            f"read {manifest} and launch log {record.get('stderr_path')}; "
            "repair or redispatch the run"
        )
    elif terminal:
        classification = "abandoned"
        if not manifest_present:
            delivery = "no manifest was delivered"
        elif manifest_error:
            delivery = f"the manifest could not be read: {manifest_error}"
        else:
            delivery = f"the manifest status {manifest_status!r} is not usable"
        detail = (
            f"the stored phase is terminal but {delivery}; nothing is eligible "
            "for promotion"
        )
        action = (
            f"read launch log {record.get('stderr_path')}; inspect the worktree at "
            f"{record.get('worktree')} and redispatch if needed"
        )
    elif alive is True:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif alive is False:
        classification = "abandoned"
        detail = (
            "the process is gone without a complete manifest; nothing is eligible "
            "for promotion"
        )
        action = (
            f"read launch log {record.get('stderr_path')}; the worktree at "
            f"{record.get('worktree')} is left in place for review and is never "
            "force-removed"
        )
    else:
        classification = "running"
        detail = (
            "an in-harness run: liveness belongs to the calling harness, so it "
            "is reported as running until a manifest appears"
        )
        action = f"reckon crew observe --run {run_id}"

    return {
        "run_id": run_id,
        "project": record.get("project"),
        "plan": (record.get("node") or {}).get("plan"),
        "node": (record.get("node") or {}).get("id"),
        "classification": classification,
        "phase": phase,
        "process_alive": alive,
        "manifest_present": manifest_present,
        "manifest_path": str(manifest) if str(manifest) != "." else "",
        "manifest_status": manifest_status or None,
        "manifest_commits": manifest_commits,
        "log_age_seconds": age,
        "log_fresh": None if age is None else age <= stale_after_seconds,
        "worktree": record.get("worktree"),
        "detail": detail,
        "next_action": action,
    }


def _utc_seconds() -> float:
    """Current time as epoch seconds, matching a file mtime's clock."""
    return datetime.now(tz=timezone.utc).timestamp()


def recover(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify every live pointer, repairing the record and nothing else.

    Each pointer is re-observed first, so the classification rests on the
    current stream and process table rather than on whatever the last writer
    believed. What gets repaired is the *record*: no worktree is removed, no
    process is signalled, and no run is promoted on this command's initiative —
    a completed-but-unpromoted run is reported with its manifest path so the
    orchestrator can promote it deliberately.
    """
    reports = []
    for pointer in list_live():
        if project and str(pointer.get("project") or "") != project:
            continue
        run_id = str(pointer.get("run_id") or "")
        observed: Mapping[str, Any] = pointer
        unreadable = ""
        if run_id:
            try:
                observed = observe(run_id, config=config)
            except CrewError as exc:
                unreadable = str(exc)
        report = classify_pointer(observed)
        if unreadable:
            report["detail"] = f"{report['detail']} (stream unreadable — {unreadable})"
        reports.append(report)
    counts = {
        name: sum(1 for item in reports if item["classification"] == name)
        for name in ("running", "completed_unpromoted", "abandoned")
    }
    for name in ("blocked", "failed"):
        count = sum(1 for item in reports if item["classification"] == name)
        if count:
            counts[name] = count
    return {"runs": reports, "counts": counts, "classes": list(RECOVERY_CLASSES)}


# ── Worker reports ──────────────────────────────────────────────────────────

_MANIFEST_LIST_KEYS = (
    "commits",
    "changed_paths",
    "test_logs",
    "artifacts",
    "evidence_inputs",
    "follow_ons",
    "blockers",
)
_NONE_VALUES = {"", "none", "n/a", "-", "nil"}


def parse_manifest(text: str) -> dict[str, Any]:
    """Parse a worker manifest into structured fields.

    Tolerant on purpose: a worker writes prose around its manifest and a strict
    parser would reject a delivered report over formatting. Unknown keys are
    kept so nothing a worker took the trouble to state is silently dropped.
    """
    fields: dict[str, Any] = {}
    key = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^([a-z][a-z0-9_-]*)\s*:\s*(.*)$", line, re.IGNORECASE)
        if match:
            key = match.group(1).lower().replace("-", "_")
            fields[key] = match.group(2).strip()
        elif key and line.startswith(("-", "*")):
            addition = line.lstrip("-* ").strip()
            fields[key] = f"{fields[key]}, {addition}" if fields[key] else addition
    for name in _MANIFEST_LIST_KEYS:
        fields[name] = _as_list(fields.get(name))
    fields["needs_help"] = parse_needs_help(text) if NEEDS_HELP_MARKER in text else None
    return fields


def _as_list(value: Any) -> list[str]:
    """Split a manifest field into items, treating explicit nothing as empty."""
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = [part.strip() for part in re.split(r"[,\n]", str(value))]
    return [item for item in items if item and item.lower() not in _NONE_VALUES]


def parse_needs_help(text: str) -> dict[str, Any]:
    """Parse an escape-hatch report, naming any of the four fields missing.

    A vague "I'm stuck" wastes as much time as thrashing, so the four fields are
    required: together they turn a plea into a decision brief the orchestrator
    can answer in one turn.
    """
    lines = text.splitlines()
    headline = ""
    for line in lines:
        if NEEDS_HELP_MARKER in line:
            headline = line.split(NEEDS_HELP_MARKER, 1)[1].strip()
            break
    fields: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        match = re.match(
            r"^(tried|options|leaning|cost-if-wrong)\s*:\s*(.*)$", stripped, re.I
        )
        if match:
            current = match.group(1).lower()
            fields[current] = match.group(2).strip()
        elif current and stripped:
            fields[current] = f"{fields[current]} {stripped}".strip()
    missing = [name for name in NEEDS_HELP_FIELDS if not fields.get(name)]
    return {
        "headline": headline,
        "fields": {name: fields.get(name, "") for name in NEEDS_HELP_FIELDS},
        "missing": missing,
        "complete": not missing and bool(headline),
    }


def audit_manifest(text: str, node: TaskNode | None = None) -> dict[str, Any]:
    """Judge a delivered manifest: is it complete, and does it stay in scope?"""
    manifest = parse_manifest(text)
    findings: list[str] = []
    status = str(manifest.get("status", "")).lower()
    if status not in ("complete", "blocked", "failed"):
        findings.append(f"status {status!r} is not complete, blocked or failed")
    if status == "complete" and not manifest["commits"]:
        findings.append("status is complete but no commit is recorded")
    if status == "complete" and not manifest.get("tests"):
        findings.append("status is complete but no test result is recorded")
    if node is not None and manifest["changed_paths"]:
        allowed = set(node.write_paths)
        stray = sorted(
            path for path in manifest["changed_paths"] if path not in allowed
        )
        if stray:
            findings.append(
                "changed paths outside the write scope: " + ", ".join(stray)
            )
    return {"manifest": manifest, "findings": findings, "ok": not findings}


def followup_ops_from_manifest(
    text: str,
    *,
    slug: str,
    section: str = "",
    written_by: str = "reckon-ship",
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Turn a manifest's candidate follow-ons into plan followup append ops.

    This is the worker end of the continuation chain. A worker fenced out of
    work it discovered has nowhere to put it but prose, where it is lost; an op
    per candidate carries it into plan state, and the one-line invocation keeps
    the live plan as the only place guidance lives.
    """
    manifest = parse_manifest(text)
    stamp = now or _utc_now()
    invocation = f"/reckon-ship {slug}" + (f" {section}" if section else "")
    ops: list[dict[str, Any]] = []
    for index, candidate in enumerate(manifest["follow_ons"], start=1):
        ops.append(
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": f"f-{re.sub(r'[^a-z0-9]+', '-', slug.lower())}-{stamp.replace(':', '').replace('-', '')}-{index}",
                    "status": "open",
                    "written_by": written_by,
                    "written_at": stamp,
                    "title": candidate[:120],
                    "body": (
                        f"<p>Found by a worker on {slug} and fenced out of its "
                        f"write scope: {candidate}</p>"
                    ),
                    "recommends_skill": invocation,
                    "prompt": invocation,
                },
            }
        )
    return ops


# ── The summary reflex ──────────────────────────────────────────────────────


def validate_summary(text: str, *, occasion: str) -> dict[str, Any]:
    """Check a four-axis summary, and that a reporting one carries evidence.

    One discipline binds the reflex to the gating reflex and is why the format
    earns its place: at completion, WHY carries the gate evidence. That forces
    every wave report to be quantitative, and makes a wave that cannot state its
    measure visibly incomplete rather than plausibly done. A hold is held to the
    same standard, because "we are out of budget" without a figure and a reset
    time is not a report a lead can act on.
    """
    axes: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^(WHAT|WHY|HOW|WHEN)\b\s*(.*)$", line)
        if match:
            current = match.group(1)
            axes.setdefault(current, [])
            if match.group(2).strip():
                axes[current].append(match.group(2).strip())
        elif current and line:
            axes[current].append(line)
    findings = [
        f"axis {axis} is missing" for axis in SUMMARY_AXES if not axes.get(axis)
    ]
    findings += [
        f"axis {axis} runs to {len(lines)} lines; at most two"
        for axis, lines in sorted(axes.items())
        if len(lines) > 2
    ]
    if occasion in ("completion", "hold"):
        why = " ".join(axes.get("WHY", []))
        if not re.search(r"\d", why):
            findings.append(
                f"{occasion} WHY carries no quantitative evidence; state the "
                "measure and its value"
            )
    return {
        "ok": not findings,
        "axes": {k: list(v) for k, v in sorted(axes.items())},
        "findings": findings,
    }
