"""Every module under ``reckon/`` imports first in a fresh interpreter.

A caller that imports the module it needs, rather than the one that happens to
load first, must succeed. A module-level import that closes a cycle makes that
false for a whole set of modules at once, and because the failure depends on
which module the interpreter reaches first, only a fresh interpreter per module
shows it.

Each module is imported in its own subprocess with the repository root placed
first on ``sys.path``. The subprocess prints the imported module's resolved
``__file__`` and asserts it lies under the root, so a run cannot pass by
importing a copy installed elsewhere. The subprocess runs with a temporary home
and configuration home, so an import that reached outside the tree would be
visible rather than silently mutating the operator's state.
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
IMPORT_WORKERS = 12


def _module_names() -> list[str]:
    names: list[str] = []
    for path in (REPO_ROOT / "reckon").rglob("*.py"):
        parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.append(".".join(parts))
    return sorted(set(names))


MODULES = _module_names()


def _import_first(root: Path, module: str, home: Path) -> tuple[int, str]:
    program = (
        "import importlib, pathlib, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        f"_m = importlib.import_module({module!r})\n"
        "_p = pathlib.Path(_m.__file__).resolve()\n"
        f"assert _p.is_relative_to(pathlib.Path({str(root)!r}).resolve()), _p\n"
        "print(_p)\n"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_STATE_HOME=str(home / "state"),
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        cwd=str(home),
        env=env,
        check=False,
    )
    detail = (done.stdout + done.stderr).strip().splitlines()
    return done.returncode, detail[-1] if detail else ""


@pytest.fixture(scope="session")
def import_results(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, tuple[int, str]]:
    home = tmp_path_factory.mktemp("module-import-home")
    results: dict[str, tuple[int, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=IMPORT_WORKERS) as pool:
        pending = {
            pool.submit(_import_first, REPO_ROOT, name, home): name for name in MODULES
        }
        for future in concurrent.futures.as_completed(pending):
            results[pending[future]] = future.result()
    leftover = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert not leftover, f"an import wrote outside the repository: {leftover}"
    return results


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_alone(
    import_results: dict[str, tuple[int, str]], module: str
) -> None:
    returncode, detail = import_results[module]
    assert returncode == 0, f"importing {module} first failed: {detail}"
