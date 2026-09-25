#!/usr/bin/env python3
"""Pre-tool-use hook: refuse a mutating git verb a crew worker aims at a
repository other than its own worktree.

Self-contained and stdlib-only on purpose: this single file is distributed
into every crew-managed repository's harness settings, and most of those
repositories carry no dependency on the ``reckon`` package. It never imports
from ``reckon`` and never reads the host's registered-project mounts file —
only this host's own crew run pointers.

Behavior, in order:

1. Scope test — the guard applies only inside a crew run, which dispatch
   exports to the worker as ``RECKON_RUN_ID``. A coordinator session, with no
   run id in its environment, is untouched and every command is allowed.
2. Command split — the Bash command is tokenised and cut into the pipeline or
   statement segments it runs, tracking any ``cd`` so a later segment's
   working directory is the one the ``cd`` left behind.
3. Target resolution — for each segment that runs ``git``, the target
   repository is taken from ``-C``, ``--git-dir`` or ``--work-tree`` when one
   is given, and from the segment's working directory otherwise.
4. Decision — a mutating verb (checkout, restore, reset, clean, stash, commit,
   merge, rebase, pull, push, add, rm, mv, switch, cherry-pick, revert, am,
   apply) whose target resolves outside the run's own worktree is refused. A
   read-only verb, and any verb outside that set, stays allowed everywhere.
   The refusal names the run, its worktree and the target it would have
   touched.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

# The harness tool this guard watches. Matched by the harness hook wiring too
# (see the sync-owned hook config); checked again here so the script degrades
# safely if it is ever wired more broadly than intended.
GUARDED_TOOL = "Bash"

# The environment key the dispatch exports to every worker. Its presence is
# what marks the caller as a run-scoped worker rather than a coordinator.
RUN_ID_ENV = "RECKON_RUN_ID"

# The git subcommands that change a repository's state. A read-only verb, and
# any verb not named here, is never refused.
MUTATING_VERBS = frozenset(
    {
        "add",
        "am",
        "apply",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "merge",
        "mv",
        "pull",
        "push",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
    }
)

# What separates one shell segment from the next. A token equal to any of
# these ends the segment a ``cd`` or a ``git`` invocation is read from.
SEPARATOR_TOKENS = frozenset({";", "&&", "||", "|", "&", "\n"})

# Options that name the target repository, in the separated form git accepts
# (``-C <path>``) and the attached one (``--git-dir=<path>``).
PATH_OPTIONS = ("-C", "--git-dir", "--work-tree")


def _config_home() -> Path:
    """Resolve the reckon config home the same way the reckon package does.

    Duplicated rather than imported — see the module docstring.
    """
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser().resolve()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _run_worktree(run_id: str) -> Path | None:
    """Return the worktree the live pointer for ``run_id`` records, or None.

    The run id is reduced to its basename before it is used as a file name, so
    a value carrying a separator cannot reach outside the live directory.
    """
    live_dir = _config_home() / "crew" / "live"
    pointer = live_dir / f"{Path(run_id).name}.json"
    try:
        record = json.loads(pointer.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    worktree = record.get("worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        return None
    try:
        return Path(worktree).expanduser().resolve()
    except OSError:
        return None


def _tokenize(command: str) -> list[str] | None:
    """Split a shell command into tokens, or None when it cannot be parsed.

    ``None`` is the caller's signal to allow rather than guess: an unbalanced
    quote leaves the command split into words that are not its arguments, and
    a target read from a misparse would refuse a command that never ran.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> list[list[str]]:
    """Cut a token stream at its shell separators."""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SEPARATOR_TOKENS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _resolve(cwd: Path, target: str) -> Path:
    """Resolve ``target`` against ``cwd``, tolerating a path that is not there."""
    candidate = Path(os.path.expanduser(target))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate


def _cd_target(segment: list[str], cwd: Path) -> Path | None:
    """Return the directory a leading ``cd`` in this segment enters, else None."""
    head = 0
    while head < len(segment) and segment[head] == "(":
        head += 1
    if head >= len(segment) or segment[head] != "cd":
        return None
    arguments = [item for item in segment[head + 1 :] if item != "--"]
    if not arguments:
        return None
    if arguments[0] == "-":
        return cwd
    return _resolve(cwd, arguments[0])


