"""Run a reckon-importing hook with this checkout's Python interpreter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 12)
_ATTEMPT_ENV = "RECKON_HOOK_REEXEC_ATTEMPT"


def checkout_interpreter(script: str | os.PathLike[str]) -> Path:
    """Return the checkout interpreter that runs a hook script.

    The venv layout is a hook script's grandparent directory, and it is named
    only here: the installer and this bootstrap both call this function, so
    they cannot name different interpreters for the same checkout. The module
    stays stdlib-only, so this is importable before any reckon import.
    """
    return Path(script).resolve().parents[2] / ".venv" / "bin" / "python"


def is_checkout_interpreter(path: str | os.PathLike[str]) -> bool:
    """Return whether a path has the layout this module resolves for a checkout."""
    candidate = Path(path)
    return (
        candidate.name == "python"
        and candidate.parent.name == "bin"
        and candidate.parent.parent.name == ".venv"
    )


def ensure_interpreter(script: str) -> str | None:
    """Re-execute an old interpreter, or return why that was impossible."""
    if sys.version_info >= MINIMUM_PYTHON:
        return None
    script_path = Path(script).resolve()
    interpreter = checkout_interpreter(script_path)
    if os.environ.get(_ATTEMPT_ENV) == str(script_path):
        return f"reckon hook: {interpreter} is below Python 3.12; cannot re-execute"
    if not interpreter.is_file():
        return f"reckon hook: checkout interpreter is missing: {interpreter}"
    try:
        os.environ[_ATTEMPT_ENV] = str(script_path)
        os.execv(  # noqa: S606 - replace this hook with the checkout interpreter
            str(interpreter), [str(interpreter), str(script_path), *sys.argv[1:]]
        )
    except OSError as exc:
        return f"reckon hook: cannot run checkout interpreter {interpreter}: {exc}"
    return None
