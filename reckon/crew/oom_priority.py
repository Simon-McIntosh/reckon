"""Which side of an out-of-memory kill a reckon process is on.

The fleet job runs every one of its processes in a single memory cgroup, and
when that cgroup runs out of memory the kernel kills the process with the
highest badness -- which, with no adjustment, is simply the largest. The
coordination services are long-lived and hold the largest resident sets in the
job (the station and the admit service have both been killed at several GiB),
while a worker is small and replaceable. Without a declared ordering the
kernel therefore kills the process the deliverable needs alive, and every kill
drops whatever pass that process was in.

The kernel reads a process's place in that ordering from
``/proc/<pid>/oom_score_adj``: the highest score in the cgroup is killed first,
and a score below zero is skipped while any process with a non-negative score
is eligible. A service is given the lowest adjustment this process is
permitted to hold, and a worker a strictly higher one, so the ordering holds
even where lowering is refused: an unprivileged process may raise its
adjustment, and may lower it only when it carries ``CAP_SYS_RESOURCE``. That
asymmetry is why the service falls back to the value the process already
carried rather than failing: a fleet without the capability still runs, with
workers strictly above services because the workers were raised and the
services were left alone.
"""

from __future__ import annotations

from pathlib import Path

#: The adjustment a coordination service asks for. Negative means the kernel
#: reaches for it only after every process at or above zero, which is where
#: workers are put.
SERVICE_OOM_SCORE_ADJUSTMENT = -900

#: The adjustment a dispatched worker asks for. Positive on purpose: a worker
#: is the process the job can afford to lose, and raising it costs nothing
#: because an unprivileged process can always raise its own adjustment.
WORKER_OOM_SCORE_ADJUSTMENT = 500

MIN_OOM_SCORE_ADJUSTMENT = -1000
MAX_OOM_SCORE_ADJUSTMENT = 1000


def _proc_path(pid: int | None = None) -> Path:
    return (
        Path("/proc/self/oom_score_adj")
        if pid is None
        else Path(f"/proc/{int(pid)}/oom_score_adj")
    )


def read_adjustment(pid: int | None = None) -> int | None:
    """The adjustment ``pid`` holds, or None when it cannot be read.

    ``None`` is the reading for a kernel without the file and for a pid that
    has already gone; a caller ordering two processes treats an unreadable one
    as unknown rather than as zero.
    """
    try:
        return int(_proc_path(pid).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def write_adjustment(value: int, pid: int | None = None) -> int | None:
    """Write ``value`` to ``pid``'s adjustment and return what it now holds.

    None means the kernel refused the write, which is the ordinary answer for
    an unprivileged process asking to go below zero. The value is read back
    rather than echoed, because the kernel clamps an adjustment into its own
    range and a caller that reported what it asked for could not tell a refusal
    from a write that took.
    """
    clamped = max(MIN_OOM_SCORE_ADJUSTMENT, min(MAX_OOM_SCORE_ADJUSTMENT, int(value)))
    try:
        _proc_path(pid).write_text(f"{clamped}\n", encoding="ascii")
    except OSError:
        return None
    return read_adjustment(pid)


def claim(preferred: int, pid: int | None = None) -> int | None:
    """Set ``preferred``, falling back to the value already held if refused.

    The fallback is the process's own current adjustment, which the kernel
    always permits re-writing. A service therefore ends up as low as it is
    allowed to be instead of being left at whatever the job handed down.
    """
    applied = write_adjustment(preferred, pid)
    if applied is not None:
        return applied
    current = read_adjustment(pid)
    if current is None:
        return None
    return write_adjustment(current, pid)


def claim_service(pid: int | None = None) -> int | None:
    """Put this process, or ``pid``, on the service side of an OOM kill."""
    return claim(SERVICE_OOM_SCORE_ADJUSTMENT, pid)


def claim_worker(pid: int | None = None) -> int | None:
    """Put a dispatched worker on the side the kernel kills first.

    Called with the worker's pid from the process that spawned it: an
    unprivileged parent may raise a child's adjustment, and the child inherits
    it across the ``exec`` into the harness and into anything the harness
    forks. It is called after the spawn rather than inside a ``preexec_fn``
    because the spawning process may hold threads -- the admit service does --
    and a ``preexec_fn`` runs in a forked child of a threaded process.
    """
    return claim(WORKER_OOM_SCORE_ADJUSTMENT, pid)


def service_outranks_worker(service_pid: int, worker_pid: int) -> bool:
    """Whether ``service_pid`` is further from the kill than ``worker_pid``.

    A reading either side cannot give is a comparison that cannot be made, and
    an unreadable process is reported as not outranking rather than as a pass.
    """
    service = read_adjustment(service_pid)
    worker = read_adjustment(worker_pid)
    if service is None or worker is None:
        return False
    return service < worker


def describe(pid: int | None = None) -> str:
    """One line naming the adjustment ``pid`` holds, for a log."""
    adjustment = read_adjustment(pid)
    where = "self" if pid is None else str(pid)
    if adjustment is None:
        return f"oom_score_adj unreadable for {where}"
    return f"oom_score_adj {adjustment} on {where}"
