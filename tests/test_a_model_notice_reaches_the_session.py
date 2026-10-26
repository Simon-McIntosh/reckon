"""A model notice reaches the session without anyone running the verb.

The producer refreshes the model catalogue check on its own cadence, the
coordinator obligations hook shows one line while a notice is unacknowledged,
and a released notice on an opted-in lane is probed once. Every case runs under
a temporary crew home with the fetch and the probe command stubbed, so no
network call is made and the workstation's real crew home is never written.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import model_catalogue, recovery_watch, runs
from reckon.hooks import coordinator_obligations
from tests.conftest import real_store_home, real_store_is_not_a_fixture_target

PROJECT = "model-notice"


class _StopTickingError(Exception):
    """Ends the producer after the case's steps have run."""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, asserted never to be the real one."""
    real_home = real_store_home().resolve()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    real_store_is_not_a_fixture_target(tmp_path / "home")
    assert model_catalogue.snapshot_path().is_relative_to(tmp_path)
    assert not model_catalogue.snapshot_path().is_relative_to(real_home)
    # The hook reads the notices through the snapshot reader; the catalogue
    # reaches the same file through its own delegating reader. Both must name
    # the same path, so a future divergence in either reddens this gate.
    assert (
        coordinator_obligations.snapshot_module().model_catalogue_snapshot_path()
        == model_catalogue.snapshot_path()
    )
    return tmp_path


def _listing(tmp_path: Path) -> Path:
    """A public list whose newer ids raise one released notice per lane."""
    path = tmp_path / "list.json"
    models = [
        {"id": "anthropic/claude-sonnet-5.5", "created": 100},
        {"id": "anthropic/claude-sonnet-9.9", "created": 200},
        {"id": "openai/gpt-6-sol", "created": 100},
        {"id": "openai/gpt-6.1-sol", "created": 200},
        {"id": "google/gem-9", "created": 200},
    ]
    path.write_text(json.dumps({"data": models}))
    return path


def _served(tmp_path: Path) -> Path:
    path = tmp_path / "served.json"
    path.write_text(
        json.dumps({"models": [{"slug": "gpt-6-sol", "visibility": "list"}]})
    )
    return path


def _config(tmp_path: Path) -> dict:
    """Lanes covering each probe branch: opt-in alone, served file, no opt-in.

    Every lane's command is a path that cannot exist, so a probe no test
    stubbed fails instantly as an unrunnable command instead of spawning the
    real CLI. Only the case about probing means to probe, and it passes its own
    ``probe_run``.
    """
    return {
        "model_catalogue": {
            "list_url": "https://example.invalid/models",
            "refresh_age": 86400,
            "watch": {
                "anthropic/claude-": "claude",
                "openai/gpt-": "codex",
                "google/gem-": "other",
            },
        },
        "lanes": {
            "claude": {
                "command": str(tmp_path / "no-lane-command"),
                "probe_new_models": True,
                "models": {
                    "sonnet": {
                        "model": "claude-sonnet-5-5",
                        "catalog_id": "anthropic/claude-sonnet-5.5",
                    }
                },
            },
            "codex": {
                "command": str(tmp_path / "no-lane-command"),
                "probe_new_models": True,
                "served_models": str(_served(tmp_path)),
                "models": {
                    "sol": {"model": "gpt-6-sol", "catalog_id": "openai/gpt-6-sol"}
                },
            },
            "other": {
                "command": str(tmp_path / "no-lane-command"),
                "models": {"gem": {"model": "gem-1", "catalog_id": "google/gem-1"}},
            },
        },
    }


def _install_flight(monkeypatch: pytest.MonkeyPatch, config: dict) -> None:
    """Resolve every project's flight to the fixture config, no files read."""
    monkeypatch.setattr(
        model_catalogue.flight,
        "resolve",
        lambda *args, **kwargs: SimpleNamespace(config=config),
    )


def _install_fetch(
    monkeypatch: pytest.MonkeyPatch, listing: Path, calls: list[int]
) -> None:
    def read_list(_url: str) -> dict:
        calls.append(1)
        return json.loads(listing.read_text())

    monkeypatch.setattr(model_catalogue, "_fetch_list", read_list)


def _drive_producer(
    monkeypatch: pytest.MonkeyPatch, *, boundaries: int, sleeper_seen: list[int]
) -> None:
    """Run the producer for ``boundaries`` quiet beats, then stop it.

    The seat, the lease renewal and the document sweeps are stubbed so the case
    exercises only the clock-cadence block that carries the catalogue refresh.
    """

    @contextmanager
    def _registration(project: str, stall_window: str, sleeper=None) -> Iterator[tuple]:
        yield (True, "watcher")

    class _Producers(dict):
        """The seat's registry, interned with the record the seat itself builds.

        A case that stubs the seat still registers a producer the loop and its
        teardown read; the real record keeps a field the producer gains later
        from arriving as a missing attribute at teardown.
        """

        def __missing__(self, key):
            value = runs._WatchStreamProducer(
                path=runs.watch_stream_path(key),
                known={},
                stall_window=runs.DEFAULT_WATCH_STALL_WINDOW,
            )
            self[key] = value
            return value

    monkeypatch.setattr(
        recovery_watch, "_refuse_unresolvable_watch", lambda *a, **k: None
    )
    monkeypatch.setattr(recovery_watch, "_watch_registration", _registration)
    monkeypatch.setattr(
        recovery_watch, "_recreate_unlinked_registration", lambda *a, **k: True
    )
    monkeypatch.setattr(
        recovery_watch.runs, "renew_watch_host_lease", lambda *a, **k: True
    )
    monkeypatch.setattr(recovery_watch.runs, "_WATCH_STREAM_PRODUCERS", _Producers())
    monkeypatch.setattr(
        recovery_watch, "WATCHER_SWEEP_INTERVAL_SECONDS", 0.0, raising=False
    )
    monkeypatch.setattr(
        recovery_watch.runs, "sweep_unfollowed_sessions", lambda *a, **k: None
    )
    monkeypatch.setattr(recovery_watch, "_stop_delivered_reviews", lambda *a, **k: None)
    monkeypatch.setattr(recovery_watch, "_sweep_lift_rows", lambda project, seen: seen)
    monkeypatch.setattr(recovery_watch, "handover_run_ids", lambda project: set())
    monkeypatch.setattr(recovery_watch, "list_live", lambda **k: [])

    def sleeper(_interval: float) -> None:
        sleeper_seen.append(1)
        if len(sleeper_seen) > boundaries:
            raise _StopTickingError

    generator = recovery_watch.watch_ticker(PROJECT, poll_interval=0.0, sleeper=sleeper)
    with contextlib.suppress(_StopTickingError):
        list(generator)


