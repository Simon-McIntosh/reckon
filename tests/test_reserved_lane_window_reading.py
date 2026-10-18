"""A qualifying pick can refresh its reserved account's five-hour window."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reckon import budget
from reckon.crew import paid_lanes, window_reading
from reckon.crew.picker import snapshot
from tests.test_picker_reserved_lanes import (
    CONFIG,
    HARDEST,
    _offered,
    _request,
    _reserved_reason,
)

pytest_plugins = ("tests.test_picker_reserved_lanes",)

GROUP_PACE = budget.group_pace


def _stream(utilisation=None, *, status="allowed_warning"):
    now = datetime.now(UTC)
    info = {"status": status, "utilization": 1.21}
    if utilisation is not None:
        info["unifiedWindows"] = {
            "five_hour": {
                "utilization": utilisation,
                "resetsAt": int((now + timedelta(hours=2)).timestamp()),
            }
        }
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info}) + "\n"


def _arrange(monkeypatch, tmp_path):
    config = copy.deepcopy(CONFIG)
    config["backends"]["claude-opus"]["budget_check"] = True
    path = tmp_path / "paid-lanes.json"
    monkeypatch.setenv("RECKON_PAID_LANES_DOCUMENT", str(path))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(snapshot.budget, "group_pace", GROUP_PACE)

    def view(project, config, root, records, *, cached_only=False, session=""):
        return snapshot._compose_budget_report(
            project,
            config,
            root,
            records,
            datetime.now(UTC),
            cached_only=cached_only,
            session=session,
        )

    monkeypatch.setattr(snapshot, "budget_view", view)
    return config, path


def _pick(root, config, *, capability=HARDEST):
    return snapshot.candidates(
        _request(capability=capability),
        config,
        root,
        records=[],
        verdict_inputs={},
    )


@pytest.mark.parametrize("used,offered", [(0.42, True), (0.60, False)])
def test_hardest_pick_reads_a_persisted_five_hour_event(
    live_facts, repo, monkeypatch, tmp_path, used, offered
):
    config, path = _arrange(monkeypatch, tmp_path)
    calls = []

    def probe(_backend, _root):
        calls.append(1)
        return _stream(used)

    monkeypatch.setattr(paid_lanes, "_run_reserved_probe", probe)
    first = _pick(repo, config)
    second = _pick(repo, config)

    assert len(calls) == 1
    assert ("claude-opus" in _offered(first)) is offered
    assert ("claude-opus" in _offered(second)) is offered
    five = paid_lanes.read_document(path)["accounts"]["claude-opus"]["windows"][
        "five_hour"
    ]
    assert five["source"] == "reserved-probe"
    assert five["utilisation"] == pytest.approx(used)
    assert datetime.fromisoformat(five["observed_at"]).tzinfo is not None
    if not offered:
        assert "60% used" in _reserved_reason(first, "claude-opus")


def test_no_window_event_stays_excluded_and_does_not_probe_twice(
    live_facts, repo, monkeypatch, tmp_path
):
    config, path = _arrange(monkeypatch, tmp_path)
    calls = []

    def probe(_backend, _root):
        calls.append(1)
        return _stream()

    monkeypatch.setattr(paid_lanes, "_run_reserved_probe", probe)
    first = _pick(repo, config)
    second = _pick(repo, config)

    assert len(calls) == 1
    assert "unknown or stale" in _reserved_reason(first, "claude-opus")
    assert "unknown or stale" in _reserved_reason(second, "claude-opus")
    account = paid_lanes.read_document(path)["accounts"]["claude-opus"]
    assert account["windows"]["five_hour"]["utilisation"] is None
    assert account["reserved_probe"]["failure"]


def test_a_refused_probe_keeps_the_lane_closed(live_facts, repo, monkeypatch, tmp_path):
    config, path = _arrange(monkeypatch, tmp_path)
    monkeypatch.setattr(
        paid_lanes, "_run_reserved_probe", lambda *_: _stream(0.42, status="rejected")
    )

    options = _pick(repo, config)

    assert "unknown or stale" in _reserved_reason(options, "claude-opus")
    account = paid_lanes.read_document(path)["accounts"]["claude-opus"]
    assert account["windows"]["five_hour"]["utilisation"] is None
    assert account["reserved_probe"]["failure"]


def test_the_probe_request_is_tool_free_and_bounded(monkeypatch, tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=_stream(0.42))

    monkeypatch.setattr(paid_lanes.subprocess, "run", run)
    stream = paid_lanes._run_reserved_probe(
        {"launch": "cli", "command": "claude", "model": "opus"}, tmp_path
    )

    assert "rate_limit_event" in stream
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[-2:] == ["--tools", ""]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.05"
    assert argv[argv.index("--model") + 1] == "opus"
    assert kwargs["input"] == "Reply with OK."
    assert kwargs["timeout"] == 60


def test_routine_pick_never_spends_the_reserved_probe(
    live_facts, repo, monkeypatch, tmp_path
):
    config, _path = _arrange(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        paid_lanes, "_run_reserved_probe", lambda *_: calls.append(1) or _stream(0.1)
    )

    options = _pick(repo, config, capability={})

    assert calls == []
    assert "claude-opus" not in _offered(options)


def test_a_fresh_weekly_figure_does_not_stand_in_for_the_five_hour_window(
    live_facts, repo, monkeypatch, tmp_path
):
    config, path = _arrange(monkeypatch, tmp_path)
    now = datetime.now(UTC)
    week = window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="seven_day",
                utilisation=0.2,
                observed_at=now,
                age_seconds=0,
                resets_at=(now + timedelta(days=2)).isoformat(),
            ),
        ),
        observed_at=now,
    )
    paid_lanes.write_document_atomically(
        paid_lanes.compose_document(
            config["backends"],
            sources={"claude-opus": [("receipt", week)]},
            moment=now,
        ),
        path,
    )
    calls = []

    def probe(_backend, _root):
        calls.append(1)
        return _stream(0.42)

    monkeypatch.setattr(paid_lanes, "_run_reserved_probe", probe)
    options = _pick(repo, config)

    assert calls == [1]
    assert "claude-opus" in _offered(options)
    windows = paid_lanes.read_document(path)["accounts"]["claude-opus"]["windows"]
    assert windows["five_hour"]["source"] == "reserved-probe"
    assert windows["seven_day"]["utilisation"] == pytest.approx(0.2)


def test_cached_pick_leaves_an_unknown_window_closed_without_a_request(
    live_facts, repo, monkeypatch, tmp_path
):
    config, _path = _arrange(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        paid_lanes, "_run_reserved_probe", lambda *_: calls.append(1) or _stream(0.1)
    )

    options = snapshot.candidates(
        _request(capability=HARDEST),
        config,
        repo,
        records=[],
        verdict_inputs={},
        cached_only=True,
    )

    assert calls == []
    assert "unknown or stale" in _reserved_reason(options, "claude-opus")


def test_publisher_preserves_the_probe_and_its_attempt_clock(
    live_facts, repo, monkeypatch, tmp_path
):
    config, path = _arrange(monkeypatch, tmp_path)
    monkeypatch.setattr(paid_lanes, "_run_reserved_probe", lambda *_: _stream(0.42))
    _pick(repo, config)
    prior = paid_lanes.read_document(path)["accounts"]["claude-opus"]

    published = paid_lanes.publish_document(
        config,
        {},
        path=path,
        probe_runner=lambda _probe: {},
        local_lane={"state": "unknown"},
    )

    account = published["accounts"]["claude-opus"]
    assert account["windows"]["five_hour"]["utilisation"] == pytest.approx(0.42)
    assert account["reserved_probe"] == prior["reserved_probe"]


def test_budget_view_reloads_the_document_after_the_probe(
    live_facts, repo, monkeypatch, tmp_path
):
    config = copy.deepcopy(CONFIG)
    config["backends"]["claude-opus"]["budget_check"] = True
    path = tmp_path / "paid-lanes.json"
    monkeypatch.setenv("RECKON_PAID_LANES_DOCUMENT", str(path))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    monkeypatch.setattr(snapshot.budget, "group_pace", GROUP_PACE)
    now = datetime.now(UTC)
    cache = tmp_path / "cache"

    before = snapshot.budget_view(
        "example", config, repo, [], now=now, cache_root=cache
    )
    changed = paid_lanes.refresh_reserved_window(
        config,
        "claude-opus",
        config["backends"]["claude-opus"],
        root=repo,
        moment=now,
        runner=lambda *_: _stream(0.42),
    )
    after = snapshot.budget_view("example", config, repo, [], now=now, cache_root=cache)

    assert changed is True
    before_group = next(g for g in before["groups"] if g["group"] == "claude")
    after_group = next(g for g in after["groups"] if g["group"] == "claude")
    assert before_group["clocks"]["five_hour"]["utilisation"] is None
    assert after_group["clocks"]["five_hour"]["utilisation"] == pytest.approx(0.42)
    assert after_group["source"] == budget.WINDOW_SOURCE_DOCUMENT
