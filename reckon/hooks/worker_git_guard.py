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
import re
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

# The environment keys a leading assignment may set to name the target
# repository, in the sense git itself reads.
ENV_GIT_DIR = "GIT_DIR"
ENV_GIT_WORK_TREE = "GIT_WORK_TREE"

# A leading ``NAME=value`` assignment on a command segment.
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# The git subcommands that only read. Their membership is deliberately narrow:
# a verb not listed here is treated as mutating when it is aimed outside the
# run's worktree, which is the safe direction. Subcommands whose safety depends
# on their arguments — ``branch``, ``tag``, ``remote``, ``config`` — are
# therefore absent, and are refused against another checkout rather than
# assumed harmless.
READ_ONLY_VERBS = frozenset(
    {
        "blame",
        "cat-file",
        "check-attr",
        "check-ignore",
        "count-objects",
        "describe",
        "diff",
        "for-each-ref",
        "fsck",
        "grep",
        "help",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "shortlog",
        "status",
        "verify-commit",
        "verify-tag",
        "version",
        "whatchanged",
    }
)

# A config entry set with ``-c`` that defines an alias, and how many expansion
# hops are followed before an alias cycle is called unresolvable.
CONFIG_OPTION = "-c"
ALIAS_PREFIX = "alias."
MAX_ALIAS_HOPS = 8

# Shells whose ``-c`` argument is a script this guard scans in its own right.
SHELL_WORDS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})

# The nested command forms a plain token split does not expose: a command
# substitution, a backtick substitution and a brace group. Each body is a
# script scanned on its own terms, with its own ``cd``.
NESTED_BODY_PATTERNS = (
    re.compile(r"\$\((.*?)\)", re.DOTALL),
    re.compile(r"`([^`]*)`"),
    re.compile(r"\{([^{}]*)\}", re.DOTALL),
)

# How deep a nested script may nest before the guard stops descending. A
# command nested deeper than this is refused when it carries a mutating verb,
# rather than allowed unread — the whole point of scanning a nested form is
# that the outer text hid the verb.
MAX_NESTING = 8

# Words are read out of an unparsable command to ask whether it carries a
# mutating verb at all.
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


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


def _split_assignments(segment: list[str]) -> tuple[dict[str, str], list[str]]:
    """Split the leading ``NAME=value`` assignments off a command segment.

    The shell applies these to the command that follows, and git reads
    ``GIT_DIR`` and ``GIT_WORK_TREE`` from them, so they are part of the target
    a ``git`` invocation resolves to.
    """
    environment: dict[str, str] = {}
    head = 0
    while head < len(segment) and ASSIGNMENT_RE.match(segment[head]):
        name, _, value = segment[head].partition("=")
        environment[name] = value
        head += 1
    return environment, segment[head:]


def _cd_target(segment: list[str], cwd: Path) -> Path | None:
    """Return the directory a leading ``cd`` in this segment enters, else None."""
    head = 0
    while head < len(segment) and segment[head] in {"(", "{"}:
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


def _git_target(
    segment: list[str], start: int, cwd: Path, environment: dict[str, str]
) -> tuple[str | None, Path, dict[str, str]]:
    """Return ``(verb, target directory, aliases)`` for one ``git`` invocation.

    Global options are consumed up to the subcommand. ``-C`` moves the base the
    other options resolve against; ``--work-tree`` wins over ``--git-dir`` when
    both are given, and each is read in its separated and attached forms. A
    leading assignment of ``GIT_WORK_TREE`` or ``GIT_DIR`` names the target the
    same way the option does, and a command-line option overrides it. ``-c``
    settings are collected so a verb the command aliases can be expanded. With
    no option and no assignment naming a target, the segment's working
    directory is the target.
    """
    aliases: dict[str, str] = {}
    base = cwd
    work_tree: str | None = environment.get(ENV_GIT_WORK_TREE)
    git_dir: str | None = environment.get(ENV_GIT_DIR)
    verb: str | None = None
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
                if option == "-C":
                    base = _resolve(cwd, value)
                elif option == "--work-tree":
                    work_tree = value
                else:
                    git_dir = value
                break
        if value is not None:
            index += consumed
            continue
        if token == CONFIG_OPTION and index + 1 < len(segment):
            _record_alias(segment[index + 1], aliases)
            index += 2
            continue
        if token.startswith(CONFIG_OPTION) and len(token) > len(CONFIG_OPTION):
            _record_alias(token[len(CONFIG_OPTION) :], aliases)
            index += 1
            continue
        if token.startswith("-"):
            # A global option that names no repository — ``--no-pager``,
            # ``--paginate`` — carries no target of its own.
            index += 1
            continue
        verb = token
        break
    if work_tree is not None:
        target = _resolve(base, work_tree)
    elif git_dir is not None:
        target = _resolve(base, git_dir)
    else:
        target = base
    return verb, target, aliases


