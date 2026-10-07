"""One obligation-snapshot module instance per process, whichever loader runs.

The prompt hook and the cli load ``reckon.crew.obligation_snapshot`` by file
path so neither pulls the crew facade and every concern module with it. A load
that replaces an instance the process already holds splits the process: the
producer reaches the module through ``reckon.crew``, keeps its per-project
sweep memory on the instance it reaches, and publishes where a caller holding
the imported module cannot see it. These cases pin the identity every surface
must agree on.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_MODULE = "reckon.crew.obligation_snapshot"


def test_both_loaders_keep_the_one_imported_snapshot_module() -> None:
    """Registration, package attribute and both loaders are one object."""
    imported = importlib.import_module(SNAPSHOT_MODULE)
    from reckon.cli import _snapshot_module
    from reckon.hooks.coordinator_obligations import snapshot_module

    hook_view = snapshot_module()
    cli_view = _snapshot_module()

    assert sys.modules[SNAPSHOT_MODULE] is imported, (
        "a loader replaced the registered module with a second instance"
    )
    assert hook_view is imported, "the hook's loader returned a second instance"
    assert cli_view is imported, "the cli's loader returned a second instance"
    # The producer reaches the module the way runs.py does, through the
    # package. That route resolves to the registered instance whether or not
    # the package attribute is bound yet: a module a loader registered by path
    # before the facade was imported is never bound onto it, which makes the
    # attribute's presence a matter of load order. A bound attribute must still
    # name the registered module, since a second instance there would split the
    # process.
    from reckon.crew import obligation_snapshot as producer_view

    assert producer_view is imported, "the producer's route reaches a second instance"
    bound = getattr(sys.modules["reckon.crew"], "obligation_snapshot", imported)
    assert bound is imported, "the package attribute names a second instance"


def test_the_hook_loads_by_path_without_the_crew_facade() -> None:
    """With nothing loaded yet, the hook's reader arrives without the facade."""
    probe = (
        "import sys; "
        "from reckon.hooks.coordinator_obligations import snapshot_module; "
        "module = snapshot_module(); "
        "print(module.__name__); "
        "print('reckon.crew' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT)},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        SNAPSHOT_MODULE,
        "False",
    ], completed.stdout
