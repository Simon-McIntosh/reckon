#!/usr/bin/env python3
"""Pre-tool-use hook: refuse a crew worker's recursive walk over the crew home.

The crew home holds every run's stream, manifest and record — hundreds of
gigabytes on a filesystem every session on the machine shares — so a search for
an id that matches few files still reads nearly all of it, and a pager at the
end of the pipeline bounds only the output, never the walk. A worker looking for
a record does not need one: a run's directory is ``<crew home>/runs/<run id>/``,
a reviewed run's records are under ``<its run directory>/review-records/``,
stored findings are read with ``reckon crew view findings``, and another run's
directory is named by ``reckon crew directory``. This guard closes the walk at
the tool boundary so the worker takes one of those paths instead.

Self-contained and stdlib-only on purpose, like the sibling guards distributed
the same way: it never imports from ``reckon`` and never reads the host's
registered-project mounts file. The Bash half does not keep a shell parser of
its own, though. It loads the worker git guard beside it by file path and reuses
that hook's tokeniser, segment split and ``cd`` tracking, so the two hooks
cannot drift on what a shell command's segments and working directory are. The
load is by file rather than by import so this hook stays stdlib-only and out of
the set of hook scripts that must be re-executed under a checkout interpreter:
it needs no package import of its own.

Behavior, in order:

1. Scope test — the guard applies only inside a crew run, which dispatch exports
   to the worker as ``RECKON_RUN_ID``. A coordinator session, with no run id in
   its environment, is untouched and every call is allowed.
2. Crew home — resolved through the git guard's own config-home resolver, so it
   honours the same ``RECKON_HOME`` override every other reckon path does.
3. Refused roots — the crew home itself, a store root inside it (``runs/``,
   ``reviews/``, ``reports/``), and any ancestor of the crew home up to ``/``. A
   walk rooted inside one run's directory, and any walk outside the crew home
   and its ancestors, stay allowed.
4. Bash — the command is tokenised and cut into segments, tracking ``cd`` and
   descending into nested scripts, exactly as the git guard does. A segment
   running a recursive walk program whose root resolves to a refused root is
   denied.
5. Grep and Glob — the tool's ``path`` is the walk's root, so it is judged the
   same way.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# The tool halves this guard watches. The harness wires one group per matcher
# (``Bash`` and ``Grep|Glob``); each is checked again here so a group wired more
# broadly than intended degrades to a decision rather than to a blanket refusal.
BASH_TOOL = "Bash"
PATH_TOOLS = frozenset({"Grep", "Glob"})

# The environment key crew dispatch exports to every worker it launches. Its
# presence marks the caller as a run-scoped worker rather than a coordinator.
RUN_ID_ENV = "RECKON_RUN_ID"

# The crew home's subdirectories that hold a whole store rather than one run's
# record. A walk rooted at one of these reads every run's records.
STORE_ROOTS = ("runs", "reviews", "reports")

# The Bash programs that walk a directory tree. Each names its root differently
# and each is read for the root this invocation would descend from.
GREP_FAMILY = frozenset({"grep", "egrep", "fgrep", "ugrep"})
# Recursive without being asked, so no flag marks them.
ALWAYS_RECURSIVE = frozenset({"rg", "fd", "tree", "du"})
# Recursive unless a shallow ``-maxdepth`` bounds them.
FIND_FAMILY = frozenset({"find", "bfs"})
# Recursive only under ``-R``/``--recursive``; a plain listing is not a walk.
LS_PROGRAM = "ls"
WALK_PROGRAMS = GREP_FAMILY | ALWAYS_RECURSIVE | FIND_FAMILY | frozenset({LS_PROGRAM})
# A ``grep``-family walk is marked by one of these, in its own token or bundled
# with other short options.
GREP_WALK_LETTERS = frozenset({"r", "R"})
LS_WALK_LETTERS = frozenset({"R"})
LONG_RECURSIVE_FLAG = "--recursive"

# ``find`` bounds its descent with a depth option; 0 or 1 lists a directory's
# own entries rather than walking the tree.
DEPTH_OPTIONS = ("-maxdepth", "--maxdepth")
SHALLOW_DEPTHS = frozenset({"0", "1"})

# The git guard beside this hook: the one shell splitter both hooks read.
SPLITTER_PATH = Path(__file__).with_name("worker_git_guard.py")
SPLITTER_MODULE = "_crew_hook_shell_splitter"

# Words are read out of a command the tokeniser could not split, to ask whether
# it carries a walk program at all before its path-like tokens are resolved.
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
PATH_WORD_RE = re.compile(r"[^\s'\"`|;&()<>]+")


def _load_splitter() -> Any:
    """Load the git guard beside this hook as a module, without importing it.

    By file rather than by import: importing the git guard through the package
    would put this hook in the set of hook scripts that import ``reckon``, and
    it needs no package import of its own. The module is loaded under a private
    name so it cannot shadow, or be shadowed by, the git guard's own entry in
    ``sys.modules``.
    """
    spec = importlib.util.spec_from_file_location(SPLITTER_MODULE, SPLITTER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"no loader for {SPLITTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_splitter: Any = None
_splitter_error: str | None = None
try:
    _splitter = _load_splitter()
except Exception as exc:  # noqa: BLE001 - an unloadable splitter must deny
    _splitter_error = str(exc)


def _crew_home() -> Path:
    """Resolve the crew home through the git guard's config-home resolver.

    Read from the splitter module rather than re-derived: the crew home is
    ``<config home>/crew`` everywhere reckon resolves it, and a second copy of
    the resolution order here could disagree with the one the rest of reckon
    reads the same environment through.
    """
    return _splitter._config_home() / "crew"


def _root_is_refused(root: Path, home: Path) -> bool:
    """Return whether a recursive walk rooted at ``root`` is refused.

    Refused: the crew home, a store root inside it, and every ancestor of the
    crew home up to ``/`` — a walk rooted above the crew home reads it too.
    Allowed: anywhere inside one run's directory, and anywhere outside the crew
    home and its ancestors.
    """
    if root == home or root in home.parents:
        return True
    return any(root == home / store for store in STORE_ROOTS)


def _refusal_message(*, root: Path, home: Path, run_id: str) -> str:
    return (
        f"recursive walk refused: crew run {run_id} may not walk {root} — the "
        f"crew home ({home}) holds every run's stream and record, so a walk "
        "rooted at it or above it reads nearly all of that on a filesystem "
        "every session here shares, and a pager at the end bounds only the "
        "output. Read the record at its own path instead: your own run "
        f"directory is {home}/runs/{run_id}/; a reviewed run's records are "
        f"under {home}/runs/<reviewed run>/review-records/; stored findings are "
        "read with `reckon crew view findings --project <project> --path "
        "<prefix>`; another run's directory is named by `reckon crew directory "
        "--run <id>`. A walk rooted inside one run's directory is allowed."
    )


def _flag_letters(arguments: list[str]) -> set[str]:
    """Return the short-option letters present in an argument list.

    Bundled options are read letter by letter, because the form that walks a
    tree is often written bundled: ``grep -rl`` and ``ls -lR`` carry the
    recursive option inside the group.
    """
    letters: set[str] = set()
    for token in arguments:
        if not token.startswith("-") or token.startswith("--") or len(token) < 2:
            continue
        letters.update(character for character in token[1:] if character.isalpha())
    return letters


def _find_depth_is_shallow(arguments: list[str]) -> bool:
    """Return whether a ``find``/``bfs`` invocation bounds itself to depth 0-1."""
    for index, token in enumerate(arguments):
        for option in DEPTH_OPTIONS:
            if token == option and index + 1 < len(arguments):
                if arguments[index + 1] in SHALLOW_DEPTHS:
                    return True
            elif token.startswith(f"{option}=") and token[len(option) + 1 :] in (
                SHALLOW_DEPTHS
            ):
                return True
    return False


def _is_recursive(program: str, arguments: list[str]) -> bool:
    """Return whether this invocation walks a directory tree."""
    if program in ALWAYS_RECURSIVE:
        return True
    if program in FIND_FAMILY:
        return not _find_depth_is_shallow(arguments)
    if program in GREP_FAMILY:
        if LONG_RECURSIVE_FLAG in arguments:
            return True
        return bool(_flag_letters(arguments) & GREP_WALK_LETTERS)
    if program == LS_PROGRAM:
        if LONG_RECURSIVE_FLAG in arguments:
            return True
        return bool(_flag_letters(arguments) & LS_WALK_LETTERS)
    return False


def _operands(arguments: list[str]) -> list[str]:
    """Return the argument list's non-option tokens, in order."""
    return [token for token in arguments if not token.startswith("-")]


