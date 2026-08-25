from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CREW = ROOT / "docs" / "ui" / "crew.jsx"


def _project_run(run: dict) -> dict:
    source = CREW.read_text(encoding="utf-8")
    helpers = source.split("function CrewRunCard", 1)[0]
    script = f"""
{helpers}
console.log(JSON.stringify(crewCardProjection({json.dumps(run)})));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_long_done_when_is_one_line_without_hiding_phase_or_activity() -> None:
    contract = "measured evidence remains visible; " * 39
    assert len(contract) >= 1_300
    run = {
        "run_id": "run-visible",
        "phase": "stalled",
        "gate": contract,
        "last_activity": "2026-08-24T13:27:35Z",
    }

    card = _project_run(run)
    source = CREW.read_text(encoding="utf-8")

    assert card["doneWhen"] == contract
    assert card["phase"] == "stalled"
    assert card["lastActivity"] != "—"
    assert '<details className="r-crew-done-when">' in source
    assert "-webkit-line-clamp:1" in source


def test_card_exposes_copyable_attach_command_only_in_connection_details() -> None:
    command = "ssh worker@compute -t 'zellij -s fleet attach'"
    card = _project_run(
        {
            "run_id": "run-attach",
            "session": "fleet",
            "host": "compute",
            "attach_command": command,
        }
    )
    source = CREW.read_text(encoding="utf-8")

    assert card["session"] == "fleet"
    assert card["host"] == "compute"
    assert card["attachCommand"] == command
    assert '<details className="r-crew-connect">' in source
    assert "navigator.clipboard?.writeText(card.attachCommand)" in source
    assert "Copy attach" in source


def test_cards_keep_poll_timing_and_use_structured_budget_and_gate_marks() -> None:
    card = _project_run(
        {
            "elapsed_seconds": 900,
            "time_budget": "30m",
            "gates_total": 4,
            "gates_done": 2,
            "current_gate": "focused tests",
            "gate_verdict": "passed",
        }
    )
    source = CREW.read_text(encoding="utf-8")

    assert card["budget"] == "30m 00s"
    assert card["budgetPercent"] == 50
    assert card["gates"] == {
        "total": 4,
        "completed": 2,
        "current": "focused tests",
        "verdict": "passed",
    }
    assert "const CREW_POLL_INTERVAL_MS = 3000;" in source
    assert "window.setInterval(poll, CREW_POLL_INTERVAL_MS)" in source
    assert "<table" not in source
