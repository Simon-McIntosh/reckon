"""The hook's cheap deferral read sees both shapes a deferral is recorded in.

A live run keeps its deferral under the pointer's ``acknowledgement`` field; a
run already promoted has no pointer, so its deferral is a file of its own whose
top level is the deferral. A reader that knows only the first shape takes the
prompt path's early return and the stop blocks on a duty the coordinator
excused.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import obligation_snapshot

PROJECT = "deferral-fixture"


def _crew_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return obligation_snapshot.crew_home()


def _write(home: Path, name: str, record: dict) -> None:
    path = home / "acknowledgements" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_a_promoted_runs_deferral_is_seen(monkeypatch, tmp_path: Path) -> None:
    home = _crew_home(monkeypatch, tmp_path)
    _write(
        home,
        "r-promoted.json",
        {
            "run_id": "r-promoted",
            "project": PROJECT,
            "reason": "the run is reviewed elsewhere",
            "until": "2099-01-01T00:00:00+00:00",
            "recorded_at": "2026-11-18T00:00:00+00:00",
        },
    )
    assert obligation_snapshot.deferrals_recorded(PROJECT) is True


def test_a_live_pointers_deferral_is_seen(monkeypatch, tmp_path: Path) -> None:
    home = _crew_home(monkeypatch, tmp_path)
    path = home / "live" / "r-live.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "acknowledgement": {"reason": "held", "until": "2099-01-01"},
            }
        ),
        encoding="utf-8",
    )
    assert obligation_snapshot.deferrals_recorded(PROJECT) is True


def test_a_record_naming_no_deferral_is_not_one(monkeypatch, tmp_path: Path) -> None:
    home = _crew_home(monkeypatch, tmp_path)
    _write(home, "r-plain.json", {"run_id": "r-plain", "project": PROJECT})
    assert obligation_snapshot.deferrals_recorded(PROJECT) is False


def test_another_projects_deferral_is_not_this_projects(
    monkeypatch, tmp_path: Path
) -> None:
    home = _crew_home(monkeypatch, tmp_path)
    _write(
        home,
        "r-other.json",
        {"run_id": "r-other", "project": "other-fixture", "until": "2099-01-01"},
    )
    assert obligation_snapshot.deferrals_recorded(PROJECT) is False
