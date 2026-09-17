"""A run the backend refused at admission is named for it, not folded away.

The recorded instance is run r-20260917T103829858481, whose stream is three
lines: an assistant record carrying the client's substitution for "no model
served this turn" (the literal <synthetic>) with error invalid_request and the
text "Prompt is too long", and a result record carrying terminal_reason
blocking_limit, is_error true, num_turns 1, duration_api_ms 0 and every token
counter zero. The classifier read that run as abandoned, the bucket for a
worker that vanished, and that reading is what let a coordinator on one fleet
report the death as a second instance of another fleet's serving defect.

The streams here are synthesised from those marks rather than read from that
path, because a test must not depend on state outside the repository under
test. The marks are the contract, not the whole file.
"""

import json
import time
from pathlib import Path

import pytest

from reckon.crew import recovery

BACKEND_FIXTURES = Path(__file__).parent / "fixtures" / "backends"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move every crew pointer and watcher claim into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_stream(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def _zero_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _assistant_refusal() -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": "m-1",
            "model": "<synthetic>",
            "role": "assistant",
            "stop_reason": "stop_sequence",
            "usage": _zero_usage(),
            "content": [{"type": "text", "text": "Prompt is too long"}],
        },
        "error": "invalid_request",
    }


def _zero_result(terminal_reason: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "terminal_reason": terminal_reason,
        "is_error": True,
        "num_turns": 1,
        "duration_api_ms": 0,
        "usage": _zero_usage(),
        "result": "Prompt is too long",
    }


def _pointer(home: Path, run_id: str, stream: Path, **kw) -> dict:
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "backend": "clive",
        "launch": "cli",
        "dialect": "claude",
        "argv": ["clive", "-p"],
        "log_path": str(stream),
        "stderr_path": str(home / "runs" / run_id / "stderr.log"),
        "phase": "starting",
        "process_alive": False,
        "session": "s",
        "manifest_path": str(home / "manifests" / f"{run_id}.md"),
    }
    record.update(kw)
    return record


def test_a_refused_admission_is_named(home, tmp_path) -> None:
    stream = _write_stream(
        tmp_path / "r-admission.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "s"},
            _assistant_refusal(),
            _zero_result("blocking_limit"),
        ],
    )
    pointer = _pointer(home, "r-admission", stream)

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] == "refused-at-admission"
    assert row["classification"] not in {"abandoned", "blocked"}
    assert row["recovery_classification"] == "refused-at-admission"
    assert row["recovery"] == "resume"
    assert row["process_alive"] is False
    assert "Prompt is too long" in row["detail"]
    assert "blocking_limit" in row["detail"]
    assert str(stream) in row["next_action"]
    assert str(pointer["stderr_path"]) in row["next_action"]


def test_the_new_classification_declares_a_recovery_verb() -> None:
    assert recovery.RECOVERY_VERBS["refused-at-admission"] == "resume"


def test_a_genuine_vanished_run_still_abandons(home, tmp_path) -> None:
    stream = _write_stream(tmp_path / "r-vanished.jsonl", [{"type": "turn.started"}])
    pointer = _pointer(home, "r-vanished", stream)

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] == "abandoned"


def test_a_terminal_manifest_run_is_judged_on_its_delivery(home, tmp_path) -> None:
    stream = _write_stream(
        tmp_path / "r-finished.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "s"},
            _assistant_refusal(),
            _zero_result("blocking_limit"),
        ],
    )
    manifest = home / "manifests" / "r-finished.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: r-finished\nstatus: complete\ncommits: deadbee\n", encoding="utf-8"
    )
    pointer = _pointer(
        home, "r-finished", stream, phase="complete", manifest_path=str(manifest)
    )

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] == "scoring"
    assert row["classification"] != "refused-at-admission"


