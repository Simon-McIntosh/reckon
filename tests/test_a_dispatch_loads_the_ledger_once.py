"""A dispatch reads one ledger snapshot and releases it before the next call."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import subprocess
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import capabilities, cli, crew_dispatch_commands, ledger
from reckon.crew import dispatch_admission, dispatch_plan, picker
from tests import test_dispatch_names_its_backend as backend_cases
from tests import test_picker_in_dispatch as picker_cases

pytest_plugins = ("tests.test_dispatch_names_its_backend",)


def _row(index):
    return {
        "run_id": f"record-{index}",
        "project": "proj",
        "node": "snapshot-dispatch",
        "plan": ("plan-a", "plan-b", "plan-c")[index % 3],
        "section": "dispatch",
        "role": "implement",
        "gate": "passed",
        "backend": "alpha",
        "agent": {"backend": "alpha", "model": "default-model"},
        "completed_at": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def populated(dispatch_repo, monkeypatch):
    repo = dispatch_repo
    source = ledger.ledger_path("proj", repo)
    source.parent.mkdir(parents=True, exist_ok=True)
    rows = [_row(index) for index in range(200)]
    source.write_text(
        json.dumps({"data": {"_version": 1, "members": [], "runs": rows, "holds": []}})
    )
    plan = repo / "docs/plans/plan-a.html"
    plan.write_text(
        plan.read_text().replace(
            "</body>",
            '<section data-reckon="section" data-id="dispatch" data-capability-version="1.0" data-capability-class="general" data-capability-reasoning="standard" data-capability-verification="strict" data-capability-risk="moderate" data-effort-hours="0.25" data-status="implementable"></section></body>',
        )
    )
    for args in (
        ["add", "docs/plans/plan-a.html"],
        [
            "commit",
            "-qm",
            "test: declare section",
            "-m",
            "Supply section routing metadata.",
        ],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    config = copy.deepcopy(backend_cases.CONFIG)
    config["default_backend"] = "clive"
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    backend_cases._observe_window(monkeypatch)
    picker_cases._fast_pick_collaborators(monkeypatch)
    monkeypatch.setattr(
        picker.snapshot, "estimated_context_tokens", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        picker.client,
        "ask",
        lambda *_a, **_k: {
            "answers": {
                "route": {
                    "choice": "beta",
                    "confidence": 1.0,
                    "probabilities": {"beta": 1.0},
                }
            }
        },
    )
    monkeypatch.setattr(
        capabilities,
        "load_capabilities",
        lambda *_a, **_k: {
            "ledger_versions": {"proj": "unmatched"},
            "configurations": [],
        },
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 2, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(dispatch_admission, "datetime", Clock)
    monkeypatch.setattr(dispatch_admission, "_utc_now", lambda: "2026-01-02T00:00:00Z")
    monkeypatch.setattr(
        dispatch_plan, "new_run_id", lambda _node: "r-fixed-snapshot-dispatch"
    )
    # Prime the index before measuring the repeated reads of an unchanged ledger.
    assert len(ledger.load("proj", repo)[0]["runs"]) == 200
    return repo, source


def _invoke(repo, route="picker"):
    result = CliRunner().invoke(
        cli.main,
        [*backend_cases._arguments(repo, node="snapshot-dispatch"), "--route", route],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _record(monkeypatch, source):
    calls = []
    stats = Counter()
    real_load = ledger.load
    real_stat = os.stat

    def load(*args, **kwargs):
        calls.append("".join(traceback.format_stack(limit=5)))
        return real_load(*args, **kwargs)

    def stat(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            candidate = Path(os.fsdecode(path))
            if candidate == source or candidate.parent == source.parent / "runs":
                stats[str(candidate)] += 1
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(ledger, "load", load)
    monkeypatch.setattr(os, "stat", stat)
    return calls, stats


def test_one_dispatch_loads_the_ledger_once(populated, monkeypatch):
    repo, source = populated
    with monkeypatch.context() as recording:
        calls, stats = _record(recording, source)
        payload = _invoke(repo)
    with monkeypatch.context() as live:
        if hasattr(ledger, "dispatch_snapshot"):
            live.setattr(
                ledger, "dispatch_snapshot", lambda **_kwargs: contextlib.nullcontext()
            )
        expected = _invoke(repo)
    print(f"ledger_loads={len(calls)} ledger_stats={dict(stats)}")
    print("DRY_RUN_DOCUMENT=" + json.dumps(payload, sort_keys=True))
    for call in calls:
        print(call)
    assert payload == expected
    assert len(calls) == 1
    assert stats and max(stats.values()) <= 1


def test_the_next_dispatch_sees_a_new_row(populated, monkeypatch):
    repo, source = populated
    first = _invoke(repo, route="deterministic")
    data = json.loads(source.read_text())
    data["data"]["runs"].append(_row(201))
    data["data"]["_version"] += 1
    source.write_text(json.dumps(data))
    second = _invoke(repo, route="deterministic")
    assert "attempts" in first["section_routing"], first["section_routing"]
    assert (
        second["section_routing"]["attempts"]
        == first["section_routing"]["attempts"] + 1
    )


def test_a_snapshot_keeps_rows_consistent_and_releases_them(populated):
    repo, source = populated
    with ledger.dispatch_snapshot():
        first, _ = ledger.dispatch_read("proj", repo)
        data = json.loads(source.read_text())
        data["data"]["runs"].append(_row(201))
        data["data"]["_version"] += 1
        source.write_text(json.dumps(data))
        # Readers of the open dispatch must agree even when another writer lands.
        assert len(ledger.picker_runs("proj", repo)) == len(first["runs"])
        first["runs"].clear()
        assert len(ledger.dispatch_read("proj", repo)[0]["runs"]) == 200
    assert len(ledger.picker_runs("proj", repo)) == 201
    assert len(ledger.indexed_headers("proj", repo)[0]["runs"]) == 201


def test_direct_dispatch_releases_its_snapshot_on_refusal(populated, monkeypatch):
    from reckon.crew.dispatching import preparation
    from reckon.crew.node import CrewError, TaskNode

    repo, _source = populated
    observed = []

    def refuse(*_args):
        observed.append(ledger.dispatch_snapshot_active())
        assert len(ledger.dispatch_read("proj", repo)[0]["runs"]) == 200
        raise CrewError("synthetic dispatch refusal")

    monkeypatch.setattr(preparation, "resolve_project_repository", refuse)
    with pytest.raises(CrewError, match="synthetic dispatch refusal"):
        preparation.dispatch(
            node=TaskNode(
                id="probe", plan="plan-a", goal="probe", done_when="pytest reports"
            ),
            project="proj",
            repo=repo,
            config={},
            session="probe",
        )
    assert observed == [True]
    assert not ledger.dispatch_snapshot_active()


def test_the_picker_thread_joins_the_dispatch_snapshot(populated, monkeypatch):
    repo, source = populated
    observed = []

    def read_in_picker(*_args, **_kwargs):
        observed.append(
            (ledger.dispatch_snapshot_active(), len(ledger.picker_runs("proj", repo)))
        )
        return picker_cases.selection()

    monkeypatch.setattr(picker, "pick", read_in_picker)
    calls, _stats = _record(monkeypatch, source)
    _invoke(repo)
    assert observed == [(True, 200)]
    assert len(calls) == 1
    assert not ledger.dispatch_snapshot_active()
