"""Reproduce the declared mutation's red log for the host-facts probe.

The probe's whole value is what the mutation removes: it replaces the cgroup
check with a ``SLURM_JOB_ID`` environment check, and the environment-only case
— job id set, cgroup naming no job — is then read as inside the allocation.
This script applies that mutation in memory, measures the environment-only case
against the mutated module, and fails: the assertion that holds for the
reviewed module is the one the mutant breaks.

The first line of the output repeats the declaration verbatim, so the log is
checkable against the declaration it is a control for. The declaration is
written here in full so the log's first line does not depend on the file under
test; its copy of the same string is printed and compared below, and the run
refuses to continue if the two differ.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

DECLARATION = "replace the cgroup check with a SLURM_JOB_ID environment check"

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[3]

sys.path.insert(0, str(WORKTREE))

from reckon import host as host_module  # noqa: E402
from tests.test_host_facts import (  # noqa: E402
    DECLARED_MUTATION,
    environment_only_case,
    load_declared_mutant,
)


def main() -> int:
    print(DECLARATION)
    revision = subprocess.run(
        ["git", "-C", str(WORKTREE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"tree: {WORKTREE}")
    print(f"revision: {revision}")
    print(f"command: python {HERE.name}  (PYTHONPATH={WORKTREE})")
    print(f"probe module under test: {host_module.__file__}")
    print(f"test file's declaration: {DECLARED_MUTATION}")
    if DECLARED_MUTATION != DECLARATION:
        raise SystemExit("the test file declares a different mutation")
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        mutant = load_declared_mutant(scratch_path)
        print(f"mutated module: {mutant.__file__}")
        facts = environment_only_case(mutant, scratch_path)
        print(f"mutated in_allocation: {facts.in_allocation}")
        assert facts.in_allocation is False, (
            "the environment-only case reads inside the allocation under the "
            "mutated probe, so the cgroup check is what makes it false"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
