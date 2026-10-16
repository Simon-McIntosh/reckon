# ruff: noqa: I001, UP035
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import (
    Path,
)
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
)
from reckon import (
    ledger,
)
from reckon.crew.node import (
    CrewError,
    PlanVisibilityError,
    TaskNode,
)
from reckon.crew.routing import (
    resolve_dispatch_authority,
    resolve_dispatch_ledger_root,
    shared_verdict_inputs,
)
from reckon.crew.runs import (
    _pointer_lock,
    _write_json,
    accepts_keyword,
    pointer_path,
    read_pointer,
    run_dir,
)


PICKER_DISPATCH_TIMEOUT_SECONDS = 5.0


def parse_orchestrator_assessments(statements: Iterable[str]) -> dict[str, str]:
    """Parse repeatable ``KEY=VALUE`` assessment statements, refusing by name.

    KEY must be a capability requirement key the schema declares and VALUE one
    of the values that key accepts. A statement naming neither is refused with
    the offending key and the accepted vocabulary in the message, so a caller
    corrects it because of what it's told rather than by reading the source. A
    repeated key takes its last value: later statements override earlier ones,
    the way a command line's later flag wins.
    """
    from reckon.capability import REQUIREMENT_LEVELS

    accepted_keys = sorted(REQUIREMENT_LEVELS)
    parsed: dict[str, str] = {}
    for statement in statements:
        key, separator, value = str(statement).partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            raise ValueError(
                f"--assess {statement!r} must be KEY=VALUE; accepted keys "
                f"{accepted_keys}"
            )
        levels = REQUIREMENT_LEVELS.get(key)
        if levels is None:
            raise ValueError(
                f"--assess {key!r}: unknown requirement key; accepted keys "
                f"{accepted_keys}"
            )
        if value not in levels:
            raise ValueError(
                f"--assess {key}={value!r}: {value!r} is not an accepted value "
                f"for {key!r}; accepted values {list(levels)!r}"
            )
        parsed[key] = value
    return parsed


# The dispatcher's own stages, in the order they run: the three inputs the
# dispatcher reads once (records, the verdict inputs, the budget snapshot) and
# the context estimate. A pick that misses its deadline names the stage still
# running in its fallback reason, so an expiry is attributed to the work that
# spent the budget rather than to the pick's own bound. The steps inside the
# picker call are not listed here: pick() names them itself in the sink it is
# handed, so a name is one vocabulary rather than two that can drift.
PICK_STAGES = (
    "records",
    "verdict-inputs",
    "budget-snapshot",
    "context-estimate",
)

# The dispatcher-scope inputs are built once, outside the pick's own bound, by
# the caller that then hands them in. Their elapsed milliseconds are stashed
# here for the pick that follows on the same thread to carry onto its selection,
# so a production pick records every one of the dispatcher's stages without the
# caller having to thread the timings through its own call.
_BUILD_STAGE_MS = threading.local()

# A picker answer carries per-call measurements: how long the ask itself took.
# They differ between two asks of the same node by construction, so a report
# that keeps them cannot be compared with a second run of the same call — which
# is the whole point of a dry run. The live run record keeps them, where the
# figures are the point; the resolved plan a preview reports drops them. The
# per-stage times are advisory measurements of the same kind and are dropped
# with them, so two previews of one node still compare on the decision.
_PICKER_PER_CALL_FIELDS = ("latency_ms", "jev_latency_ms", "stage_ms")


