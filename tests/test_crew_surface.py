from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "crew-public-surface.txt"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MODULES = (
    "node",
    "routing",
    "runs",
    "prompts",
    "dispatch",
    "promotion",
    "recovery",
    "reports",
    "summary",
    "ticker",
)

# Runs in a fresh interpreter, printing the facade's export names one per line.
# A submodule binds onto this package when anything imports it, and a bound
# submodule is not an export, so module-valued crew submodules are dropped.
_MEASURE = """\
import types

import reckon.crew as crew

prefix = crew.__name__ + "."
print(
    "\\n".join(
        sorted(
            name
            for name in dir(crew)
            if not name.startswith("_")
            and not (
                isinstance(getattr(crew, name), types.ModuleType)
                and getattr(crew, name).__name__.startswith(prefix)
            )
        )
    )
)
"""


def _fresh_surface() -> set[str]:
    """Measure the facade's export surface where no peer test has imported into it.

    Importing a submodule binds it onto its parent package, so a test that
    imports ``reckon.crew.<concern>`` adds that name to ``dir(reckon.crew)`` for
    every test collected after it in the same process. The snapshot describes
    the facade's exports, not the collection order, so it is read in an
    interpreter that has none of that history. ``PYTHONPATH`` names this
    checkout's root first, so the child resolves the tree under test rather
    than an installed copy of the package.
    """
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(PACKAGE_ROOT) + (
        os.pathsep + inherited if inherited else ""
    )
    result = subprocess.run(
        [sys.executable, "-c", _MEASURE],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return set(result.stdout.splitlines())


def test_public_surface_matches_snapshot() -> None:
    expected = set(FIXTURE.read_text().splitlines())

    assert _fresh_surface() == expected


def test_concern_modules_import_in_fresh_interpreters() -> None:
    for module in MODULES:
        result = subprocess.run(
            [sys.executable, "-c", f"import reckon.crew.{module}"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
