"""Find the real binary behind a reckon shim, and never another shim.

A shim forwards to the tool it stands in for, which it finds on ``PATH``.
Dropping the shim's own directory from that search is not enough to keep it
from finding a shim: every checkout of reckon carries its own shim directories
— the main checkout, a worker's worktree, a base-revision copy a gate runs its
suite from — and a ``PATH`` holding two of them makes each shim resolve to the
other. The git shim asks the real git a question by running it as a child, so
two copies on one ``PATH`` form a chain in which every level waits on the next
and a fresh interpreter starts at the leaf, several a second, until the host
runs out of memory. Killing the top of the chain does not stop it, because the
growth is at the far end.

So a candidate is judged by what it is rather than where it sits: every shim
file carries :data:`SHIM_MARKER` in its header, and a candidate carrying it is
skipped wherever on ``PATH`` it is found. The same test lets a launch drop an
inherited shim directory before it prepends its own, so a worker's ``PATH``
carries one set of shims.

:data:`SHIM_DEPTH_ENV` bounds whatever loop the lookup cannot see. Each shim
passes it one higher to the binary it runs, and a shim invoked at
:data:`MAX_SHIM_DEPTH` refuses instead of forwarding, so a cycle ends within a
handful of processes rather than when memory does. A git hook that runs git is
the deepest legitimate nesting, and it stays well below the bound.

Stdlib only: the shims import this before anything else of the package.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

# The token every shim file carries in its header. A real binary never holds it
# in its first bytes, so its presence is what identifies a shim on ``PATH``.
SHIM_MARKER = b"reckon-shim"

# How much of a candidate is read to look for the marker: the shim files state
# it on their second line, well inside this.
_HEADER_BYTES = 512

# The nesting count each shim passes to what it runs.
SHIM_DEPTH_ENV = "RECKON_SHIM_DEPTH"

# The count at which a shim refuses rather than forwards.
MAX_SHIM_DEPTH = 8


def is_shim(path: str | os.PathLike[str]) -> bool:
    """Whether the file at ``path`` is a reckon shim."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_HEADER_BYTES)
    except OSError:
        return False
    return SHIM_MARKER in head


def real_executable(
    name: str, path: str, *, skipped: Iterable[str | os.PathLike[str]] = ()
) -> str | None:
    """The first executable ``name`` on ``path`` that is not a reckon shim.

    ``skipped`` names directories left out of the search by resolved path, so a
    ``PATH`` entry reaching one through a symlink is left out too. Empty entries
    are ignored rather than read as the working directory.
    """
    left_out = {os.path.realpath(os.fspath(directory)) for directory in skipped}
    for entry in str(path).split(os.pathsep):
        if not entry or os.path.realpath(entry) in left_out:
            continue
        candidate = os.path.join(entry, name)
        if (
            os.path.isfile(candidate)
            and os.access(candidate, os.X_OK)
            and not is_shim(candidate)
        ):
            return candidate
    return None


def holds_shim(directory: str | os.PathLike[str], names: Iterable[str]) -> bool:
    """Whether ``directory`` holds a reckon shim for any of ``names``."""
    return any(is_shim(os.path.join(directory, name)) for name in names)


def shim_depth(environ: Mapping[str, str]) -> int:
    """The nesting count ``environ`` carries; absent or unreadable is zero."""
    try:
        return max(0, int(str(environ.get(SHIM_DEPTH_ENV) or "0")))
    except ValueError:
        return 0


def deeper(environ: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``environ`` with the nesting count one higher."""
    environment = dict(environ)
    environment[SHIM_DEPTH_ENV] = str(shim_depth(environ) + 1)
    return environment


def depth_refusal(tool: str, environ: Mapping[str, str]) -> str | None:
    """The refusal a shim prints at the depth bound, or None below it."""
    depth = shim_depth(environ)
    if depth < MAX_SHIM_DEPTH:
        return None
    return (
        f"{tool} shim: refusing at {SHIM_DEPTH_ENV}={depth}: this invocation is "
        f"nested {depth} shims deep, so a shim on PATH is resolving to another "
        "shim instead of the real tool, and forwarding would only deepen the "
        "loop. Nothing ran. Check PATH for more than one reckon shim directory."
    )
