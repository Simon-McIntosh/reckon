"""A picker hold is written under the crew home, not in the project's checkout.

A hold is machine-local operational state, like the picks' timing log. Writing
it inside the project's checked-out tree left untracked files in every mounted
project and made ``git status`` walk them. A hold now lands under the crew home
keyed by project, its oversized payload beside the log it belongs to, and no
file is created under the project's own docs directory. A log left at the old
docs-side location is still read and its referenced payload still replays, so
the move loses no history.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from reckon.crew.picker.outcomes import (
    HOLD_PAYLOAD_INLINE_LIMIT,
    _read_holds,
    hold_request_digest,
    record_picker_hold,
    replay_hold,
)
from reckon.crew.runs import crew_home

PROJECT = "demo"

#: The crew-home layout, spelled out here rather than imported, so this test
#: collects and fails cleanly against a revision that predates the move rather
#: than erroring at import.
CREW_HOLD_DIR = "picker-holds"
HOLD_LOG_FILENAME = "picker-holds.jsonl"
HOLD_PAYLOAD_DIR = "picker-holds"


def _crew_log(project: str = PROJECT):
    return crew_home() / CREW_HOLD_DIR / project / HOLD_LOG_FILENAME


def _crew_payload_dir(project: str = PROJECT):
    return crew_home() / CREW_HOLD_DIR / project / HOLD_PAYLOAD_DIR


def _legacy_log(docs, project: str = PROJECT):
    return docs / "state" / project / HOLD_LOG_FILENAME


def _legacy_payload_dir(docs, project: str = PROJECT):
    return docs / "state" / project / HOLD_PAYLOAD_DIR


def _record(docs, *, node, state, questions, project: str = PROJECT) -> dict:
    return record_picker_hold(
        project=project,
        docs=docs,
        node=node,
        plan="",
        selection={"rendered_state": state, "questions": questions},
        reason="picker selected hold",
        now=datetime(2026, 10, 13, 12, tzinfo=UTC),
    )


def _crew_rows(project: str = PROJECT) -> list[dict]:
    path = _crew_log(project)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _seed_legacy(docs, *, node, project: str = PROJECT) -> dict:
    """Write a pre-move hold row with its oversized payload beside the log."""
    state = json.dumps({"notes": "y" * (HOLD_PAYLOAD_INLINE_LIMIT * 2)})
    questions = {"route": {"criteria": {"hold": {}}}}
    payload = json.dumps(
        {"state": state, "questions": questions},
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    digest = hold_request_digest(state, questions)
    payload_dir = _legacy_payload_dir(docs, project)
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / f"{digest}.json").write_bytes(payload)
    row = {
        "project": project,
        "node": node,
        "plan": "",
        "held_at": "2026-10-13T00:00:00+00:00",
        "confidence": 0.5,
        "reasons": ["picker selected hold"],
        "template_digest": "legacy-template",
        "request_digest": digest,
        "payload_bytes": len(payload),
        "payload_ref": f"{HOLD_PAYLOAD_DIR}/{digest}.json",
    }
    log = _legacy_log(docs, project)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    return row


def test_holds_and_their_payloads_are_written_under_the_crew_home(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    docs = tmp_path / "docs"

    small = _record(docs, node="small", state='{"k": 1}', questions={})
    large = _record(
        docs,
        node="large",
        state=json.dumps({"notes": "x" * (HOLD_PAYLOAD_INLINE_LIMIT * 2)}),
        questions={"route": {"criteria": {"hold: one": {}}}},
    )

    # The small request is inline; the large one is written beside the log.
    assert small.get("payload_ref") is None and "state" in small
    assert large["payload_bytes"] > HOLD_PAYLOAD_INLINE_LIMIT
    assert "payload_ref" in large and "state" not in large
    assert str(large["payload_ref"]).startswith(f"{HOLD_PAYLOAD_DIR}/")

    rows = _crew_rows()
    assert [row["node"] for row in rows] == ["small", "large"]
    assert (_crew_payload_dir() / large["payload_ref"].split("/")[-1]).is_file()

    # Nothing was created under the project's own docs directory.
    assert not docs.exists()

    replay = replay_hold(large, project=PROJECT)
    assert replay["request_digest"] == large["request_digest"]
    assert json.loads(replay["state"])["notes"].startswith("x")


def test_a_legacy_hold_replays_from_its_own_payload_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    docs = tmp_path / "docs"

    row = _seed_legacy(docs, node="legacy-large")
    legacy_dir = _legacy_log(docs).parent

    replay = replay_hold(row, project=PROJECT, log_dir=legacy_dir)
    assert replay["request_digest"] == row["request_digest"]
    assert json.loads(replay["state"]) == {
        "notes": "y" * (HOLD_PAYLOAD_INLINE_LIMIT * 2)
    }


def test_a_reader_returns_holds_from_the_legacy_and_crew_home_logs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    docs = tmp_path / "docs"

    _seed_legacy(docs, node="legacy")
    _record(docs, node="crew", state='{"k": 2}', questions={})

    records, malformed = _read_holds(docs, PROJECT)
    assert malformed == 0
    assert {row["node"] for row in records} == {"legacy", "crew"}

    # Each row carries the directory it was read from, and replays from there
    # without the caller having to remember which log it came from.
    origins = {row["node"]: row["_log_dir"] for row in records}
    assert origins["legacy"] == str(_legacy_log(docs).parent)
    assert origins["crew"] == str(_crew_log().parent)
    for row in records:
        assert (
            replay_hold(row, project=PROJECT)["request_digest"] == row["request_digest"]
        )