def _record_alias(entry: str, aliases: dict[str, str]) -> None:
    """Record a ``-c`` setting when it defines an alias."""
    name, separator, expansion = entry.partition("=")
    if separator and name.startswith(ALIAS_PREFIX):
        aliases[name[len(ALIAS_PREFIX) :]] = expansion


def _is_mutating(verb: str, aliases: dict[str, str]) -> bool:
    """Return whether ``verb`` changes a repository, following its aliases.

    A verb that is neither a known mutating nor a known read-only subcommand is
    not a git built-in this guard can classify. An alias the command itself
    defines is expanded and its first word judged in turn; one that cannot be
    resolved that way may expand to anything, so it counts as mutating.
    """
    name = verb
    for _ in range(MAX_ALIAS_HOPS):
        if name in MUTATING_VERBS:
            return True
        if name in READ_ONLY_VERBS:
            return False
        expansion = aliases.get(name)
        if not expansion:
            return True
        words = expansion.split()
        if not words or words[0] == name:
            return True
        name = words[0]
    return True


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


def _mutating_word(text: str) -> str | None:
    """Return the first mutating git verb word in an unparsable command."""
    for word in WORD_RE.findall(text):
        if word in MUTATING_VERBS:
            return word
    return None


def _unparsable_refusal(text: str, *, run_id: str, worktree: Path) -> str | None:
    """Refuse a command that cannot be read and carries a mutating verb.

    A command the tokeniser cannot split has no segments to resolve a target
    from, so the guard cannot show that its ``git`` invocation stays in this
    run's worktree. Allowing it would let a nested or malformed form carry a
    mutating verb past the check by hiding it, which is the defect this scan
    exists to close; a command with no mutating verb in it is still allowed.
    """
    verb = _mutating_word(text)
    if verb is None:
        return None
    return (
        f"mutating git refused: crew run {run_id} may run a mutating git verb "
        f"only against its own worktree ({worktree}), and this command cannot "
        f"be parsed as a shell script while it carries `{verb}`, so the guard "
        "cannot show which repository it would touch. Write it as one plain "
        "command the guard can resolve, or run it in your own worktree."
    )


def _nested_bodies(text: str) -> list[str]:
    """Return the script bodies a nested command form hides inside ``text``."""
    bodies: list[str] = []
    for pattern in NESTED_BODY_PATTERNS:
        bodies.extend(match.group(1) for match in pattern.finditer(text))
    return bodies


def _shell_script(segment: list[str]) -> str | None:
    """Return the script a ``bash -c``/``sh -c`` segment runs, if it does."""
    for index, token in enumerate(segment):
        if Path(token).name not in SHELL_WORDS:
            continue
        for following in range(index + 1, len(segment) - 1):
            if segment[following] == "-c":
                return segment[following + 1]
            if segment[following] in SEPARATOR_TOKENS:
                break
        return None
    return None


def _scan_text(
    text: str, cwd: Path, worktree: Path, run_id: str, depth: int
) -> str | None:
    """Scan one shell script, returning the refusal it earns or None."""
    if depth > MAX_NESTING:
        return _unparsable_refusal(text, run_id=run_id, worktree=worktree)
    tokens = _tokenize(text)
    if tokens is None:
        return _unparsable_refusal(text, run_id=run_id, worktree=worktree)
    return _scan_segments(tokens, cwd, worktree, run_id, depth)


def _scan_segments(
    tokens: list[str], cwd: Path, worktree: Path, run_id: str, depth: int
) -> str | None:
    """Scan a token stream, returning the first refusal it earns or None.

    Each segment's nested scripts are scanned before its own ``git`` invocation
    is judged, with the working directory the segment was reached with, so a
    ``cd`` inside a nested script is tracked within that script.
    """
    for segment in _segments(tokens):
        environment, rest = _split_assignments(segment)
        entered = _cd_target(rest, cwd)
        if entered is not None:
            cwd = entered
            continue
        script = _shell_script(rest)
        if script is not None:
            message = _scan_text(script, cwd, worktree, run_id, depth + 1)
            if message is not None:
                return message
        for token in rest:
            for body in _nested_bodies(token):
                message = _scan_text(body, cwd, worktree, run_id, depth + 1)
                if message is not None:
                    return message
        start = _git_index(rest)
        if start is None:
            continue
        verb, target, aliases = _git_target(rest, start, cwd, environment)
        if verb is None or not _is_mutating(verb, aliases):
            continue
        if not _within(target, worktree):
            return _refusal_message(
                run_id=run_id, worktree=worktree, verb=verb, target=target
            )
    return None


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

    for body in _nested_bodies(command):
        message = _scan_text(body, cwd, worktree, run_id, 1)
        if message is not None:
            return False, message

    tokens = _tokenize(command)
    if tokens is None:
        message = _unparsable_refusal(command, run_id=run_id, worktree=worktree)
        return (True, None) if message is None else (False, message)

    message = _scan_segments(tokens, cwd, worktree, run_id, 0)
    return (True, None) if message is None else (False, message)


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
