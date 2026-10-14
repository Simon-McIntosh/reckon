"""One check of whether a moved checkout may be loaded.

A code stamp that has moved says the checkout changed on disk; it does not say
the change is fit to run. Two readers must reach the same answer about that
tree. A follower or producer replaces its own image only onto a tree it can
trust, and the MCP's stale response tells its caller to reconnect onto the
checkout only when that same tree can be trusted. Held as separate probes the
two disagreed: on a half-edited tree the MCP pointed its caller at code the
followers refused to load, which is the tree that took five producers down.

The check has two conditions, in order. The tree must be clean under ``reckon/``
against ``HEAD``: a rename applied at a call site and not at its definition
imports cleanly and fails only at first use, so git is asked which tracked files
differ rather than the module being run. And every tracked module must import:
the probe compiles each one and imports it in a throwaway interpreter, because
the running process is the wrong place to discover that the new code does not
load.

This module holds the only import probe in the source. Both readers call
:func:`moved_checkout_is_trustworthy` rather than carrying a probe of their own.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from reckon.crew.obligation_snapshot import FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS

# The one proof that the checkout's current code loads. A fresh interpreter is
# required: the running process is the wrong place to discover a broken module,
# because the check must not import the new code into the image it is judging.
# Every tracked module is compiled first, so a syntax error anywhere is reported
# as itself, and then imported, so a rename bound at a call site is caught at
# load rather than at first use.
IMPORT_PROBE = (
    "import importlib, pathlib, sys\n"
    "root = pathlib.Path(sys.argv[1]).resolve()\n"
    "sys.path.insert(0, str(root))\n"
    "package = root / 'reckon'\n"
    "sources = sorted(package.rglob('*.py'))\n"
    "for path in sources:\n"
    "    compile(path.read_bytes(), str(path), 'exec')\n"
    "for path in sources:\n"
    "    parts = list(path.relative_to(root).with_suffix('').parts)\n"
    "    if parts and parts[-1] == '__init__':\n"
    "        parts = parts[:-1]\n"
    "    if not parts or parts[-1] == '__main__':\n"
    "        continue\n"
    "    importlib.import_module('.'.join(parts))\n"
)

IMPORT_PROBE_TIMEOUT = FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS


@dataclass(frozen=True)
class CheckoutTrust:
    """The answer: ``trusted``, and when it is false, which condition refused.

    ``dirty`` distinguishes the two refusals so a caller can say which one it
    is: true means a tracked file differs from ``HEAD`` and ``detail`` names the
    files; false with ``trusted`` false means the import proof refused and
    ``detail`` names the reason.
    """

    trusted: bool
    dirty: bool
    detail: str


def tracked_reckon_tree_is_dirty(root: Path) -> tuple[bool, str]:
    """Whether any tracked file under ``reckon/`` differs from ``HEAD``.

    A tree a reloader reads mid-edit can import cleanly and still fail at its
    first use: a rename half-applied binds a name the import proof never
    reaches. The committed image is what a replacement should run, so git is
    asked which tracked files differ from it rather than the answer being
    re-derived here.

    A tree git cannot read -- not a checkout, or no git on ``PATH`` -- is
    reported clean: the check has no opinion there and the import proof is still
    a guard, so a reader that cannot see a repository keeps its old behaviour
    rather than deferring every load forever. Untracked files are excluded by
    git itself; a newly written module that fails to import is caught by the
    import proof.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--",
                "reckon",
            ],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return False, ""
    entries = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not entries:
        return False, ""
    return True, ", ".join(entries)


def reckon_imports(root: Path) -> tuple[bool, str]:
    """Whether the checkout's current code loads in a throwaway interpreter."""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", IMPORT_PROBE, str(root)],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode == 0:
        return True, ""
    lines = (completed.stderr or completed.stdout or "").strip().splitlines()
    return False, (lines[-1] if lines else f"exit {completed.returncode}")


def moved_checkout_is_trustworthy(root: Path) -> CheckoutTrust:
    """Whether a checkout whose code has moved may be loaded.

    Clean under ``reckon/`` against ``HEAD`` and importable: both, and the tree
    is trusted. The dirty check is asked first and short-circuits -- a dirty
    tree is not fit to run whatever its imports say, and the import proof, which
    loads every module, is the expensive half.
    """
    dirty, detail = tracked_reckon_tree_is_dirty(root)
    if dirty:
        return CheckoutTrust(trusted=False, dirty=True, detail=detail)
    imports, reason = reckon_imports(root)
    if not imports:
        return CheckoutTrust(trusted=False, dirty=False, detail=reason)
    return CheckoutTrust(trusted=True, dirty=False, detail="")
