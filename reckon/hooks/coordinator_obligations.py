#!/usr/bin/env python3
"""Put a coordinator's reckon obligations in front of it, every turn.

A coordinator's duties live in its own context, so a coordinator forgets: a
turn can end over an unpromoted run, an unanswered blocker, or a review nobody
dispatched. This hook binds the harness to the list the project's watch
producer derives and publishes for each session, injected as a checklist at
the open of every turn, and re-raised when the session tries to stop with
duties remaining.

Two modes, selected by ``--hook``:

- ``prompt`` — wired as SessionStart and UserPromptSubmit. Reads the session's
  published snapshot and prints its checklist as ``additionalContext`` so the
  duties open the turn and survive compaction. Nothing is derived here: the
  snapshot module is loaded by file path and the derivation modules are never
  imported, so a turn's opening costs one stat, one small JSON read and a
  formatting pass. A snapshot that is not fresh is answered by one line naming
  the reason and the remedy that matches the reading: the command that arms
  the seat when no producer lease is live, and this session's own follower
  when a live producer stands behind a snapshot gone stale because the session
  stopped its follower. When this session has a live session host, that host
  re-arms the follower on the next dispatch, so the line names the host instead
  of giving a manual follower command. The exception is a producer reload in progress,
  which shows the last snapshot's checklist headed by its age and the words
  ``producer reloading`` with no remedy, because the replacement is already on
  its way and cycling the seat would be the wrong thing to do. The hook never
  writes the snapshot itself. A worktree-held row is rechecked as it is read,
  against the run's committed record and the tree on disk, because a promotion
  can release that tree between the sweep and the read; a row whose tree the
  fleet has released is dropped, at a cost of one stat per row.
  It speaks when the duties *change* and stays quiet otherwise: a checklist
  repeated at the open of every turn is one a coordinator learns to skip. The
  session's last-injected set of ``(kind, run_id)`` pairs is kept beside that
  session's follower registration, and an injection happens only when a duty
  has appeared or gone since the last one, or when the reload state under the
  list has changed -- entering a reload and coming back from one are each
  said once, because the sentence over the duties changed even though the set
  did not. An age that moved without either moving is not a change worth
  saying again. A list that empties clears that record, because emptying is
  the one change a comparison cannot record: a duty that went away and
  returned would otherwise match the set left behind and never be spoken
  again.
- ``stop`` — wired as Stop. Prints ``{"decision": "block", "reason": ...}``
  while duties remain, so the turn cannot end into forgotten work. It reads the
  same snapshot as the prompt path and blocks on what that snapshot lists,
  except the duty kinds a reflex already owns and retries, which are listed but
  never hold a turn open, and except the duties an acknowledgement recorded
  after the snapshot was computed defers, which are re-read from the live
  pointers as the hook refuses. When the snapshot is not fresh for any reason it
  derives inline under a bounded budget instead, and if that budget runs out it
  allows the stop with one line naming the not-fresh reason, so no producer
  state can trap a coordinator. The block fires at most once per unchanged
  list: the mode records the duty set it last refused a turn over and stays
  silent when that set is read again, so a growing age or overdue figure — the
  only thing that changes about a list nobody has added to — does not re-block.
  A list that gains or loses a duty is a different set and blocks again, and an
  emptied list clears the record so a duty that drains and returns is refused
  once more. That record is the stop mode's own, not the prompt mode's: the
  prompt path injects the very list a stop that follows it reads, so comparing
  against the prompt record would let every stop after an injection through.
  ``stop_hook_active`` still marks a turn that already continued on a blocking
  reason, and both guards run, so neither a re-fired stop nor a repeated list
  can loop.

A command the hook prints is one a coordinator may type, so it edits the lane a
composed remedy names. A remedy that dispatches new work — a review, a new node
— names no lane, so the picker chooses one or holds, and a lane a caller names
stays the caller's to name. A remedy that continues an existing run keeps the
lane that run was carried on, because the run's own session and state live
there. Both modes print a checklist through the same formatting, so both carry
that edit.

Silence is a mode of operation here, not a failure. A working directory
outside the registered mounts, or a session that armed no follower, both mean
this session is not coordinating, and both are quiet. Every path exits 0: a
hook that breaks the session it guards is worse than a hook that says nothing.

Session resolution.  The harness identifies a session by its own session id;
reckon identifies fleet work by a crew session, which is the name that
session's follower registered under. The two are matched through the follower
registration itself: a follower records the process that armed it, so a
registration is this session's when that process descends from this session's
``claude`` process. A registration is also accepted when its session name is
the harness session id verbatim, which is how a session that named its crew
session after itself reads back.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import importlib.util
import json
import os
import re
import shlex
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

_bootstrap_path = Path(__file__).with_name("interpreter_bootstrap.py")
_bootstrap_spec = importlib.util.spec_from_file_location(
    "interpreter_bootstrap", _bootstrap_path
)
_bootstrap = importlib.util.module_from_spec(_bootstrap_spec)
_bootstrap_spec.loader.exec_module(_bootstrap)
_bootstrap_error = _bootstrap.ensure_interpreter(__file__)
if _bootstrap_error:
    raise SystemExit(_bootstrap_error)

UTC = _datetime.UTC
datetime = _datetime.datetime

# The checklist's framing is fixed by the hook's external contract.
# It is repeated verbatim in the tests, so a change here is a contract change.
AUTHORITY_LINE = "mirror these into your task list; reckon's list is the authority"

# The snapshot reader is loaded from its file rather than imported as
# ``reckon.crew.obligation_snapshot``: reaching it through the crew facade --
# ``reckon/crew.py`` -- would import every concern module, the plan, backend and
# ledger derivations included, which is the cost this hook exists to avoid. The
# module is stdlib-only, so loading it by path keeps the prompt path inside the
# standard library plus the formatting here.
_SNAPSHOT_MODULE_NAME = "reckon.crew.obligation_snapshot"
_SESSION_HOST_STATE_DIR_ENV = "RECKON_SESSION_HOST_STATE_DIR"


@cache
def snapshot_module() -> Any:
    """The snapshot reader, loaded by file path when nothing has loaded it.

    An instance ``sys.modules`` already holds is reused rather than replaced:
    the producer reaches this module through ``reckon.crew`` and keeps its
    per-project sweep memory (``_SWEEPS``) on the instance it reaches, so a
    second copy here would split a process into two readers that disagree
    about what a sweep has published. Reaching the module through the facade
    is still avoided -- that would import every concern module -- and the path
    load runs only when no instance is registered at all. Registration
    precedes ``exec_module`` so the module's own name resolves while its body
    runs.
    """
    loaded = sys.modules.get(_SNAPSHOT_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parents[1] / "crew" / "obligation_snapshot.py"
    specification = importlib.util.spec_from_file_location(_SNAPSHOT_MODULE_NAME, path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load the snapshot reader from {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[_SNAPSHOT_MODULE_NAME] = module
    specification.loader.exec_module(module)
    return module


# How far up a process tree the ownership walk climbs before giving up. The
# measured shape is arming shell -> claude process, so one hop covers it; the
# bound only keeps a pathological tree from spinning inside a hook.
_OWNERSHIP_DEPTH = 12

# The stop path derives inline when the snapshot is not fresh. A bounded wait
# is what keeps a stalled machine from trapping a coordinator behind a hook:
# the derivation is measured in seconds, and when the budget runs out the stop
# is allowed with one line naming the not-fresh reason. Tests override the
# budget through the environment to exercise that branch.
STOP_DERIVATION_BUDGET_SECONDS = 5.0
STOP_DERIVATION_BUDGET_ENV = "RECKON_STOP_DERIVATION_BUDGET_SECONDS"


class _DerivationBudgetError(Exception):
    """Raised inside the stop hook when the inline derivation runs out of time."""


def stop_derivation_budget() -> float:
    """The inline derivation's budget in seconds, honouring the test override."""
    from reckon.env_duration import read_duration_override

    return read_duration_override(
        STOP_DERIVATION_BUDGET_ENV, STOP_DERIVATION_BUDGET_SECONDS, 0, False
    )