def test_a_refusal_missing_a_mark_does_not_reach_the_arm(home, tmp_path) -> None:
    # The gate declines on either half being absent, and the decline is what
    # keeps it from swallowing the neighbours. A bad model id is the closest
    # shape: a synthetic message with a zero-token error result, but no prompt
    # length refusal, so it stays a crash.
    other = _write_stream(
        tmp_path / "r-other.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "s"},
            _assistant_refusal(),
            _zero_result("api_error"),
        ],
    )
    row = recovery.classify_pointer(
        _pointer(home, "r-other", other), now_seconds=time.time()
    )
    assert row["classification"] == "abandoned"

    # The mirror half: the result marks without the synthetic message. Nothing
    # names the client substitution, so no reading is available and the run
    # keeps its ordinary dead-process classification.
    worked = _write_stream(
        tmp_path / "r-worked.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "s"},
            {
                "type": "assistant",
                "message": {
                    "id": "m-2",
                    "model": "deepseek-v4.1-flash",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": _zero_usage(),
                },
            },
            _zero_result("blocking_limit"),
        ],
    )
    row = recovery.classify_pointer(
        _pointer(home, "r-worked", worked), now_seconds=time.time()
    )
    assert row["classification"] == "abandoned"


def test_a_live_process_with_those_marks_still_reads_running(home, tmp_path) -> None:
    # Liveness outranks every stream reading: a worker still in flight is never
    # called refused, whatever its stream carries.
    stream = _write_stream(
        tmp_path / "r-live.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "s"},
            _assistant_refusal(),
            _zero_result("blocking_limit"),
        ],
    )
    pointer = _pointer(home, "r-live", stream, process_alive=True)

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] == "running"
    assert row["classification"] != "refused-at-admission"


def test_an_in_harness_run_with_those_marks_is_not_refused(home, tmp_path) -> None:
    # The gate reads a cli run's stream only. An in-harness run has no stream
    # for the classifier to read, so its reading is unchanged.
    stream = _write_stream(
        tmp_path / "r-harness.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "s"},
            _assistant_refusal(),
            _zero_result("blocking_limit"),
        ],
    )
    pointer = _pointer(home, "r-harness", stream, launch=None)

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] == "abandoned"


def test_every_other_classification_is_unchanged(home, tmp_path) -> None:
    # The existing arms report what they reported before this classification:
    # a vanished worker, a bad-model crash, a refused lane, an unreadable
    # manifest and a worker-reported failure each keep their own label, so the
    # new branch can be seen not to have swallowed a neighbour.
    vanished = _write_stream(tmp_path / "r-gone.jsonl", [{"type": "turn.started"}])
    unreadable_manifest = home / "manifests" / "r-unreadable.md"
    unreadable_manifest.parent.mkdir(parents=True, exist_ok=True)
    unreadable_manifest.write_text(
        '{"node": "x", "status": "complete", ', encoding="utf-8"
    )
    failed_manifest = home / "manifests" / "r-failed.md"
    failed_manifest.write_text(
        "node: r-failed\nstatus: failed\nblockers: the suite is red\n",
        encoding="utf-8",
    )

    cases = {
        "abandoned": _pointer(home, "r-gone", vanished),
        "unreadable": _pointer(
            home,
            "r-unreadable",
            vanished,
            manifest_path=str(unreadable_manifest),
        ),
        "failed": _pointer(
            home,
            "r-failed",
            vanished,
            phase="failed",
            manifest_path=str(failed_manifest),
        ),
    }
    for expected, pointer in cases.items():
        row = recovery.classify_pointer(pointer, now_seconds=time.time())
        assert row["classification"] == expected

    # The same holds for the recorded fixtures the existing gate rests on: the
    # bad-model crash is still a crash, and a metered lane refusal is still a
    # block rather than any reading this change produces.
    crash = _pointer(home, "r-crash", BACKEND_FIXTURES / "claude-failed-turn.jsonl")
    assert (
        recovery.classify_pointer(crash, now_seconds=time.time())["classification"]
        == "abandoned"
    )
    refused = _pointer(
        home,
        "r-refused",
        BACKEND_FIXTURES / "codex-usage-limit.jsonl",
        backend="codex",
        dialect="codex",
        argv=["codex"],
    )
    assert (
        recovery.classify_pointer(refused, now_seconds=time.time())["classification"]
        == "blocked"
    )
