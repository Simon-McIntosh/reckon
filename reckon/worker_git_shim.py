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

A read verb aimed at some other repository is a separate problem from a
mutating one: it changes no ref, but some reads refresh the stat cache and
record the refresh in that repository's index, and the answer git gives for
it depends on the index it writes. Rather than enumerate the option spellings
that make a verb refresh, a read that resolves to a repository which is not the
run's worktree is run with ``GIT_INDEX_FILE`` naming a private copy of that
repository's index, so an in-process refresh writes the copy whichever spelling
the verb took; the copy is removed when the command ends. That case runs the
real git as a child (the copy can only be removed once git has returned) and
everything else still execs.

A probe that cannot resolve the run's own git dir refuses, because the safe
direction is to leave the repository alone: the guard exists to stop a write
that would otherwise have happened, and refusing a command that turns out
harmless is visible and recoverable where an allowed write is not.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
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
# the option token alone is matched, and a spelling that only abbreviates one of
# these is matched too (see ``_abbreviates``).
#
# The short ``-o`` is deliberately absent. On this allowlist it never names an
# output: ``git ls-files -o`` means ``--others`` and ``git grep -o`` means
# ``--only-matching``, both reads. The verbs where ``-o`` does name an output
# directory (``format-patch``, ``archive``) are not on the allowlist and are
# refused whole, so a short-form check here would refuse reads and add nothing.
_OUTPUT_OPTIONS = ("--output", "--output-directory")

# The shortest a long option's name may be and still be a stable refusal target
# when it abbreviates a banned name. git resolves any unambiguous abbreviation,
# so a check comparing whole names would forward ``--outp=<path>`` while
# refusing ``--output=<path>``, which is the same write. Three characters is the
# floor below which a spelling is too short to be a guess at any one option.
_MIN_OPTION_PREFIX_LENGTH = 3


def _long_option_name(token: str) -> str | None:
    """The name part of a long option, or None when the token is not one.

    ``--output=x`` and ``--output`` both give ``output``.
    """
    if not token.startswith("--"):
        return None
    name = token[2:].split("=", 1)[0]
    return name or None


def _abbreviates(name: str, banned: Sequence[str]) -> str | None:
    """The banned option ``name`` abbreviates, or None.

    An abbreviation is a prefix of the name it stands for, so the test is which
    banned name starts with what was written. ``--output-indent`` abbreviates
    nothing here, because no banned name starts with it.
    """
    if len(name) < _MIN_OPTION_PREFIX_LENGTH:
        return None
    for option in banned:
        if option[2:].startswith(name):
            return option
    return None


def writing_argument(tail: Sequence[str]) -> str | None:
    """The option in ``tail`` that names an output file, as written, or None.

    Returning the spelling rather than the canonical name lets a refusal name
    what the caller wrote. A separate value (``--output <path>``) is left in the
    tail and does not have to be joined to the option for the option to be
    found. Abbreviations are matched, because git resolves any unambiguous
    prefix and would otherwise take the write through a spelling this check had
    never seen.
    """
    for token in tail:
        name = _long_option_name(token)
        if name is None:
            continue
        if _abbreviates(name, _OUTPUT_OPTIONS) is not None:
            return "--" + name
    return None


def output_refusal(verb: str, option: str) -> str:
    """The refusal naming an output-file option carried by a read verb.

    A read-only verb is forwarded without inspecting its repository, and it is
    forwarded with optional locks disabled, so nothing it does normally writes.
    An output option defeats that: the file it names is written wherever the
    path points, so the option is refused instead of the invocation. A spelling
    that only abbreviates a banned option is named together with the option it
    stands for, so the refusal is legible to a caller who wrote a prefix git
    would have resolved.
    """
    banned = _abbreviates(option[2:], _OUTPUT_OPTIONS)
    spelled = "" if banned == option else f" (abbreviating `{banned}`)"
    lines = [
        (
            f"refusing `git {verb}`: `{option}`{spelled} names an output file, "
            "so the command would write wherever the path points."
        ),
        (
            "A crew worker may write only inside its own worktree. Drop the "
            "option, or run a form that writes nothing."
        ),
    ]
    return "\n".join(lines)