def _obligations_module():
    """The obligations derivation module, imported where it is used, not before.

    Kept out of every other path on purpose: a prompt turn reaches none of the
    derivation modules, and only a stop that must derive, or must re-read the
    deferrals a snapshot may predate, pays for loading them.
    """
    from reckon.crew import obligations as module

    return module


def _obligations_view():
    """The obligations derivation itself."""
    return _obligations_module().obligations


def derive_within_budget(
    project: str, session: str, budget: float
) -> dict[str, Any] | None:
    """Derive one session's duties under a wall-clock budget, or None.

    The derivation is imported inside the timed region because the import is
    part of the cost the budget bounds. A budget of zero or less runs nothing,
    which is how the over-budget branch is exercised without depending on a
    race. The alarm needs the main thread, where a hook always runs; a caller
    on another thread cannot arm it and derives unbounded rather than going
    without an answer.
    """
    if budget <= 0:
        return None
    alarm = getattr(signal, "SIGALRM", None)
    if alarm is None:
        return _obligations_view()(project, session)

    def _expired(signum: int, frame: Any) -> None:
        raise _DerivationBudgetError

    try:
        previous = signal.signal(alarm, _expired)
    except ValueError:
        return _obligations_view()(project, session)
    try:
        signal.setitimer(signal.ITIMER_REAL, budget)
        return _obligations_view()(project, session)
    except _DerivationBudgetError:
        return None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(alarm, previous)


# The duty kinds listed but never holding a stop open. A queued review's work
# is already owned by a reflex that retries, a queued run waits on the lane's
# own admission pass, a stalled run is one that pass could launch and did not,
# an unreadable record is a reading of a file caught mid-write rather than a
# duty the coordinator can act on, and an index that stages a path HEAD still
# holds belongs to the project rather than to any run's work, so blocking would
# ask a coordinator to act on a condition no run of this session created.
# Mirrors the derivation's own vocabulary, which cannot be imported here on the
# paths that read a fresh snapshot.
_NON_BLOCKING_KINDS = frozenset(
    {
        "review-queued",
        "run-queued",
        "run-stalled",
        "unreadable-review-record",
        "index-lags-head",
    }
)

# A session worktree's directory component is the repository name plus a short
# hex digest, e.g. ``reckon-c8f839407e49``.
_WORKTREE_COMPONENT = ".reckon-worktrees"


def _config_home() -> Path:
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser().resolve()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _mounts() -> dict[str, Path]:
    """Read the mounts file as project name -> docs directory.

    A mounts file that cannot be read resolves to no projects, and a directory
    in no project is silent.
    """
    path = Path(os.environ.get("RECKON_MOUNTS_PATH") or _config_home() / "mounts.json")
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    mounts: dict[str, Path] = {}
    for project, value in raw.items():
        if not isinstance(project, str) or not isinstance(value, str):
            continue
        try:
            mounts[project] = Path(value).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
    return mounts


def _repository_named_by_worktree(cwd: Path) -> str | None:
    """The repository a session worktree path names, or None.

    Session worktrees sit beside the repository at
    ``<repo-parent>/.reckon-worktrees/<repo>-<digest>/<session>/...``, so the
    component under ``.reckon-worktrees`` names the repository the tree
    belongs to. Reading the name from the path keeps git off this path
    entirely, and a mount still has to match the name before anything is
    resolved.
    """
    parts = cwd.parts
    for index, part in enumerate(parts):
        if part != _WORKTREE_COMPONENT or index + 1 >= len(parts):
            continue
        component = parts[index + 1]
        name, _, digest = component.rpartition("-")
        shaped = len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)
        if name and shaped:
            return name
        return component
    return None


def resolve_project(directory: Path) -> str | None:
    """Resolve the mounted project a working directory belongs to.

    A directory inside a registered repository's tree maps to that project
    directly, and the longest matching root wins so a checkout nested inside
    another resolves to the narrower mount. A session worktree of a registered
    project resolves through its path name as well, because the sessions that
    most need the checklist work in one.
    """
    cwd = directory.expanduser().resolve()
    best: tuple[int, str] | None = None
    for project, docs in sorted(_mounts().items()):
        root = docs.parent
        if len(root.parts) > len(cwd.parts):
            continue
        if cwd.parts[: len(root.parts)] == root.parts:
            score = len(root.parts)
            if best is None or score > best[0]:
                best = (score, project)
    if best is not None:
        return best[1]

    repository = _repository_named_by_worktree(cwd)
    if repository is None:
        return None
    for project, docs in sorted(_mounts().items()):
        if docs.parent.name == repository:
            return project
    return None


def _parent_of(pid: int) -> int | None:
    """The parent pid of one process, or None when it is gone or unreadable."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    try:
        tail = text.rsplit(")", 1)[1].split()
        return int(tail[1])
    except (IndexError, ValueError):
        return None


def _command_line(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()


def _ancestor_pids(*, limit: int = _OWNERSHIP_DEPTH) -> list[int]:
    """This process's ancestors, nearest first, stopping before pid 1."""
    chain: list[int] = []
    pid = os.getppid()
    while pid > 1 and len(chain) < limit:
        chain.append(pid)
        parent = _parent_of(pid)
        if parent is None or parent == pid:
            break
        pid = parent
    return chain


def _claude_pid() -> int | None:
    """The harness process this hook runs under, or None.

    The harness exports its own pid into every child, hooks included. The
    fallback names the nearest ancestor whose executable is ``claude`` for the
    harness versions that do not, and the reading comes up empty rather than
    guessing when neither is available.
    """
    try:
        exported = int(os.environ.get("CLAUDE_PID") or "")
    except ValueError:
        exported = 0
    if exported > 1:
        return exported
    for pid in _ancestor_pids():
        command = _command_line(pid)
        if command and Path(command.split(" ", 1)[0]).name == "claude":
            return pid
    return None


def _live_session_host_pid() -> int | None:
    """The running host recorded for this hook's harness process, if any.

    The host's state-directory override takes precedence over the crew home.
    """
    owner_pid = _claude_pid()
    if owner_pid is None:
        return None
    module = snapshot_module()
    owner_start = module.process_start_time(owner_pid)
    if not owner_start:
        return None
    override = os.environ.get(_SESSION_HOST_STATE_DIR_ENV)
    directory = Path(override) if override else module.crew_home() / "session-hosts"
    path = directory / f"{owner_pid}-{owner_start}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("owner") != {
        "pid": owner_pid,
        "start_time": owner_start,
    }:
        return None
    host_pid = record.get("pid")
    if isinstance(host_pid, bool) or not isinstance(host_pid, int):
        return None
    return host_pid if module._process_is_running(host_pid) else None


