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
  stopped its follower. The exception is a producer reload in progress,
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
  state can trap a coordinator. The block fires at most once per list:
  ``stop_hook_active`` marks a turn that already continued on a blocking
  reason, and the hook then stays silent rather than looping. Stopping is read
  by the harness as a verdict on the turn, so this mode's verdict never consults
  the digest; it clears that record when the list is empty, because whichever
  event first sees the emptying is the last one that can notice it.

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
    try:
        return float(os.environ[STOP_DERIVATION_BUDGET_ENV])
    except (KeyError, ValueError):
        return STOP_DERIVATION_BUDGET_SECONDS


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
# is already owned by a reflex that retries, and an unreadable record is a
# reading of a file caught mid-write rather than a duty the coordinator can act
# on, so blocking would ask a coordinator to act on a wait it cannot shorten.
# Mirrors the derivation's own vocabulary, which cannot be imported here on the
# paths that read a fresh snapshot.
_NON_BLOCKING_KINDS = frozenset({"review-queued", "unreadable-review-record"})

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
    """
    module = snapshot_module()

    for row in module.read_followers(project):
        record = row.get("session")
        if not isinstance(record, str) or not record:
            continue
        if harness_session and record == harness_session:
            return record
        follower_record = row.get("follower") or {}
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


def digest_path(project: str, session: str) -> Path:
    """The file holding the duty set one session was last injected with."""
    lock = snapshot_module().follower_lock_path(project, session)
    return lock.with_suffix(_DIGEST_SUFFIX)


def duty_digest(items: Sequence[Mapping[str, Any]], *, reloading: bool = False) -> str:
    """A digest over the ``(kind, run_id)`` pairs, tagged with the reload state.

    The pairs alone would silence a reloading producer's list, because it is
    the same list the session was already shown. The tag makes entering a
    reload and coming back from one each read as the change they are, while
    two turns inside one reload, and two ordinary turns, stay equal.
    """
    pairs = sorted(
        (str(item.get("kind") or ""), str(item.get("run_id") or "")) for item in items
    )
    body = "\n".join(f"{kind}\t{run_id}" for kind, run_id in pairs)
    if reloading:
        body = f"reloading\n{body}"
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
_NEW_WORK_SUBCOMMANDS = frozenset({"dispatch", "review-plan", "shadow"})


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


def format_checklist(payload: dict[str, Any], *, note: str = "") -> str:
    """Render one obligations payload as the checklist the hook emits.

    ``note`` is the state the list is shown under, placed in the header before
    the counts: a reload shows the last snapshot's list with the words
    ``producer reloading`` and its age there, in place of any remedy.
    """
    items = payload.get("obligations") or ()
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
    lines = [header]
    lines.extend(_work_lines(items))
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
    try:
        value = float(os.environ[PRODUCER_LEASE_ENV])
    except (KeyError, ValueError):
        return PRODUCER_LEASE_SECONDS
    return value if value > 0 else PRODUCER_LEASE_SECONDS


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
    state: str, *, project: str, session: str, document: Mapping[str, Any] | None
) -> str:
    """One line naming why the session's snapshot is not fresh, and the remedy.

    Each of the three not-fresh states is said in its own words a coordinator
    reads at the open of a turn: no producer, a producer running older code
    than the checkout, or the age of a snapshot. A stale snapshot beside a live
    producer lease -- the seat is healthy and only this session's list is old,
    because a session's snapshot is refreshed only while that session has a
    live follower -- is answered with the follower arming, which is the command
    that can actually refresh it; the seat command only reports that the seat
    is held. Every other reading, including a stale snapshot whose lease has
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
    else:
        reason = "no producer"
    if state == module.STALE_SNAPSHOT and producer_lease_is_live(project):
        remedy = (
            "the producer is live, so run "
            f"`reckon crew follow --project {project} --session {session}` "
            "to arm this session's follower"
        )
    else:
        remedy = f"run `reckon crew watch --ensure --project {project}`"
    return (
        f"reckon obligations for session {session} (project {project}) are not "
        f"current: {reason}; {remedy}"
    )


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
    if not items and not reloading:
        if _read_digest(digest_file):
            try:
                from reckon._store import write_atomically

                digest_file.parent.mkdir(parents=True, exist_ok=True)
                write_atomically(
                    digest_file, lambda handle: handle.write("\n"), fsync=False
                )
            except OSError:
                pass
        return
    digest = duty_digest(items, reloading=reloading)
    if _read_digest(digest_file) == digest:
        return
    inject(payload, format_checklist(obligations, note=note))
    try:
        from reckon._store import write_atomically

        digest_file.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(
            digest_file, lambda handle: handle.write(digest + "\n"), fsync=False
        )
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
    document = module.read_snapshot(project, session)
    state = module.freshness(document)
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
    if document is not None and module.reload_in_progress(
        document,
        state=state,
        reload_started_at=module.watch_reload_started_at(project),
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
    inject(
        payload,
        not_fresh_line(state, project=project, session=session, document=document),
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
    """The payload minus worktree-held rows whose tree the fleet has released.

    A row whose run still holds its tree is left exactly as the snapshot
    carried it; only a row whose record shows the tree released, or whose
    recorded tree is no longer a directory, is dropped, and the summary is
    recomputed so the header counts the rows actually shown. A row whose run
    record cannot be read is kept: the snapshot's own derivation is the
    evidence it was raised on, and dropping a duty on an unreadable file would
    turn a damaged ledger into a silent omission.
    """
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

    Kinds a reflex already owns are listed like any other duty and never hold
    a turn open, because the coordinator could not shorten the wait by acting.
    """
    blocking: list[Any] = []
    for item in items:
        kind = str(item.get("kind") or "") if isinstance(item, Mapping) else ""
        if kind not in _NON_BLOCKING_KINDS:
            blocking.append(item)
    return blocking


def _stop(payload: dict[str, Any]) -> int:
    """Answer one stop turn from the session's snapshot, or a bounded derivation.

    The snapshot decides when it is fresh, exactly as it does for the prompt
    path, and the verdict is read from what it lists minus the kinds a reflex
    already owns and minus the deferrals in force, so an acknowledgement
    written after the snapshot was computed is honoured before a refusal. A
    snapshot that is not fresh -- for any of its three reasons
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
    document = module.read_snapshot(project, session)
    state = module.freshness(document)
    if state == module.FRESH:
        resolved = recheck_worktree_held_rows(
            module.live_payload(document), project=project
        )
    else:
        resolved = derive_within_budget(project, session, stop_derivation_budget())
        if resolved is None:
            if not payload.get("stop_hook_active"):
                line = not_fresh_line(
                    state, project=project, session=session, document=document
                )
                sys.stdout.write(json.dumps({"systemMessage": line}))
            return 0
    follow_each_local_lane(resolved, project=project)
    items = resolved.get("obligations") or ()
    digest_file = digest_path(
        str(resolved.get("project") or project),
        str(resolved.get("session") or session),
    )
    if not items:
        # An empty list is the one change the digest cannot record by
        # comparison: there is no checklist to inject and nothing to compare
        # it with, so the set that was last injected has to be cleared
        # instead. Left in place, it makes the same duties *returning*
        # read as a repeat of what the session was already shown, and the duty
        # that emptied and came back is never spoken again. Either mode clears
        # it, because whichever event first sees the list empty is the last one
        # that can notice it went away: a duty drained over a stop and returned
        # before the next prompt is exactly the case a prompt-only clear would
        # swallow.
        if _read_digest(digest_file):
            try:
                from reckon._store import write_atomically

                digest_file.parent.mkdir(parents=True, exist_ok=True)
                write_atomically(
                    digest_file, lambda handle: handle.write("\n"), fsync=False
                )
            except OSError:
                pass
        return 0
    if not _blocking_items(items):
        # Only duties some reflex already owns: they are listed, and they do
        # not hold the turn open. The recorded set is left as it stands,
        # because the prompt path injected this very list and clearing it
        # would make the same duties speak twice.
        return 0
    # The snapshot's list is what the sweep computed, not what the fleet owes
    # now. An acknowledgement written since it was published is honoured here
    # rather than at the next republish, so a deferral does not hold the turn
    # open for a duty the coordinator has already excused.
    resolved = reapply_acknowledgements(
        resolved, project=str(resolved.get("project") or project)
    )
    if not _blocking_items(resolved.get("obligations") or ()):
        return 0
    emit("stop", payload, resolved)
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
