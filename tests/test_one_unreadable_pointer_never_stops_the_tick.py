"""One pointer that cannot be classified never ends the station's tick.

A station tick classifies every live pointer in one pass. On 14 November a
blocked CLI pointer whose backend command was ``srun`` made the classifier
raise ``BackendError`` from the session resolver -- ``srun`` has no launch
translation -- and nothing between the resolver and the tick isolated that one
row, so the pass ended for every other pointer and the station with it.

Two repairs are measured here, and they are separate facts:

* The station's snapshot classifies each pointer under its own guard. A row
  whose classification raises is recorded unreadable, carrying the exception's
  type and message, and the pass carries on to the rest.
* The session resolver treats a backend whose command has no launch translation
  as carrying no session id, so a blocked ``srun`` pointer classifies normally
  instead of raising at all.

The guard is measured against the exception the incident produced, injected at
the resolver the incident's call path named: the sibling repair removes the
natural trigger, and a guard is only worth having if it holds for the failure
that motivated it. The last case runs the same three pointers with no
injection, which is what the repaired resolver does at head.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import resumption, station_store
from reckon.crew.run_paths import crew_home

STALL_SECONDS = 60
# The sentence the incident's log carried, so the injected failure is the one a
# reader can find in the record rather than a stand-in for it.
UNTRANSLATABLE = (
    "no launch translation for command 'srun'; "
    "reckon can translate: claude, clive, codex, omp"
)


def _stream(path: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "system", "subtype": "init", "session_id": session_id},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "working"}],
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                },
            )
        )
        + "\n"
    )


def _pointer(
    home: Path,
    run_id: str,
    *,
    command: str = "claude",
    blocked: bool = False,
) -> Path:
    run_directory = home / "runs" / run_id
    stream = run_directory / "stream.jsonl"
    _stream(stream, f"session-{run_id}")
    manifest = run_directory / "manifest.md"
    if blocked:
        manifest.write_text("---\nstatus: blocked\n---\n\nwaiting on the lane\n")
    record = {
        "run_id": run_id,
        "project": "fixture",
        "session": "s1",
        "phase": "working",
        # A foreign launcher keeps the process probe off this test host, so the
        # row's classification is read from its manifest and stream alone.
        "launcher_host": "another-node",
        "created_at": "2026-11-14T16:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "worktree": str(run_directory),
        "repo": str(run_directory),
        "node": {},
        "launch": "cli",
        "command": command,
        "backend": "srun" if command == "srun" else "claude",
    }
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


@pytest.fixture()
def pointers(home: Path) -> dict[str, Path]:
    """Two ordinary live pointers and one blocked ``srun`` pointer."""
    crew = crew_home()
    assert crew.is_relative_to(home), "the crew home must be the test's own"
    return {
        "one": _pointer(crew, "r-one"),
        "two": _pointer(crew, "r-two"),
        "srun": _pointer(crew, "r-srun", command="srun", blocked=True),
    }


def _tick(tmp_path: Path) -> dict[str, dict]:
    store = station_store.StationStore(tmp_path / "station")
    store.tick(home=crew_home())
    return store.classify_tick(stall_seconds=STALL_SECONDS)


def _raise_for_srun(monkeypatch) -> None:
    """Make the resolver raise the incident's exception for the ``srun`` run."""
    real = resumption._stream_session

    def raising(record):
        if str(record.get("run_id") or "") == "r-srun":
            raise _backends.BackendError(UNTRANSLATABLE)
        return real(record)

    monkeypatch.setattr(resumption, "_stream_session", raising)


def test_a_pointer_whose_classification_raises_is_recorded_unreadable(
    pointers, tmp_path, monkeypatch
):
    """The guard: the incident's own exception costs one row, not the tick."""
    _raise_for_srun(monkeypatch)

    verdicts = _tick(tmp_path)

    assert set(verdicts) == {"r-one", "r-two", "r-srun"}
    assert verdicts["r-one"]["state"] == "working"
    assert verdicts["r-two"]["state"] == "working"
    unreadable = verdicts["r-srun"]
    assert unreadable["state"] == "unreadable"
    assert unreadable["classification"] == "unreadable"
    assert "BackendError" in unreadable["detail"]
    assert UNTRANSLATABLE in unreadable["detail"]


def test_stream_session_answers_empty_for_an_untranslatable_backend(pointers):
    """The resolver: an untranslatable command carries no session, and says so."""
    record = json.loads(pointers["srun"].read_text())

    assert resumption._stream_session(record) == ""


def test_the_station_snapshot_survives_an_srun_pointer(pointers, tmp_path):
    """With the resolver repaired, the same three pointers tick with no raise."""
    verdicts = _tick(tmp_path)

    assert set(verdicts) == {"r-one", "r-two", "r-srun"}
    assert verdicts["r-one"]["state"] == "working"
    assert verdicts["r-two"]["state"] == "working"
    assert verdicts["r-srun"]["state"] == "blocked"


def test_the_guard_names_the_failing_pointer_on_the_station_log(
    pointers, tmp_path, monkeypatch, capsys
):
    """The row is not the only record: the tick's own log names the row."""
    _raise_for_srun(monkeypatch)

    assert len(_tick(tmp_path)) == 3
    assert "station-tick-unreadable: r-srun: BackendError" in capsys.readouterr().err