def _descends_from(pid: int | None, ancestor: int | None) -> bool:
    """Whether one process's ancestry reaches another, itself included."""
    if pid is None or ancestor is None or ancestor <= 1:
        return False
    current = pid
    for _ in range(_OWNERSHIP_DEPTH):
        if current == ancestor:
            return True
        if current <= 1:
            return False
        parent = _parent_of(current)
        if parent is None or parent == current:
            return False
        current = parent
    return False


def _session_from_followers(
    project: str, *, harness_session: str, claude_pid: int | None
) -> str | None:
    """The crew session this harness session coordinates under, or None.

    A follower registration names the crew session; the registration is this
    session's when the harness session id is that name, or when the process
    that armed the follower descends from this harness's process. A released
    registration still counts as this session's: the name it registered under
    is the identity the session used, and whether delivery is live is the
    follower's own reported state, not this hook's question.

    The harness session's own registration wins over descent. A host can hold
    another session's follower, whose arming process still descends from this
    harness, so reading descent first would adopt that other session whenever
    its registration sorts earlier. Descent is the fallback for a harness
    session that names no registration of its own.
    """
    module = snapshot_module()

    registrations: list[tuple[str, Any]] = []
    for row in module.read_followers(project):
        record = row.get("session")
        if not isinstance(record, str) or not record:
            continue
        registrations.append((record, row.get("follower") or {}))

    if harness_session:
        for record, _ in registrations:
            if record == harness_session:
                return record

    for record, follower_record in registrations:
        if not isinstance(follower_record, dict):
            continue
        try:
            owner_pid = int(follower_record.get("parent_pid") or 0)
        except (TypeError, ValueError):
            continue
        if _descends_from(owner_pid, claude_pid):
            return record
    return None


