"""Codex window publication from the session homes used by crew runs."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from reckon.crew import paid_lanes


def _rollout(root: Path, observed: datetime, used_percent: float) -> None:
    folder = root / observed.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{observed.strftime('%H%M%S')}.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": observed.isoformat(),
                "type": "event_msg",
                "payload": {
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {
                            "used_percent": used_percent,
                            "window_minutes": 10_080,
                            "resets_at": int(
                                (observed + timedelta(days=3)).timestamp()
                            ),
                        },
                    }
                },
            }
        )
        + "\n"
    )


def test_publisher_uses_newest_run_home_without_a_project(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    crew_home = tmp_path / "crew-home"
    main_sessions = crew_home / "codex-home" / "sessions"
    run_sessions = crew_home / "crew" / "runs" / "r-current" / "codex-home" / "sessions"
    _rollout(main_sessions, now - timedelta(minutes=25), 39.0)
    _rollout(run_sessions, now - timedelta(minutes=2), 42.0)
    _rollout(run_sessions, now + timedelta(minutes=1), 80.0)
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    from reckon import flight

    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *_a, **_k: SimpleNamespace(
            config={
                # The flat backend block lane expansion writes: each profile of
                # the provider carries the group whose quota it draws, which is
                # the declaration the publisher joins profiles to an account on.
                "backends": {
                    name: {"budget_group": "codex-sub"}
                    for name in ("codex", "codex-astra", "codex-luna")
                }
            }
        ),
    )
    target = tmp_path / "paid-lanes.json"
    assert paid_lanes.main(["--once", "--path", str(target)]) == 0
    accounts = json.loads(target.read_text())["accounts"]
    for name in ("codex", "codex-astra", "codex-luna"):
        account = accounts[name]
        assert account["state"] == "observed"
        assert account["source"] == "rollout"
        week = account["windows"]["seven_day"]
        assert week["utilisation"] == 0.42
        assert week["source"] == "rollout"
        assert datetime.fromisoformat(week["observed_at"]) == now - timedelta(minutes=2)


def test_rollout_scan_excludes_old_readings(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    crew_home = tmp_path / "crew-home"
    main_sessions = crew_home / "codex-home" / "sessions"
    _rollout(main_sessions, now - timedelta(days=8), 61.0)
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    sources = paid_lanes.gather_sources(["codex"], moment=now, pointers=[], records=[])
    assert sources["codex"] == []
