#!/usr/bin/env python3
"""Put a coordinator's reckon obligations in front of it, every turn.

A coordinator's duties live in its own context, so a coordinator forgets: a
turn can end over an unpromoted run, an unanswered blocker, or a review nobody
dispatched. This hook binds the harness to the derived list instead. The
obligations are recomputed from state at the moment the hook runs, injected as
a checklist at the open of every turn, and re-raised when the session tries to
stop with duties remaining.

Two modes, selected by ``--hook``:

- ``prompt`` — wired as SessionStart and UserPromptSubmit. Prints the checklist
  as ``additionalContext`` so the duties open the turn and survive compaction.
  It speaks when the duties *change* and stays quiet otherwise: a checklist
  repeated at the open of every turn is one a coordinator learns to skip. The
  session's last-injected set of ``(kind, run_id)`` pairs is kept beside that
  session's follower registration, and an injection happens only when a duty
  has appeared or gone since the last one. An age that moved without the set
  moving is not a change worth saying again.
- ``stop`` — wired as Stop. Prints ``{"decision": "block", "reason": ...}``
  while duties remain, so the turn cannot end into forgotten work. The block
  fires at most once per list: ``stop_hook_active`` marks a turn that already
  continued on a blocking reason, and the hook then stays silent rather than
  looping. Stopping is read by the harness as a verdict on the turn, so this
  mode is unconditional on the digest and says nothing about it.

A command the hook prints is one a coordinator may type, so it follows the
configured local lane rather than whichever backend the run was carried on: the
lane a run arrived on is the right lane to *read* about and the wrong one to
route new work to silently.

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

import hashlib
import json
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# The checklist's framing, wording fixed by the plan section this hook serves.
# It is repeated verbatim in the tests, so a change here is a contract change.
AUTHORITY_LINE = "mirror these into your task list; reckon's list is the authority"

# How far up a process tree the ownership walk climbs before giving up. The
# measured shape is arming shell -> claude process, so one hop covers it; the
# bound only keeps a pathological tree from spinning inside a hook.
_OWNERSHIP_DEPTH = 12

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
    from reckon.crew import runs

    for row in runs.list_followers(project):
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
    from reckon.crew import runs

    return runs.follower_lock_path(project, session).with_suffix(_DIGEST_SUFFIX)


def duty_digest(items: Sequence[Mapping[str, Any]]) -> str:
    """A digest over the ``(kind, run_id)`` pairs and nothing else."""
    pairs = sorted(
        (str(item.get("kind") or ""), str(item.get("run_id") or "")) for item in items
    )
    return hashlib.sha256(
        "\n".join(f"{kind}\t{run_id}" for kind, run_id in pairs).encode()
    ).hexdigest()


def _read_digest(path: Path) -> str:
    """The digest last injected, or empty when there is none to read."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _store_digest(path: Path, digest: str) -> None:
    """Record one digest atomically, never raising into the session.

    A config home this session cannot write to leaves the hook speaking every
    turn, which is the harmless direction: the digest suppresses a repeat, it
    must never suppress the first telling.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(digest + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        return


def local_lane(project: str) -> str:
    """The backend this host's local lane resolves to, or empty when none is."""
    try:
        from reckon import flight

        resolved = flight.resolve(project=project)
    except Exception:  # noqa: BLE001 - an unreadable config leaves the command alone
        return ""
    return str((resolved.config or {}).get("local_backend") or "").strip()


def follow_local_lane(command: str, *, project: str) -> str:
    """Point a printed command's lane at the configured local default.

    A composed review dispatch names the lane its run was carried on, which is
    the lane that run's coordinator chose and so the right lane to *run* — and
    the wrong one to hand a coordinator as the next command to type, because a
    backend named there routes the next dispatch to a metered lane without
    anyone deciding to. The local lane's own spelling is ``--local``, which
    resolves through the same configuration, so the printed command follows
    ``local_backend`` instead of naming the run's backend. A host that declares
    no local lane has no default to follow and the command is left as composed.
    """
    if not command or "--backend" not in command:
        return command
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if not any(part == "--backend" or part.startswith("--backend=") for part in tokens):
        return command
    if not local_lane(project):
        return command
    rewritten: list[str] = []
    index = 0
    while index < len(tokens):
        part = tokens[index]
        if part == "--backend" and index + 1 < len(tokens):
            rewritten.append("--local")
            index += 2
            continue
        if part.startswith("--backend="):
            rewritten.append("--local")
            index += 1
            continue
        rewritten.append(part)
        index += 1
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


def format_checklist(payload: dict[str, Any]) -> str:
    """Render one obligations payload as the checklist the hook emits."""
    items = payload.get("obligations") or ()
    summary = payload.get("summary") or {}
    project = str(payload.get("project") or "")
    session = str(payload.get("session") or "")
    header = (
        f"reckon obligations for session {session} (project {project}): "
        f"{summary.get('count', len(items))} outstanding, "
        f"oldest {_format_age(int(summary.get('oldest_age_seconds') or 0))}"
    )
    lines = [header]
    lines.extend(_work_lines(items))
    unreconciled = f"unreconciled runs: {summary.get('unreconciled_runs', 0)}"
    lines.append(unreconciled + "; work the list to empty before ending the turn.")
    lines.append(AUTHORITY_LINE)
    return "\n".join(lines)


def resolve(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The obligations payload this hook should act on, or None."""
    from reckon.crew.obligations import obligations as obligations_view

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
    obligations = obligations_view(project, session)
    items = obligations.get("obligations") or ()
    if not items:
        return None
    for item in items:
        if isinstance(item, dict):
            item["next_command"] = follow_local_lane(
                str(item.get("next_command") or ""), project=project
            )
    return obligations


def emit(mode: str, payload: dict[str, Any], obligations: dict[str, Any]) -> None:
    """Write the one JSON object the mode produces, if any."""
    checklist = format_checklist(obligations)
    if mode == "stop":
        if payload.get("stop_hook_active"):
            return
        sys.stdout.write(json.dumps({"decision": "block", "reason": checklist}))
        return
    event = str(payload.get("hook_event_name") or "UserPromptSubmit")
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": checklist,
                }
            }
        )
    )


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
        resolved = resolve(payload)
    except Exception as exc:  # noqa: BLE001 - a hook never kills the session it guards
        sys.stderr.write(f"coordinator_obligations: {exc}\n")
        return 0
    if resolved is None:
        return 0
    digest = ""
    digest_file: Path | None = None
    if mode == "prompt":
        digest = duty_digest(resolved.get("obligations") or ())
        digest_file = digest_path(
            str(resolved.get("project") or ""), str(resolved.get("session") or "")
        )
        if _read_digest(digest_file) == digest:
            return 0
    try:
        emit(mode, payload, resolved)
    except Exception as exc:  # noqa: BLE001 - emission failure is silence, not a session fault
        sys.stderr.write(f"coordinator_obligations: {exc}\n")
        return 0
    if digest_file is not None:
        _store_digest(digest_file, digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