def _format_age(seconds: int) -> str:
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes, secs = divmod(remaining, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


# What the prompt mode last injected, per session, is the set of duties — not
# their text. A line that changed only its age says the same thing as before,
# and a mode that repeats it every turn is one a reader skips, which is the
# failure the injection exists to prevent. The file sits beside the session's
# follower registration, so the record of what this coordinator was told lives
# with the record of the session itself.
_DIGEST_SUFFIX = ".obligations"
# The stop mode's own record of the duty set it last refused a turn over. The
# prompt record cannot serve it: the prompt path injects the very list a stop
# that follows it reads, so a stop compared against that record would be let
# through after every injection, and a stop allowed over outstanding work is
# the forgetting this hook exists to prevent.
_STOP_DIGEST_SUFFIX = ".obligations-stop"


def digest_path(project: str, session: str) -> Path:
    """The file holding the duty set one session was last injected with."""
    lock = snapshot_module().follower_lock_path(project, session)
    return lock.with_suffix(_DIGEST_SUFFIX)


def stop_digest_path(project: str, session: str) -> Path:
    """The file holding the duty set the stop mode last blocked a turn over."""
    lock = snapshot_module().follower_lock_path(project, session)
    return lock.with_suffix(_STOP_DIGEST_SUFFIX)


def duty_digest(
    items: Sequence[Mapping[str, Any]],
    *,
    reloading: bool = False,
    model_notices: int = 0,
    limit_resets: Sequence[str] = (),
    reading: str = "",
) -> str:
    """A digest over the ``(kind, run_id)`` pairs, tagged with what else spoke.

    The pairs alone would silence a reloading producer's list, because it is
    the same list the session was already shown. The tag makes entering a
    reload and coming back from one each read as the change they are, while
    two turns inside one reload, and two ordinary turns, stay equal. The
    unacknowledged-notice count is carried too, so a notice appearing or being
    acknowledged while the duty set stands still is still spoken once. The ids
    of the limit resets the session is told of are carried for the same reason:
    a reset arriving while the duty set stands still is news, and a reset that
    ages past the horizon is news because it stops being said.

    ``reading`` tags a prompt answered by something other than a duty list — a
    not-fresh reason — so a repeated reason stays quiet while a different one
    speaks, and a reset arriving underneath it still speaks.
    """
    pairs = sorted(
        (str(item.get("kind") or ""), str(item.get("run_id") or "")) for item in items
    )
    body = "\n".join(f"{kind}\t{run_id}" for kind, run_id in pairs)
    if reloading:
        body = f"reloading\n{body}"
    if model_notices:
        body = f"{body}\nmodels\t{model_notices}"
    if reading:
        body = f"{body}\nreading\t{reading}"
    if limit_resets:
        body = f"{body}\nresets\t" + ",".join(sorted(str(one) for one in limit_resets))
    return hashlib.sha256(body.encode()).hexdigest()


def _read_digest(path: Path) -> str:
    """The digest last injected, or empty when there is none to read."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def local_lane(project: str) -> str:
    """The backend this host's local lane resolves to, or empty when none is."""
    try:
        from reckon import flight

        resolved = flight.resolve(project=project)
    except Exception:  # noqa: BLE001 - an unreadable config leaves the command alone
        return ""
    return str((resolved.config or {}).get("local_backend") or "").strip()


# The ``crew`` subcommands that launch new work. A remedy naming one of these
# routes something new — a review, a node — and so leaves the lane to the
# picker; every other composed command continues the run it already names.
_NEW_WORK_SUBCOMMANDS = frozenset({"dispatch", "review", "shadow"})


def _dispatches_new_work(tokens: Sequence[str]) -> bool:
    """True when a composed command launches new work rather than continuing a run."""
    return any(token in _NEW_WORK_SUBCOMMANDS for token in tokens)


def _without_lane(tokens: Sequence[str]) -> list[str]:
    """The same command with every lane flag removed, so the picker routes it."""
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--local":
            index += 1
            continue
        if token == "--backend":
            index += 2
            continue
        if token.startswith("--backend="):
            index += 1
            continue
        kept.append(token)
        index += 1
    return kept


def follow_local_lane(command: str, *, project: str) -> str:
    """Leave a printed new-work remedy's lane to the picker; keep a run's own.

    A command the hook prints is one a coordinator may retype, so it edits the
    lane a composed remedy names. A remedy that dispatches new work — a review,
    a new node — names no lane, so the picker chooses one or holds, and a lane a
    caller names stays the caller's to name. A remedy that continues an existing
    run — a resume, a redispatch, a repair of that run — keeps the lane that run
    was carried on, because the run's own session and state live there and a
    continuation answered on another lane is a continuation the run never sees.
    """
    if not command:
        return command
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if not _dispatches_new_work(tokens):
        return command
    rewritten = _without_lane(tokens)
    if rewritten == tokens:
        return command
    return " ".join(shlex.quote(part) for part in rewritten)


# Only housekeeping is collapsed. A worktree-held item's remedy is a single
# repository-wide command that answers for every tree it reaches, so a fleet of
# them is one piece of work. Every actionable kind keeps a line per run: two
# completed review runs share the same promotion sentence, which names no run,
# and reading one run's review is not the same work as reading another's.
_COLLAPSIBLE_KINDS = frozenset({"worktree-held"})


def pending_model_notices() -> int:
    """How many model notices await acknowledgement, from the snapshot alone.

    The hook reads the catalogue's published snapshot and never fetches: it
    counts the notices the producer already recorded and left unacknowledged,
    through the snapshot reader's single definition of "unacknowledged". An
    absent or unreadable snapshot counts nothing.
    """
    module = snapshot_module()
    return len(
        module.unacknowledged_model_notices(module.read_model_catalogue_snapshot())
    )


def model_notice_line(count: int) -> str:
    """One line naming the unacknowledged notices and the view that lists them."""
    plural = "" if count == 1 else "s"
    return (
        f"- [model-notice] {count} unacknowledged model notice{plural}: "
        'run `crew(view="models")`'
    )


# How far back the prompt reads limit resets, in seconds. It matches the horizon
# a re-armed producer announces by, so a session without a pane learns of
# exactly the resets a pane would still draw. Exercised through an override by
# the gate's negative control, which drops the horizon: the older event is then
# named too, so the line is shown to be bounded by it rather than by the store's
# contents.
LIMIT_RESET_HORIZON_SECONDS = 3_600.0
LIMIT_RESET_HORIZON_ENV = "RECKON_LIMIT_RESET_HORIZON_SECONDS"

#: The closing clause a reset line carries. A reset is not work to clear: it is
#: permission to carry on, which is why the line is rendered beside the duty
#: list rather than as a duty item.
LIMIT_RESET_INSTRUCTION = "sessions stopped at the usage limit can continue"


def limit_reset_horizon_seconds() -> float:
    """The reset horizon in seconds, honouring the test override."""
    from reckon.env_duration import read_duration_override

    return read_duration_override(
        LIMIT_RESET_HORIZON_ENV, LIMIT_RESET_HORIZON_SECONDS, 0, False
    )


def budget_reset_store_path() -> Path:
    """The banked-reset store the announcement is written to."""
    return snapshot_module().crew_home() / "budget-resets.json"


def _instant(value: Any) -> datetime | None:
    """One stored UTC instant, or None when it cannot be read."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def limit_reset_events(
    session: str | None, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """The recent limit resets that apply to one session, newest first.

    The store is read here rather than through the module that owns it, for the
    same reason :func:`pending_model_notices` reads the catalogue's own
    snapshot: the line must show even when the obligations snapshot is absent,
    and reaching ``reckon.crew.budget_reset`` would import the crew facade and
    every derivation module with it, which is the cost this hook exists to keep
    off a turn's opening. The reading therefore mirrors the owner's
    :func:`reckon.crew.budget_reset.recent_resets` exactly — an event older than
    the horizon is not due, a session sees every global event and only the ones
    scoped to itself, and the newest comes first — and a test pins the two
    together so they cannot drift.

    ``None`` asks for every event, which a sweep wants; naming a session selects
    what applies to it.
    """
    moment = now or datetime.now(tz=UTC)
    floor = moment - _datetime.timedelta(seconds=limit_reset_horizon_seconds())
    try:
        document = json.loads(budget_reset_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    groups = document.get("groups") if isinstance(document, dict) else None
    if not isinstance(groups, dict):
        return []
    rows: list[dict[str, Any]] = []
    for group, found in groups.items():
        if not isinstance(found, Mapping):
            continue
        events = found.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            stamp = _instant(event.get("detected_at"))
            if stamp is None or stamp < floor:
                continue
            owner = event.get("session")
            if session is not None and owner and owner != session:
                continue
            row = {**event, "group": str(group)}
            if not row.get("id"):
                row["id"] = f"reset-{group}-{stamp.strftime('%Y%m%dT%H%M%SZ')}"
            rows.append(row)
    rows.sort(key=lambda row: (_instant(row["detected_at"]), str(row.get("id"))))
    rows.reverse()
    return rows


def limit_reset_line(event: Mapping[str, Any]) -> str:
    """One reset as the single line a prompt is told.

    It names the group and clock the reset belongs to, the instant it is for in
    UTC, and who announced it and why — or, for an event the window readings
    detected, that it was detected rather than announced. The closing clause is
    what a session stopped at the limit acts on.
    """
    group = str(event.get("group") or "")
    clock = str(event.get("clock") or "").strip() or "seven_day"
    where = f" ({group}, {clock})" if group else f" ({clock})"
    stamp = _instant(event.get("detected_at"))
    at = stamp.strftime("%H:%M") if stamp is not None else "an unreadable instant"
    if str(event.get("kind") or "") == "consumed":
        provenance = "detected from the window readings"
    else:
        provenance = (
            f"announced by {event.get('by') or 'unknown'}: "
            f"{event.get('reason') or 'marked used'}"
        )
    return (
        f"- [limit-reset] usage limit reset{where} at {at} UTC, "
        f"{provenance}; {LIMIT_RESET_INSTRUCTION}"
    )


def _row_is_actionable(item: Mapping[str, Any]) -> bool:
    """Whether the coordinator can do anything about this duty right now.

    The decision is read from the duty's own fields, never from its text: a
    kind no coordinator shortens is never actionable, and a review the reflex
    owns is actionable only once it has stood past the window that reflex
    promises, because until then acting on it duplicates a dispatch already
    under way. A reflex-owned row carrying no window says nothing about how
    long the wait lasts, so it is printed rather than hidden.
    """
    kind = str(item.get("kind") or "")
    if kind in _NON_BLOCKING_KINDS:
        return False
    if str(item.get("review_owner") or "") != "reflex":
        return True
    window = item.get("reflex_window_seconds")
    if not isinstance(window, (int, float)) or isinstance(window, bool):
        return True
    age = item.get("age_seconds")
    if not isinstance(age, (int, float)) or isinstance(age, bool):
        age = 0
    return age >= window


def _collapsed_line(items: Sequence[Mapping[str, Any]]) -> str:
    """One line summarising the duties no coordinator action can shorten.

    The rows are counted by kind with the oldest age in each, which is what
    tells a reader whether a wait is normal or has stopped moving, without
    reprinting commands that nobody at this session can run.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("kind") or "?"), []).append(item)
    parts = [
        f"{kind} {len(members)} "
        f"(oldest {_format_age(max(int(m.get('age_seconds') or 0) for m in members))})"
        for kind, members in sorted(groups.items())
    ]
    return f"- [{len(items)} waiting, no action shortens them] " + ", ".join(parts)


def _work_lines(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """One counted line per shared remedy, and one line per run for the rest.

    Items of a collapsible kind sharing a next command collapse into one line
    carrying the count, the oldest age and the command once; the collapse is
    what keeps a fleet's worth of identical housekeeping from pushing the
    actionable items off the checklist. Every other item keeps its own line
    naming its run and its own age, however its command happens to read.
    """
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in items:
        if str(item.get("kind") or "") not in _COLLAPSIBLE_KINDS:
            continue
        key = (str(item.get("kind") or ""), str(item.get("next_command") or ""))
        grouped.setdefault(key, []).append(item)
    collapsed = {key: members for key, members in grouped.items() if len(members) > 1}
    lines: list[str] = []
    emitted: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("kind") or ""), str(item.get("next_command") or ""))
        members = collapsed.get(key)
        if members is not None:
            if key in emitted:
                continue
            emitted.add(key)
            oldest = max(int(member.get("age_seconds") or 0) for member in members)
            lines.append(
                f"- [{key[0] or '?'}] {len(members)} items "
                f"(oldest {_format_age(oldest)} old): {key[1]}"
            )
            continue
        name = str(item.get("node") or item.get("run_id") or "?")
        lines.append(
            f"- [{item.get('kind') or '?'}] {item.get('run_id') or '?'} "
            f"({name}, {_format_age(int(item.get('age_seconds') or 0))} old): "
            f"{item.get('next_command') or ''}"
        )
    return lines


