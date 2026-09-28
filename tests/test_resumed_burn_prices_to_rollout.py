"""Price a resumed codex run's burn by its thread, not by summing its attempts.

A codex ``exec resume`` re-enters the same client thread, and its stream
restates that thread's cumulative usage rather than reporting only the new
work. A fold that adds every attempt therefore counts each earlier attempt
again: measured over this host's codex lineage runs the summed total came to
157% of the client's own rollout, up to 227% on a three-attempt run. The fold
keeps the newest attempt of a thread and adds threads together, because a lane
change opens a second thread whose stream is new work rather than a restatement
of the first.

The fixture below models that shape: each run is a directory of attempt streams
that share one thread identity and carry strictly increasing cumulative
readings, beside a rollout receipt carrying the client's own total for the
session. A run prices correctly when the fold lands within the same 1% band the
receipt is read at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import rollout
from reckon.crew.metering import AccumulatedRunSpend, accumulate_run_spend
from reckon.crew.rollout import read_rollout_receipt

PRICE_TOLERANCE = 0.01

# Each reading is one attempt's (input, cached_input, output). Inputs and
# outputs climb because a resume restates the whole thread so far; the newest
# reading is the thread's own total.
LINEAGE_CASES = {
    "three-attempts": [(1_000, 800, 10), (1_600, 1_300, 16), (2_000, 1_650, 20)],
    "two-attempts-a": [(900, 700, 9), (1_500, 1_200, 15)],
    "two-attempts-b": [(1_100, 900, 11), (1_800, 1_500, 18)],
}


def _turn(input_tokens: int, cached_input_tokens: int, output_tokens: int) -> dict:
    # Reasoning output is zero so the stream's output equals the rollout's
    # output_tokens and the comparison is between like quantities.
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": 0,
        },
    }


def _write_codex_stream(
    path: Path,
    *,
    thread_id: str | None,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> Path:
    records: list[dict] = []
    if thread_id is not None:
        records.append({"type": "thread.started", "thread_id": thread_id})
    records.append(_turn(input_tokens, cached_input_tokens, output_tokens))
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records))
    return path


def _write_rollout(root: Path, session_id: str, total: tuple[int, int, int]) -> Path:
    input_tokens, cached_input_tokens, output_tokens = total
    directory = root / "2026" / "09" / "28"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-28T00-00-00-{session_id}.jsonl"
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                },
                "model_context_window": 200_000,
            },
        },
    }
    path.write_text(f"{json.dumps(record)}\n")
    return path


def _attempt_names(count: int) -> list[str]:
    return ["stream.jsonl", *[f"resume-{index}.jsonl" for index in range(1, count)]]


def _lineage_dir(
    root: Path, run_id: str, readings: list[tuple[int, int, int]], thread_id: str
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, reading in zip(_attempt_names(len(readings)), readings, strict=True):
        _write_codex_stream(
            run_dir / name,
            thread_id=thread_id,
            input_tokens=reading[0],
            cached_input_tokens=reading[1],
            output_tokens=reading[2],
        )
    return run_dir


def _charged(reading: tuple[int, int, int]) -> int:
    input_tokens, cached_input_tokens, output_tokens = (
        reading[0],
        reading[1],
        reading[2],
    )
    return input_tokens + cached_input_tokens + output_tokens


def _fold(runs: list[dict], run_id: str, root: Path) -> AccumulatedRunSpend:
    result = accumulate_run_spend(runs, run_id, streams_root=root)
    assert isinstance(result, AccumulatedRunSpend)
    return result


def _rollout_total(session_id: str) -> int:
    receipt = read_rollout_receipt(session_id)
    assert isinstance(receipt.cumulative_input_tokens, int)
    assert isinstance(receipt.cumulative_cached_input_tokens, int)
    assert isinstance(receipt.cumulative_output_tokens, int)
    return (
        receipt.cumulative_input_tokens
        + receipt.cumulative_cached_input_tokens
        + receipt.cumulative_output_tokens
    )


def _ratio(priced: int, total: int) -> float:
    return priced / total


@pytest.mark.parametrize("case", sorted(LINEAGE_CASES))
def test_a_resumed_codex_lineage_prices_within_one_percent_of_its_rollout(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path / "codex")
    readings = LINEAGE_CASES[case]
    run_id = f"r-{case}"
    session_id = f"resume-burn-{case}"
    root = tmp_path / "runs"
    _lineage_dir(root, run_id, readings, thread_id=f"thread-{case}")
    _write_rollout(tmp_path / "codex", session_id, readings[-1])

    result = _fold([{"run_id": run_id}], run_id, root)
    total = _rollout_total(session_id)

    assert result.measured_stream_count == len(readings)
    assert result.unmeasured_stream_count == 0
    assert abs(_ratio(result.total_charged_tokens, total) - 1.0) <= PRICE_TOLERANCE


def test_summing_the_attempts_is_the_overcount_the_thread_fold_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect the fold corrects, shown on the same fixture in one run.

    Every attempt re-states the thread, so adding them overshoots the client's
    own total by a wide margin while the thread fold lands on it. Asserting
    only the corrected figure would not show that the fixture can fail.
    """
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path / "codex")
    readings = LINEAGE_CASES["three-attempts"]
    run_id = "r-overcount"
    session_id = "resume-burn-overcount"
    root = tmp_path / "runs"
    _lineage_dir(root, run_id, readings, thread_id="thread-overcount")
    _write_rollout(tmp_path / "codex", session_id, readings[-1])

    result = _fold([{"run_id": run_id}], run_id, root)
    total = _rollout_total(session_id)
    summed = sum(_charged(reading) for reading in readings)

    assert summed == 8_396
    assert abs(_ratio(summed, total) - 2.29) <= 0.01
    assert abs(_ratio(result.total_charged_tokens, total) - 1.0) <= PRICE_TOLERANCE