def _reportable_picker_selection(
    selection: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The picker answer a resolved plan records so two previews compare.

    The per-call latencies are removed, leaving the decision — the action, the
    selected backend and model, the probabilities and the reason — which is what
    a preview exists to show. Observation stamps reach the report already
    carrying their ``_at`` suffix, which the comparison masks as a clock reading
    rather than a decision.
    """
    if selection is None:
        return None
    return {
        key: value
        for key, value in selection.items()
        if key not in _PICKER_PER_CALL_FIELDS
    }


def _picker_fallback(
    reason: str,
    comment: str,
    *,
    input_errors: Mapping[str, str] | None = None,
    latency_ms: float | None = None,
    authority_error: str | None = None,
    stage_ms: Mapping[str, float | None] | None = None,
) -> dict[str, Any]:
    """The selection dispatch records when the picker did not decide one.

    One shape serves every fallback — a picker that raised, one that ran past
    its bound, and one whose inputs could not be built — so a reader settles
    each case by the ``fallback_reason`` rather than by which keys are present.
    The stages that finished before the pick gave up carry their elapsed
    milliseconds here, so a reader reaches the work that spent the budget even
    when the reason names only the stage still running.
    """
    client = sys.modules.get("reckon.crew.picker.client")
    return {
        "action": "fallback",
        "backend": None,
        "family": None,
        "model": None,
        "effort": None,
        "probabilities": {},
        "confidence": None,
        "jev_model": getattr(client, "JEV_MODEL", None),
        "fallback_reason": reason,
        "latency_ms": latency_ms,
        "stage_ms": dict(stage_ms) if stage_ms else {},
        "offered": [],
        "excluded": [],
        "comment": comment,
        **({"input_errors": dict(input_errors)} if input_errors else {}),
        **({"authority_error": authority_error} if authority_error else {}),
    }


def resolve_picker_authority(
    project: str, repo: Path
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the dispatch authority for a picker path that holds none.

    The dry-run preview and the deferred shadow pick hold no dispatcher-resolved
    authority, so they resolve the same one the main dispatch would and hand it
    into the pick. A resolution that fails returns ``None`` beside its reason so
    the failure is reported on the selection rather than swallowed: the granted
    landing fragment is then charged, and the reader who sees the figure can see
    why it was not exempted.
    """
    try:
        return resolve_dispatch_authority(project, repo), None
    except (CrewError, PlanVisibilityError, OSError, KeyError, TypeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _picker_ledger_rows(project: str, ledger_root: Path) -> list[dict[str, Any]]:
    return ledger.picker_runs(project, root=ledger_root)


def _picker_verdict_inputs(project: str, repo_root: Path) -> Mapping[str, Any]:
    return shared_verdict_inputs(project, repo_root)


def _picker_budget_snapshot(
    project: str,
    config: Mapping[str, Any],
    repo_root: Path,
    records: list[dict[str, Any]] | None,
    session: str = "",
) -> Mapping[str, Any]:
    from reckon.crew.picker import snapshot as picker_snapshot

    return picker_snapshot.budget_view(
        project, dict(config), repo_root, records, cached_only=True, session=session
    )


def _picker_input(
    name: str, build: Callable[[], Any], errors: dict[str, str]
) -> Any:
    """Build one dispatch-scope picker input, recording a failure instead of raising.

    These inputs are advisory: the picker re-reads whatever it is not handed, so
    a damaged ledger, a conflicting merge marker or a missing mount that makes
    one of them unreadable must leave the dispatch to reach its own verdict
    rather than abort the run's bookkeeping before the picker is consulted.
    """
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - a picker input never blocks dispatch
        errors[name] = f"{type(exc).__name__}: {exc}"
        return None


def build_picker_inputs(
    project: str,
    config: Mapping[str, Any],
    repo: str | Path,
    *,
    ledger_root: Path | None = None,
    stage_ms: dict[str, float | None] | None = None,
    running: dict[str, str | None] | None = None,
    session: str = "",
) -> tuple[
    list[dict[str, Any]] | None,
    Mapping[str, Any] | None,
    Mapping[str, Any] | None,
    dict[str, str],
]:
    """Build the three dispatch-scope picker inputs once, capturing failures.

    The ledger rows, the verdict inputs and the budget snapshot are read here,
    beside the pick, so their cost is paid outside the picker's own latency
    bound and the picker re-reads nothing per candidate. A caller that picks
    separately calls this too, so the same work is done once whichever entry
    asks. Each input is built behind its own guard: a failure is recorded
    against the input that failed and returned in the error map rather than
    raised, so the picker falls back with the failure named instead of the
    dispatch aborting before it is consulted.

    ``stage_ms``, when given, receives each input's elapsed milliseconds under
    its stage name, so a caller assembling a selection's per-stage times reads
    the three inputs' cost where it was actually paid rather than charging it
    to the picker call that consumed the figures. ``running``, when given,
    records the input currently being read so a caller that times out mid-build
    knows which input still had the budget when the deadline expired.

    The times are recorded even when the caller hands no ``stage_ms`` sink: they
    are stashed for the pick that follows on this thread, whose caller built
    these inputs outside its own bound and would otherwise carry no record of
    what they cost.

    ``session`` is the dispatching session the budget snapshot is composed for.
    A session-scoped lift releases the reserves only for the session it names,
    so the caller that holds the session hands it here and the snapshot reads the
    figures the picker will judge against. No session leaves the snapshot as a
    sessionless preflight composed it.
    """
    input_errors: dict[str, str] = {}
    repo_root = Path(repo)
    sink = stage_ms if stage_ms is not None else {}
    if stage_ms is None:
        _BUILD_STAGE_MS.times = sink

    def timed(stage: str, build: Callable[[], Any]) -> Any:
        if running is not None:
            running["stage"] = stage
        started = time.perf_counter()
        try:
            return build()
        finally:
            sink[stage] = round((time.perf_counter() - started) * 1000, 3)
            if running is not None:
                running["stage"] = None

    def build_records() -> list[dict[str, Any]] | None:
        nonlocal_ledger_root = ledger_root
        if nonlocal_ledger_root is None:
            nonlocal_ledger_root = _picker_input(
                "records",
                lambda: resolve_dispatch_ledger_root(
                    resolve_dispatch_authority(project, repo_root)
                ),
                input_errors,
            )
        if nonlocal_ledger_root is None:
            return None
        return _picker_input(
            "records",
            lambda: _picker_ledger_rows(project, nonlocal_ledger_root),
            input_errors,
        )

    records = timed("records", build_records)
    verdict_inputs = timed(
        "verdict-inputs",
        lambda: _picker_input(
            "verdict_inputs",
            lambda: _picker_verdict_inputs(project, repo_root),
            input_errors,
        ),
    )
    budget_snapshot = timed(
        "budget-snapshot",
        lambda: _picker_input(
            "budget_snapshot",
            lambda: _picker_budget_snapshot(
                project, config, repo_root, records, session
            ),
            input_errors,
        ),
    )
    return records, verdict_inputs, budget_snapshot, input_errors


def dispatch_picker_selection(
    *,
    node: TaskNode,
    config: Mapping[str, Any],
    project: str,
    repo: Path,
    session: str = "",
    comment: str = "",
    records: list[dict[str, Any]] | None = None,
    verdict_inputs: Mapping[str, Any] | None = None,
    budget_snapshot: Mapping[str, Any] | None = None,
    input_errors: Mapping[str, str] | None = None,
    authority: Mapping[str, Any] | None = None,
    authority_error: str | None = None,
    orchestrator_assessment: Mapping[str, Any] | None = None,
    stage_ms: dict[str, float | None] | None = None,
    _allow_reask: bool = True,
) -> dict[str, Any]:
    """Ask the picker without letting its latency or failure stop dispatch.

    Every stage of the ask is timed and carried on the returned selection, and a
    pick that misses the deadline names the stage still running in its fallback
    reason — ``timeout in budget-snapshot`` or ``timeout in context-estimate``
    for the dispatcher's own stages, and ``timeout in pick:<step>`` for a step
    inside the picker call, which pick() marks in a sink handed to it rather
    than being named from a list kept here. A caller that hands ``stage_ms`` and
    no inputs has the dispatcher-scope inputs read here, inside the bound, so a
    pick whose rendered state render, estimate or Jev call outlasts the bound
    is named rather than read as the bare ``timeout``.
    """
    if input_errors:
        # An input the dispatcher could not build is not re-read here: the same
        # source that failed once would only fail again, so the picker's own
        # pick is skipped and the failure is named in the recorded fallback.
        # This pick consumes no build times, so the stash is cleared here too: a
        # builder that failed part-way left a partial map on the thread, and a
        # later pick on it must read its own build rather than this one's.
        _BUILD_STAGE_MS.times = None
        return _picker_fallback(
            "; ".join(f"{name}: {detail}" for name, detail in input_errors.items()),
            comment,
            input_errors=input_errors,
            latency_ms=0.0,
            authority_error=authority_error,
        )
    finished = threading.Event()
    result: dict[str, Any] = {}
    started = time.monotonic()
    stages: dict[str, float | None] = {name: None for name in PICK_STAGES}
    if stage_ms:
        stages.update(stage_ms)
    elif records is not None or verdict_inputs is not None or budget_snapshot is not None:
        # A caller that built the dispatcher-scope inputs and handed them in left
        # their cost stashed here; carry it onto the selection so every
        # dispatcher stage is recorded, then clear it so a later pick cannot
        # read a stale build.
        built = getattr(_BUILD_STAGE_MS, "times", None)
        _BUILD_STAGE_MS.times = None
        if built:
            stages.update({name: built[name] for name in built if name in stages})
    # The dispatcher stage the ask is inside when the deadline expires, so a
    # fallback names the work that spent the budget rather than the call it
    # overlapped. The context estimate sets it while it runs, since it is the
    # stage before the picker call and the one a production pick spends its
    # budget in. The steps inside the picker call set ``pick_running`` instead,
    # in pick()'s own vocabulary, so the two are named from the work that ran.
    running: dict[str, str | None] = {"stage": None}
    pick_running: dict[str, str | None] = {"step": None}

    def marked(stage: str, build: Callable[[], Any]) -> Any:
        """Run one timed stage, recording the stage the ask is inside."""
        running["stage"] = stage
        moment = time.perf_counter()
        try:
            return build()
        finally:
            stages[stage] = round((time.perf_counter() - moment) * 1000, 3)
            running["stage"] = None

    def ask() -> None:
        try:
            from reckon.crew.picker import PickRequest, pick
            from reckon.crew.picker import snapshot as picker_snapshot

            # The dispatcher's own authority travels into the estimate and the
            # pick, so a dispatcher-granted landing fragment is exempt in the
            # figure Jev weighs exactly as it is in every candidate's context-fit
            # verdict. A caller that does not hold the resolved authority (the
            # advisory shadow path, the dry-run pick) resolves the same one here;
            # a resolution that fails leaves the picker with no authority rather
            # than aborting an advisory.
            pick_authority = authority
            pick_authority_error = authority_error
            if pick_authority is None and pick_authority_error is None:
                pick_authority, pick_authority_error = resolve_picker_authority(
                    project, repo
                )
            if pick_authority_error is not None:
                result["authority_error"] = pick_authority_error

            # The dispatcher-scope inputs are read inside the bound only when the
            # caller handed none and asked for per-stage accounting: a build a
            # caller already made must not be paid twice, and its stage times are
            # already carried on ``stages`` from the build that measured them.
            pick_records, pick_verdicts, pick_budget = (
                records,
                verdict_inputs,
                budget_snapshot,
            )
            if (
                pick_records is None
                and pick_verdicts is None
                and pick_budget is None
                and stage_ms is not None
            ):
                pick_records, pick_verdicts, pick_budget, _ = build_picker_inputs(
                    project,
                    config,
                    repo,
                    stage_ms=stages,
                    running=running,
                    session=session,
                )

            # The estimate is the same deterministic measurement the context-fit
            # verdict charges a node against, measured with the same authority
            # and the same harness-independent standing chain, so the request's
            # figure and every candidate block's are one. It is measured here,
            # before the pick's own bound, so its duration is a real census and
            # the pick reuses the figure rather than measuring a second time;
            # both the figure and its duration travel into the pick. It is
            # advisory: a failure to measure leaves the figure at zero and never
            # aborts the pick.
            def measure_estimate() -> int:
                try:
                    return picker_snapshot.estimated_context_tokens(
                        node, repo, authority=pick_authority
                    )
                except Exception:  # noqa: BLE001 - the estimate is advisory to the pick
                    return 0

            estimated_context = marked("context-estimate", measure_estimate)
            estimated_context_ms = stages["context-estimate"]

            inputs = {
                "records": pick_records,
                "verdict_inputs": pick_verdicts,
                "budget_snapshot": pick_budget,
                "authority": pick_authority,
                "estimated_context_ms": estimated_context_ms,
                "running": pick_running,
            }
            inputs = {
                key: value
                for key, value in inputs.items()
                if accepts_keyword(pick, key)
            }
            # The pick times and names each of its own steps and marks the one
            # it is inside in ``pick_running``, so an expiry during the call is
            # named from pick()'s own vocabulary rather than from a step list
            # kept here. The sink travels in with the other inputs and is
            # dropped for any pick that does not accept it.
            selection = pick(
                PickRequest(
                    project,
                    node,
                    comment=comment,
                    session=session,
                    estimated_context=estimated_context,
                    orchestrator_assessment=dict(orchestrator_assessment or {}),
                ),
                dict(config),
                repo=repo,
                cached_only=True,
                **inputs,
            )
            result["selection"] = selection.as_dict()
        except Exception as exc:  # noqa: BLE001 - shadow routing cannot block dispatch
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            finished.set()

    threading.Thread(target=ask, name="dispatch-picker", daemon=True).start()
    if not finished.wait(PICKER_DISPATCH_TIMEOUT_SECONDS):
        result["error"] = "timeout"
    authority_error = result.get("authority_error")
    if "selection" in result and "error" not in result:
        selection = {**result["selection"], "stage_ms": dict(stages)}
        if authority_error:
            selection["authority_error"] = authority_error
        return _reask_off_zero_slot_lane(
            selection,
            node=node,
            config=config,
            project=project,
            repo=repo,
            session=session,
            comment=comment,
            authority=authority,
            authority_error=authority_error,
            orchestrator_assessment=orchestrator_assessment,
            allow=_allow_reask,
        )
    error = result.get("error") or "picker returned no selection"
    reason = error
    if error == "timeout":
        # A step pick() marked while it ran is named in pick()'s vocabulary; a
        # pick that marked none keeps the bare reading, as does one that spent
        # the budget in a dispatcher stage instead.
        pick_step = pick_running["step"]
        if pick_step:
            reason = f"timeout in pick:{pick_step}"
        elif running["stage"]:
            reason = f"timeout in {running['stage']}"
    return _picker_fallback(
        reason,
        comment,
        latency_ms=round((time.monotonic() - started) * 1000, 3),
        authority_error=authority_error,
        stage_ms=stages,
    )


# The specification levels a route must not be swallowed by at a lane granting
# the session no worker. A node whose design is fixed and only the
# implementation remains (exact), and one whose implementation is derived from a
# settled design (guided), are the well-defined work the lead's rule sends to a
# lane with room. An open node must also choose its design, so a hold for the
# local lane is a legitimate answer for it and is left alone.
_ZERO_SLOT_REASK_ROLES = ("implement", "test")
_ZERO_SLOT_REASK_SPEC_LEVELS = ("exact", "guided")


def _zero_slot_lane(selection: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    """The lane a route landed on that grants this session no worker, or ``""``.

    The figure is the one the state offered Jev, read back from the candidate it
    chose: the session's own worker slots as the lane's router published them.
    Zero or below is the router granting no room; a null figure is no published
    reading, which is unknown rather than a hold, so neither a refusal nor an
    unknown figure triggers a re-ask.
    """
    if str(selection.get("action") or "") != "route":
        return ""
    backend = str(selection.get("backend") or "")
    if not backend:
        return ""
    for row in selection.get("offered") or ():
        if not isinstance(row, Mapping) or str(row.get("backend")) != backend:
            continue
        slots = row.get("worker_slots")
        if isinstance(slots, bool) or not isinstance(slots, (int, float)):
            return ""
        if slots > 0:
            return ""
        return str(row.get("family") or row.get("lane") or backend)
    return ""


def lane_family(
    config: Mapping[str, Any],
    name: str,
    backend: Mapping[str, Any] | None = None,
) -> str:
    """The lane family one configured backend belongs to, read as the state reads it.

    A candidate's lane is the local marker, the backend's resolved dialect, or --
    when its command names no dialect -- its launch kind. The local marker is the
    backend the configuration names as its ``local_backend``; every other backend
    is read through :func:`reckon._backends.dialect_for` on the backend mapping
    handed in, which a caller may pass already resolved against a role overlay.
    One owner for the derivation so a candidate's lane and an exclusion's lane are
    the same reading, never a backend's declared ``lane`` key, which a
    configuration need not set.
    """
    if name and name == str(config.get("local_backend") or ""):
        return "local"
    if backend is None:
        backend = (config.get("backends") or {}).get(name)
    if not isinstance(backend, Mapping):
        return name
    from reckon import _backends

    try:
        return _backends.dialect_for(backend).name
    except _backends.BackendError:
        return str(backend.get("launch") or name)


def _lane_backend_names(config: Mapping[str, Any], lane: str) -> set[str]:
    """The configured backends belonging to one lane, read as the state reads it.

    A candidate's lane is the local marker or the backend's resolved dialect,
    so the same derivation is used here rather than a backend's declared
    ``lane`` key, which a configuration need not set and which would leave a
    metered lane's backends behind unexcluded.
    """
    names: set[str] = set()
    for name, backend in (config.get("backends") or {}).items():
        if not isinstance(backend, Mapping):
            continue
        if lane_family(config, str(name), backend) == lane:
            names.add(str(name))
    return names


def _config_without_lane(config: Mapping[str, Any], lane: str) -> dict[str, Any]:
    """The configuration with one lane's backends removed from the offering."""
    excluded = _lane_backend_names(config, lane)
    return {
        **config,
        "backends": {
            name: backend
            for name, backend in (config.get("backends") or {}).items()
            if str(name) not in excluded
        },
    }


def _ask_summary(selection: Mapping[str, Any]) -> dict[str, Any]:
    """One recorded ask, compact enough to keep both beside each other."""
    return {
        "action": selection.get("action"),
        "backend": selection.get("backend"),
        "lane": selection.get("family"),
        "model": selection.get("model"),
        "confidence": selection.get("confidence"),
        "probabilities": dict(selection.get("probabilities") or {}),
        "offered": [
            row.get("backend")
            for row in selection.get("offered") or ()
            if isinstance(row, Mapping)
        ],
    }


def _ask_excluding_lane(
    lane: str,
    first_ask: Mapping[str, Any],
    *,
    node: Any,
    config: Mapping[str, Any],
    project: str,
    repo: Path,
    session: str,
    comment: str = "",
    authority: Mapping[str, Any] | None = None,
    authority_error: str | None = None,
    orchestrator_assessment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask Jev once more with one lane left out of the offering, keeping both asks.

    The zero-slot re-ask and the reflex's held-review retry are the same question
    with different triggers: each asks a second time with the refusing lane
    excluded and records the first ask beside the answer, so the decision is
    visible. The second ask is asked with the re-ask disabled, so a hold it lands
    on is not chased further. Its selection is returned with ``asks`` carrying the
    recorded pair -- the first ask as the caller summarized it, and the second.
    """
    second = dispatch_picker_selection(
        node=node,
        config=_config_without_lane(config, lane),
        project=project,
        repo=repo,
        session=session,
        comment=comment,
        authority=authority,
        authority_error=authority_error,
        orchestrator_assessment=orchestrator_assessment,
        _allow_reask=False,
    )
    return {**second, "asks": [dict(first_ask), _ask_summary(second)]}


def _reask_off_zero_slot_lane(
    selection: Mapping[str, Any],
    *,
    node: Any,
    config: Mapping[str, Any],
    project: str,
    repo: Path,
    session: str,
    comment: str,
    authority: Mapping[str, Any] | None,
    authority_error: str | None,
    orchestrator_assessment: Mapping[str, Any] | None,
    allow: bool,
) -> dict[str, Any]:
    """Re-ask Jev once with a zero-slot lane excluded, for well-defined work.

    A route that lands on a lane whose router grants this session no worker is
    the case that queued well-defined work behind a full lane while another lane
    had room. Jev is asked a second time with that lane left out of the
    offering, so the answer it gives with the lane's pressure in view is not the
    only thing it was able to say; the second answer stands, and both asks are
    recorded on the selection so the decision is visible. A node that may choose
    its own design, one already at the re-ask, and a hold or refusal are left
    exactly as the first ask answered them.
    """
    if not allow:
        return dict(selection)
    lane = _zero_slot_lane(selection, config)
    if not lane:
        return dict(selection)
    role = str(getattr(node, "role", "") or "")
    if role not in _ZERO_SLOT_REASK_ROLES:
        return dict(selection)
    spec_level = str(getattr(node, "spec_level", "") or "")
    if spec_level not in _ZERO_SLOT_REASK_SPEC_LEVELS:
        return dict(selection)
    second = _ask_excluding_lane(
        lane,
        _ask_summary(selection),
        node=node,
        config=config,
        project=project,
        repo=repo,
        session=session,
        comment=comment,
        authority=authority,
        authority_error=authority_error,
        orchestrator_assessment=orchestrator_assessment,
    )
    return {
        **second,
        "action": second.get("action") or "refuse",
        "excluded_zero_slot_lane": lane,
        "reissued": True,
    }


def _write_existing_pointer(run_id: str, record: Mapping[str, Any]) -> bool:
    """Write a present pointer while its caller holds the removal lock.

    The run directory checks also keep a removed directory from being restored
    by a late supervisor write.
    """
    path = pointer_path(run_id)
    directory = run_dir(run_id)
    if not path.exists() or not directory.is_dir():
        return False
    _write_json(path, record)
    if not directory.is_dir():
        path.unlink(missing_ok=True)
        return False
    return True


def _attach_shadow_picker_selection(run_id: str, selection: Mapping[str, Any]) -> None:
    """Update a live pointer without recreating a discarded run."""
    with _pointer_lock(run_id):
        if pointer_path(run_id).exists():
            pointer = read_pointer(run_id)
            pointer["picker_selection"] = dict(selection)
            _write_existing_pointer(run_id, pointer)


def _record_shadow_picker_selection(spec_path: Path) -> None:
    """Finish an advisory pick after launch and attach it to the live run."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    node = TaskNode(**spec["node"])
    repo = Path(spec["repo"])
    result: dict[str, Any] = {}
    finished = threading.Event()
    started = time.monotonic()

    def pick_shadow() -> None:
        try:
            records, inputs, budget, errors = build_picker_inputs(
                spec["project"],
                spec["config"],
                repo,
                ledger_root=Path(spec["ledger_root"]),
                session=spec["session"],
            )
            # The deferred shadow pick holds no dispatcher-resolved authority,
            # so it resolves the same one and passes it, reporting a resolution
            # failure on the selection rather than leaving the granted fragment
            # silently charged.
            authority, authority_error = resolve_picker_authority(
                spec["project"], repo
            )
            result["selection"] = dispatch_picker_selection(
                node=node,
                config=spec["config"],
                project=spec["project"],
                repo=repo,
                session=spec["session"],
                comment=spec["comment"],
                records=records,
                verdict_inputs=inputs,
                budget_snapshot=budget,
                input_errors=errors,
                authority=authority,
                authority_error=authority_error,
            )
        except Exception as exc:  # noqa: BLE001 - an advisory cannot stop a run
            result["selection"] = _picker_fallback(
                f"{type(exc).__name__}: {exc}", spec["comment"]
            )
        finally:
            finished.set()

    threading.Thread(target=pick_shadow, name="shadow-picker", daemon=True).start()
    if not finished.wait(PICKER_DISPATCH_TIMEOUT_SECONDS):
        result["selection"] = _picker_fallback(
            "timeout",
            spec["comment"],
            latency_ms=round((time.monotonic() - started) * 1000, 3),
        )
    selection = result["selection"]
    _attach_shadow_picker_selection(spec["run_id"], selection)


def _start_shadow_picker_selection(
    *,
    run_id: str,
    node: TaskNode,
    config: Mapping[str, Any],
    project: str,
    repo: Path,
    ledger_root: Path,
    session: str,
    comment: str,
) -> None:
    """Start a bounded detached reader without extending dispatch's lifetime."""
    directory = run_dir(run_id)
    spec_path = directory / "shadow-picker.json"
    _write_json(
        spec_path,
        {
            "run_id": run_id,
            "node": node.as_dict(),
            "config": dict(config),
            "project": project,
            "repo": str(repo),
            "ledger_root": str(ledger_root),
            "session": session,
            "comment": comment,
        },
    )
    log = (directory / "shadow-picker.log").open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from reckon.crew.dispatch import _record_shadow_picker_selection; import sys; _record_shadow_picker_selection(Path(sys.argv[1]))",
                str(spec_path),
            ],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[2])
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            },
        )
    finally:
        log.close()