def _leading_operands(arguments: list[str]) -> list[str]:
    """Return the tokens before a ``find`` argument list's first expression.

    ``find`` takes its roots first and everything after the first option is its
    expression, so only the leading non-option tokens name paths.
    """
    roots: list[str] = []
    for token in arguments:
        if token.startswith("-") or token in {"(", "!", ")"}:
            break
        roots.append(token)
    return roots


def _walk_roots(program: str, arguments: list[str]) -> list[str]:
    """Return the unresolved root arguments this invocation would walk.

    Each program names its root differently. ``grep`` and its family take the
    pattern first and every later operand as a path; ``rg`` and ``fd`` put the
    pattern there too; ``tree``, ``du`` and ``ls`` take paths only; ``find``
    takes its roots before its expression.
    """
    if program in FIND_FAMILY:
        return _leading_operands(arguments)
    operands = _operands(arguments)
    if program in GREP_FAMILY or program in {"rg", "fd"}:
        return operands[1:]
    return operands


def _split_program(segment: list[str]) -> tuple[str, list[str]]:
    """Return the program a command segment runs and its arguments.

    The segment's leading ``NAME=value`` assignments are the shell's, not the
    command's, and the splitter has already removed them by the time this is
    called.
    """
    if not segment:
        return "", []
    return Path(segment[0]).name, segment[1:]