def format_checklist(
    payload: dict[str, Any],
    *,
    note: str = "",
    limit_resets: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Render one obligations payload as the checklist the hook emits.

    ``note`` is the state the list is shown under, placed in the header before
    the counts: a reload shows the last snapshot's list with the words
    ``producer reloading`` and its age there, in place of any remedy.

    The limit resets due to this session ride beside the list rather than in it,
    the way an unacknowledged model notice does, because a reset asks a session
    to continue rather than to clear anything. ``limit_resets`` is the caller's
    own read of them; a caller that passes none has the store read here.
    """
    items = list(payload.get("obligations") or ())
    summary = payload.get("summary") or {}
    project = str(payload.get("project") or "")
    session = str(payload.get("session") or "")
    count = summary.get("count", len(items))
    header = (
        f"reckon obligations for session {session} (project {project}): "
        f"{note + '; ' if note else ''}"
        f"{count} outstanding"
    )
    # An empty list has no oldest item, and the age such a payload carries is
    # the snapshot's own age rather than any duty's, so naming it would invent
    # a figure no duty supports.
    if items:
        header += f", oldest {_format_age(int(summary.get('oldest_age_seconds') or 0))}"
    actionable = [item for item in items if _row_is_actionable(item)]
    waiting = [item for item in items if not _row_is_actionable(item)]
    lines = [header]
    lines.extend(_work_lines(actionable))
    if waiting:
        # The waits a coordinator cannot shorten are the bulk of a fleet's
        # list and the same rows turn after turn, so they are counted rather
        # than reprinted: what a reader needs from them is the kind and how
        # long each has stood, not the command none of them can run.
        lines.append(_collapsed_line(waiting))
    pending = pending_model_notices()
    if pending:
        lines.append(model_notice_line(pending))
    resets = limit_reset_events(session) if limit_resets is None else list(limit_resets)
    lines.extend(limit_reset_line(event) for event in resets)
    unreconciled = f"unreconciled runs: {summary.get('unreconciled_runs', 0)}"
    lines.append(unreconciled + "; work the list to empty before ending the turn.")
    lines.append(AUTHORITY_LINE)
    return "\n".join(lines)


def locate_session(payload: dict[str, Any]) -> tuple[str, str] | None:
    """The project and crew session this hook answers for, or None.

    None means this session is not coordinating reckon work: its directory is
    in no mount, or no follower registered it. Both modes resolve through here
    so the stop path reads the same session's snapshot the prompt path wrote.
    """
    cwd = Path(
        str(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    )
    project = resolve_project(cwd)
    if project is None:
        return None
    session = _session_from_followers(
        project,
        harness_session=str(payload.get("session_id") or ""),
        claude_pid=_claude_pid(),
    )
    if session is None:
        return None
    return project, session


def follow_each_local_lane(
    obligations: dict[str, Any], *, project: str
) -> dict[str, Any]:
    """Leave each item's printed command the lane its own remedy calls for."""
    for item in obligations.get("obligations") or ():
        if isinstance(item, dict):
            item["next_command"] = follow_local_lane(
                str(item.get("next_command") or ""), project=project
            )
    return obligations


# A producer's lease is renewed by the seat when it claims and by every live
# follower on its wait pass, so a renewal instant inside one lease interval is
# the seat's liveness as a reader can see it without probing a process. Mirrors
# the watcher's own default and environment override -- its module is not
# imported on this path -- so a test asserts the two figures agree.
PRODUCER_LEASE_SECONDS = 600.0
PRODUCER_LEASE_ENV = "RECKON_PRODUCER_LEASE_SECONDS"


def producer_lease_seconds() -> float:
    """The producer's lease interval in seconds, honouring the override."""
    from reckon.env_duration import read_duration_override

    return read_duration_override(PRODUCER_LEASE_ENV, PRODUCER_LEASE_SECONDS, 0, False)


def producer_lease_path(project: str) -> Path:
    """The lease registration one project's producer renews.

    The name mirrors the seat lock's own derivation -- readable stem plus a
    digest of the name -- so this reader looks for exactly the file the
    producer writes beside the seat record the reload reading already consults.
    A test pins it against the writer's own path so the two cannot drift.
    """
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", project).strip("-") or "project"
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    return (
        snapshot_module().crew_home()
        / "watch"
        / f"{readable}-{digest}.lock.registration"
    )


def producer_lease_is_live(project: str, *, now: float | None = None) -> bool:
    """Whether the lease record says a producer still stands behind the seat.

    A renewal inside one lease interval means the seat is held: the record is
    rewritten as the seat is claimed and on every live follower's wait pass,
    and it is the same record the producer's identity is published from. A
    missing or unreadable record, or one whose renewal has fallen a full
    interval behind, answers no -- the reader then keeps the command that can
    (re)arm the seat rather than telling a coordinator to arm a follower
    against no producer.
    """
    try:
        record = json.loads(
            producer_lease_path(project).read_text(encoding="utf-8") or "{}"
        )
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    renewed = record.get("lease_renewed_at")
    if isinstance(renewed, bool) or not isinstance(renewed, (int, float)):
        return False
    moment = time.time() if now is None else now
    return (moment - float(renewed)) < producer_lease_seconds()


def not_fresh_line(
    state: str,
    *,
    project: str,
    session: str,
    document: Mapping[str, Any] | None,
    foreign_host: str | None = None,
) -> str:
    """One line naming why the session's snapshot is not fresh, and the remedy.

    Each of the five not-fresh states is said in its own words a coordinator
    reads at the open of a turn: no producer, no follower for a session beside a
    producer seated on another host, a producer running older code than the
    checkout, a stopped follower behind an aged snapshot, or the age of a
    snapshot. A no-follower reading -- this session has
    no snapshot at all while the project's seat names a live producer elsewhere,
    because a session's snapshot is refreshed only while that session has a live
    follower -- names the host the seat is on and is answered with the follower
    arming, which is the command that can register this session and start its
    lease; the seat command would change nothing on this host. A stale snapshot
    beside a live producer lease earns the same follower arming, for the same
    reason. When this session's host is running, the host instead re-arms the
    follower with the next dispatch; a manual arming would duplicate it.
    Every other reading, including a stale snapshot whose lease has
    lapsed, keeps the command that publishes a snapshot again. A caller that
    routes a live stale producer through the reload reading never reaches this
    line for it, and a direct caller gets the state it asked about rather than
    the no-producer sentence.
    """
    module = snapshot_module()
    if state == module.STALE_CODE:
        reason = "the producer is running older code than the checkout"
    elif state == module.STALE_SNAPSHOT:
        age = module.snapshot_age_seconds(document)
        reason = (
            "the snapshot is stamped with no readable age"
            if age is None
            else f"the snapshot is {age}s old"
        )
    elif state == module.FOLLOWER_RELEASED:
        age = module.snapshot_age_seconds(document)
        published = "with no readable age" if age is None else f"{age}s ago"
        reason = (
            "this session's follower has stopped, and its obligations were "
            f"last published {published}"
        )
    elif state == module.NO_FOLLOWER:
        host = (
            module.live_foreign_seat_host(project)
            if foreign_host is None
            else foreign_host
        )
        reason = (
            f"the seat is held on {host}, whose producer has published no "
            "snapshot for this session"
        )
    else:
        reason = "no producer"
    live_lease = state == module.STALE_SNAPSHOT and producer_lease_is_live(project)
    host_pid = (
        _live_session_host_pid() if live_lease or state == module.NO_FOLLOWER else None
    )
    if host_pid is not None:
        remedy = (
            f"the session host (pid {host_pid}) is live and re-arms this "
            "session's follower with the session's next dispatch"
        )
    elif live_lease:
        remedy = (
            "the producer is live, so run "
            f"`reckon crew follow --project {project} --session {session}` "
            "to arm this session's follower"
        )
    elif state in (module.NO_FOLLOWER, module.FOLLOWER_RELEASED):
        remedy = (
            "run "
            f"`reckon crew follow --project {project} --session {session}` "
            f"to arm this session's follower"
        )
    else:
        remedy = f"run `reckon crew watch --ensure --project {project}`"
    return (
        f"reckon obligations for session {session} (project {project}) are not "
        f"current: {reason}; {remedy}"
    )


def deferred_reload_line(
    *, project: str, session: str, deferral: Mapping[str, Any]
) -> str:
    """Name the producer's recorded reason and files while it waits to reload."""
    paths = deferral.get("paths")
    named = [str(path) for path in paths if path] if isinstance(paths, list) else []
    reason = str(deferral.get("reason") or "checkout changes")
    files = ", ".join(f"`{path}`" for path in named) or "the checkout"
    return (
        f"reckon obligations for session {session} (project {project}) are not "
        f"current: the producer deferred its reload ({reason}) and is waiting "
        f"for {files} to be committed"
    )


def reloading_line(*, project: str, session: str, document: Mapping[str, Any]) -> str:
    """Name the reload from the last snapshot without offering a seat command."""
    age = snapshot_module().snapshot_age_seconds(document) or 0
    return (
        f"reckon obligations for session {session} (project {project}) are not "
        f"current: producer reloading, last snapshot {_format_age(age)} old"
    )


def stale_station_line(*, project: str, session: str, answer: Mapping[str, Any]) -> str:
    """One line naming a stale station heartbeat and how old it is.

    A quiet fleet and a stopped station look identical from the stored rows, so
    the heartbeat's age is what separates them: past the stale window the answer
    is reported with that age rather than served as current.
    """
    age = answer.get("heartbeat_age_seconds")
    module = snapshot_module()
    if age is None:
        measured = "is stamped with no readable time"
    else:
        measured = f"is {_format_age(int(age))} old"
    where = (
        "the station's heartbeat"
        if answer.get("source") == "station"
        else "the exported station snapshot's heartbeat"
    )
    return (
        f"reckon obligations for session {session} (project {project}) are not "
        f"current: {where} {measured}, past the "
        f"{int(module.STATION_STALE_SECONDS)}s stale window; run "
        f"`reckon crew watch --ensure --project {project}` on this node"
    )


def slow_station_note(*, answer: Mapping[str, Any]) -> str:
    """The header note a live station's running tick puts on the checklist.

    A tick that outlives the stale window is not a station to restart: its beat
    is fresh, so the process is alive, and the stage it has entered is the work
    it is actually doing. Cycling it would cost the fleet the refresh in
    flight, so the note names the stage and the age and carries no remedy.
    """
    stage = str(answer.get("tick_stage") or "")
    age = answer.get("tick_age_seconds")
    measured = (
        f"its tick has run {_format_age(int(age))}"
        if age is not None
        else "its tick is running"
    )
    if stage:
        measured += f" in the {stage} stage"
    return f"the station is alive but slow: {measured}, so no restart is needed"


def inject(payload: dict[str, Any], text: str) -> None:
    """Write one prompt-mode additionalContext object, dismissing nothing."""
    event = str(payload.get("hook_event_name") or "UserPromptSubmit")
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": text,
                }
            }
        )
    )


