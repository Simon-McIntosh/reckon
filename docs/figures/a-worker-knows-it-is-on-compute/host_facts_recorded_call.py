"""Record one live call of the host-facts probe in the job running it.

The probe's fourth case is a reading of the real host rather than a fixture:
this process is inside the held allocation, so ``in_allocation`` must read
true and every fact must name the file or helper it was read from. The log
this writes is the evidence that the real path -- not only the fixture path --
answers correctly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKTREE = HERE.parents[3]

sys.path.insert(0, str(WORKTREE))

from reckon import host as host_module  # noqa: E402


def main() -> int:
    print(f"tree: {WORKTREE}")
    print(f"probe module under test: {host_module.__file__}")
    print(
        f"job env: SLURM_JOB_ID={os.environ.get('SLURM_JOB_ID', 'unset')} "
        f"SLURMD_NODENAME={os.environ.get('SLURMD_NODENAME', 'unset')}"
    )
    facts = host_module.host_facts()
    print(f"in_allocation: {facts.in_allocation}")
    print(f"reason: {facts.reason!r}")
    for key, entry in facts.facts().items():
        print(f"{key}: {entry['value']!r} <- {entry['source']}")
    assert facts.in_allocation is True, (
        "this process runs inside the allocation, so the probe must read true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
