# ruff: noqa: I001, UP035
from __future__ import annotations
import inspect
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
    pointer_path,
    read_pointer,
    run_dir,
)



PICKER_DISPATCH_TIMEOUT_SECONDS = 5.0

# The stages a dispatch-path pick records elapsed milliseconds for, in the order
# they run: the three inputs the dispatcher reads once (records, the verdict
# inputs, the budget snapshot), the context estimate, and the picker call. A
# pick that misses its deadline names the stage still running in its fallback
# reason, so an expiry is attributed to the work that spent the budget rather
# than to the pick's own bound. The estimate and the picker call run inside the
# bound; the three inputs are read before it, so they are timed where they run
# and merged onto the selection rather than charged to the picker call.
PICK_STAGES = (
    "records",
    "verdict-inputs",
    "budget-snapshot",
    "context-estimate",
    "jev",
)

# The dispatcher-scope inputs are built once, outside the pick's own bound, by
# the caller that then hands them in. Their elapsed milliseconds are stashed
# here for the pick that follows on the same thread to carry onto its selection,
# so a production pick records all five stages' cost without the caller having
# to thread the timings through its own call.
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
) -> Mapping[str, Any]:
    from reckon.crew.picker import snapshot as picker_snapshot

    return picker_snapshot.budget_view(
        project, dict(config), repo_root, records, cached_only=True
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
            lambda: _picker_budget_snapshot(project, config, repo_root, records),
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
    stage_ms: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Ask the picker without letting its latency or failure stop dispatch.

    Every stage of the ask is timed and carried on the returned selection, and a
    pick that misses the deadline names the stage still running in its fallback
    reason (``timeout in budget-snapshot`` or ``timeout in context-estimate``)
    with the finished stages' milliseconds beside it. A caller that hands
    ``stage_ms`` and no inputs has the dispatcher-scope inputs read here, inside
    the bound, so a slow input is attributed to itself; a caller that built the
    inputs outside the bound keeps them, and their cost is carried here from the
    build that measured it — so a production pick records all five stages either
    way, and names the estimate it was inside rather than the bare ``timeout``.
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
        # their cost stashed here; carry it onto the selection so all five stages
        # are recorded, then clear it so a later pick cannot read a stale build.
        built = getattr(_BUILD_STAGE_MS, "times", None)
        _BUILD_STAGE_MS.times = None
        if built:
            stages.update({name: built[name] for name in built if name in stages})
    # The stage the ask is inside when the deadline expires, so a fallback names
    # the work that spent the budget rather than the call it overlapped. The
    # context estimate sets it while it runs, since it is the stage before the
    # picker call and the one that spends a production pick's budget; the picker
    # call itself is opaque and leaves the bare ``timeout``, recording its
    # duration beside it for the reader.
    running: dict[str, str | None] = {"stage": None}

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
            }
            parameters = inspect.signature(pick).parameters.values()
            if not any(item.kind is item.VAR_KEYWORD for item in parameters):
                accepted = {item.name for item in parameters}
                inputs = {
                    key: value for key, value in inputs.items() if key in accepted
                }
            # The picker call is timed but not marked: its own snapshot, render
            # and Jev stages are inside it, so an expiry during the call is
            # attributed to no single dispatch stage and keeps the bare
            # ``timeout`` rather than naming a stage the dispatcher cannot see.
            moment = time.perf_counter()
            try:
                selection = pick(
                    PickRequest(
                        project,
                        node,
                        comment=comment,
                        session=session,
                        estimated_context=estimated_context,
                    ),
                    dict(config),
                    repo=repo,
                    cached_only=True,
                    **inputs,
                )
            finally:
                stages["jev"] = round((time.perf_counter() - moment) * 1000, 3)
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
        return selection
    error = result.get("error") or "picker returned no selection"
    reason = error
    if error == "timeout" and running["stage"]:
        reason = f"timeout in {running['stage']}"
    return _picker_fallback(
        reason,
        comment,
        latency_ms=round((time.monotonic() - started) * 1000, 3),
        authority_error=authority_error,
        stage_ms=stages,
    )


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