def emit(mode: str, payload: dict[str, Any], obligations: dict[str, Any]) -> None:
    """Write the one JSON object the mode produces, if any."""
    checklist = format_checklist(obligations)
    if mode == "stop":
        if payload.get("stop_hook_active"):
            return
        sys.stdout.write(json.dumps({"decision": "block", "reason": checklist}))
        return
    inject(payload, checklist)


def _inject_list(
    payload: dict[str, Any],
    *,
    project: str,
    session: str,
    obligations: dict[str, Any],
    reloading: bool,
    note: str = "",
) -> None:
    """Inject a list once per change, staying silent when there is no news.

    The fresh reading and the reloading reading of the same snapshot go
    through here, so both carry the lane edit and the same change record;
    only the header's note and the tag on the recorded digest differ. An empty
    list on an ordinary turn has nothing to say -- but an empty list under a
    reload is the news that the producer is reloading, so it is spoken once
    like any other change. The fresh empty turn is the one change the digest
    cannot record by comparison: there is nothing to compare it with, so the
    set that was last injected has to be cleared instead. Left in place, it
    makes the same duties *returning* read as a repeat of what the session was
    already shown, and the duty that emptied and came back is never spoken
    again.
    """
    for item in obligations.get("obligations") or ():
        if isinstance(item, dict):
            item["next_command"] = follow_local_lane(
                str(item.get("next_command") or ""), project=project
            )
    items = obligations.get("obligations") or ()
    digest_file = digest_path(project, session)
    notices = pending_model_notices()
    resets = limit_reset_events(session)
    if not items and not reloading and not notices and not resets:
        _clear_digest(digest_file)
        return
    digest = duty_digest(
        items,
        reloading=reloading,
        model_notices=notices,
        limit_resets=[str(event.get("id") or "") for event in resets],
    )
    if _read_digest(digest_file) == digest:
        return
    inject(payload, format_checklist(obligations, note=note, limit_resets=resets))
    _write_digest(digest_file, digest)


def _inject_reading(
    payload: dict[str, Any],
    *,
    project: str,
    session: str,
    line: str,
    reading: str,
) -> None:
    """Speak a not-fresh reading once per change, carrying any limit reset.

    A prompt the snapshot cannot answer is still a turn a session may be stopped
    at a limit over, so the resets due to it are said here too, beside the
    reason. The change record is the one the checklist writes: the reading's own
    tag is compared alongside the reset ids, so the same reason repeated is
    quiet, a different reason speaks, and a reset arriving underneath a reason
    already given is still the news that matters. It is written as a digest like
    any other rather than injected every turn, because a sentence repeated at
    the open of every turn is one a coordinator learns to skip.
    """
    resets = limit_reset_events(session)
    digest_file = digest_path(project, session)
    digest = duty_digest(
        (),
        reading=reading,
        limit_resets=[str(event.get("id") or "") for event in resets],
    )
    if _read_digest(digest_file) == digest:
        return
    lines = [line, *(limit_reset_line(event) for event in resets)]
    inject(payload, "\n".join(lines))
    _write_digest(digest_file, digest)


def _clear_digest(path: Path) -> None:
    """Forget what was last injected, so whatever returns next is spoken."""
    if not _read_digest(path):
        return
    _write_digest(path, "")


def _write_digest(path: Path, digest: str) -> None:
    """Record the digest last injected, tolerating an unwritable home."""
    try:
        from reckon._store import write_atomically

        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(path, lambda handle: handle.write(digest + "\n"), fsync=False)
    except OSError:
        pass


