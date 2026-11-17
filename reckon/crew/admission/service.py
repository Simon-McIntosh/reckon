"""Admission service lifetime, locking and code reload."""

from __future__ import annotations

from reckon.node_exclusive_lock import exclusive_lock

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
    with exclusive_lock(_admit_lock_path()):
        yield


def _admitting_pass_identity() -> dict[str, Any]:
    """The admission pass that has taken a request, as its handover records it.

    The handover outlives the queued pointer, so it is the only place a launch
    that loses its pointer can learn which pass launched it, on which process,
    from which checkout.
    """
    import socket

    return {
        "pid": os.getpid(),
        "pid_start_time": runs._process_start_time(os.getpid()),
        "host": socket.gethostname(),
        "at": _utc_now(),
        "code": _admit_code_stamp(),
    }


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
    root = Path(os.environ.get("RECKON_FOLLOWER_SOURCE_ROOT", str(_source_root())))
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
    from reckon import checkout_trust
    from reckon.crew import dispatch_claims, follower_reload

    imported_root = _source_root()
    source_root = Path(
        os.environ.get("RECKON_FOLLOWER_SOURCE_ROOT", str(imported_root))
    )
    pin = None
    root = imported_root
    environment = dict(os.environ)
    if source_root != imported_root:
        current = _admit_code_stamp()
        root, _removed, _kept, pin = checkout_trust.materialize_committed_tree(
            source_root, current, active_root=imported_root
        )
        imports, reason = checkout_trust.reckon_imports(root)
        if not imports:
            os.close(pin)
            raise RuntimeError(f"admission reload image does not import: {reason}")
        environment = follower_reload.image_reexec_environment(
            source_root, current, pin
        )
    launcher = f"import sys; sys.path.insert(0, {str(root)!r}); from reckon.cli import main; main()"
    # A replacement must not cut off the thread between claiming and launching.
    dispatch_claims.wait_for_dispatches_in_flight()
    try:
        os.execve(
            sys.executable,
            [sys.executable, "-c", launcher, *sys.argv[1:]],
            environment,
        )
    finally:
        if pin is not None:
            os.close(pin)


def _pause(seconds: float) -> None:
    """Wait out the interval between two admission passes."""
    time.sleep(seconds)


def node_disk_admission_hold() -> dict[str, Any] | None:
    """Read the target fleet disk locally or through its published sample."""
    import json
    import socket

    from reckon.crew import fleet_supervisor as supervisor

    try:
        fleet = json.loads(
            (supervisor.state_directory() / supervisor.RECORD_NAME).read_text()
        )
    except (OSError, ValueError):
        return None
    if not isinstance(fleet, dict) or not fleet.get("node"):
        return None
    if str(fleet["node"]).split(".")[0] != socket.gethostname().split(".")[0]:
        try:
            sample = supervisor.published_node_disk_sample()
        except supervisor.NodeDiskSampleError as exc:
            return supervisor.node_disk_reading_obligation(exc)
        if sample is None:
            return supervisor.node_disk_reading_obligation(
                supervisor.NodeDiskSampleError(
                    "unreadable",
                    f"Fleet node {fleet['node']} has no current published disk reading",
                )
            )
        return supervisor.node_disk_pressure(sample)
    mount = Path(os.environ.get(supervisor.NODE_DISK_ROOT_ENV) or "/tmp")  # noqa: S108 - node-local
    try:
        sample = supervisor.node_disk_snapshot(
            mount, reserve_bytes=supervisor.node_disk_reserve_bytes()
        )
    except OSError as exc:
        return {
            "kind": "node-disk-unreadable",
            "run_id": "",
            "age_seconds": 0,
            "reason": f"Fleet local disk {mount} cannot be read: {exc}",
            "next_command": "Restore the fleet local disk reading before admitting launches",
        }
    return supervisor.node_disk_pressure(sample)


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

    The shared reload window observes the checkout at the head of each pass.
    Code changes coalesce until its deadline and until this image has finished
    and published a pass. An immediate request bypasses the deadline, while
    the trust check and the drain still protect every replacement.

    A replacement ends every thread this process started, so a stamp that has
    moved no longer re-executes at once: the loop starts no further promotion or
    landing, waits for those in flight to finish on their own threads, and then
    replaces the image. Waiting inside the pass is what bounds the reload, since
    the supervisor's pass bound is measured from the pass's own start.
    """
    # A service that replaced its own image in place still owns the children
    # the old image left. Registering them here, once, lets the shared reaper
    # collect them; nothing in this image refers to them otherwise.
    from reckon import flight
    from reckon.crew.dispatch_launch import _adopt_launched_workers_from_reexec
    from reckon.crew.fleet_supervisor import service_pass
    from reckon.crew.follower_reload import ReloadWindow
    from reckon.crew.promotion_requests import PromotionService
    from reckon.crew.resumption import _promotion_settings

    _adopt_launched_workers_from_reexec()
    promotion_width, _ = _promotion_settings(flight.resolve().config)
    # The window owns the decision to drain; a moved stamp alone keeps work flowing.
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
        with runs.resolution_scope():
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
    from reckon import checkout_trust

    code_stamp = (
        checkout_trust.running_code_identity(_source_root() / "reckon")
        if checkout_trust.published_reload_image(_source_root())
        else _admit_code_stamp()
    )
    service_name = Path(
        os.environ.get("RECKON_SERVICE_PASS_PATH", "admission.pass")
    ).stem
    reload_window = ReloadWindow(service_name, code_stamp)
    worker = _landing._LandingWorker(lander)
    # The queue refills itself between passes.
    promotions.start_refill()
    # A delivered run owes a review before it can land, and its coordinator may
    # not be there to dispatch it, so the service dispatches owed reviews on a
    # clock of its own.
    from .review_sweep import ReviewSweep

    reviews = ReviewSweep()
    reviews.start()
    try:
        while True:
            with service_pass():
                newest_code = _admit_code_stamp()
                if reload_window.due(newest_code) and _checkout_code_is_trusted():
                    # Replacing this image ends every thread it started, and the
                    # promotions and landings run on those threads. So no new
                    # one starts once the stamp has moved: the loop drains the
                    # work in flight, and only then replaces the process.
                    promotions.begin_drain()
                    reviews.stop()
                    promotions.wait_for_in_flight()
                    worker.settle()
                    with reload_window.replacement(newest_code):
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
                with runs.resolution_scope():
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
                reload_window.exported()
                passes += 1
            _pause(interval)
    except KeyboardInterrupt:
        return {"passes": passes}
    finally:
        promotions.stop_refill()
        reviews.stop(timeout=5.0)
        worker.stop()
