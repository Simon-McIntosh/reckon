"""One function names the checkout interpreter for both sides that need it.

A hook script that runs before any reckon import resolves the checkout's own
interpreter through ``interpreter_bootstrap``; the installer composes the same
path into the commands it writes into a harness settings file. If the two
derive the venv layout separately they can drift, and a command then names an
interpreter that does not exist or belongs to another checkout. These tests
build a synthetic checkout and hold both sides to the one resolver.
"""

from __future__ import annotations

import re
from pathlib import Path

from reckon.hooks import install, interpreter_bootstrap

HOOKS_DIR = Path(install.__file__).resolve().parent

# The layout in its composed source form: the quoted directory names joined by
# ``/``. Prose that merely spells the path out in a docstring carries no quoted
# separators, so it is not mistaken for a derivation.
_COMPOSES_LAYOUT = re.compile(
    r"""["']\.venv["']\s*/\s*["']bin["']\s*/\s*["']python["']"""
)


def _make_checkout(root: Path) -> Path:
    """Build a checkout layout and return the interpreter it should name."""
    hooks = root / "reckon" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "coordinator_obligations.py").write_text("#!/usr/bin/env python3\n")
    (hooks / "interpreter_bootstrap.py").write_bytes(
        (HOOKS_DIR / "interpreter_bootstrap.py").read_bytes()
    )
    interpreter = root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/usr/bin/env python3\n")
    return interpreter


def test_installer_and_bootstrap_name_the_same_interpreter(tmp_path: Path) -> None:
    """For one synthetic checkout, both sides resolve the same interpreter."""
    checkout = tmp_path / "checkout"
    interpreter = _make_checkout(checkout)
    script = checkout / "reckon" / "hooks" / "coordinator_obligations.py"

    assert install.interpreter_path(script) == interpreter
    assert interpreter_bootstrap.checkout_interpreter(script) == interpreter
    assert install.interpreter_path(
        script
    ) == interpreter_bootstrap.checkout_interpreter(script)


def test_installer_default_names_the_same_interpreter_as_the_bootstrap() -> None:
    """With no script named, the installer still agrees with the resolver."""
    assert install.interpreter_path() == interpreter_bootstrap.checkout_interpreter(
        Path(install.__file__)
    )
    assert (
        install.interpreter_path() == HOOKS_DIR.parents[1] / ".venv" / "bin" / "python"
    )


def test_only_the_bootstrap_composes_the_venv_layout() -> None:
    """No module under reckon/hooks but the bootstrap builds the path itself."""
    holders = sorted(
        path.name
        for path in HOOKS_DIR.glob("*.py")
        if _COMPOSES_LAYOUT.search(path.read_text())
    )
    assert holders == ["interpreter_bootstrap.py"]


def test_checkout_interpreter_predicate_agrees_with_the_resolver(
    tmp_path: Path,
) -> None:
    """The predicate recognises exactly what the resolver names."""
    checkout = tmp_path / "checkout"
    interpreter = _make_checkout(checkout)
    script = checkout / "reckon" / "hooks" / "coordinator_obligations.py"

    assert interpreter_bootstrap.is_checkout_interpreter(interpreter)
    assert interpreter_bootstrap.is_checkout_interpreter(
        interpreter_bootstrap.checkout_interpreter(script)
    )
    assert not interpreter_bootstrap.is_checkout_interpreter(
        checkout / ".venv" / "bin" / "python3"
    )
    assert not interpreter_bootstrap.is_checkout_interpreter(
        tmp_path / "system" / "python3"
    )
