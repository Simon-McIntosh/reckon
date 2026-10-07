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

class PickerMode(str, Enum):
    """
    Whether the recorded picker selection controls dispatch.
    """
    shadow = "shadow"
    """
    Record the selection and use deterministic routing.
    """
    route = "route"
    """
    Route dispatch by the picker selection.
    """


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
    version: int | None = Field(default=None, description="""Schema version of this configuration document.""", ge=1)
    default_backend: str | None = Field(default=None, description="""Name of the backend used when a role does not select one. Must name a key of `backends` once every layer has been merged; a name with no backend behind it is a configuration error rather than an implicit fallback.""")
    local_backend: str | None = Field(default=None, description="""Name of the locally served backend selected by `reckon crew dispatch --local`. Must name a key of `backends` once every layer has been merged. Absent means this host has no declared local worker route.""")
    review_excluded_backends: list[str] | None = Field(default=None, description="""Names of backends that review routing must never select, whatever lane composes the review and whatever backend has already dropped the dispatch. A review fallback walks a list of configured backends, so an exclusion it can step around is a note rather than a rule; naming a backend here removes it from every composed review lane.""")
    protected_paths: list[str] | None = Field(default=None, description="""Extra filesystem paths a host or project layer adds to the set a worker's fence seals read-only. The key is additive: the fence's shipped default set is today's built-in list — operator state, credentials, shared records and every main checkout, the worktree pool included — and a layer can add to it but never replace it, so a layer that omits a default here leaves that default protected rather than silently dropping it. Entries are home-relative or absolute and are resolved against the operator's home the same way the defaults are. The only way to remove a default is to name it under `unprotected_paths`.""")
    unprotected_paths: list[str] | None = Field(default=None, description="""Defaults the fence's protected set carries that a host or project layer leaves writable. A layer removes a default only by naming it here, so a reduction of the protected set is a deliberate, named act rather than a side effect of editing `protected_paths`. Each entry names a default as the fence would resolve it — home-relative or absolute — and an entry that names no default is ignored. Every run whose fence leaves out a named default carries the list on its run record.""")
    backends: dict[str, BackendConfig] | None = Field(default=None, description="""Available worker backends, keyed by a name chosen by whoever writes the configuration. The schema fixes no backend names.""")
    lanes: dict[str, LaneConfig] | None = Field(default=None, description="""A lane is a subscription or host, and the models chosen inside it. Declaring a lane is an alternative to writing one backend block per model: resolution expands each lane back into the backend entries today's callers read, so a lane that declares its models and an equivalent set of backend blocks resolve to the same config. Absent means no lane is declared and resolution is unchanged.""")
    roles: dict[str, RoleConfig] | None = Field(default=None, description="""Per-role routing overlays, keyed by role name. A role overrides only the keys it names; everything else falls through to its backend.""")
    routing: RoutingConfig | None = Field(default=None, description="""How dispatch selects its worker backend.""")
    gates: GateConfig | None = Field(default=None)
    review: ReviewConfig | None = Field(default=None)
    budget: BudgetConfig | None = Field(default=None)
    fences: FenceConfig | None = Field(default=None)
    capability_raise: CapabilityRaise | None = Field(default=None, description="""Rule that raises the capability a node resolves at once its plan section has been attempted at or above a threshold, so a section that keeps costing attempts stops landing on the raise's own lane without anyone deciding it by hand. The raise moves a node upward only: a node already at or above the raised level resolves unchanged.""")
    worktree: WorktreeConfig | None = Field(default=None)
    summary: SummaryConfig | None = Field(default=None)
    ticker: TickerConfig | None = Field(default=None, description="""The follower pane's own memory of what it has rendered. A reader tracking a fleet across re-arms wants the rows it just saw restored rather than an empty pane; these bounds decide how much of the view comes back.""")


class RoutingConfig(ConfiguredBaseModel):
    """
    Dispatch routing policy, overridable per dispatch.
    """
    picker: PickerMode | None = Field(default=None, description="""Record the picker selection in shadow mode, or route dispatch by it. A per-dispatch route override takes precedence over this layered value.""")


