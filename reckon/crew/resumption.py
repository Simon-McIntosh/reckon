"""Resume runs after a provider hold or declared external wait ends.

A per-request refusal stops a worker without breaking anything: the account is
spent until a moment the refusal names, the session still holds every turn of
the worker's orientation, and the worktree it was oriented against is still on
disk. The only thing missing minutes later is the word *continue* — and until
now a person had to notice the outage and type it, which is the wrong shape for
a failure whose defining property is happening while nobody is watching.

Eligibility needs no provider call. A refusal naming a reset time is dated by
it; one naming none ages out of the shelf life declared in flight
configuration. Both readings come from records already on disk, so the sweep
costs nothing to run and can therefore run often.

An external wait is explicit worker delivery rather than an inferred stop. Its
manifest names the condition, a shell-free probe argument vector, the returned
states that mean terminal, and the brief for the resumed turn. The same sweep
checks that trigger on its cadence and leaves the run alone until it terminates.

Four bounds keep the sweep from becoming its own hazard, and each is checked
before anything is launched:

* **At most one resume per trigger.** A resumed run is stamped with the hold or
  condition it was resumed for. A fresh refusal or rewritten waiting manifest
  carries a new identity, so it can be judged independently.
* **Never onto a lane still held.** The resume plan is built through the same
  budget verdict a hand-typed resume passes, so a lane whose hold is in force
  refuses here too and the sweep can never spend the last of a recovering quota.
* **Never into a scope another run owns.** A write path claimed by a live run
  is a collision this would be creating rather than recovering from.
* **Never without a worktree.** A resume has no working directory to start in,
  and papering over that hides the very state a retained tree exists to prevent.

Every decision is reported with its reason, and every sweep that touched
anything is appended to a per-project record, so an operator reading afterwards
sees the recovery happen rather than inferring it from a run that is suddenly
alive again.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _backends, ledger
from reckon import budget as budget_module
from reckon.crew.dispatch import (
    BudgetHold,
    _backend_settings,
    _spawn,
    record_resumption,
    resume_plan,
)
from reckon.crew.node import CrewError
from reckon.crew.recovery import _stream_refusal_block, classify_pointer, external_wait
from reckon.crew.runs import (
    _manifest_mtime_ns,
    _pointer_lock,
    _utc_now,
    _write_json,
    crew_home,
    list_live,
    pointer_path,
    process_alive,
    read_pointer,
    run_dir,
)

# What the resumed worker is told. It states the two things the worker cannot
# know from inside its own stopped turn — that the limit which stopped it has
# passed, and that nothing else about its node has changed — and then asks for
# the only thing it needs to do, which is carry on.
CONTINUE_ADVICE = (
    "the provider limit that stopped this run has reset; continue from where "
    "you stopped, keeping the same node, fence and manifest path"
)

# How often the follower may sweep. Long enough that a tight poll loop cannot
# turn the sweep into a hot path, short enough that a lane returning minutes
# later is picked up while the wave still matters.
DEFAULT_SWEEP_SECONDS = 120.0

# A served request is current availability evidence, but only briefly. The
# cache exists to turn a wave of callers into one provider request; it never
# turns a historical observation into a long-lived verdict.
DEFAULT_AVAILABILITY_PROBE_CACHE_SECONDS = 60.0
AVAILABILITY_PROBE_PROMPT = "Reply with OK."

# What a sweep writer can call itself, so the status record can tell a
# reader's own follower apart from a hand-run pass without a process lookup.
# A follower keeps its own entry; the other kinds share one slot each, because
# they have no durable identity a reader could name.
SWEEP_WRITER_KINDS = frozenset({"follower", "command", "mcp", "other"})

# The status record keeps at most this many writer entries. Hand-run, MCP and
# other callers each collapse to a single slot, so the only unbounded growth is
# distinct follower sessions; every session that ends leaves one stale entry,
# and this cap evicts the least recently swept so the file stays bounded while
# a reader who saw many sessions keeps their history.
MAX_SWEEP_WRITERS = 8


def _parse_stamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp, returning None for anything unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(tz=UTC)


def _safe_project(project: str) -> str:
    return (
        "".join(
            character if character.isalnum() or character in "._-" else "-"
            for character in str(project)
        ).strip("-")
        or "project"
    )


def lane_probe_cache_path(project: str, backend_name: str) -> Path:
    """Return one backend's durable availability observation cache."""
    return (
        crew_home()
        / "recovery"
        / "lane-probes"
        / _safe_project(project)
        / f"{_safe_project(backend_name)}.json"
    )


