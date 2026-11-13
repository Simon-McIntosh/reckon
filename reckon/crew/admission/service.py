"""Admission service lifetime, locking and code reload."""

from __future__ import annotations

from . import landing as _landing
from . import release as _release
from . import selection as _selection
from . import verdicts as _verdicts
from .shared import (
    _TRUSTED_CODE_STAMPS,
    ADMIT_LOCK_NAME,
    DEFAULT_ADMIT_INTERVAL_SECONDS,
    UTC,
    Any,
    Callable,
    Mapping,
    Path,
    contextmanager,
    datetime,
    fcntl,
    os,
    runs,
    sys,
    time,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc_now() -> str:
    return _now().isoformat()


def _admit_lock_path() -> Path:
    return runs.crew_home() / "locks" / ADMIT_LOCK_NAME


@contextmanager
def _admit_lock():
    """Serialise two admission passes so one queued run launches once."""
    path = _admit_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _admitting_pass_identity() -> dict[str, Any]:
    """The admission pass that has taken a request, as its handover records it.

    The handover outlives the queued pointer, so it is the only place a launch
    that loses its pointer can learn which pass launched it, on which process,
    from which checkout.
    """
    return {"pid": os.getpid(), "at": _utc_now(), "code": _admit_code_stamp()}


def _admit_code_stamp() -> str:
    """The content identity of the checkout the admission service would run.

    The same identity the followers compute, so a commit that changes no file
    under ``reckon/`` moves nothing here and re-executes nothing.
    """
    from reckon.crew.follower_registration import follower_code_stamp

    return follower_code_stamp()


def _source_root() -> Path:
    """The sys.path root that supplied this imported package.

    The followers' reloader resolves its checkout the same way, so both name
    one root rather than each counting its own way up the tree. This module
    sits in a subpackage, where counting parents stops at ``reckon/`` -- the
    package directory, which is not a checkout and reads as clean and
    importable wherever it is asked.
    """
    from reckon.crew_follow_commands import _import_root

    return _import_root(sys.modules[__name__])


def _checkout_code_is_trusted() -> bool:
    """Whether the checkout's current code may be loaded and run.

    The same trusted-checkout test the followers' reloader uses: clean under
    ``reckon/`` against ``HEAD`` and importable. A tree mid-merge refuses, so a
    running service is never replaced by code a half-finished edit produced.
    """
    from reckon import checkout_trust

    stamp = _admit_code_stamp()
    root = _source_root()
    dirty, _detail = checkout_trust.tracked_reckon_tree_is_dirty(root)
    if dirty:
        return False
    key = (str(root), stamp)
    remembered = _TRUSTED_CODE_STAMPS.get(key)
    if remembered is not None:
        return remembered
    verdict = checkout_trust.moved_checkout_is_trustworthy(root)
    if (
        not verdict.dirty
        and stamp != checkout_trust.IDENTITY_UNKNOWN
        and not isinstance(
            verdict.detail,
            (checkout_trust.ImportProbeTimedOut, checkout_trust.ImportProofPending),
        )
    ):
        _TRUSTED_CODE_STAMPS.clear()
        _TRUSTED_CODE_STAMPS[key] = verdict.trusted
    return verdict.trusted


def _reexec_into_current_code() -> None:
    """Replace this process with a fresh image of the checkout's current code.

    ``os.execve`` reuses the process, so the supervisor that declared this
    service need not restart it: the same pid continues on the code now on
    disk. The launcher leads with the sys.path root that supplied this import,
    so the replacement loads the checkout rather than whatever argv[0] named.
    """
    root = str(_source_root())
    launcher = (
        f"import sys; sys.path.insert(0, {root!r}); from reckon.cli import main; main()"
    )
    # An admission pass runs on this service's threads, and one between taking a
    # node's dispatch claim and starting its worker would be cut off by the
    # replacement, leaving that claim with no worker behind it. Wait, bounded.
    from reckon.crew import dispatch_claims

    dispatch_claims.wait_for_dispatches_in_flight()
    os.execve(
        sys.executable,
        [sys.executable, "-c", launcher, *sys.argv[1:]],
        dict(os.environ),
    )


def _pause(seconds: float) -> None:
    """Wait out the interval between two admission passes."""
    time.sleep(seconds)


def _record_stage_timings(
    result: dict[str, Any],
    *,
    started_at: str,
    started: float,
    released: float,
    ended: float,
    landing: Mapping[str, Any],
) -> None:
    """Record the pass's start, end and per-stage seconds on its verdict row.

    Release, waits, launch and land are measured apart because they are held by
    different things — a lane slot, a plan's wait resolution and review holds, a
    merge, a suite — and a row that reported one duration would not tell a
    reader which of them stalled. The pass reports the wait stage from its own
    clock, so no time.sleep or plan read there is charged to ``launch_seconds``,
    which then covers only launch decisions and launches. ``land_seconds`` is
    the duration of the landing action the row reports: the pass itself never
    waits for it.
    """
    result["started_at"] = started_at
    result["ended_at"] = _utc_now()
    result["release_seconds"] = round(released - started, 3)
    waits = float(result.get("waits_seconds") or 0.0)
    result["launch_seconds"] = round(max(0.0, ended - released - waits), 3)
    result["land_seconds"] = round(float(landing.get("seconds") or 0.0), 3)


def admit_until_stopped(
    *,
    lane: str = "",
    interval: float = DEFAULT_ADMIT_INTERVAL_SECONDS,
    once: bool = False,
    launcher: Callable[..., int] | None = None,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    lander: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Repeat the admission pass on a cadence until stopped.

    ``--once`` runs a single pass and returns, which is what a test drives. The
    loop otherwise catches ``KeyboardInterrupt`` so a stopped service returns
    the count of passes it ran rather than a traceback.

    The landing pass is not a stage of this loop: it runs in a worker thread
    under a lock that admits at most one action at a time, so a slow landing
    delays only the next landing and never a launch or a release. ``--once``
    waits for the landing pass it starts, because a manual pass reports what
    it did. That pass submits scheduler measurements without waiting for their
    jobs; their persisted identities are adopted by a later pass.

    Every pass whose lane had a queued pointer leaves one JSON line in the
    verdict log under the crew home, so a request that waited without launching
    can be read afterwards rather than inferred from a pointer that may since
    have been discarded. A pass that examined no queued pointer writes nothing.

    The loop also prints that same line to stdout as it is recorded, so a
    coordinator watching the bare verb sees what each pass decided while it is
    running rather than having to read the log afterwards to learn whether it
    was working. ``--once`` returns its pass to the caller instead and prints
    nothing here, because the verb reports that pass itself.

    The code stamp is compared at the head of every pass, before the pass runs.
    A pass must never validate or launch a queued request with code older than
    the checkout it launches from, and the checkout can move during the wait
    between passes: checking afterwards would let one more pass run on the old
    code before the move was noticed. So a stamp the checkout has moved past, on
    a checkout the trusted test accepts, re-executes this process in place before
    the next pass begins, and the failure guard's code fingerprint retries
    work held before re-exec. A stamp on a checkout the trusted test refuses
    leaves the service on its current code, so a tree mid-merge never replaces a
    running service.
    """
    # A service that replaced its own image in place still owns the children
    # the old image left. Registering them here, once, lets the shared reaper
    # collect them; nothing in this image refers to them otherwise.
    from reckon import flight
    from reckon.crew.dispatch_launch import _adopt_launched_workers_from_reexec
    from reckon.crew.fleet_supervisor import service_pass
    from reckon.crew.promotion_requests import PromotionService
    from reckon.crew.resumption import _promotion_settings

    _adopt_launched_workers_from_reexec()
    promotion_width, _ = _promotion_settings(flight.resolve().config)
    promotions = PromotionService(width=promotion_width)
    admission_options: dict[str, Any] = {"lane": lane}
    if launcher is not None:
        admission_options["launcher"] = launcher
    if config_for is not None:
        admission_options["config_for"] = config_for
    if once:
        stamp = _admit_code_stamp()
        worker = _landing._LandingWorker(lander)

        # The release stage runs before the landing and the launch pass, on a
        # clock of its own: a section that has already landed releases its
        # waiters whether or not this pass goes on to launch or land anything.
        # A manual pass reports the landing it started, so it waits for it; the
        # service loop below does not.
        started_at = _utc_now()
        started = time.monotonic()
        release = _release._release_tick()
        released = time.monotonic()
        worker.tick()
        promotions.tick()
        landing = worker.settle()
        result = _selection.admit_once(**admission_options)
        ended = time.monotonic()
        result["release"] = release
        result["landing"] = landing
        _record_stage_timings(
            result,
            started_at=started_at,
            started=started,
            released=released,
            ended=ended,
            landing=landing,
        )
        _verdicts._record_pass_verdicts(result, lane=lane, code_stamp=stamp)
        return result
    passes = 0
    code_stamp = _admit_code_stamp()
    worker = _landing._LandingWorker(lander)
    try:
        while True:
            with service_pass():
                if _admit_code_stamp() != code_stamp and _checkout_code_is_trusted():
                    _reexec_into_current_code()
                started_at = _utc_now()
                started = time.monotonic()
                # The landing runs in its own thread under its own lock, so this
                # pass proceeds whether or not one is in flight, and the landing
                # holds only the next landing.
                worker.tick()
                promotions.tick()
                release = _release._release_tick()
                released = time.monotonic()
                admitted = _selection.admit_once(**admission_options)
                ended = time.monotonic()
                landing = worker.last
                admitted["release"] = release
                admitted["landing"] = landing
                _record_stage_timings(
                    admitted,
                    started_at=started_at,
                    started=started,
                    released=released,
                    ended=ended,
                    landing=landing,
                )
                _verdicts._publish_pass_verdicts(
                    _verdicts._record_pass_verdicts(
                        admitted, lane=lane, code_stamp=code_stamp
                    )
                )
                passes += 1
            _pause(interval)
    except KeyboardInterrupt:
        return {"passes": passes}
    finally:
        worker.stop()