# A ``diff`` naming ``--cached``, ``--staged`` or ``--no-index`` answers from
# the object store; every other ``diff`` compares the work tree against the
# index. No list of those spellings is kept here: a read that resolves to
# another repository is run against a private copy of its index, so which
# spellings refresh and which do not no longer decides whether a write can land,
# and nothing here has to keep up with git's option grammar.


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


def _resolved_target_lines(
    *,
    run_id: str,
    worktree: Path,
    worktree_git_dir: str | None,
    invocation_git_dir: str | None,
    invocation_toplevel: str | None,
) -> list[str]:
    """The run and resolved-target lines both refusals print.

    Both halves of the resolved target are named, because either one alone can
    be the mismatch: a foreign git dir, or the run's own git dir with a work
    tree pointing somewhere else.
    """
    return [
        f"  run:      {run_id}",
        f"  worktree: {worktree} (git dir {worktree_git_dir or 'unresolved'})",
        (
            f"  target:   git dir {invocation_git_dir or 'unresolved'}, "
            f"work tree {invocation_toplevel or 'unresolved'}"
        ),
    ]


def refusal_message(
    *,
    run_id: str,
    verb: str,
    worktree: Path,
    worktree_git_dir: str | None,
    invocation_git_dir: str | None,
    invocation_toplevel: str | None,
) -> str:
    """The refusal, naming the run, its worktree and the resolved target."""
    head = (
        f"refusing `git {verb}`: it runs against a repository that is not this "
        "run's worktree."
    )
    return "\n".join(
        [
            head,
            *_resolved_target_lines(
                run_id=run_id,
                worktree=worktree,
                worktree_git_dir=worktree_git_dir,
                invocation_git_dir=invocation_git_dir,
                invocation_toplevel=invocation_toplevel,
            ),
            (
                "A crew worker may run a mutating git verb only against its own "
                "worktree, so nothing was changed. Run the verb inside the worktree "
                "instead. A read aimed at another checkout is forwarded instead, "
                "with optional locks disabled and against a private copy of that "
                "checkout's index, so it answers without writing there."
            ),
        ]
    )