@contextmanager
def _lane_probe_lock(project: str, backend_name: str):
    """Serialize cache refreshes so concurrent callers issue one request."""
    path = lane_probe_cache_path(project, backend_name).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_lane_probe_cache(project: str, backend_name: str) -> dict[str, Any]:
    path = lane_probe_cache_path(project, backend_name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_lane_probe_cache(
    project: str, backend_name: str, observation: Mapping[str, Any]
) -> None:
    path = lane_probe_cache_path(project, backend_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(observation), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _normalise_lane_probe(
    backend_name: str,
    result: Mapping[str, Any] | None,
    *,
    observed_at: str,
) -> dict[str, Any]:
    """Return the closed availability observation shape used by the fence."""
    result = result if isinstance(result, Mapping) else {}
    status = str(result.get("status") or "unavailable")
    if status not in {"served", "refused", "unavailable"}:
        status = "unavailable"
    return {
        "backend": backend_name,
        "status": status,
        "observed_at": str(result.get("observed_at") or observed_at),
        "detail": str(
            result.get("detail")
            or (
                f"the minimal request was {status}"
                if status != "unavailable"
                else "availability probe returned no result"
            )
        ),
        "budget": dict(result.get("budget") or {}),
        "cached": False,
    }


def _request_lane_availability(
    backend_name: str,
    backend: Mapping[str, Any],
    *,
    root: str | Path | None,
) -> dict[str, Any]:
    """Issue the smallest supported model request and classify its stream."""
    if backend.get("launch") != "cli":
        return {
            "status": "unavailable",
            "detail": "the host cannot spawn an in-harness backend for a probe",
        }
    worktree = Path(root or ".").resolve()
    if not worktree.is_dir():
        return {
            "status": "unavailable",
            "detail": f"the probe working directory {worktree} is unavailable",
        }
    directory = crew_home() / "recovery" / "lane-probes" / "requests"
    directory.mkdir(parents=True, exist_ok=True)
    safe_backend = _safe_project(backend_name)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%f")
    log_path = directory / f"{safe_backend}-{stamp}.jsonl"
    stderr_path = directory / f"{safe_backend}-{stamp}.stderr.log"
    final_path = directory / f"{safe_backend}-{stamp}.final.txt"
    try:
        plan = _backends.launch_plan(
            backend_name=backend_name,
            backend=backend,
            prompt=AVAILABILITY_PROBE_PROMPT,
            worktree=worktree,
            final_message_path=final_path,
        )
        completed = subprocess.run(
            plan.argv,
            cwd=plan.cwd,
            env={**os.environ, **plan.environment},
            input=plan.stdin_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        log_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=log_path,
        )
    except (_backends.BackendError, OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "detail": f"lane probe could not run: {exc}"}
    if observation.budget.get("refusal"):
        return {
            "status": "refused",
            "detail": observation.detail or "the minimal request was refused",
            "budget": observation.budget,
        }
    if completed.returncode == 0:
        return {
            "status": "served",
            "detail": observation.detail or "the minimal request was served",
            "budget": observation.budget,
        }
    return {
        "status": "unavailable",
        "detail": f"lane probe exited {completed.returncode} without availability evidence",
        "budget": observation.budget,
    }


def probe_lane_availability(
    project: str,
    backend_name: str,
    backend: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    cache_seconds: float = DEFAULT_AVAILABILITY_PROBE_CACHE_SECONDS,
    now: datetime | None = None,
    runner: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Return one bounded, serialized serving observation for a backend."""
    moment = _now(now)
    with _lane_probe_lock(project, backend_name):
        previous = _read_lane_probe_cache(project, backend_name)
        if previous and cache_seconds > 0:
            observed = _parse_stamp(previous.get("observed_at"))
            if observed is not None:
                age = max(0.0, (moment - observed).total_seconds())
                if age <= cache_seconds:
                    return {**dict(previous), "cached": True, "cache_age_seconds": age}
        result = (
            runner(backend_name, backend)
            if runner is not None
            else _request_lane_availability(backend_name, backend, root=root)
        )
        observation = _normalise_lane_probe(
            backend_name,
            result,
            observed_at=moment.isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        _write_lane_probe_cache(project, backend_name, observation)
        return observation


def recovery_log_path(project: str) -> Path:
    """Where one project's recovery record accumulates."""
    readable = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in str(project)
    ).strip("-")
    return crew_home() / "recovery" / f"{readable or 'project'}.jsonl"


def _refusal_observed_at(record: Mapping[str, Any]) -> datetime | None:
    """When the refusal was recorded, read from the stream that carries it.

    The budget block states what the account said, never when this side heard
    it, so the stream's own mtime is the observation time — the same reading
    the classifier already uses to age a quiet run.
    """
    log = Path(str(record.get("log_path") or ""))
    try:
        return datetime.fromtimestamp(log.stat().st_mtime, tz=UTC)
    except OSError:
        return _parse_stamp(
            record.get("attempt_started_at") or record.get("created_at")
        )


def hold_state(
    record: Mapping[str, Any],
    refusal: Mapping[str, Any],
    *,
    policy_block: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Whether this run's own hold is still in force, and what identifies it.

    A hold that names a reset is dated by it. One that names none is aged
    against the declared shelf life, from the moment the refusal was observed —
    the same bound the pre-flight applies, read from the same policy block, so
    a project that lengthens its shelf life lengthens both together.
    """
    moment = _now(now)
    resets_at = _parse_stamp(refusal.get("resets_at"))
    if resets_at is not None:
        return {
            "signature": f"resets:{resets_at.isoformat()}",
            "in_force": resets_at > moment,
            "detail": f"the refusal states a reset at {refusal['resets_at']}",
        }
    bound = float(
        policy_block.get(
            "evidence_shelf_life_minutes", budget_module.DEFAULT_SHELF_LIFE_MINUTES
        )
    )
    observed = _refusal_observed_at(record)
    if observed is None:
        return {
            "signature": "",
            "in_force": True,
            "detail": "the refusal states no reset and carries no readable observation time",
        }
    minutes = int((moment - observed).total_seconds() // 60)
    return {
        "signature": f"observed:{observed.isoformat()}",
        "in_force": bound > 0 and minutes <= bound,
        "detail": (
            f"the refusal states no reset and is {minutes} minutes old against "
            f"a {bound:g} minute shelf life"
        ),
    }


# The three places a run's session can be, in the order a resolver must read
# them: the cheap authoritative one, the one that answers when the cheap one has
# simply not been folded in yet, and the one that answers after promotion has
# taken the pointer away.
SESSION_SOURCES = ("pointer", "stream", "ledger")


def _pointer_session(record: Mapping[str, Any]) -> str:
    return str(record.get("session_id") or "").strip()


def _stream_session(record: Mapping[str, Any]) -> str:
    """The session id the run's own stream carries, or empty.

    This is the source the incident turned on. A pointer carrying no session id
    means only that nothing has folded the stream in yet — resume has re-read
    it for months — so reading the pointer alone reports an absence that is not
    one.
    """
    from reckon import _backends

    log = Path(str(record.get("log_path") or ""))
    if record.get("launch") != "cli" or not log.is_file():
        return ""
    try:
        backend = _backend_settings(record, None)
        observation = _backends.observe_log(
            backend_name=str(record.get("backend") or ""),
            backend=backend,
            log_path=log,
        )
    except (CrewError, OSError, ValueError):
        return ""
    return str(observation.session_id or "").strip()


def _ledger_session(run_id: str, *, project: str, root: Any) -> str:
    """The session the promoted row recorded, or empty.

    A promoted run has no pointer and no live stream to consult, and its
    session may still be resumable — so the committed row is the third source
    rather than the end of the search.
    """
    if not project:
        return ""
    try:
        data, _version = ledger.load(project, root=root)
    except Exception:  # noqa: BLE001 - an unreadable ledger answers nothing
        return ""
    for row in reversed(data.get("runs") or []):
        if str(row.get("run_id") or "") == run_id:
            return str(row.get("session_id") or "").strip()
    return ""


def resolve_session(
    run_id: str,
    *,
    record: Mapping[str, Any] | None = None,
    project: str = "",
    root: Any = None,
) -> dict[str, Any]:
    """Answer the session for one run, and say which source answered.

    Every surface that reports a session asks this, so two surfaces asked about
    the same run cannot disagree. The sources are consulted in order — live
    pointer, the run's own stream, the promoted ledger row — and the answer
    carries the one that supplied it.

    Where none has an id the answer is a stated absence naming all three as
    consulted, because *not yet observed* and *genuinely absent* are different
    facts and nothing expressed the difference. A coordinator read a bare null
    as a verdict on five runs and promoted them; the ids were in their streams.
    """
    pointer: Mapping[str, Any] = {}
    if record is not None:
        pointer = record
    else:
        try:
            pointer = read_pointer(run_id)
        except (CrewError, OSError):
            pointer = {}
    # A promoted run has no pointer to name its project, so a caller asking
    # about one states it; otherwise the pointer does.
    project = str(project or pointer.get("project") or "")
    answer = {
        "run_id": run_id,
        "session_id": None,
        "source": None,
        "resolved": False,
        "consulted": list(SESSION_SOURCES),
    }
    found = _pointer_session(pointer)
    if found:
        return {**answer, "session_id": found, "source": "pointer", "resolved": True}
    found = _stream_session(pointer)
    if found:
        return {**answer, "session_id": found, "source": "stream", "resolved": True}
    found = _ledger_session(
        run_id, project=project, root=root if root is not None else pointer.get("repo")
    )
    if found:
        return {**answer, "session_id": found, "source": "ledger", "resolved": True}
    return {
        **answer,
        "detail": (
            "no session id in the live pointer, the run's own stream or the "
            "promoted ledger row; all three were consulted, so this is an "
            "absence rather than a reading nobody has taken yet"
        ),
    }


def _claimed_write_paths(record: Mapping[str, Any]) -> list[str]:
    """Write paths this run declared that a different live run now claims.

    Only a run that is still working can hold a scope: a pointer sitting in its
    own terminal state is what the coordinator is about to reconcile, and
    treating it as an owner would make every stopped wave permanently
    unrecoverable.
    """
    node = record.get("node") or {}
    mine = {str(path) for path in (node.get("write_paths") or ())}
    if not mine:
        return []
    run_id = str(record.get("run_id") or "")
    repo = str(record.get("repo") or "")
    claimed: set[str] = set()
    for other in list_live():
        if str(other.get("run_id") or "") == run_id:
            continue
        if str(other.get("repo") or "") != repo:
            continue
        if process_alive(other.get("pid")) is not True:
            continue
        other_node = other.get("node") or {}
        claimed |= mine & {str(path) for path in (other_node.get("write_paths") or ())}
    return sorted(claimed)


def _resume(
    run_id: str,
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    launcher: Callable[..., int],
    advice: str = CONTINUE_ADVICE,
) -> dict[str, Any]:
    """Launch one resumption exactly the way a hand-typed resume does."""
    plan = resume_plan(run_id, advice, config=config)
    directory = run_dir(run_id)
    turn = len(list(directory.glob("resume-*.jsonl"))) + 1
    advice_path = directory / f"resume-{turn}-advice.txt"
    advice_path.parent.mkdir(parents=True, exist_ok=True)
    advice_path.write_text(advice + "\n", encoding="utf-8")
    log_path = directory / f"resume-{turn}.jsonl"
    stderr_path = directory / f"resume-{turn}.stderr.log"
    attempt_started_at = _utc_now()
    manifest_baseline_mtime_ns = _manifest_mtime_ns(record.get("manifest_path") or "")
    pid = launcher(
        plan, log_path=log_path, stderr_path=stderr_path, prompt_path=advice_path
    )
    record_resumption(
        run_id,
        pid=pid,
        turn=turn,
        log_path=log_path,
        stderr_path=stderr_path,
        attempt_started_at=attempt_started_at,
        manifest_baseline_mtime_ns=manifest_baseline_mtime_ns,
    )
    return {"pid": pid, "turn": turn, "log_path": str(log_path)}


def _run_condition_probe(
    record: Mapping[str, Any], wait: Mapping[str, Any]
) -> dict[str, Any]:
    """Run a waiting manifest's argument vector without invoking a shell."""
    worktree = Path(str(record.get("worktree") or "."))
    try:
        completed = subprocess.run(
            list(wait.get("probe") or ()),
            cwd=worktree if worktree.is_dir() else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "terminal": False,
            "observed": "unavailable",
            "detail": f"condition probe could not run: {exc}",
        }
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    observed = lines[-1] if lines else f"exit:{completed.returncode}"
    candidates = {observed.casefold()}
    candidates.update(
        line.split(maxsplit=1)[0].rstrip("+").casefold() for line in lines
    )
    terminal = {str(value).strip().casefold() for value in wait.get("terminal") or ()}
    return {
        "terminal": bool(candidates & terminal),
        "observed": observed,
        "detail": (
            f"condition probe reported {observed!r} with exit {completed.returncode}"
        ),
    }


def _condition_observation(value: Any) -> dict[str, Any]:
    """Normalise an injected or subprocess-backed condition observation."""
    if isinstance(value, bool):
        return {
            "terminal": value,
            "observed": "terminal" if value else "pending",
            "detail": "condition test returned a boolean verdict",
        }
    if not isinstance(value, Mapping):
        raise TypeError("condition test must return a mapping or boolean")
    terminal = value.get("terminal")
    if not isinstance(terminal, bool):
        raise TypeError("condition test result must carry a boolean terminal field")
    return {
        "terminal": terminal,
        "observed": str(value.get("observed") or "unknown"),
        "detail": str(value.get("detail") or "condition test returned a verdict"),
    }


def _stamp_resumption_trigger(run_id: str, signature: str) -> None:
    """Record which hold or condition caused a resumption, so it happens once."""
    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return
        current["auto_resume"] = {"trigger": signature, "at": _utc_now()}
        _write_json(pointer_path(run_id), current)


def sweep_status_path(project: str) -> Path:
    """Where one project's sweep status accumulates, one entry per writer."""
    return recovery_log_path(project).with_suffix(".status.json")


def _read_sweep_status(project: str) -> dict[str, Any]:
    """The recorded sweep status for a project, or an empty mapping."""
    try:
        value = json.loads(sweep_status_path(project).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalise_writer(writer: Any) -> tuple[str, str]:
    """Return (kind, key) identifying the writer of one sweep.

    A caller declares the kind it is — a follower on its cadence, a hand-run
    sweep from the command line, the MCP surface, or anything else — because a
    reader distinguishing follower health from their own manual pass cannot do
    it from a pid on a shared filesystem. The key is the caller's durable
    identity within its kind: the session id for a follower, empty for a kind
    with no stable identity, which gives every such caller one shared slot.
    """
    kind = "other"
    key = ""
    if isinstance(writer, Mapping):
        kind = str(writer.get("kind") or "other")
        key = str(writer.get("key") or writer.get("session") or writer.get("id") or "")
    elif isinstance(writer, str) and writer:
        kind, _, key = writer.partition(":")
    if kind not in SWEEP_WRITER_KINDS:
        kind = "other"
    return kind, key


def _writer_key(kind: str, key: str) -> str:
    """The stable map key a writer's entry lives under in the status record."""
    return f"follower:{key}" if kind == "follower" else kind


def _sweep_writer_entry(
    report: Mapping[str, Any], kind: str, key: str
) -> dict[str, Any]:
    """One writer's last sweep, carrying enough to answer without a process."""
    return {
        "kind": kind,
        "key": key,
        "swept_at": report.get("swept_at"),
        "dry_run": report.get("dry_run"),
        "checked": report.get("checked"),
        "resumed": len(report.get("resumed") or []),
        "skipped": len(report.get("skipped") or []),
        "swept_by_pid": os.getpid(),
    }


@contextmanager
def _sweep_status_lock(project: str):
    """Serialize the read-modify-write on the status record.

    Followers write on a cadence that keeps concurrent writes rare, but the
    record is a merge after this change rather than a blind overwrite, and two
    followers on a shared filesystem must not lose each other's entries.
    """
    path = sweep_status_path(project).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_sweep_status(
    project: str, report: Mapping[str, Any], writer: Any = None
) -> None:
    """Record that a sweep ran, what it found, which process ran it, and who.

    Written per writer rather than per project, because the question a reader
    actually asks is whether THEIR follower is sweeping — a question one global
    slot cannot answer when the file is written by every follower on the
    project, by hand-run sweeps, and from the MCP surface, and the stamp is
    only the last write by any writer. Each writer keeps its own last sweep; a
    kind with no durable identity shares a single slot recording its most
    recent pass. The project-level fields answer the question the file answered
    before — the latest sweep's pid and instant — while ``writers`` holds each
    writer's own, so existing readers keep their answer.

    The map is bounded so a finished session does not accumulate forever: when
    it passes MAX_SWEEP_WRITERS the least recently swept entries are dropped
    until it fits again, the writer that just wrote always kept. Hand-run, MCP
    and other callers already share one slot each, so the only growth is
    distinct follower sessions; the cap keeps the file bounded while retaining
    the session history a reader may still want.
    """
    path = sweep_status_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _sweep_status_lock(project):
            previous = _read_sweep_status(project)
            kind, key = _normalise_writer(writer)
            writers = dict(previous.get("writers") or {})
            writers[_writer_key(kind, key)] = _sweep_writer_entry(report, kind, key)
            if len(writers) > MAX_SWEEP_WRITERS:
                freshly_written = _writer_key(kind, key)
                # Deterministic: drop by sweep time, ties by key, never the
                # writer that just wrote.
                stale = sorted(
                    writers,
                    key=lambda name: (
                        str(writers[name].get("swept_at") or ""),
                        name,
                    ),
                )
                for name in stale:
                    if name == freshly_written:
                        continue
                    writers.pop(name)
                    if len(writers) <= MAX_SWEEP_WRITERS:
                        break
            path.write_text(
                json.dumps(
                    {
                        "project": project,
                        "swept_at": report.get("swept_at"),
                        "dry_run": report.get("dry_run"),
                        "checked": report.get("checked"),
                        "resumed": len(report.get("resumed") or []),
                        "skipped": len(report.get("skipped") or []),
                        "swept_by_pid": os.getpid(),
                        "writers": writers,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    except OSError:
        # A status that cannot be written must not stop the recovery it
        # describes; the next tick writes it.
        return


def read_sweep_status(project: str) -> dict[str, Any]:
    """The recorded project-level sweep view, or an empty mapping.

    The answer the file was built for — whether a sweep ran and found nothing,
    or never ran — is the project-level view: present with ``checked`` zero
    meaning it ran and found nothing, an empty mapping meaning no sweep has
    ever been recorded.
    """
    return _read_sweep_status(project)


def writer_sweep_status(project: str, writer: Any) -> dict[str, Any] | None:
    """The recorded last sweep by one named writer, or None when it never wrote.

    None means the writer is unknown to the record, which a reader must be able
    to tell apart from a writer that swept and found nothing — that is a
    mapping whose ``checked`` is zero. The answer comes from the record's own
    contents: no process is consulted, because a pid on a shared filesystem is
    meaningless on another host.
    """
    kind, key = _normalise_writer(writer)
    return read_sweep_status(project).get("writers", {}).get(_writer_key(kind, key))


def sweep(
    project: str,
    *,
    config: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    launcher: Callable[..., int] | None = None,
    now: datetime | None = None,
    condition_test: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
    writer: Any = None,
) -> dict[str, Any]:
    """Resume runs whose provider hold or declared external wait has ended.

    Idempotent by construction: a resumed run carries the trigger it was
    resumed for, so a second pass over the same fleet reports nothing to do.
    That is what makes it safe for something already running to call on a
    cadence.

    ``writer`` names who is sweeping — a follower on its cadence, a hand-run
    sweep from the command line or the MCP surface, or anything else — so the
    status record can tell a reader's own follower apart from every other
    writer of the same file without a process lookup. A caller that declares
    nothing is recorded as ``other``.
    """
    launch = _spawn if launcher is None else launcher
    test_condition = _run_condition_probe if condition_test is None else condition_test
    policy_block = budget_module.policy(config)
    resumed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    considered = 0

    for pointer in list_live(project=project):
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        moment = _now(now)
        wait = external_wait(pointer, now_seconds=moment.timestamp())
        refusal = _stream_refusal_block(pointer)
        if wait is None and refusal is None:
            continue
        considered += 1
        advice = CONTINUE_ADVICE
        observation: dict[str, Any] = {}
        if wait is not None:
            entry = {
                "run_id": run_id,
                "backend": pointer.get("backend"),
                "trigger": "external-condition",
                "condition": wait.get("condition"),
            }
            if not wait.get("valid"):
                skipped.append(
                    {
                        **entry,
                        "reason": "condition-declaration-invalid",
                        "detail": wait.get("error"),
                    }
                )
                continue
            try:
                observation = _condition_observation(test_condition(pointer, wait))
            except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
                skipped.append(
                    {
                        **entry,
                        "reason": "condition-check-failed",
                        "detail": str(exc),
                    }
                )
                continue
            if not observation["terminal"]:
                reason = (
                    "condition-overdue" if wait.get("overdue") else "condition-pending"
                )
                skipped.append(
                    {
                        **entry,
                        "reason": reason,
                        "observed": observation["observed"],
                        "detail": (
                            f"waiting {wait['age_seconds']}s; {observation['detail']}"
                        ),
                    }
                )
                continue
            signature = str(wait["signature"])
            advice = str(wait["resume_brief"])
            duplicate_reason = "already-resumed-for-this-condition"
            duplicate_detail = (
                "this run was already resumed after this external condition "
                "terminated; a rewritten waiting manifest creates a new condition"
            )
        else:
            if str(classify_pointer(pointer).get("classification") or "") != "blocked":
                continue
            entry = {"run_id": run_id, "backend": refusal.get("backend")}
            hold = hold_state(pointer, refusal, policy_block=policy_block, now=now)
            if hold["in_force"]:
                skipped.append(
                    {**entry, "reason": "hold-in-force", "detail": hold["detail"]}
                )
                continue
            signature = str(hold["signature"])
            duplicate_reason = "already-resumed-for-this-hold"
            duplicate_detail = (
                "this run was already resumed for the hold that lapsed; "
                "a further refusal writes its own hold to wait on"
            )
        previous = pointer.get("auto_resume")
        if (
            isinstance(previous, Mapping)
            and signature
            and str(previous.get("trigger") or previous.get("hold") or "") == signature
        ):
            skipped.append(
                {
                    **entry,
                    "reason": duplicate_reason,
                    "detail": duplicate_detail,
                }
            )
            continue
        session = resolve_session(run_id, record=pointer)
        if not session["resolved"]:
            skipped.append(
                {
                    **entry,
                    "reason": "no-recoverable-session",
                    "detail": session["detail"],
                }
            )
            continue
        worktree = Path(str(pointer.get("worktree") or ""))
        if not str(pointer.get("worktree") or "") or not worktree.is_dir():
            skipped.append(
                {
                    **entry,
                    "reason": "worktree-absent",
                    "detail": (
                        f"the run's worktree {str(worktree) or '(unset)'} is not on "
                        "disk, and a resume has no working directory to start in"
                    ),
                }
            )
            continue
        collisions = _claimed_write_paths(pointer)
        if collisions:
            skipped.append(
                {
                    **entry,
                    "reason": "scope-claimed-elsewhere",
                    "detail": (
                        "another live run now claims "
                        + ", ".join(collisions)
                        + ", so resuming would create the collision"
                    ),
                }
            )
            continue
        if dry_run:
            resumed.append(
                {
                    **entry,
                    "would_resume": True,
                    "session_id": session["session_id"],
                    "session_source": session["source"],
                    "hold": signature,
                    "advice": advice,
                    **({"observed": observation["observed"]} if observation else {}),
                }
            )
            continue
        try:
            launched = _resume(
                run_id,
                pointer,
                config=config,
                launcher=launch,
                advice=advice,
            )
        except BudgetHold as exc:
            skipped.append(
                {
                    **entry,
                    "reason": "hold-in-force",
                    "detail": (
                        "the lane's own budget verdict still holds it: "
                        + str(exc.verdict.get("reason") or exc)
                    ),
                }
            )
            continue
        except (CrewError, OSError) as exc:
            skipped.append({**entry, "reason": "resume-refused", "detail": str(exc)})
            continue
        _stamp_resumption_trigger(run_id, signature)
        resumed.append(
            {
                **entry,
                "session_id": session["session_id"],
                "session_source": session["source"],
                "hold": signature,
                "advice": advice,
                **({"observed": observation["observed"]} if observation else {}),
                **launched,
            }
        )

    report = {
        "project": project,
        "dry_run": bool(dry_run),
        "checked": considered,
        "resumed": resumed,
        "skipped": skipped,
        "swept_at": _utc_now(),
    }
    # Two records, because they answer different questions. The append-only log
    # holds what happened, and only sweeps that judged something write to it —
    # an operator reading it wants recoveries and the refusals to recover, not a
    # heartbeat from every tick of every follower.
    if considered:
        path = recovery_log_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, sort_keys=True) + "\n")
    # The status file holds THAT it happened, and is written every sweep. A
    # sweep that runs and finds nothing and a sweep that never ran are
    # indistinguishable otherwise, which is exactly how a fleet of followers
    # running pre-merge code looked healthy while none of them could recover
    # anything: the capability was absent and the pane said the same thing it
    # says when the fleet is simply quiet. Written per writer now, so a reader
    # can ask about one writer rather than only about the project.
    _write_sweep_status(project, report, writer)
    return report
