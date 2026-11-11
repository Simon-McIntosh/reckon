"""A board row carries its coordinator's context use at its last turn, and its window.

A coordinator that stalls at the edge of its window is invisible on every surface
until it stops dispatching. The row is read from the session's own harness
transcript — the same transcript, and the same charging rule, that records
``coordinator.authoring_turn`` — so the board's figure and the dispatch record
cannot disagree. A session with no readable transcript reads unmeasured, never
zero: zero is the measurement of an empty context, and nothing here measured one.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import directory as directory_module
from reckon.crew.directory import directory
from reckon.crew.dispatch_accounting import _claude_authoring_turn

PROJECT = "power_convertor"
REPOSITORY = "/repos/power_convertor"
SESSION = "6f0a2b1c-1111-4a2b-9c3d-abcdef012345"
PROJECT_KEY = "-repos-power_convertor"


def _turn(
    *, model: str, identity: str, input_tokens: int, created: int, read: int
) -> dict:
    """One assistant request, in the shape the harness writes."""
    return {
        "type": "assistant",
        "message": {
            "id": identity,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": created,
                "cache_read_input_tokens": read,
                "output_tokens": 120,
            },
        },
    }


def _transcript(root: Path, session: str, *, model: str = "claude-opus-5-5") -> Path:
    """Write a session transcript whose last turn is much larger than its first."""
    path = root / PROJECT_KEY / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user", "message": {"content": "start"}},
        _turn(
            model=model,
            identity="msg-first",
            input_tokens=5,
            created=1_000,
            read=0,
        ),
        {"type": "user", "message": {"content": "continue"}},
        _turn(
            model=model,
            identity="msg-last",
            input_tokens=40,
            created=60_000,
            read=190_000,
        ),
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def _pointer(home: Path, *, run_id: str, session: str, node: str) -> dict:
    """One live pointer, written where the views read pointers from."""
    log = home / "crew" / "runs" / run_id / "stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n")
    record = {
        "run_id": run_id,
        "session": session,
        "project": PROJECT,
        "repo": REPOSITORY,
        "worktree": f"{REPOSITORY}/.reckon-worktrees/{session}/{node}",
        "node": {"id": node, "plan": "coil-build", "section": "s1", "write_paths": []},
        "phase": "working",
        "process_alive": True,
        "log_path": str(log),
    }
    path = home / "crew" / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


#: The model-id prefixes a catalogue declares a window for. The board reads each
#: model's window from the catalogue, and the suite points the catalogue at an
#: absent file, so a fixture that expects a measured window supplies its own.
#: A window no table in the tree holds, so the row can only carry it when the
#: reader consults the configuration rather than a hardcoded figure.
_FIXTURE_WINDOW = 123_456

_WINDOW_PREFIXES = (
    "claude-fable-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-haiku-4",
)


def _catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the catalogue at a fixture declaring the models' context windows."""
    path = tmp_path / "model-catalogue.yaml"
    windows = "".join(f"  {prefix}: {_FIXTURE_WINDOW}\n" for prefix in _WINDOW_PREFIXES)
    path.write_text(f"version: 1\nmodel_windows:\n{windows}", encoding="utf-8")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(path))


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, session: str) -> dict:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.setattr(directory_module, "_TRANSCRIPT_ROOT", tmp_path / "projects")
    _catalogue(tmp_path, monkeypatch)
    return _pointer(home, run_id="run-live", session=session, node="build-the-coil")


def _row(view: dict, session: str) -> dict:
    return next(
        row for row in view["coordinators"] if str(row.get("session") or "") == session
    )


def test_a_fixture_transcript_yields_its_context_use_and_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _seed(tmp_path, monkeypatch, session=SESSION)
    transcript = _transcript(tmp_path / "projects", SESSION)

    view = directory(PROJECT, pointers=[record])
    use = _row(view, SESSION)["context_use"]

    # The figure is the last request's charged input: uncached input plus cache
    # creation plus cache read. The first request's figure is far smaller, so a
    # reader that took the first turn, or summed the turns, misses this.
    charging = _claude_authoring_turn(transcript)
    assert charging is not None
    assert use["used_tokens"] == charging["input_tokens"] == 250_040
    assert use["status"] == "measured"
    assert use["model"] == "claude-opus-5-5"
    assert use["window_tokens"] == _FIXTURE_WINDOW
    # The fraction is the figure against its own window, at full precision.
    assert use["fraction"] == pytest.approx(250_040 / _FIXTURE_WINDOW)
    assert use["source"] == "claude-code-session-transcript"