class ReviewConfig(ConfiguredBaseModel):
    """
    How a finished run is sized to a review. Declared on the flight config so a host or project layer retunes the tier thresholds without a code change; the resolver reads them from the resolved config.
    """
    tiers: ReviewTiers | None = Field(default=None, description="""The thresholds that size a review to a finished run's risk. The resolver reads them from the resolved flight config so a host or project layer retunes the tiers without a code change.""")
    suite: ReviewSuite | None = Field(default=None, description="""The project's own suite command and whole-run budget. Absent means the project declares no standing suite, and no run is recorded for it.""")


class ReviewSuite(ConfiguredBaseModel):
    """
    The project's own suite: the command the coordinator runs once at the merged head, and the whole-run wall-clock budget it is held to. Declared on the flight config so a project names its own suite rather than reckon fixing one.
    """
    command: list[str] = Field(default=..., description="""The suite command as an argument vector, run from the checkout root. The first entry is the executable, resolved against the working directory and PATH the run is started in.""")
    budget: str = Field(default=..., description="""Whole-run wall-clock budget, written as an integer followed by a unit — `s`, `m` or `h`. A run that exceeds it is stopped and recorded as over budget rather than being left to hang.""")

    @field_validator('budget')
    def pattern_budget(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid budget format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid budget format: {v}"
            raise ValueError(err_msg)
        return v


class ReviewTiers(ConfiguredBaseModel):
    """
    The thresholds that separate the light and full review tiers. A run that changes runtime source below the changed-line ceiling, at a spec level that fixes the done-when, earns a light review.
    """
    light_changed_lines: int | None = Field(default=None, description="""Changed-line ceiling (added plus deleted) below which a run that changes runtime source can earn a light review rather than a full one. A run at or above this ceiling is reviewed at full, because the size of a diff is itself a reason a reviewer has more to read than a light pass covers.""", ge=1)
    light_time_budget: str | None = Field(default=None, description="""Wall-clock budget granted to a light review, written as an integer followed by a unit — `s`, `m` or `h`. A light review that exceeds it escalates to a full review rather than being abandoned.""")

    @field_validator('light_time_budget')
    def pattern_light_time_budget(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid light_time_budget format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid light_time_budget format: {v}"
            raise ValueError(err_msg)
        return v


class BackendConfig(ConfiguredBaseModel):
    """
    One worker backend and the routing knobs that apply to it.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    launch: LaunchMode | None = Field(default=None, description="""How this backend's workers are started.""")
    command: str | None = Field(default=None, description="""Executable name or path looked up on PATH for a `cli` backend. User data; the schema never supplies one.""")
    auth_check: list[str] | None = Field(default=None, description="""Argument vector run to test whether this backend is authenticated, as an exit status. Optional, and user data: it is how a provider-specific credential check reaches reckon without reckon knowing any provider. Availability probing reports the result and never acts on it.""")
    catalog: CatalogConfig | None = Field(default=None, description="""Optional declaration for asking a backend command which models it serves. Availability probing runs the declared argument vector and matches the configured model against its output; the declaration remains provider-neutral.""")
    endpoints_document: str | None = Field(default=None, description="""Optional path to a document publishing the endpoints this backend's lane is currently serving, as JSON with an `endpoints` list. Availability probing reads it to report whether the lane can serve rather than only that its launcher exists: a document listing at least one endpoint reports serving, one listing none reports not-serving, and an absent declaration or an unreadable or unparsable document reports unknown. User data naming one host's own publication; the schema supplies no path, and the probe never reads it over the network.""")
    lane_document: str | None = Field(default=None, description="""Optional path to a document a serving lane publishes, carrying the lane's measured state, its remaining headroom, whether a worker binding was observed, the stamp the reading was taken at, and the shelf life the lane itself suggests the reading stays fresh for. Availability probing reads it to report the lane observation beside the serving verdict: a reading older than its own suggested shelf life reports unknown while keeping the figure and its age, and an absent, unreadable or unparsable document reports unknown with a reason and never raises. User data naming one host's own publication; the schema supplies no path, and the probe never reads it over the network.""")
    gate_document: str | None = Field(default=None, description="""Optional absolute path to the JSON gate file that decides whether a serving lane will admit work, as the router resolves it: a `paused` boolean and an optional `reason` string. A dispatch whose resolved backend declares this path reads it before creating a pointer or a worktree, and waits rather than launching while `paused` is true or the gate cannot be answered; the declared path is also checked against the lane document's published `router_generation_gate.config_path`, and a mismatch waits. User data naming one host's own gate; the schema supplies no path, and the read never goes over the network.""")
    environment: dict[str, Union[str, EnvironmentVariable]] | None = Field(default=None, description="""Environment variables added when this backend's worker is spawned, keyed by variable name. Values are strings and may reference a variable from the dispatcher's environment using `${NAME}`.""")
    model: str | None = Field(default=None, description="""Model identifier passed to this backend. User data; free text so that no provider vocabulary is encoded here.""")
    input_modalities: list[InputModality] | None = Field(default=None, description="""Input modalities this backend's model actually receives. Absent means undeclared, never \"every modality\": a lane that does not say it reads images is treated as not reading them, so a profile requiring an image is refused by name rather than routed here and answered from the filename. User data naming the lane's own measured path to the model; the schema supplies no value, and a declaration is a claim about the delivered input rather than about what the attachment flag accepts.""")
    input_rate_per_million: float | None = Field(default=None, description="""Public input-token price per million tokens for the model this backend serves, as published on the backend's `as_of` date. Together with the output price it is the basis of a notional per-run cost from measured tokens, and of the quota weight comparison. Optional operator data; absence leaves this backend explicitly unpriced.""", ge=0)
    output_rate_per_million: float | None = Field(default=None, description="""Public output-token price per million tokens for the model this backend serves, as published on the backend's `as_of` date. Together with the input price it is the basis of a notional per-run cost from measured tokens, and of the quota weight comparison. Optional operator data; absence leaves this backend explicitly unpriced.""", ge=0)
    as_of: date | None = Field(default=None, description="""Date on which this backend's per-million rates were published. A rate is priced only when dated: one whose `as_of` is older than the declared staleness horizon is returned with its age attached rather than silently priced as current. Optional operator data; a backend declaring rates without a date stays explicitly unpriced.""")
    alias: str | None = Field(default=None, description="""Display label rendered in the fleet pane in place of this backend's model identifier. The spelling belongs beside the model it shortens and is decided by whoever writes the configuration; the schema supplies none.""")
    effort: str | None = Field(default=None, description="""Reasoning-effort level passed to this backend. Free text because each backend defines its own vocabulary, and because an effort ladder must not be fixed by reckon.""")
    effort_spelling: dict[str, Union[str, EffortSpelling]] | None = Field(default=None, description="""Display suffixes for this backend's effort levels, keyed by the effort word. A declared spelling replaces the derived two-character suffix; an effort with no entry renders its first two characters lowercased. User data; the schema enumerates no effort ladder.""")
    sandbox: SandboxMode | None = Field(default=None, description="""Filesystem blast radius granted to workers of this backend.""")
    session_reuse: bool | None = Field(default=None, description="""Whether a finished worker session can be resumed rather than respawned.""")
    serves_orchestrators: bool | None = Field(default=None, description="""Whether this backend's lane carries this deployment's orchestrators. Declared, never inferred from the backend's name: the orchestrator role is a property of the deployment, so an alias or a backend that renames must both reach a fence that reads this key, and a rule matching on a name breaks the moment an orchestrator runs somewhere else. Absent means the lane declares nothing and is dispatchable as before. A lane declaring true is one whose capacity background work spends against the sessions that dispatch, merge, promote and record, so a lane saturated there stops every session rather than one node.""")
    metered: bool | None = Field(default=None, description="""Whether using this backend consumes a metered provider allowance. Optional operator data; absence leaves the backend's meteredness undeclared rather than inferring it from the backend name or another routing property.""")
    budget_check: bool | None = Field(default=None, description="""Whether a pre-flight may read this backend's own account-limit surface instead of relying on what earlier runs recorded. Off by default, because a read that has to be asked for cannot happen by accident, and because a backend exposing no such surface reports unknown rather than a guess. It is never a model call and consumes no worker budget.""")
    usable_input_window: int | None = Field(default=None, description="""Maximum input tokens this backend can hold after any output reservation has already been removed. Dispatch compares this declared window with a deterministic estimate of the node's standing instructions and named repository files before creating a worktree. Absence means unbounded, never zero: an unknown ceiling cannot justify refusing work.""", ge=1)
    effective_input_window: int | None = Field(default=None, description="""Lowest input size this backend's endpoint is recorded as refusing, so that dispatch gates on the boundary that kills rather than on the declared window alone. Measured from what the lane's own endpoint refused, never declared by the configuration: a run whose last result is an error carrying the endpoint's prompt-too-long text reports the input size it was refused at, and the boundary is the smallest such input recorded for this backend's own model — a census pooled across models answers with the narrowest neighbour's figure instead. Dispatch refuses an estimate that exceeds either this figure or usable_input_window, naming both, so a node sized inside the band is refused before a worktree is created rather than by the endpoint after three records and no deliverable. Absence means the effective boundary is undeclared, never zero: only the declared window gates such a lane.""", ge=1)
    max_concurrent_runs: int | None = Field(default=None, description="""Retired and ignored. Reckon enforces no numeric worker cap: a session holds its runs for as long as it needs them, and engine load is bounded by the router's admission gate rather than by a declared ceiling. The slot stays declared only so an existing host file that sets it keeps loading; a value here changes no behaviour. Absent or null is treated exactly as a declared value.""", ge=1)
    fallback: str | None = Field(default=None, description="""Backend to substitute when this one is held on budget. Declared, never inferred: a backend naming no fallback still refuses a held dispatch exactly as one would with this key absent. The substitution is recorded on the run — the backend asked for, the backend used and the hold that caused it — so a calibration slice never attributes a fallback run to the backend the caller named. A fallback that is itself held still refuses; this key does not chain into a search across backends.""")
    budget_group: str | None = Field(default=None, description="""Name shared by backends that draw on the same account quota. A pool is declared, never inferred: identical probe readings or reset times are evidence only that two backends were observed the same way, never that they share a budget, so a backend with no declared group stays ungrouped however alike its siblings read. Optional operator data naming the owner's own pool; the schema supplies none.""")
    time_budget: str | None = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: int | None = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)
    placement: PlacementConfig | None = Field(default=None, description="""Optional scheduler placement for this backend's workers. A declared placement wraps the launch in the scheduler invocation rather than replacing it, so the resolved command, its environment and its stream paths are the ones that run. Absent means the login-node launch, which is what keeps every backend that declares none behaving exactly as before.""")

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


class LaneModelConfig(ConfiguredBaseModel):
    """
    One model inside a lane, and the routing knobs that apply to it alone. The model key is the short name a caller types; the identifier it launches is ``model``.
    """
    name: str | None = Field(default=None, description="""Map key for an inlined entry.""")
    model: str | None = Field(default=None, description="""Model identifier passed to this model's launch. User data; free text so that no provider vocabulary is encoded here.""")
    effort: str | None = Field(default=None, description="""Reasoning-effort level passed to this model. Free text because each backend defines its own vocabulary, and because an effort ladder must not be fixed by reckon.""")
    alias: str | None = Field(default=None, description="""Display label rendered in the fleet pane in place of this model's identifier. The spelling belongs beside the model it shortens and is decided by whoever writes the configuration; the schema supplies none.""")
    input_rate_per_million: float | None = Field(default=None, description="""Public input-token price per million tokens for this model, as published on its ``as_of`` date. Absence leaves the model explicitly unpriced.""", ge=0)
    output_rate_per_million: float | None = Field(default=None, description="""Public output-token price per million tokens for this model, as published on its ``as_of`` date. Absence leaves the model explicitly unpriced.""", ge=0)
    as_of: date | None = Field(default=None, description="""Date on which this model's per-million rates were published. A rate is priced only when dated.""")
    budget_group: str | None = Field(default=None, description="""Name of the account quota this model draws on, overriding the lane's for this model. Absent means the model inherits the lane's budget_group.""")
    time_budget: str | None = Field(default=None, description="""Wall-clock allowance for this model, written as an integer followed by a unit — `s`, `m` or `h`. Overrides the lane's allowance for this model alone.""")

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


class LaneConfig(BackendConfig):
    """
    One lane: a subscription or host, carrying the backend-block keys that apply to every model inside it, plus the models themselves and the default one a dispatch on the lane gets when it names none. Resolution expands a lane into backend entries, so a reader of ``backends`` sees one entry per lane and one per derived model name.
    """
    name: str | None = Field(default=None, description="""Map key for an inlined entry.""")
    default_model: str | None = Field(default=None, description="""Model key a dispatch on this lane resolves to when it names no model. A lane declaring none has no default and only its derived model entries are reachable by name.""")
    models: dict[str, LaneModelConfig] | None = Field(default=None, description="""The models this lane offers, keyed by the short name a caller types. A lane with no models expands to its own named entry alone.""")


class PlacementConfig(ConfiguredBaseModel):
    """
    A scheduler wrapper declared for one backend's workers. It describes how the launch is wrapped and never what runs: the resolved command stays the backend's own, so a placement moves a worker between hosts without changing which lane serves it. It also declares what the worker needs visible where it lands, and how to ask the wrapper about a job it started.
    """
    scheduler: str = Field(default=..., description="""Scheduler executable the launch is wrapped in. Resolved against the same PATH the launch searches, so an unresolvable wrapper is refused before launch rather than dying at exec and leaving an empty stream that reads as a worker turn.""")
    options: list[str] | None = Field(default=None, description="""Argument strings passed to the scheduler ahead of the resolved command, such as a partition and a resource request. User data; the schema names no partition and no site.""")
    job_id_probe: list[str] | None = Field(default=None, description="""Argument vector asked which job the launch became, with `{run}` replaced by the run id. Read rather than guessed, and declared per backend because reckon knows no scheduler's own vocabulary; a probe that answers no identifier records why instead of a fabricated id.""")
    requirements: list[PlacementRequirement] | None = Field(default=None, description="""Filesystem paths and network endpoints this placement's workers need visible from the node the scheduler places them on. Checked before launch, so a placement into a partition that cannot see one is refused while naming which, rather than failing later in a way that reads as a worker defect. A path on per-node storage is refused by name: it exists on the dispatcher and not where the worker runs, so it fails silently.""")
    state_query: list[str] | None = Field(default=None, description="""Argument vector that asks the scheduler for one job's state, with `{job}` replaced by the job id. Declared beside the wrapper it asks, so which reporting verb answers a given scheduler is configuration rather than a table in reckon's own code, and a wrapper declared without one is a placement reckon cannot follow rather than one it silently cannot query.""")
    reason_query: list[str] | None = Field(default=None, description="""Argument vector that asks the scheduler for one job's own reason string, with `{job}` replaced by the job id. A job that never started reports why here rather than through an exit status the scheduler client never produced.""")


class PlacementRequirement(ConfiguredBaseModel):
    """
    One filesystem path or network endpoint this placement's workers need visible from the node they run on. Exactly one of `path` and `endpoint` is declared; a requirement naming both or neither is a configuration error rather than a check that quietly passes.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    path: str | None = Field(default=None, description="""A filesystem path this placement's workers must be able to see. User data; the schema names no site and fixes no layout.""")
    endpoint: str | None = Field(default=None, description="""A network endpoint, given as host and port, this placement's workers must be able to reach — the served model and its router are the intended case.""")


class CatalogConfig(ConfiguredBaseModel):
    """
    Provider-neutral model catalog probe owned by a backend.
    """
    list_command: list[str] | None = Field(default=None, description="""Argument vector that prints the models served by this backend. The vector is user data and includes the executable.""")
    model_pattern: str | None = Field(default=None, description="""Regular expression used against each catalog output line. The required `{model}` placeholder is replaced by the escaped configured model.""")


class EnvironmentVariable(ConfiguredBaseModel):
    """
    One environment-variable name and its string value.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    value: str | None = Field(default=None, description="""Value associated with an environment-variable name.""")


class EffortSpelling(ConfiguredBaseModel):
    """
    One effort level and its declared display suffix.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    spelling: str | None = Field(default=None, description="""The display suffix rendered for an effort level's word.""")


class DimensionFloor(ConfiguredBaseModel):
    """
    One review dimension and the lowest score it may carry and still read as a pass. The name is a key of this inlined map, so a configuration writes the dimension and its floor together and neither can be read apart.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    floor: int | None = Field(default=None, description="""Lowest score the dimension this entry names may carry and still read as a pass, on the review schema's own 0..20 scale. A stored score below it is reported as a sub-floor finding until a disposition answers it.""", ge=0)


class RoleConfig(ConfiguredBaseModel):
    """
    A routing overlay for one kind of node. Every slot is optional; an unset slot inherits from the selected backend.
    """
    name: str = Field(default=..., description="""Map key for an inlined entry.""")
    backend: str | None = Field(default=None, description="""Backend this role dispatches to. Absent means `default_backend`.""")
    model: str | None = Field(default=None, description="""Model identifier passed to this backend. User data; free text so that no provider vocabulary is encoded here.""")
    effort: str | None = Field(default=None, description="""Reasoning-effort level passed to this backend. Free text because each backend defines its own vocabulary, and because an effort ladder must not be fixed by reckon.""")
    execution_capable: bool | None = Field(default=None, description="""Whether this role runs commands that can write build, test, cache or product state inside its detached worktree.""")
    sandbox: SandboxMode | None = Field(default=None, description="""Filesystem blast radius granted to workers of this backend.""")
    session_reuse: bool | None = Field(default=None, description="""Whether a finished worker session can be resumed rather than respawned.""")
    time_budget: str | None = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: int | None = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)
    write_paths: list[str] | None = Field(default=None, description="""Default write scope granted to a node of this role when it declares no write_paths of its own. Entries are relative and are resolved against the dispatching run's own durable report-and-log directory — the same directory `manifest_path` already defaults into — never against the repository being worked on. A shipped or host layer therefore names no host-specific location, and a role whose entries all stay under that directory grants no reach into repository source.""")
    by_spec_level: SpecificationRouting | None = Field(default=None, description="""Routing overlays selected by the specification completeness declared for a node. An undeclared level applies no overlay.""")
    by_capability_class: CapabilityClassRouting | None = Field(default=None, description="""Routing overlays selected by the capability class a node resolves at. A node whose plan section has been attempted at or above the configured threshold resolves at the raised class, and that class selects the lane; a node attempted fewer times resolves at the class its section declares. A node raised onto a class whose overlay declares nothing keeps the routing its role and backend already resolve to.""")

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
    exact: RoutingOverlay | None = Field(default=None, description="""Routing for a node whose implementation is fully prescribed.""")
    guided: RoutingOverlay | None = Field(default=None, description="""Routing for a node whose design is fixed but implementation is derived.""")
    open: RoutingOverlay | None = Field(default=None, description="""Routing for a node whose design and implementation remain to the worker.""")


