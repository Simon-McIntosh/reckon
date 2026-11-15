"""Keep a managed repository packed on a schedule.

Git stores a new object loose and keeps an unreachable one loose for two weeks,
so a landing pass that writes a merged tree on every conflict check, and a
discarded run whose commits nobody keeps, each add to a population nothing
collects. Once the loose count passes ``gc.auto`` git writes ``.git/gc.log``
and switches automatic collection off for a day, so every git call from every
worktree pays for the loose objects until somebody repacks by hand.

One pass here packs reachable and unreachable objects alike, which is what
leaves the unreachable ones waiting in a cruft pack beside its ``.mtimes``
index until git's own expiry removes them, rather than as loose files.

The pass runs from a compute partition rather than from any process a
coordinator owns: a repack of a busy checkout takes minutes of IO, and the
nodes that host the fleet hold coordination work. ``MaintenanceSweep`` is the
schedule -- the station calls it on its own beat, it submits a pass for every
managed repository whose last recorded run is older than a day, and it records
each run's counts beside its repository so the fleet view can report the
loose-object count without reading the repository itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: How old a repository's last recorded pass must be before another is due.
MAINTENANCE_INTERVAL_SECONDS = 24 * 60 * 60

#: The partition a scheduled pass runs on. A packing pass is IO-heavy and long
#: enough to matter on a shared node, so it never runs where the fleet's own
#: coordinators run.
MAINTENANCE_PARTITION = "all_debug"

#: Wall clock a submitted pass is granted. A repository far behind on packing
#: takes minutes, and the grant is generous enough that a slow filesystem does
#: not cut one short; the scheduler ends an overrun rather than a client.
MAINTENANCE_JOB_MINUTES = 30

#: The allocation a submitted pass is given. Packing is one process reading and
#: writing one object store, so a wider allocation buys nothing.
MAINTENANCE_JOB_CPUS = 2
MAINTENANCE_JOB_MEMORY = "8G"

#: How long one pass may run before this process stops waiting for it.
PASS_TIMEOUT_SECONDS = 3600

#: The station re-reads each repository's record at most this often. The record
#: it reads already gates a submission on a day's age, so this only bounds the
#: reads a quiet fleet pays for.
CHECK_INTERVAL_SECONDS = 60 * 60

RECORD_NAME = "maintenance.json"

#: ``git count-objects -v`` fields this module keeps. ``count`` is the loose
#: population; ``in-pack`` and ``packs`` describe what a pass produced.
_COUNT_FIELDS = ("count", "in-pack", "packs", "size", "size-pack", "prune-packable")


def _home(home: str | Path | None) -> Path:
    if home is not None:
        return Path(home)
    from reckon.crew.obligation_snapshot import crew_home

    return crew_home()


def repository_token(repository: str | Path) -> str:
    """A filesystem-safe stem naming one repository's records.

    The digest of the resolved path keeps two checkouts that share a basename
    apart, so one repository's record is never read as another's.
    """

    resolved = Path(repository).expanduser().resolve()
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", resolved.name).strip("-") or "repo"
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:12]
    return f"{readable}-{digest}"


def record_path(repository: str | Path, *, home: str | Path | None = None) -> Path:
    """Where one repository's last pass is recorded."""

    return _home(home) / "repositories" / repository_token(repository) / RECORD_NAME