def _prompt(payload: dict[str, Any]) -> int:
    """Answer one prompt turn from the session's published snapshot.

    The snapshot is read, never written: a fresh one is formatted and injected
    exactly as a derivation would have been; a not-fresh one whose producer is
    plausibly mid-reload is answered with the last snapshot's own list, headed
    by the snapshot's age and the words ``producer reloading`` and carrying no
    remedy, because the replacement is on its way and cycling the seat would
    be the wrong thing to do; and any other not-fresh one is answered by one
    line saying why and how to publish a snapshot again. Everything this
    function imports is loaded before the read -- the snapshot module by file
    path -- so no derivation module reaches a turn's opening.
    """
    located = locate_session(payload)
    if located is None:
        return 0
    project, session = located
    module = snapshot_module()
    station = module.read_station_answer(project, session)
    if station is not None:
        if station.get("stale"):
            _inject_reading(
                payload,
                project=project,
                session=session,
                line=stale_station_line(
                    project=project, session=session, answer=station
                ),
                reading="station-stale",
            )
            return 0
        if station.get("state") == module.STATION_SLOW:
            # The station is alive and working through a long tick: the rows it
            # serves are its own, so the list is shown as it is, headed by what
            # it is doing. Only a stale beat is a station to restart.
            _inject_list(
                payload,
                project=project,
                session=session,
                obligations=recheck_worktree_held_rows(
                    station["payload"], project=project
                ),
                reloading=False,
                note=slow_station_note(answer=station),
            )
            return 0
        _inject_list(
            payload,
            project=project,
            session=session,
            obligations=recheck_worktree_held_rows(station["payload"], project=project),
            reloading=False,
        )
        return 0
    document = module.read_snapshot(project, session)
    state, foreign_host = module.freshness_reading(
        document, project=project, session=session
    )
    if state == module.FRESH:
        _inject_list(
            payload,
            project=project,
            session=session,
            obligations=recheck_worktree_held_rows(
                module.live_payload(document), project=project
            ),
            reloading=False,
        )
        return 0
    reload_started_at, deferral = module.watch_reload_state(project)
    if deferral is not None:
        _inject_reading(
            payload,
            project=project,
            session=session,
            line=deferred_reload_line(
                project=project, session=session, deferral=deferral
            ),
            reading="deferred-reload",
        )
        return 0
    if document is not None and module.reload_in_progress(
        document,
        state=state,
        reload_started_at=reload_started_at,
    ):
        age = module.snapshot_age_seconds(document) or 0
        _inject_list(
            payload,
            project=project,
            session=session,
            obligations=recheck_worktree_held_rows(
                module.live_payload(document), project=project
            ),
            reloading=True,
            note=f"producer reloading, last snapshot {_format_age(age)} old",
        )
        return 0
    _inject_reading(
        payload,
        project=project,
        session=session,
        line=not_fresh_line(
            state,
            project=project,
            session=session,
            document=document,
            foreign_host=foreign_host,
        ),
        reading=f"not-fresh:{state}",
    )
    return 0


# A snapshot is a reading of the fleet at one instant, and the fleet can move
# between that instant and the read: a promotion releases its run's worktree
# after a sweep has already published the row, so the checklist would offer
# housekeeping whose own remedy is refused for a run with no tree. A
# worktree-held row is therefore rechecked where it is offered, against the
# run's committed ledger record and the tree on disk, and dropped when the
# record shows the tree released or the tree is no longer a directory. The
# record is the per-run file a project's ledger writes under its own state
# directory, never the aggregate beside it, which carries every run a project
# has promoted and would cost a hook's whole budget to parse; the tree check
# costs one stat per row.
_WORKTREE_HELD_KIND = "worktree-held"

# A run id is a path component when the record is resolved, so only the shape
# the ledger itself accepts is resolved at all.
_RUN_ID = re.compile(r"[A-Za-z0-9._-]+")


def _read_record(path: Path) -> Mapping[str, Any] | None:
    """One JSON object from disk, or None when it cannot stand for a record."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, Mapping) else None


def _run_ledger_record(
    project: str, run_id: str, *, docs_dir: Path | None
) -> Mapping[str, Any] | None:
    """The committed record for one run, from the project's own state.

    Only the per-run file is read. The aggregate beside it gathers every run a
    project has ever promoted and is orders of magnitude larger, so reading it
    on this path would spend a hook's whole budget on one row; a run with no
    file of its own therefore answers None, which the caller reads as no
    evidence about the tree rather than as the tree being gone.
    """
    if docs_dir is None or not _RUN_ID.fullmatch(run_id):
        return None
    return _read_record(docs_dir / "state" / project / "runs" / f"{run_id}.json")


def _recorded_tree(record: Mapping[str, Any]) -> str:
    """The worktree path a run's record names, or empty when it names none.

    The release audit's reading is the most recent path a promotion wrote, a
    retained tree is named by its retention block, and the record's own
    worktree field is the dispatcher's. The audit is read first because a
    released run's own worktree field is not carried on the committed row.
    """
    release = record.get("release")
    if isinstance(release, Mapping):
        audit = release.get("worktree_audit")
        if isinstance(audit, Mapping):
            trees = audit.get("worktrees")
            if isinstance(trees, list):
                for entry in trees:
                    if isinstance(entry, Mapping):
                        path = str(entry.get("path") or "").strip()
                        if path:
                            return path
    retention = record.get("worktree_retention")
    if isinstance(retention, Mapping):
        path = str(retention.get("worktree") or "").strip()
        if path:
            return path
    return str(record.get("worktree") or "").strip()


def _worktree_row_is_stale(
    item: Mapping[str, Any], *, project: str, docs_dir: Path | None
) -> bool:
    """Whether one worktree-held row's run no longer holds a tree."""
    run_id = str(item.get("run_id") or "").strip()
    if not run_id or docs_dir is None:
        return False
    record = _run_ledger_record(project, run_id, docs_dir=docs_dir)
    if record is None:
        return False
    release = record.get("release")
    if isinstance(release, Mapping) and release.get("worktree_released") is True:
        return True
    tree = _recorded_tree(record)
    if not tree:
        return False
    return not Path(tree).expanduser().is_dir()


def recheck_worktree_held_rows(
    payload: dict[str, Any], *, project: str
) -> dict[str, Any]:
    """The payload minus the rows a held answer can no longer stand behind.

    Every reading of a held answer passes through here, so this is where it is
    settled against the fleet as it reads now. Two rows are dropped:

    * a duty whose run holds no live pointer. The pointer was there when the
      answer was computed; its removal is the run landing or ending, which no
      held answer can see until its producer ticks again (``snapshot_module``
      owns that rule, and the crew view reads it the same way).
    * a worktree-held row whose tree the fleet has released.

    A row whose run still holds its tree is left exactly as the snapshot
    carried it; only a row whose record shows the tree released, or whose
    recorded tree is no longer a directory, is dropped, and the summary is
    recomputed so the header counts the rows actually shown. A row whose run
    record cannot be read is kept: the snapshot's own derivation is the
    evidence it was raised on, and dropping a duty on an unreadable file would
    turn a damaged ledger into a silent omission.
    """
    payload = snapshot_module().without_departed_run_duties(payload)
    items = payload.get("obligations") or ()
    if not any(
        isinstance(item, Mapping) and str(item.get("kind") or "") == _WORKTREE_HELD_KIND
        for item in items
    ):
        return payload
    docs_dir = _mounts().get(project)
    kept: list[Any] = []
    dropped = False
    for item in items:
        if (
            isinstance(item, Mapping)
            and str(item.get("kind") or "") == _WORKTREE_HELD_KIND
            and _worktree_row_is_stale(item, project=project, docs_dir=docs_dir)
        ):
            dropped = True
            continue
        kept.append(item)
    if not dropped:
        return payload
    summary = payload.get("summary")
    summary = dict(summary) if isinstance(summary, Mapping) else {}
    summary["count"] = len(kept)
    summary["oldest_age_seconds"] = max(
        (
            int(item.get("age_seconds") or 0)
            for item in kept
            if isinstance(item, Mapping)
        ),
        default=0,
    )
    return {**payload, "obligations": kept, "summary": summary}


