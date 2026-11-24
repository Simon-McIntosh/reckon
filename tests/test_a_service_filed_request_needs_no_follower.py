"""A request the admission service files launches without a session follower.

The admission service files requests for plans no live session coordinates,
under its own session identity. No follower can ever be registered for that
identity, so a launch that required one was refused on every admission pass:
on 24 November nine review requests were held this way for ten hours while the
local lane sat idle. Such a launch needs only the project's producer, which is
what the station's sweep and the lander read to review and land its run.
"""

from __future__ import annotations

import pytest

from reckon.crew import dispatch_watch
from reckon.crew.dispatch_batch import ADMISSION_SERVICE_SESSION

PROJECT = "service-filed"
ENSURE = f"reckon crew watch --ensure --project {PROJECT}"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))


def test_a_service_filed_launch_is_admitted_with_a_live_producer() -> None:
    assert (
        dispatch_watch._watcher_delivery_admission(
            PROJECT,
            {"watcher_live": True, "ensure_line": ENSURE},
            session=ADMISSION_SERVICE_SESSION,
            launch_kind="cli",
        )
        is None
    )


def test_a_service_filed_launch_still_needs_a_producer() -> None:
    with pytest.raises(dispatch_watch.WatcherRequired):
        dispatch_watch._watcher_delivery_admission(
            PROJECT,
            {"watcher_live": False, "ensure_line": ENSURE},
            session=ADMISSION_SERVICE_SESSION,
            launch_kind="cli",
        )


def test_a_coordinator_session_without_a_follower_is_still_refused() -> None:
    with pytest.raises(dispatch_watch._FollowerAdmissionUnmet):
        dispatch_watch._watcher_delivery_admission(
            PROJECT,
            {"watcher_live": True, "ensure_line": ENSURE},
            session="s-a-coordinator-with-no-follower",
            launch_kind="cli",
        )
