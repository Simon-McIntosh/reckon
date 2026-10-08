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

# A picker answer carries per-call measurements: how long the ask itself took.
# They differ between two asks of the same node by construction, so a report
# that keeps them cannot be compared with a second run of the same call — which
# is the whole point of a dry run. The live run record keeps them, where the
# figures are the point; the resolved plan a preview reports drops them.
_PICKER_PER_CALL_FIELDS = ("latency_ms", "jev_latency_ms")


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
) -> dict[str, Any]:
    """The selection dispatch records when the picker did not decide one.

    One shape serves every fallback — a picker that raised, one that ran past
    its bound, and one whose inputs could not be built — so a reader settles
    each case by the ``fallback_reason`` rather than by which keys are present.
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
    """
    input_errors: dict[str, str] = {}
    repo_root = Path(repo)
    if ledger_root is None:
        ledger_root = _picker_input(
            "records",
            lambda: resolve_dispatch_ledger_root(
                resolve_dispatch_authority(project, repo_root)
            ),
            input_errors,
        )
    records = (
        _picker_input(
            "records",
            lambda: _picker_ledger_rows(project, ledger_root),
            input_errors,
        )
        if ledger_root is not None
        else None
    )
    verdict_inputs = _picker_input(
        "verdict_inputs",
        lambda: _picker_verdict_inputs(project, repo_root),
        input_errors,
    )
    budget_snapshot = _picker_input(
        "budget_snapshot",
        lambda: _picker_budget_snapshot(project, config, repo_root, records),
        input_errors,
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
) -> dict[str, Any]:
    """Ask the picker without letting its latency or failure stop dispatch."""
    if input_errors:
        # An input the dispatcher could not build is not re-read here: the same
        # source that failed once would only fail again, so the picker's own
        # pick is skipped and the failure is named in the recorded fallback.
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

            # The estimate is the same deterministic measurement the context-fit
            # verdict charges a node against, measured with the same authority
            # and the same harness-independent standing chain, so the request's
            # figure and every candidate block's are one. It is measured here,
            # before the pick's own bound, so its duration is a real census and
            # the pick reuses the figure rather than measuring a second time;
            # both the figure and its duration travel into the pick. It is
            # advisory: a failure to measure leaves the figure at zero and never
            # aborts the pick.
            estimate_started = time.perf_counter()
            try:
                estimated_context = picker_snapshot.estimated_context_tokens(
                    node, repo, authority=pick_authority
                )
            except Exception:  # noqa: BLE001 - the estimate is advisory to the pick
                estimated_context = 0
            estimated_context_ms = round(
                (time.perf_counter() - estimate_started) * 1000, 3
            )

            inputs = {
                "records": records,
                "verdict_inputs": verdict_inputs,
                "budget_snapshot": budget_snapshot,
                "authority": pick_authority,
                "estimated_context_ms": estimated_context_ms,
            }
            parameters = inspect.signature(pick).parameters.values()
            if not any(item.kind is item.VAR_KEYWORD for item in parameters):
                accepted = {item.name for item in parameters}
                inputs = {
                    key: value for key, value in inputs.items() if key in accepted
                }
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
        selection = result["selection"]
        if authority_error:
            selection = {**selection, "authority_error": authority_error}
        return selection
    return _picker_fallback(
        result.get("error") or "picker returned no selection",
        comment,
        latency_ms=round((time.monotonic() - started) * 1000, 3),
        authority_error=authority_error,
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