def test_a_sweep_past_the_refresh_age_fetches_once_and_within_it_none(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One producer fetch past the age; a sweep inside the age fetches none."""
    config = _config(home)
    _install_flight(monkeypatch, config)
    calls: list[int] = []
    _install_fetch(monkeypatch, _listing(home), calls)
    _drive_producer(monkeypatch, boundaries=3, sleeper_seen=[])
    assert len(calls) == 1, calls


def test_two_producers_sweeping_at_once_fetch_once(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock makes a simultaneous second producer read, not fetch."""
    config = _config(home)
    listing = _listing(home)
    calls: list[int] = []
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(_url: str) -> dict:
        calls.append(1)
        entered.set()
        assert release.wait(10), "the peer never released the fetch"
        return json.loads(listing.read_text())

    monkeypatch.setattr(model_catalogue, "_fetch_list", slow_fetch)

    def first() -> None:
        model_catalogue.check_model_catalogue(PROJECT, config=config, fetch=slow_fetch)

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(10), "the first producer never reached its fetch"
    # The second producer sweeps while the first holds the lock.
    model_catalogue.check_model_catalogue(PROJECT, config=config, fetch=slow_fetch)
    release.set()
    worker.join(10)
    # The worker must be finished before the fixture home is torn down: a
    # thread still running when the temporary home is restored would resolve
    # the real crew home on its next write.
    assert not worker.is_alive(), "the first producer did not finish"
    assert len(calls) == 1, calls


def test_a_pending_notice_is_one_line_and_an_acknowledged_one_is_silent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook shows one line naming the count and the view while a notice stands."""
    snapshot = model_catalogue.snapshot_path()
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps(
            {
                "notices": [
                    {"id": "n1", "kind": "released", "acknowledgement": None},
                    {"id": "n2", "kind": "released", "acknowledgement": None},
                ]
            }
        )
    )
    payload = {"project": PROJECT, "session": "s1", "obligations": [], "summary": {}}
    rendered = coordinator_obligations.format_checklist(payload)
    notice_lines = [line for line in rendered.splitlines() if "model notice" in line]
    assert len(notice_lines) == 1, rendered
    line = notice_lines[0]
    assert "2" in line
    assert 'crew(view="models")' in line

    document = json.loads(snapshot.read_text())
    for notice in document["notices"]:
        notice["acknowledgement"] = {
            "reason": "reviewed",
            "at": "2026-10-20T00:00:00Z",
        }
    snapshot.write_text(json.dumps(document))
    silent = coordinator_obligations.format_checklist(payload)
    assert "model notice" not in silent, silent


def test_a_released_notice_is_probed_once_on_an_opted_in_lane(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the opt-in, served-file-free lane is probed, and only once."""
    config = _config(home)
    listing = _listing(home)
    probes: list[list[str]] = []

    def read_list(_url: str) -> dict:
        return json.loads(listing.read_text())

    def probe_run(argv: list[str]) -> tuple[int, str]:
        probes.append(list(argv))
        return (0, "ok\n")

    snapshot = model_catalogue.check_model_catalogue(
        PROJECT, config=config, fetch=read_list, force=True, probe_run=probe_run
    )
    released = {
        item["model_id"]: item
        for item in snapshot["notices"]
        if item["kind"] == "released"
    }
    assert "anthropic/claude-sonnet-9.9" in released, released
    assert len(probes) == 1, probes
    assert "claude-sonnet-9-9" in probes[0], probes
    record = released["anthropic/claude-sonnet-9.9"]["probe"]
    assert record["result"] == "served"
    assert record["reply"] == "ok"
    # Neither the served-file lane nor the lane that did not opt in was probed.
    assert all("gpt-6" not in " ".join(argv) for argv in probes), probes
    assert all("gem" not in " ".join(argv) for argv in probes), probes

    # A second refresh probes nothing: the notice already carries its result.
    model_catalogue.check_model_catalogue(
        PROJECT, config=config, fetch=read_list, force=True, probe_run=probe_run
    )
    assert len(probes) == 1, probes


def test_a_refresh_that_raises_leaves_the_producer_loop_running(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh that raises is printed, and the seat keeps sweeping."""
    config = _config(home)
    _install_flight(monkeypatch, config)

    def raising(*_args, **_kwargs):
        raise RuntimeError("fixture refresh failed")

    monkeypatch.setattr(model_catalogue, "check_model_catalogue", raising)
    seen: list[int] = []
    _drive_producer(monkeypatch, boundaries=3, sleeper_seen=seen)
    assert len(seen) >= 3, seen
