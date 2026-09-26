"""Refuse, at execution time, a mutating git verb aimed at another checkout.

A guard that reads a command's *text* cannot be made complete: a shell spelling
it does not model reaches the real git unexamined, and a wrapper inside another
process (`env GIT_DIR=... git ...`, a `cd` earlier in a script, a shell function)
is invisible to a parser reading one string. This module is the decision the
``git`` shim under :mod:`reckon.worker_shims` composes its behaviour from, and
it does not parse the command at all: it asks the real git which repository one
invocation resolves to, under that invocation's own environment, working
directory and ``-C``, and refuses a mutating verb unless that repository is the
run's worktree — or the git dir of that worktree.

Scope comes from ``RECKON_RUN_ID``, the identity dispatch exports into every
worker; a coordinator session carries no run id and the shim is transparent for
it. The run's worktree is read from the run's live pointer, exactly as the
pre-tool-use guard reads it. Read-only verbs, and any verb outside the mutating
set, are forwarded unchanged. A forwarded invocation replaces this process with
the real git resolved with the shim's own directory removed from ``PATH``, so
the shim never finds itself, and the exit status and signals are the real
tool's.

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

# Git's global options that consume the token after them. Everything before the
# subcommand is forwarded verbatim, so this list only has to find the verb.
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

# Options that carry their value attached (`--git-dir=<path>`, `-C<path>`).
_ATTACHED_PREFIXES = ("-C", "--git-dir=", "--work-tree=", "--config-env=")

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


def _split_verb(argv: Sequence[str]) -> tuple[list[str], str]:
    """Return the global-option prefix and the subcommand of one git argv.

    A token that is not an option ends the prefix: everything before it is the
    global options git accepts, and the token itself is the verb (an empty
    string when the argv names none).
    """
    prefix: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if not argument.startswith("-"):
            return prefix, argument
        prefix.append(argument)
        if argument in _VALUE_OPTIONS and index + 1 < len(argv):
            prefix.append(argv[index + 1])
            index += 2
            continue
        index += 1
    return prefix, ""


def _aliases(prefix: Sequence[str]) -> dict[str, str]:
    """Alias definitions the invocation declares with ``-c alias.<name>=...``."""
    aliases: dict[str, str] = {}
    for index, argument in enumerate(prefix):
        entry = ""
        if argument == "-c" and index + 1 < len(prefix):
            entry = prefix[index + 1]
        elif argument.startswith("-c") and len(argument) > 2:
            entry = argument[2:]
        if not entry.startswith("alias."):
            continue
        name, separator, value = entry[len("alias.") :].partition("=")
        if separator and name:
            aliases[name] = value
    return aliases


def mutating_verb(prefix: Sequence[str], verb: str) -> str | None:
    """The mutating verb this invocation runs, or None when it runs none.

    A verb the invocation aliases is expanded through the invocation's own
    ``-c alias.<name>=...`` settings, so an alias that expands to a mutating
    verb is refused rather than read as an unknown name.
    """
    if not verb:
        return None
    if verb in MUTATING_VERBS:
        return verb
    expansion = _aliases(prefix).get(verb)
    if expansion:
        first = _split_verb(expansion.split())[1]
        if first in MUTATING_VERBS:
            return first
    return None


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

    The run id is reduced to its basename before it names a file, so a value
    carrying a separator cannot reach outside the live directory.
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

    The repository that decides it is the git dir, because that is where a
    mutating verb's HEAD, refs and index live; naming a matching ``--work-tree``
    while pointing ``--git-dir`` at another checkout is not the run's own
    repository, and it is the git dir alone that is compared when both resolve.
    """
    if worktree_git_dir is None:
        return True
    if _same(invocation_git_dir, worktree_git_dir):
        return False
    # A bare or otherwise top-level-only report is accepted only when the git
    # dir itself could not be resolved, which keeps an invocation that mutates
    # another checkout's HEAD from being allowed on a work-tree name alone.
    return not (
        invocation_git_dir is None and _same(invocation_toplevel, str(worktree))
    )


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
    target = invocation_git_dir or invocation_toplevel or "an unresolved repository"
    head = (
        f"refusing `git {verb}`: it runs against a repository that is not this "
        "run's worktree."
    )
    lines = [
        head,
        f"  run:      {run_id}",
        f"  worktree: {worktree} (git dir {worktree_git_dir or 'unresolved'})",
        f"  target:   {target}",
        (
            "A crew worker may run a mutating git verb only against its own "
            "worktree, so nothing was changed. Run the verb inside the worktree "
            "instead. Read-only verbs (status, log, diff, show, rev-parse, grep) "
            "pass everywhere."
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
    prefix, verb = _split_verb(argv)
    if not run_id:
        return _forward(found, argv)
    guarded = mutating_verb(prefix, verb)
    if guarded is None:
        return _forward(found, argv)

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
        return _forward(found, argv)
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


def _forward(git: str, argv: Sequence[str]) -> int:
    """Replace this process with the real git, argv unchanged."""
    # The binary is resolved by absolute path and the argv is already split, so
    # there is no shell to route through; exec forwards signals and the exit
    # status unchanged.
    os.execv(git, ["git", *argv])  # noqa: S606
    raise AssertionError("os.execv returned; a forwarded invocation cannot continue.")
