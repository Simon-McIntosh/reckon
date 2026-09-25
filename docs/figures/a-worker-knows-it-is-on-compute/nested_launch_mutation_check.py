"""Reproduce a declared mutation's red log for the nested-launch refusal.

The refusal is the whole point of the srun/sbatch/salloc shims, and the checks
it rests on are the in-allocation gate and the lookup that finds the real
binary. This script applies one declared mutation from the test file and runs a
named case against it, so the log shows the case that held against the real
module failing against the mutant.

The mutation is loaded as ``reckon.nested_launch`` for the shim subprocesses by
the test file's own fact-injection seam: ``NESTED_LAUNCH_TEST_MODULE`` names
the module to load, and the test file's ``launch_env`` carries it into every
shim. Nothing here edits the reviewed module or test — the mutant is written
beside the run.

The first line of the output repeats the mutation's declaration verbatim, so the
log is checkable against the declaration it is a control for.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TEST = "test_plain_srun_inside_the_allocation_is_refused"

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[3]
VENV_PYTHON = WORKTREE / ".venv" / "bin" / "python"
TEST_FILE = WORKTREE / "tests" / "test_nested_launch.py"

sys.path.insert(0, str(WORKTREE))

from tests.test_nested_launch import (  # noqa: E402
    DECLARED_MUTATION,
    load_declared_mutant,
    mutation_names,
)


def main(argv: list[str]) -> int:
    declaration = argv[0] if argv else DECLARED_MUTATION
    test = argv[1] if len(argv) > 1 else DEFAULT_TEST
    print(declaration)
    revision = subprocess.run(
        ["git", "-C", str(WORKTREE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"tree: {WORKTREE}")
    print(f"revision: {revision}")
    print(f"command: {VENV_PYTHON.name} -m pytest {TEST_FILE}::{test}")
    print(f"declared mutations in the test file: {mutation_names()}")
    if declaration not in mutation_names():
        raise SystemExit(f"the test file declares no mutation {declaration!r}")
    with tempfile.TemporaryDirectory() as scratch:
        mutant = load_declared_mutant(Path(scratch), declaration)
        print(f"mutated module: {mutant}")
        env = dict(os.environ)
        env["NESTED_LAUNCH_TEST_MODULE"] = str(mutant)
        env["PYTHONPATH"] = str(WORKTREE)
        env.pop("NESTED_LAUNCH_TEST_FACTS", None)
        result = subprocess.run(
            [
                str(VENV_PYTHON),
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                f"{TEST_FILE}::{test}",
            ],
            cwd=str(WORKTREE),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout, end="")
        if result.stderr.strip():
            print(result.stderr, end="")
        if result.returncode == 0:
            raise SystemExit("the test survived the mutation")
        print(
            "the test failed under the mutation, as declared "
            f"(pytest exit {result.returncode})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
