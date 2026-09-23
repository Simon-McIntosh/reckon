# Generated from reckon/schema/flight.yaml — do not edit.
# Regenerate with: uv run python scripts/regen_flight_schema.py
from __future__ import annotations

import re
import sys
from datetime import (
    date,
    datetime,
    time
)
from decimal import Decimal
from enum import Enum
from typing import (
    Any,
    ClassVar,
    Literal,
    Optional,
    Union
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer
)


metamodel_version = "1.11.0"
version = "None"


class ConfiguredBaseModel(BaseModel):
    model_config = ConfigDict(
        serialize_by_alias = True,
        validate_by_name = True,
        validate_assignment = True,
        validate_default = True,
        extra = "forbid",
        arbitrary_types_allowed = True,
        use_enum_values = True,
        strict = False,
    )





class LinkMLMeta(RootModel):
    root: dict[str, Any] = {}
    model_config = ConfigDict(frozen=True)

    def __getattr__(self, key:str):
        return getattr(self.root, key)

    def __getitem__(self, key:str):
        return self.root[key]

    def __setitem__(self, key:str, value):
        self.root[key] = value

    def __contains__(self, key:str) -> bool:
        return key in self.root


linkml_meta = None

class LaunchMode(str, Enum):
    """
    How a worker process for a backend is started.
    """
    cli = "cli"
    """
    Spawned as an external command resolved on PATH.
    """
    in_harness = "in-harness"
    """
    Run inside the calling harness; no process is spawned.
    """


class SandboxMode(str, Enum):
    """
    The filesystem blast radius granted to a worker.
    """
    read_only = "read-only"
    """
    No writes beyond the worker's own manifest file.
    """
    workspace_write = "workspace-write"
    """
    Writable workspace. Inherited by child processes, so it breaks test runners, builds and anything spawning subprocesses.
    """
    worktree_full = "worktree-full"
    """
    Full access, bounded by a detached worktree. The worktree, not the sandbox, is the blast-radius boundary.
    """


class GateEnforcement(str, Enum):
    """
    How strictly evidence gates are applied.
    """
    strict = "strict"
    """
    A gate without its evidence stops the work it guards.
    """
    advisory = "advisory"
    """
    A missing gate is reported but does not stop work.
    """
    disabled = "disabled"
    """
    Gates are not evaluated. Spelled out rather than `off`, which YAML reads as the boolean false in both this schema and the config files it validates.
    """


class GateFailureAction(str, Enum):
    """
    What happens to downstream work when a gate fails.
    """
    hold = "hold"
    """
    Downstream work stays visibly closed.
    """
    warn = "warn"
    """
    Downstream work proceeds with a recorded warning.
    """
    continue_ = "continue"
    """
    The failure is recorded and otherwise ignored.
    """


class WorktreeCleanup(str, Enum):
    """
    How aggressively finished worktrees are removed.
    """
    conservative = "conservative"
    """
    Remove only a clean worktree whose commit is reachable.
    """
    force = "force"
    """
    Remove regardless of dirty or unmerged state.
    """
    never = "never"
    """
    Leave every worktree in place for manual triage.
    """


class SummaryOccasion(str, Enum):
    """
    A point in a worker's life at which it reports.
    """
    dispatch = "dispatch"
    completion = "completion"
    micro_plan = "micro-plan"
    hold = "hold"
    """
    A wave that was held before it opened. It reports like a dispatched one, because a hold that looks like silence is indistinguishable from a crashed orchestrator.
    """


class InputModality(str, Enum):
    """
    A kind of input a backend's model actually receives beside the prompt. Closed structural vocabulary: a value names an input channel, never a provider, a model or an effort level.
    """
    text = "text"
    """
    The prompt and any text the running harness reads for itself. Every backend accepts this, so a lane that declares nothing is still usable for work that asks only for text.
    """
    image = "image"
    """
    A raster image attached to the prompt and received by the model rather than only accepted by the command line. Declared only where the attachment reaches the model: a lane whose launcher accepts an image flag but whose dialect never forwards it must not declare this, because a review composed there answers from the filename and the diff and returns a well-formed verdict it could not have formed.
    """



