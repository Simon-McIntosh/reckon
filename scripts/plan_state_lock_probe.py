"""Hold a plan-state resource lock on one node and probe it from another.

Run two instances concurrently on two nodes, against one temporary docs tree
on shared storage. The second instance asks for the same plan-state resource
lock through the production lock code and reports whether the holder excluded
it, printing its own hostname so two hostnames can be compared.

    python scripts/plan_state_lock_probe.py hold --docs-dir T \
        --project probe --resource-id r1 --ready R --release X --hold-seconds 120
    python scripts/plan_state_lock_probe.py try --docs-dir T \
        --project probe --resource-id r1

``pair`` drives both from one ``srun -n 2`` step, one rank per node.

The tree must live on storage both nodes open. A node-local tree makes the
second node's "refused" an absent path, so every report names the lock file
and whether it exists, and a refusal with no lock file is a failure.
"""

from __future__ import annotations

import argparse
import inspect
import os
import socket
import sys
import threading
import time
from pathlib import Path

from reckon import project_state

try:
    from reckon.node_exclusive_lock import LockHeld
except ImportError:  # a tree whose lock predates the refusal path

    class LockHeld(RuntimeError):  # noqa: N818 - mirror of the lock's own name
        pass


PROJECT = "probe"
RESOURCE_TYPE = "sprint"


def lock_path(docs_dir: Path, resource_id: str) -> Path:
    return (
        docs_dir / ".reckon" / "locks" / f"{PROJECT}-{RESOURCE_TYPE}-{resource_id}.lock"
    )


def report(role: str, state: str, path: Path) -> None:
    print(f"HOST={socket.gethostname()}")
    print(f"ROLE={role}")
    print(f"LOCK_PATH={path}")
    print(f"LOCK_STATE={state}")
    print(f"LOCK_FILE_PRESENT={'yes' if path.exists() else 'no'}")
    sys.stdout.flush()


def hold(args) -> int:
    path = lock_path(args.docs_dir, args.resource_id)
    with project_state._resource_lock(
        args.docs_dir, PROJECT, RESOURCE_TYPE, args.resource_id
    ):
        report("hold", "acquired", path)
        args.ready.parent.mkdir(parents=True, exist_ok=True)
        args.ready.write_text(f"{socket.gethostname()}\n", encoding="utf-8")
        deadline = time.monotonic() + args.hold_seconds
        while time.monotonic() < deadline:
            if args.release.exists():
                break
            time.sleep(0.2)
        print("HOLD=released")
    return 0


def attempt(args) -> int:
    path = lock_path(args.docs_dir, args.resource_id)
    if "blocking" in inspect.signature(project_state._resource_lock).parameters:
        try:
            with project_state._resource_lock(
                args.docs_dir,
                PROJECT,
                RESOURCE_TYPE,
                args.resource_id,
                blocking=False,
            ):
                report("try", "acquired", path)
                return 0
        except LockHeld:
            report("try", "refused", path)
            return 0 if path.exists() else 3
    # The previous primitive has no refusal path: ask for it in blocking mode
    # and report a lock we did not get within the probe window as refused.
    outcome: dict[str, str] = {}

    def take() -> None:
        with project_state._resource_lock(
            args.docs_dir, PROJECT, RESOURCE_TYPE, args.resource_id
        ):
            outcome["state"] = "acquired"

    worker = threading.Thread(target=take, daemon=True)
    worker.start()
    worker.join(timeout=args.hold_seconds)
    if outcome.get("state") == "acquired":
        report("try", "acquired", path)
        return 0
    report("try", "refused", path)
    return 0 if path.exists() else 3


def pair(args) -> int:
    rank = int(os.environ.get("SLURM_PROCID", "0"))
    nodes = int(os.environ.get("SLURM_NNODES", "1"))
    args.docs_dir.mkdir(parents=True, exist_ok=True)
    if rank == 0:
        return hold(args)
    if rank != 1:
        return 0
    deadline = time.monotonic() + args.hold_seconds
    while not args.ready.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not args.ready.exists():
        print("VERDICT=no-holder")
        return 4
    code = attempt(args)
    args.release.write_text(f"{socket.gethostname()}\n", encoding="utf-8")
    print(f"NODES={nodes}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["hold", "try", "pair"])
    parser.add_argument("--docs-dir", type=Path, required=True)
    parser.add_argument("--resource-id", default="probe")
    parser.add_argument("--ready", type=Path, default=Path("/dev/null"))
    parser.add_argument("--release", type=Path, default=Path("/dev/null"))
    parser.add_argument("--hold-seconds", type=float, default=120.0)
    args = parser.parse_args(argv)
    args.docs_dir = args.docs_dir.resolve()
    if args.mode == "hold":
        return hold(args)
    if args.mode == "try":
        return attempt(args)
    return pair(args)


if __name__ == "__main__":
    raise SystemExit(main())