def test_a_host_layer_window_is_read_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A window is routing configuration, so a host layer that declares one must
    # reach the row: reading only the catalogue would drop this silently.
    record = _seed(tmp_path, monkeypatch, session=SESSION)
    host = tmp_path / "host-flight.yaml"
    host.write_text(
        "version: 1\nmodel_windows:\n  claude-opus-5: 777000\n", encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(host))
    _transcript(tmp_path / "projects", SESSION)

    use = _row(directory(PROJECT, pointers=[record]), SESSION)["context_use"]

    assert use["window_tokens"] == 777_000
    assert use["fraction"] == pytest.approx(250_040 / 777_000)


def test_a_missing_transcript_reads_unmeasured_never_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The session has no transcript anywhere under the transcript root.
    record = _seed(tmp_path, monkeypatch, session=SESSION)

    use = _row(directory(PROJECT, pointers=[record]), SESSION)["context_use"]

    assert use["status"] == "unmeasured"
    assert use["used_tokens"] is None
    assert use["window_tokens"] is None
    assert use["fraction"] is None
    # The negative control: a reader returning zero here reports an empty
    # context for a session nobody measured.
    assert use["used_tokens"] != 0
    assert "no claude-code session transcript" in use["detail"]


def test_a_transcript_without_a_charged_request_reads_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A readable transcript whose assistant records charge nothing is a
    # different fact from a missing transcript, and neither is a zero.
    record = _seed(tmp_path, monkeypatch, session=SESSION)
    root = tmp_path / "projects"
    path = root / PROJECT_KEY / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "user", "message": {"content": "only a prompt"}}) + "\n",
        encoding="utf-8",
    )

    use = _row(directory(PROJECT, pointers=[record]), SESSION)["context_use"]

    assert use["status"] == "unmeasured"
    assert use["used_tokens"] is None
    assert "no charged assistant request" in use["detail"]


def test_an_unknown_model_carries_its_use_but_no_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model with no declared window still measured its context use; the window
    # is absent rather than guessed, so no fraction is invented.
    record = _seed(tmp_path, monkeypatch, session=SESSION)
    _transcript(tmp_path / "projects", SESSION, model="unpublished-model-9")

    use = _row(directory(PROJECT, pointers=[record]), SESSION)["context_use"]

    assert use["status"] == "measured"
    assert use["used_tokens"] == 250_040
    assert use["window_tokens"] is None
    assert use["fraction"] is None
    assert "no declared context window" in use["detail"]


def test_the_newest_transcript_of_a_session_is_the_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A session can be recorded under more than one working directory, and the
    # row reports the transcript still being written.
    record = _seed(tmp_path, monkeypatch, session=SESSION)
    root = tmp_path / "projects"
    stale = _transcript(root, SESSION)
    current = root / "-another-worktree" / f"{SESSION}.jsonl"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(
        json.dumps(
            _turn(
                model="claude-opus-5-5",
                identity="msg-new",
                input_tokens=900_000,
                created=0,
                read=0,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    later = datetime.now(UTC).timestamp() + 60
    os.utime(current, (later, later))

    use = _row(directory(PROJECT, pointers=[record]), SESSION)["context_use"]

    assert use["used_tokens"] == 900_000
    assert stale.is_file()


def test_a_request_deeper_than_the_tail_is_read_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tail read starts inside a line and drops the fragment it lands in, so a
    # tail shorter than the last request cannot see it. The reader then falls
    # back to the forward read the dispatch accounting owns, and answers the same
    # figure rather than reporting the session unmeasured.
    record = _seed(tmp_path, monkeypatch, session=SESSION)
    _transcript(tmp_path / "projects", SESSION)
    monkeypatch.setattr(directory_module, "_TRANSCRIPT_TAIL_SIZES", (32,))

    use = _row(directory(PROJECT, pointers=[record]), SESSION)["context_use"]

    assert use["status"] == "measured"
    assert use["used_tokens"] == 250_040
