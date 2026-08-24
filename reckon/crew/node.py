from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from reckon import ledger

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

_TERMINAL_RUN_PHASES = frozenset({"complete", "failed", "stopped"})

# A live pointer may remain at session closure only when another session owns
# its reconciliation, or while its worker is verifiably still running. Keeping
# this vocabulary closed makes an unexplained pointer fail closed instead of
# accepting arbitrary prose as proof that somebody owns it.
RUN_DRAIN_DISPOSITIONS = ("handed-off", "still-working")

# Session-owned roster rows are a short-lived index over durable run records.
# The default leaves enough time for an ordinary follow-up to reuse a warm
# worker while bounding growth when a caller does not supply a narrower policy.
DEFAULT_MEMBER_IDLE_WINDOW = "24h"

# A fleet watcher reports a quiet stream after the same fifteen-minute window
# used by the live view's freshness field. The command accepts a narrower or
# wider duration when a host needs a different wake cadence.
LOG_STALE_AFTER_SECONDS = 900
DEFAULT_WATCH_STALL_WINDOW = "15m"

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
    r"\bTBD\b|\bTODO\b|\bFIXME\b|\?\?\?|\bfigure out\b|\bsomehow\b"
    r"|\bas appropriate\b|<[a-z-]+>",
    re.IGNORECASE,
)

# ``decide`` is only unspecified intent when the node hands the choice to the
# WORKER. Describing machinery that decides something ("the resolver decides
# whether the relation reproduces its unit") is a specification, not a gap, so
# matching the bare verb refused well-formed nodes while missing "decides".
_DECISION_DEFERRED = re.compile(
    r"\b(?:you|worker|agent|implementer)\s+(?:\w+\s+){0,2}?decides?\b"
    r"|\bdecides?\s+(?:as\s+appropriate|for\s+yourself|at\s+your\s+discretion)\b"
    r"|(?:^|[.;]\s*)decide\b",
    re.IGNORECASE,
)

# A flagged adjective is the MEASURE only when it completes a copula: "is clean",
# "reads better", "is correctly formatted". The same word qualifying an input
# beside a counted result ("a correctly spelled property produces no row") leaves
# the measure intact, and refusing it teaches wording rather than measurability.
_SUBJECTIVE_PREDICATE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|look|looks|feel|feels|seem|seems"
    r"|read|reads)\s+(?:\w+\s+){0,1}?(?:"
    + "|".join(re.escape(term) for term in SUBJECTIVE_TERMS)
    + r")\b",
    re.IGNORECASE,
)

_DURATION = re.compile(r"^(\d+)([smh])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600}
STALL_BUDGET_MULTIPLE = 2

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


class MemberInFlight(CrewError):
    """A roster member already owns a live, non-terminal run."""

    def __init__(self, member: str, run_id: str) -> None:
        self.member = member
        self.run_id = run_id
        super().__init__(
            f"crew member {member!r} already holds in-flight run {run_id!r}"
        )


class ScopeConflict(CrewError):
    """A live run already claims a containing or contained write path."""

    def __init__(
        self,
        *,
        run_id: str,
        node_id: str,
        candidate_path: str,
        claimed_path: str,
    ) -> None:
        self.run_id = run_id
        self.node_id = node_id
        self.candidate_path = candidate_path
        self.claimed_path = claimed_path
        super().__init__(
            f"write scope {candidate_path!r} conflicts with live claim "
            f"{claimed_path!r} held by run {run_id!r} (node {node_id!r})"
        )


class UnreconciledRuns(CrewError):
    """Finished worker records must be reconciled before more work starts."""

    def __init__(self, runs: Iterable[Mapping[str, Any]], grace: str) -> None:
        self.runs = [dict(run) for run in runs]
        self.grace = grace
        actions = "\n".join(
            f"- {run['run_id']}: {run['next_action']}" for run in self.runs
        )
        super().__init__(
            f"project has {len(self.runs)} unreconciled run(s) older than the "
            f"{grace} grace:\n{actions}\n"
            "reconcile each run, or pass --allow-unreconciled-runs to record "
            "an explicit waiver on the new run"
        )


class WatcherRequired(CrewError):
    """A dispatch needs a live project watcher before work may start."""

    def __init__(self, project: str, watch: Mapping[str, Any]) -> None:
        self.project = project
        self.watch = dict(watch)
        super().__init__(
            f"project {project!r} has no live crew watcher; arm one with "
            f"`{watch['arming_line']}`, or pass --no-watch to record an "
            "explicit waiver for a synchronous dispatch"
        )


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
    spec_level: str = ""
    done_when: str = ""
    write_paths: list[str] = field(default_factory=list)
    time_budget: str = ""
    manifest_path: str = ""
    estimated_hours: float | None = None
    requires_decisions: list[str] = field(default_factory=list)
    peer_scopes: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the node as sorted JSON-ready data."""
        return {
            "done_when": self.done_when,
            "goal": self.goal,
            "id": self.id,
            "manifest_path": self.manifest_path,
            "estimated_hours": self.estimated_hours,
            "plan": self.plan,
            "requires_decisions": list(self.requires_decisions),
            "role": self.role,
            "section": self.section,
            "spec_level": self.spec_level,
            "time_budget": self.time_budget,
            "write_paths": list(self.write_paths),
        }


def normalize_section(value: str) -> str:
    """Return the canonical spelling for a numbered plan section."""
    return ledger.normalize_section(value)


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
    combined = f"{goal} {node.done_when}"
    unspecified = _UNSPECIFIED.search(combined) or _DECISION_DEFERRED.search(combined)
    if unspecified:
        fail(
            "fully-specified",
            f"{unspecified.group(0).strip()!r} leaves the worker to infer intent; "
            "state the input or add a decision node before this one",
        )

    done_when = node.done_when.strip()
    if not done_when:
        fail("demonstrable", "no done-when measure is stated")
    else:
        predicate = _SUBJECTIVE_PREDICATE.search(done_when)
        if predicate:
            present = sorted(
                term
                for term in SUBJECTIVE_TERMS
                if re.search(rf"(?<!-)\b{re.escape(term)}\b", done_when, re.I)
            )
            fail(
                "demonstrable",
                f"the measure itself is {predicate.group(0).strip()!r} "
                f"(subjective term(s): {', '.join(present)}); replace that clause "
                "with what would be observed. The same word qualifying an input is "
                "fine when the verdict beside it is concrete.",
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
