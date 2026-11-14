"""Run landing actions beside admission without blocking launches."""

from __future__ import annotations

from . import service as _service
from .shared import (
    LANDING_STOP_SECONDS,
    LANDING_THREAD_NAME,
    Any,
    Callable,
    Mapping,
    threading,
    time,
)


class _LandingWorker:
    """Run one landing action at a time beside the admission pass.

    The landing pass lives in a thread of its own rather than a stage of the
    pass, so a landing that is slow — a merge or publication — delays
    only the next landing. Scheduler measurements submit and return within
    that action; subsequent ticks adopt their persisted job records.
    The lock is the whole of the exclusion and it is
    taken without blocking, so a tick that finds an action in flight leaves it
    alone instead of stacking a second behind it.

    An action that raises is recorded as a result on the worker and never
    propagates: a defect in one landing must not end the service that runs the
    launches, and the next tick starts a fresh action.
    """

    def __init__(self, lander: Callable[[], Mapping[str, Any]] | None = None) -> None:
        self._lander = lander
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last: dict[str, Any] = {}

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def last(self) -> dict[str, Any]:
        return dict(self._last)

    def _land(self) -> Mapping[str, Any]:
        if self._lander is not None:
            return self._lander()
        from reckon.crew.queue_landing import land_once

        return land_once()

    def _run(self) -> None:
        started_at = _service._utc_now()
        started = time.monotonic()
        try:
            result = dict(self._land() or {})
        except Exception as exc:  # noqa: BLE001 — a landing defect ends no service
            result = {
                "landed": [],
                "obligations": [],
                "deferred": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            ended_at = _service._utc_now()
            seconds = round(time.monotonic() - started, 3)
            self._last = {
                **result,
                "started_at": started_at,
                "ended_at": ended_at,
                "seconds": seconds,
            }
            self._lock.release()

    def tick(self) -> bool:
        """Start one landing action unless one is in flight."""
        if not self._lock.acquire(blocking=False):
            return False
        thread = threading.Thread(
            target=self._run, name=LANDING_THREAD_NAME, daemon=True
        )
        self._thread = thread
        thread.start()
        return True

    def settle(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the action this worker started, and return its last result."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.last

    def stop(self) -> None:
        """Let a bounded landing action finish as the service ends."""
        self.settle(timeout=LANDING_STOP_SECONDS)