def _picker_refusal_reasons(selection: Mapping[str, Any]) -> str:
    reasons = [
        str(selection[key])
        for key in ("reason", "fallback_reason")
        if selection.get(key)
    ]
    if selection.get("action") == "hold":
        confidence = selection.get("confidence")
        hold_probability = (selection.get("probabilities") or {}).get("hold")
        detail = "picker selected hold"
        if isinstance(confidence, (int, float)):
            detail += f" at confidence {confidence:g}"
        if isinstance(hold_probability, (int, float)):
            detail += f" (hold probability {hold_probability:g})"
        reasons.insert(0, detail)
    reasons.extend(
        str(reason)
        for candidate in selection.get("excluded") or ()
        for reason in candidate.get("reasons") or ()
    )
    return "; ".join(dict.fromkeys(reasons)) or "no eligible backend"


def resolve_dispatch_route(config: Mapping[str, Any], route: str | None) -> str:
    """Resolve a per-dispatch override before the layered picker setting."""
    if route is not None:
        if route not in {"shadow", "picker", "deterministic"}:
            raise CrewError(f"unknown dispatch route {route!r}")
        return route
    mode = (config.get("routing") or {}).get("picker", "shadow")
    if mode not in {"shadow", "route"}:
        raise CrewError(f"unknown routing.picker value {mode!r}")
    return "picker" if mode == "route" else "shadow"
