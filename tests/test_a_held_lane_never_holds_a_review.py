"""A review that followed its run does not inherit that run's budget hold.

The review reflex composes a review onto the lane the reviewed run was carried
on. That is a preference derived from which lane ran the work, never a judgement
that the lane will serve this request: when the lane is under a budget hold whose
stated reset is days away, a review placed behind it waits for a reason nobody
chose. A coordinator who names a lane makes the other kind of statement, and
that one is authoritative — the answer for it is a queued request that waits,
not a silent move onto another lane.

So the route admission may rebind is the one the reflex wrote, and the field
that says which is ``named_route.set_by``. These assertions argue it from both
sides: the reflex's route rebinds off a held lane with no picked selection to
justify it, and a caller-named route on the same held lane does not move.
"""

from __future__ import annotations

import importlib
import os

import pytest

from reckon.crew import queue_admission, recovery, runs
from reckon.crew.dispatch_admission import LaneHeld
from tests import test_review_reflex_selects_a_backend as reflex_tests
from tests import test_route_before_queue as routing_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")
REVIEW_FOLLOWS_RUN = dispatch_module.REVIEW_FOLLOWS_RUN_ORIGIN
LOCAL = routing_tests.LOCAL
METERED = routing_tests.METERED


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    return routing_tests.repository.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def routing(repository, monkeypatch):
    return routing_tests.routing.__wrapped__(repository, monkeypatch)


def _dispatch(repository, config, name: str, *, lane_origin: str = ""):
    """Queue a request whose lane was named, recording who named it.

    ``backend_override`` is what makes dispatch write a ``named_route`` at all;
    ``lane_origin`` is what it records on it, and leaving it empty is a
    coordinator's own naming.
    """
    return dispatch_module.dispatch(
        node=routing_tests._node(name),
        project=routing_tests.PROJECT,
        repo=repository,
        config=config,
        session=routing_tests.SESSION,
        route="deterministic",
        backend_override=METERED,
        lane_origin=lane_origin,
        watch_required=False,
        watch_override=True,
    )


def _admit(config):
    return queue_admission.admit_once(lane=LOCAL, config_for=lambda _pointer: config)


def test_a_review_following_its_run_rebinds_off_a_held_lane(
    repository, routing
) -> None:
    """The reflex's own route yields to the hold, with no picker_selection."""
    config, state = routing
    state["metered_held"] = True
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "reflex-reviewed", lane_origin=REVIEW_FOLLOWS_RUN)
    request_id = held.value.queued["run_id"]
    queued = runs.read_pointer(request_id)
    assert queued["named_route"]["set_by"] == REVIEW_FOLLOWS_RUN
    assert "picker_selection" not in queued
    assert queued["backend"] == METERED

    routing_tests._open_local(config)
    result = _admit(config)
    assert result["launched"] == [request_id]
    run = runs.read_pointer(request_id)
    assert run["backend"] == LOCAL
    assert run["route_fallback"]["from_backend"] == METERED
    assert run["route_fallback"]["to_backend"] == LOCAL
    assert "codex pace window is held" in run["route_fallback"]["reason"]


def test_a_caller_named_route_holds_without_rebinding(repository, routing) -> None:
    """A lane a coordinator named is authoritative; the hold is its answer."""
    config, state = routing
    state["metered_held"] = True
    with pytest.raises(LaneHeld) as held:
        _dispatch(repository, config, "caller-named")
    request_id = held.value.queued["run_id"]
    request = runs.read_pointer(request_id)
    assert request["named_route"]["set_by"] == "dispatch"
    assert request["backend"] == METERED
    assert not request.get("route_fallback")
    result = _admit(config)
    assert request_id in result["held"]
    assert runs.read_pointer(request_id)["backend"] == METERED


@pytest.fixture()
def isolated_project(tmp_path, monkeypatch):
    return reflex_tests.isolated_project.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.arms_watch_producer
def test_a_run_review_records_the_lane_it_followed(
    isolated_project, monkeypatch
) -> None:
    """A run review's lane was the reviewed run's, and its route says so.

    This is the write side of the rebind above: without it the queue cannot
    tell a lane the reflex followed from one a coordinator named, and a review
    sitting on a held lane waits for the reset it was never bound to.

    The assertion reads the review's own pointer rather than the arguments the
    composer handed dispatch. What admission reads is the written
    ``named_route``, so a composer that passed the origin and a dispatch that
    dropped it would still satisfy a capture of the call and leave the queue
    unable to tell the two routes apart.
    """
    config_home, repo = isolated_project
    record = reflex_tests._scoring_pointer(config_home, repo, "r-followed")
    record["backend"] = reflex_tests.OTHER_BACKEND

    def budget(**kwargs):
        held = kwargs["backend_name"] == reflex_tests.OTHER_BACKEND
        return {
            "held": held,
            "backend": kwargs["backend_name"],
            "reason": "beta pace window is held",
            "state": {"resets_at": "2026-11-18T09:00:00Z"},
            "warnings": [],
        }

    monkeypatch.setattr(dispatch_module, "_budget_verdict", budget)
    try:
        with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
            recovery.dispatch_review_for_run(
                record,
                config=reflex_tests.CONFIG,
                launcher=lambda *a, **k: os.getpid(),
            )
        queued = [
            pointer
            for pointer in runs.list_live(project="sample", phase="queued")
            if str(pointer.get("node", {}).get("id", "")).startswith(
                recovery.REVIEW_NODE_PREFIX
            )
        ]
        assert len(queued) == 1, queued
        assert queued[0]["named_route"] == {
            "backend": reflex_tests.OTHER_BACKEND,
            "set_by": REVIEW_FOLLOWS_RUN,
        }
    finally:
        reflex_tests._release_watcher()