def _judge_segment(
    segment: list[str], cwd: Path, home: Path, run_id: str
) -> str | None:
    """Return the refusal a segment earns, or None.

    A segment that does not run a recursive walk program, or whose root resolves
    outside the crew home and its ancestors, stays allowed.
    """
    program, arguments = _split_program(segment)
    if program not in WALK_PROGRAMS or not _is_recursive(program, arguments):
        return None
    roots = _walk_roots(program, arguments) or ["."]
    for raw in roots:
        root = _splitter._resolve(cwd, raw)
        if _root_is_refused(root, home):
            return _refusal_message(root=root, home=home, run_id=run_id)
    return None


def _unparsable_refusal(text: str, cwd: Path, home: Path, run_id: str) -> str | None:
    """Refuse a command that cannot be read and names a refused root to a walker.

    A command the tokeniser cannot split has no segments to resolve a root from,
    so the guard cannot show that a walk inside it stays outside the crew home.
    Allowing it would let a nested or malformed form carry the walk past the
    check by hiding it; a command naming no refused root from a walk program's
    own words is still allowed.
    """
    if not WALK_PROGRAMS & set(WORD_RE.findall(text)):
        return None
    for word in PATH_WORD_RE.findall(text):
        root = _splitter._resolve(cwd, word)
        if _root_is_refused(root, home):
            return _refusal_message(root=root, home=home, run_id=run_id)
    return None


def _scan_text(text: str, cwd: Path, home: Path, run_id: str, depth: int) -> str | None:
    """Scan one shell script, returning the refusal it earns or None."""
    if depth > _splitter.MAX_NESTING:
        return _unparsable_refusal(text, cwd, home, run_id)
    tokens = _splitter._tokenize(text)
    if tokens is None:
        return _unparsable_refusal(text, cwd, home, run_id)
    return _scan_segments(tokens, cwd, home, run_id, depth)


def _scan_segments(
    tokens: list[str], cwd: Path, home: Path, run_id: str, depth: int
) -> str | None:
    """Scan a token stream, returning the first refusal it earns or None.

    Each segment's nested scripts are scanned before the segment's own command
    is judged, with the working directory the segment was reached with, so a
    ``cd`` inside a ``bash -c`` script or a command substitution is tracked
    within that script.
    """
    for segment in _splitter._segments(tokens):
        _, rest = _splitter._split_assignments(segment)
        entered = _splitter._cd_target(rest, cwd)
        if entered is not None:
            cwd = entered
            continue
        script = _splitter._shell_script(rest)
        if script is not None:
            message = _scan_text(script, cwd, home, run_id, depth + 1)
            if message is not None:
                return message
        for token in rest:
            for body in _splitter._nested_bodies(token):
                message = _scan_text(body, cwd, home, run_id, depth + 1)
                if message is not None:
                    return message
        message = _judge_segment(rest, cwd, home, run_id)
        if message is not None:
            return message
    return None


def _path_tool_refusal(
    tool_input: dict[str, Any], cwd: Path, home: Path, run_id: str
) -> str | None:
    """Return the refusal the Grep/Glob tool's target directory earns."""
    raw = tool_input.get("path")
    root = cwd if not isinstance(raw, str) or not raw.strip() else _resolve(raw, cwd)
    if _root_is_refused(root, home):
        return _refusal_message(root=root, home=home, run_id=run_id)
    return None


def _resolve(value: str, cwd: Path) -> Path:
    """Resolve a command word or tool path against the working directory."""
    return _splitter._resolve(cwd, value)


def decide(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(allowed, message)`` for one tool-call payload.

    ``message`` carries the refusal prose when ``allowed`` is False, and None
    when the call walks no refused root.
    """
    if _splitter is None:
        return False, f"worker walk guard could not load its rules: {_splitter_error}"

    tool_name = str(payload.get("tool_name") or "")
    if tool_name != BASH_TOOL and tool_name not in PATH_TOOLS:
        return True, None

    run_id = os.environ.get(RUN_ID_ENV)
    if not run_id:
        return True, None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return True, None

    try:
        cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    except OSError:
        return True, None

    try:
        home = _crew_home()
        if tool_name in PATH_TOOLS:
            message = _path_tool_refusal(tool_input, cwd, home, run_id)
            return (True, None) if message is None else (False, message)

        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return True, None
        for body in _splitter._nested_bodies(command):
            message = _scan_text(body, cwd, home, run_id, 1)
            if message is not None:
                return False, message
        tokens = _splitter._tokenize(command)
        if tokens is None:
            message = _unparsable_refusal(command, cwd, home, run_id)
            return (True, None) if message is None else (False, message)
        message = _scan_segments(tokens, cwd, home, run_id, 0)
    except Exception as exc:  # noqa: BLE001 - an unreadable guard must deny
        return False, f"worker walk guard could not read its rules: {exc}"
    return (True, None) if message is None else (False, message)


def hook_output(allowed: bool, message: str | None) -> dict[str, Any] | None:
    """Compose the harness pre-tool-use JSON for one decision.

    The harness parses this JSON only from stdout on exit 0. A refusal is a
    ``deny`` permission decision whose reason the calling session reads as the
    tool result.
    """
    if allowed:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    output = hook_output(*decide(payload))
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