def _git_index(segment: list[str]) -> int | None:
    """Return where the ``git`` invocation starts in this segment, if any.

    The invocation is recognised by the basename of its executable token, so a
    qualified path such as ``/usr/bin/git`` and a bare ``git`` are one shape.
    """
    for index, token in enumerate(segment):
        if token.startswith("-"):
            continue
        if Path(token).name == "git":
            return index
    return None


def _git_target(segment: list[str], start: int, cwd: Path) -> tuple[str | None, Path]:
    """Return ``(verb, target directory)`` for one ``git`` invocation.

    Global options are consumed up to the subcommand. ``--work-tree`` wins over
    ``--git-dir`` when both are given, and each option is read in its separated
    and attached forms. With no option naming a target, the segment's working
    directory is the target.
    """
    verb: str | None = None
    target: Path | None = None
    index = start + 1
    while index < len(segment):
        token = segment[index]
        if token in SEPARATOR_TOKENS:
            break
        value: str | None = None
        consumed = 1
        for option in PATH_OPTIONS:
            if token == option and index + 1 < len(segment):
                following = segment[index + 1]
                if following not in SEPARATOR_TOKENS:
                    value = following
                    consumed = 2
            elif token.startswith(f"{option}="):
                value = token[len(option) + 1 :]
            elif option == "-C" and token.startswith("-C") and len(token) > 2:
                value = token[2:]
            if value is not None:
                if option != "--git-dir" or target is None:
                    target = _resolve(cwd, value)
                break
        if value is not None:
            index += consumed
            continue
        if token.startswith("-"):
            # A global option that names no repository — ``-c``, ``--no-pager``,
            # ``--paginate`` — carries no target of its own.
            index += 1
            continue
        verb = token
        break
    return verb, cwd if target is None else target


def _within(target: Path, root: Path) -> bool:
    """Return whether ``target`` is ``root`` or lies inside it."""
    return target == root or root in target.parents


def _refusal_message(*, run_id: str, worktree: Path, verb: str, target: Path) -> str:
    return (
        f"mutating git refused: crew run {run_id} may run a mutating git verb "
        f"only against its own worktree ({worktree}), and this command runs "
        f"`git {verb}` against {target}. A mutating git verb aimed at another "
        "checkout can destroy work its author did not commit; run it in your "
        "own worktree instead. Read-only verbs (status, log, diff, show, "
        "rev-parse, grep) are allowed anywhere."
    )


def decide(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(allowed, message)`` for one Bash tool-call payload.

    ``message`` carries the refusal prose when ``allowed`` is False, and None
    when every mutating verb in the command targets the run's own worktree.
    """
    if str(payload.get("tool_name") or "") != GUARDED_TOOL:
        return True, None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return True, None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return True, None

    run_id = os.environ.get(RUN_ID_ENV)
    if not run_id:
        return True, None

    worktree = _run_worktree(run_id)
    if worktree is None:
        # A run id whose worktree cannot be read leaves no target to compare
        # against; refusing there would refuse every command the worker runs.
        return True, None

    try:
        cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    except OSError:
        return True, None

    tokens = _tokenize(command)
    if tokens is None:
        return True, None

    for segment in _segments(tokens):
        entered = _cd_target(segment, cwd)
        if entered is not None:
            cwd = entered
            continue
        start = _git_index(segment)
        if start is None:
            continue
        verb, target = _git_target(segment, start, cwd)
        if verb is None or verb not in MUTATING_VERBS:
            continue
        if not _within(target, worktree):
            return False, _refusal_message(
                run_id=run_id, worktree=worktree, verb=verb, target=target
            )
    return True, None


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    allowed, message = decide(payload)
    if allowed:
        if message:
            sys.stdout.write(json.dumps({"systemMessage": message}))
        return 0

    sys.stderr.write(
        json.dumps(
            {
                "hookSpecificOutput": {"permissionDecision": "deny"},
                "systemMessage": message,
            }
        )
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