def reapply_acknowledgements(
    payload: dict[str, Any], *, project: str
) -> dict[str, Any]:
    """The payload's duties minus every deferral in force at read time.

    A snapshot is computed at one instant and can be read many seconds later,
    and ``crew ack`` records its deferral on the run's live pointer without
    republishing the session's snapshot, so the list a fresh snapshot carries
    may still name a duty the coordinator has already excused. The deferrals
    are therefore re-applied here, where a stop would refuse, against the
    pointers as they read now. The acknowledgement's own deadline bounds the
    effect: a deferral that has expired is not in force, so the duty returns to
    the list and the stop blocks again.
    """
    module = _obligations_module()
    items = list(payload.get("obligations") or ())
    in_force = module._acknowledgements_in_force(project, now=datetime.now(tz=UTC))
    owed, _deferred = module._partition_acknowledged(items, in_force)
    summary = payload.get("summary")
    summary = dict(summary) if isinstance(summary, Mapping) else {}
    summary["count"] = len(owed)
    summary["oldest_age_seconds"] = max(
        (int(item.get("age_seconds") or 0) for item in owed), default=0
    )
    return {**payload, "obligations": owed, "summary": summary}


def _blocking_items(items: Sequence[Any]) -> list[Any]:
    """The duties a stop is held open for, in list order.

    Only the rows a coordinator can act on hold the turn open, which is the
    same predicate that decides what the checklist prints in full: a queue, a
    stalled request and a review the reflex still owes inside its window are
    all waits no action at this session shortens, so a stop that refused on
    them would ask for work the coordinator cannot do.
    """
    return [
        item
        for item in items
        if not isinstance(item, Mapping) or _row_is_actionable(item)
    ]


def _stop(payload: dict[str, Any]) -> int:
    """Answer one stop turn from the session's snapshot, or a bounded derivation.

    The snapshot decides when it is fresh, exactly as it does for the prompt
    path, and the verdict is read from what it lists minus the kinds a reflex
    already owns and minus the deferrals in force, so an acknowledgement
    written after the snapshot was computed is honoured before a refusal. A
    snapshot that is not fresh -- for any of its four reasons
    -- sends the hook to the derivation, imported lazily and run under the
    bounded budget, because a stop is a verdict on the turn and a list no
    producer stands behind must not decide it. If the budget runs out, the stop
    is allowed with one line naming the not-fresh reason: no producer state may
    trap a coordinator.
    """
    located = locate_session(payload)
    if located is None:
        return 0
    project, session = located
    module = snapshot_module()
    station = module.read_station_answer(project, session)
    if station is not None and not station.get("stale"):
        resolved = recheck_worktree_held_rows(station["payload"], project=project)
    else:
        document = module.read_snapshot(project, session)
        state, foreign_host = module.freshness_reading(
            document, project=project, session=session
        )
        if state == module.FRESH:
            resolved = recheck_worktree_held_rows(
                module.live_payload(document), project=project
            )
        else:
            resolved = derive_within_budget(project, session, stop_derivation_budget())
        if resolved is None:
            if not payload.get("stop_hook_active"):
                reload_started_at, deferral = module.watch_reload_state(project)
                if deferral is not None:
                    line = deferred_reload_line(
                        project=project, session=session, deferral=deferral
                    )
                elif document is not None and module.reload_in_progress(
                    document, state=state, reload_started_at=reload_started_at
                ):
                    line = reloading_line(
                        project=project, session=session, document=document
                    )
                else:
                    line = not_fresh_line(
                        state,
                        project=project,
                        session=session,
                        document=document,
                        foreign_host=foreign_host,
                    )
                sys.stdout.write(json.dumps({"systemMessage": line}))
            return 0
    follow_each_local_lane(resolved, project=project)
    resolved_project = str(resolved.get("project") or project)
    resolved_session = str(resolved.get("session") or session)
    items = resolved.get("obligations") or ()
    digest_file = digest_path(resolved_project, resolved_session)
    stop_digest_file = stop_digest_path(resolved_project, resolved_session)
    if not items:
        # An empty list is the one change the digest cannot record by
        # comparison: there is no checklist to inject and nothing to compare
        # it with, so the set that was last injected has to be cleared
        # instead. Left in place, it makes the same duties *returning*
        # read as a repeat of what the session was already shown, and the duty
        # that emptied and came back is never spoken again. Either mode clears
        # it, because whichever event first sees the list empty is the last one
        # that can notice it went away: a duty drained over a list and returned
        # before the next prompt is exactly the case a prompt-only clear would
        # swallow. The stop's own refused-set record clears with it, so the
        # duty that returns is refused once more rather than read as the list
        # this mode already blocked over.
        _clear_digest(digest_file)
        _clear_digest(stop_digest_file)
        return 0
    if not _blocking_items(items):
        # Only duties some reflex already owns: they are listed, and they do
        # not hold the turn open.
        return 0
    # The snapshot's list is what the sweep computed, not what the fleet owes
    # now. An acknowledgement written since it was published is honoured here
    # rather than at the next republish, so a deferral does not hold the turn
    # open for a duty the coordinator has already excused.
    resolved = reapply_acknowledgements(resolved, project=resolved_project)
    blocking = _blocking_items(resolved.get("obligations") or ())
    if not blocking:
        # Nothing is held open: either the snapshot listed no actionable duty
        # or an acknowledgement in force defers the ones it named. The record
        # of what this mode last refused is cleared with the emptiness, because
        # an acknowledgement bounds only itself. A deferral that lapses puts
        # the same duty identity back in the list, and a record left standing
        # across the deferral would read it as the set already refused and
        # never block for it again -- one acknowledgement silencing a duty for
        # good.
        _clear_digest(stop_digest_file)
        return 0
    # The block fires at most once per unchanged list. A list whose duties are
    # the same ones this mode last refused a turn over has nothing new to say:
    # only its ages and overdue figures moved, and repeating the refusal for
    # those is what made a coordinator's hook block four times in a row on one
    # duty. The digest is over identities, so an added or removed duty is a
    # different set and blocks again. ``stop_hook_active`` still guards a turn
    # that already continued, so the two together cannot loop.
    blocking_digest = duty_digest(blocking)
    if _read_digest(stop_digest_file) == blocking_digest:
        return 0
    emit("stop", payload, resolved)
    _write_digest(stop_digest_file, blocking_digest)
    return 0


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode = ""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--hook" and index + 1 < len(arguments):
            mode = arguments[index + 1]
            index += 2
            continue
        if argument.startswith("--hook="):
            mode = argument.split("=", 1)[1]
        index += 1

    payload = _read_payload()
    if mode not in {"prompt", "stop"}:
        return 0
    try:
        if mode == "prompt":
            return _prompt(payload)
        return _stop(payload)
    except Exception as exc:  # noqa: BLE001 - a hook never kills the session it guards
        sys.stderr.write(f"coordinator_obligations: {exc}\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
