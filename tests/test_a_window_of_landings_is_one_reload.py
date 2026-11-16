"""Standing services batch code changes and publish their reload schedule."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from reckon import checkout_trust, flight
from reckon.crew import follower_reload, station
from reckon.crew.admission import service


def _drive_station(root, monkeypatch, *, immediate=False):
    now = [0.0]
    newest = ["0" * 40]
    reloads, exports = [], []
    tick_started = [0.0]
    last_reload = [0.0]
    monkeypatch.setenv("RECKON_HOME", str(root))
    monkeypatch.setattr(follower_reload, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(station, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(
        checkout_trust, "source_code_identity", lambda *a, **k: newest[0]
    )
    active = station.StationReloader(root)
    if hasattr(active, "start_window"):
        active.start_window()

    def replace(code):
        reloads.append((now[0], bool(exports and exports[-1] > last_reload[0])))
        active.identity = code
        window = getattr(active, "window", None)
        if window is not None:
            window.reloaded(code)
        tick_started[0] = last_reload[0] = now[0]

    monkeypatch.setattr(active, "_replace", replace)
    for minute in range(1, 61):
        now[0] = minute * 60.0
        if minute % 5 == 0:
            newest[0] = f"{minute // 5:040x}"
        if immediate and minute == 15:
            follower_reload.request_service_reload("station", "urgent correction")
        if now[0] - tick_started[0] >= 7 * 60:
            exports.append(now[0])
            tick_started[0] = now[0]
            window = getattr(active, "window", None)
            if window is not None:
                window.exported()
        active.poll()
    print(
        f"station changes=12 reloads={len(reloads)} completed_exported_ticks={len(exports)}"
    )
    return reloads


def _drive_admission(root, monkeypatch, *, immediate=False):
    from reckon.crew import fleet_supervisor, promotion_requests
    from reckon.crew.admission import review_sweep

    now = [0.0]
    reloads, exports = [], []
    monkeypatch.setenv("RECKON_HOME", str(root))
    monkeypatch.delenv("RECKON_SERVICE_PASS_PATH", raising=False)
    monkeypatch.setattr(follower_reload, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(
        service, "_admit_code_stamp", lambda: f"{int(now[0] // 300):040x}"
    )
    monkeypatch.setattr(service, "_checkout_code_is_trusted", lambda: True)
    monkeypatch.setattr(
        service,
        "_reexec_into_current_code",
        lambda: reloads.append((now[0], bool(exports))),
    )
    monkeypatch.setattr(fleet_supervisor, "service_pass", nullcontext)

    def empty(*a, **k):
        return None

    monkeypatch.setattr(
        promotion_requests,
        "PromotionService",
        lambda **k: SimpleNamespace(
            start_refill=empty,
            stop_refill=empty,
            tick=empty,
            begin_drain=empty,
            wait_for_in_flight=empty,
        ),
    )
    monkeypatch.setattr(
        review_sweep,
        "ReviewSweep",
        lambda **k: SimpleNamespace(start=empty, stop=empty),
    )
    monkeypatch.setattr(
        service._landing,
        "_LandingWorker",
        lambda *a: SimpleNamespace(tick=empty, settle=empty, stop=empty, last={}),
    )
    monkeypatch.setattr(service._release, "_release_tick", dict)
    monkeypatch.setattr(service._selection, "admit_once", lambda **k: {})
    monkeypatch.setattr(
        service._verdicts, "_record_pass_verdicts", lambda row, **k: row
    )
    monkeypatch.setattr(
        service._verdicts, "_publish_pass_verdicts", lambda row: exports.append(now[0])
    )

    def pause(seconds):
        now[0] += seconds
        if immediate and now[0] == 900:
            follower_reload.request_service_reload("admission", "urgent correction")
        if now[0] > 3600:
            raise KeyboardInterrupt

    monkeypatch.setattr(service, "_pause", pause)
    service.admit_until_stopped(interval=60)
    print(
        f"admission changes=12 reloads={len(reloads)} completed_exported_ticks={len(exports)}"
    )
    return reloads


def test_twelve_changes_take_two_scheduled_reloads_and_one_requested_reload(
    tmp_path, monkeypatch
):
    for drive in (_drive_station, _drive_admission):
        for immediate, expected in ((False, [1800, 3600]), (True, [900, 1800, 3600])):
            with monkeypatch.context() as patch:
                root = tmp_path / f"{drive.__name__}-{immediate}"
                result = drive(root, patch, immediate=immediate)
                assert [at for at, _ in result] == expected, result
                assert all(exported for _, exported in result), result
                if immediate:
                    name = "station" if drive is _drive_station else "admission"
                    record = follower_reload.service_reload_status(name)
                    assert record["last_request"]["reason"] == "urgent correction"
                    assert record["last_request"]["requested_by"]


def test_status_names_running_seen_and_next_reload_and_requires_an_export(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    now = [0.0]
    window = follower_reload.ReloadWindow("station", "running", clock=lambda: now[0])
    assert flight.service_reload_window_seconds({}) == 1800
    assert (
        flight.service_reload_window_seconds({"service_reload_window_seconds": 1200})
        == 1200
    )
    now[0] = 1800
    assert not window.due("latest"), (
        "a due window cannot replace an image before its tick exports"
    )
    status = follower_reload.service_reload_status("station")
    assert (
        status["running_code"],
        status["newest_seen"],
        status["next_reload_at"],
    ) == ("running", "latest", 1800)
    window.exported()
    assert window.due("latest")
    window.reloaded("latest")
    with (
        pytest.raises(OSError, match="exec refused"),
        window.replacement("unstarted"),
    ):
        raise OSError("exec refused")
    assert window.running == "latest"
    assert window.reload_count == 1
    assert follower_reload.service_reload_status("station")["running_code"] == "latest"
    replacement = follower_reload.ReloadWindow(
        "station", "latest", clock=lambda: now[0]
    )
    assert replacement.next_at == 3600
    assert replacement.exported_at is None
    with pytest.raises(ValueError, match="requires a reason"):
        follower_reload.request_service_reload("station", " ")
    with pytest.raises(ValueError, match="simple name"):
        follower_reload.request_service_reload("../station", "urgent correction")
