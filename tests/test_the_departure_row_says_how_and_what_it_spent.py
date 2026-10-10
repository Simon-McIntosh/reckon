"""Departure words and spend come from the run's recorded outcome."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import metering, recovery, recovery_watch, ticker


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    root = tmp_path / "repo"
    (root / "docs" / "state" / "sample").mkdir(parents=True)
    (config / "state").mkdir()
    (config / "state" / "sample").symlink_to(root / "docs" / "state" / "sample")
    (config / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _snapshot(run_id: str, state: str = "working") -> dict:
    return {
        "run_id": run_id,
        "project": "sample",
        "node": "example",
        "state": state,
        "detail": "",
        "role": "implement",
    }


def _row(
    root: Path,
    run_id: str,
    *,
    commits: list[str],
    throughput=None,
    no_commit: str | None = None,
) -> None:
    path = ledger.run_path("sample", run_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": run_id, "commits": commits, "throughput": throughput}
    if no_commit is not None:
        record["no_commit"] = no_commit
    path.write_text(
        json.dumps(record),
        encoding="utf-8",
    )


def _event(snapshot: dict, state: str, *, spend_runs=None) -> dict:
    return recovery._watch_transition(
        "sample",
        kind="transition",
        snapshot=snapshot,
        previous="working",
        current=state,
        counts={"working": 0, "blocked": 0, "unpromoted": 0},
        spend_runs=spend_runs,
        rate_statuses={},
    )


def _plain(event: dict) -> str:
    return recovery.format_watch_transition(event)


def test_four_departures_render_four_words(home: Path, monkeypatch) -> None:
    run_id = "r-same-shape"
    snapshot = _snapshot(run_id)
    _row(home, run_id, commits=["recorded-commit"])
    promoted = recovery.fleet_transitions({run_id: snapshot}, {})[0][0][2]

    # The other three outcomes keep the same run and snapshot shape. The
    # marker is the durable fact a deliberate discard leaves behind.
    path = ledger.run_path("sample", run_id, home)
    path.unlink()
    monkeypatch.setattr(recovery_watch, "_discard_recorded", lambda _run: True)
    discarded = recovery.fleet_transitions({run_id: snapshot}, {})[0][0][2]
    monkeypatch.setattr(recovery_watch, "_discard_recorded", lambda _run: False)
    stopped = recovery.fleet_transitions(
        {run_id: snapshot}, {run_id: _snapshot(run_id, "stopped")}
    )[0][0][2]
    withdrawn = recovery.fleet_transitions({run_id: snapshot}, {})[0][0][2]

    words = [promoted, discarded, stopped, withdrawn]
    assert words == ["promoted", "discarded", "stopped", "withdrawn"]
    lines = [_plain(_event(snapshot, word)) for word in words]
    assert all(word in line for word, line in zip(words, lines, strict=True))
    assert len(set(lines)) == 4


def test_a_row_with_no_commits_cannot_claim_promotion(home: Path) -> None:
    run_id = "r-no-commits"
    _row(home, run_id, commits=[], no_commit="the report is the deliverable")
    snapshot = _snapshot(run_id)
    word = recovery.fleet_transitions({run_id: snapshot}, {})[0][0][2]
    assert word == "recorded"
    assert "promoted" not in _plain(_event(snapshot, word))
    assert "recorded" in _plain(_event(snapshot, word))

    pointer = {"run_id": run_id, "project": "sample", "repo": str(home)}
    assert recovery._promote_record_holds(pointer)
    assert recovery._recorded_pointer_word(pointer) == "recorded"
    classified = recovery.classify_pointer(pointer)
    assert classified["classification"] == "recorded"
    assert classified["fleet_verdict"]["state"] == "recorded"
    _row(home, run_id, commits=["recorded-commit"])
    assert recovery._promote_record_holds(pointer)
    assert recovery._recorded_pointer_word(pointer) == "promoted"


def test_malformed_run_id_cannot_hold_a_promotion(home: Path) -> None:
    pointer = {"run_id": "bad id", "project": "sample", "repo": str(home)}
    assert recovery._promote_record_holds(pointer) is False


def test_promoted_transition_keeps_the_live_pointer_figures(home: Path) -> None:
    run_id = "r-measured"
    directory = home.parent / "config" / "crew" / "runs" / run_id
    directory.mkdir(parents=True)
    stream = directory / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 0,
                    "output_tokens": 50,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": "sample",
        "log_path": str(stream),
        "throughput": {
            "elapsed_seconds": 20.0,
            "generation_seconds": 5.0,
            "cumulative_input_tokens": 100,
            "generated_tokens": 50,
            "tokens_per_second": 10.0,
        },
    }
    snapshot = _snapshot(run_id)
    before = _event(snapshot, "complete", spend_runs=[pointer])
    assert metering.accumulate_run_spend([pointer], run_id).measured_stream_count == 1
    assert [
        before[key]
        for key in (
            "spend_wall_seconds",
            "spend_charged_tokens",
            "spend_generation_rate",
        )
    ] == [20.0, 150, 10.0]

    _row(home, run_id, commits=["recorded-commit"], throughput=pointer["throughput"])
    word = recovery.fleet_transitions({run_id: snapshot}, {})[0][0][2]
    assert word == "promoted"
    after = _event(snapshot, word, spend_runs=[])
    for key in ("spend_wall_seconds", "spend_charged_tokens", "spend_generation_rate"):
        assert after[key] == before[key], key
    assert "promoted" in _plain(after)
    assert "0m" in _plain(after)


def test_unknown_spend_and_measured_zero_render_differently(home: Path) -> None:
    unknown_id = "r-unknown"
    zero_id = "r-zero"
    _row(home, unknown_id, commits=["recorded-commit"])
    _row(
        home,
        zero_id,
        commits=["recorded-commit"],
        throughput={
            "elapsed_seconds": 0.0,
            "generation_seconds": 0.0,
            "cumulative_input_tokens": 0,
            "generated_tokens": 0,
            "tokens_per_second": 0.0,
        },
    )
    unknown = _event(_snapshot(unknown_id), "promoted", spend_runs=[])
    zero = _event(_snapshot(zero_id), "promoted", spend_runs=[])
    assert unknown["spend_wall_seconds"] is None
    assert zero["spend_wall_seconds"] == 0
    assert unknown["spend_charged_tokens"] is None
    assert zero["spend_charged_tokens"] == 0
    assert _plain(unknown) != _plain(zero)
    assert ticker.DIM_MARKER in _plain(unknown)
    assert ticker.DIM_MARKER not in _plain(zero)
    assert "0m" in _plain(zero)