class FlightConfig(ConfiguredBaseModel):
    """
    A complete flight configuration, or one layer of one.
    """
    version: Optional[int] = Field(default=None, description="""Schema version of this configuration document.""", ge=1)
    default_backend: Optional[str] = Field(default=None, description="""Name of the backend used when a role does not select one. Must name a key of `backends` once every layer has been merged; a name with no backend behind it is a configuration error rather than an implicit fallback.""")
    local_backend: Optional[str] = Field(default=None, description="""Name of the locally served backend selected by `reckon crew dispatch --local`. Must name a key of `backends` once every layer has been merged. Absent means this host has no declared local worker route.""")
    review_excluded_backends: Optional[list[str]] = Field(default=None, description="""Names of backends that review routing must never select, whatever lane composes the review and whatever backend has already dropped the dispatch. A review fallback walks a list of configured backends, so an exclusion it can step around is a note rather than a rule; naming a backend here removes it from every composed review lane.""")
    backends: Optional[dict[str, BackendConfig]] = Field(default=None, description="""Available worker backends, keyed by a name chosen by whoever writes the configuration. The schema fixes no backend names.""")
    roles: Optional[dict[str, RoleConfig]] = Field(default=None, description="""Per-role routing overlays, keyed by role name. A role overrides only the keys it names; everything else falls through to its backend.""")
    gates: Optional[GateConfig] = Field(default=None)
    budget: Optional[BudgetConfig] = Field(default=None)
    fences: Optional[FenceConfig] = Field(default=None)
    worktree: Optional[WorktreeConfig] = Field(default=None)
    summary: Optional[SummaryConfig] = Field(default=None)


