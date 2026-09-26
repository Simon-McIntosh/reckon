"""Refuse, at execution time, a mutating git verb aimed at another checkout.

A guard that reads a command's *text* cannot be made complete: a shell spelling
it does not model reaches the real git unexamined, and a wrapper inside another
process (`env GIT_DIR=... git ...`, a `cd` earlier in a script, a shell function)
is invisible to a parser reading one string. This module is the decision the
``git`` shim under :mod:`reckon.worker_shims` composes its behaviour from, and
it does not parse the command at all: it asks the real git which repository one
invocation resolves to, under that invocation's own environment, working
directory and ``-C``, and refuses a mutating verb unless that invocation's git
dir is the run's and the work tree it resolves lies inside the run's worktree.
Both must agree: ``--git-dir`` can name the run's own repository while
``--work-tree`` (or ``GIT_WORK_TREE``) writes another directory's files.

Scope comes from ``RECKON_RUN_ID``, the identity dispatch exports into every
worker; a coordinator session carries no run id and the shim is transparent for
it. The run's worktree is read from the run's live pointer, exactly as the
pre-tool-use guard reads it. A run id that is not a single safe path component
is refused rather than used to name a pointer file, so a value carrying a
separator cannot read a record outside the live directory.

The verb test is an allowlist, not a deny-list. Enumerating the ways a
repository can be changed is open-ended — a verb the list does not name, or a
name the target repository's own configuration aliases to a mutating verb, would
be forwarded unexamined — so every verb is treated as mutating unless it is on
the read-only allowlist below, and an alias, whose name is by definition not a
built-in read-only verb, is refused without being resolved. A forwarded
invocation replaces this process with the real git resolved with the shim's own
directory removed from ``PATH``, so the shim never finds itself, and the exit
status and signals are the real tool's.

A probe that cannot resolve the run's own git dir refuses, because the safe
direction is to leave the repository alone: the guard exists to stop a write
that would otherwise have happened, and refusing a command that turns out
harmless is visible and recoverable where an allowed write is not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# The environment key dispatch exports into every worker. Its presence marks
# the caller as a run-scoped worker rather than a coordinator.
RUN_ID_ENV = "RECKON_RUN_ID"

# The status a refusal exits with. Distinct from the codes the real git uses so
# a caller can tell a refusal from a failed git command.
REFUSAL_STATUS = 97

# The status returned when no real git can be found outside the shim.
MISSING_BINARY_STATUS = 127

# The built-in verbs that never change a repository, whatever arguments they
# carry. Every verb not named here is treated as mutating, so this list is the
# only way an invocation is forwarded under a run id. A verb that writes under
# one of its options is kept out: `fsck --lost-found` writes into the target's
# git dir, so no form of `fsck` is forwarded.
_READ_ONLY_VERBS = frozenset(
    {
        "blame",
        "cat-file",
        "check-attr",
        "check-ignore",
        "cherry",
        "count-objects",
        "describe",
        "diff",
        "diff-tree",
        "for-each-ref",
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
        "shortlog",
        "show",
        "show-ref",
        "status",
        "var",
        "verify-commit",
        "verify-tag",
        "version",
        "whatchanged",
    }
)

# Verbs that read with some arguments and write with others. A verb named here
# is read-only only in an explicit listing or reading form: the tail must name
# one of the verb's read actions, every option it carries must be an allowed
# token, and a bare invocation names no action at all and is therefore mutating
# (`git stash` pushes, `git branch` may create). The action and option sets are
# kept apart so a reading modifier (`config --local`) cannot stand in for an
# action: `config --local a.b value` writes; `config --local --list` reads.
_READ_ACTIONS: dict[str, frozenset[str]] = {
    "branch": frozenset(  # the listing modes; any other name creates a branch
        {
            "-a",
            "-l",
            "-r",
            "-v",
            "-vv",
            "--all",
            "--contains",
            "--format",
            "--list",
            "--merged",
            "--no-merged",
            "--points-at",
            "--remotes",
            "--show-current",
            "--sort",
            "--verbose",
        }
    ),
    "config": frozenset(  # the reading actions; anything else may set a value
        {
            "-l",
            "--get",
            "--get-all",
            "--get-color",
            "--get-colorbool",
            "--get-regexp",
            "--list",
        }
    ),
    "notes": frozenset({"list", "show"}),
    "reflog": frozenset({"show"}),
    "remote": frozenset({"-v", "--verbose", "get-url", "show"}),
    "stash": frozenset({"list", "show"}),
    "tag": frozenset(  # the listing modes; any other name creates a tag
        {
            "-l",
            "--list",
            "--column",
            "--contains",
            "--format",
            "--merged",
            "--no-merged",
            "--points-at",
            "--sort",
            "--verify",
        }
    ),
    "worktree": frozenset({"list"}),
}

# Every token that may appear anywhere in a read form: the verb's read actions
# plus the modifiers that qualify them without changing what they do. An option
# outside this union (`--unset`, `-D`) makes the invocation mutating wherever it
# sits.
_READ_OPTIONS: dict[str, frozenset[str]] = {
    "branch": _READ_ACTIONS["branch"],
    "config": _READ_ACTIONS["config"]
    | frozenset(
        {
            "-f",
            "-z",
            "--file",
            "--includes",
            "--local",
            "--name-only",
            "--null",
            "--show-origin",
            "--show-scope",
            "--type",
        }
    ),
    "notes": _READ_ACTIONS["notes"],
    "reflog": _READ_ACTIONS["reflog"],
    "remote": _READ_ACTIONS["remote"],
    "stash": _READ_ACTIONS["stash"],
    "tag": _READ_ACTIONS["tag"] | frozenset({"-n", "-v", "--format"}),
    "worktree": _READ_ACTIONS["worktree"],
}

# Git's global options that consume the token after them. Everything before the
# subject is forwarded verbatim, so this list only has to find the verb.
_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "-C",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
        "--attr-source",
    }
)

# Environment variables the run-worktree probe must not inherit: a ``GIT_DIR``
# left in a worker's environment would otherwise make the probe report the same
# foreign repository the invocation names, and the comparison would agree.
_DROPPED_PREFIX = "GIT_"


def worker_shim_directory() -> Path:
    """The directory the worker shims live in, derived from this module."""
    return Path(__file__).resolve().parent / "worker_shims"


def real_git(shim_dir: Path, path: str) -> str | None:
    """The real ``git`` on ``path`` with the shim's own directory removed.

    Removing the shim's directory is what stops the shim exec'ing itself: the
    lookup is by resolved directory, so a ``PATH`` entry reaching the shim
    directory through a symlink is dropped too.
    """
    skipped = os.path.realpath(str(shim_dir))
    kept = [
        entry
        for entry in str(path).split(os.pathsep)
        if entry and os.path.realpath(entry) != skipped
    ]
    if not kept:
        return None
    return shutil.which("git", path=os.pathsep.join(kept))


def _split_verb(argv: Sequence[str]) -> tuple[list[str], str, list[str]]:
    """Return one git argv as its global-option prefix, verb and remaining args.

    A token that is not an option ends the prefix: everything before it is the
    global options git accepts, the token itself is the verb (an empty string
    when the argv names none), and everything after it is returned as the tail.
    """
    prefix: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if not argument.startswith("-"):
            return prefix, argument, list(argv[index + 1 :])
        prefix.append(argument)
        if argument in _VALUE_OPTIONS and index + 1 < len(argv):
            prefix.append(argv[index + 1])
            index += 2
            continue
        index += 1
    return prefix, "", []


def _read_only_form(verb: str, tail: Sequence[str]) -> bool:
    """Whether a multi-purpose verb's arguments are an explicit read form.

    The form is read-only only when it names one of the verb's read actions,
    carries no option outside the verb's allowed options, and is not bare: a
    bare invocation names no action, and every multi-purpose verb does
    something on its own (`git stash` pushes, `git branch` may create). A
    positional token is the data a reading action consumes (`config --get a.b`),
    so only the action's presence is required, not the absence of arguments.
    """
    if not tail:
        return False
    actions = _READ_ACTIONS[verb]
    options = _READ_OPTIONS[verb]
    if tail[0] not in options:
        return False
    if any(token not in options for token in tail if token.startswith("-")):
        return False
    return any(token in actions for token in tail)


# Options that name a file or directory for git to write. A read verb carrying
# one is not a read: `git diff --output=<path>` writes <path>, whatever the verb
# on the allowlist. Both spellings are caught — the joined form
# (``--output=<path>``) and the separate form (``--output <path>``) — because
# the option token alone is matched.
#
# The short ``-o`` is deliberately absent. On this allowlist it never names an
# output: ``git ls-files -o`` means ``--others`` and ``git grep -o`` means
# ``--only-matching``, both reads. The verbs where ``-o`` does name an output
# directory (``format-patch``, ``archive``) are not on the allowlist and are
# refused whole, so a short-form check here would refuse reads and add nothing.
_OUTPUT_OPTIONS = frozenset({"--output", "--output-directory"})


def writing_argument(tail: Sequence[str]) -> str | None:
    """The option in ``tail`` that names an output file, or None.

    Only the option token is returned, so a refusal can name it. A separate
    value (``--output <path>``) is left in the tail and does not have to be
    joined to the option for the option to be found. The head is compared
    exactly, so ``--output-indent`` and the other ``--output-*`` modifiers
    — which write nothing — are not matched.
    """
    for token in tail:
        if not token.startswith("--"):
            continue
        head = token.split("=", 1)[0]
        if head in _OUTPUT_OPTIONS:
            return head
    return None


def output_refusal(verb: str, option: str) -> str:
    """The refusal naming an output-file option carried by a read verb.

    A read-only verb is forwarded without inspecting its repository, and it is
    forwarded with optional locks disabled, so nothing it does normally writes.
    An output option defeats that: the file it names is written wherever it
    points, so the option is refused instead of the invocation.
    """
    lines = [
        (
            f"refusing `git {verb}`: `{option}` names an output file, so the "
            "command would write wherever the path points."
        ),
        (
            "A crew worker may write only inside its own worktree. Drop the "
            "option, or run a form that writes nothing."
        ),
    ]
    return "\n".join(lines)


def mutating_verb(verb: str, tail: Sequence[str]) -> str | None:
    """The verb to treat as mutating, or None when it is read-only.

    The allowlist is the decision: a verb is read-only only when it is a
    built-in reader, or a multi-purpose verb in an explicit read form. Anything
    else — including a name the target repository aliases to a reader or a
    writer, since the name itself is neither — is returned as mutating and
    refused. Refusing an alias that would have been harmless is visible and
    recoverable; forwarding an alias whose expansion this cannot see is not.
    """
    if not verb:
        return None
    if verb in _READ_ONLY_VERBS:
        return None
    if verb in _READ_ACTIONS and _read_only_form(verb, tail):
        return None
    return verb


def _safe_run_component(run_id: str) -> bool:
    """Whether a run id is a single path component safe to name a file with.

    A value carrying a separator or standing for a directory of its own is not
    one run's id, and using it would read a record outside the live directory.
    The test is the same one the pre-tool-use guard applies when it reduces a
    run id to a basename: the name must survive the reduction unchanged.
    """
    if not run_id or run_id in {os.curdir, os.pardir}:
        return False
    if os.sep in run_id or (os.altsep and os.altsep in run_id):
        return False
    return Path(run_id).name == run_id


def _probe(
    git: str, prefix: Sequence[str], *, environ: Mapping[str, str], cwd: str | None
) -> tuple[str | None, str | None]:
    """Ask the real git which repository ``prefix`` resolves to.

    Returns the absolute git dir and the work tree top level, either of which
    may be None when git cannot report it. The probe runs under the caller's
    own environment and working directory, which is the whole point: it sees
    ``GIT_DIR``, ``GIT_WORK_TREE`` and ``-C`` exactly as the real invocation
    would.
    """
    try:
        completed = subprocess.run(
            [git, *prefix, "rev-parse", "--absolute-git-dir", "--show-toplevel"],
            env=dict(environ),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None, None
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    git_dir = lines[0].strip() if lines else None
    toplevel = lines[1].strip() if len(lines) > 1 else None
    return git_dir, toplevel


def _run_git_dir(git: str, worktree: Path, *, environ: Mapping[str, str]) -> str | None:
    """The absolute git dir of the run's worktree, or None when unreadable.

    The probe environment drops every ``GIT_*`` variable so a ``GIT_DIR`` in the
    worker's own environment cannot make the run's worktree report some other
    repository — the failure that would make the comparison agree with the very
    command it must refuse.
    """
    clean = {
        key: value
        for key, value in environ.items()
        if not key.startswith(_DROPPED_PREFIX)
    }
    git_dir, _ = _probe(git, ["-C", str(worktree)], environ=clean, cwd=None)
    return git_dir


def _run_worktree(run_id: str) -> Path | None:
    """The worktree the live pointer for ``run_id`` records, or None.

    The caller rejects a run id that is not a single safe path component before
    reaching here, so the id names a file inside the live directory; a record
    the pointer cannot be read from is None, and the caller refuses rather than
    guessing a worktree.
    """
    from reckon.crew.runs import pointer_path

    try:
        record = json.loads(pointer_path(run_id).read_text())
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


def _same(left: str | None, right: str | None) -> bool:
    """Whether two reported paths name the same directory."""
    if not left or not right:
        return False
    return os.path.realpath(left) == os.path.realpath(right)


def _within(child: str | None, parent: Path) -> bool:
    """Whether a reported directory is ``parent`` itself or beneath it."""
    if not child:
        return False
    try:
        inner = Path(os.path.realpath(child))
        outer = Path(os.path.realpath(parent))
    except OSError:
        return False
    return inner == outer or outer in inner.parents


def refuses(
    *,
    run_id: str,
    verb: str,
    invocation_git_dir: str | None,
    invocation_toplevel: str | None,
    worktree: Path,
    worktree_git_dir: str | None,
) -> bool:
    """Whether this invocation must be refused.

    Two things must agree, because either alone can be pointed elsewhere. The
    git dir must be the run's, since that is where a mutating verb's HEAD, refs
    and index live; and the work tree the invocation resolves must be inside the
    run's worktree, since ``--work-tree`` (or ``GIT_WORK_TREE``) can name the
    run's own git dir while writing a different directory's files. A matching
    git dir with a foreign work tree is ``git --git-dir <run>/.git --work-tree
    <other>``, which is the escape both checks together close.
    """
    if worktree_git_dir is None:
        return True
    if not _same(invocation_git_dir, worktree_git_dir):
        return True
    return not _within(invocation_toplevel, worktree)


def refusal_message(
    *,
    run_id: str,
    verb: str,
    worktree: Path,
    worktree_git_dir: str | None,
    invocation_git_dir: str | None,
    invocation_toplevel: str | None,
) -> str:
    """The refusal, naming the run, its worktree and the resolved target.

    Both halves of the resolved target are named, because either one alone can
    be the mismatch: a foreign git dir, or the run's own git dir with a work
    tree pointing somewhere else.
    """
    head = (
        f"refusing `git {verb}`: it runs against a repository that is not this "
        "run's worktree."
    )
    lines = [
        head,
        f"  run:      {run_id}",
        f"  worktree: {worktree} (git dir {worktree_git_dir or 'unresolved'})",
        (
            f"  target:   git dir {invocation_git_dir or 'unresolved'}, "
            f"work tree {invocation_toplevel or 'unresolved'}"
        ),
        (
            "A crew worker may run a mutating git verb only against its own "
            "worktree, so nothing was changed. Run the verb inside the worktree "
            "instead. Read-only verbs (status, log, diff, show, rev-parse, grep) "
            "are forwarded with GIT_OPTIONAL_LOCKS=0, so they read another "
            "checkout without refreshing its index, and an option naming an "
            "output file is refused."
        ),
    ]
    return "\n".join(lines)


def main(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> int:
    """Run, or refuse, one git invocation.

    Returns the refusal status when the invocation is refused and otherwise
    replaces this process with the real git. The return is for the refusal and
    the missing-binary cases only; a forwarded invocation does not come back.
    """
    env = os.environ if environ is None else environ
    shim_dir = worker_shim_directory()
    found = real_git(shim_dir, str(env.get("PATH") or os.defpath))
    if found is None:
        print(
            f"git shim: no real git on PATH outside {shim_dir}",
            file=sys.stderr,
        )
        return MISSING_BINARY_STATUS

    run_id = str(env.get(RUN_ID_ENV) or "").strip()
    prefix, verb, tail = _split_verb(argv)
    if not run_id:
        return _forward(found, argv, environ=env)
    guarded = mutating_verb(verb, tail)
    writing = writing_argument(tail)
    if guarded is None and writing is None:
        return _forward(found, argv, environ=env)
    if writing is not None:
        print(output_refusal(verb or "git", writing), file=sys.stderr)
        return REFUSAL_STATUS
    if not _safe_run_component(run_id):
        print(
            f"refusing `git {guarded}`: {RUN_ID_ENV}={run_id!r} is not a single "
            "path component, so it cannot name this run's live pointer and the "
            "command changed nothing.",
            file=sys.stderr,
        )
        return REFUSAL_STATUS

    worktree = _run_worktree(run_id)
    if worktree is None:
        print(
            f"refusing `git {guarded}`: {RUN_ID_ENV}={run_id} has no readable "
            "live pointer, so this run's worktree cannot be resolved and the "
            "command changed nothing.",
            file=sys.stderr,
        )
        return REFUSAL_STATUS

    worktree_git_dir = _run_git_dir(found, worktree, environ=env)
    invocation_git_dir, toplevel = _probe(found, prefix, environ=env, cwd=cwd)
    if not refuses(
        run_id=run_id,
        verb=guarded,
        invocation_git_dir=invocation_git_dir,
        invocation_toplevel=toplevel,
        worktree=worktree,
        worktree_git_dir=worktree_git_dir,
    ):
        return _forward(found, argv, environ=env)
    print(
        refusal_message(
            run_id=run_id,
            verb=guarded,
            worktree=worktree,
            worktree_git_dir=worktree_git_dir,
            invocation_git_dir=invocation_git_dir,
            invocation_toplevel=toplevel,
        ),
        file=sys.stderr,
    )
    return REFUSAL_STATUS


def _forward(git: str, argv: Sequence[str], *, environ: Mapping[str, str]) -> int:
    """Replace this process with the real git, argv unchanged.

    ``GIT_OPTIONAL_LOCKS=0`` is set on the forwarded environment: it is git's
    documented switch that stops a read from taking an optional lock, and
    without it a plain `status` against another checkout refreshes and rewrites
    that checkout's index when the stat cache is stale — a write performed by a
    verb treated as read-only. The binary is resolved by absolute path and the
    argv is already split, so there is no shell to route through; exec forwards
    signals and the exit status unchanged.
    """
    environment = dict(environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    os.execve(git, ["git", *argv], environment)  # noqa: S606
    raise AssertionError("os.execve returned; a forwarded invocation cannot continue.")
