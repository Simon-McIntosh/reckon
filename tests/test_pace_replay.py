"""Tests for replaying committed pace rows as an independent report."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from reckon.crew import pace


def _replay_project(root: Path, project: str, **kwargs):
    from reckon.crew.pace_replay import replay_project

    return replay_project(root, project, **kwargs)


def _render_report(report):
    from reckon.crew.pace_replay import render_report

    return render_report(report)


def _instant(hours_from_start: float) -> str:
    start = datetime(2026, 9, 22, 8, tzinfo=UTC)
    return (start + timedelta(hours=hours_from_start)).isoformat()


def _row(
    *,
    node: str,
    role: str,
    backend: str,
    local: bool,
    utilisation: float,
    elapsed: float,
    hold: dict | None = None,
) -> dict:
    recorded_at = _instant(elapsed)
    reset_at = datetime.fromisoformat(recorded_at) + timedelta(
        hours=pace.WEEK_HOURS - elapsed
    )
    policy = pace.PacePolicy(drain_lead_hours=12.0, pace_multiple=1.1)
    reading = pace.GroupReading(
        group="shared",
        utilisation=utilisation,
        elapsed_hours=elapsed,
    )
    allowance = pace.allowance_for_group(reading, pace=policy).as_dict()
    pace_row = {
        "lane": backend,
        "node": node,
        "score": 0.4,
        "recorded_at": recorded_at,
        "policy": {
            "drain_lead_hours": policy.drain_lead_hours,
            "pace_multiple": policy.pace_multiple,
        },
        "hold": hold,
        "group": "shared",
        "state": "observed",
        "source": "recorded_windows",
        "member": backend,
        "clocks": {
            "five_hour": {
                "period": "five_hour",
                "state": "observed",
                "utilisation": 0.2,
                "observed_at": recorded_at,
                "resets_at": _instant(elapsed + 5.0),
                "age_seconds": 0.0,
            },
            "seven_day": {
                "period": "seven_day",
                "state": "observed",
                "utilisation": utilisation,
                "observed_at": recorded_at,
                "resets_at": reset_at.isoformat(),
                "age_seconds": 0.0,
            },
        },
        "allowance": allowance,
        "bar": {
            "name": node,
            "score": 0.4,
            "state": "observed",
            "verdict": "send-metered",
            "window_fill": 0.2,
        },
        "reason": None,
    }
    return {
        "run_id": f"run-{node}",
        "role": role,
        "backend": backend,
        "local": local,
        "pace": pace_row,
    }


def _held_row() -> dict:
    return _row(
        node="held-review",
        role="review",
        backend="codex",
        local=False,
        utilisation=0.95,
        elapsed=96.0,
        hold={
            "backend": "codex",
            "held": True,
            "effective_ceiling_pct": 92.0,
            "state": {"utilisation_pct": 95.0},
            "reason": "95.0% exceeds 92.0% ceiling",
        },
    )


@pytest.fixture()
def committed_week(tmp_path: Path) -> Path:
    run_dir = tmp_path / "docs" / "state" / "sample" / "runs"
    run_dir.mkdir(parents=True)
    records = [
        _row(
            node="build-early",
            role="implement",
            backend="codex",
            local=False,
            utilisation=0.1,
            elapsed=24.0,
        ),
        _row(
            node="build-late",
            role="implement",
            backend="clive",
            local=True,
            utilisation=0.6,
            elapsed=120.0,
        ),
        _held_row(),
    ]
    for record in records:
        (run_dir / f"{record['run_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    (run_dir / "without-pace.json").write_text(
        json.dumps({"run_id": "without-pace"}), encoding="utf-8"
    )
    return tmp_path


def _write_run(root: Path, record: dict) -> None:
    run_dir = root / "docs" / "state" / "sample" / "runs"
    (run_dir / f"{record['run_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def test_report_module_is_available_at_head():
    assert importlib.util.find_spec("reckon.crew.pace_replay") is not None


def test_committed_week_replays_allowances_holds_and_work_split(committed_week):
    report = _replay_project(committed_week, "sample")

    assert report["ok"] is True, report["text"]
    assert report["row_count"] == 3, report
    assert report["allowances"] == {
        "checked": 3,
        "matched": 3,
        "mismatches": [],
        "unmeasured": 0,
        "all_match": True,
    }
    assert report["holds"] == {
        "checked": 3,
        "matched": 3,
        "mismatches": [],
        "unverifiable": 0,
        "all_match": True,
    }
    assert report["split_by_class"] == {
        "implement": {"local": 1, "metered": 1, "unknown": 0, "total": 2},
        "review": {"local": 0, "metered": 1, "unknown": 0, "total": 1},
    }
    assert "implement: local=1 metered=1 unknown=0 total=2" in report["text"]
    assert "review: local=0 metered=1 unknown=0 total=1" in report["text"]


def test_mistuned_lead_is_detected_from_rows_alone(committed_week):
    report = _replay_project(
        committed_week,
        "sample",
        drain_lead_hours=24.0,
    )

    assert report["mistuned"] == {
        "requested": True,
        "candidate": 24.0,
        "detected": True,
        "mismatches": ["run-build-early", "run-build-late", "run-held-review"],
    }
    assert report["allowances"]["all_match"] is False
    assert "mistuned drain_lead_hours=24.0: detected" in report["text"]


def test_a_changed_recorded_allowance_or_hold_is_reported(committed_week):
    path = (
        committed_week / "docs" / "state" / "sample" / "runs" / "run-build-early.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record["pace"]["allowance"]["derived"] = 99.0
    path.write_text(json.dumps(record), encoding="utf-8")

    report = _replay_project(committed_week, "sample")

    assert report["ok"] is False, report
    assert report["allowances"]["mismatches"] == ["run-build-early"]


def test_render_report_accepts_a_replay_result(committed_week):
    report = _replay_project(committed_week, "sample")

    assert _render_report(report) == report["text"]
    assert report["text"].splitlines()[:4] == [
        "rows: 3",
        "allowances: 3/3 reproduced (ok)",
        "holds: 3/3 reproduced (ok)",
        "split by class:",
    ]


def test_an_unobserved_week_clock_is_unmeasured_not_reproduced(committed_week):
    record = _row(
        node="unmeasured",
        role="implement",
        backend="codex",
        local=False,
        utilisation=0.2,
        elapsed=48.0,
    )
    record["pace"]["clocks"]["seven_day"] = {
        "period": "seven_day",
        "state": "unknown",
        "utilisation": None,
        "observed_at": None,
        "resets_at": None,
        "age_seconds": None,
    }
    record["pace"]["allowance"] = None
    _write_run(committed_week, record)

    report = _replay_project(committed_week, "sample")

    assert report["allowances"]["unmeasured"] == 1
    assert report["allowances"]["matched"] == 3
    assert report["allowances"]["all_match"] is False
    assert report["ok"] is False
    assert "1 unmeasured" in report["text"]


def test_a_measured_week_without_reset_stamp_is_unmeasured(committed_week):
    record = _row(
        node="missing-reset",
        role="implement",
        backend="codex",
        local=False,
        utilisation=0.3,
        elapsed=72.0,
    )
    record["pace"]["clocks"]["seven_day"]["resets_at"] = None
    record["pace"]["allowance"] = None
    _write_run(committed_week, record)

    report = _replay_project(committed_week, "sample")

    assert report["row_count"] == 4
    assert report["allowances"]["unmeasured"] == 1
    assert report["allowances"]["matched"] == 3
    assert report["ok"] is False
    missing = next(
        row for row in report["rows"] if row["run_id"] == "run-missing-reset"
    )
    assert missing["allowance_unmeasured"] is True
    assert missing["allowance_unmeasured_reason"] == (
        "the seven-day clock has no reset stamp"
    )
    assert all(
        row["allowance_match"]
        for row in report["rows"]
        if row["run_id"] != "run-missing-reset"
    )
    assert "no reset stamp" in report["text"]


@pytest.mark.parametrize(
    "reset_stamp",
    ["", "not-a-date", "2026-13-40T99:00Z", 12345, None],
)
def test_malformed_reset_stamps_are_unmeasured_with_the_week_replayed(
    committed_week, reset_stamp
):
    record = _row(
        node="malformed-reset",
        role="implement",
        backend="codex",
        local=False,
        utilisation=0.3,
        elapsed=72.0,
    )
    record["pace"]["clocks"]["seven_day"]["resets_at"] = reset_stamp
    record["pace"]["allowance"] = None
    _write_run(committed_week, record)

    report = _replay_project(committed_week, "sample")

    assert report["row_count"] == 4
    assert report["allowances"]["unmeasured"] == 1
    assert report["allowances"]["matched"] == 3
    assert report["ok"] is False
    malformed = next(
        row for row in report["rows"] if row["run_id"] == "run-malformed-reset"
    )
    assert malformed["allowance_unmeasured"] is True
    assert malformed["allowance_unmeasured_reason"]
    assert all(
        row["allowance_match"]
        for row in report["rows"]
        if row["run_id"] != "run-malformed-reset"
    )


def test_a_hold_without_threshold_evidence_is_unverifiable(committed_week):
    record = _row(
        node="bare-hold",
        role="review",
        backend="codex",
        local=False,
        utilisation=0.95,
        elapsed=96.0,
        hold={"backend": "codex", "held": True},
    )
    _write_run(committed_week, record)

    report = _replay_project(committed_week, "sample")

    assert report["holds"]["unverifiable"] == 1
    assert report["holds"]["matched"] == 3
    assert report["holds"]["all_match"] is False
    assert report["ok"] is False
    assert "1 unverifiable" in report["text"]


def test_a_row_without_lane_identity_is_in_the_unknown_split(committed_week):
    record = _row(
        node="unknown-lane",
        role="implement",
        backend="codex",
        local=False,
        utilisation=0.3,
        elapsed=72.0,
    )
    record.pop("backend")
    record.pop("local")
    record["pace"].pop("lane")
    record["pace"].pop("member")
    _write_run(committed_week, record)

    report = _replay_project(committed_week, "sample")

    assert report["split_by_class"]["implement"] == {
        "local": 1,
        "metered": 1,
        "unknown": 1,
        "total": 3,
    }


# The values a damaged row is made of.  One of them is a legal value in some
# fields -- a lane may be named "x" -- so a case decides per field whether the
# candidate is a value the producer writes there or damage.
_GARBAGE: tuple[object, ...] = (None, "x", [], {}, -1)

# A declared wallet, so the producer writes a paced row rather than the row of a
# lane that declares no group.
_CONFIG: dict = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
            "budget_group": "sol",
            "fallback": "beta",
        },
        "beta": {
            "launch": "cli",
            "command": "claude",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
            "budget_group": "other",
        },
    },
    "roles": {"implement": {}, "review": {}, "verify": {}, "investigate": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "coordinator_reserve_pct": 3,
        "drain_lead_hours": 12.0,
        "pace_multiple": 1.25,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _stamp(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat()


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _figure(value: object) -> bool:
    return isinstance(value, float)


def _object(value: object) -> bool:
    return isinstance(value, dict) and bool(value)


def _null(value: object) -> bool:
    return value is None


def _refused(value: object) -> bool:
    return False


def _observed_word(value: object) -> bool:
    return value == "observed"


# The values a reader admits at each path, written as the predicate that admits
# a candidate.  A candidate one of these admits is a value the producer itself
# writes in a row that still measures, so the row must NOT read unmeasured: the
# reader may only refuse a value it cannot use.  Every path not listed admits
# nothing, so a field a later producer starts writing is swept as damage here
# without an edit to this table.
_WRITABLE: dict[str, tuple] = {
    "lane": (_text,),
    "node": (_text,),
    "group": (_text,),
    "state": (_text,),
    "source": (_text,),
    "member": (_text,),
    "reason": (_text, _null),
    "score": (_figure,),
    "policy": (_object,),
    "clocks": (_object,),
    "policy.drain_lead_hours": (_figure,),
    "policy.pace_multiple": (_figure,),
    "clocks.*": (_object,),
    "clocks.*.period": (_text,),
    "clocks.*.utilisation": (_figure,),
    "clocks.*.age_seconds": (_figure, _null),
    "clocks.five_hour.state": (_text,),
    # The seven-day clock's own state is what makes a row measurable at all, so
    # the one text admitted there is the word that says it was observed.  Any
    # other word -- damage, or the producer's own "unknown" -- leaves the row
    # unmeasured, which is the outcome a case expects either way.
    "clocks.seven_day.state": (_observed_word,),
    "hold": (_object, _null),
}


def _writable(path: tuple[str, ...], value: object) -> bool:
    """Whether a candidate is a value the producer can write at that path."""
    name = ".".join(path)
    if name not in _WRITABLE and len(path) == 3 and path[0] == "clocks":
        name = f"clocks.*.{path[2]}"
    return any(admits(value) for admits in _WRITABLE.get(name, (_refused,)))


def _row_paths(row: dict, prefix: tuple[str, ...] = ()):
    """Every field of a row, and every leaf of the containers derived from.

    The replay reads a row's figures wherever they sit, so the sweep descends
    into the blocks it derives from -- the policy and each metered clock -- and
    reaches the leaves a later figure would be written as.  The blocks a row
    carries beside its derivation, the recorded allowance, bar and hold, are read
    whole and compared whole, so they are swept as values: a changed one is
    already reported as a disagreement by the sibling tests.
    """
    for key, value in row.items():
        path = (*prefix, key)
        yield path
        if isinstance(value, dict) and (
            key in ("policy", "clocks") or prefix[:1] == ("clocks",)
        ):
            yield from _row_paths(value, path)


def _at(row: dict, path: tuple[str, ...]):
    value = row
    for key in path:
        value = value[key]
    return value


def _put(row: dict, path: tuple[str, ...], value: object) -> None:
    _at(row, path[:-1])[path[-1]] = value


def _producer_records(tmp_path: Path, *, nodes: tuple[str, ...]) -> list[dict]:
    """Records whose pace rows were written by the producer, one per node.

    The row's shape is the producer's to decide, so a case that hand-built one
    would sweep the fields this test remembered rather than the fields a
    dispatch records.  The wallet is observed through a receipt planted on a
    live pointer, as a run in flight records it.
    """
    from reckon import budget
    from reckon.crew import runs

    observed_at = _stamp(0)
    receipt = {
        "quota_state": "measured",
        "observed_at": observed_at,
        "quota_windows": [
            {
                "window_minutes": minutes,
                "used_percent": percent,
                "resets_at": int(
                    (datetime.now(UTC) + timedelta(hours=hours)).timestamp()
                ),
                "observed_at": observed_at,
            }
            for minutes, percent, hours in ((300, 22.0, 4.0), (10080, 45.0, 100.0))
        ],
    }
    runs.live_dir().mkdir(parents=True, exist_ok=True)
    (runs.live_dir() / "r-lane-wallet.json").write_text(
        json.dumps(
            {
                "run_id": "r-lane-wallet",
                "project": "sample",
                "backend": "alpha",
                "phase": "running",
                "pid": os.getpid(),
                "created_at": observed_at,
                "observed_at": observed_at,
                "lane_receipt": receipt,
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "root"
    root.mkdir()
    return [
        {
            "run_id": f"run-{node}",
            "role": "implement",
            "backend": "alpha",
            "local": False,
            "pace": budget.pace_row(
                _CONFIG,
                project="sample",
                lane="alpha",
                node=node,
                score=0.3,
                root=root,
            ),
        }
        for node in nodes
    ]


def test_a_damaged_field_marks_its_row_and_never_the_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """One damaged field yields one marked row, never a dead report.

    Each case replaces one field of a producer-written pace row, and requires
    three things of the week's replay: it returns at all, the row carrying the
    damage is marked rather than silently derived, and the rows nobody touched
    replay exactly as they did before.  The mark is "unmeasured with a reason"
    wherever the candidate is not a value the producer writes at that path --
    every candidate for a figure, a stamp or a block, and four of the five for a
    label.  A candidate the producer does write there is held to the other half
    of the reader's contract: the row stays measurable, because refusing a value
    the module can use would cost a week its report over a healthy row.  The
    fields swept are the row's own keys, so a field the producer starts writing
    is covered here without an edit.
    """
    from reckon.crew.pace_replay import replay

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    subject, control = _producer_records(tmp_path, nodes=("subject", "control"))

    intact = replay([subject, control])
    assert intact["allowances"]["all_match"] is True, intact["text"]

    paths = list(_row_paths(subject["pace"]))
    assert len({path[0] for path in paths}) == len(subject["pace"])

    for path in paths:
        original = _at(subject["pace"], path)
        for candidate in _GARBAGE:
            if candidate is None and original is None:
                # Not a mutation: a null hold and a null reason are values a
                # dispatch that was not held records, not damage.
                continue
            mutated = copy.deepcopy(subject)
            _put(mutated["pace"], path, candidate)

            report = replay([mutated, control])

            damaged = next(
                row for row in report["rows"] if row["run_id"] == "run-subject"
            )
            untouched = next(
                row for row in report["rows"] if row["run_id"] == "run-control"
            )
            if _writable(path, candidate):
                assert damaged["allowance_unmeasured"] is False, (path, candidate)
            else:
                assert damaged["allowance_unmeasured"] is True, (path, candidate)
                assert damaged["allowance_match"] is False, (path, candidate)
                assert damaged["allowance_unmeasured_reason"], (path, candidate)
                assert report["allowances"]["all_match"] is False, (path, candidate)
            assert untouched["allowance_unmeasured"] is False, (path, candidate)
            assert untouched["allowance_match"] is True, (path, candidate)