class BackendConfig(ConfiguredBaseModel):
    """
    One worker backend and the routing knobs that apply to it.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    launch: Optional[LaunchMode] = Field(default=None, description="""How this backend's workers are started.""")
    command: Optional[str] = Field(default=None, description="""Executable name or path looked up on PATH for a `cli` backend. User data; the schema never supplies one.""")
    auth_check: Optional[list[str]] = Field(default=None, description="""Argument vector run to test whether this backend is authenticated, as an exit status. Optional, and user data: it is how a provider-specific credential check reaches reckon without reckon knowing any provider. Availability probing reports the result and never acts on it.""")
    catalog: Optional[CatalogConfig] = Field(default=None, description="""Optional declaration for asking a backend command which models it serves. Availability probing runs the declared argument vector and matches the configured model against its output; the declaration remains provider-neutral.""")
    endpoints_document: Optional[str] = Field(default=None, description="""Optional path to a document publishing the endpoints this backend's lane is currently serving, as JSON with an `endpoints` list. Availability probing reads it to report whether the lane can serve rather than only that its launcher exists: a document listing at least one endpoint reports serving, one listing none reports not-serving, and an absent declaration or an unreadable or unparsable document reports unknown. User data naming one host's own publication; the schema supplies no path, and the probe never reads it over the network.""")
    lane_document: Optional[str] = Field(default=None, description="""Optional path to a document a serving lane publishes, carrying the lane's measured state, its remaining headroom, whether a worker binding was observed, the stamp the reading was taken at, and the shelf life the lane itself suggests the reading stays fresh for. Availability probing reads it to report the lane observation beside the serving verdict: a reading older than its own suggested shelf life reports unknown while keeping the figure and its age, and an absent, unreadable or unparsable document reports unknown with a reason and never raises. User data naming one host's own publication; the schema supplies no path, and the probe never reads it over the network.""")
    environment: Optional[dict[str, Union[str, EnvironmentVariable]]] = Field(default=None, description="""Environment variables added when this backend's worker is spawned, keyed by variable name. Values are strings and may reference a variable from the dispatcher's environment using `${NAME}`.""")
    model: Optional[str] = Field(default=None, description="""Model identifier passed to this backend. User data; free text so that no provider vocabulary is encoded here.""")
    input_modalities: Optional[list[InputModality]] = Field(default=None, description="""Input modalities this backend's model actually receives. Absent means undeclared, never \"every modality\": a lane that does not say it reads images is treated as not reading them, so a profile requiring an image is refused by name rather than routed here and answered from the filename. User data naming the lane's own measured path to the model; the schema supplies no value, and a declaration is a claim about the delivered input rather than about what the attachment flag accepts.""")
    input_rate_per_million: Optional[float] = Field(default=None, description="""Public input-token price per million tokens for the model this backend serves, as published on the backend's `as_of` date. Together with the output price it is the basis of a notional per-run cost from measured tokens, and of the quota weight comparison. Optional operator data; absence leaves this backend explicitly unpriced.""", ge=0)
    output_rate_per_million: Optional[float] = Field(default=None, description="""Public output-token price per million tokens for the model this backend serves, as published on the backend's `as_of` date. Together with the input price it is the basis of a notional per-run cost from measured tokens, and of the quota weight comparison. Optional operator data; absence leaves this backend explicitly unpriced.""", ge=0)
    as_of: Optional[date] = Field(default=None, description="""Date on which this backend's per-million rates were published. A rate is priced only when dated: one whose `as_of` is older than the declared staleness horizon is returned with its age attached rather than silently priced as current. Optional operator data; a backend declaring rates without a date stays explicitly unpriced.""")
    alias: Optional[str] = Field(default=None, description="""Display label rendered in the fleet pane in place of this backend's model identifier. The spelling belongs beside the model it shortens and is decided by whoever writes the configuration; the schema supplies none.""")
    effort: Optional[str] = Field(default=None, description="""Reasoning-effort level passed to this backend. Free text because each backend defines its own vocabulary, and because an effort ladder must not be fixed by reckon.""")
    effort_spelling: Optional[dict[str, Union[str, EffortSpelling]]] = Field(default=None, description="""Display suffixes for this backend's effort levels, keyed by the effort word. A declared spelling replaces the derived two-character suffix; an effort with no entry renders its first two characters lowercased. User data; the schema enumerates no effort ladder.""")
    sandbox: Optional[SandboxMode] = Field(default=None, description="""Filesystem blast radius granted to workers of this backend.""")
    session_reuse: Optional[bool] = Field(default=None, description="""Whether a finished worker session can be resumed rather than respawned.""")
    metered: Optional[bool] = Field(default=None, description="""Whether using this backend consumes a metered provider allowance. Optional operator data; absence leaves the backend's meteredness undeclared rather than inferring it from the backend name or another routing property.""")
    budget_check: Optional[bool] = Field(default=None, description="""Whether a pre-flight may read this backend's own account-limit surface instead of relying on what earlier runs recorded. Off by default, because a read that has to be asked for cannot happen by accident, and because a backend exposing no such surface reports unknown rather than a guess. It is never a model call and consumes no worker budget.""")
    usable_input_window: Optional[int] = Field(default=None, description="""Maximum input tokens this backend can hold after any output reservation has already been removed. Dispatch compares this declared window with a deterministic estimate of the node's standing instructions and named repository files before creating a worktree. Absence means unbounded, never zero: an unknown ceiling cannot justify refusing work.""", ge=1)
    max_concurrent_runs: Optional[int] = Field(default=None, description="""Maximum live worker runs this backend may carry at once. Dispatch counts the non-terminal live pointers claiming this backend and refuses a new dispatch that would exceed the ceiling, naming the backend, the ceiling, the current count and the occupying run ids. Absent or null means unlimited, the default for every backend that does not declare one.""", ge=1)
    fallback: Optional[str] = Field(default=None, description="""Backend to substitute when this one is held on budget. Declared, never inferred: a backend naming no fallback still refuses a held dispatch exactly as one would with this key absent. The substitution is recorded on the run — the backend asked for, the backend used and the hold that caused it — so a calibration slice never attributes a fallback run to the backend the caller named. A fallback that is itself held still refuses; this key does not chain into a search across backends.""")
    budget_group: Optional[str] = Field(default=None, description="""Name shared by backends that draw on the same account quota. A pool is declared, never inferred: identical probe readings or reset times are evidence only that two backends were observed the same way, never that they share a budget, so a backend with no declared group stays ungrouped however alike its siblings read. Optional operator data naming the owner's own pool; the schema supplies none.""")
    time_budget: Optional[str] = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: Optional[int] = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)
    placement: Optional[PlacementConfig] = Field(default=None, description="""Optional scheduler placement for this backend's workers. A declared placement wraps the launch in the scheduler invocation rather than replacing it, so the resolved command, its environment and its stream paths are the ones that run. Absent means the login-node launch, which is what keeps every backend that declares none behaving exactly as before.""")

    @field_validator('time_budget')
    def pattern_time_budget(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid time_budget format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid time_budget format: {v}"
            raise ValueError(err_msg)
        return v


class PlacementConfig(ConfiguredBaseModel):
    """
    A scheduler wrapper declared for one backend's workers. It describes how the launch is wrapped and never what runs: the resolved command stays the backend's own, so a placement moves a worker between hosts without changing which lane serves it. It also declares what the worker needs visible where it lands, and how to ask the wrapper about a job it started.
    """
    scheduler: str = Field(default=..., description="""Scheduler executable the launch is wrapped in. Resolved against the same PATH the launch searches, so an unresolvable wrapper is refused before launch rather than dying at exec and leaving an empty stream that reads as a worker turn.""")
    options: Optional[list[str]] = Field(default=None, description="""Argument strings passed to the scheduler ahead of the resolved command, such as a partition and a resource request. User data; the schema names no partition and no site.""")
    job_id_probe: Optional[list[str]] = Field(default=None, description="""Argument vector asked which job the launch became, with `{run}` replaced by the run id. Read rather than guessed, and declared per backend because reckon knows no scheduler's own vocabulary; a probe that answers no identifier records why instead of a fabricated id.""")
    requirements: Optional[list[PlacementRequirement]] = Field(default=None, description="""Filesystem paths and network endpoints this placement's workers need visible from the node the scheduler places them on. Checked before launch, so a placement into a partition that cannot see one is refused while naming which, rather than failing later in a way that reads as a worker defect. A path on per-node storage is refused by name: it exists on the dispatcher and not where the worker runs, so it fails silently.""")
    state_query: Optional[list[str]] = Field(default=None, description="""Argument vector that asks the scheduler for one job's state, with `{job}` replaced by the job id. Declared beside the wrapper it asks, so which reporting verb answers a given scheduler is configuration rather than a table in reckon's own code, and a wrapper declared without one is a placement reckon cannot follow rather than one it silently cannot query.""")
    reason_query: Optional[list[str]] = Field(default=None, description="""Argument vector that asks the scheduler for one job's own reason string, with `{job}` replaced by the job id. A job that never started reports why here rather than through an exit status the scheduler client never produced.""")


class PlacementRequirement(ConfiguredBaseModel):
    """
    One filesystem path or network endpoint this placement's workers need visible from the node they run on. Exactly one of `path` and `endpoint` is declared; a requirement naming both or neither is a configuration error rather than a check that quietly passes.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    path: Optional[str] = Field(default=None, description="""A filesystem path this placement's workers must be able to see. User data; the schema names no site and fixes no layout.""")
    endpoint: Optional[str] = Field(default=None, description="""A network endpoint, given as host and port, this placement's workers must be able to reach — the served model and its router are the intended case.""")


class CatalogConfig(ConfiguredBaseModel):
    """
    Provider-neutral model catalog probe owned by a backend.
    """
    list_command: Optional[list[str]] = Field(default=None, description="""Argument vector that prints the models served by this backend. The vector is user data and includes the executable.""")
    model_pattern: Optional[str] = Field(default=None, description="""Regular expression used against each catalog output line. The required `{model}` placeholder is replaced by the escaped configured model.""")


class EnvironmentVariable(ConfiguredBaseModel):
    """
    One environment-variable name and its string value.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    value: Optional[str] = Field(default=None, description="""Value associated with an environment-variable name.""")


class EffortSpelling(ConfiguredBaseModel):
    """
    One effort level and its declared display suffix.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    spelling: Optional[str] = Field(default=None, description="""The display suffix rendered for an effort level's word.""")


class RoleConfig(ConfiguredBaseModel):
    """
    A routing overlay for one kind of node. Every slot is optional; an unset slot inherits from the selected backend.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    backend: Optional[str] = Field(default=None, description="""Backend this role dispatches to. Absent means `default_backend`.""")
    model: Optional[str] = Field(default=None, description="""Model identifier passed to this backend. User data; free text so that no provider vocabulary is encoded here.""")
    effort: Optional[str] = Field(default=None, description="""Reasoning-effort level passed to this backend. Free text because each backend defines its own vocabulary, and because an effort ladder must not be fixed by reckon.""")
    execution_capable: Optional[bool] = Field(default=None, description="""Whether this role runs commands that can write build, test, cache or product state inside its detached worktree.""")
    sandbox: Optional[SandboxMode] = Field(default=None, description="""Filesystem blast radius granted to workers of this backend.""")
    session_reuse: Optional[bool] = Field(default=None, description="""Whether a finished worker session can be resumed rather than respawned.""")
    time_budget: Optional[str] = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: Optional[int] = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)
    write_paths: Optional[list[str]] = Field(default=None, description="""Default write scope granted to a node of this role when it declares no write_paths of its own. Entries are relative and are resolved against the dispatching run's own durable report-and-log directory — the same directory `manifest_path` already defaults into — never against the repository being worked on. A shipped or host layer therefore names no host-specific location, and a role whose entries all stay under that directory grants no reach into repository source.""")
    by_spec_level: Optional[SpecificationRouting] = Field(default=None, description="""Routing overlays selected by the specification completeness declared for a node. An undeclared level applies no overlay.""")

    @field_validator('time_budget')
    def pattern_time_budget(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid time_budget format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid time_budget format: {v}"
            raise ValueError(err_msg)
        return v


class SpecificationRouting(ConfiguredBaseModel):
    """
    Routing overlays keyed by the closed specification-level vocabulary.
    """
    exact: Optional[RoutingOverlay] = Field(default=None, description="""Routing for a node whose implementation is fully prescribed.""")
    guided: Optional[RoutingOverlay] = Field(default=None, description="""Routing for a node whose design is fixed but implementation is derived.""")
    open: Optional[RoutingOverlay] = Field(default=None, description="""Routing for a node whose design and implementation remain to the worker.""")


class RoutingOverlay(ConfiguredBaseModel):
    """
    Settings that replace the selected role and backend routing for one level.
    """
    backend: Optional[str] = Field(default=None, description="""Backend this role dispatches to. Absent means `default_backend`.""")
    model: Optional[str] = Field(default=None, description="""Model identifier passed to this backend. User data; free text so that no provider vocabulary is encoded here.""")
    effort: Optional[str] = Field(default=None, description="""Reasoning-effort level passed to this backend. Free text because each backend defines its own vocabulary, and because an effort ladder must not be fixed by reckon.""")
    time_budget: Optional[str] = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: Optional[int] = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)

    @field_validator('time_budget')
    def pattern_time_budget(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid time_budget format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid time_budget format: {v}"
            raise ValueError(err_msg)
        return v


class GateConfig(ConfiguredBaseModel):
    """
    How evidence gates are enforced.
    """
    enforce: Optional[GateEnforcement] = Field(default=None)
    require_evidence: Optional[bool] = Field(default=None, description="""Whether a gate must produce recorded evidence to be considered met.""")
    on_fail: Optional[GateFailureAction] = Field(default=None)
    suite_command: Optional[str] = Field(default=None, description="""Project test-suite command whose presence arms promotion consequence checks. Absent means the project is unarmed; no command is supplied by shipped defaults.""")


class BudgetConfig(ConfiguredBaseModel):
    """
    Thresholds that decide whether a wave opens. They are compared against whatever a backend actually reported; a backend that reports nothing is never held by them.
    """
    utilisation_ceiling_pct: Optional[float] = Field(default=None, description="""Reported utilisation, as a percentage, at or above which a wave will not open. A backend reporting no headroom is never held by this: absence of a signal is not evidence of exhaustion, and a false hold stalls everything while a rejected call is cheap and announces itself.""", ge=0, le=100)
    resume_reserve_pct: Optional[float] = Field(default=None, description="""Headroom withheld from new dispatches, in percentage points, so a worker that stops and asks for help can still be answered in its own session. A fresh dispatch stops at the ceiling less this reserve; answering a stuck worker may spend it. Spending the last of a quota on a new dispatch strands the wave in its worst state — work in flight and no way to unblock it.""", ge=0, le=100)
    coordinator_reserve_pct: Optional[float] = Field(default=None, description="""Headroom withheld from new dispatches, in percentage points, so the coordinator that must survive a wave keeps headroom of its own. The reported account position already counts the coordinator's traffic, but reckon cannot see the total; a wave sized to fill the remaining headroom leaves nothing for the audit, merge and outcome-recording the coordinator does after the workers finish, so it can be refused mid-wave with every worker's output stranded in a worktree and nobody left to land it. A fresh dispatch stops at the ceiling less both this reserve and the resume reserve; a resume may spend either, because answering a stuck worker is the expenditure the reserves were withheld for. The figure is declared, not measured: there is no coordinator burn-rate record on this machine, and reading the account surface on a cadence is what calibrates it later.""", ge=0, le=100)
    exhausted_statuses: Optional[list[str]] = Field(default=None, description="""Threshold-status values that count as exhausted whatever the utilisation reads — the overage question, answered as data so that the schema enumerates no backend's vocabulary. Empty leaves the ceiling as the only test.""")
    evidence_shelf_life_minutes: Optional[float] = Field(default=None, description="""Minutes a refusal that names no reset time keeps describing the present before it is treated as stale and stops holding the wave on its own. Has no effect on a refusal that named a reset, since a stated reset is stronger evidence than an age and already carries its own expiry. A value at or below zero disables ageing and restores an indefinite hold.""")


class FenceConfig(ConfiguredBaseModel):
    """
    Limits a worker applies to itself before asking for help.
    """
    time_budget: Optional[str] = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: Optional[int] = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)
    needs_help_after_failures: Optional[int] = Field(default=None, description="""Consecutive failures after which a worker stops retrying and asks for help. Zero disables the fence.""", ge=0)
    manifest_required: Optional[bool] = Field(default=None, description="""Whether a worker must write its manifest to the orchestrator-named path before its node counts as delivered.""")
    enforce_budget_watchdog: Optional[bool] = Field(default=None, description="""Whether observation stops a live CLI worker after its declared time budget multiplied by the configured grace. Off by default; classification always reports the overrun without mutating the run.""")
    budget_grace_multiple: Optional[float] = Field(default=None, description="""Multiple of a run's declared time budget allowed before opt-in watchdog enforcement stops it. Values below one would stop before the declared allowance elapsed.""", ge=1)
    unreconciled_run_grace: Optional[str] = Field(default=None, description="""How long a complete or blocked worker manifest may remain as a live pointer before dispatch refuses more work for that project.""")

    @field_validator('time_budget')
    def pattern_time_budget(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid time_budget format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid time_budget format: {v}"
            raise ValueError(err_msg)
        return v

    @field_validator('unreconciled_run_grace')
    def pattern_unreconciled_run_grace(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid unreconciled_run_grace format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid unreconciled_run_grace format: {v}"
            raise ValueError(err_msg)
        return v


class WorktreeConfig(ConfiguredBaseModel):
    """
    Worktree lifecycle policy.
    """
    cleanup: Optional[WorktreeCleanup] = Field(default=None)


class SummaryConfig(ConfiguredBaseModel):
    """
    When and how workers report.
    """
    reflex: Optional[str] = Field(default=None, description="""The reporting shape a worker follows when it summarises.""")
    at: Optional[list[SummaryOccasion]] = Field(default=None, description="""Occasions on which a worker emits a summary.""")


# Model rebuild
# see https://pydantic-docs.helpmanual.io/usage/models/#rebuilding-a-model
FlightConfig.model_rebuild()
BackendConfig.model_rebuild()
PlacementConfig.model_rebuild()
PlacementRequirement.model_rebuild()
CatalogConfig.model_rebuild()
EnvironmentVariable.model_rebuild()
EffortSpelling.model_rebuild()
RoleConfig.model_rebuild()
SpecificationRouting.model_rebuild()
RoutingOverlay.model_rebuild()
GateConfig.model_rebuild()
BudgetConfig.model_rebuild()
FenceConfig.model_rebuild()
WorktreeConfig.model_rebuild()
SummaryConfig.model_rebuild()
