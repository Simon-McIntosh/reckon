"""A discarded request is not written back by the pass that was holding it.

The admission pass holds a queued request in memory while it attempts a launch
and writes the request back when the launch does not survive. A ``crew discard``
issued during that pass removes the pass's pointer and records the departure;
the restore then wrote the request back and undid the discard. A request that
never launched has no run directory, so its departure is recorded in the shared
discards spool instead of a run directory the discard must not create. These
tests bind the restore to that record: a discarded request stays gone whether or
not it has a run directory, and a request nobody discarded is still restored.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

from pathlib import Path

from reckon.crew import promotion, queue_admission, runs
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    _config_for,
    _failing_launcher,
    _queue,
    admit_repo,
)


def _attempted_request(repository: Path, run_id: str) -> None:
    """Queue a request that already has a run directory behind it.

    A queued request that has been attempted before carries a run directory,
    which is where a discard records its departure. The request in the field
    that came back had cycled through the queue, so its home already existed.
    """
    runs.queue_dispatch(_queue(repository, node_id="queued", run_id=run_id))
    runs.run_dir(run_id).mkdir(parents=True, exist_ok=True)


def _discard_mid_pass(monkeypatch) -> None:
    """Make the pass discard the request it holds, before its restore runs.

    The pass snapshots the queued pointer and then releases it inside
    ``_launch_queued``. Wrapping that step issues the discard after the snapshot
    but before the release, which is the window the field showed.
    """
    real_launch = queue_admission._launch_queued

    def discard_then_launch(pointer, **kwargs):
        # The operator discards the request mid-pass; its pointer is still the
        # one the pass is holding and has not yet released.
        promotion.discard(str(pointer["run_id"]))
        return real_launch(pointer, **kwargs)

    monkeypatch.setattr(queue_admission, "_launch_queued", discard_then_launch)


def test_a_request_discarded_during_the_pass_is_not_written_back(
    admit_repo: Path, monkeypatch
) -> None:
    """A discard made while the pass holds the request stands.

    The restore must read the discard's record and leave the pointer removed.
    """
    _, resolve = _config_for()
    run_id = "r-discarded"
    _attempted_request(admit_repo, run_id)

    _discard_mid_pass(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert promotion.discard_recorded(run_id), "the discard left a record"
    assert not runs.pointer_path(run_id).exists(), (
        "a discarded request must not be written back by the pass"
    )
    assert run_id in result["failed"]


def test_a_request_with_no_run_directory_is_found_discarded(
    admit_repo: Path, monkeypatch
) -> None:
    """A queued review that never launched has no run directory.

    The discard must leave a durable record the restore can read even then, and
    must not create the run directory to do it — recreating a run's home is what
    a discard exists to avoid.
    """
    _, resolve = _config_for()
    run_id = "r-no-home"
    runs.queue_dispatch(_queue(admit_repo, node_id="queued", run_id=run_id))
    assert not runs.run_dir(run_id).exists(), "the request never launched"

    _discard_mid_pass(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert not runs.pointer_path(run_id).exists(), (
        "a discarded request must not be written back by the pass"
    )
    assert promotion.discard_recorded(run_id), "the discard left a readable record"
    assert not runs.run_dir(run_id).exists(), "the discard must not create a run dir"
    assert run_id in result["failed"]


def test_a_request_nobody_discarded_is_still_restored(admit_repo: Path) -> None:
    """A failed launch whose request nobody discarded is written back queued."""
    _, resolve = _config_for()
    run_id = "r-kept"
    _attempted_request(admit_repo, run_id)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert not promotion.discard_recorded(run_id)
    pointer = runs.read_pointer(run_id)
    assert pointer["phase"] == "queued"
    assert run_id in result["failed"]