class CapabilityClassRouting(ConfiguredBaseModel):
    """
    Routing overlays keyed by the closed capability-class vocabulary.
    """
    routine: RoutingOverlay | None = Field(default=None, description="""Routing for a node resolving at the routine capability class.""")
    general: RoutingOverlay | None = Field(default=None, description="""Routing for a node resolving at the general capability class.""")
    orchestrator: RoutingOverlay | None = Field(default=None, description="""Routing for a node resolving at the orchestrator capability class, including a node raised there by its section's attempt count.""")


class CapabilityRaise(ConfiguredBaseModel):
    """
    The threshold at which a section's attempt count raises a node's effective capability, and the levels it is raised to.
    """
    attempts_threshold: int | None = Field(default=None, description="""Section attempt count at which the raise applies. A section attempted fewer times resolves unchanged.""", ge=1)
    raised_class: str | None = Field(default=None, description="""Capability class a raised node resolves at, named in the capability vocabulary the section contract already declares.""")
    raised_reasoning: str | None = Field(default=None, description="""Reasoning level a raised node requires.""")
    raised_verification: str | None = Field(default=None, description="""Verification level a raised node requires.""")


class RoutingOverlay(ConfiguredBaseModel):
    """
    Settings that replace the selected role and backend routing for one level.
    """
    backend: str | None = Field(default=None, description="""Backend this role dispatches to. Absent means `default_backend`.""")
    model: str | None = Field(default=None, description="""Model identifier passed to this backend. User data; free text so that no provider vocabulary is encoded here.""")
    effort: str | None = Field(default=None, description="""Reasoning-effort level passed to this backend. Free text because each backend defines its own vocabulary, and because an effort ladder must not be fixed by reckon.""")
    time_budget: str | None = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: int | None = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)

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
    enforce: GateEnforcement | None = Field(default=None)
    require_evidence: bool | None = Field(default=None, description="""Whether a gate must produce recorded evidence to be considered met.""")
    on_fail: GateFailureAction | None = Field(default=None)
    suite_command: str | None = Field(default=None, description="""Project test-suite command whose presence arms promotion consequence checks. Absent means the project is unarmed; no command is supplied by shipped defaults.""")
    dimension_floors: dict[str, Union[int, DimensionFloor]] | None = Field(default=None, description="""Lowest score each review dimension may carry and still read as a pass, keyed by dimension name. A stored dimension below its floor is a finding that carries a disposition of its own rather than being folded into the review's total, so a low score cannot be averaged away by four high ones. Declared here beside the other gate settings rather than compiled in, so the standard a stored score was read against is readable by whoever is deciding what to do next. A dimension absent from the map has no floor: an undeclared floor is not a floor of zero, which would report every dimension of every review as a finding. Keys are the review schema's own dimension names; a key it does not define is ignored by the reader.""")


