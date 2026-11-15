"""Dispatch the review every delivered run owes, on the admit service's clock.

A delivered run lands only after its review, and a review used to be dispatched
only by the follower of the session that dispatched the run, while that
follower was alive. A coordinator that stopped, or whose follower was
restarting, left its delivered runs unreviewed and therefore unlanded, with
their commits in a worktree and every request waiting on their section parked
behind them. The admit service is the one process that is always running, so it
makes the same sweep itself, for every session's runs.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: How long the sweep waits between two passes over the live projects.
REVIEW_SWEEP_SECONDS = 60.0

#: How long a replacement of this image waits for a sweep in flight. A dispatch
#: cut off part way leaves a pointer no process owns, so the wait is generous.
REVIEW_SWEEP_DRAIN_SECONDS = 600.0


def _live_projects() -> list[str]:
    """Every project that holds a live pointer, in a stable order."""
    from reckon.crew import runs

    return sorted(
        {
            str(pointer.get("project") or "")
            for pointer in runs.list_live()
            if pointer.get("project")
        }
    )


def _sweep_project(project: str) -> Mapping[str, Any]:
    """Dispatch the owed reviews of one project, whoever dispatched the runs."""
    from reckon.crew import recovery_watch

    return recovery_watch.recover(
        project=project, dispatch_reviews=True, any_session=True
    )


class ReviewSweep:
    """One thread that dispatches owed reviews until it is stopped."""

    def __init__(
        self,
        *,
        projects: Callable[[], Iterable[str]] = _live_projects,
        sweep: Callable[[str], Mapping[str, Any]] = _sweep_project,
        interval: float = REVIEW_SWEEP_SECONDS,
        code_stamp: Callable[[], Any] | None = None,
    ) -> None:
        self._projects = projects
        self._sweep = sweep
        self._interval = interval
        self._code_stamp = code_stamp
        self._armed_stamp: Any = None
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self.passes = 0
        self.dispatched: list[str] = []

    def start(self) -> None:
        """Start the sweep thread; a second call while it runs starts nothing."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._armed_stamp = None if self._code_stamp is None else self._code_stamp()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="review-sweep", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = REVIEW_SWEEP_DRAIN_SECONDS) -> bool:
        """Start no further dispatch and wait for the one in flight.

        Returns whether the sweep was idle when the wait ended.
        """
        self._stop.set()
        return self._idle.wait(timeout)

    def _code_moved(self) -> bool:
        if self._code_stamp is None:
            return False
        try:
            return self._code_stamp() != self._armed_stamp
        except Exception:  # noqa: BLE001 - an unreadable stamp is not a move
            return False

    def sweep_once(self) -> list[str]:
        """Sweep every live project once and return the reviews it dispatched."""
        dispatched: list[str] = []
        for project in self._projects():
            if self._stop.is_set() or self._code_moved():
                break
            try:
                report = self._sweep(project)
            except Exception:  # one project's failure must not end the sweep
                _LOGGER.exception("review sweep failed for project %s", project)
                continue
            dispatched.extend(
                str(run) for run in (report.get("reviews_dispatched") or ())
            )
        self.passes += 1
        self.dispatched.extend(dispatched)
        return dispatched

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._code_moved():
                # The replacement image starts its own sweep; this one ends
                # here rather than begin a dispatch that image would cut off.
                return
            self._idle.clear()
            try:
                self.sweep_once()
            finally:
                self._idle.set()
            self._stop.wait(self._interval)