def read_record(
    repository: str | Path, *, home: str | Path | None = None
) -> dict[str, Any] | None:
    """One repository's recorded pass, or ``None`` when it has none readable.

    A record that cannot be read is an absent record rather than an error: the
    question it answers is whether a pass is due, and a repository whose record
    is unreadable is one whose pass is due.
    """

    path = record_path(repository, home=home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_record(
    repository: str | Path, record: Mapping[str, Any], *, home: str | Path | None
) -> Path:
    path = record_path(repository, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}")
    staging.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(staging, path)
    return path


def _git(
    repository: Path, args: list[str], *, timeout: float = 180.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def object_counts(repository: str | Path) -> dict[str, int]:
    """The repository's object counts, as ``git count-objects -v`` reports them.

    Raises rather than reading a failed command as an empty repository: a count
    of zero is exactly the answer a broken invocation would give, and it is the
    answer that would read as "nothing left to pack".
    """

    result = _git(Path(repository), ["count-objects", "-v"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(f"git count-objects failed in {repository}: {detail}")
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in _COUNT_FIELDS:
            counts[key.strip()] = int(value.strip() or 0)
    return counts


def automatic_threshold(repository: str | Path) -> int | None:
    """The loose count at which git collects by itself, or ``None`` when off.

    ``gc.auto`` of zero disables automatic collection; a repository that has
    toggled it off has no threshold to fall under, and a caller reports the
    count rather than a verdict about it.
    """

    result = _git(Path(repository), ["config", "--get", "gc.auto"])
    if result.returncode != 0:
        return 6700
    try:
        value = int(result.stdout.strip() or "0")
    except ValueError:
        return 6700
    return value if value > 0 else None


def _common_directory(repository: Path) -> Path:
    """The repository's common git directory, which a linked worktree shares."""

    result = _git(repository, ["rev-parse", "--git-common-dir"])
    if result.returncode != 0:
        return repository / ".git"
    common = Path(result.stdout.strip() or ".git")
    return common if common.is_absolute() else (repository / common)


def gc_log_path(repository: str | Path) -> Path:
    """The ``gc.log`` git writes when a collection fails or cannot be scheduled."""

    repo = Path(repository)
    return _common_directory(repo) / "gc.log"


def _clear_gc_log(repository: Path) -> Path | None:
    """Remove a stale ``gc.log`` after a successful pass, reporting the removal.

    The file is what switches automatic collection off for a day, so a pass
    that has packed the repository is the moment it stops being needed.
    """

    path = gc_log_path(repository)
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return path


def _pack_files(repository: Path) -> list[Path]:
    return sorted((_common_directory(repository) / "objects" / "pack").glob("pack-*"))


def cruft_packs(repository: str | Path) -> list[Path]:
    """The cruft packs the repository holds, named by their ``.mtimes`` index."""

    return [path for path in _pack_files(Path(repository)) if path.suffix == ".mtimes"]


def maintenance_pass(
    repository: str | Path,
    *,
    home: str | Path | None = None,
    now: datetime | None = None,
    timeout: float = PASS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Pack one repository, record its counts, and clear a stale ``gc.log``.

    ``repack -a -d`` rewrites every pack and drops the loose objects it packed;
    ``--cruft`` keeps the unreachable ones in a cruft pack with its ``.mtimes``
    index, so they wait for git's own expiry there rather than as loose files
    nothing collects. The record is written whether the pack succeeds or fails,
    because a failed pass is the one a later reader has to see.
    """

    repo = Path(repository).expanduser().resolve()
    moment = now or datetime.now(tz=UTC)
    before = object_counts(repo)
    packs_before = [path.name for path in _pack_files(repo)]
    failure = ""
    try:
        result = _git(repo, ["repack", "-a", "--cruft", "-d"], timeout=timeout)
        if result.returncode != 0:
            failure = (result.stderr or result.stdout or "").strip()
            failure = failure or "git repack failed"
    except subprocess.TimeoutExpired:
        failure = f"git repack exceeded {timeout:.0f}s"
    after = None if failure else object_counts(repo)
    cleared = None if failure else _clear_gc_log(repo)
    record: dict[str, Any] = {
        "repository": str(repo),
        "ran_at": moment.astimezone(UTC).isoformat(),
        "ok": not failure,
        "error": failure,
        "threshold": automatic_threshold(repo),
        "before": before,
        "after": after,
        "loose_before": before.get("count", 0),
        "loose_after": None if after is None else after.get("count", 0),
        "packs_before": packs_before,
        "packs_after": [path.name for path in _pack_files(repo)],
        "cruft_packs": [path.name for path in cruft_packs(repo)],
        "gc_log_removed": str(cleared) if cleared is not None else "",
    }
    _write_record(repo, record, home=home)
    return record


def maintenance_due(
    repository: str | Path,
    *,
    home: str | Path | None = None,
    now: datetime | None = None,
    interval_seconds: float = MAINTENANCE_INTERVAL_SECONDS,
) -> bool:
    """Whether a pass is due: no usable record, or one older than the interval."""

    record = read_record(repository, home=home)
    if record is None:
        return True
    ran_at = record.get("ran_at")
    try:
        stamp = datetime.fromisoformat(str(ran_at))
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    moment = now or datetime.now(tz=UTC)
    return (moment.astimezone(UTC) - stamp.astimezone(UTC)).total_seconds() >= (
        interval_seconds
    )


def managed_repositories(
    projects: Mapping[str, Path] | None = None,
) -> list[Path]:
    """Every repository reckon manages, from the projects mounted into the SPA.

    A project's docs directory lives inside its checkout, so the repository is
    the docs directory's parent. Several projects may be mounted from one
    repository; it is packed once.
    """

    if projects is None:
        from reckon import flight

        projects = flight.mounted_project_docs()
    repositories: dict[str, Path] = {}
    for docs in projects.values():
        repository = Path(docs).parent.resolve()
        repositories.setdefault(str(repository), repository)
    return [repositories[key] for key in sorted(repositories)]


def maintenance_job_script(
    repository: str | Path,
    *,
    partition: str = MAINTENANCE_PARTITION,
    log_path: str | Path | None = None,
    home: str | Path | None = None,
) -> str:
    """The batch script one scheduled pass runs.

    The job runs the same maintenance verb a hand run uses, so the scheduled
    path and the manual one cannot drift about what a pass does. The
    interpreter is this process's own, which is the one that resolves the
    installed ``reckon`` package, and ``TMPDIR`` is the node's own disk so the
    pack's temporary files never land on the shared filesystem.
    """

    repo = Path(repository).expanduser().resolve()
    if log_path is None:
        log_path = record_path(repo, home=home).with_name("maintenance.log")
    log = Path(log_path)
    entry = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('from reckon.cli import main; main()')} "
        f"crew gc --maintain --repo {shlex.quote(str(repo))}"
    )
    return "\n".join(
        [
            "#!/bin/bash",
            f"#SBATCH --partition={partition}",
            "#SBATCH --job-name=reckon-maintenance",
            f"#SBATCH --time=00:{MAINTENANCE_JOB_MINUTES:02d}:00",
            "#SBATCH --nodes=1",
            "#SBATCH --ntasks=1",
            f"#SBATCH --cpus-per-task={MAINTENANCE_JOB_CPUS}",
            f"#SBATCH --mem={MAINTENANCE_JOB_MEMORY}",
            f"#SBATCH --output={log}",
            f"#SBATCH --error={log}",
            "set -euo pipefail",
            "export TMPDIR=/tmp",
            f"cd {shlex.quote(str(repo))}",
            entry,
            "",
        ]
    )


def submit_pass(
    repository: str | Path,
    *,
    partition: str = MAINTENANCE_PARTITION,
    log_path: str | Path | None = None,
    home: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Submit one maintenance pass as a scheduler job, reporting where it went.

    The submission does not wait: a station tick must not hold its heartbeat
    behind a job's runtime, and the job's own record is what the next reader
    finds. A refusal from the scheduler is raised rather than reported as a
    submission, so a pass that never started is never recorded as due-done.
    """

    repo = Path(repository).expanduser().resolve()
    script = maintenance_job_script(
        repo, partition=partition, log_path=log_path, home=home
    )
    run = subprocess.run if runner is None else runner
    result = run(
        ["sbatch", "--parsable"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(f"sbatch refused the maintenance pass: {detail}")
    job_id = (result.stdout or "").strip().split(";", maxsplit=1)[0]
    return {
        "repository": str(repo),
        "job_id": job_id,
        "partition": partition,
        "script": script,
    }


class MaintenanceSweep:
    """Submit a maintenance pass for each managed repository that is due.

    Called from the station's tick, which is the fleet's long-running service,
    so keeping the repositories packed needs no hand command and no systemd
    timer: this cluster refuses lingering, and a user timer stops at logout
    while the station runs for as long as the fleet does.
    """

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = MAINTENANCE_INTERVAL_SECONDS,
        check_interval_seconds: float = CHECK_INTERVAL_SECONDS,
        partition: str = MAINTENANCE_PARTITION,
        submit: Callable[[str | Path], dict[str, Any]] | None = None,
    ) -> None:
        self.home = home
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.interval_seconds = interval_seconds
        self.check_interval_seconds = check_interval_seconds
        self.partition = partition
        self._submit = submit
        self._last_check: datetime | None = None

    def _submit_pass(self, repository: Path) -> dict[str, Any]:
        if self._submit is not None:
            return self._submit(repository)
        return submit_pass(repository, partition=self.partition, home=self.home)

    def due_repositories(
        self, repositories: list[Path], *, moment: datetime
    ) -> list[Path]:
        return [
            repository
            for repository in repositories
            if maintenance_due(
                repository,
                home=self.home,
                now=moment,
                interval_seconds=self.interval_seconds,
            )
        ]

    def run(
        self,
        projects: Mapping[str, Path] | None = None,
        *,
        moment: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Submit what is due, at most once per check interval.

        A submission the scheduler refuses is reported in the row rather than
        raised into the tick: one repository's refusal must not stop the other
        repositories' passes, and the row is what a later reader sees.
        """

        now = moment or self.clock()
        if self._last_check is not None:
            elapsed = (now - self._last_check).total_seconds()
            if 0 <= elapsed < self.check_interval_seconds:
                return []
        self._last_check = now
        rows: list[dict[str, Any]] = []
        for repository in self.due_repositories(
            managed_repositories(projects), moment=now
        ):
            try:
                rows.append({"ok": True, **self._submit_pass(repository)})
            except (OSError, subprocess.SubprocessError) as error:
                rows.append(
                    {"ok": False, "repository": str(repository), "error": str(error)}
                )
        return rows