class BudgetConfig(ConfiguredBaseModel):
    """
    Thresholds that decide whether a wave opens. They are compared against whatever a backend actually reported; a backend that reports nothing is never held by them.
    """
    utilisation_ceiling_pct: float | None = Field(default=None, description="""Reported utilisation, as a percentage, at or above which a wave will not open. A backend reporting no headroom is never held by this: absence of a signal is not evidence of exhaustion, and a false hold stalls everything while a rejected call is cheap and announces itself.""", ge=0, le=100)
    resume_reserve_pct: float | None = Field(default=None, description="""Headroom withheld from new dispatches, in percentage points, so a worker that stops and asks for help can still be answered in its own session. A fresh dispatch stops at the ceiling less this reserve; answering a stuck worker may spend it. Spending the last of a quota on a new dispatch strands the wave in its worst state — work in flight and no way to unblock it.""", ge=0, le=100)
    coordinator_reserve_pct: float | None = Field(default=None, description="""Headroom withheld from new dispatches, in percentage points, so the coordinator that must survive a wave keeps headroom of its own. The reported account position already counts the coordinator's traffic, but reckon cannot see the total; a wave sized to fill the remaining headroom leaves nothing for the audit, merge and outcome-recording the coordinator does after the workers finish, so it can be refused mid-wave with every worker's output stranded in a worktree and nobody left to land it. A fresh dispatch stops at the ceiling less both this reserve and the resume reserve; a resume may spend either, because answering a stuck worker is the expenditure the reserves were withheld for. The figure is declared, not measured: there is no coordinator burn-rate record on this machine, and reading the account surface on a cadence is what calibrates it later.""", ge=0, le=100)
    exhausted_statuses: list[str] | None = Field(default=None, description="""Threshold-status values that count as exhausted whatever the utilisation reads — the overage question, answered as data so that the schema enumerates no backend's vocabulary. Empty leaves the ceiling as the only test.""")
    evidence_shelf_life_minutes: float | None = Field(default=None, description="""Minutes a refusal that names no reset time keeps describing the present before it is treated as stale and stops holding the wave on its own. Has no effect on a refusal that names a reset, since a stated reset is stronger evidence than an age and already carries its own expiry. A value at or below zero disables ageing and restores an indefinite hold.""")
    drain_lead_hours: float | None = Field(default=None, description="""How long before a group's weekly quota reset its budget should be drained, in hours. The pace deadline is the weekly period less this lead, so the week ends spent rather than exhausted mid-window with work still queued. A longer lead closes the deadline earlier and raises the share each remaining five-hour window is asked for; zero drains exactly at the reset, and a lead at or beyond the weekly period asks the next window for whatever remains.""", ge=0)
    pace_multiple: float | None = Field(default=None, description="""Deliberate lean above the linear share, applied on top of the same derivation for every declared budget group. A group exactly on pace returns this multiple times the nominal share of the weekly budget per five-hour window, rather than the bare nominal share, so the week converges on its deadline slightly ahead of it. A group's returned allowance records the multiple that produced it, because the multiple is retuned over a longer horizon than one week.""", ge=0)