def main(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> int:
    """Run, or refuse, one git invocation.

    Returns the refusal status when the invocation is refused. Otherwise it
    replaces this process with the real git, except for a read whose repository
    has an index to isolate, which runs git as a child: the return is for the
    refusal, the missing-binary and the isolated-child cases.
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
    writing = writing_argument(tail)
    if writing is not None:
        print(output_refusal(verb or "git", writing), file=sys.stderr)
        return REFUSAL_STATUS
    guarded = mutating_verb(verb, tail)
    if guarded is None:
        return _forward_read(found, argv, prefix, run_id=run_id, environ=env, cwd=cwd)
    subject = guarded or "git"
    if not _safe_run_component(run_id):
        print(
            f"refusing `git {subject}`: {RUN_ID_ENV}={run_id!r} is not a single "
            "path component, so it cannot name this run's live pointer and the "
            "command changed nothing.",
            file=sys.stderr,
        )
        return REFUSAL_STATUS

    worktree = _run_worktree(run_id)
    if worktree is None:
        print(
            f"refusing `git {subject}`: {RUN_ID_ENV}={run_id} has no readable "
            "live pointer, so this run's worktree cannot be resolved and the "
            "command changed nothing.",
            file=sys.stderr,
        )
        return REFUSAL_STATUS

    worktree_git_dir = _run_git_dir(found, worktree, environ=env)
    invocation_git_dir, toplevel = _probe(found, prefix, environ=env, cwd=cwd)
    if not refuses(
        run_id=run_id,
        verb=subject,
        invocation_git_dir=invocation_git_dir,
        invocation_toplevel=toplevel,
        worktree=worktree,
        worktree_git_dir=worktree_git_dir,
    ):
        return _forward(found, argv, environ=env)
    print(
        refusal_message(
            run_id=run_id,
            verb=subject,
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


def _probe_index(
    git: str, prefix: Sequence[str], *, environ: Mapping[str, str], cwd: str | None
) -> Path | None:
    """The absolute path of the index the invocation's repository would use.

    ``--path-format=absolute`` is asked for because the answer has to be usable
    from the shim's own working directory, which is not the caller's: it is the
    caller's ``-C`` and environment the probe carries, but the copy is made and
    removed here. ``--path-format`` must precede ``--git-path`` for git to apply
    it to that query.
    """
    try:
        completed = subprocess.run(
            [
                git,
                *prefix,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "index",
            ],
            env=dict(environ),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    for line in completed.stdout.splitlines():
        if line.strip():
            return Path(line.strip())
    return None


def _aimed_elsewhere(
    git: str,
    prefix: Sequence[str],
    *,
    run_id: str,
    environ: Mapping[str, str],
    cwd: str | None,
) -> bool:
    """Whether the invocation resolves to a repository other than this run's.

    The same resolution the mutating check uses, so the two cannot disagree
    about where a command points. A run id that cannot be reduced to a pointer,
    or a pointer that names no worktree, counts as elsewhere: the run's own
    repository cannot be proved, and the caller gets the isolated copy rather
    than a write into the target's index.
    """
    if not _safe_run_component(run_id):
        return True
    worktree = _run_worktree(run_id)
    if worktree is None:
        return True
    worktree_git_dir = _run_git_dir(git, worktree, environ=environ)
    invocation_git_dir, toplevel = _probe(git, prefix, environ=environ, cwd=cwd)
    return refuses(
        run_id=run_id,
        verb="read",
        invocation_git_dir=invocation_git_dir,
        invocation_toplevel=toplevel,
        worktree=worktree,
        worktree_git_dir=worktree_git_dir,
    )


def _exit_status(returncode: int) -> int:
    """The status to report for a child git that ended on ``returncode``.

    A negative return code means the child was killed by that signal. Returning
    the negative number would be a status the shim invented, so the signal is
    re-raised on this process with its default disposition restored, which is
    what a directly forwarded invocation would have shown its caller. A signal
    that cannot be given a disposition (``SIGKILL``, ``SIGSTOP``) is left to the
    caller's own handling of the negative status.
    """
    if returncode < 0:
        signum = -returncode
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (OSError, ValueError):
            return returncode
        os.kill(os.getpid(), signum)
    return returncode


def _forward_isolated(
    git: str, argv: Sequence[str], *, environ: Mapping[str, str], index: Path
) -> int:
    """Run the real git as a child, pointed at a private copy of ``index``.

    A read verb changes no ref, but some read forms refresh the stat cache and
    record the refresh in the index of the repository they resolve to, and the
    answer they give is defined in terms of that refreshed index — so a read
    aimed at another checkout writes there. Copying the index first and pointing
    ``GIT_INDEX_FILE`` at the copy makes the refresh land on the copy for
    whichever option spelling reaches the form, because the decision is no
    longer about the spelling. The copy is removed once git has returned, which
    is why this case runs git as a child instead of replacing this process with
    it; a copy that cannot be made falls back to the plain forward.
    """
    environment = dict(environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    scratch = tempfile.mkdtemp(prefix="reckon-git-shim-")
    copy = Path(scratch) / "index"
    try:
        shutil.copyfile(index, copy)
    except OSError:
        shutil.rmtree(scratch, ignore_errors=True)
        return _forward(git, argv, environ=environ)
    environment["GIT_INDEX_FILE"] = str(copy)
    try:
        completed = subprocess.run([git, *argv], env=environment, check=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return _exit_status(completed.returncode)


def _forward_read(
    git: str,
    argv: Sequence[str],
    prefix: Sequence[str],
    *,
    run_id: str,
    environ: Mapping[str, str],
    cwd: str | None,
) -> int:
    """Forward a read verb, isolating the index when it points elsewhere.

    Isolation is skipped in two cases. A repository with no index file (bare, or
    never read from) has nothing to write, and ``GIT_INDEX_FILE`` naming a file
    that does not exist would make git answer from an empty index — a different
    answer, not a safer one. A read that resolves to this run's own worktree is
    forwarded with the index in place: the worker may write its own index, and
    pointing at a copy would make an invocation that asks git for the index path
    answer with the copy's path instead.
    """
    index = _probe_index(git, prefix, environ=environ, cwd=cwd)
    if index is None or not index.is_file():
        return _forward(git, argv, environ=environ)
    if not _aimed_elsewhere(git, prefix, run_id=run_id, environ=environ, cwd=cwd):
        return _forward(git, argv, environ=environ)
    return _forward_isolated(git, argv, environ=environ, index=index)