def test_a_lane_change_stream_is_summed_not_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane change is a second thread, so its stream adds rather than replaces.

    Applying the newest-attempt rule across threads would undercount by the
    order of magnitude it corrects, so the fixture holds two threads in one run
    directory and requires both to be counted.
    """
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path / "codex")
    run_id = "r-lane-change"
    session_id = "resume-burn-lane-change"
    root = tmp_path / "runs"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    first = (1_200, 1_000, 12)
    second = (800, 600, 8)
    _write_codex_stream(
        run_dir / "stream.jsonl",
        thread_id="thread-lane-first",
        input_tokens=first[0],
        cached_input_tokens=first[1],
        output_tokens=first[2],
    )
    _write_codex_stream(
        run_dir / "lane-change-2.jsonl",
        thread_id="thread-lane-second",
        input_tokens=second[0],
        cached_input_tokens=second[1],
        output_tokens=second[2],
    )
    _write_rollout(
        tmp_path / "codex",
        session_id,
        (first[0] + second[0], first[1] + second[1], first[2] + second[2]),
    )

    result = _fold([{"run_id": run_id}], run_id, root)
    total = _rollout_total(session_id)

    assert result.measured_stream_count == 2
    assert result.total_charged_tokens == _charged(first) + _charged(second)
    assert result.total_charged_tokens > max(_charged(first), _charged(second))
    assert abs(_ratio(result.total_charged_tokens, total) - 1.0) <= PRICE_TOLERANCE


def test_a_stream_without_a_thread_identity_is_its_own_thread(tmp_path: Path) -> None:
    """An unlabelled pair still adds up, so no attempt is silently dropped.

    A stream that declares no thread carries nothing to fold under, so it is
    summed with the others rather than superseded by one that declares less.
    """
    root = tmp_path / "runs"
    run_id = "r-unlabelled"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    readings = [(400, 300, 4), (700, 500, 7)]
    for name, reading in zip(_attempt_names(2), readings, strict=True):
        _write_codex_stream(
            run_dir / name,
            thread_id=None,
            input_tokens=reading[0],
            cached_input_tokens=reading[1],
            output_tokens=reading[2],
        )

    result = _fold([{"run_id": run_id}], run_id, root)

    assert result.measured_stream_count == 2
    assert result.total_charged_tokens == sum(_charged(reading) for reading in readings)