class FenceConfig(ConfiguredBaseModel):
    """
    Limits a worker applies to itself before asking for help.
    """
    time_budget: str | None = Field(default=None, description="""Wall-clock allowance, written as an integer followed by a unit — `s`, `m` or `h`. It remains the ceiling that bounds a hang: a process that has stopped producing is only caught by elapsed wall clock, never by a token count.""")
    token_budget: int | None = Field(default=None, description="""Worker allowance denominated in generated output tokens — the quantity the same task needs regardless of what else the lane is doing, so a slow lane inside its token budget is not an overrun however long it took. Written as a bare integer of output tokens. Cannot bound a hang, so the wall-clock `time_budget` ceiling stays under its own name.""", ge=1)
    needs_help_after_failures: int | None = Field(default=None, description="""Consecutive failures after which a worker stops retrying and asks for help. Zero disables the fence.""", ge=0)
    manifest_required: bool | None = Field(default=None, description="""Whether a worker must write its manifest to the orchestrator-named path before its node counts as delivered.""")
    enforce_budget_watchdog: bool | None = Field(default=None, description="""Whether observation stops a live CLI worker after its declared time budget multiplied by the configured grace. Off by default; classification always reports the overrun without mutating the run.""")
    budget_grace_multiple: float | None = Field(default=None, description="""Multiple of a run's declared time budget allowed before opt-in watchdog enforcement stops it. Values below one would stop before the declared allowance elapsed.""", ge=1)
    unreconciled_run_grace: str | None = Field(default=None, description="""How long a complete or blocked worker manifest may remain as a live pointer before dispatch refuses more work for that project.""")

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
    cleanup: WorktreeCleanup | None = Field(default=None)
    scratch_min_free_bytes: int | None = Field(default=None, description="""Minimum free bytes on the worker scratch filesystem before a dispatch.""", ge=0)
    scratch_min_free_pct: int | None = Field(default=None, description="""Minimum free percentage on the worker scratch filesystem before a dispatch.""", ge=0, le=100)


class SummaryConfig(ConfiguredBaseModel):
    """
    When and how workers report.
    """
    reflex: str | None = Field(default=None, description="""The reporting shape a worker follows when it summarises.""")
    at: list[SummaryOccasion] | None = Field(default=None, description="""Occasions on which a worker emits a summary.""")


class TickerConfig(ConfiguredBaseModel):
    """
    Bounds on the follower pane's own memory. The follower keeps the rows it rendered so a re-arm can restore a reader's view rather than opening empty; the caps decide how much of that view comes back.
    """
    history_rows: int | None = Field(default=None, description="""Most rows a follower re-emits as history when it re-arms. A pane restored from a long outage should show recent work rather than the whole session, so the cap counts rendered rows and is applied with `history_window`, whichever admits fewer.""", ge=1)
    history_window: str | None = Field(default=None, description="""Oldest history row a follower re-emits when it re-arms, written as an integer followed by a unit — `s`, `m` or `h`. Trimmed against `history_rows`, whichever admits fewer, so a quiet session's short log is not replayed in full after a long gap.""")

    @field_validator('history_window')
    def pattern_history_window(cls, v):
        pattern=re.compile(r"^[0-9]+[smh]$")
        if isinstance(v, list):
            for element in v:
                if isinstance(element, str) and not pattern.match(element):
                    err_msg = f"Invalid history_window format: {element}"
                    raise ValueError(err_msg)
        elif isinstance(v, str) and not pattern.match(v):
            err_msg = f"Invalid history_window format: {v}"
            raise ValueError(err_msg)
        return v


# Model rebuild
# see https://pydantic-docs.helpmanual.io/usage/models/#rebuilding-a-model
FlightConfig.model_rebuild()
RoutingConfig.model_rebuild()
ReviewConfig.model_rebuild()
ReviewSuite.model_rebuild()
ReviewTiers.model_rebuild()
BackendConfig.model_rebuild()
LaneModelConfig.model_rebuild()
LaneConfig.model_rebuild()
PlacementConfig.model_rebuild()
PlacementRequirement.model_rebuild()
CatalogConfig.model_rebuild()
EnvironmentVariable.model_rebuild()
EffortSpelling.model_rebuild()
DimensionFloor.model_rebuild()
RoleConfig.model_rebuild()
SpecificationRouting.model_rebuild()
CapabilityClassRouting.model_rebuild()
CapabilityRaise.model_rebuild()
RoutingOverlay.model_rebuild()
GateConfig.model_rebuild()
BudgetConfig.model_rebuild()
FenceConfig.model_rebuild()
WorktreeConfig.model_rebuild()
SummaryConfig.model_rebuild()
TickerConfig.model_rebuild()
